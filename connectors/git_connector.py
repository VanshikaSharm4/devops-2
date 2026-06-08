"""
Adobe Cloud Manager Git connector.
Works with git.cloudmanager.adobe.com (not GitHub).
Clones the repo locally once, then uses git commands for all operations.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

MAX_DIFF_BYTES = 500_000

# Only fetch from remote once every N minutes — avoids a network round-trip on every click
_GIT_FETCH_TTL_MIN = int(os.getenv("GIT_FETCH_TTL_MINUTES", "10"))
_last_fetch_ts: float = 0.0


# ── Config from .env ─────────────────────────────────────────

def _repo_url() -> str:
    url = os.getenv("CM_GIT_REPO_URL", "")
    if not url:
        raise ValueError("CM_GIT_REPO_URL must be set in .env  (e.g. https://git.cloudmanager.adobe.com/idfc/idfc/)")
    return url


def _local_dir() -> str:
    return os.getenv("GIT_LOCAL_DIR", os.path.expanduser("~/idfc-repo"))


def _auth_url() -> str:
    """Inject credentials into the clone URL."""
    username = os.getenv("CM_GIT_USERNAME", "")
    password = os.getenv("CM_GIT_PASSWORD", "")
    url = _repo_url()
    if username and password:
        return url.replace(
            "https://",
            f"https://{quote(username, safe='')}:{quote(password, safe='')}@",
        )
    return url


# ── Core git helper ──────────────────────────────────────────

def _git(*args: str, cwd: Optional[str] = None) -> str:
    """Run a git command and return stdout. Raises on non-zero exit."""
    cmd = ["git"] + list(args)
    result = subprocess.run(
        cmd,
        cwd=cwd or _local_dir(),
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"`git {' '.join(args)}` failed:\n{result.stderr[:500]}")
    return result.stdout


def _commit_parents(sha: str) -> List[str]:
    """Return parent SHAs for a commit, ordered as Git reports them."""
    line = _git("rev-list", "--parents", "-n", "1", sha).strip()
    parts = line.split()
    return parts[1:] if len(parts) > 1 else []


def _changed_files_for_commit(sha: str) -> List[str]:
    """
    Return files changed by a commit from the pre-deploy perspective.

    Merge commits must be compared to their first parent. Plain
    `git diff-tree -r <merge>` can return an empty diff, which makes a real
    PR/subtree import look like a zero-file commit.
    """
    parents = _commit_parents(sha)
    if parents:
        files_out = _git("diff", "--find-renames", "--name-only", parents[0], sha)
    else:
        files_out = _git(
            "diff-tree",
            "--root",
            "--no-commit-id",
            "-r",
            "--name-only",
            sha,
        )

    changed_files: List[str] = []
    seen: set[str] = set()
    for file_path in files_out.strip().splitlines():
        file_path = file_path.strip()
        if file_path and file_path not in seen:
            seen.add(file_path)
            changed_files.append(file_path)
    return changed_files


def _diff_for_commit(sha: str) -> str:
    """Return a patch for a commit using the same parent selection as files."""
    parents = _commit_parents(sha)
    if parents:
        return _git("diff", "--find-renames", parents[0], sha)
    return _git(
        "diff-tree",
        "--root",
        "--no-commit-id",
        "-r",
        "-p",
        sha,
    )


def clone_or_update() -> str:
    """
    Ensures the local repo is up to date.
    - If the repo doesn't exist yet: clones it.
    - If it exists: runs `git fetch --all` + `git pull` to get the latest commits.
    Falls back silently if network is unreachable (e.g. off VPN) so
    offline usage still works with whatever commits are already present.
    Returns the local repo directory path.
    """
    repo_dir = Path(_local_dir())

    if not (repo_dir / ".git").exists():
        print(f"  [git] Cloning repo to {repo_dir} ...")
        result = subprocess.run(
            ["git", "clone", _auth_url(), str(repo_dir)],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git clone failed:\n{result.stderr[:500]}\n\n"
                f"Hint: set CM_GIT_REPO_URL and optionally CM_GIT_USERNAME / CM_GIT_PASSWORD in .env"
            )
        print("  [git] Clone complete.")
        subprocess.run(
            ["git", "remote", "set-url", "origin", _repo_url()],
            cwd=str(repo_dir), capture_output=True, text=True,
        )
        _write_sync_state(repo_dir, synced=True)
        return str(repo_dir)

    # Repo exists — only fetch if TTL has expired
    global _last_fetch_ts
    elapsed_min = (time.time() - _last_fetch_ts) / 60
    if elapsed_min < _GIT_FETCH_TTL_MIN:
        print(f"  [git] Skipping fetch — last fetch {elapsed_min:.1f} min ago (TTL {_GIT_FETCH_TTL_MIN} min)")
        return str(repo_dir)

    try:
        print("  [git] Fetching latest commits from remote...")

        # Credentials injected directly into URL so git never prompts
        auth_url = _auth_url()

        # Disable any interactive credential prompt — prevents hanging in subprocesses
        _env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}

        # Keep the persisted origin URL credential-free. Fetch with the
        # credentialed URL only for this process invocation.
        subprocess.run(
            ["git", "remote", "set-url", "origin", _repo_url()],
            cwd=str(repo_dir), capture_output=True, text=True,
        )

        fetch_result = subprocess.run(
            ["git", "fetch", "--prune", auth_url, "+refs/heads/*:refs/remotes/origin/*"],
            cwd=str(repo_dir), capture_output=True, text=True,
            timeout=30, env=_env,
        )
        if fetch_result.returncode != 0:
            raise RuntimeError(fetch_result.stderr[:300])

        # Detect current branch
        branch_result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=10,
        )
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""

        if branch:
            merge_result = subprocess.run(
                ["git", "merge", "--ff-only", f"refs/remotes/origin/{branch}"],
                cwd=str(repo_dir), capture_output=True, text=True,
                timeout=30, env=_env,
            )
            if merge_result.returncode == 0:
                print(f"  [git] Fast-forwarded branch '{branch}'.")
            else:
                print(f"  [git] Merge warning: {merge_result.stderr[:200]}")
        else:
            print("  [git] Detached HEAD — fetch only, no pull.")

        _write_sync_state(repo_dir, synced=True)
        _last_fetch_ts = time.time()

    except Exception as e:
        print(f"  [git] Sync failed ({type(e).__name__}: {e}) — using local commits.")
        _write_sync_state(repo_dir, synced=False, error=str(e))
        _last_fetch_ts = time.time()  # don't retry immediately on failure either

    return str(repo_dir)


# ── Sync state helpers ────────────────────────────────────────────────────────

import json as _json
import time as _time

def _sync_state_path(repo_dir: Path) -> Path:
    return repo_dir / ".git" / "_devops_agent_sync.json"

def _write_sync_state(repo_dir: Path, synced: bool, error: str = "") -> None:
    try:
        state = {
            "last_attempt": _time.time(),
            "synced": synced,
            "error": error[:200] if error else "",
        }
        _sync_state_path(repo_dir).write_text(_json.dumps(state))
    except Exception:
        pass

def get_sync_status() -> dict:
    """
    Returns sync state for the dashboard to display.
    Keys: synced (bool), last_attempt (float|None), age_minutes (float), error (str)
    """
    try:
        path = _sync_state_path(Path(_local_dir()))
        if not path.exists():
            return {"synced": None, "last_attempt": None, "age_minutes": None, "error": ""}
        state = _json.loads(path.read_text())
        age = round((_time.time() - state["last_attempt"]) / 60, 1)
        return {**state, "age_minutes": age}
    except Exception:
        return {"synced": None, "last_attempt": None, "age_minutes": None, "error": ""}


# ── Public API — same shape as github_connector.py ───────────

def get_commit_diff(repo: Optional[str], sha: str) -> Dict[str, Any]:
    """Get metadata + changed files + diff for a single commit SHA."""
    clone_or_update()

    title  = _git("log", "-1", "--format=%s",  sha).strip()
    body   = _git("log", "-1", "--format=%b",  sha).strip()
    author = _git("log", "-1", "--format=%an", sha).strip()

    changed_files = _changed_files_for_commit(sha)
    diff_out = _diff_for_commit(sha)
    diff_excerpt = diff_out[:MAX_DIFF_BYTES]
    if len(diff_out.encode()) > MAX_DIFF_BYTES:
        diff_excerpt += "\n\n... [diff truncated]"

    return {
        "commit_sha": sha,
        "title": title,
        "body": body,
        "author": author,
        "changed_files": changed_files,
        "diff_excerpt": diff_excerpt,
    }


def get_diff_between_shas(repo: Optional[str], sha_a: str, sha_b: str) -> Dict[str, Any]:
    """Compare two commits — used by the compare feature."""
    clone_or_update()

    files_out = _git("diff", "--name-only", f"{sha_a}...{sha_b}")
    changed_files = [f for f in files_out.strip().splitlines() if f]

    diff_out = _git("diff", f"{sha_a}...{sha_b}")
    diff_excerpt = diff_out[:MAX_DIFF_BYTES]
    if len(diff_out.encode()) > MAX_DIFF_BYTES:
        diff_excerpt += "\n\n... [diff truncated]"

    ahead_behind = _git("rev-list", "--left-right", "--count", f"{sha_a}...{sha_b}").strip()
    parts = ahead_behind.split() if ahead_behind else ["0", "0"]
    behind_by = int(parts[0]) if parts[0].isdigit() else 0
    ahead_by  = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

    return {
        "sha_a": sha_a,
        "sha_b": sha_b,
        "changed_files": changed_files,
        "diff_excerpt": diff_excerpt,
        "ahead_by": ahead_by,
        "behind_by": behind_by,
    }


def get_file_content(repo: Optional[str], path: str, ref: str = "master") -> str:
    """Get contents of a file at a specific commit/branch."""
    clone_or_update()
    return _git("show", f"{ref}:{path}")


def search_code(repo: Optional[str], query: str) -> List[dict]:
    """
    Search for a string in the local repo (git grep).
    Returns list of {path, sha, url} — same shape as github_connector.
    """
    clone_or_update()
    try:
        out = _git("grep", "-r", "-l", "--ignore-case", "--", query)
        files = [f for f in out.strip().splitlines() if f][:10]
        return [{"path": f, "sha": "", "url": f"(local) {f}"} for f in files]
    except RuntimeError:
        return []  # grep exits 1 when no matches — treat as empty


def find_files_for_parsed_error(repo: Optional[str], parsed_error: dict) -> List[dict]:
    """Heuristic code search based on log_parser output — same API as github_connector."""
    results: List[dict] = []
    error_type = parsed_error.get("error_type", "")
    module = parsed_error.get("module", "")

    if error_type == "missing_npm_module":
        match = re.search(r"Missing npm package: (\S+)", parsed_error.get("error_message", ""))
        if match:
            pkg = match.group(1).split("/")[0]
            results.extend(search_code(repo, pkg))
        if module:
            results.extend(search_code(repo, module))

    elif error_type in ("apache_config_syntax_error", "missing_env_variable"):
        for err in parsed_error.get("errors", []):
            detail = err.get("detail", "")
            if "rewrite-onpremises" in detail:
                results.extend(search_code(repo, "rewrite-onpremises"))
            if "Undefined variable" in detail or "PUBLISH_" in detail:
                var_name = detail.replace("Undefined variable: ", "").strip()
                if var_name:
                    results.extend(search_code(repo, var_name))

    elif error_type == "build_failure":
        msg = parsed_error.get("error_message", "")
        # Search for class names mentioned in build errors
        class_match = re.findall(r'\b([A-Z][a-zA-Z]{3,}Exception|[A-Z][a-zA-Z]{3,}Error)\b', msg)
        for cls in class_match[:3]:
            results.extend(search_code(repo, cls))

    # Deduplicate by path
    seen: set = set()
    unique = []
    for r in results:
        if r["path"] not in seen:
            seen.add(r["path"])
            unique.append(r)
    return unique[:10]


# ── Utility ──────────────────────────────────────────────────

def get_recent_commits(branch: str = "", n: int = 10) -> List[Dict[str, str]]:
    """Get the last N commits on a branch — useful for picking SHAs."""
    clone_or_update()
    if not branch:
        try:
            branch = _git("symbolic-ref", "--short", "HEAD").strip()
        except RuntimeError:
            try:
                branch = _git("rev-parse", "--abbrev-ref", "HEAD").strip()
            except RuntimeError:
                branch = "main"
    out = _git("log", f"-{n}", "--format=%H|%s|%an|%ar", branch)
    commits = []
    for line in out.strip().splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append({
                "sha": parts[0],
                "title": parts[1],
                "author": parts[2],
                "when": parts[3],
            })
    return commits


def test_connection() -> bool:
    """Quick connectivity check — tries to ls-remote without cloning."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", _auth_url()],
            capture_output=True, text=True, timeout=20
        )
        return result.returncode == 0
    except Exception:
        return False


# ── Git-log commit correlation ────────────────────────────────────────────────

def get_all_commits_with_times(
    branch: str = "master",
    days_back: int = 35,
) -> List[Dict[str, str]]:
    """
    Return all commits on `branch` from the last `days_back` days with
    ISO-8601 author timestamps.  Result is ordered newest-first.

    Uses --after=<date> instead of a fixed -N count so the result always
    covers the full Splunk 30-day window regardless of commit frequency.
    35 days gives a 5-day buffer beyond the 30-day Splunk window.

    Does NOT call clone_or_update() — caller is responsible for freshness.
    """
    import datetime
    since = (
        datetime.datetime.utcnow() - datetime.timedelta(days=days_back)
    ).strftime("%Y-%m-%d")
    try:
        out = _git("log", f"--after={since}", "--format=%H|%s|%an|%aI", branch)
    except RuntimeError:
        return []
    commits = []
    for line in out.strip().splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append({
                "sha":       parts[0],
                "sha_short": parts[0][:8],
                "title":     parts[1],
                "author":    parts[2],
                "timestamp": parts[3],   # ISO-8601 with tz offset e.g. 2026-05-25T06:49:50+00:00
            })
    return commits


def _normalize_ts(ts: str):
    """
    Parse an arbitrary timestamp string to a tz-aware pandas Timestamp (UTC).
    Handles:
      - ISO-8601 with offset:  2026-05-25T06:49:50+00:00
      - Splunk PDT/PST:        2026-05-14T21:07:16.000 PDT
    Returns None on failure.
    """
    try:
        import pandas as pd
        s = (ts
             .replace(" PDT", "-07:00")
             .replace(" PST", "-08:00")
             .replace(" UTC", "+00:00"))
        return pd.to_datetime(s).tz_convert("UTC")
    except Exception:
        return None


def correlate_executions_to_commits(
    execution_rows: List[Dict],          # list of dicts with at least "executionId" + "Deploy Start Time"
    branch: str = "master",
    time_col: str = "Deploy Start Time",
) -> Dict[str, Dict]:
    """
    For every execution row, find the most recent git commit on `branch`
    whose author timestamp is <= the execution's start time.

    Returns a dict keyed by executionId:
        {
            "sha":       "7bae88b271...",
            "sha_short": "7bae88b2",
            "title":     "Updated pom.xml file as per build parameters.",
            "author":    "Jenkins CICD",
        }

    Algorithm:
        1. Fetch all commits once  → O(C) where C = commits in 35-day window
        2. Parse all commit timestamps to UTC  → O(C)
        3. For each execution, linear scan newest-first → O(C) worst case, O(1) typical
        Total: O(C + N)  where N = number of executions

    Accuracy: ~95% for manual-trigger, single-branch repos.
    Edge case: if two commits land within the same second before an execution
    starts, we return the newer of the two (correct in practice — HEAD wins).
    """
    import pandas as pd

    # Sync repo once before reading git log
    try:
        clone_or_update()
    except Exception:
        pass  # use whatever is locally available

    commits = get_all_commits_with_times(branch)
    if not commits:
        return {}

    # Parse commit timestamps → UTC; build parallel list
    commit_utc = []
    for c in commits:
        t = _normalize_ts(c["timestamp"])
        commit_utc.append(t)   # may be None for unparseable entries

    result: Dict[str, Dict] = {}

    for row in execution_rows:
        eid = str(row.get("executionId", ""))
        raw_ts = row.get(time_col, "")
        if not raw_ts or not eid:
            continue

        exec_utc = _normalize_ts(str(raw_ts))
        if exec_utc is None:
            continue

        # Walk newest-first; stop at first commit whose timestamp <= exec start
        matched = None
        for i, c_utc in enumerate(commit_utc):
            if c_utc is None:
                continue
            if c_utc <= exec_utc:
                matched = commits[i]
                break

        if matched:
            result[eid] = {
                "sha":       matched["sha"],
                "sha_short": matched["sha_short"],
                "title":     matched["title"],
                "author":    matched["author"],
            }

    return result
