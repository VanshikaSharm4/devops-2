"""
Cold Start Initializer

When a new customer is onboarded, this module bootstraps the prediction system
using historical Splunk data — so predictions are useful on day 1 instead of
requiring weeks of warmup.

Two steps:
1. Retroactive prediction resolution — create resolved predictions from
   historical Splunk outcomes (last 30 days). Gives calibration engine
   labeled data immediately.

2. ChromaDB ingestion from historical failures — parse git diffs for failed
   executions and ingest into vector store with tenant metadata. Gives
   Historical Match signal real data on day 1.

No generic/cross-customer data is used. All data is tenant-specific.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional


# ── Step 1: Retroactive prediction resolution ──────────────────────────────────

def retroactive_resolve(
    program_id: str,
    tenant_id: str,
    pipeline_df,
    failed_df,
    git_local_dir: str,
    git_branch: str = "master",
    days_back: int = 30,
    on_progress: Optional[Callable[[str], None]] = None,
    git_url: str = "",
    git_username: str = "",
    git_password: str = "",
) -> Dict[str, int]:
    """
    Create resolved predictions from historical Splunk data.

    For every completed execution in the last `days_back` days:
    - Correlate execution → commit SHA via timestamp matching
    - Create a RESOLVED prediction record with actual outcome
    - This immediately gives the calibration engine labeled data

    Returns {"created": N, "skipped": N, "errors": N}
    """
    import pandas as pd
    from analysis.prediction_store import (
        _prediction_id, _store_path, load_all, _rewrite_tenant
    )

    def _log(msg: str) -> None:
        if on_progress:
            on_progress(msg)
        print(f"  [cold_start] {msg}")

    if pipeline_df is None or pipeline_df.empty:
        return {"created": 0, "skipped": 0, "errors": 0}

    # Get completed executions with known outcomes
    completed = pipeline_df[
        pipeline_df["Status"].isin(["FINISHED", "FAILED", "ERROR"])
    ].drop_duplicates("executionId").copy()

    if completed.empty:
        _log("No completed executions found")
        return {"created": 0, "skipped": 0, "errors": 0}

    _log(f"Found {len(completed)} completed executions — correlating to git SHAs...")

    # Build failed step lookup
    failed_steps: Dict[str, str] = {}
    if failed_df is not None and not failed_df.empty and "firstFailedStep" in failed_df.columns:
        for _, row in failed_df.drop_duplicates("executionId").iterrows():
            eid  = str(row.get("executionId", ""))
            step = str(row.get("firstFailedStep", "") or "")
            if eid and step and step != "nan":
                failed_steps[eid] = step

    # Correlate executions to SHAs
    os.environ["GIT_LOCAL_DIR"] = git_local_dir
    # Set customer-specific git URL/credentials so clone_or_update() fetches
    # from the correct remote (not whatever CM_GIT_REPO_URL is set to in .env)
    if git_url:
        os.environ["CM_GIT_REPO_URL"] = git_url
    if git_username:
        os.environ["CM_GIT_USERNAME"] = git_username
    if git_password:
        os.environ["CM_GIT_PASSWORD"] = git_password
    sha_map: Dict[str, dict] = {}
    try:
        from connectors.git_connector import correlate_executions_to_commits
        rows = completed.to_dict("records")
        sha_map = correlate_executions_to_commits(rows, branch=git_branch)
        _log(f"Correlated {len(sha_map)}/{len(completed)} executions to SHAs")
    except Exception as e:
        _log(f"SHA correlation failed: {e}")

    # Load existing resolved predictions to avoid duplicates
    existing = load_all(program_id)
    existing_ids = {r["id"] for r in existing}

    created = skipped = errors = 0
    new_records = []

    for _, row in completed.iterrows():
        eid    = str(row.get("executionId", ""))
        status = str(row.get("Status", ""))
        sha_info = sha_map.get(eid, {})
        sha    = sha_info.get("sha", "")

        if not sha:
            skipped += 1
            continue

        pred_id = _prediction_id(sha, eid, program_id)
        if pred_id in existing_ids:
            skipped += 1
            continue

        try:
            failed_step = failed_steps.get(eid, "")
            actual_failed = status in ("FAILED", "ERROR")

            # Determine correctness — we don't have a prediction to compare
            # against so we mark as correct=None (historical, not predicted)
            record = {
                "id":                pred_id,
                "status":            "RESOLVED",
                "program_id":        program_id,
                "tenant_id":         tenant_id,
                "pipeline_name":     str(row.get("pipelineName", "")),
                "execution_id":      eid,
                "commit_sha":        sha,
                "commit_title":      sha_info.get("title", ""),
                "commit_author":     sha_info.get("author", ""),
                "predicted_risk":    "unknown",   # retroactive — no prediction was made
                "predicted_step":    "",
                "confidence":        0,
                "modules_at_risk":   [],
                "top_factors":       [],
                "predicted_at":      row.get("Deploy Start Time", ""),
                "env_prediction":    {},
                "commit_prediction": {},
                "primary_driver":    "retroactive",
                "resolved_at":       datetime.now(timezone.utc).isoformat(),
                "actual_status":     status,
                "actual_failed_step": failed_step,
                "correct":           None,   # can't evaluate without prediction
                "correct_env":       None,
                "correct_commit":    None,
                "evaluation_note":   f"Retroactive — {status} at {failed_step or 'unknown step'}. No prediction was made.",
                "is_retroactive":    True,
            }
            new_records.append(record)
            existing_ids.add(pred_id)
            created += 1

            if created % 20 == 0:
                _log(f"  {created} records created...")

        except Exception as e:
            errors += 1

    if new_records:
        all_records = existing + new_records
        _rewrite_tenant(program_id, all_records)
        _log(f"Created {created} retroactive records for program {program_id}")

    return {"created": created, "skipped": skipped, "errors": errors}


# ── Step 2: Ingest historical failures into ChromaDB ──────────────────────────

def ingest_historical_failures(
    program_id: str,
    tenant_id: str,
    pipeline_df,
    failed_df,
    git_local_dir: str,
    git_branch: str = "master",
    max_failures: int = 50,
    on_progress: Optional[Callable[[str], None]] = None,
    git_url: str = "",
    git_username: str = "",
    git_password: str = "",
) -> Dict[str, int]:
    """
    Ingest historical failure patterns into ChromaDB for this customer.

    For each failed execution:
    - Get the commit diff (what changed)
    - Parse error context from the failed step label
    - Upsert into vector store with tenant_id metadata

    This populates Signal 3 (Historical Match) so it works on day 1.
    Uses only Splunk metadata + git diff — no Azure logs needed.

    Returns {"ingested": N, "skipped": N, "errors": N}
    """
    def _log(msg: str) -> None:
        if on_progress:
            on_progress(msg)
        print(f"  [cold_start] {msg}")

    if pipeline_df is None or pipeline_df.empty:
        return {"ingested": 0, "skipped": 0, "errors": 0}

    # Get recent failures with known step
    failed = pipeline_df[
        pipeline_df["Status"].isin(["FAILED", "ERROR"])
    ].drop_duplicates("executionId")

    if failed_df is not None and not failed_df.empty:
        failed = failed.merge(
            failed_df[["executionId", "firstFailedStep"]].drop_duplicates("executionId"),
            on="executionId", how="left"
        )
    else:
        failed["firstFailedStep"] = ""

    # Only ingest failures with a known step
    failed = failed[failed.get("firstFailedStep", "").notna()].head(max_failures)

    if failed.empty:
        _log("No failures with known step found")
        return {"ingested": 0, "skipped": 0, "errors": 0}

    _log(f"Ingesting {len(failed)} historical failures into ChromaDB...")

    # Correlate to SHAs — set customer-specific git remote so fetch goes to correct repo
    os.environ["GIT_LOCAL_DIR"] = git_local_dir
    if git_url:
        os.environ["CM_GIT_REPO_URL"] = git_url
    if git_username:
        os.environ["CM_GIT_USERNAME"] = git_username
    if git_password:
        os.environ["CM_GIT_PASSWORD"] = git_password
    sha_map: Dict[str, dict] = {}
    try:
        from connectors.git_connector import correlate_executions_to_commits
        sha_map = correlate_executions_to_commits(failed.to_dict("records"), branch=git_branch)
    except Exception as e:
        _log(f"SHA correlation failed: {e}")

    ingested = skipped = errors = 0

    for _, row in failed.iterrows():
        eid        = str(row.get("executionId", ""))
        step       = str(row.get("firstFailedStep", "") or "").strip()
        pipeline   = str(row.get("pipelineName", "") or "")
        sha_info   = sha_map.get(eid, {})
        sha        = sha_info.get("sha", "")
        title      = sha_info.get("title", "")

        if not step or not sha:
            skipped += 1
            continue

        try:
            # Get changed files from git diff
            from connectors.git_connector import get_commit_diff
            diff_data     = get_commit_diff(git_local_dir, sha)
            changed_files = diff_data.get("changed_files", [])
            author        = diff_data.get("author", "")

            # Build error context from what we know (no Azure logs)
            error_msg = (
                f"Pipeline {row.get('Status','')} at {step}. "
                f"Commit: {title[:100]}. "
                f"Changed: {', '.join(changed_files[:5])}"
            )

            # Determine root cause hint from step type
            step_hints = {
                "build":             "Build or unit test failure. Check Maven compilation and test output.",
                "codeQuality":       "Code quality gate failed. Check SonarQube or code coverage rules.",
                "securityTest":      "Security test failed. Check CRXDE, DavEx, WebDAV, dispatcher config.",
                "deploy":            "Deployment failed. Check package installation and bundle activation.",
                "loadTest":          "Load test failed. Check performance thresholds and AEM response times.",
                "reportPerformanceTest": "Performance test failed. Check resource utilization metrics.",
            }
            root_cause = step_hints.get(step, f"Pipeline failed at {step}.")

            from vector_store.store import store_failure
            store_failure(
                execution_id  = f"hist:{eid}",
                step          = step,
                error_type    = step,
                error_message = error_msg,
                key_lines     = changed_files[:5] + [title[:80]],
                root_cause    = root_cause,
                fix           = f"Review {step} logs for execution {eid} and fix the underlying issue.",
                pipeline      = pipeline,
                extra_meta    = {
                    "tenant_id":    tenant_id,
                    "program_id":   program_id,
                    "environment":  "prod" if "prod" in pipeline.lower() else "stage",
                    "source":       "historical_ingest",
                    "commit_sha":   sha,
                    "author":       author,
                    "changed_files": ",".join(changed_files[:10]),
                },
            )
            ingested += 1
            _log(f"  Ingested: eid={eid} step={step} sha={sha[:8]}")

        except Exception as e:
            errors += 1
            _log(f"  Skipped eid={eid}: {e}")

    _log(f"Done — ingested={ingested} skipped={skipped} errors={errors}")
    return {"ingested": ingested, "skipped": skipped, "errors": errors}


# ── Combined initializer ───────────────────────────────────────────────────────

def initialize_customer(
    program_id: str,
    tenant_id: str,
    pipeline_df,
    failed_df,
    git_local_dir: str,
    git_branch: str = "master",
    on_progress: Optional[Callable[[str], None]] = None,
    git_url: str = "",
    git_username: str = "",
    git_password: str = "",
) -> Dict[str, dict]:
    """
    Full cold start initialization for a new customer.
    Run once after onboarding — takes 2-5 minutes.

    Returns summary of what was done.
    """
    results = {}

    if on_progress:
        on_progress("Step 1/2: Creating retroactive prediction records...")
    results["retroactive"] = retroactive_resolve(
        program_id=program_id, tenant_id=tenant_id,
        pipeline_df=pipeline_df, failed_df=failed_df,
        git_local_dir=git_local_dir, git_branch=git_branch,
        on_progress=on_progress,
        git_url=git_url, git_username=git_username, git_password=git_password,
    )

    if on_progress:
        on_progress("Step 2/2: Ingesting historical failures into ChromaDB...")
    results["chromadb"] = ingest_historical_failures(
        program_id=program_id, tenant_id=tenant_id,
        pipeline_df=pipeline_df, failed_df=failed_df,
        git_local_dir=git_local_dir, git_branch=git_branch,
        on_progress=on_progress,
        git_url=git_url, git_username=git_username, git_password=git_password,
    )

    return results
