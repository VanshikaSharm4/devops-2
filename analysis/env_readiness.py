"""
Environment readiness assessment — derived entirely from Splunk data, no LLM.

Answers: "Is it safe to trigger a pipeline right now?"
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple, TypedDict

import pandas as pd

from analysis.ingest import normalize_failed_steps_df, normalize_pipeline_df


ENV_STEPS  = {"securityTest", "deploy", "loadTest", "activation", "reportPerformanceTest"}
CODE_STEPS = {"build", "codeQuality"}

ACTIVE_STATUSES  = {"FAILED", "ERROR", "FINISHED"}
FAILURE_STATUSES = {"FAILED", "ERROR"}

# Default scope for production pre-deploy risk (matches failure_history filter)
DEFAULT_PROD_PIPELINE = "Production Pipeline"


class EnvReadiness(TypedDict):
    status: str              # "READY" | "CAUTION" | "NOT_READY"
    consecutive_failures: int
    last_success_ago: str    # e.g. "2 days ago"
    dominant_step: str       # most common failing step in recent window
    is_env_issue: bool       # True = securityTest/deploy/loadTest dominant (env config, not code)
    recommendation: str
    env_step_failure_count: int  # raw count of env-step failures in recent window (0 if unknown)


def _prepare_pipeline_scope(
    pipeline_df: pd.DataFrame,
    failed_df: pd.DataFrame,
    pipeline_name: Optional[str],
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Deduplicate executions and optionally restrict to one pipeline (e.g. Production).
    Splunk can emit the same executionId dozens of times — counting without dedupe
    inflates consecutive failures (e.g. 8 real failures shown as 262).
    """
    df = normalize_pipeline_df(pipeline_df)

    if pipeline_name and "pipelineName" in df.columns:
        scoped = df[df["pipelineName"] == pipeline_name]
        if not scoped.empty:
            df = scoped

    fdf = failed_df
    if fdf is not None and not fdf.empty:
        if "executionId" in fdf.columns:
            fdf = fdf.copy()
            fdf["executionId"] = fdf["executionId"].astype(str)
            fdf = fdf.drop_duplicates(subset=["executionId"], keep="last")
        if "executionId" in df.columns:
            allowed = set(df["executionId"].astype(str))
            fdf = fdf[fdf["executionId"].isin(allowed)]

    return df, fdf


def assess_environment_readiness(
    pipeline_df: pd.DataFrame,
    failed_df: pd.DataFrame,
    recent_n: int = 10,
    pipeline_name: Optional[str] = DEFAULT_PROD_PIPELINE,
    as_of_date: Optional[str] = None,
) -> EnvReadiness:
    """
    Assess whether the AEM environment is safe to deploy to right now.

    Uses only pipeline execution history — no LLM involved.
    Ignores CANCELLED executions (user-triggered cancellations are not env signals).

    pipeline_name: restrict to this Cloud Manager pipeline (default Production Pipeline).
    as_of_date: ISO date string (e.g. "2026-06-17") — when set, only uses executions
                that started BEFORE this date. Critical for retroactive SHA assessment:
                assessing a SHA from June 17 should use June 17 env state, not today's.
    """
    if pipeline_df is None or pipeline_df.empty:
        return _unknown()

    df, failed_df = _prepare_pipeline_scope(pipeline_df, failed_df, pipeline_name)
    if df.empty:
        return _unknown()

    # Filter to executions before as_of_date if provided
    if as_of_date and "Deploy Start Time" in df.columns:
        try:
            _cutoff = pd.to_datetime(as_of_date, utc=True, errors="coerce")
            if _cutoff is not None and not pd.isna(_cutoff):
                _ts = pd.to_datetime(
                    df["Deploy Start Time"].str.replace(r"\s*(PDT|PST|UTC|GMT)$", "", regex=True),
                    utc=True, errors="coerce"
                )
                df = df[_ts <= _cutoff].copy()
                if df.empty:
                    return _unknown()
        except Exception:
            pass

    # Sort by start time descending, ignore cancellations
    try:
        df = df.copy()
        df["_ts"] = pd.to_datetime(
            df["Deploy Start Time"].str.replace(r"\s*(PDT|PST|UTC|GMT)$", "", regex=True),
            utc=True, errors="coerce"
        )
        df = df.dropna(subset=["_ts"]).sort_values("_ts", ascending=False)
    except Exception:
        return _unknown()

    active = df[df["Status"].isin(ACTIVE_STATUSES)]
    if active.empty:
        return _unknown()

    # ── Consecutive failures from the most recent active execution ────────
    consecutive = 0
    for _, row in active.iterrows():
        if row["Status"] in FAILURE_STATUSES:
            consecutive += 1
        else:
            break

    # ── Last success ──────────────────────────────────────────────────────
    successes = df[df["Status"] == "FINISHED"]
    last_success_ago = "never"
    if not successes.empty:
        last_ts = successes.iloc[0]["_ts"]
        now = datetime.now(timezone.utc)
        diff = now - last_ts
        days = diff.days
        hours = diff.seconds // 3600
        if days >= 1:
            last_success_ago = f"{days} day{'s' if days != 1 else ''} ago"
        else:
            last_success_ago = f"{hours} hour{'s' if hours != 1 else ''} ago"

    # ── Dominant failing step in recent window ────────────────────────────
    # failed_df only has executionId + firstFailedStep — no timestamps.
    # Join with pipeline_df to get timestamps for proper time-ordering.
    dominant_step = ""
    is_env_issue  = False
    if failed_df is not None and not failed_df.empty and "firstFailedStep" in failed_df.columns:
        try:
            # CANCELLED executions have firstFailedStep = the step they were at when cancelled
            # — NOT a genuine failure. Only include FAILED and ERROR executions.
            # This prevents "20 securityTest cancellations" from looking like failures.
            _fdf_filtered = failed_df.copy()
            if "Status" in _fdf_filtered.columns:
                _fdf_filtered = _fdf_filtered[_fdf_filtered["Status"].isin(FAILURE_STATUSES)]

            # Merge with pipeline_df to get timestamps
            merged = _fdf_filtered[["executionId", "firstFailedStep"]].copy()
            merged["executionId"] = merged["executionId"].astype(str)

            pipe_ts = df[["executionId", "_ts"]].copy()
            pipe_ts["executionId"] = pipe_ts["executionId"].astype(str)

            merged = merged.merge(pipe_ts, on="executionId", how="left")
            merged = merged.dropna(subset=["firstFailedStep"])
            merged = merged[merged["firstFailedStep"].str.strip() != ""]

            # Sort by timestamp if available, else use raw order
            if "_ts" in merged.columns and merged["_ts"].notna().any():
                merged = merged.sort_values("_ts", ascending=False)

            recent_window = merged.head(recent_n)
            if not recent_window.empty:
                step_counts = recent_window["firstFailedStep"].value_counts()
                dominant_step = step_counts.index[0] if not step_counts.empty else ""
                env_count  = int(recent_window["firstFailedStep"].isin(ENV_STEPS).sum())
                code_count = recent_window["firstFailedStep"].isin(CODE_STEPS).sum()
                is_env_issue = env_count > code_count
        except Exception:
            pass

    # ── Determine status ──────────────────────────────────────────────────
    if consecutive == 0:
        # Even with no consecutive failures, if securityTest dominates recent history
        # it's a persistent env issue (CRXDE/DavEx active) — flag as CAUTION
        if dominant_step in ENV_STEPS and is_env_issue:
            status = "CAUTION"
        else:
            status = "READY"
    elif consecutive < 2:
        status = "CAUTION"
    elif is_env_issue:
        status = "NOT_READY"
    else:
        status = "CAUTION"

    # ── Failure probability — use window rate when consecutive=0 ─────────
    # consecutive=0 means the last run passed, but the window may still show
    # many env failures. Use the window failure rate as the base probability.
    if consecutive == 0:
        if env_count > 0:
            # Window-based rate: e.g. 6 securityTest failures in 10 runs = 60% base
            _window_rate = env_count / recent_n
            # Discount since last run passed — but don't go below 25% if rate is high
            fail_prob = max(0.05, _window_rate * 0.6)
            score     = fail_prob
        else:
            fail_prob = 0.05
            score     = 0.05

    # ── Recommendation ────────────────────────────────────────────────────
    recommendation = _build_recommendation(
        status, consecutive, dominant_step, is_env_issue, last_success_ago, env_count
    )

    return EnvReadiness(
        status=status,
        consecutive_failures=consecutive,
        last_success_ago=last_success_ago,
        dominant_step=dominant_step,
        is_env_issue=is_env_issue,
        recommendation=recommendation,
        env_step_failure_count=env_count,
    )


def _build_recommendation(
    status: str,
    consecutive: int,
    dominant_step: str,
    is_env_issue: bool,
    last_success_ago: str,
    env_count: int = 0,
) -> str:
    step_label = dominant_step.replace("_", " ") if dominant_step else "unknown"

    if status == "READY":
        return f"Environment looks healthy. Last pipeline succeeded {last_success_ago}."

    if status == "NOT_READY":
        if dominant_step in ("securityTest",):
            return (
                f"{consecutive} consecutive failures at {step_label} — "
                f"Adobe-managed infrastructure issue (AEM node configuration). "
                f"This is not caused by your code. "
                f"If this has been ongoing, raise with Adobe Support."
            )
        if dominant_step in ("deploy", "activation"):
            return (
                f"{consecutive} consecutive failures at {step_label} — "
                f"Adobe-managed infrastructure issue, not code-related. "
                f"If this persists, raise with Adobe Support."
            )
        return (
            f"{consecutive} consecutive environment-level failures at {step_label}. "
            f"Last success was {last_success_ago}. This is an infrastructure issue — not caused by your code."
        )

    # CAUTION
    if is_env_issue:
        if consecutive == 0 and env_count > 0:
            # Last run passed but window shows env failures — use window count
            return (
                f"{env_count} recent failures at {step_label} in the pipeline window "
                f"(last run passed {last_success_ago}). "
                f"Adobe-managed environment has a recurring {step_label} issue — "
                f"this step may fail again. Not caused by your code."
            )
        if consecutive == 0:
            return f"Environment looks stable. Last success: {last_success_ago}."
        return (
            f"{consecutive} recent failures at {step_label}. "
            f"Adobe-managed environment issue — this step may fail again. Not caused by your code."
        )
    return (
        f"{consecutive} recent failures at {step_label}. "
        f"Likely a code issue — review the failing step before triggering a new pipeline."
    )


def _unknown() -> EnvReadiness:
    return EnvReadiness(
        status="UNKNOWN",
        consecutive_failures=0,
        last_success_ago="unknown",
        dominant_step="",
        is_env_issue=False,
        recommendation="Could not determine environment readiness — no pipeline data available.",
        env_step_failure_count=0,
    )
