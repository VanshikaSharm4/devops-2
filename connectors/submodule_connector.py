"""
Submodule connector — fetches submodule repos and extracts real diffs.

When the parent repo shows a submodule pointer bump (160000 mode change),
this connector:
  1. Reads repo_config.json to find the submodule URL + local path
  2. Clones the submodule repo if not already present
  3. Fetches the old and new SHAs
  4. Returns the real git diff between them

This gives the LLM actual code to analyze instead of an empty diff.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from analysis.paths import repo_config_path as _repo_config_path, secrets_path as _secrets_path
REPO_CONFIG_PATH = _repo_config_path()
MAX_SUBMODULE_DIFF_BYTES = 200_000   # 200KB cap per submodule diff

# Background refresh: keep submodule bare repos current so SHA lookups succeed.
# Adobe git server doesn't support arbitrary SHA fetches — you must fetch by branch.
# Without periodic refreshes, bare repos go stale and every new SHA lookup misses.
_SM_REFRESH_TTL_MIN  = int(os.getenv("SUBMODULE_REFRESH_TTL_MIN", "60"))  # refresh every 60 min
_sm_last_refresh: Dict[str, float] = {}  # {local_dir: timestamp}


# ── Config loading ─────────────────────────────────────────────────────────────


def _get_ctx(attr: str, env_key: str, default: str = "") -> str:
    """Read customer-specific value from thread-safe context, fallback to os.environ."""
    try:
        import analysis.customer_context as _cc
        val = getattr(_cc, f"get_{attr}")()
        return val if val else os.getenv(env_key, default)
    except Exception:
        return os.getenv(env_key, default)

def load_repo_config() -> dict:
    """Load repo_config.json. Returns empty dict if not found."""
    try:
        if REPO_CONFIG_PATH.exists():
            return json.loads(REPO_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_repo_config(config: dict) -> None:
    """Save repo_config.json."""
    REPO_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPO_CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def get_customer_config(customer_name: str) -> Optional[dict]:
    """Get config for a specific customer."""
    return load_repo_config().get(customer_name)


def get_submodule_config(customer_name: str, submodule_name: str) -> Optional[dict]:
    """Get config for a specific submodule."""
    cfg = get_customer_config(customer_name)
    if not cfg:
        return None
    for sm in cfg.get("submodules", []):
        if sm["name"] == submodule_name:
            return sm
    return None


# ── Git helpers ────────────────────────────────────────────────────────────────

def _git(repo_dir: str, *args: str, timeout: int = 60) -> str:
    path = Path(repo_dir)
    # Bare repos: use --git-dir instead of cwd
    is_bare = (path / "objects").exists() and not (path / ".git").exists()
    if is_bare:
        cmd = ["git", f"--git-dir={repo_dir}"] + list(args)
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"},
        )
    else:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=repo_dir, capture_output=True, text=True,
            timeout=timeout, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"},
        )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[:300])
    return result.stdout


def _auth_url(url: str, username: str, password: str) -> str:
    return url.replace("https://", f"https://{quote(username, safe='')}:{quote(password, safe='')}@")


def _ensure_cloned(url: str, local_dir: str, username: str, password: str) -> bool:
    """Clone submodule repo if not present. Returns True if ready."""
    path = Path(local_dir)
    # Regular clone has .git subfolder; bare clone has HEAD + objects at root
    if (path / ".git").exists() or (path / "objects").exists():
        return True
    path.mkdir(parents=True, exist_ok=True)
    auth_url = _auth_url(url, username, password)
    result = subprocess.run(
        ["git", "clone", "--bare", auth_url, str(path)],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        # Try non-bare clone
        result2 = subprocess.run(
            ["git", "clone", auth_url, str(path)],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        return result2.returncode == 0
    return True


def refresh_all_submodules(customer_name: str, force: bool = False) -> Dict[str, bool]:
    """
    Fetch the default branch for every configured submodule for a customer.
    Run this periodically (background thread) so bare repos stay current.

    Returns {submodule_name: success}.
    This is the correct way to keep submodule repos fresh — NOT SHA-targeted fetches,
    which Adobe's git server rejects. Fetch by branch name, let the objects accumulate.
    """
    import time
    config = get_customer_config(customer_name)
    if not config:
        return {}

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    username = config.get("git_username", "") or _get_ctx("git_username", "CM_GIT_USERNAME")
    password = ""
    try:
        _s = _secrets_path()
        if _s.exists():
            password = json.loads(_s.read_text()).get(customer_name, {}).get("git_password", "")
    except Exception:
        pass
    if not password:
        password = _get_ctx("git_password", "CM_GIT_PASSWORD")
    if not username or not password:
        return {}

    results: Dict[str, bool] = {}
    _now = time.time()

    for sm in config.get("submodules", []):
        sm_name  = sm["name"]
        sm_url   = sm["url"]
        sm_local = sm["local_dir"]
        sm_branch = sm.get("branch", "master")

        # TTL: skip if refreshed recently (unless forced)
        _last = _sm_last_refresh.get(sm_local, 0.0)
        if not force and (_now - _last) / 60 < _SM_REFRESH_TTL_MIN:
            results[sm_name] = True  # assumed still fresh
            continue

        # Ensure bare clone exists
        ok = _ensure_cloned(sm_url, sm_local, username, password)
        if not ok:
            results[sm_name] = False
            continue

        # Fetch branch by name — what Adobe's server actually supports
        auth_url = _auth_url(sm_url, username, password)
        _env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
        try:
            r = subprocess.run(
                ["git", "fetch", auth_url, f"+refs/heads/{sm_branch}:refs/heads/{sm_branch}"],
                cwd=sm_local, capture_output=True, text=True, timeout=25, env=_env,
            )
            success = r.returncode == 0
            if success:
                _sm_last_refresh[sm_local] = _now
                print(f"  [submodule] {sm_name}: refreshed branch '{sm_branch}'")
            else:
                print(f"  [submodule] {sm_name}: fetch failed — {r.stderr[:100]}")
            results[sm_name] = success
        except Exception as e:
            print(f"  [submodule] {sm_name}: fetch error — {e}")
            results[sm_name] = False

    return results


def refresh_all_submodules_background(customer_name: str) -> None:
    """Launch refresh_all_submodules in a daemon thread (non-blocking)."""
    import threading
    t = threading.Thread(
        target=refresh_all_submodules,
        args=(customer_name,),
        daemon=True,
        name=f"sm-refresh-{customer_name}",
    )
    t.start()


def _fetch_sha(local_dir: str, sha: str, url: str, username: str, password: str,
               branch: str = "") -> bool:
    """
    Ensure a specific SHA is available locally.

    Adobe Cloud Manager's git server (Bitbucket-based) does NOT support fetching
    arbitrary SHAs via `git fetch <url> <sha>` — the server only advertises named
    refs (branches/tags). A targeted SHA fetch will silently fail or be rejected.

    Correct strategy:
    1. Check if SHA already present (fast path, ~3ms)
    2. Fetch the branch that likely contains the SHA (using known branch or common defaults)
    3. Fall back to fetching all refs if branch fetch didn't bring the SHA
    """
    try:
        _git(local_dir, "cat-file", "-t", sha, timeout=3)
        return True  # already have it — fast path
    except RuntimeError:
        pass

    auth_url = _auth_url(url, username, password)
    _env = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo", **__import__("os").environ}

    # Strategy 1: fetch the configured/common branch by name (Adobe supports this)
    # This is what Adobe git server actually supports — ref-based, not SHA-based.
    _branches_to_try = []
    if branch:
        _branches_to_try.append(branch)
    # Common AEM submodule branch patterns
    _branches_to_try += ["master", "main", "develop", "release"]

    for _branch in _branches_to_try:
        try:
            result = __import__("subprocess").run(
                ["git", "fetch", "--depth=50", auth_url, _branch],
                cwd=local_dir, capture_output=True, text=True, timeout=20, env=_env,
            )
            if result.returncode == 0:
                try:
                    _git(local_dir, "cat-file", "-t", sha, timeout=3)
                    return True  # SHA is now available
                except RuntimeError:
                    continue  # wrong branch, try next
        except Exception:
            continue

    # Strategy 2: full fetch of all refs — slower but finds SHAs on any branch
    try:
        result = __import__("subprocess").run(
            ["git", "fetch", auth_url, "+refs/heads/*:refs/remotes/origin/*"],
            cwd=local_dir, capture_output=True, text=True, timeout=30, env=_env,
        )
        if result.returncode == 0:
            try:
                _git(local_dir, "cat-file", "-t", sha, timeout=3)
                return True
            except RuntimeError:
                pass
    except Exception:
        pass

    return False


# ── Diff extraction ────────────────────────────────────────────────────────────

def _parse_submodule_changes(diff_text: str) -> List[Tuple[str, str, str]]:
    """
    Parse parent repo diff for submodule pointer changes.

    Returns list of (submodule_path, old_sha, new_sha).
    Submodule changes look like:
      -Subproject commit abc123
      +Subproject commit def456
    or in unified diff format with mode 160000.
    """
    changes = []
    current_path = ""
    old_sha = ""
    new_sha = ""

    for line in diff_text.splitlines():
        # File header
        m = re.match(r'^diff --git a/(.+?) b/', line)
        if m:
            if current_path and old_sha and new_sha and old_sha != new_sha:
                changes.append((current_path, old_sha, new_sha))
            current_path = m.group(1)
            old_sha = new_sha = ""
            continue

        # Subproject commit lines
        if line.startswith("-Subproject commit "):
            old_sha = line.replace("-Subproject commit ", "").strip()
        elif line.startswith("+Subproject commit "):
            new_sha = line.replace("+Subproject commit ", "").strip()

    if current_path and old_sha and new_sha and old_sha != new_sha:
        changes.append((current_path, old_sha, new_sha))

    return changes


def get_submodule_diffs(
    diff_text: str,
    customer_name: str,
) -> Dict[str, str]:
    """
    For a parent repo diff that contains submodule pointer changes,
    fetch the actual code diffs from each changed submodule.

    Returns dict: {submodule_name: diff_text}
    """
    config = get_customer_config(customer_name)
    if not config:
        return {}

    # Load dotenv so env vars from .env file are available
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    username = config.get("git_username", "") or _get_ctx("git_username", "CM_GIT_USERNAME")

    # Try multiple password sources in order of priority
    password = ""
    # 1. Direct env var from git_password_env field
    password_env = config.get("git_password_env", "")
    if password_env:
        password = os.getenv(password_env, "")
    # 2. .secrets.json file
    if not password:
        try:
            _sp = _secrets_path()
            if _sp.exists():
                _secrets = json.loads(_sp.read_text(encoding="utf-8"))
                password = _secrets.get(customer_name, {}).get("git_password", "")
        except Exception:
            pass
    # 3. Short-code based env var (e.g. HDFC_CM_GIT_PASSWORD)
    if not password:
        _short = config.get("short", "").upper()
        if _short:
            password = os.getenv(f"{_short}_CM_GIT_PASSWORD", "")
    # 4. Generic fallback
    if not password:
        password = _get_ctx("git_password", "CM_GIT_PASSWORD")

    if not username or not password:
        print(f"  [submodule] No credentials for '{customer_name}' — skipping submodule diff")
        return {}

    # Build submodule lookup: path → config
    sm_by_path = {sm["name"]: sm for sm in config.get("submodules", [])}
    # Also match by path (submodule path may differ from name)
    sm_by_path.update({sm.get("path", sm["name"]): sm for sm in config.get("submodules", [])})

    submodule_changes = _parse_submodule_changes(diff_text)
    if not submodule_changes:
        return {}

    results: Dict[str, str] = {}

    # Build work list first
    work_items = []
    for sm_path, old_sha, new_sha in submodule_changes:
        sm_name = sm_path.strip("/").split("/")[-1]
        sm_cfg = sm_by_path.get(sm_name) or sm_by_path.get(sm_path)
        if not sm_cfg:
            sm_cfg = next((v for k, v in sm_by_path.items() if sm_name in k or k in sm_name), None)
        if sm_cfg:
            work_items.append((sm_name, old_sha, new_sha, sm_cfg["url"], sm_cfg["local_dir"]))

    # Get parent repo dir for object-store fallback
    _parent_repo = _get_ctx("git_local_dir", "GIT_LOCAL_DIR")

    def _fetch_one(args):
        sm_name, old_sha, new_sha, sm_url, sm_local = args
        auth_url = _auth_url(sm_url, username, password)

        # ── Strategy 1: use existing local clone (no network if SHAs present) ─
        # Only attempt if the directory is already a valid git repo — don't try
        # to clone (60s timeout) when we have a faster fallback available.
        _sm_path = Path(sm_local)
        _is_valid_git = (_sm_path / ".git").exists() or (_sm_path / "objects").exists()
        if _is_valid_git:
            try:
                for sha in [old_sha, new_sha]:
                    _fetch_sha(sm_local, sha, sm_url, username, password)
                diff_out = _git(sm_local, "diff", old_sha, new_sha, timeout=30)
                if diff_out:
                    diff_trimmed = diff_out[:MAX_SUBMODULE_DIFF_BYTES]
                    if len(diff_out.encode()) > MAX_SUBMODULE_DIFF_BYTES:
                        diff_trimmed += "\n\n... [submodule diff truncated]"
                    print(f"  [submodule] {sm_name}: {diff_out.count(chr(10))} lines (via local clone)")
                    return sm_name, diff_trimmed
            except Exception:
                pass

        # ── Strategy 2: fetch objects into parent repo (no clone, minimal disk) ─
        # If the submodule is not cloned locally, fetch just the two SHAs needed
        # into the parent repo's object store. Uses `git fetch --depth=1` directly.
        # Zero disk overhead — objects stored in parent's .git/objects/.
        if _parent_repo and os.path.isdir(os.path.join(_parent_repo, ".git")):
            try:
                _env2 = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
                print(f"  [submodule] {sm_name}: no local clone — fetching objects into parent repo...")
                for sha in [old_sha, new_sha]:
                    _has = subprocess.run(
                        ["git", "-C", _parent_repo, "cat-file", "-t", sha],
                        capture_output=True, text=True, timeout=3, env=_env2,
                    ).returncode == 0
                    if not _has:
                        subprocess.run(
                            ["git", "-C", _parent_repo, "fetch", "--depth=1", auth_url, sha],
                            capture_output=True, text=True, timeout=20, env=_env2,
                        )

                diff_out = subprocess.run(
                    ["git", "-C", _parent_repo, "diff", old_sha, new_sha],
                    capture_output=True, text=True, timeout=30, env=_env2,
                ).stdout
                if diff_out:
                    diff_trimmed = diff_out[:MAX_SUBMODULE_DIFF_BYTES]
                    if len(diff_out.encode()) > MAX_SUBMODULE_DIFF_BYTES:
                        diff_trimmed += "\n\n... [submodule diff truncated]"
                    print(f"  [submodule] {sm_name}: {diff_out.count(chr(10))} lines (via parent fetch)")
                    return sm_name, diff_trimmed
            except Exception as e:
                print(f"  [submodule] {sm_name} parent-fetch failed: {e}")

        print(f"  [submodule] {sm_name}: unavailable (API, clone, and parent fetch all failed)")
        return sm_name, None

    # Fetch all submodule diffs in parallel — major speedup for 8+ submodules
    from concurrent.futures import ThreadPoolExecutor, as_completed
    print(f"  [submodule] Fetching {len(work_items)} submodule diffs in parallel...")
    with ThreadPoolExecutor(max_workers=min(6, len(work_items) or 1)) as executor:
        futures = {executor.submit(_fetch_one, item): item[0] for item in work_items}
        for future in as_completed(futures):
            sm_name, diff = future.result()
            if diff:
                results[sm_name] = diff

    return results


def summarize_submodule_diffs(submodule_diffs: Dict[str, str]) -> str:
    """
    Build a human-readable summary of what actually changed in submodules.
    This goes into the LLM context.
    """
    if not submodule_diffs:
        return ""

    parts = [f"## Submodule Code Changes ({len(submodule_diffs)} submodule(s) changed)\n"]
    parts.append("Note: the parent repo diff only shows pointer changes. "
                 "The following is the ACTUAL code that will be built:\n")

    for sm_name, diff in submodule_diffs.items():
        parts.append(f"\n### {sm_name}\n")
        # Count changed files
        files_changed = len(re.findall(r'^diff --git', diff, re.MULTILINE))
        lines_added   = len(re.findall(r'^\+[^+]', diff, re.MULTILINE))
        lines_removed = len(re.findall(r'^-[^-]', diff, re.MULTILINE))
        parts.append(f"Files changed: {files_changed} | +{lines_added} -{lines_removed} lines\n")
        parts.append("```diff\n")
        parts.append(diff[:3_000])  # first 3KB per submodule — enough for LLM to understand pattern
        parts.append("\n```\n")

    return "\n".join(parts)


# ── Auto-discover submodules from .gitmodules ──────────────────────────────────

def discover_submodules_from_repo(local_dir: str) -> List[dict]:
    """
    Read .gitmodules from a cloned repo and return all submodule definitions.

    Returns list of dicts:
      {"name": "hdfcbankformscommon",
       "path": "hdfcbankformscommon",
       "url":  "https://git.cloudmanager.adobe.com/hdfcbank/hdfcbankformscommon/",
       "branch": "develop-3.7.6"}

    Used during customer onboarding to auto-populate the submodule list
    instead of requiring manual URL entry.
    """
    gitmodules_path = Path(local_dir) / ".gitmodules"
    if not gitmodules_path.exists():
        return []

    content = gitmodules_path.read_text(encoding="utf-8", errors="ignore")
    submodules = []
    current: dict = {}

    for line in content.splitlines():
        line = line.strip()
        if line.startswith("[submodule"):
            if current.get("url"):
                submodules.append(current)
            # Extract name from: [submodule "hdfcbankformscommon"]
            m = re.match(r'\[submodule\s+"([^"]+)"\]', line)
            current = {"name": m.group(1) if m else "", "path": "", "url": "", "branch": ""}
        elif "=" in line:
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key in ("path", "url", "branch"):
                current[key] = val

    if current.get("url"):
        submodules.append(current)

    return submodules


def auto_populate_submodules_in_config(customer_name: str, local_dir: str, base_local_dir: str = "/opt/repos/submodules") -> int:
    """
    Discover submodules from .gitmodules and add them to repo_config.json.
    Called automatically after cloning a new customer's repo.

    Returns number of submodules added.
    """
    submodules = discover_submodules_from_repo(local_dir)
    if not submodules:
        return 0

    config = load_repo_config()
    existing = config.get(customer_name, {})
    existing_names = {s["name"] for s in existing.get("submodules", [])}

    new_sms = list(existing.get("submodules", []))
    added = 0
    for sm in submodules:
        if sm["name"] not in existing_names and sm["url"]:
            new_sms.append({
                "name":      sm["name"],
                "url":       sm["url"],
                "local_dir": str(Path(base_local_dir) / sm["name"]),
            })
            added += 1

    if added:
        config[customer_name] = {**existing, "submodules": new_sms}
        save_repo_config(config)

    return added
