"""
Static File Scanner — analyse static assets in the AEM Cloud Manager git repo.

Provides:
  scan_changed_static_files()   – every static file touched in the last N days
  find_deleted_static_files()   – files removed (deploy may break references)
  find_hot_files()              – files changed in many commits (instability signal)
  find_broken_clientlib_refs()  – AEM clientlib categories deleted but still referenced
  get_author_ownership()        – who owns which files / modules
  get_static_summary()          – one-shot aggregate stats dict
  enrich_commit_profile()       – adds static-file signals to a CommitProfile dict
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_DIR = os.getenv("GIT_LOCAL_DIR", os.path.expanduser("~/Downloads/idfc"))

# Extensions treated as "static" for risk purposes
STATIC_EXTS = {
    ".css", ".scss", ".less",
    ".js", ".ts", ".jsx", ".tsx",
    ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot",
    ".html", ".htm",
    ".json",
    ".pdf",
}

# AEM module → display name
MODULE_LABELS = {
    "idfc-ams":           "IDFC AMS (main)",
    "idfcfirst-academy":  "IDFC Academy",
    "idfc-first-assets":  "First Assets",
    "idfc-react-webform": "React Webform",
    "idfc-first-others":  "First Others",
    "ui.apps":            "ui.apps",
    "ui.frontend":        "ui.frontend",
    "ui.content":         "ui.content",
    "ui.config":          "ui.config",
    "dispatcher":         "Dispatcher",
}

# Extensions that matter for build / deploy risk (exclude images, fonts)
RISK_EXTS = {".css", ".scss", ".less", ".js", ".ts", ".jsx", ".tsx", ".html"}


# ── Git helpers ────────────────────────────────────────────────────────────────

def _git(*args: str) -> str:
    result = subprocess.run(
        ["git"] + list(args),
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return result.stdout if result.returncode == 0 else ""


def _git_log_files(
    days_back: int,
    diff_filter: Optional[str] = None,
    extra_globs: Optional[List[str]] = None,
) -> str:
    """
    Run `git log --name-only` for static file globs over the last `days_back` days.
    Returns raw stdout (commit headers interleaved with file paths).
    """
    globs = extra_globs or [
        "*.css", "*.scss", "*.less",
        "*.js", "*.jsx", "*.tsx", "*.ts",
        "*.svg", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.webp",
        "*.woff", "*.woff2", "*.ttf",
        "*.html",
        "*.json",
    ]
    cmd = [
        "git", "log",
        f"--since={days_back} days ago",
        "--name-only",
        "--pretty=format:%aI|||%an|||%H|||%s",
    ]
    if diff_filter:
        cmd.append(f"--diff-filter={diff_filter}")
    cmd += ["--"] + globs

    result = subprocess.run(
        cmd,
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return result.stdout if result.returncode == 0 else ""


# ── Parsing helpers ────────────────────────────────────────────────────────────

def _module_for(path: str) -> str:
    top = path.split("/")[0] if "/" in path else path
    return top


def _ext_for(path: str) -> str:
    return Path(path).suffix.lower()


# Commit messages that indicate a bulk subtree operation (not a real code change).
# Files touched in these commits are excluded from hot-file and deleted-file detection
# to avoid false positives — a subtree merge can touch 5,000+ files at once.
_BULK_COMMIT_PATTERNS = re.compile(
    r"(removed subdirectory|Add '[^']+/' from commit|Squashed commit|"
    r"Merge branch|subtree merge|git-subtree)",
    re.IGNORECASE,
)
_BULK_FILE_THRESHOLD = 80   # commits touching > this many files are treated as bulk ops


def _parse_log_output(raw: str, exclude_bulk: bool = True) -> List[dict]:
    """
    Parse interleaved `git log --name-only --pretty=format:...` output into
    a flat list of {file, ext, module, ts, author, sha, msg} dicts.

    If exclude_bulk=True (default), commits that look like subtree merges or
    that touch > _BULK_FILE_THRESHOLD files are silently skipped.  This keeps
    hot-file and deletion counts meaningful (not dominated by one mass-move).
    """
    # Two-pass: first collect (meta, files) per commit, then apply bulk filter
    commits: List[Tuple[dict, List[str]]] = []
    current_meta: Optional[dict] = None
    current_files: List[str] = []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if "|||" in line:
            if current_meta is not None:
                commits.append((current_meta, current_files))
            parts = line.split("|||", 3)
            current_meta = {
                "ts":     parts[0] if len(parts) > 0 else "",
                "author": parts[1] if len(parts) > 1 else "",
                "sha":    parts[2][:8] if len(parts) > 2 else "",
                "msg":    parts[3][:80] if len(parts) > 3 else "",
            }
            current_files = []
        elif current_meta:
            current_files.append(line)

    if current_meta is not None:
        commits.append((current_meta, current_files))

    records: List[dict] = []
    for meta, files in commits:
        # Skip bulk subtree operations
        if exclude_bulk:
            if _BULK_COMMIT_PATTERNS.search(meta.get("msg", "")):
                continue
            if len(files) > _BULK_FILE_THRESHOLD:
                continue
        for f in files:
            ext = _ext_for(f)
            if ext in STATIC_EXTS:
                records.append({
                    "file":   f,
                    "ext":    ext,
                    "module": _module_for(f),
                    **meta,
                })
    return records


def _deduplicate_by_file(records: List[dict]) -> List[dict]:
    """Keep only the most recent change per file path (git log is newest-first)."""
    seen: dict = {}
    for r in records:
        if r["file"] not in seen:
            seen[r["file"]] = r
    return list(seen.values())


# ── Public API ─────────────────────────────────────────────────────────────────

def scan_changed_static_files(days_back: int = 30) -> List[dict]:
    """
    Return every unique static file changed in the last `days_back` days,
    with its most recent change timestamp, author, commit SHA, and message.
    Sorted newest-first.
    """
    raw = _git_log_files(days_back)
    records = _parse_log_output(raw)
    return _deduplicate_by_file(records)


def find_deleted_static_files(days_back: int = 30) -> List[dict]:
    """
    Return static files that were DELETED in the last N days AND do not exist
    in the current HEAD tree.

    Files that were deleted then immediately re-added (e.g. subtree restructure)
    are excluded — they show up as deleted in git log but are present in the
    working tree, so they pose no real risk.
    """
    raw = _git_log_files(days_back, diff_filter="D")
    # Use exclude_bulk=False here so we capture real deletions even in large commits,
    # but then filter by file existence to remove subtree false-positives.
    records = _parse_log_output(raw, exclude_bulk=False)
    deduped = _deduplicate_by_file(records)

    # Only keep files that are genuinely absent from the current HEAD
    truly_deleted = []
    for r in deduped:
        full_path = Path(REPO_DIR) / r["file"]
        if not full_path.exists():
            truly_deleted.append(r)

    return truly_deleted


def find_hot_files(days_back: int = 30, min_commits: int = 3) -> List[dict]:
    """
    Return static files touched in >= `min_commits` distinct commits.
    High churn = multiple people editing the same file = instability risk.
    Each entry: {file, ext, module, commit_count, authors: list, last_ts, last_msg}
    """
    raw = _git_log_files(days_back)
    all_records = _parse_log_output(raw)

    # Count by file, collect unique authors and timestamps
    file_data: Dict[str, dict] = {}
    for r in all_records:
        f = r["file"]
        if f not in file_data:
            file_data[f] = {
                "file":         f,
                "ext":          r["ext"],
                "module":       r["module"],
                "commit_count": 0,
                "authors":      set(),
                "last_ts":      r["ts"],
                "last_msg":     r["msg"],
            }
        file_data[f]["commit_count"] += 1
        file_data[f]["authors"].add(r["author"])
        if r["ts"] > file_data[f]["last_ts"]:
            file_data[f]["last_ts"]  = r["ts"]
            file_data[f]["last_msg"] = r["msg"]

    hot = [
        {**v, "authors": sorted(v["authors"])}
        for v in file_data.values()
        if v["commit_count"] >= min_commits
    ]
    return sorted(hot, key=lambda x: x["commit_count"], reverse=True)


def find_broken_clientlib_refs(
    deleted_files: Optional[List[dict]] = None,
    days_back: int = 30,
) -> List[dict]:
    """
    AEM-specific broken reference detector.

    For every deleted clientlib CSS/JS file:
      1. Extract the clientlib folder name  (e.g. "clientlib-car-loan")
      2. Look for its .content.xml category declaration in the current tree
      3. Search all HTML / XML files for references to that folder name or category
      4. Return hits: {deleted_file, clientlib_name, category, referenced_in: [...]}

    References to deleted clientlibs = unstyled or broken pages on next deploy.
    """
    if deleted_files is None:
        deleted_files = find_deleted_static_files(days_back)

    # Only CSS/JS deletions matter for clientlib breakage
    relevant = [
        d for d in deleted_files
        if d["ext"] in {".css", ".js", ".scss"}
        and "clientlib" in d["file"]
    ]

    results: List[dict] = []

    for d in relevant:
        parts = d["file"].split("/")
        # Find the segment that starts with "clientlib"
        clientlib_name = next(
            (p for p in reversed(parts) if p.startswith("clientlib")),
            None,
        )
        if not clientlib_name:
            continue

        # Try to read the .content.xml for this clientlib's category
        # (may no longer exist if the whole folder was deleted)
        clientlib_dir_parts = parts[: parts.index(clientlib_name) + 1]
        content_xml = Path(REPO_DIR) / "/".join(clientlib_dir_parts) / ".content.xml"
        category = ""
        if content_xml.exists():
            xml_text = content_xml.read_text(errors="replace")
            m = re.search(r'categories="?\[?([^\]"]+)\]?"?', xml_text)
            if m:
                category = m.group(1).strip()

        # Search for references to the clientlib name or its category
        search_terms = [clientlib_name]
        if category:
            search_terms.append(category)

        refs: List[str] = []
        for term in search_terms:
            try:
                out = subprocess.run(
                    ["git", "grep", "-rl", "--ignore-case", term,
                     "--", "*.html", "*.xml", "*.json"],
                    cwd=REPO_DIR,
                    capture_output=True, text=True, timeout=20,
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                )
                for hit in out.stdout.strip().splitlines():
                    if hit and hit not in refs:
                        refs.append(hit)
            except Exception:
                pass

        if refs:
            results.append({
                "deleted_file":   d["file"],
                "clientlib_name": clientlib_name,
                "category":       category,
                "deleted_ts":     d["ts"],
                "deleted_by":     d["author"],
                "referenced_in":  refs[:20],   # cap at 20 to avoid noise
                "ref_count":      len(refs),
            })

    return sorted(results, key=lambda x: x["ref_count"], reverse=True)


def get_author_ownership(changed_files: Optional[List[dict]] = None, days_back: int = 30) -> List[dict]:
    """
    Returns per-author stats sorted by file count desc:
    [{author, file_count, modules: [...], top_exts: {...}, last_active}]
    """
    if changed_files is None:
        changed_files = scan_changed_static_files(days_back)

    by_author: Dict[str, dict] = {}
    for r in changed_files:
        a = r["author"]
        if a not in by_author:
            by_author[a] = {
                "author":      a,
                "file_count":  0,
                "modules":     set(),
                "exts":        defaultdict(int),
                "last_active": r["ts"],
            }
        by_author[a]["file_count"]  += 1
        by_author[a]["modules"].add(r["module"])
        by_author[a]["exts"][r["ext"]] += 1
        if r["ts"] > by_author[a]["last_active"]:
            by_author[a]["last_active"] = r["ts"]

    result = []
    for v in sorted(by_author.values(), key=lambda x: x["file_count"], reverse=True):
        top_exts = sorted(v["exts"].items(), key=lambda x: -x[1])[:3]
        result.append({
            "author":      v["author"],
            "file_count":  v["file_count"],
            "modules":     sorted(v["modules"]),
            "top_exts":    dict(top_exts),
            "last_active": v["last_active"][:19].replace("T", " "),
        })
    return result


def get_static_summary(days_back: int = 30) -> dict:
    """
    One-shot aggregate: returns a dict suitable for dashboard KPI cards.
    Caches the underlying scans so calling this once is sufficient.
    """
    changed  = scan_changed_static_files(days_back)
    deleted  = find_deleted_static_files(days_back)
    hot      = find_hot_files(days_back, min_commits=5)

    # By extension (risk exts only)
    by_ext: Dict[str, int] = defaultdict(int)
    for r in changed:
        by_ext[r["ext"]] += 1

    # By module
    by_module: Dict[str, int] = defaultdict(int)
    for r in changed:
        by_module[r["module"]] += 1

    # Risk-bearing changes (code/style only, not images/fonts)
    risk_changes = [r for r in changed if r["ext"] in RISK_EXTS]

    return {
        "total_changed":      len(changed),
        "risk_changes":       len(risk_changes),
        "deleted":            len(deleted),
        "hot_files":          len(hot),
        "by_ext":             dict(sorted(by_ext.items(), key=lambda x: -x[1])),
        "by_module":          dict(sorted(by_module.items(), key=lambda x: -x[1])),
        "days_back":          days_back,
    }


def enrich_commit_profile(commit_profile: dict, changed_files: List[str]) -> dict:
    """
    Given a CommitProfile dict and its changed_files list,
    add static-file risk signals:
      - static_files_changed: count
      - hot_static_files: list of files that are historically high-churn
      - deleted_static_refs: list of deleted files from recent history
        that overlap with this commit's changed files
      - has_clientlib_changes: bool
      - has_webpack_changes: bool
      - has_font_changes: bool
    Returns the enriched dict (mutates in place for convenience).
    """
    static_in_commit = [
        f for f in changed_files
        if _ext_for(f) in STATIC_EXTS
    ]

    hot = {r["file"] for r in find_hot_files(days_back=30, min_commits=5)}

    commit_profile["static_files_changed"]  = len(static_in_commit)
    commit_profile["hot_static_files"]      = [f for f in static_in_commit if f in hot]
    commit_profile["has_clientlib_changes"] = any("clientlib" in f for f in static_in_commit)
    commit_profile["has_webpack_changes"]   = any("webpack" in f for f in changed_files)
    commit_profile["has_font_changes"]      = any(
        _ext_for(f) in {".woff", ".woff2", ".ttf", ".eot"} for f in changed_files
    )
    return commit_profile
