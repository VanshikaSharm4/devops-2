"""
Repository scanner — given changed files/classes, find callers and dependencies
in the local git clone without requiring a full index.

Used by build_predictor.py to detect:
  - Java classes that call changed interfaces (compilation break risk)
  - pom.xml files that reference changed artifacts (version conflict risk)
  - filter.xml files with overlapping JCR paths (deploy conflict risk)
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Set, Tuple


def _git_grep(repo_dir: str, pattern: str, file_glob: str = "*.java",
              timeout: int = 15) -> List[Tuple[str, str]]:
    """
    Run git grep in the repo. Returns list of (filepath, matching_line).
    Fast — uses git's index, no full filesystem scan.
    """
    try:
        result = subprocess.run(
            ["git", "grep", "-l", "--", pattern, f"*.{file_glob.lstrip('*.')}"],
            cwd=repo_dir, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        files = [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]
        return [(f, "") for f in files[:20]]  # cap at 20 to avoid noise
    except Exception:
        return []


def find_java_callers(repo_dir: str, class_name: str) -> List[str]:
    """
    Find Java files that import or reference a given class name.
    Returns list of file paths.
    """
    if not repo_dir or not Path(repo_dir).exists():
        return []
    results = _git_grep(repo_dir, class_name, "java")
    return [r[0] for r in results if r[0]]


def find_pom_references(repo_dir: str, artifact_id: str) -> List[str]:
    """
    Find pom.xml files that reference a given artifactId.
    Returns list of file paths.
    """
    if not repo_dir or not Path(repo_dir).exists():
        return []
    try:
        result = subprocess.run(
            ["git", "grep", "-l", "--", artifact_id, "*/pom.xml", "pom.xml"],
            cwd=repo_dir, capture_output=True, text=True, timeout=15,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        return [f.strip() for f in result.stdout.strip().splitlines() if f.strip()][:10]
    except Exception:
        return []


def find_filter_xml_conflicts(repo_dir: str, changed_root: str) -> List[str]:
    """
    Find other filter.xml files that have the same JCR root path.
    Overlapping roots = package install conflict.
    """
    if not repo_dir or not Path(repo_dir).exists():
        return []
    try:
        result = subprocess.run(
            ["git", "grep", "-l", "--", f'root="{changed_root}', "*/filter.xml"],
            cwd=repo_dir, capture_output=True, text=True, timeout=15,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        return [f.strip() for f in result.stdout.strip().splitlines() if f.strip()][:10]
    except Exception:
        return []


def check_osgi_service_exists(repo_dir: str, service_interface: str) -> bool:
    """
    Check if a referenced OSGi service interface has an implementation.

    Searches the main repo AND all cloned submodule repos from repo_config.json.
    For HDFC-style multi-submodule codebases, the @Service implementation is
    often in a different submodule than where the @Reference is defined.

    Returns True if at least one @Service implementing it exists anywhere.
    Returns True (assume exists) if no repo is available — avoids false positives.
    """
    def _grep_repo(rdir: str) -> bool:
        if not rdir or not Path(rdir).exists():
            return False
        try:
            result = subprocess.run(
                ["git", "grep", "-l", "--", f"implements.*{service_interface}"],
                cwd=rdir, capture_output=True, text=True, timeout=10,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            return bool(result.stdout.strip())
        except Exception:
            return False

    # Search main repo first
    if not repo_dir:
        return True  # no repo configured — assume exists
    if _grep_repo(repo_dir):
        return True

    # Search all cloned submodule repos
    try:
        import json as _json
        from pathlib import Path as _Path
        _rc = _json.loads(_Path("data/repo_config.json").read_text())
        # Find the customer config that matches this repo_dir
        for _cname, _ccfg in _rc.items():
            if _ccfg.get("git_local_dir") == repo_dir:
                for _sm in _ccfg.get("submodules", []):
                    _sm_dir = _sm.get("local_dir", "")
                    if _sm_dir and _Path(_sm_dir).exists() and _grep_repo(_sm_dir):
                        return True
                for _ar in _ccfg.get("additional_repos", []):
                    _ar_dir = _ar.get("local_dir", "")
                    if _ar_dir and _Path(_ar_dir).exists() and _grep_repo(_ar_dir):
                        return True
                break
    except Exception:
        pass

    # Also check the submodules directory directly
    try:
        _sm_base = Path(repo_dir).parent / "submodules"
        if _sm_base.exists():
            for _sm_path in _sm_base.iterdir():
                if _sm_path.is_dir() and (_sm_path / ".git").exists() or (_sm_path / "objects").exists():
                    if _grep_repo(str(_sm_path)):
                        return True
    except Exception:
        pass

    return False


def read_current_file(repo_dir: str, filepath: str) -> Optional[str]:
    """
    Read a file from the current HEAD of the repo.
    Used to compare current state vs diff changes.
    """
    if not repo_dir or not Path(repo_dir).exists():
        return None
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{filepath}"],
            cwd=repo_dir, capture_output=True, text=True, timeout=10,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        return result.stdout if result.returncode == 0 else None
    except Exception:
        return None
