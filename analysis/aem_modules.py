"""Map file paths to AEM Cloud Manager modules and typical failure steps."""

from __future__ import annotations

import re
from typing import Dict, List, Set, Tuple

# (path regex, module name, typical failure step)
MODULE_RULES: List[Tuple[str, str, str]] = [
    (r"ui\.frontend/", "ui.frontend", "build"),
    (r"ui\.apps/", "ui.apps", "build"),
    (r"ui\.content/", "ui.content", "deploy"),
    (r"ui\.config/", "ui.config", "securityTest"),
    (r"dispatcher", "dispatcher", "deploy"),
    (r"\.conf$", "dispatcher", "deploy"),
    (r"rewrite-.*\.conf", "dispatcher", "deploy"),
    (r"pom\.xml$", "core", "build"),
    (r"package\.json$", "core", "build"),
    (r"securityTest", "security", "securityTest"),
]


def get_module_for_path(path: str) -> Tuple[str, str]:
    """Return (module_name, typical_failed_step) for a file path."""
    path_norm = path.replace("\\", "/")
    for pattern, module, step in MODULE_RULES:
        if re.search(pattern, path_norm, re.IGNORECASE):
            return module, step
    return "other", "build"


def get_changed_modules(changed_files: List[str]) -> List[str]:
    """Return unique AEM modules touched by a list of changed file paths."""
    modules: Set[str] = set()
    for path in changed_files:
        module, _ = get_module_for_path(path)
        if module != "other":
            modules.add(module)
    return sorted(modules)


def modules_to_steps(modules: List[str]) -> Dict[str, List[str]]:
    """Map modules to the pipeline steps they most often affect."""
    step_map: Dict[str, List[str]] = {"build": [], "securityTest": [], "deploy": []}
    for path_fragment, module, step in MODULE_RULES:
        if module in modules and step in step_map and module not in step_map[step]:
            step_map[step].append(module)
    return {k: v for k, v in step_map.items() if v}


def detect_env_references_in_diff(diff_text: str) -> List[str]:
    """Find env variable names referenced in a diff that may need Cloud Manager config."""
    patterns = [
        r"PUBLISH_[A-Z0-9_]+",
        r"AUTHOR_[A-Z0-9_]+",
        r"Config variables are not defined: (\S+)",
        r"\$\{([A-Z][A-Z0-9_]*)\}",
        r"os\.environ\[['\"]([A-Z][A-Z0-9_]*)['\"]\]",
    ]
    found: Set[str] = set()
    for pat in patterns:
        for match in re.finditer(pat, diff_text):
            found.add(match.group(1) if match.lastindex else match.group(0))
    return sorted(found)
