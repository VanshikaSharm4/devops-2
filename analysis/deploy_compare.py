"""Feature 3: Cross-deployment comparison."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from analysis.ingest import get_execution_by_id, load_data
from connectors.azure_connector import get_log_for_execution
from connectors.git_connector import get_diff_between_shas
from parsers.log_parser import parse_log


def _parse_execution_logs(
    execution_id: str,
    failed_step: str,
    share_map: dict,
) -> dict:
    share = share_map.get(str(execution_id))
    if not share or not failed_step or failed_step == "nan":
        return {"error_type": "no_log", "error_message": "No log available"}
    log_text = get_log_for_execution(share, failed_step)
    return parse_log(failed_step, log_text)


def compare_executions(
    exec_a: str,
    exec_b: str,
    sha_a: Optional[str] = None,
    sha_b: Optional[str] = None,
    fetch_logs: bool = False,
) -> Dict[str, Any]:
    """Build comparison payload for LLM."""
    pipeline_df, failed_df, _, share_map = load_data()

    row_a = get_execution_by_id(pipeline_df, exec_a)
    row_b = get_execution_by_id(pipeline_df, exec_b)
    if not row_a or not row_b:
        missing = [x for x, r in [(exec_a, row_a), (exec_b, row_b)] if not r]
        raise ValueError(f"Execution(s) not found in CSV: {missing}")

    failed_a = failed_df[failed_df["executionId"].astype(str) == str(exec_a)]
    failed_b = failed_df[failed_df["executionId"].astype(str) == str(exec_b)]
    step_a = str(failed_a.iloc[0]["firstFailedStep"]) if len(failed_a) else ""
    step_b = str(failed_b.iloc[0]["firstFailedStep"]) if len(failed_b) else ""

    parsed_a = parsed_b = {}
    if fetch_logs:
        if step_a and step_a != "nan":
            parsed_a = _parse_execution_logs(exec_a, step_a, share_map)
        if step_b and step_b != "nan":
            parsed_b = _parse_execution_logs(exec_b, step_b, share_map)

    dur_a = row_a.get("Duration (Min)", 0)
    dur_b = row_b.get("Duration (Min)", 0)

    git_diff = None
    if sha_a and sha_b:
        try:
            git_diff = get_diff_between_shas(None, sha_a, sha_b)
        except Exception as e:
            git_diff = {"error": str(e)}

    return {
        "execution_a": {
            "id": exec_a,
            "status": row_a.get("Status"),
            "pipeline": row_a.get("pipelineName"),
            "duration_min": dur_a,
            "first_failed_step": step_a,
            "parsed_error": parsed_a,
        },
        "execution_b": {
            "id": exec_b,
            "status": row_b.get("Status"),
            "pipeline": row_b.get("pipelineName"),
            "duration_min": dur_b,
            "first_failed_step": step_b,
            "parsed_error": parsed_b,
        },
        "duration_delta_min": (dur_a or 0) - (dur_b or 0),
        "git_diff": git_diff,
    }


def run_compare(
    exec_a: str,
    exec_b: str,
    sha_a: Optional[str] = None,
    sha_b: Optional[str] = None,
    use_llm: bool = True,
    fetch_logs: bool = False,
) -> Tuple[Dict[str, Any], str]:
    data = compare_executions(exec_a, exec_b, sha_a=sha_a, sha_b=sha_b, fetch_logs=fetch_logs)
    if not use_llm:
        return data, _format_compare_rules_only(data)

    from agent.devops_agent import run_compare_markdown

    return data, run_compare_markdown(data)


def _format_compare_rules_only(data: dict) -> str:
    a, b = data["execution_a"], data["execution_b"]
    lines = [
        f"# Comparison: {a['id']} ({a['status']}) vs {b['id']} ({b['status']})",
        f"",
        f"| | Execution A | Execution B |",
        f"|---|---|---|",
        f"| Status | {a['status']} | {b['status']} |",
        f"| Duration | {a['duration_min']} min | {b['duration_min']} min |",
        f"| Failed step | {a['first_failed_step']} | {b['first_failed_step']} |",
        f"| Duration delta | {data['duration_delta_min']:+.0f} min | |",
        f"",
    ]
    if data.get("git_diff") and data["git_diff"].get("changed_files"):
        lines.append("## Files changed (git)")
        for f in data["git_diff"]["changed_files"][:20]:
            lines.append(f"- {f}")
    return "\n".join(lines)
