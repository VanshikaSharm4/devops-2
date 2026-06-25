"""
Prediction Lifecycle Store — Phase 1

Records every risk prediction as PENDING, then enriches it with the actual
pipeline outcome when Splunk data arrives. This creates labeled ground truth
for accuracy measurement and future ML training.

Lifecycle:
    prediction made  → status: PENDING
    actual arrives   → status: RESOLVED  (correct/incorrect labeled)
    reviewed summary → optionally embedded into ChromaDB (Phase 3)

Storage: JSONL file — append-only, one JSON record per line.
         Human readable, no database dependency, trivially portable.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

STORE_PATH = Path(os.getenv("PREDICTION_STORE_PATH", "data/predictions.jsonl"))


# ── Write ─────────────────────────────────────────────────────────────────────

def _prediction_id(commit_sha: str, execution_id: str, program_id: str) -> str:
    key = f"{program_id}:{execution_id}:{commit_sha[:12]}"
    return "pred_" + hashlib.sha256(key.encode()).hexdigest()[:12]


def save_prediction(
    commit_sha: str,
    predicted_risk: str,
    predicted_step: str,
    confidence: int,
    program_id: str,
    execution_id: str = "",
    tenant_id: str = "",
    pipeline_name: str = "",
    modules_at_risk: Optional[List[str]] = None,
    top_factors: Optional[List[str]] = None,
) -> str:
    """
    Save a new PENDING prediction. Returns the prediction ID.
    Called immediately when Risk Assessment produces a result.
    If the same execution + commit already exists, skips writing and returns existing ID.
    """
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)

    pred_id = _prediction_id(commit_sha, execution_id, program_id)

    # Deduplicate — don't write if same prediction already exists
    if STORE_PATH.exists():
        existing_ids = set()
        with open(STORE_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    existing_ids.add(json.loads(line.strip()).get("id", ""))
                except Exception:
                    continue
        if pred_id in existing_ids:
            return pred_id  # already stored, skip

    record = {
        "id":             pred_id,
        "status":         "PENDING",
        "program_id":     program_id,
        "tenant_id":      tenant_id,
        "pipeline_name":  pipeline_name,
        "execution_id":   execution_id,
        "commit_sha":     commit_sha,
        "predicted_risk": predicted_risk,
        "predicted_step": predicted_step,
        "confidence":     confidence,
        "modules_at_risk": modules_at_risk or [],
        "top_factors":    top_factors or [],
        "predicted_at":   datetime.now(timezone.utc).isoformat(),
        # Filled in when outcome arrives
        "resolved_at":    None,
        "actual_status":  None,
        "actual_failed_step": None,
        "correct":        None,
        "evaluation_note": None,
    }

    with open(STORE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    return pred_id


# ── Read ──────────────────────────────────────────────────────────────────────

def load_all() -> List[Dict[str, Any]]:
    """Load all prediction records."""
    if not STORE_PATH.exists():
        return []
    records = []
    with open(STORE_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def load_pending() -> List[Dict[str, Any]]:
    return [r for r in load_all() if r.get("status") == "PENDING"]


def load_resolved() -> List[Dict[str, Any]]:
    return [r for r in load_all() if r.get("status") == "RESOLVED"]


# ── Resolve ───────────────────────────────────────────────────────────────────

def resolve_prediction(
    pred_id: str,
    actual_status: str,
    actual_failed_step: str,
) -> Optional[Dict[str, Any]]:
    """
    Update a PENDING prediction with the real pipeline outcome.
    Rewrites the JSONL file with the updated record.
    Returns the updated record, or None if not found.
    """
    records = load_all()
    updated = None

    for r in records:
        if r["id"] == pred_id:
            r["status"]            = "RESOLVED"
            r["resolved_at"]       = datetime.now(timezone.utc).isoformat()
            r["actual_status"]     = actual_status
            r["actual_failed_step"] = actual_failed_step

            # Label correctness
            predicted_step = r.get("predicted_step", "")
            predicted_risk = r.get("predicted_risk", "").lower()
            actual_failed  = bool(actual_status in ("FAILED", "ERROR"))

            if not actual_failed:
                # Pipeline passed — any non-Low prediction is wrong
                r["correct"] = predicted_risk == "low"
                r["evaluation_note"] = (
                    f"Pipeline FINISHED. "
                    f"{'Correct — predicted Low.' if r['correct'] else f'Incorrect — predicted {predicted_risk} but no failure.'}"
                )
            else:
                # Pipeline failed — check if we got the step right
                step_correct = (
                    predicted_step.lower() == actual_failed_step.lower()
                    if predicted_step and actual_failed_step else False
                )
                risk_correct = predicted_risk in ("high", "medium")
                r["correct"] = risk_correct  # at minimum predicted failure
                r["evaluation_note"] = (
                    f"Pipeline {actual_status} at {actual_failed_step}. "
                    f"Risk direction {'correct' if risk_correct else 'incorrect'}. "
                    f"Step {'correct' if step_correct else f'incorrect (predicted {predicted_step}, actual {actual_failed_step})'}."
                )

            updated = r
            break

    if updated:
        # Rewrite file with updated record
        with open(STORE_PATH, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    return updated


# ── Auto-enrich from Splunk data ──────────────────────────────────────────────

def backfill_execution_ids(pipeline_df) -> int:
    """
    For PENDING predictions that have a commit_sha but no execution_id,
    try to link them to an execution by correlating SHA against pipeline history.

    Uses the same timestamp-correlation approach as git_connector:
    find the execution whose start time is closest AFTER the commit timestamp.

    Returns number of predictions updated with an execution_id.
    """
    import pandas as pd

    pending_no_eid = [p for p in load_pending() if not p.get("execution_id")]
    if not pending_no_eid or pipeline_df is None or pipeline_df.empty:
        return 0

    # Build SHA → execution mapping from Splunk + git correlation
    try:
        from connectors.git_connector import correlate_executions_to_commits
        rows = pipeline_df.to_dict("records")
        sha_map = correlate_executions_to_commits(rows)
        # sha_map: {executionId: {sha, sha_short, title, author}}
        # Reverse: sha → executionId
        sha_to_eid: Dict[str, str] = {
            v["sha"]: k for k, v in sha_map.items() if v.get("sha")
        }
    except Exception:
        return 0

    if not sha_to_eid:
        return 0

    # Load all records, update matching ones
    records = load_all()
    updated = 0
    pred_index = {r["id"]: i for i, r in enumerate(records)}

    for pred in pending_no_eid:
        sha = pred.get("commit_sha", "")
        if not sha:
            continue
        eid = sha_to_eid.get(sha)
        if not eid:
            # Try short SHA match
            eid = next((e for e, v in sha_map.items() if v.get("sha", "").startswith(sha[:12])), None)
        if not eid:
            continue

        idx = pred_index.get(pred["id"])
        if idx is not None:
            records[idx]["execution_id"] = eid
            updated += 1

    if updated:
        with open(STORE_PATH, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    return updated


def enrich_from_splunk(pipeline_df, failed_df) -> int:
    """
    Called after every Splunk data load.
    1. First backfills missing execution_ids via SHA correlation
    2. Then resolves PENDING predictions whose execution has completed
    Returns total number of predictions resolved.
    """
    import pandas as pd

    # Step 1: backfill execution_ids for predictions that are missing them
    backfill_execution_ids(pipeline_df)

    pending = load_pending()
    if not pending:
        return 0

    # Build lookup: executionId → (status, firstFailedStep)
    exec_status: Dict[str, str] = {}
    exec_step:   Dict[str, str] = {}

    if pipeline_df is not None and not pipeline_df.empty:
        for _, row in pipeline_df.iterrows():
            eid = str(row.get("executionId", ""))
            st  = str(row.get("Status", ""))
            if eid and st not in ("RUNNING", ""):
                exec_status[eid] = st

    if failed_df is not None and not failed_df.empty and "firstFailedStep" in failed_df.columns:
        for _, row in failed_df.iterrows():
            eid  = str(row.get("executionId", ""))
            step = str(row.get("firstFailedStep", "") or "")
            if eid and step and step != "nan":
                exec_step[eid] = step

    resolved_count = 0
    for pred in load_pending():  # reload after backfill
        eid = pred.get("execution_id", "")
        if not eid or eid not in exec_status:
            continue

        status      = exec_status[eid]
        failed_step = exec_step.get(eid, "")

        if status in ("FINISHED", "FAILED", "ERROR"):
            resolve_prediction(pred["id"], status, failed_step)
            resolved_count += 1

    return resolved_count


# ── Bulk resolve from manual test Excel ──────────────────────────────────────

def resolve_from_excel(excel_path: str) -> Dict[str, int]:
    """
    Bulk-resolve PENDING predictions using manually recorded test results.

    Excel must have columns: sha (or 'Git SHA'), actual result (or 'Actual Outcome'),
    and optionally 'Actual Failed Step'.

    Matches predictions by commit_sha prefix (first 12 chars).
    Returns {"resolved": N, "not_found": N, "already_resolved": N}
    """
    try:
        import pandas as pd
        df = pd.read_excel(excel_path)
    except Exception as e:
        return {"error": str(e)}

    # Normalize column names
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    # Find SHA column
    sha_col = next((c for c in df.columns if "sha" in c), None)
    # Find result column
    result_col = next((c for c in df.columns if "actual" in c and "result" in c), None) or \
                 next((c for c in df.columns if "actual" in c), None)
    # Find failed step column
    step_col = next((c for c in df.columns if "step" in c and "actual" in c), None) or \
               next((c for c in df.columns if "failed_step" in c), None)

    if not sha_col or not result_col:
        return {"error": f"Could not find SHA or result columns. Found: {list(df.columns)}"}

    pending = {p["commit_sha"]: p for p in load_pending()}

    resolved = not_found = already_resolved = 0

    for _, row in df.iterrows():
        raw_sha = str(row.get(sha_col, "") or "").strip()
        if not raw_sha or raw_sha == "nan":
            continue

        raw_result = str(row.get(result_col, "") or "").strip().upper()
        if not raw_result or raw_result == "NAN":
            continue

        # Normalize result
        if raw_result in ("PASSED", "FINISHED", "SUCCESS", "PASS"):
            actual_status = "FINISHED"
            actual_step   = ""
        elif raw_result in ("FAILED", "FAIL", "FAILURE"):
            actual_status = "FAILED"
            actual_step   = str(row.get(step_col, "") or "").strip() if step_col else ""
        elif raw_result in ("ERROR", "ERR"):
            actual_status = "ERROR"
            actual_step   = str(row.get(step_col, "") or "").strip() if step_col else ""
        else:
            continue  # unknown result, skip

        # Match by full SHA or prefix
        pred = pending.get(raw_sha)
        if not pred:
            pred = next((p for sha, p in pending.items() if sha.startswith(raw_sha[:12]) or raw_sha.startswith(sha[:12])), None)

        if not pred:
            not_found += 1
            continue

        resolve_prediction(pred["id"], actual_status, actual_step)
        resolved += 1

    return {"resolved": resolved, "not_found": not_found, "already_resolved": already_resolved}


# ── Stats ─────────────────────────────────────────────────────────────────────

def accuracy_stats(program_id: str = "") -> Dict[str, Any]:
    """
    Compute accuracy metrics from resolved predictions.
    Optionally filter by program_id.
    """
    records = load_resolved()
    if program_id:
        records = [r for r in records if r.get("program_id") == program_id]

    if not records:
        return {"total": 0, "correct": 0, "accuracy": None, "pending": len(load_pending())}

    total   = len(records)
    correct = sum(1 for r in records if r.get("correct") is True)

    # False positive rate (predicted failure, actual pass)
    false_positives = sum(
        1 for r in records
        if r.get("predicted_risk", "").lower() in ("high", "medium")
        and r.get("actual_status") == "FINISHED"
    )

    # False negative rate (predicted low, actual failure)
    false_negatives = sum(
        1 for r in records
        if r.get("predicted_risk", "").lower() == "low"
        and r.get("actual_status") in ("FAILED", "ERROR")
    )

    # Step accuracy (among predictions where pipeline actually failed)
    actual_failures = [r for r in records if r.get("actual_status") in ("FAILED", "ERROR")]
    step_correct = sum(
        1 for r in actual_failures
        if r.get("predicted_step", "").lower() == r.get("actual_failed_step", "").lower()
    )

    return {
        "total":            total,
        "correct":          correct,
        "accuracy_pct":     round(correct / total * 100, 1) if total else None,
        "false_positives":  false_positives,
        "false_negatives":  false_negatives,
        "fp_rate_pct":      round(false_positives / total * 100, 1) if total else None,
        "fn_rate_pct":      round(false_negatives / total * 100, 1) if total else None,
        "step_accuracy_pct": round(step_correct / len(actual_failures) * 100, 1) if actual_failures else None,
        "pending":          len(load_pending()),
        "resolved":         total,
    }
