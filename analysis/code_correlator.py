"""Feature 4: Code-to-log correlation."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from analysis.ingest import load_data
from connectors.azure_connector import get_log_for_execution
from connectors.git_connector import find_files_for_parsed_error, get_file_content
from parsers.log_parser import parse_log


def correlate_execution(
    execution_id: str,
    fetch_log: bool = True,
) -> Dict[str, Any]:
    """Load execution, parse log, search Cloud Manager Git for matching code."""
    pipeline_df, failed_df, failed_steps_df, share_map = load_data()

    row = pipeline_df[pipeline_df["executionId"].astype(str) == str(execution_id)]
    if row.empty:
        raise ValueError(f"Execution {execution_id} not found")

    failed_row = failed_df[failed_df["executionId"].astype(str) == str(execution_id)]
    step = ""
    if len(failed_row):
        step = str(failed_row.iloc[0].get("firstFailedStep", ""))

    parsed = {"error_type": "unknown"}
    if fetch_log and step and step != "nan":
        share = share_map.get(str(execution_id))
        if share:
            log_text = get_log_for_execution(share, step)
            result = parse_log(step, log_text)
            parsed = result.model_dump() if hasattr(result, "model_dump") else result

    code_hits = []
    file_snippets = []
    if os.getenv("CM_GIT_REPO_URL"):
        try:
            code_hits = find_files_for_parsed_error(None, parsed)
            for hit in code_hits[:3]:
                try:
                    content = get_file_content(None, hit["path"], ref="master")
                    file_snippets.append({
                        "path": hit["path"],
                        "excerpt": content[:2000],
                    })
                except Exception:
                    pass
        except Exception as e:
            code_hits = [{"error": str(e)}]

    return {
        "execution_id": execution_id,
        "pipeline": row.iloc[0].get("pipelineName", ""),
        "status": row.iloc[0].get("Status", ""),
        "failed_step": step,
        "parsed_error": parsed,
        "code_search_results": code_hits,
        "file_snippets": file_snippets,
    }


def run_correlate(
    execution_id: str,
    parsed_error: Optional[dict] = None,
    use_llm: bool = True,
) -> Tuple[Dict[str, Any], str]:
    if parsed_error:
        data = {
            "execution_id": "manual",
            "parsed_error": parsed_error,
            "code_search_results": find_files_for_parsed_error(None, parsed_error)
            if os.getenv("CM_GIT_REPO_URL")
            else [],
            "file_snippets": [],
        }
    else:
        data = correlate_execution(execution_id)

    if not use_llm:
        return data, _format_correlate_rules_only(data)

    from agent.devops_agent import run_correlate_markdown

    parsed = data.get("parsed_error", {})
    return data, run_correlate_markdown(
        execution_id=data.get("execution_id", ""),
        failed_step=data.get("failed_step", ""),
        parse_result=parsed,
        code_hits=data.get("code_search_results", []),
        file_snippets=data.get("file_snippets", []),
    )


def _format_correlate_rules_only(data: dict) -> str:
    parsed = data.get("parsed_error", {})
    if hasattr(parsed, "model_dump"):
        parsed = parsed.model_dump()
    lines = [
        "# Code-to-Log Correlation",
        f"",
        f"**Execution:** {data.get('execution_id')}",
        f"**Error type:** {parsed.get('error_type')}",
        f"**Message:** {parsed.get('error_message', parsed.get('errors', ''))}",
        f"",
        f"## Matching files (Cloud Manager Git search)",
    ]
    for hit in data.get("code_search_results", []):
        if "path" in hit:
            lines.append(f"- [{hit['path']}]({hit.get('url', '')})")
    return "\n".join(lines)
