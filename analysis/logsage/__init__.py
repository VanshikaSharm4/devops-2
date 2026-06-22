"""LogSage Stage 1 — log filtering and token pruning pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from analysis.logsage.context_window import expand_context_windows
from analysis.logsage.drain_templates import DrainTemplateDB
from analysis.logsage.log_filter import filter_log_lines
from analysis.logsage.success_log_fetcher import fetch_success_logs
from analysis.logsage.token_pruner import run_token_pruning


@dataclass
class Stage1Result:
    filtered_blocks: List[str] = field(default_factory=list)
    pruning_stats: Dict = field(default_factory=dict)
    filter_stats: Dict = field(default_factory=dict)
    drain_template_count: int = 0
    success_log_count: int = 0


def run_stage1(
    log_text: str,
    *,
    pipeline_name: str = "",
    failed_step: str = "",
    pipeline_df: Optional[pd.DataFrame] = None,
    share_map: Optional[dict] = None,
    fetch_success: bool = True,
) -> Stage1Result:
    """
    Full LogSage Stage 1: Drain templates → filter → context → prune.
    Operates on raw log text only — does not call parse_log().
    """
    share_map = share_map or {}
    drain_db = DrainTemplateDB()
    success_count = 0

    if fetch_success and pipeline_df is not None and pipeline_name and failed_step:
        _, drain_db = fetch_success_logs(
            pipeline_df,
            share_map,
            pipeline_name,
            failed_step,
            fetch_logs=True,
        )
        success_count = len(drain_db.templates)

    filt = filter_log_lines(log_text, drain_db=drain_db, failed_step=failed_step)
    blocks = expand_context_windows(filt.lines, filt.candidate_indices)
    pruned = run_token_pruning(filt.lines, filt.candidate_indices, blocks)

    return Stage1Result(
        filtered_blocks=pruned.filtered_text_blocks,
        pruning_stats=pruned.stats,
        filter_stats=filt.stats,
        drain_template_count=len(drain_db.templates),
        success_log_count=success_count,
    )
