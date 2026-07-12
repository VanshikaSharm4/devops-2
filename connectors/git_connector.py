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

# Per-repo fetch TTL — keyed by repo_dir so HDFC TTL doesn't block IDFC
_GIT_FETCH_TTL_MIN = int(os.getenv("GIT_FETCH_TTL_MINUTES", "10"))
_last_fetch_ts: dict = {}   # {repo_dir: timestamp}

# Dedup: track which repos were already checked in this Python process invocation.
# clone_or_update() is called by every git function independently.  Within a single
# analysis run (one Streamlit request) they all fire within milliseconds — the TTL
# skip is cheap but still prints 5+ log lines.  A per-repo "checked this tick" flag
# silences the noise without changing any behaviour.
_checked_this_tick: dict = {}  # {repo_dir: timestamp}  — cleared every 30s
_TICK_WINDOW_S = 30


# ── Config from .env ─────────────────────────────────────────

def _repo_url() -> str:
    url = os.getenv("CM_GIT_REPO_URL", "")
    if not url:
        raise ValueError("CM_GIT_REPO_URL must be set in .env  (e.g. https://git.cloudmanager.adobe.com/idfc/idfc/)")
    return url


def _local_dir(override: Optional[str] = None) -> str:
    if override:
        return override
    return os.getenv("GIT_LOCAL_DIR", "")


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

def _git(*args: str, cwd: Optional[str] = None, repo_dir: Optional[str] = None) -> str:
    """Run a git command and return stdout. Raises on non-zero exit."""
    cmd = ["git"] + list(args)
    result = subprocess.run(
        cmd,
        cwd=cwd or _local_dir(repo_dir),
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"`git {' '.join(args)}` failed:\n{result.stderr[:500]}")
    return result.stdout


def _commit_parents(sha: str, repo_dir: Optional[str] = None) -> List[str]:
    """Return parent SHAs for a commit, ordered as Git reports them."""
    line = _git("rev-list", "--parents", "-n", "1", sha, repo_dir=repo_dir).strip()
    parts = line.split()
    return parts[1:] if len(parts) > 1 else []


def _changed_files_for_commit(sha: str, repo_dir: Optional[str] = None) -> List[str]:
    """
    Return files changed by a commit from the pre-deploy perspective.

    Merge commits must be compared to their first parent. Plain
    `git diff-tree -r <merge>` can return an empty diff, which makes a real
    PR/subtree import look like a zero-file commit.
    """
    parents = _commit_parents(sha, repo_dir=repo_dir)
    if parents:
        files_out = _git("diff", "--find-renames", "--name-only", parents[0], sha, repo_dir=repo_dir)
    else:
        files_out = _git(
            "diff-tree",
            "--root",
            "--no-commit-id",
            "-r",
            "--name-only",
            sha,
            repo_dir=repo_dir,
        )

    changed_files: List[str] = []
    seen: set[str] = set()
    for file_path in files_out.strip().splitlines():
        file_path = file_path.strip()
        if file_path and file_path not in seen:
            seen.add(file_path)
            changed_files.append(file_path)
    return changed_files


def _diff_for_commit(sha: str, repo_dir: Optional[str] = None) -> str:
    """Return a patch for a commit using the same parent selection as files."""
    parents = _commit_parents(sha, repo_dir=repo_dir)
    if parents:
        return _git("diff", "--find-renames", parents[0], sha, repo_dir=repo_dir)
    return _git(
        "diff-tree",
        "--root",
        "--no-commit-id",
        "-r",
        "-p",
        sha,
        repo_dir=repo_dir,
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
        _write_sync_state(repo_dir, synced=True)
        return str(repo_dir)

    # Repo exists — only fetch if TTL has expired (per-repo, not global)
    global _last_fetch_ts, _checked_this_tick
    _repo_key = str(repo_dir)

    # Dedup: if we already checked this repo within the last _TICK_WINDOW_S seconds,
    # skip silently — many git functions call clone_or_update() independently and
    # generate identical "Skipping fetch" log spam within the same analysis request.
    _now = time.time()
    if (_now - _checked_this_tick.get(_repo_key, 0.0)) < _TICK_WINDOW_S:
        return str(repo_dir)
    _checked_this_tick[_repo_key] = _now

    elapsed_min = (_now - _last_fetch_ts.get(_repo_key, 0.0)) / 60
    if elapsed_min < _GIT_FETCH_TTL_MIN:
        return str(repo_dir)  # TTL not expired — silent skip

    try:
        print("  [git] Fetching latest commits from remote...")

        # Credentials injected directly into URL so git never prompts
        auth_url = _auth_url()

        # Disable any interactive credential prompt — prevents hanging in subprocesses
        _env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}

        # Do NOT reset the remote URL — the local clone may be a different customer's
        # repo (e.g. HDFC) while CM_GIT_REPO_URL points to IDFC.
        # The remote URL is set once at clone time and managed by the customer registry.

        fetch_result = subprocess.run(
            ["git", "fetch", "--prune", auth_url, "+refs/heads/*:refs/remotes/origin/*"],
            cwd=str(repo_dir), capture_output=True, text=True,
            timeout=20, env=_env,
        )
        if fetch_result.returncode != 0:
            raise RuntimeError(fetch_result.stderr[:300])

        # Detect current branch — also check GIT_BRANCH env var (customer-specific)
        branch_result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=10,
        )
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""

        # If HEAD is detached or on wrong branch, use the customer's configured branch
        configured_branch = os.getenv("GIT_BRANCH", "").strip()
        target_branch = branch or configured_branch

        if target_branch:
            # Strip "origin/" prefix if present (some configs pass full remote ref)
            _bare = target_branch.replace("origin/", "").strip()
            merge_result = subprocess.run(
                ["git", "merge", "--ff-only", f"refs/remotes/origin/{_bare}"],
                cwd=str(repo_dir), capture_output=True, text=True,
                timeout=30, env=_env,
            )
            if merge_result.returncode == 0:
                print(f"  [git] Fast-forwarded branch '{_bare}'.")
            else:
                # Try checkout + pull for remote-tracking branches (e.g. stage_and_prod)
                _co = subprocess.run(
                    ["git", "checkout", "-B", _bare, f"origin/{_bare}"],
                    cwd=str(repo_dir), capture_output=True, text=True,
                    timeout=30, env=_env,
                )
                if _co.returncode == 0:
                    print(f"  [git] Switched to branch '{_bare}' (fast-forward).")
                else:
                    print(f"  [git] Merge warning: {merge_result.stderr[:200]}")
        else:
            print("  [git] Detached HEAD and no GIT_BRANCH configured — fetch only.")

        _write_sync_state(repo_dir, synced=True)
        _last_fetch_ts[_repo_key] = time.time()

    except Exception as e:
        _err_str = str(e)
        print(f"  [git] Sync failed ({type(e).__name__}: {e}) — using local commits.")
        _write_sync_state(repo_dir, synced=False, error=_err_str)
        # Auth failure: set TTL to 10 min so we don't immediately retry on every call.
        # Retrying an auth failure is pointless — it will fail the same way every time
        # until the token is rotated. Without this, every SHA lookup triggers a full
        # fetch loop that wastes 45+ seconds and blocks the UI.
        if "authentication failed" in _err_str.lower() or "403" in _err_str or "401" in _err_str:
            print(f"  [git] Auth failure detected — suppressing retries for 10 min. "
                  f"Update the git token in data/.secrets.json or .env.")
            _last_fetch_ts[_repo_key] = time.time()  # reset TTL → won't retry for TTL minutes
        _last_fetch_ts[_repo_key] = time.time()  # don't retry immediately on failure either

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

def _sha_exists(sha: str, repo_dir: Optional[str] = None) -> bool:
    """Check if a SHA object exists in the local repo."""
    try:
        result = subprocess.run(
            ["git", "cat-file", "-t", sha],
            cwd=_local_dir(repo_dir), capture_output=True, text=True, timeout=10,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        return result.returncode == 0
    except Exception:
        return False


def _fetch_all(repo_dir: Optional[str] = None) -> None:
    """Fetch all remote branches. Injects credentials from env if available."""
    _env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
    cwd = _local_dir(repo_dir)

    # Build credentialed fetch URL if username+password are available
    username = os.getenv("CM_GIT_USERNAME", "")
    password = os.getenv("CM_GIT_PASSWORD", "")
    fetch_cmd = ["git", "fetch", "--all", "--prune"]

    if username and password:
        # Get current remote URL and inject credentials
        try:
            r = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=cwd, capture_output=True, text=True, timeout=5,
            )
            remote_url = r.stdout.strip()
            if remote_url and "://" in remote_url:
                from urllib.parse import quote as _q
                proto, rest = remote_url.split("://", 1)
                # Strip any existing credentials
                if "@" in rest:
                    rest = rest.split("@", 1)[1]
                auth_url = f"{proto}://{_q(username, safe='')}:{_q(password, safe='')}@{rest}"
                fetch_cmd = ["git", "fetch", "--prune", auth_url, "+refs/heads/*:refs/remotes/origin/*"]
        except Exception:
            pass

    result = subprocess.run(
        fetch_cmd, cwd=cwd, capture_output=True, text=True, timeout=30, env=_env,
    )
    if result.returncode != 0:
        print(f"  [git] fetch failed: {result.stderr[:300]}")


def get_commit_diff(repo: Optional[str], sha: str) -> Dict[str, Any]:
    """Get metadata + changed files + diff for a single commit SHA.
    repo: explicit path to local git clone — overrides GIT_LOCAL_DIR env var.
    """
    # Use explicit repo path if provided, otherwise fall back to env var
    repo_dir = repo or None

    clone_or_update()

    # If SHA not found locally, force a fresh fetch regardless of TTL.
    # The TTL prevents redundant fetches in normal flow, but when a specific SHA
    # is requested and not found, we MUST refetch — the commit was pushed after the
    # last TTL fetch. Reset TTL so clone_or_update fetches immediately.
    if not _sha_exists(sha, repo_dir):
        # ── Targeted fetch first: fetch ONLY this SHA's objects ──────────────
        # Much faster than fetching all branches (+refs/heads/*) which can take
        # 30-60s on large repos. Direct SHA fetch typically completes in 1-3s.
        print(f"  [git] SHA {sha[:12]} not found — trying targeted fetch...")
        _repo_path = str(_local_dir(repo_dir))
        _auth = _auth_url()
        _env  = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
        _targeted_ok = False
        _auth_failed = False
        try:
            _r = subprocess.run(
                ["git", "fetch", "--depth=1", _auth, sha],
                cwd=_repo_path, capture_output=True, text=True,
                timeout=15, env=_env,
            )
            if _r.returncode == 0 and _sha_exists(sha, repo_dir):
                _targeted_ok = True
                print(f"  [git] SHA {sha[:12]} fetched via targeted fetch.")
                _last_fetch_ts[_repo_path] = time.time()
            elif _r.returncode != 0:
                _stderr = (_r.stderr or "").lower()
                if "authentication failed" in _stderr or "403" in _stderr or "401" in _stderr:
                    _auth_failed = True
        except Exception:
            pass

        if _auth_failed:
            # Auth failure — retrying the full fetch will fail the same way.
            # Set TTL so we don't loop. Surface a clear error immediately.
            _last_fetch_ts[_repo_path] = time.time()
            raise RuntimeError(
                f"Git authentication failed for this repository. "
                f"The token has expired — update it in data/.secrets.json or .env "
                f"(Adobe CM → Repositories → Generate password)."
            )

        if not _targeted_ok:
            # Targeted fetch failed (not auth — server doesn't support SHA fetch) —
            # fall back to full branch fetch via clone_or_update
            _last_fetch_ts[_repo_path] = 0.0  # reset TTL → force full fetch
            print(f"  [git] Targeted fetch failed — falling back to full fetch...")
            clone_or_update()

        if not _sha_exists(sha, repo_dir):
            raise RuntimeError(
                f"SHA {sha[:12]} not found after fetch. "
                f"The commit may be on a branch not tracked locally, or credentials may be wrong."
            )

    title       = _git("log", "-1", "--format=%s",  sha, repo_dir=repo_dir).strip()
    body        = _git("log", "-1", "--format=%b",  sha, repo_dir=repo_dir).strip()
    author      = _git("log", "-1", "--format=%an", sha, repo_dir=repo_dir).strip()
    commit_date = _git("log", "-1", "--format=%ai", sha, repo_dir=repo_dir).strip()[:10]  # YYYY-MM-DD

    changed_files = _changed_files_for_commit(sha, repo_dir=repo_dir)
    diff_out = _diff_for_commit(sha, repo_dir=repo_dir)
    diff_excerpt = diff_out[:MAX_DIFF_BYTES]
    if len(diff_out.encode()) > MAX_DIFF_BYTES:
        diff_excerpt += "\n\n... [diff truncated]"

    return {
        "commit_sha":  sha,
        "title":       title,
        "body":        body,
        "author":      author,
        "commit_date": commit_date,
        "changed_files": changed_files,
        "diff_excerpt":  diff_excerpt,
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
    """
    Get the last N commits on a branch — useful for picking SHAs.

    Branch resolution order (handles both local and remote-tracking refs):
    1. Passed branch name as-is (e.g. "stage_and_prod")
    2. "origin/{branch}" — remote-tracking ref (most repos only have this)
    3. Current HEAD branch
    4. "main" / "master" fallback
    """
    clone_or_update()

    # Resolve which ref to read from
    _branch_ref = ""
    if branch:
        # Strip "origin/" prefix if already included — we'll add it if needed
        _bare = branch.replace("origin/", "").strip()
        # Try local branch first, then remote-tracking ref
        for _ref in (_bare, f"origin/{_bare}"):
            try:
                _git("rev-parse", "--verify", _ref)
                _branch_ref = _ref
                break
            except RuntimeError:
                continue

    if not _branch_ref and branch:
        # Branch not found locally or as origin/ ref — try fetching it explicitly.
        # Only attempt if credentials are configured — skip silently if password is empty
        # to avoid auth failure errors and 30-second timeouts.
        _bare = branch.replace("origin/", "").strip()
        _has_creds = bool(os.getenv("CM_GIT_PASSWORD", "").strip())
        if not _has_creds:
            print(f"  [git] Branch '{_bare}' not found locally and no credentials set — skipping fetch")
        else:
            try:
                print(f"  [git] Branch '{_bare}' not found locally — fetching from remote…")
                _env2 = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
                _auth = _auth_url()
                subprocess.run(
                    ["git", "fetch", _auth, f"refs/heads/{_bare}:refs/remotes/origin/{_bare}"],
                    cwd=_local_dir(), capture_output=True, text=True, timeout=30, env=_env2,
                )
                _git("rev-parse", "--verify", f"origin/{_bare}")
                _branch_ref = f"origin/{_bare}"
                print(f"  [git] Fetched branch '{_bare}' → {_branch_ref}")
            except Exception:
                pass

    if not _branch_ref:
        # Fallback — use current HEAD
        try:
            _branch_ref = _git("symbolic-ref", "--short", "HEAD").strip()
        except RuntimeError:
            try:
                _branch_ref = _git("rev-parse", "--abbrev-ref", "HEAD").strip()
            except RuntimeError:
                _branch_ref = "main"

    try:
        out = _git("log", f"-{n}", "--format=%H|%s|%an|%ar", _branch_ref)
    except RuntimeError:
        return []

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
        if branch:
            # Resolve: try local branch first, then origin/ remote-tracking ref
            # If neither exists, fall back to --all so we don't call git log with
            # an unknown ref (which causes "ambiguous argument" fatal error)
            _bare = branch.replace("origin/", "").strip()
            _resolved = ""  # empty = not found yet, will use --all fallback
            for _ref in (_bare, f"origin/{_bare}"):
                try:
                    _git("rev-parse", "--verify", _ref)
                    _resolved = _ref
                    break
                except RuntimeError:
                    continue
            if _resolved:
                out = _git("log", f"--after={since}", "--format=%H|%s|%an|%aI", _resolved)
            else:
                # Branch not found locally or as remote ref — search all refs
                out = _git("log", "--all", f"--after={since}", "--format=%H|%s|%an|%aI")
        else:
            out = _git("log", "--all", f"--after={since}", "--format=%H|%s|%an|%aI")
    except RuntimeError:
        # Branch may not exist locally — fall back to --all
        try:
            out = _git("log", "--all", f"--after={since}", "--format=%H|%s|%an|%aI")
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


# ── Submodule file reader (fetch-only, no clone) ─────────────────────────────
# Reads files from a submodule at a specific SHA by fetching objects directly
# into the PARENT repo's object store — no separate directory, no working copy.
#
# How it works:
#   git -C <parent_repo> fetch --depth=1 <submodule_url> <sha>
#   git -C <parent_repo> show <sha>:path/to/file.java
#
# Disk cost: ~2-10MB of compressed objects added to the parent's .git/objects/
# (which already exists). Same SHA fetched twice = zero additional disk.
# No new directories, no bare repos, no clones.


def read_submodule_file(
    submodule_name: str,
    submodule_sha: str,
    file_path: str,
    remote_url: str = "",
    parent_repo_dir: str = "",
) -> Optional[str]:
    """
    Read a file from a submodule at a specific SHA without cloning.

    Fetches only the needed commit objects into the parent repo's object store,
    then reads the file directly. No working copy, no separate directory.

    Args:
        submodule_name:   e.g. "hdfcbankcustomerinfo" (for logging only)
        submodule_sha:    exact SHA the parent bumped to
        file_path:        path inside submodule e.g. "core/src/main/.../Foo.java"
        remote_url:       authenticated URL for the submodule remote
        parent_repo_dir:  parent repo path (defaults to GIT_LOCAL_DIR env var)

    Returns:
        File content as string, or None if unavailable.
    """
    # ── Strategy 1: Bitbucket REST API (no clone, no disk) ──────────────────
    if remote_url and file_path:
        try:
            from connectors.bitbucket_connector import parse_cm_git_url, read_file_at_sha
            _project, _repo = parse_cm_git_url(remote_url)
            return read_file_at_sha(_project, _repo, submodule_sha, file_path,
                                    os.getenv("CM_GIT_USERNAME", ""),
                                    os.getenv("CM_GIT_PASSWORD", ""))
        except Exception as _api_err:
            print(f"  [git] read_submodule_file API failed ({_api_err}) — falling back to git fetch")

    # ── Strategy 2: fetch objects into parent repo object store ──────────────
    repo = parent_repo_dir or os.getenv("GIT_LOCAL_DIR", "")
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        return None

    _env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}

    # Check if SHA already in parent's object store
    _has = subprocess.run(
        ["git", "-C", repo, "cat-file", "-t", submodule_sha],
        capture_output=True, text=True, timeout=5, env=_env,
    ).returncode == 0

    if not _has and remote_url:
        # Fetch just this commit's objects into parent's .git/objects — no clone
        try:
            subprocess.run(
                ["git", "-C", repo, "fetch", "--depth=1", remote_url, submodule_sha],
                capture_output=True, text=True, timeout=30, env=_env,
            )
        except Exception:
            pass

    if not file_path:
        return None

    # Read file from object store — no working copy needed
    try:
        result = subprocess.run(
            ["git", "-C", repo, "show", f"{submodule_sha}:{file_path}"],
            capture_output=True, text=True, timeout=10, env=_env,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return None


def list_submodule_files(
    submodule_name: str,
    submodule_sha: str,
    directory: str = "",
    remote_url: str = "",
    pattern: str = "*.java",
    parent_repo_dir: str = "",
) -> List[str]:
    """
    List files in a submodule directory at a specific SHA.
    Uses the same fetch-into-parent approach as read_submodule_file.
    """
    # ── Strategy 1: Bitbucket REST API (no clone, no disk) ──────────────────
    if remote_url:
        try:
            from connectors.bitbucket_connector import parse_cm_git_url, list_files_at_sha
            _project, _repo = parse_cm_git_url(remote_url)
            return list_files_at_sha(_project, _repo, submodule_sha,
                                     os.getenv("CM_GIT_USERNAME", ""),
                                     os.getenv("CM_GIT_PASSWORD", ""),
                                     directory=directory, pattern=pattern)
        except Exception as _api_err:
            print(f"  [git] list_submodule_files API failed ({_api_err}) — falling back to git ls-tree")

    # ── Strategy 2: use parent repo object store ──────────────────────────────
    repo = parent_repo_dir or os.getenv("GIT_LOCAL_DIR", "")
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        return []

    # Ensure objects are fetched via read_submodule_file (which also tries API)
    read_submodule_file(submodule_name, submodule_sha, "", remote_url, repo)

    _env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
    try:
        tree_ref = f"{submodule_sha}:{directory}" if directory else submodule_sha
        result = subprocess.run(
            ["git", "-C", repo, "ls-tree", "-r", "--name-only", tree_ref],
            capture_output=True, text=True, timeout=10, env=_env,
        )
        if result.returncode == 0:
            files = result.stdout.strip().splitlines()
            if pattern:
                import fnmatch
                files = [f for f in files if fnmatch.fnmatch(f.split("/")[-1], pattern)]
            return files
    except Exception:
        pass
    return []
