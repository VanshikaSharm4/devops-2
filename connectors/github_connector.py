"""GitHub API connector for PR diffs, commits, and code search."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

load_dotenv()

GITHUB_API = "https://api.github.com"
MAX_DIFF_BYTES = 500_000


def _headers() -> Dict[str, str]:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise ValueError("GITHUB_TOKEN must be set in .env")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _parse_repo(repo: Optional[str] = None) -> Tuple[str, str]:
    full = repo or os.getenv("GITHUB_REPO", "")
    if "/" not in full:
        raise ValueError("GITHUB_REPO must be set to owner/repo in .env")
    owner, name = full.split("/", 1)
    return owner, name


def _get(url: str, params: Optional[dict] = None) -> Any:
    r = requests.get(url, headers=_headers(), params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def get_pr(repo: Optional[str], pr_number: int) -> dict:
    owner, name = _parse_repo(repo)
    return _get(f"{GITHUB_API}/repos/{owner}/{name}/pulls/{pr_number}")


def get_pr_diff(repo: Optional[str], pr_number: int) -> Dict[str, Any]:
    """
    Fetch PR metadata and list of changed files with patches.
    Returns dict with title, body, author, commit_sha, changed_files, diff_excerpt.
    """
    owner, name = _parse_repo(repo)
    pr = get_pr(repo, pr_number)
    files = _get(f"{GITHUB_API}/repos/{owner}/{name}/pulls/{pr_number}/files")

    changed_files: List[str] = []
    patches: List[str] = []
    for f in files:
        changed_files.append(f["filename"])
        if f.get("patch"):
            patches.append(f"## {f['filename']}\n{f['patch']}")

    diff_excerpt = "\n\n".join(patches)
    if len(diff_excerpt.encode()) > MAX_DIFF_BYTES:
        diff_excerpt = diff_excerpt[:MAX_DIFF_BYTES] + "\n\n... [diff truncated]"

    return {
        "pr_number": pr_number,
        "commit_sha": pr.get("head", {}).get("sha", ""),
        "title": pr.get("title", ""),
        "body": pr.get("body") or "",
        "author": pr.get("user", {}).get("login", ""),
        "changed_files": changed_files,
        "diff_excerpt": diff_excerpt,
        "base_sha": pr.get("base", {}).get("sha", ""),
        "head_sha": pr.get("head", {}).get("sha", ""),
    }


def get_commit_message(repo: Optional[str], sha: str) -> dict:
    owner, name = _parse_repo(repo)
    commit = _get(f"{GITHUB_API}/repos/{owner}/{name}/commits/{sha}")
    msg = commit.get("commit", {}).get("message", "")
    lines = msg.split("\n", 1)
    return {
        "commit_sha": sha,
        "title": lines[0],
        "body": lines[1].strip() if len(lines) > 1 else "",
        "author": commit.get("commit", {}).get("author", {}).get("name", ""),
    }


def get_commit_diff(repo: Optional[str], sha: str) -> Dict[str, Any]:
    owner, name = _parse_repo(repo)
    commit = _get(f"{GITHUB_API}/repos/{owner}/{name}/commits/{sha}")
    changed_files = [f["filename"] for f in commit.get("files", [])]
    patches = []
    for f in commit.get("files", []):
        if f.get("patch"):
            patches.append(f"## {f['filename']}\n{f['patch']}")
    diff_excerpt = "\n\n".join(patches)[:MAX_DIFF_BYTES]
    meta = get_commit_message(repo, sha)
    return {
        **meta,
        "changed_files": changed_files,
        "diff_excerpt": diff_excerpt,
    }


def get_diff_between_shas(repo: Optional[str], sha_a: str, sha_b: str) -> Dict[str, Any]:
    """Compare two commits (e.g. successful vs failed deployment)."""
    owner, name = _parse_repo(repo)
    comparison = _get(
        f"{GITHUB_API}/repos/{owner}/{name}/compare/{sha_a}...{sha_b}"
    )
    changed_files = [f["filename"] for f in comparison.get("files", [])]
    patches = []
    for f in comparison.get("files", []):
        if f.get("patch"):
            patches.append(f"## {f['filename']}\n{f['patch']}")
    return {
        "sha_a": sha_a,
        "sha_b": sha_b,
        "changed_files": changed_files,
        "diff_excerpt": "\n\n".join(patches)[:MAX_DIFF_BYTES],
        "status": comparison.get("status", ""),
        "ahead_by": comparison.get("ahead_by", 0),
        "behind_by": comparison.get("behind_by", 0),
    }


def get_file_content(repo: Optional[str], path: str, ref: str = "main") -> str:
    owner, name = _parse_repo(repo)
    import base64

    data = _get(f"{GITHUB_API}/repos/{owner}/{name}/contents/{path}", params={"ref": ref})
    if data.get("encoding") == "base64":
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return data.get("content", "")


def search_code(repo: Optional[str], query: str) -> List[dict]:
    owner, name = _parse_repo(repo)
    q = f"{query} repo:{owner}/{name}"
    data = _get(f"{GITHUB_API}/search/code", params={"q": q, "per_page": 10})
    return [
        {
            "path": item["path"],
            "sha": item.get("sha", ""),
            "url": item.get("html_url", ""),
        }
        for item in data.get("items", [])
    ]


def find_files_for_parsed_error(repo: Optional[str], parsed_error: dict) -> List[dict]:
    """Heuristic code search based on log_parser output."""
    results: List[dict] = []
    error_type = parsed_error.get("error_type", "")
    module = parsed_error.get("module", "")

    if error_type == "missing_npm_module":
        match = re.search(r"Missing npm package: (\S+)", parsed_error.get("error_message", ""))
        if match:
            pkg = match.group(1).split("/")[0]
            results.extend(search_code(repo, f'"{pkg}"'))
        if module:
            results.extend(search_code(repo, module))

    elif error_type in ("apache_config_syntax_error", "missing_env_variable"):
        for err in parsed_error.get("errors", []):
            if "rewrite-onpremises" in err.get("detail", ""):
                results.extend(search_code(repo, "rewrite-onpremises-migration"))
            var = err.get("detail", "")
            if "Undefined variable" in var or "PUBLISH_" in var:
                var_name = var.replace("Undefined variable: ", "").strip()
                if var_name:
                    results.extend(search_code(repo, var_name))

    # Deduplicate by path
    seen = set()
    unique = []
    for r in results:
        if r["path"] not in seen:
            seen.add(r["path"])
            unique.append(r)
    return unique[:10]
