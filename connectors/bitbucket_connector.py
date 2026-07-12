"""
Bitbucket REST API connector for Adobe Cloud Manager git server.

Replaces clone-based submodule diff fetching with direct API calls.
The Adobe CM git server is Bitbucket-based and exposes a standard
Bitbucket Server REST API at:
  https://git.cloudmanager.adobe.com/rest/api/1.0/...

No cloning, no disk usage — pure HTTP.
"""
from __future__ import annotations

import fnmatch
import re
from typing import Dict, List, Optional, Tuple

import requests

BITBUCKET_BASE = "https://git.cloudmanager.adobe.com/rest/api/1.0"
_DEFAULT_TIMEOUT = 15


# ── URL parsing ────────────────────────────────────────────────────────────────

def parse_cm_git_url(url: str) -> Tuple[str, str]:
    """
    Parse an Adobe Cloud Manager git URL into (project, repo).

    Example:
        parse_cm_git_url("https://git.cloudmanager.adobe.com/hdfcbank/hdfcbankalforms/")
        -> ("hdfcbank", "hdfcbankalforms")

    Raises ValueError if the URL does not look like a CM git URL.
    """
    url = url.rstrip("/")
    m = re.search(r'git\.cloudmanager\.adobe\.com/([^/]+)/([^/]+)', url)
    if not m:
        raise ValueError(f"Cannot parse CM git URL: {url!r}")
    return m.group(1), m.group(2)


# ── Core HTTP helper ───────────────────────────────────────────────────────────

def bitbucket_get(
    path: str,
    username: str,
    password: str,
    params: Optional[Dict] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> dict:
    """
    Authenticated GET to https://git.cloudmanager.adobe.com/rest/api/1.0/{path}.

    Auth priority:
    1. BITBUCKET_PAT env var — one Argus admin PAT works for all customer repos.
       Generated once in Bitbucket account settings → Personal Access Tokens.
       Use Bearer token auth (Bitbucket PAT format).
    2. username + password (Basic auth) — the git clone password from CM → Repositories.
       Works for git clone/fetch but NOT for the REST API (Adobe blocks it).

    Returns parsed JSON dict.
    Raises RuntimeError on HTTP error or network failure.
    """
    url = f"{BITBUCKET_BASE}/{path.lstrip('/')}"

    # Prefer PAT — one token for all customers, no per-customer credential needed
    _pat = os.getenv("BITBUCKET_PAT", "").strip()
    if _pat:
        _headers = {"Accept": "application/json", "Authorization": f"Bearer {_pat}"}
        _auth = None
    else:
        _headers = {"Accept": "application/json"}
        _auth = (username, password)

    try:
        resp = requests.get(
            url,
            auth=_auth,
            params=params or {},
            timeout=timeout,
            headers=_headers,
        )
    except requests.exceptions.Timeout:
        raise RuntimeError(f"Bitbucket API timeout ({timeout}s): {url}")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Bitbucket API request failed: {e}")

    if not resp.ok:
        raise RuntimeError(
            f"Bitbucket API {resp.status_code} for {url}: {resp.text[:200]}"
        )

    try:
        return resp.json()
    except Exception as e:
        raise RuntimeError(f"Bitbucket API returned non-JSON: {e}")


# ── Diff ───────────────────────────────────────────────────────────────────────

def _hunk_header(source_line: int, source_span: int, dest_line: int, dest_span: int) -> str:
    """Build a unified diff @@ hunk header."""
    src = f"{source_line},{source_span}" if source_span != 1 else str(source_line)
    dst = f"{dest_line},{dest_span}" if dest_span != 1 else str(dest_line)
    return f"@@ -{src} +{dst} @@"


def _bb_diff_to_unified(diffs_payload: dict) -> List[str]:
    """
    Convert a Bitbucket compare/diff JSON payload to unified diff lines.

    Each entry in diffs_payload["diffs"] becomes a diff --git block.
    Returns list of lines (no trailing newlines).
    """
    lines: List[str] = []

    for file_diff in diffs_payload.get("diffs", []):
        src = file_diff.get("source")
        dst = file_diff.get("destination")

        src_path = src["toString"] if src else None
        dst_path = dst["toString"] if dst else None

        a_path = src_path or dst_path
        b_path = dst_path or src_path

        lines.append(f"diff --git a/{a_path} b/{b_path}")

        if src_path is None:
            # New file
            lines.append("new file mode 100644")
            lines.append("--- /dev/null")
            lines.append(f"+++ b/{dst_path}")
        elif dst_path is None:
            # Deleted file
            lines.append("deleted file mode 100644")
            lines.append(f"--- a/{src_path}")
            lines.append("+++ /dev/null")
        else:
            lines.append(f"--- a/{src_path}")
            lines.append(f"+++ b/{dst_path}")

        for hunk in file_diff.get("hunks", []):
            src_line = hunk.get("sourceLine", 1)
            src_span = hunk.get("sourceSpan", 0)
            dst_line = hunk.get("destinationLine", 1)
            dst_span = hunk.get("destinationSpan", 0)
            lines.append(_hunk_header(src_line, src_span, dst_line, dst_span))

            for segment in hunk.get("segments", []):
                seg_type = segment.get("type", "CONTEXT")
                if seg_type == "CONTEXT":
                    prefix = " "
                elif seg_type == "REMOVED":
                    prefix = "-"
                else:
                    prefix = "+"
                for seg_line in segment.get("lines", []):
                    lines.append(f"{prefix}{seg_line.get('line', '')}")

    return lines


def get_compare_diff(
    project: str,
    repo: str,
    old_sha: str,
    new_sha: str,
    username: str,
    password: str,
    max_lines: int = 5000,
) -> str:
    """
    Fetch the diff between two SHAs via the Bitbucket compare/diff API.

    Handles pagination (isLastPage / nextPageStart).
    Returns unified diff text compatible with git diff output.
    Caps output at max_lines lines.
    """
    path = f"projects/{project}/repos/{repo}/compare/diff"
    params: Dict = {
        "from": old_sha,
        "to": new_sha,
        "contextLines": 3,
        "limit": 100,
    }

    all_lines: List[str] = []
    page_start = 0

    while True:
        if page_start:
            params["start"] = page_start

        data = bitbucket_get(path, username, password, params=params)
        page_lines = _bb_diff_to_unified(data)
        all_lines.extend(page_lines)

        if len(all_lines) >= max_lines:
            all_lines = all_lines[:max_lines]
            all_lines.append("... [diff truncated at max_lines]")
            break

        if data.get("isLastPage", True):
            break

        next_start = data.get("nextPageStart")
        if next_start is None:
            break
        page_start = next_start

    return "\n".join(all_lines)


# ── File browsing ──────────────────────────────────────────────────────────────

def _browse_level(
    project: str,
    repo: str,
    sha: str,
    directory: str,
    username: str,
    password: str,
    depth: int,
    max_depth: int,
    pattern: str,
    results: List[str],
) -> None:
    """Recursive helper: one directory level, recurse up to max_depth."""
    path = f"projects/{project}/repos/{repo}/browse"
    if directory:
        path = f"{path}/{directory}"

    params: Dict = {"at": sha, "limit": 999}
    page_start = 0

    while True:
        if page_start:
            params["start"] = page_start

        try:
            data = bitbucket_get(path, username, password, params=params)
        except RuntimeError:
            break

        children = data.get("children", {})
        for entry in children.get("values", []):
            entry_type = entry.get("type", "")
            entry_path_obj = entry.get("path", {})
            entry_name = entry_path_obj.get("toString", entry_path_obj.get("name", ""))

            full_path = f"{directory}/{entry_name}".lstrip("/") if directory else entry_name

            if entry_type == "FILE":
                if not pattern or fnmatch.fnmatch(entry_name, pattern):
                    results.append(full_path)
            elif entry_type == "DIRECTORY" and depth < max_depth:
                _browse_level(
                    project, repo, sha, full_path,
                    username, password,
                    depth + 1, max_depth,
                    pattern, results,
                )

        if children.get("isLastPage", True):
            break
        next_start = children.get("nextPageStart")
        if next_start is None:
            break
        page_start = next_start


def list_files_at_sha(
    project: str,
    repo: str,
    sha: str,
    username: str,
    password: str,
    directory: str = "",
    pattern: str = "*.java",
) -> List[str]:
    """
    List files matching pattern at a given SHA via Bitbucket browse API.

    Recurses up to 2 directory levels from the starting directory.
    Returns list of file paths relative to the repo root.
    """
    results: List[str] = []
    _browse_level(
        project, repo, sha, directory,
        username, password,
        depth=0, max_depth=2,
        pattern=pattern, results=results,
    )
    return results


# ── File reading ───────────────────────────────────────────────────────────────

def read_file_at_sha(
    project: str,
    repo: str,
    sha: str,
    filepath: str,
    username: str,
    password: str,
) -> str:
    """
    Read raw file content from a repo at a specific SHA.

    Tries the ?raw endpoint first; falls back to JSON lines[].text.
    Raises RuntimeError if the file cannot be read.
    """
    api_path = f"projects/{project}/repos/{repo}/browse/{filepath.lstrip('/')}"
    url = f"{BITBUCKET_BASE}/{api_path}"

    # Strategy 1: raw content via ?raw parameter
    try:
        resp = requests.get(
            url,
            auth=(username, password),
            params={"at": sha, "raw": ""},
            timeout=_DEFAULT_TIMEOUT,
            headers={"Accept": "text/plain"},
        )
        if resp.ok and resp.text:
            return resp.text
    except requests.exceptions.RequestException:
        pass

    # Strategy 2: JSON browse response with lines[].text
    try:
        data = bitbucket_get(api_path, username, password, params={"at": sha})
        line_objects = data.get("lines", [])
        if line_objects:
            return "\n".join(obj.get("text", "") for obj in line_objects)
    except RuntimeError:
        pass

    raise RuntimeError(
        f"Cannot read {filepath} at {sha[:12]} from {project}/{repo}"
    )
