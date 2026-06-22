"""Fetch recent successful execution logs for Drain template building."""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import pandas as pd

from analysis.logsage.constants import SUCCESS_TEMPLATE_COUNT
from analysis.logsage.drain_templates import (
    DrainTemplateDB,
    load_cached_templates,
    save_cached_templates,
)
from connectors.azure_connector import get_log_for_execution


def fetch_success_logs(
    pipeline_df: pd.DataFrame,
    share_map: Dict[str, str],
    pipeline_name: str,
    failed_step: str,
    *,
    count: int = SUCCESS_TEMPLATE_COUNT,
    fetch_logs: bool = True,
) -> Tuple[List[str], DrainTemplateDB]:
    """
    Return log texts from the last `count` FINISHED executions for the same
    pipeline and step. Builds (or loads cached) Drain template DB.
    """
    cached = load_cached_templates(pipeline_name, failed_step)
    if cached and cached.templates:
        return [], cached

    if pipeline_df is None or pipeline_df.empty:
        return [], DrainTemplateDB()

    mask = (
        (pipeline_df["Status"] == "FINISHED")
        & (pipeline_df["pipelineName"].astype(str) == str(pipeline_name))
    )
    successes = pipeline_df[mask].copy()
    if not successes.empty and "Deploy Start Time" in successes.columns:
        successes = successes.sort_values("Deploy Start Time", ascending=False)

    log_texts: List[str] = []
    for _, row in successes.head(count * 3).iterrows():
        if len(log_texts) >= count:
            break
        eid = str(row.get("executionId", ""))
        share = share_map.get(eid)
        if not share or not fetch_logs:
            continue
        try:
            text = get_log_for_execution(share, failed_step, eid)
            if text and len(text) > 50:
                log_texts.append(text)
        except Exception:
            continue

    db = DrainTemplateDB().build_from_logs(log_texts)
    if db.templates:
        save_cached_templates(pipeline_name, failed_step, db)

    return log_texts, db
