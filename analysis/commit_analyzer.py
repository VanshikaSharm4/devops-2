"""
Commit Analyzer — deep commit profiler for pre-deployment risk analysis.
Reads changed_files + diff_text and produces a rich CommitProfile dataclass.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

from analysis.aem_modules import detect_env_references_in_diff, get_module_for_path


# ── Module criticality weights ─────────────────────────────────────────────────

MODULE_CRITICALITY: dict[str, float] = {
    "core": 0.9,
    "ui.frontend": 0.7,
    "ui.apps": 0.8,
    "ui.content": 0.4,
    "ui.config": 0.6,
    "dispatcher": 0.8,
}

# ── CommitProfile dataclass ────────────────────────────────────────────────────

@dataclass
class CommitProfile:
    commit_sha: str = ""
    title: str = ""
    changed_files: list = field(default_factory=list)
    modules_touched: list = field(default_factory=list)  # sorted list
    dependency_files: list = field(default_factory=list)
    config_files: list = field(default_factory=list)
    security_sensitive_files: list = field(default_factory=list)
    test_files: list = field(default_factory=list)
    cicd_files: list = field(default_factory=list)
    has_dependency_changes: bool = False
    has_reactor_module_changes: bool = False
    has_config_changes: bool = False
    has_security_changes: bool = False
    has_test_changes: bool = False
    has_cicd_changes: bool = False
    has_app_changes: bool = False
    lines_added: int = 0
    lines_removed: int = 0
    lines_changed: int = 0
    blast_radius: int = 0
    added_dependencies: list = field(default_factory=list)
    removed_dependencies: list = field(default_factory=list)
    added_reactor_modules: list = field(default_factory=list)
    removed_reactor_modules: list = field(default_factory=list)
    env_vars_referenced: list = field(default_factory=list)
    anti_patterns: list = field(default_factory=list)
    criticality_score: float = 0.0


# ── Internal helpers ───────────────────────────────────────────────────────────

_DEPENDENCY_FILES = re.compile(
    r"(^|/)pom\.xml$|package\.json$|package-lock\.json$", re.IGNORECASE
)
_CONFIG_FILES = re.compile(r"\.(conf|vhost|vars|any)$", re.IGNORECASE)
_SECURITY_PATHS = re.compile(
    r"(auth|acl|login|oauth|saml|security|password|token)", re.IGNORECASE
)
_TEST_FILES = re.compile(r"(test|spec|/it/)", re.IGNORECASE)
_CICD_FILES = re.compile(
    r"(^|/)Jenkinsfile$|\.(yml|yaml)$|(^|/)Dockerfile$", re.IGNORECASE
)
_APP_FILES = re.compile(r"\.(java|js|ts|jsx|tsx|groovy)$", re.IGNORECASE)

# Maven dep regex (lines in diff)
_ADDED_MAVEN_DEP = re.compile(r"^\+(?!\+\+).*<artifactId>([^<]+)</artifactId>", re.MULTILINE)
_REMOVED_MAVEN_DEP = re.compile(r"^-(?!--).*<artifactId>([^<]+)</artifactId>", re.MULTILINE)
_ADDED_REACTOR_MODULE = re.compile(r"^\+(?!\+\+)\s*<module>([^<]+)</module>", re.MULTILINE)
_REMOVED_REACTOR_MODULE = re.compile(r"^-(?!--)\s*<module>([^<]+)</module>", re.MULTILINE)

# npm dep added: lines like +  "some-package": "version"
_ADDED_NPM_DEP = re.compile(r'^\+\s+"([^"@][^"]+)":\s+"', re.MULTILINE)

# Code churn: lines starting with + (not +++) or - (not ---)
_ADDED_LINE = re.compile(r"^\+(?!\+\+)", re.MULTILINE)
_REMOVED_LINE = re.compile(r"^-(?!--)", re.MULTILINE)


def _classify_file(path: str, profile: CommitProfile) -> None:
    """Classify a single file path into the appropriate buckets of the profile."""
    norm = path.replace("\\", "/")

    if _DEPENDENCY_FILES.search(norm):
        profile.dependency_files.append(path)

    if _CONFIG_FILES.search(norm):
        profile.config_files.append(path)

    if _SECURITY_PATHS.search(norm):
        profile.security_sensitive_files.append(path)

    if _TEST_FILES.search(norm):
        profile.test_files.append(path)

    if _CICD_FILES.search(norm):
        profile.cicd_files.append(path)

    if _APP_FILES.search(norm):
        profile.has_app_changes = True


def _detect_anti_patterns(profile: CommitProfile) -> List[str]:
    """Detect anti-patterns and return a list of descriptive strings."""
    patterns: List[str] = []

    reactor_only = (
        profile.has_reactor_module_changes
        and not profile.added_dependencies
        and not profile.removed_dependencies
    )

    if profile.dependency_files and not profile.has_test_changes:
        if reactor_only:
            patterns.append("reactor module list changed without validation")
        else:
            patterns.append("pom.xml modified without test changes")

    if "dispatcher" in profile.modules_touched and profile.has_app_changes:
        patterns.append("Dispatcher config changed alongside application code")

    if profile.security_sensitive_files:
        patterns.append("Security-sensitive files modified")

    if profile.blast_radius > 3:
        patterns.append("High blast radius (>3 modules)")

    if profile.has_cicd_changes and profile.has_app_changes:
        patterns.append("CI/CD scripts modified with application code")

    if profile.lines_changed > 200 and not profile.has_test_changes:
        patterns.append("Large churn (>200 lines) without test coverage")

    if "core" in profile.modules_touched:
        patterns.append("Shared core module modified")

    return patterns


def _compute_criticality(profile: CommitProfile) -> float:
    """Compute criticality_score from module weights and anti-pattern count."""
    if not profile.modules_touched:
        base = 0.2
    else:
        base = max(
            MODULE_CRITICALITY.get(mod, 0.2) for mod in profile.modules_touched
        )
    score = base + 0.15 * len(profile.anti_patterns)
    return min(score, 1.0)


# ── Public API ─────────────────────────────────────────────────────────────────

def analyze_commit(
    changed_files: List[str],
    diff_text: str,
    commit_sha: str = "",
    title: str = "",
) -> CommitProfile:
    """
    Build a CommitProfile from a list of changed file paths and the diff text.

    Parameters
    ----------
    changed_files : list of file paths from the commit
    diff_text     : raw unified diff text (may be empty string)
    commit_sha    : optional git SHA
    title         : optional commit message / PR title

    Returns
    -------
    CommitProfile with all fields populated.
    """
    profile = CommitProfile(
        commit_sha=commit_sha,
        title=title,
        changed_files=list(changed_files),
    )

    # ── Classify each changed file ────────────────────────────────────────────
    modules_set: set[str] = set()

    for path in changed_files:
        _classify_file(path, profile)
        module, _ = get_module_for_path(path)
        if module != "other":
            modules_set.add(module)

    profile.modules_touched = sorted(modules_set)

    # ── Boolean flags from file lists ─────────────────────────────────────────
    profile.has_dependency_changes = bool(profile.dependency_files)
    profile.has_config_changes = bool(profile.config_files)
    profile.has_security_changes = bool(profile.security_sensitive_files)
    profile.has_test_changes = bool(profile.test_files)
    profile.has_cicd_changes = bool(profile.cicd_files)
    # has_app_changes is set inside _classify_file per file

    # ── Blast radius ──────────────────────────────────────────────────────────
    profile.blast_radius = len(modules_set)

    # ── Code churn from diff ──────────────────────────────────────────────────
    if diff_text:
        profile.lines_added = len(_ADDED_LINE.findall(diff_text))
        profile.lines_removed = len(_REMOVED_LINE.findall(diff_text))
        profile.lines_changed = profile.lines_added + profile.lines_removed

        # Maven deps
        profile.added_dependencies = list(
            dict.fromkeys(_ADDED_MAVEN_DEP.findall(diff_text))
        )
        profile.removed_dependencies = list(
            dict.fromkeys(_REMOVED_MAVEN_DEP.findall(diff_text))
        )
        profile.added_reactor_modules = list(
            dict.fromkeys(_ADDED_REACTOR_MODULE.findall(diff_text))
        )
        profile.removed_reactor_modules = list(
            dict.fromkeys(_REMOVED_REACTOR_MODULE.findall(diff_text))
        )
        profile.has_reactor_module_changes = bool(
            profile.added_reactor_modules or profile.removed_reactor_modules
        )

        # npm deps (only added lines)
        npm_added = list(dict.fromkeys(_ADDED_NPM_DEP.findall(diff_text)))
        if npm_added:
            # Merge with maven added (both are dep changes)
            seen = set(profile.added_dependencies)
            for dep in npm_added:
                if dep not in seen:
                    profile.added_dependencies.append(dep)
                    seen.add(dep)

        # Env vars
        profile.env_vars_referenced = detect_env_references_in_diff(diff_text)

    # ── Anti-patterns ─────────────────────────────────────────────────────────
    profile.anti_patterns = _detect_anti_patterns(profile)

    # ── Criticality score ─────────────────────────────────────────────────────
    profile.criticality_score = _compute_criticality(profile)

    return profile


# ── Failure mode inference ─────────────────────────────────────────────────────

def infer_failure_modes(profile: CommitProfile, diff_text: str) -> List[str]:
    """
    Return a deduplicated list of likely failure_type strings based on what the
    commit changed.  Order is not significant — callers should treat this as a set.
    """
    modes: list[str] = []

    reactor_only = (
        profile.has_reactor_module_changes
        and not profile.added_dependencies
        and not profile.removed_dependencies
    )

    # Dependency-file signals
    if profile.has_dependency_changes and not reactor_only:
        dep_files_lower = [f.lower() for f in profile.dependency_files]
        if any("pom.xml" in f for f in dep_files_lower):
            modes += ["osgi_activation_failure", "classpath_conflict"]
        if any("package.json" in f for f in dep_files_lower):
            modes += ["classpath_conflict"]

    if reactor_only:
        modes += ["deployment_ordering_issue"]

    # Security changes
    if profile.has_security_changes:
        modes += ["auth_regression", "config_propagation_issue"]

    # Config changes
    if profile.has_config_changes:
        modules_lower = [m.lower() for m in profile.modules_touched]
        if "dispatcher" in modules_lower:
            modes += ["cache_invalidation", "deployment_ordering_issue"]
        if "ui.config" in modules_lower:
            modes += ["config_propagation_issue", "osgi_activation_failure"]

    # Core + app changes
    modules_lower = [m.lower() for m in profile.modules_touched]
    if "core" in modules_lower and profile.has_app_changes:
        modes += ["dependency_injection_failure", "api_contract_mismatch"]

    # Env var references
    if profile.env_vars_referenced:
        modes += ["config_propagation_issue"]

    # Diff-text signals
    if diff_text:
        if "ResourceResolver" in diff_text or "Session" in diff_text:
            modes += ["resource_resolver_leak"]
        if "@Reference" in diff_text or "@Inject" in diff_text:
            modes += ["dependency_injection_failure", "osgi_activation_failure"]
        if "HttpClient" in diff_text or "RestTemplate" in diff_text or "fetch(" in diff_text:
            modes += ["integration_timeout"]
        if (
            "serialize" in diff_text
            or "ObjectMapper" in diff_text
            or "JSONObject" in diff_text
            or "<xs:" in diff_text
        ):
            modes += ["serialization_failure"]
        if "CacheControl" in diff_text or "cache" in diff_text or "Dispatcher" in diff_text:
            modes += ["cache_invalidation"]

    # Deduplicate while preserving first-seen order
    seen: set[str] = set()
    result: List[str] = []
    for mode in modes:
        if mode not in seen:
            seen.add(mode)
            result.append(mode)
    return result


def infer_change_intent(title: str, profile: CommitProfile) -> str:
    """
    Infer the high-level intent of the commit from its title and profile.

    Returns one of:
        "feature_addition" | "refactor" | "dependency_upgrade" | "hotfix" |
        "config_change" | "migration" | "security_patch" | "unknown"
    """
    title_lower = (title or "").lower()

    if any(kw in title_lower for kw in ("fix", "hotfix", "bug")):
        return "hotfix"

    if any(kw in title_lower for kw in ("refactor", "cleanup", "clean up", "rename")):
        return "refactor"

    reactor_only = (
        profile.has_reactor_module_changes
        and not profile.added_dependencies
        and not profile.removed_dependencies
    )

    if any(kw in title_lower for kw in ("upgrade", "bump", "update", "migrate")):
        if reactor_only:
            return "config_change"
        return "dependency_upgrade" if profile.has_dependency_changes else "migration"

    if any(kw in title_lower for kw in ("security", "auth", "saml")):
        return "security_patch"

    if any(kw in title_lower for kw in ("config", "conf", "setting")):
        return "config_change"

    # Profile-based fallbacks
    if profile.has_dependency_changes and profile.lines_changed < 30 and not reactor_only:
        return "dependency_upgrade"

    if profile.has_app_changes and profile.lines_changed > 200:
        return "feature_addition"

    if profile.has_app_changes and profile.lines_changed < 50:
        return "hotfix"

    return "unknown"
