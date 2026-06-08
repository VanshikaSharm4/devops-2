"""Shared pipeline data ingestion — Splunk API (live) or CSV fallback."""

from __future__ import annotations

import os
import pickle
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from connectors.azure_connector import get_log_for_execution
from connectors.splunk_csv_reader import (
    build_failed_share_map,
    get_failed_executions,
    summarize_failures,
)
from models.bundle import AnalysisBundle, ErrorDetail, ExecutionSummary
from parsers.log_parser import parse_log

PIPELINE_CSV    = "data/splunk_exports/pipelines-list.csv"
FAILED_STEP_CSV = "data/splunk_exports/first-failed-steps.csv"
SHARE_NAMES_CSV = "data/splunk_exports/share-names.csv"

# Disk cache — keeps Splunk results so repeat loads are instant
CACHE_DIR     = Path("data/cache")
CACHE_FILE    = CACHE_DIR / "splunk_cache.pkl"
# Default TTL: 30 min. Override with SPLUNK_CACHE_TTL_MINUTES env var.
CACHE_TTL_MIN = int(os.getenv("SPLUNK_CACHE_TTL_MINUTES", "30"))


def _use_splunk_api() -> bool:
    return bool(os.getenv("SPLUNK_USERNAME") and os.getenv("SPLUNK_PASSWORD"))


# ── Disk cache helpers ────────────────────────────────────────────────────────

def _cache_is_fresh() -> bool:
    """True if cache file exists and is younger than CACHE_TTL_MIN."""
    if not CACHE_FILE.exists():
        return False
    age_minutes = (time.time() - CACHE_FILE.stat().st_mtime) / 60
    return age_minutes < CACHE_TTL_MIN


def _load_cache() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, str]]:
    with open(CACHE_FILE, "rb") as f:
        data = pickle.load(f)
    age = round((time.time() - CACHE_FILE.stat().st_mtime) / 60, 1)
    print(f"  [ingest] Loaded from disk cache (age: {age} min, TTL: {CACHE_TTL_MIN} min)")
    return data["pipeline_df"], data["failed_df"], data["failed_steps_df"], data["share_map"]


def _save_cache(
    pipeline_df: pd.DataFrame,
    failed_df: pd.DataFrame,
    failed_steps_df: pd.DataFrame,
    share_map: Dict[str, str],
) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "wb") as f:
        pickle.dump({
            "pipeline_df":    pipeline_df,
            "failed_df":      failed_df,
            "failed_steps_df": failed_steps_df,
            "share_map":      share_map,
        }, f)
    print(f"  [ingest] Results cached to disk ({CACHE_FILE})")


def clear_cache() -> None:
    """Force-clear the disk cache so next load re-fetches from Splunk."""
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()
        print("  [ingest] Cache cleared.")


def cache_info() -> dict:
    """Return cache status for display in the dashboard."""
    if not CACHE_FILE.exists():
        return {"exists": False}
    age_min = round((time.time() - CACHE_FILE.stat().st_mtime) / 60, 1)
    return {
        "exists":    True,
        "age_min":   age_min,
        "ttl_min":   CACHE_TTL_MIN,
        "fresh":     age_min < CACHE_TTL_MIN,
        "path":      str(CACHE_FILE),
    }


# ── Live data loader (parallel queries) ───────────────────────────────────────

def load_live_data(
    program_id: Optional[int] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, str]]:
    """
    Fetch all three datasets from Splunk REST API in PARALLEL.
    All three jobs are submitted simultaneously — total time ≈ slowest single query
    instead of sum of all three.
    """
    from connectors.splunk_connector import (
        fetch_failed_steps,
        fetch_pipeline_list,
        fetch_share_names,
    )

    pid = program_id or int(os.getenv("PROGRAM_ID", "19905"))

    # Submit all 3 Splunk jobs at the same time
    with ThreadPoolExecutor(max_workers=3) as pool:
        future_pipeline = pool.submit(fetch_pipeline_list, pid)
        future_failed   = pool.submit(fetch_failed_steps,  pid)
        future_shares   = pool.submit(fetch_share_names,   pid)

        # Collect results — raises if any query failed
        pipeline_df     = future_pipeline.result()
        failed_steps_df = future_failed.result()
        share_names_dict = future_shares.result()

    failed_df = get_failed_executions(pipeline_df, failed_steps_df)
    share_map = {
        eid: sname
        for eid, sname in share_names_dict.items()
        if eid in set(failed_steps_df["executionId"].astype(str))
    }
    return pipeline_df, failed_df, failed_steps_df, share_map


def load_csv_data(
    pipeline_csv: str = PIPELINE_CSV,
    failed_step_csv: str = FAILED_STEP_CSV,
    share_names_csv: str = SHARE_NAMES_CSV,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, str]]:
    """Load all three Splunk CSV exports. CSV-only — no Splunk API."""
    from connectors.splunk_csv_reader import (
        load_failed_steps,
        load_pipeline_list,
        load_share_names,
    )

    pipeline_df     = load_pipeline_list(pipeline_csv)
    failed_steps_df = load_failed_steps(failed_step_csv)
    failed_df       = get_failed_executions(pipeline_df, failed_steps_df)
    all_share_names = load_share_names(share_names_csv)
    share_map       = build_failed_share_map(all_share_names, failed_steps_df)
    return pipeline_df, failed_df, failed_steps_df, share_map


def load_data(
    program_id: Optional[int] = None,
    force_csv: bool = False,
    force_refresh: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, str]]:
    """
    Smart loader with disk cache:
      1. Cache fresh?  → load from disk instantly
      2. Splunk creds? → fetch in parallel, save to disk
      3. Otherwise     → fall back to CSV exports

    force_refresh=True skips the cache and re-fetches from Splunk.
    Override cache TTL with env var: SPLUNK_CACHE_TTL_MINUTES (default 30).
    """
    if not force_csv and _use_splunk_api():
        if not force_refresh and _cache_is_fresh():
            return _load_cache()

        print("  [ingest] Fetching from Splunk (3 parallel queries)...")
        try:
            result = load_live_data(program_id)
            _save_cache(*result)
            return result
        except Exception as live_err:
            _err_msg = f"{type(live_err).__name__}: {live_err}"
            print(f"  [ingest] Splunk API failed — {_err_msg}")
            # Persist the error so dashboard can show exact reason
            try:
                import json as _j, time as _t
                (CACHE_DIR / "splunk_error.json").write_text(
                    _j.dumps({"error": _err_msg[:400], "ts": _t.time()})
                )
            except Exception:
                pass
            if CACHE_FILE.exists():
                print("  [ingest] Falling back to stale disk cache...")
                return _load_cache()
            print("  [ingest] No cache — falling back to CSV exports...")
            return load_csv_data()
    else:
        print("  [ingest] Using CSV exports...")
        return load_csv_data()


def collect_error_details(
    failed_df: pd.DataFrame,
    share_map: dict,
    fetch_logs: bool = True,
) -> List[ErrorDetail]:
    """For each unique firstFailedStep, fetch one representative log and parse it."""
    seen_steps = set()
    error_details: List[ErrorDetail] = []

    for _, row in failed_df.iterrows():
        execution_id = str(row["executionId"])
        failed_step = str(row.get("firstFailedStep", ""))

        if not failed_step or failed_step == "nan":
            continue
        if failed_step in seen_steps:
            continue

        share_name = share_map.get(execution_id)
        parsed = {"error_type": "unknown", "error_message": "No log fetched"}

        if fetch_logs and share_name:
            print(f"  Fetching log for execution {execution_id} (step: {failed_step})...")
            log_text = get_log_for_execution(share_name, failed_step)
            result = parse_log(failed_step, log_text)
            # parse_log returns LogParseResult (Pydantic); ErrorDetail.parsed_error expects dict
            parsed = result.model_dump() if hasattr(result, "model_dump") else result
        elif not share_name:
            parsed = {"error_type": "no_share", "error_message": f"No Azure share for {execution_id}"}

        error_details.append(
            ErrorDetail(
                execution_id=execution_id,
                failed_step=failed_step,
                pipeline=str(row.get("pipelineName", "")),
                parsed_error=parsed,
            )
        )
        seen_steps.add(failed_step)

    return error_details


def find_stuck_executions(pipeline_df: pd.DataFrame, threshold_minutes: int = 120) -> List[dict]:
    stuck = pipeline_df[
        (pipeline_df["Status"] == "CANCELLED")
        & (pipeline_df["Duration (Min)"] > threshold_minutes)
    ].copy()
    if stuck.empty:
        return []
    cols = ["executionId", "pipelineName", "Duration (Min)", "Deploy Start Time"]
    cols = [c for c in cols if c in stuck.columns]
    return (
        stuck[cols]
        .sort_values("Duration (Min)", ascending=False)
        .head(10)
        .to_dict(orient="records")
    )


def get_execution_by_id(pipeline_df: pd.DataFrame, execution_id: str) -> Optional[dict]:
    row = pipeline_df[pipeline_df["executionId"].astype(str) == str(execution_id)]
    if row.empty:
        return None
    return row.iloc[0].to_dict()


def build_execution_summary(pipeline_df: pd.DataFrame) -> ExecutionSummary:
    total = len(pipeline_df)
    finished = len(pipeline_df[pipeline_df["Status"] == "FINISHED"])
    failed = len(pipeline_df[pipeline_df["Status"].isin(["FAILED", "ERROR"])])
    cancelled = len(pipeline_df[pipeline_df["Status"] == "CANCELLED"])
    rate = round(finished / total * 100, 1) if total else 0.0
    return ExecutionSummary(
        total_executions=total,
        finished=finished,
        failed_or_error=failed,
        cancelled=cancelled,
        success_rate_pct=rate,
    )


def build_base_bundle(
    fetch_logs: bool = True,
    include_history: bool = True,
    force_csv: bool = False,
) -> Tuple[AnalysisBundle, pd.DataFrame, pd.DataFrame, Dict[str, str]]:
    """Build AnalysisBundle — uses live Splunk API when credentials available, else CSVs."""
    from analysis.failure_history import build_failure_history

    pipeline_df, failed_df, _, share_map = load_data(force_csv=force_csv)
    summary = build_execution_summary(pipeline_df)
    patterns = summarize_failures(failed_df)
    error_details = collect_error_details(failed_df, share_map, fetch_logs=fetch_logs)
    stuck = find_stuck_executions(pipeline_df)

    from models.bundle import FailureHistory

    history = FailureHistory()
    if include_history:
        history = build_failure_history(failed_df, patterns, error_details, pipeline_df)

    bundle = AnalysisBundle(
        program_id=os.getenv("PROGRAM_ID", "19905"),
        repo=os.getenv("CM_GIT_REPO_URL", ""),
        window_days=30,
        execution_summary=summary,
        failure_patterns=patterns,
        error_details=error_details,
        stuck_executions=stuck,
        failure_history=history,
    )
    return bundle, pipeline_df, failed_df, share_map
