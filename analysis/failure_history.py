"""Build failure history index from Splunk CSV data and parsed error details."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

import pandas as pd

from analysis.aem_modules import get_module_for_path
from models.bundle import ErrorDetail, FailureHistory, KnownRootCause


def build_failure_history(
    merged_df: pd.DataFrame,
    failure_patterns: List[Dict[str, Any]],
    error_details: List[ErrorDetail],
    pipeline_df: Optional[pd.DataFrame] = None,
) -> FailureHistory:
    """Aggregate 30-day patterns for risk scoring."""
    by_step: Counter = Counter()
    by_module: Counter = Counter()

    for _, row in merged_df.iterrows():
        step = str(row.get("firstFailedStep", "") or "unknown")
        if step and step != "nan":
            by_step[step] += 1

    for pattern in failure_patterns:
        step = str(pattern.get("firstFailedStep", "") or "")
        if step and step != "nan":
            by_step[step] += 0  # ensure key exists

    known: List[KnownRootCause] = []
    seen_types: Dict[str, KnownRootCause] = {}

    for detail in error_details:
        step = detail.failed_step
        parsed = detail.parsed_error or {}
        error_type = parsed.get("error_type", "unknown")

        if error_type == "missing_npm_module":
            module = parsed.get("module", "ui.frontend")
            by_module[module] += 1
            key = f"{error_type}:{parsed.get('error_message', '')}"
            if key not in seen_types:
                seen_types[key] = KnownRootCause(
                    error_type=error_type,
                    detail=parsed.get("error_message", ""),
                    affected_files=[f"ui.frontend/**/{module}/**"],
                    failed_step=step,
                    occurrence_count=1,
                )
            else:
                seen_types[key].occurrence_count += 1

        elif step == "securityTest":
            for check in parsed.get("checks_failing_all_nodes", []):
                by_module["security"] += 1
                key = f"security:{check}"
                if key not in seen_types:
                    seen_types[key] = KnownRootCause(
                        error_type="security_check",
                        detail=check,
                        failed_step="securityTest",
                        occurrence_count=1,
                    )

        elif step == "deploy":
            for err in parsed.get("errors", []):
                et = err.get("type", "deploy_error")
                key = f"{et}:{err.get('detail', '')[:80]}"
                if "rewrite-onpremises" in err.get("detail", ""):
                    seen_types[key] = KnownRootCause(
                        error_type=et,
                        detail=err.get("detail", ""),
                        affected_files=["**/rewrite-onpremises-migration.conf"],
                        failed_step="deploy",
                        occurrence_count=1,
                    )
                elif et == "missing_env_variable":
                    var = err.get("detail", "").replace("Undefined variable: ", "")
                    seen_types[key] = KnownRootCause(
                        error_type=et,
                        detail=var,
                        failed_step="deploy",
                        occurrence_count=1,
                    )
                by_module["dispatcher"] += 1

    known = list(seen_types.values())

    # Enrich by_module from failure patterns (pipeline + step counts)
    for pattern in failure_patterns:
        step = str(pattern.get("firstFailedStep", "") or "")
        count = int(pattern.get("count", 1))
        if step == "build":
            by_module["ui.frontend"] += count // 2  # heuristic weight

    avg_duration = None
    if pipeline_df is not None:
        finished = pipeline_df[pipeline_df["Status"] == "FINISHED"]
        if len(finished) > 0 and "Duration (Min)" in finished.columns:
            avg_duration = float(finished["Duration (Min)"].mean())

    return FailureHistory(
        by_step=dict(by_step),
        by_module=dict(by_module),
        by_pipeline_step=failure_patterns,
        known_root_causes=known,
        avg_success_duration_min=avg_duration,
    )


def file_matches_known_cause(file_path: str, history: FailureHistory) -> List[KnownRootCause]:
    """Return known root causes that may apply to a changed file."""
    matches = []
    path_norm = file_path.replace("\\", "/")
    for cause in history.known_root_causes:
        for pattern in cause.affected_files:
            frag = pattern.replace("**/", "").replace("**", "").strip("/")
            if frag and frag in path_norm:
                matches.append(cause)
                break
        if cause.error_type == "missing_env_variable" and "config" in path_norm.lower():
            matches.append(cause)
    return matches
