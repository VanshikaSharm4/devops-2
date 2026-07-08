"""Tests for environment readiness — dedupe and pipeline scoping."""

import pickle
from pathlib import Path

import pandas as pd

from analysis.env_readiness import DEFAULT_PROD_PIPELINE, assess_environment_readiness


def test_dedupe_fixes_inflated_consecutive_failures():
    """Splunk cache with duplicate rows must not report 262 consecutive failures."""
    cache_path = Path("data/cache/splunk_cache_19905.pkl")
    if not cache_path.exists():
        return

    data = pickle.load(cache_path.open("rb"))
    pdf = data["pipeline_df"]
    fdf = data.get("failed_df")

    raw_active = pdf[pdf["Status"].isin({"FAILED", "ERROR", "FINISHED"})]
    raw_consec = 0
    for _, row in raw_active.iterrows():
        if row["Status"] in ("FAILED", "ERROR"):
            raw_consec += 1
        else:
            break

    env = assess_environment_readiness(pdf, fdf, pipeline_name=DEFAULT_PROD_PIPELINE)
    assert env["consecutive_failures"] < raw_consec or raw_consec < 50
    assert env["consecutive_failures"] <= 20


def test_synthetic_duplicate_rows_deduped():
    """One execution repeated 100 times counts as one failure."""
    rows = []
    for i in range(100):
        rows.append({
            "executionId": "999",
            "Status": "FAILED",
            "Deploy Start Time": "2026-06-29T10:00:00 PDT",
            "pipelineName": DEFAULT_PROD_PIPELINE,
        })
    rows.append({
        "executionId": "998",
        "Status": "FINISHED",
        "Deploy Start Time": "2026-06-28T10:00:00 PDT",
        "pipelineName": DEFAULT_PROD_PIPELINE,
    })
    pdf = pd.DataFrame(rows)
    fdf = pd.DataFrame([{"executionId": "999", "firstFailedStep": "securityTest"}])

    env = assess_environment_readiness(pdf, fdf, pipeline_name=DEFAULT_PROD_PIPELINE)
    assert env["consecutive_failures"] == 1
