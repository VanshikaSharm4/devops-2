"""
Context Builder — Layer 5 of the data processing pipeline.
Assembles one clean, typed payload per feature.
Each build_*_context() function returns a plain dict ready for json.dumps().
This is the last step before the prompt is constructed.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from analysis.compressor import (
    compress_bundle_for_risk,
    compress_comparison,
    compress_correlate_context,
    compress_error_details,
    compress_failure_patterns,
    compress_pinpoint_context,
    compress_scan_findings,
    compress_stuck_executions,
)


# ── Feature 1: Failure report ─────────────────────────────────────────────────

def build_report_context(bundle: Any) -> dict:
    """
    Build the context dict for the 30-day failure analysis report.
    bundle is an AnalysisBundle (Pydantic model).
    """
    b = bundle.model_dump(mode="json") if hasattr(bundle, "model_dump") else bundle
    history = b.get("failure_history") or {}

    return {
        "program_id": b.get("program_id", "19905"),
        "window_days": b.get("window_days", 30),
        "execution_summary": b.get("execution_summary", {}),
        "top_failure_patterns": compress_failure_patterns(
            b.get("failure_patterns") or [], top_n=5
        ),
        "representative_errors": compress_error_details(
            b.get("error_details") or [], max_per_step=2
        ),
        "stuck_executions": compress_stuck_executions(
            b.get("stuck_executions") or [], top_n=5
        ),
        "failure_by_step": history.get("by_step", {}),
        "avg_success_duration_min": history.get("avg_success_duration_min"),
        "known_root_causes": (history.get("known_root_causes") or [])[:5],
    }


# ── Feature 2: Risk analysis ──────────────────────────────────────────────────

def build_risk_context(bundle: Any) -> dict:
    """Build the context dict for pre-deployment risk analysis."""
    b = bundle.model_dump(mode="json") if hasattr(bundle, "model_dump") else bundle
    return compress_bundle_for_risk(b)


# ── Feature 3: Comparison ─────────────────────────────────────────────────────

def build_compare_context(data: dict) -> dict:
    """
    Build context for cross-execution comparison.
    data is the raw dict from deploy_compare.compare_executions().
    """
    return compress_comparison(
        exec_a=data.get("execution_a", {}),
        exec_b=data.get("execution_b", {}),
        git_diff=data.get("git_diff"),
    )


# ── Feature 4: Correlation ────────────────────────────────────────────────────

def build_correlate_context(
    execution_id: str,
    failed_step: str,
    parse_result: Any,
    code_hits: list,
    file_snippets: list,
) -> dict:
    """Build context for log-to-code correlation."""
    return compress_correlate_context(
        execution_id=execution_id,
        failed_step=failed_step,
        parse_result=parse_result,
        code_hits=code_hits,
        file_snippets=file_snippets,
    )


# ── Feature 5a: Scan ──────────────────────────────────────────────────────────

def build_scan_context(findings: list, repo_dir: Optional[str] = None) -> dict:
    """Build context for proactive repo scan."""
    compressed = compress_scan_findings(findings)
    return {
        "repo": repo_dir or os.getenv("GIT_LOCAL_DIR", "idfc-repo"),
        **compressed,
    }


# ── Feature 5b: Pinpoint ──────────────────────────────────────────────────────

def build_pinpoint_context(
    execution_id: str,
    failed_step: str,
    parse_result: Any,
    code_findings: list,
) -> dict:
    """Build context for root cause pinpointing."""
    return compress_pinpoint_context(
        execution_id=execution_id,
        failed_step=failed_step,
        parse_result=parse_result,
        code_findings=code_findings,
    )
