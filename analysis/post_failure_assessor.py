"""Post-failure risk assessment orchestrator (LogSage two-stage pipeline)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import pandas as pd

from analysis.logsage import run_stage1
from analysis.retrieval.hybrid_retriever import hybrid_retrieve, normalize_query
from models.post_failure_report import LogSageRCAReport, PostFailureRiskReport


def _offline_placeholder_log(step: str) -> str:
    """Minimal synthetic log for offline --no-logs runs."""
    return (
        f"[INFO] Pipeline step {step} started\n"
        "[ERROR] BUILD FAILURE — simulated offline log for stage-1 pruning test\n"
        "npm ERR! code ELIFECYCLE\n"
        "FAIL: integration test failed\n"
    )


def _fetch_execution_log(
    share_name: str,
    failed_step: str,
    execution_id: str,
) -> str:
    from connectors.azure_connector import get_log_for_execution
    return get_log_for_execution(share_name, failed_step, execution_id)


def _resolve_commit_sha(execution_id: str, pipeline_name: str) -> Optional[str]:
    try:
        from connectors.cm_connector import get_commit_sha_from_execution
        program_id = os.getenv("PROGRAM_ID", "19905")
        pipeline_id = os.getenv("PIPELINE_ID_DEV", "47202398")
        if "production" in pipeline_name.lower():
            pipeline_id = os.getenv("PIPELINE_ID_PROD", "2357452")
        return get_commit_sha_from_execution(program_id, pipeline_id, execution_id)
    except Exception:
        return None


def _build_failure_history_dict(
    pipeline_df: pd.DataFrame,
    failed_df: pd.DataFrame,
) -> dict:
    try:
        from connectors.splunk_csv_reader import summarize_failures
        from analysis.failure_history import build_failure_history
        patterns = summarize_failures(failed_df)
        history = build_failure_history(failed_df, patterns, [], pipeline_df)
        return history.model_dump() if hasattr(history, "model_dump") else {}
    except Exception:
        return {}


def _commit_context(commit_sha: Optional[str]) -> dict:
    if not commit_sha:
        return {}
    try:
        from connectors.git_connector import get_commit_diff
        from analysis.commit_analyzer import analyze_commit
        diff_data = get_commit_diff(None, commit_sha)
        changed = diff_data.get("changed_files") or []
        diff_excerpt = diff_data.get("diff_excerpt") or ""
        profile = analyze_commit(changed, diff_excerpt, commit_sha=commit_sha)
        return {
            "commit_sha": commit_sha,
            "changed_files": changed[:30],
            "diff_excerpt": diff_excerpt[:2500],
            "modules_touched": profile.modules_touched,
        }
    except Exception:
        return {"commit_sha": commit_sha}


def _code_snippets(error_type: str, error_message: str, step: str) -> str:
    try:
        from analysis.file_resolver import resolve_snippets, format_for_llm
        parsed = {
            "error_type": error_type,
            "error_message": error_message,
            "key_lines": [],
        }
        snippets = resolve_snippets(parsed, step=step)
        return format_for_llm(snippets)
    except Exception:
        return ""


def assess_failed_execution(
    execution_id: str,
    *,
    use_llm: bool = True,
    use_reranker: bool = True,
    fetch_logs: bool = True,
    pipeline_df: Optional[pd.DataFrame] = None,
    failed_df: Optional[pd.DataFrame] = None,
    share_map: Optional[dict] = None,
) -> Tuple[Union[PostFailureRiskReport, dict], str]:
    """
    Full LogSage post-failure risk assessment for one execution.
    Returns (report_or_dict, markdown).
    """
    if failed_df is None or share_map is None or pipeline_df is None:
        from analysis.ingest import load_data
        pipeline_df, failed_df, _, share_map = load_data(force_csv=not fetch_logs)

    row = failed_df[failed_df["executionId"].astype(str) == str(execution_id)]
    if row.empty:
        row = pipeline_df[pipeline_df["executionId"].astype(str) == str(execution_id)]
    if row.empty:
        return {}, f"Execution {execution_id} not found."

    rec = row.iloc[0]
    step = str(rec.get("firstFailedStep", "") or "")
    pipeline = str(rec.get("pipelineName", "") or "")
    share = share_map.get(str(execution_id))

    log_text = ""
    if fetch_logs and share and step and step != "nan":
        try:
            log_text = _fetch_execution_log(share, step, execution_id)
        except Exception as e:
            log_text = f"[log fetch failed: {e}]"
    elif not fetch_logs:
        log_text = _offline_placeholder_log(step)

    # Stage 1
    stage1 = run_stage1(
        log_text or "No log available",
        pipeline_name=pipeline,
        failed_step=step,
        pipeline_df=pipeline_df,
        share_map=share_map or {},
        fetch_success=bool(fetch_logs and log_text),
    )

    if not use_llm:
        preview = {
            "execution_id": execution_id,
            "failed_step": step,
            "pipeline": pipeline,
            "filtered_log_blocks": stage1.filtered_blocks,
            "pruning_stats": stage1.pruning_stats,
            "filter_stats": stage1.filter_stats,
        }
        md = _format_stage1_markdown(preview)
        return preview, md

    from agent.devops_agent import run_logsage_rca, run_post_failure_assessment

    rca = run_logsage_rca(
        execution_id=execution_id,
        failed_step=step,
        filtered_blocks=stage1.filtered_blocks,
        pruning_stats=stage1.pruning_stats,
    )

    commit_sha = _resolve_commit_sha(execution_id, pipeline)
    commit_ctx = _commit_context(commit_sha)
    failure_history = _build_failure_history_dict(pipeline_df, failed_df)

    query = normalize_query(
        rca.model_dump(),
        stage1.filtered_blocks,
    )

    incidents = hybrid_retrieve(
        query,
        rca=rca.model_dump(),
        step=step,
        pipeline=pipeline,
        error_type=rca.error_type,
        failure_history=failure_history,
        changed_files=commit_ctx.get("changed_files"),
        modules=commit_ctx.get("modules_touched"),
        use_llm_routes=use_llm,
        use_reranker=use_reranker,
    )

    snippet_text = _code_snippets(rca.error_type, rca.error_summary, step)

    report = run_post_failure_assessment(
        rca=rca,
        incidents=incidents[:15],
        commit_context=commit_ctx,
        pipeline=pipeline,
        snippet_text=snippet_text,
        filtered_blocks=stage1.filtered_blocks,
        pruning_stats=stage1.pruning_stats,
    )

    os.makedirs("reports", exist_ok=True)
    out = Path(f"reports/post_failure_{execution_id}.json")
    with open(out, "w") as f:
        json.dump(report.model_dump(mode="json"), f, indent=2)

    try:
        from vector_store.store import ingest_post_failure_report
        ingest_post_failure_report(report)
    except Exception:
        pass

    return report, _format_report_markdown(report)


def _format_stage1_markdown(data: dict) -> str:
    lines = [
        f"# Post-Failure Assessment (Stage 1 only) — {data.get('execution_id')}",
        f"**Step:** {data.get('failed_step')} | **Pipeline:** {data.get('pipeline')}",
        "",
        "## Pruning Stats",
        f"```json\n{json.dumps(data.get('pruning_stats', {}), indent=2)}\n```",
        "",
        "## Filtered Blocks",
    ]
    for i, block in enumerate(data.get("filtered_log_blocks") or [], 1):
        lines += [f"### Block {i}", "```", block[:2000], "```", ""]
    return "\n".join(lines)


def _format_report_markdown(report: PostFailureRiskReport) -> str:
    lines = [
        f"# Post-Failure Risk Assessment — {report.execution_id}",
        f"**Risk Level:** {report.risk_level} | **Retry:** {report.retry_recommendation} ({report.retry_confidence}%)",
        f"**Step:** {report.failed_step} | **Pipeline:** {report.pipeline}",
        f"**Error Type:** {report.error_type}",
        "",
        "## Root Cause",
        report.root_cause_summary,
        "",
        "## Business Impact",
        report.business_impact or "—",
        "",
        "## Recommended Actions",
    ]
    for a in report.recommended_actions:
        lines.append(f"- {a}")
    lines += ["", "## Fix Steps"]
    for i, s in enumerate(report.fix_steps, 1):
        lines.append(f"{i}. {s}")
    if report.similar_incidents:
        lines += ["", "## Similar Incidents"]
        for inc in report.similar_incidents[:5]:
            lines.append(
                f"- {inc.execution_id} ({inc.route}) — {inc.root_cause[:120]}"
            )
    if report.narrative:
        lines += ["", "## Summary", report.narrative]
    stats = report.pruning_stats
    if stats:
        lines += [
            "",
            "## LogSage Pruning",
            f"Tokens: {stats.get('tokens_after', '?')} / {stats.get('token_limit', '?')} "
            f"({stats.get('blocks_selected', '?')} blocks)",
        ]
    return "\n".join(lines)
