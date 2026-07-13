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
from analysis.paths import cache_dir, splunk_exports_dir



def _get_ctx(attr: str, env_key: str, default: str = "") -> str:
    """Read customer-specific value from thread-safe context, fallback to os.environ."""
    try:
        import analysis.customer_context as _cc
        val = getattr(_cc, f"get_{attr}")()
        return val if val else os.getenv(env_key, default)
    except Exception:
        return os.getenv(env_key, default)

def _pipeline_csv() -> str:
    return str(splunk_exports_dir() / "pipelines-list.csv")


def _failed_step_csv() -> str:
    return str(splunk_exports_dir() / "first-failed-steps.csv")


def _share_names_csv() -> str:
    return str(splunk_exports_dir() / "share-names.csv")


# Disk cache — one file per program ID so switching customers never loses data
CACHE_DIR     = cache_dir()
CACHE_TTL_MIN = int(os.getenv("SPLUNK_CACHE_TTL_MINUTES", "30"))


def _cache_file(program_id: Optional[int] = None) -> Path:
    pid = program_id or int(_get_ctx("program_id", "PROGRAM_ID"))
    return CACHE_DIR / f"splunk_cache_{pid}.pkl"


# Keep CACHE_FILE as an alias for the current program's cache (backward compat)
CACHE_FILE = _cache_file()


def _use_splunk_api() -> bool:
    return bool(os.getenv("SPLUNK_USERNAME") and os.getenv("SPLUNK_PASSWORD"))


# ── Disk cache helpers ────────────────────────────────────────────────────────

def _cache_is_fresh(program_id: Optional[int] = None) -> bool:
    """True if cache file for this program exists and is younger than CACHE_TTL_MIN."""
    cf = _cache_file(program_id)
    if not cf.exists():
        return False
    age_minutes = (time.time() - cf.stat().st_mtime) / 60
    return age_minutes < CACHE_TTL_MIN


def _cache_exists(program_id: Optional[int] = None) -> bool:
    """True if ANY cache exists for this program (fresh or stale)."""
    return _cache_file(program_id).exists()


def normalize_pipeline_df(pipeline_df: pd.DataFrame) -> pd.DataFrame:
    """
    One row per executionId. Splunk exports can repeat the same execution many
    times (event fan-out) — without deduping, env consecutive-failure counts inflate.
    """
    if pipeline_df is None or pipeline_df.empty or "executionId" not in pipeline_df.columns:
        return pipeline_df
    df = pipeline_df.copy()
    df["executionId"] = df["executionId"].astype(str)
    # keep="last" by Deploy Start Time so a re-queried execution with updated
    # status (e.g. RUNNING→FINISHED) overwrites the stale row, not vice versa.
    if "Deploy Start Time" in df.columns:
        df = df.sort_values("Deploy Start Time", ascending=True, na_position="first")
    return df.drop_duplicates(subset=["executionId"], keep="last")


def normalize_failed_steps_df(failed_steps_df: pd.DataFrame) -> pd.DataFrame:
    """One failed-step row per executionId — keep last to get final status."""
    if failed_steps_df is None or failed_steps_df.empty or "executionId" not in failed_steps_df.columns:
        return failed_steps_df
    df = failed_steps_df.copy()
    df["executionId"] = df["executionId"].astype(str)
    return df.drop_duplicates(subset=["executionId"], keep="last")


def _normalize_splunk_frames(
    pipeline_df: pd.DataFrame,
    failed_df: pd.DataFrame,
    failed_steps_df: pd.DataFrame,
    share_map: Dict[str, str],
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, str]]:
    pipeline_df = normalize_pipeline_df(pipeline_df)
    failed_steps_df = normalize_failed_steps_df(failed_steps_df)
    if failed_df is not None and not failed_df.empty and "executionId" in failed_df.columns:
        failed_df = failed_df.copy()
        failed_df["executionId"] = failed_df["executionId"].astype(str)
        failed_df = failed_df.drop_duplicates(subset=["executionId"], keep="last")
    return pipeline_df, failed_df, failed_steps_df, share_map


def _load_cache(program_id: Optional[int] = None) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, str]]:
    cf = _cache_file(program_id)
    with open(cf, "rb") as f:
        data = pickle.load(f)
    age = round((time.time() - cf.stat().st_mtime) / 60, 1)
    print(f"  [ingest] Loaded from disk cache (age: {age} min, TTL: {CACHE_TTL_MIN} min)")
    return _normalize_splunk_frames(
        data["pipeline_df"], data["failed_df"], data["failed_steps_df"], data["share_map"]
    )


def _save_cache(
    pipeline_df: pd.DataFrame,
    failed_df: pd.DataFrame,
    failed_steps_df: pd.DataFrame,
    share_map: Dict[str, str],
    program_id: Optional[int] = None,
) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cf = _cache_file(program_id)
    with open(cf, "wb") as f:
        pickle.dump({
            "pipeline_df":    pipeline_df,
            "failed_df":      failed_df,
            "failed_steps_df": failed_steps_df,
            "share_map":      share_map,
        }, f)
    print(f"  [ingest] Results cached to disk ({cf})")


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
    skip_share_names: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, str]]:
    """
    Fetch pipeline and failure data from Splunk REST API in PARALLEL.

    skip_share_names=True: skip the Azure share names query (used during risk
    assessment where log URLs aren't needed — saves ~3-8s per fetch).
    Share names are only needed for the Failure Pinpoint log download feature.
    """
    from connectors.splunk_connector import (
        fetch_failed_steps,
        fetch_pipeline_list,
        fetch_share_names,
    )

    pid = program_id or int(_get_ctx("program_id", "PROGRAM_ID"))

    # Submit pipeline + failed steps in parallel.
    # Azure share names are optional — skip during risk assessment to save time.
    with ThreadPoolExecutor(max_workers=3) as pool:
        future_pipeline = pool.submit(fetch_pipeline_list, pid)
        future_failed   = pool.submit(fetch_failed_steps,  pid)
        future_shares   = (
            None if skip_share_names
            else pool.submit(fetch_share_names, pid)
        )

        pipeline_df     = future_pipeline.result()
        failed_steps_df = future_failed.result()
        share_names_dict = {} if future_shares is None else future_shares.result()

    failed_df = get_failed_executions(pipeline_df, failed_steps_df)
    share_map = {str(eid): str(sname) for eid, sname in share_names_dict.items()}
    return _normalize_splunk_frames(pipeline_df, failed_df, failed_steps_df, share_map)


def load_csv_data(
    pipeline_csv: str = None,
    failed_step_csv: str = None,
    share_names_csv: str = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, str]]:
    """Load all three Splunk CSV exports. CSV-only — no Splunk API."""
    if pipeline_csv is None:
        pipeline_csv = str(splunk_exports_dir() / "pipelines-list.csv")
    if failed_step_csv is None:
        failed_step_csv = str(splunk_exports_dir() / "first-failed-steps.csv")
    if share_names_csv is None:
        share_names_csv = str(splunk_exports_dir() / "share-names.csv")
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
    return _normalize_splunk_frames(pipeline_df, failed_df, failed_steps_df, share_map)


def load_data(
    program_id: Optional[int] = None,
    force_csv: bool = False,
    force_refresh: bool = False,
    skip_share_names: bool = True,
    stale_while_revalidate: bool = True,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, str]]:
    """
    Smart loader with stale-while-revalidate:
      1. Cache fresh?   → return instantly from disk
      2. Cache stale?   → return stale data NOW + trigger background refresh
      3. No cache?      → fetch live (blocks until complete)
      4. Splunk down?   → use stale cache silently

    skip_share_names=True (default): skip Azure share names fetch during risk
    assessment — they're only needed for Failure Pinpoint log downloads.
    Saves 3-8s per cold fetch.

    stale_while_revalidate=True (default): return stale cache immediately,
    refresh in background. Developer sees results instantly; fresh data on
    next assessment.
    """
    pid = program_id or int(_get_ctx("program_id", "PROGRAM_ID"))

    if not force_csv and _use_splunk_api():
        if not force_refresh and _cache_is_fresh(pid):
            print(f"  [ingest] Loaded from disk cache (fresh)")
            return _load_cache(pid)

        # Stale cache exists — return it immediately and refresh in background
        if stale_while_revalidate and _cache_exists(pid):
            import threading as _threading

            def _bg_refresh():
                try:
                    result = load_live_data(pid, skip_share_names=skip_share_names)
                    _save_cache(*result, program_id=pid)
                    print(f"  [ingest] Background refresh complete for program {pid}")
                except Exception as _e:
                    print(f"  [ingest] Background refresh failed: {_e}")

            _t = _threading.Thread(target=_bg_refresh, daemon=True)
            _t.start()
            print(f"  [ingest] Returning stale cache, refreshing in background…")
            return _load_cache(pid)

        # No cache — must fetch synchronously
        print(f"  [ingest] Fetching from Splunk ({'2 queries — skipping share names' if skip_share_names else '3 parallel queries'})…")
        try:
            result = load_live_data(pid, skip_share_names=skip_share_names)
            _save_cache(*result, program_id=pid)
            return result
        except Exception as live_err:
            _err_msg = f"{type(live_err).__name__}: {live_err}"
            print(f"  [ingest] Splunk API failed — {_err_msg}")
            try:
                import json as _j, time as _t
                (CACHE_DIR / "splunk_error.json").write_text(
                    _j.dumps({"error": _err_msg[:400], "ts": _t.time()})
                )
            except Exception:
                pass
            if _cache_exists(pid):
                print("  [ingest] Falling back to stale disk cache…")
                return _load_cache(pid)
            print("  [ingest] No cache — loading from CSV, share_map empty…")
            pdf, fdf, fsteps, _ = load_csv_data()
            return pdf, fdf, fsteps, {}
    else:
        if _cache_exists(pid):
            return _load_cache(pid)
        print("  [ingest] Using CSV exports (no Splunk creds, no cache)…")
        pdf, fdf, fsteps, _ = load_csv_data()
        return pdf, fdf, fsteps, {}


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
            log_text = get_log_for_execution(share_name, failed_step, execution_id)
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
    # Success rate excludes CANCELLED — user-triggered cancellations are not failures.
    # Including them deflates the rate: IDFC showed 7.7% success because most were
    # cancelled retriggers, not genuine pipeline failures. This caused every commit
    # to score as "historically unstable" → always REVIEW BEFORE PROMOTING.
    _non_cancelled = total - cancelled
    rate = round(finished / _non_cancelled * 100, 1) if _non_cancelled else 0.0
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

    pid = _get_ctx("program_id", "PROGRAM_ID")
    pipeline_df, failed_df, _, share_map = load_data(
        program_id=int(pid) if pid else None,
        force_csv=force_csv,
    )
    summary = build_execution_summary(pipeline_df)
    patterns = summarize_failures(failed_df)
    error_details = collect_error_details(failed_df, share_map, fetch_logs=fetch_logs)
    stuck = find_stuck_executions(pipeline_df)

    from models.bundle import FailureHistory

    history = FailureHistory()
    if include_history:
        history = build_failure_history(failed_df, patterns, error_details, pipeline_df)

    bundle = AnalysisBundle(
        program_id=_get_ctx("program_id", "PROGRAM_ID"),
        repo=_get_ctx("git_url", "CM_GIT_REPO_URL"),
        window_days=30,
        execution_summary=summary,
        failure_patterns=patterns,
        error_details=error_details,
        stuck_executions=stuck,
        failure_history=history,
    )
    return bundle, pipeline_df, failed_df, share_map
