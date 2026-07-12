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

def _resolve_store_dir() -> Path:
    from analysis.paths import data_dir
    _env = os.getenv("PREDICTION_STORE_DIR", "")
    return Path(_env) if _env else data_dir() / "predictions"


def _resolve_legacy_path() -> Path:
    from analysis.paths import data_dir
    _env = os.getenv("PREDICTION_STORE_PATH", "")
    return Path(_env) if _env else data_dir() / "predictions.jsonl"


_STORE_DIR   = _resolve_store_dir()
_LEGACY_PATH = _resolve_legacy_path()


def _store_path(program_id: str = "") -> Path:
    """
    Per-tenant prediction file. Hard isolation — IDFC predictions never
    live in the same file as HDFC predictions.

    data/predictions/19905.jsonl  ← IDFC
    data/predictions/16360.jsonl  ← HDFC
    data/predictions/465.jsonl    ← Malaysia
    data/predictions/unknown.jsonl ← fallback

    Legacy single-file (data/predictions.jsonl) is migrated on first write.
    """
    _STORE_DIR.mkdir(parents=True, exist_ok=True)
    pid = str(program_id).strip() if program_id else "unknown"
    return _STORE_DIR / f"{pid}.jsonl"


def _migrate_legacy() -> None:
    """One-time migration from single predictions.jsonl → per-tenant files."""
    if not _LEGACY_PATH.exists():
        return
    try:
        with open(_LEGACY_PATH, encoding="utf-8") as f:
            records = [json.loads(l) for l in f if l.strip()]
        if not records:
            return
        by_program: Dict[str, list] = {}
        for r in records:
            pid = str(r.get("program_id", "unknown"))
            by_program.setdefault(pid, []).append(r)
        for pid, recs in by_program.items():
            dest = _store_path(pid)
            # Only migrate records not already in the partition
            existing_ids: set = set()
            if dest.exists():
                with open(dest, encoding="utf-8") as f:
                    for l in f:
                        try:
                            existing_ids.add(json.loads(l.strip()).get("id", ""))
                        except Exception:
                            pass
            with open(dest, "a", encoding="utf-8") as f:
                for r in recs:
                    if r.get("id", "") not in existing_ids:
                        f.write(json.dumps(r) + "\n")
        # Rename legacy file so migration doesn't run again
        _LEGACY_PATH.rename(_LEGACY_PATH.with_suffix(".migrated"))
        print(f"  [predictions] Migrated {len(records)} records to per-tenant files.")
    except Exception as e:
        print(f"  [predictions] Migration warning: {e}")


# Run migration on import (one-time, safe to call repeatedly)
_migrate_legacy()

# Keep STORE_PATH for any code that still references it directly (backward compat)
STORE_PATH = _LEGACY_PATH


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
    env_prediction: Optional[Dict[str, Any]] = None,
    commit_prediction: Optional[Dict[str, Any]] = None,
    primary_driver: str = "llm",
) -> str:
    """
    Save a new PENDING prediction. Returns the prediction ID.
    Written to the tenant-partitioned file (data/predictions/{program_id}.jsonl).
    If the same execution + commit already exists, skips writing and returns existing ID.
    """
    _path = _store_path(program_id)

    pred_id = _prediction_id(commit_sha, execution_id, program_id)

    # Deduplicate — don't write if same prediction already exists
    if _path.exists():
        existing_ids = set()
        with open(_path, encoding="utf-8") as f:
            for line in f:
                try:
                    existing_ids.add(json.loads(line.strip()).get("id", ""))
                except Exception:
                    continue
        if pred_id in existing_ids:
            return pred_id  # already stored, skip

    record = {
        "id":               pred_id,
        "status":           "PENDING",
        "program_id":       program_id,
        "tenant_id":        tenant_id,
        "pipeline_name":    pipeline_name,
        "execution_id":     execution_id,
        "commit_sha":       commit_sha,
        "predicted_risk":   predicted_risk,
        "predicted_step":   predicted_step,
        "confidence":       confidence,
        "modules_at_risk":  modules_at_risk or [],
        "top_factors":      top_factors or [],
        "predicted_at":     datetime.now(timezone.utc).isoformat(),
        # Split signals — environment vs commit (env failures are not model errors)
        "env_prediction":    env_prediction or {},
        "commit_prediction": commit_prediction or {},
        "primary_driver":    primary_driver,
        # Filled in when outcome arrives
        "resolved_at":      None,
        "actual_status":    None,
        "actual_failed_step": None,
        "correct":          None,
        "correct_env":      None,   # was env signal correct?
        "correct_commit":   None,   # was code signal correct?
        "evaluation_note":  None,
    }

    with open(_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    return pred_id


# ── Read ──────────────────────────────────────────────────────────────────────

def _load_from_path(path: Path) -> List[Dict[str, Any]]:
    """Load records from a single JSONL file."""
    if not path.exists():
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def load_all(program_id: str = "") -> List[Dict[str, Any]]:
    """
    Load prediction records.
    If program_id provided: load only that tenant's file.
    If not: load all tenant files (for global stats/admin).
    """
    if program_id:
        return _load_from_path(_store_path(program_id))

    # Load all tenant files
    records = []
    if _STORE_DIR.exists():
        for f in sorted(_STORE_DIR.glob("*.jsonl")):
            records.extend(_load_from_path(f))
    # Also include legacy if not yet migrated
    if _LEGACY_PATH.exists():
        records.extend(_load_from_path(_LEGACY_PATH))
    return records


def _load_all_compat() -> List[Dict[str, Any]]:
    """Backward compat wrapper — loads all."""
    return load_all()


def _load_tenant(program_id: str) -> List[Dict[str, Any]]:
    """Load only records for a specific tenant."""
    return _load_from_path(_store_path(program_id))


def _rewrite_tenant(program_id: str, records: List[Dict[str, Any]]) -> None:
    """Rewrite a tenant's partition file atomically."""
    path = _store_path(program_id)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# Legacy compat — code still calling with no args gets all records
def _load_legacy_compat(program_id: str = "") -> List[Dict[str, Any]]:
    if program_id:
        return _load_from_path(_store_path(program_id))
    # Load file that matches current PROGRAM_ID env var if set
    _pid = os.getenv("PROGRAM_ID", "")
    if _pid:
        return _load_from_path(_store_path(_pid))
    return load_all()


def load_all_records() -> List[Dict[str, Any]]:
    """Load all prediction records across ALL tenants."""
    records = []
    if not STORE_PATH.exists():
        pass
    if _STORE_DIR.exists():
        for f in sorted(_STORE_DIR.glob("*.jsonl")):
            records.extend(_load_from_path(f))
    return records


def _load_compat() -> List[Dict[str, Any]]:
    """For code that uses STORE_PATH — reads current tenant's file."""
    pid = os.getenv("PROGRAM_ID", "")
    return _load_from_path(_store_path(pid)) if pid else load_all()


def load_pending(program_id: str = "") -> List[Dict[str, Any]]:
    return [r for r in load_all(program_id) if r.get("status") == "PENDING"]


def load_resolved(program_id: str = "") -> List[Dict[str, Any]]:
    return [r for r in load_all(program_id) if r.get("status") == "RESOLVED"]


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
    # Find which tenant owns this prediction (scan tenant files)
    _owner_program = ""
    if _STORE_DIR.exists():
        for _tf in _STORE_DIR.glob("*.jsonl"):
            _tenant_recs = _load_from_path(_tf)
            if any(r.get("id") == pred_id for r in _tenant_recs):
                _owner_program = _tf.stem  # filename = program_id
                break

    records = load_all(_owner_program)
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

            # Env step categories
            _ENV_STEPS  = {"securityTest", "deploy", "loadTest", "activation", "reportPerformanceTest"}
            _CODE_STEPS = {"build", "codeQuality"}
            _CODE_PERF_STEPS = {"loadTest", "reportPerformanceTest"}

            if not actual_failed:
                # Pipeline passed — overall prediction correct only if we said Low
                r["correct"] = predicted_risk == "low"

                # Env signal correct if p_fail was low (< 0.5) OR env said GO/READY
                _ep = r.get("env_prediction") or {}
                r["correct_env"] = _ep.get("p_fail", 0.5) < 0.5 or _ep.get("status") == "READY"

                # Commit signal correct if p_fail was low (< 0.5)
                _cp = r.get("commit_prediction") or {}
                r["correct_commit"] = _cp.get("p_fail", 0.5) < 0.5

                r["evaluation_note"] = (
                    f"Pipeline FINISHED. "
                    f"{'Correct — predicted Low.' if r['correct'] else f'Incorrect — predicted {predicted_risk} but no failure.'} "
                    f"env_signal={'correct' if r['correct_env'] else 'over-predicted'} "
                    f"commit_signal={'correct' if r['correct_commit'] else 'over-predicted'}."
                )
            else:
                # Pipeline failed
                step_correct = (
                    predicted_step.lower() == actual_failed_step.lower()
                    if predicted_step and actual_failed_step else None  # None = unknown
                )
                risk_correct = predicted_risk in ("high", "medium")
                r["correct"] = risk_correct

                # Env signal correct if: failure was at an env step AND env p_fail >= 0.5
                _ep = r.get("env_prediction") or {}
                _actual_is_env = actual_failed_step in _ENV_STEPS if actual_failed_step else None
                if _actual_is_env is None:
                    r["correct_env"] = None  # can't evaluate without step info
                elif _actual_is_env:
                    r["correct_env"] = _ep.get("p_fail", 0) >= 0.5
                else:
                    # Failure was at a code step — env signal should not have triggered HOLD
                    r["correct_env"] = _ep.get("p_fail", 0) < 0.5

                # Commit signal correct if: failure was at a code step AND commit p_fail >= 0.5
                _cp = r.get("commit_prediction") or {}
                _actual_is_code = actual_failed_step in _CODE_STEPS if actual_failed_step else None
                _actual_is_code_perf = actual_failed_step in _CODE_PERF_STEPS if actual_failed_step else None
                if _actual_is_code is None:
                    r["correct_commit"] = None
                elif _actual_is_code or _actual_is_code_perf:
                    r["correct_commit"] = _cp.get("p_fail", 0) >= 0.5 or (
                        _actual_is_code_perf
                        and r.get("predicted_step") in _CODE_PERF_STEPS
                        and r.get("primary_driver") == "code"
                    )
                else:
                    # Failure was at env step — commit signal shouldn't dominate
                    r["correct_commit"] = None  # not applicable

                r["evaluation_note"] = (
                    f"Pipeline {actual_status}"
                    f"{' at ' + actual_failed_step if actual_failed_step else ' (step unknown)'}. "
                    f"Risk direction {'correct' if risk_correct else 'incorrect'}. "
                    f"Step {'correct' if step_correct else ('incorrect (predicted ' + predicted_step + ', actual ' + actual_failed_step + ')') if step_correct is False else 'unknown'}. "
                    f"env_correct={r['correct_env']} commit_correct={r['correct_commit']}."
                )

            updated = r
            break

    if updated:
        # Rewrite the correct tenant partition file
        if _owner_program:
            _rewrite_tenant(_owner_program, records)
        else:
            # Fallback: rewrite legacy file
            with open(STORE_PATH, "w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r) + "\n")

    return updated


# ── Auto-enrich from Splunk data ──────────────────────────────────────────────

def _get_branch_for_program(program_id: str) -> str:
    """
    Look up the deploy branch for a program_id from repo_config.json.
    repo_config.json now includes "program_id" on each customer block — direct lookup.
    Falls back to env-var mapping, then 'master'.
    """
    try:
        from connectors.submodule_connector import load_repo_config
        config = load_repo_config()
        for customer_cfg in config.values():
            # Direct match via program_id field (now present in repo_config.json)
            if str(customer_cfg.get("program_id", "")) == str(program_id):
                return customer_cfg.get("git_branch", "master")
        # Env-var fallback for new customers not yet in repo_config.json
        import os
        for key, val in {
            os.getenv("PROGRAM_ID_HDFC", "16360"):     "stage_and_prod",
            os.getenv("PROGRAM_ID_IDFC", "19905"):     "master",
            os.getenv("PROGRAM_ID_MALAYSIA", "465"):   "master",
        }.items():
            if str(program_id) == str(key):
                return val
    except Exception:
        pass
    return "master"


def backfill_execution_ids(pipeline_df, program_id: str = "") -> int:
    """
    For PENDING predictions that have a commit_sha but no execution_id,
    try to link them to an execution by correlating SHA against pipeline history.

    Uses per-customer branch from repo_config.json — critical for HDFC which
    deploys from stage_and_prod, not master. Using the wrong branch produces
    wrong SHA correlations and wrong training labels.

    program_id: if provided, only backfill predictions for that customer.

    Returns number of predictions updated with an execution_id.
    """
    import pandas as pd

    pending_no_eid = [p for p in load_pending(program_id) if not p.get("execution_id")]
    if not pending_no_eid or pipeline_df is None or pipeline_df.empty:
        return 0

    # Group pending predictions by program_id so we use the right branch per customer
    from collections import defaultdict
    by_program: Dict[str, list] = defaultdict(list)
    for p in pending_no_eid:
        by_program[p.get("program_id", "")].append(p)

    # Build SHA → executionId mapping per program (using correct branch)
    # Store per-program so we look up the right sha_map for each prediction.
    sha_to_eid: Dict[str, str] = {}
    sha_maps_by_program: Dict[str, dict] = {}
    try:
        from connectors.git_connector import correlate_executions_to_commits
        rows = pipeline_df.to_dict("records")
        for prog_id, preds in by_program.items():
            branch = _get_branch_for_program(prog_id)
            # Filter rows to this program_id if possible
            prog_rows = [r for r in rows if str(r.get("programId", "")) == str(prog_id)] or rows
            try:
                sha_map = correlate_executions_to_commits(prog_rows, branch=branch)
                sha_maps_by_program[prog_id] = sha_map
                for eid, info in sha_map.items():
                    if info.get("sha"):
                        sha_to_eid[info["sha"]] = eid
            except Exception:
                sha_maps_by_program[prog_id] = {}
    except Exception:
        return 0

    if not sha_to_eid:
        return 0

    # Update per-tenant — never write to legacy STORE_PATH
    # Group pending predictions by their program_id so we rewrite only the correct file
    from collections import defaultdict as _dd
    updated_by_program: Dict[str, list] = _dd(list)

    for pred in pending_no_eid:
        sha = pred.get("commit_sha", "")
        if not sha:
            continue
        eid = sha_to_eid.get(sha)
        if not eid:
            # Fall back to prefix match using the correct sha_map for this prediction's program
            pred_prog = pred.get("program_id", "")
            _sha_map = sha_maps_by_program.get(pred_prog, {})
            eid = next((e for e, v in _sha_map.items() if v.get("sha", "").startswith(sha[:12])), None)
        if not eid:
            continue
        updated_by_program[pred.get("program_id", "unknown")].append((pred["id"], eid))

    if not updated_by_program:
        return 0

    updated = 0
    for prog_id, id_eid_pairs in updated_by_program.items():
        tenant_records = load_all(prog_id)
        id_set = {pid: eid for pid, eid in id_eid_pairs}
        for r in tenant_records:
            if r["id"] in id_set:
                r["execution_id"] = id_set[r["id"]]
                updated += 1
        _rewrite_tenant(prog_id, tenant_records)

    return updated


def enrich_from_splunk(pipeline_df, failed_df, program_id: str = "") -> int:
    """
    Called after every Splunk data load.
    1. First backfills missing execution_ids via SHA correlation
    2. Then resolves PENDING predictions whose execution has completed
    Returns total number of predictions resolved.

    program_id: restrict to one customer's predictions — prevents IDFC Splunk
    data from resolving HDFC predictions.
    """
    import pandas as pd

    # Step 1: backfill execution_ids for predictions that are missing them
    backfill_execution_ids(pipeline_df, program_id=program_id)

    pending = load_pending(program_id)
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
    for pred in load_pending(program_id):  # reload after backfill, scoped to this customer
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
    records = load_resolved(program_id)

    if not records:
        return {"total": 0, "correct": 0, "accuracy": None, "pending": len(load_pending(program_id))}

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

    # Step accuracy — only count failures where Splunk actually recorded the failed step
    # Infrastructure failures (scaling, JDK) don't emit firstFailedStep events in Splunk
    # so we exclude them from step accuracy denominator (not a model failure)
    actual_failures = [r for r in records if r.get("actual_status") in ("FAILED", "ERROR")]
    failures_with_step = [r for r in actual_failures if r.get("actual_failed_step", "").strip()]
    step_correct = sum(
        1 for r in failures_with_step
        if r.get("predicted_step", "").lower() == r.get("actual_failed_step", "").lower()
    )

    # Infra failures — failed but no step recorded (scaling, JDK, network)
    infra_failures = len(actual_failures) - len(failures_with_step)

    # True false negatives — exclude infra failures (unpredictable from code)
    # An infra failure where we predicted Low is not a model error
    fn_code_caused = sum(
        1 for r in records
        if r.get("predicted_risk", "").lower() == "low"
        and r.get("actual_status") in ("FAILED", "ERROR")
        and r.get("actual_failed_step", "").strip()  # only count if we know the step
    )

    # Env signal accuracy — was the environment prediction correct?
    env_evaluable = [r for r in records if r.get("correct_env") is not None]
    env_correct   = sum(1 for r in env_evaluable if r.get("correct_env") is True)

    # Commit signal accuracy — was the code prediction correct?
    commit_evaluable = [r for r in records if r.get("correct_commit") is not None]
    commit_correct   = sum(1 for r in commit_evaluable if r.get("correct_commit") is True)

    # Primary driver breakdown — which signal drove the prediction?
    drivers = {}
    for r in records:
        d = r.get("primary_driver", "llm")
        drivers[d] = drivers.get(d, 0) + 1

    return {
        "total":                  total,
        "correct":                correct,
        "accuracy_pct":           round(correct / total * 100, 1) if total else None,
        "false_positives":        false_positives,
        "false_negatives":        false_negatives,
        "false_negatives_code":   fn_code_caused,
        "fp_rate_pct":            round(false_positives / total * 100, 1) if total else None,
        "fn_rate_pct":            round(false_negatives / total * 100, 1) if total else None,
        "infra_failures":         infra_failures,
        "step_accuracy_pct":      round(step_correct / len(failures_with_step) * 100, 1) if failures_with_step else None,
        "step_accuracy_denominator": len(failures_with_step),
        # Signal-level accuracy — tells you WHICH signal needs improvement
        "env_signal_accuracy_pct":    round(env_correct / len(env_evaluable) * 100, 1) if env_evaluable else None,
        "commit_signal_accuracy_pct": round(commit_correct / len(commit_evaluable) * 100, 1) if commit_evaluable else None,
        "env_evaluable":              len(env_evaluable),
        "commit_evaluable":           len(commit_evaluable),
        "primary_driver_breakdown":   drivers,  # how often env/code/history/llm drove the decision
        "pending":                len(load_pending(program_id)),
        "resolved":               total,
    }
