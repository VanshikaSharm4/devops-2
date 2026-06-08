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

    # Rule 1 — CRXDE/DavEx type issues repeat every run regardless of code
    if et in ("security_failure", "osgi_error") and occurrence_count >= 3:
        return "infra_failure"

    # Rule 2 — missing npm / compile errors → dependency issue
    if et in ("missing_npm_module", "java_compile_error", "typescript_error"):
        return "dependency_issue"

    # Rule 3 — config / env issues
    if et in ("apache_config_syntax_error", "missing_env_variable"):
        return "config_issue"

    # Rule 4 — repeated build failures with same error = environment issue
    if et in ("build_failure",) and occurrence_count >= 5:
        return "flaky_test"

    # Rule 5 — code regression (build-time failures)
    if et in ("build_failure", "java_compile_error"):
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
