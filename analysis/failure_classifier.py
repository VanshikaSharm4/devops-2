"""
Failure Classifier — classifies historical failure records into failure classes
so the risk scorer can down-weight environmental/flaky noise vs. real code risk.
"""
from __future__ import annotations

from typing import Any, Dict, List

# ── Failure classes and their signal weights ───────────────────────────────────

FAILURE_CLASSES: Dict[str, float] = {
    "code_regression": 1.0,      # high signal — code caused it
    "dependency_issue": 0.9,     # high signal — dep mismatch
    "config_issue": 0.8,         # medium-high — config caused it
    "deployment_issue": 0.6,     # medium
    "infra_failure": 0.1,        # low signal — environment, not code
    "flaky_test": 0.05,          # very low — noise
    "unknown": 0.3,
}


# ── Public classification functions ───────────────────────────────────────────

def classify_failure(
    error_type: str,
    step: str,
    occurrence_count: int = 1,
) -> str:
    """
    Classify a single failure record into a failure class.
    Rules are applied in order; first match wins.

    Parameters
    ----------
    error_type       : parsed error_type string (e.g. "security_failure")
    step             : pipeline step where failure occurred (e.g. "deploy")
    occurrence_count : how many times this error appeared in the window

    Returns
    -------
    Failure class string (key in FAILURE_CLASSES).
    """
    et = error_type or ""
    st = step or ""

    # Rule 1 — Environment security issues (CRXDE/DavEx active, dispatcher misconfigured).
    # Only infra when on securityTest step — a security_failure on build is code-caused.
    # Require count >= 3 to distinguish env noise from a commit that broke security config.
    if et in ("security_failure", "osgi_error") and occurrence_count >= 3 and st == "securityTest":
        return "infra_failure"

    # Rule 2 — Java compile errors are code regressions, not dependency issues.
    # Missing npm modules and TypeScript errors are dependency/configuration issues.
    if et == "java_compile_error":
        return "code_regression"
    if et in ("missing_npm_module", "typescript_error"):
        return "dependency_issue"

    # Rule 3 — config / env issues
    if et in ("apache_config_syntax_error", "missing_env_variable"):
        return "config_issue"

    # Rule 4 — repeated build_failure with same error.
    # Previously classified as flaky_test which down-weighted real build failures.
    # A build that fails 5+ times is a persistent code or infra issue — not flaky.
    # Classify as infra_failure (weight 0.1) only if on a non-build step (env issue).
    # On build step: still code_regression — someone needs to fix it.
    if et == "build_failure" and occurrence_count >= 5 and st != "build":
        return "infra_failure"

    # Rule 5 — code regression (build-time failures)
    if et in ("build_failure",):
        return "code_regression"

    # Rule 6 — deploy step failures
    if st == "deploy":
        return "deployment_issue"

    # Default
    return "unknown"


def get_signal_weight(failure_class: str) -> float:
    """Return the signal weight for a given failure class (0.0–1.0)."""
    return FAILURE_CLASSES.get(failure_class, FAILURE_CLASSES["unknown"])


def classify_error_details(error_details: List[Any]) -> List[Dict]:
    """
    Classify a list of error detail records (Pydantic models or dicts).

    Returns a list of dicts, one per input record, each containing:
        error_type      : str
        step            : str
        occurrence_count: int
        failure_class   : str
        signal_weight   : float
        original        : the original record (model or dict)
    """
    classified: List[Dict] = []

    for ed in error_details:
        # Support both Pydantic models and plain dicts
        if isinstance(ed, dict):
            parsed = ed.get("parsed_error") or {}
            error_type = (
                parsed.get("error_type")
                or ed.get("error_type")
                or "unknown"
            )
            step = ed.get("failed_step") or ed.get("step") or ""
            occurrence_count = ed.get("occurrence_count", 1)
        else:
            parsed = getattr(ed, "parsed_error", {}) or {}
            if isinstance(parsed, dict):
                error_type = parsed.get("error_type", "unknown")
            else:
                error_type = getattr(parsed, "error_type", "unknown")
            step = getattr(ed, "failed_step", "") or getattr(ed, "step", "") or ""
            occurrence_count = getattr(ed, "occurrence_count", 1)

        failure_class = classify_failure(
            error_type=error_type,
            step=step,
            occurrence_count=occurrence_count,
        )
        signal_weight = get_signal_weight(failure_class)

        classified.append({
            "error_type": error_type,
            "step": step,
            "occurrence_count": occurrence_count,
            "failure_class": failure_class,
            "signal_weight": signal_weight,
            "original": ed,
        })

    return classified


def deduplicate_correlated_failures(classified: List[Dict]) -> List[Dict]:
    """
    Remove correlated failures that share the same root error_type across steps.

    Problem with linear aggregation: if securityTest fails 15 times AND deploy
    fails 12 times with the same `security_failure` error type (both caused by
    CRXDE being active), the scorer counts two independent signals and inflates
    the risk. They're one problem showing at two steps.

    Rule: for each error_type, keep only the highest-weight classification.
    This prevents double-counting correlated failures while preserving genuinely
    independent failures (different error types at different steps).

    Also caps the effective signal of any single error type: a pipeline with
    100 security_failures gives no more evidence than one with 10, because
    they're the same recurring issue. Cap occurrence_count at 10 for weight purposes.
    """
    seen: Dict[str, Dict] = {}
    for c in classified:
        et = c["error_type"]
        # Cap occurrence count so high-frequency recurring issues don't dominate
        capped = dict(c)
        capped["occurrence_count"] = min(c.get("occurrence_count", 1), 10)
        if et not in seen or capped["signal_weight"] > seen[et]["signal_weight"]:
            seen[et] = capped
    return list(seen.values())


def filter_high_signal_failures(
    classified: List[Dict],
    min_weight: float = 0.5,
) -> List[Dict]:
    """
    Return only failures whose signal_weight >= min_weight.
    Removes infra_failure (0.1) and flaky_test (0.05) noise.

    Parameters
    ----------
    classified  : output of classify_error_details()
    min_weight  : minimum signal_weight to keep (default 0.5)

    Returns
    -------
    Filtered list in the same format as classify_error_details().
    """
    return [item for item in classified if item.get("signal_weight", 0.0) >= min_weight]
