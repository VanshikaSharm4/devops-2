"""
Environment readiness assessment — derived entirely from Splunk data, no LLM.

Answers: "Is it safe to trigger a pipeline right now?"
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TypedDict

import pandas as pd


ENV_STEPS  = {"securityTest", "deploy", "loadTest", "activation", "reportPerformanceTest"}
CODE_STEPS = {"build", "codeQuality"}

ACTIVE_STATUSES  = {"FAILED", "ERROR", "FINISHED"}
FAILURE_STATUSES = {"FAILED", "ERROR"}


class EnvReadiness(TypedDict):
    status: str              # "READY" | "CAUTION" | "NOT_READY"
    consecutive_failures: int
    last_success_ago: str    # e.g. "2 days ago"
    dominant_step: str       # most common failing step in recent window
    is_env_issue: bool       # True = securityTest/deploy/loadTest dominant (env config, not code)
    recommendation: str


def assess_environment_readiness(
    pipeline_df: pd.DataFrame,
    failed_df: pd.DataFrame,
    recent_n: int = 10,
) -> EnvReadiness:
    """
    Assess whether the AEM environment is safe to deploy to right now.

    Uses only pipeline execution history — no LLM involved.
    Ignores CANCELLED executions (user-triggered cancellations are not env signals).
    """
    if pipeline_df is None or pipeline_df.empty:
        return _unknown()

    # Sort by start time descending, ignore cancellations
    try:
        df = pipeline_df.copy()
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
    dominant_step = ""
    is_env_issue  = False
    if failed_df is not None and not failed_df.empty and "firstFailedStep" in failed_df.columns:
        try:
            recent_failed = failed_df.copy()
            recent_failed["_ts"] = pd.to_datetime(
                recent_failed["Deploy Start Time"].str.replace(r"\s*(PDT|PST|UTC|GMT)$", "", regex=True),
                utc=True, errors="coerce"
            )
            recent_failed = recent_failed.dropna(subset=["_ts"]).sort_values("_ts", ascending=False)
            recent_window = recent_failed.head(recent_n)
            if not recent_window.empty:
                step_counts = recent_window["firstFailedStep"].value_counts()
                dominant_step = step_counts.index[0] if not step_counts.empty else ""
                env_count  = recent_window["firstFailedStep"].isin(ENV_STEPS).sum()
                code_count = recent_window["firstFailedStep"].isin(CODE_STEPS).sum()
                is_env_issue = env_count > code_count
        except Exception:
            pass

    # ── Determine status ──────────────────────────────────────────────────
    if consecutive == 0:
        status = "READY"
    elif consecutive < 3:
        status = "CAUTION"
    elif is_env_issue:
        status = "NOT_READY"
    else:
        status = "CAUTION"

    # ── Recommendation ────────────────────────────────────────────────────
    recommendation = _build_recommendation(
        status, consecutive, dominant_step, is_env_issue, last_success_ago
    )

    return EnvReadiness(
        status=status,
        consecutive_failures=consecutive,
        last_success_ago=last_success_ago,
        dominant_step=dominant_step,
        is_env_issue=is_env_issue,
        recommendation=recommendation,
    )


def _build_recommendation(
    status: str,
    consecutive: int,
    dominant_step: str,
    is_env_issue: bool,
    last_success_ago: str,
) -> str:
    step_label = dominant_step.replace("_", " ") if dominant_step else "unknown"

    if status == "READY":
        return f"Environment looks healthy. Last pipeline succeeded {last_success_ago}."

    if status == "NOT_READY":
        if dominant_step in ("securityTest",):
            return (
                f"{consecutive} consecutive failures at {step_label} — "
                f"AEM node config issue (CRXDE Lite / DavEx likely still active). "
                f"This is not a code problem. Fix the environment before triggering a new pipeline."
            )
        if dominant_step in ("deploy", "activation"):
            return (
                f"{consecutive} consecutive failures at {step_label} — "
                f"infrastructure-level issue, not code. "
                f"Check AEM instance health before triggering a new pipeline."
            )
        return (
            f"{consecutive} consecutive environment-level failures at {step_label}. "
            f"Last success was {last_success_ago}. Fix the environment before re-deploying."
        )

    # CAUTION
    if is_env_issue:
        return (
            f"{consecutive} recent failures at {step_label}. "
            f"Looks like an environment issue — verify AEM node config before triggering."
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
    )
