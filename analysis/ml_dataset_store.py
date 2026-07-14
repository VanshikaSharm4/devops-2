"""
ML Dataset Store — factual features + Splunk ground-truth labels.

Separate from prediction_store (prototype accuracy). One JSONL file per tenant.
Append-only; designed for long-running collection on a shared Eris deployment.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from analysis.ml_feature_extractor import extract_ml_features


def _get_ctx(attr: str, env_key: str, default: str = "") -> str:
    try:
        import analysis.customer_context as _cc
        val = getattr(_cc, f"get_{attr}")()
        return val if val else os.getenv(env_key, default)
    except Exception:
        return os.getenv(env_key, default)


def _store_dir() -> Path:
    from analysis.paths import ml_dataset_dir
    _env = os.getenv("ML_DATASET_DIR", "")
    return Path(_env) if _env else ml_dataset_dir()


def _store_path(program_id: str = "") -> Path:
    d = _store_dir()
    d.mkdir(parents=True, exist_ok=True)
    pid = str(program_id).strip() if program_id else "unknown"
    return d / f"{pid}.jsonl"


def _observation_id(commit_sha: str, execution_id: str, program_id: str) -> str:
    key = f"{program_id}:{execution_id}:{commit_sha[:12]}"
    return "obs_" + hashlib.sha256(key.encode()).hexdigest()[:12]


def _load_from_path(path: Path) -> List[Dict[str, Any]]:
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
    if program_id:
        return _load_from_path(_store_path(program_id))
    records = []
    d = _store_dir()
    if d.exists():
        for f in sorted(d.glob("*.jsonl")):
            records.extend(_load_from_path(f))
    return records


def load_pending(program_id: str = "") -> List[Dict[str, Any]]:
    return [r for r in load_all(program_id) if r.get("status") == "PENDING"]


def load_resolved(program_id: str = "") -> List[Dict[str, Any]]:
    return [r for r in load_all(program_id) if r.get("status") == "RESOLVED"]


def _rewrite_tenant(program_id: str, records: List[Dict[str, Any]]) -> None:
    path = _store_path(program_id)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def save_observation(
    features: Dict[str, Any],
    commit_sha: str,
    program_id: str,
    execution_id: str = "",
    tenant_id: str = "",
    pipeline_name: str = "",
) -> str:
    """
    Append a PENDING observation with factual features.
    Skips write if the same (program_id, execution_id, commit_sha) already exists.
    """
    path = _store_path(program_id)
    obs_id = _observation_id(commit_sha, execution_id, program_id)

    if path.exists():
        existing = {r.get("id", "") for r in _load_from_path(path)}
        if obs_id in existing:
            return obs_id

    record = {
        "id": obs_id,
        "status": "PENDING",
        "program_id": program_id,
        "tenant_id": tenant_id,
        "pipeline_name": pipeline_name,
        "execution_id": execution_id,
        "commit_sha": commit_sha,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        **features,
        "resolved_at": None,
        "actual_status": None,
        "actual_failed_step": None,
        "is_failure": None,
        "deploy_start_time": None,
    }

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    return obs_id


def record_risk_assessment_observation(
    commit_sha: str,
    program_id: str,
    tenant_id: str = "",
    execution_id: str = "",
    pipeline_name: str = "",
    bundle=None,
    report=None,
    pipeline_df=None,
    failed_df=None,
    diff_text: str = "",
    changed_files: Optional[List[str]] = None,
    repo_dir: str = "",
    as_of_date: str = "",
) -> Optional[str]:
    """
    Extract factual features from an already-completed risk assessment run
    and persist them. Safe to call in try/except — never raises.
    """
    try:
        git_ctx = getattr(bundle, "git_context", None) if bundle is not None else None
        bdict = bundle.__dict__ if bundle is not None else {}

        _diff = diff_text or (git_ctx.diff_excerpt if git_ctx else "") or ""
        _files = changed_files or (list(git_ctx.changed_files) if git_ctx else [])
        _title = git_ctx.title if git_ctx else ""
        _author = git_ctx.author if git_ctx else ""
        _date = as_of_date or (git_ctx.commit_date if git_ctx else "") or bdict.get("execution_date", "")

        build_pred = bdict.get("build_prediction")
        _repo = repo_dir or bdict.get("git_local_dir", "") or os.getenv("GIT_LOCAL_DIR", "")
        _sub = bdict.get("submodule_diffs")
        features = extract_ml_features(
            commit_sha=commit_sha,
            diff_text=_diff,
            changed_files=_files,
            commit_title=_title,
            commit_author=_author,
            commit_date=_date,
            repo_dir=_repo,
            pipeline_df=pipeline_df,
            failed_df=failed_df,
            pipeline_name=pipeline_name or "Production Pipeline",
            program_id=program_id,
            as_of_date=_date,
            build_prediction=build_pred,
            submodule_diffs=_sub,
            java_upgrade_pending=bool(bdict.get("java_upgrade_pending", False)),
        )

        return save_observation(
            features=features,
            commit_sha=commit_sha,
            program_id=program_id,
            execution_id=execution_id,
            tenant_id=tenant_id,
            pipeline_name=pipeline_name,
        )
    except Exception as exc:
        print(f"  [ml_dataset] observation skipped: {exc}")
        return None


def resolve_observation(
    obs_id: str,
    actual_status: str,
    actual_failed_step: str = "",
    deploy_start_time: str = "",
) -> Optional[Dict[str, Any]]:
    owner = ""
    d = _store_dir()
    if d.exists():
        for tf in d.glob("*.jsonl"):
            if any(r.get("id") == obs_id for r in _load_from_path(tf)):
                owner = tf.stem
                break

    if not owner:
        return None

    records = load_all(owner)
    updated = None
    for r in records:
        if r["id"] != obs_id:
            continue
        r["status"] = "RESOLVED"
        r["resolved_at"] = datetime.now(timezone.utc).isoformat()
        r["actual_status"] = actual_status
        r["actual_failed_step"] = actual_failed_step or ""
        r["is_failure"] = actual_status in ("FAILED", "ERROR")
        if deploy_start_time:
            r["deploy_start_time"] = deploy_start_time
        updated = r
        break

    if updated:
        _rewrite_tenant(owner, records)
    return updated


def _get_branch_for_program(program_id: str) -> str:
    try:
        from connectors.submodule_connector import load_repo_config
        config = load_repo_config()
        for customer_cfg in config.values():
            if str(customer_cfg.get("program_id", "")) == str(program_id):
                return customer_cfg.get("git_branch", "master")
    except Exception:
        pass
    return "master"


def backfill_execution_ids(pipeline_df, program_id: str = "") -> int:
    """Link PENDING observations missing execution_id via SHA correlation."""
    pending = [p for p in load_pending(program_id) if not p.get("execution_id")]
    if not pending or pipeline_df is None or getattr(pipeline_df, "empty", True):
        return 0

    from collections import defaultdict

    by_program: Dict[str, list] = defaultdict(list)
    for p in pending:
        by_program[p.get("program_id", "")].append(p)

    sha_maps_by_program: Dict[str, dict] = {}
    try:
        from connectors.git_connector import correlate_executions_to_commits
        rows = pipeline_df.to_dict("records")
        for prog_id in by_program:
            branch = _get_branch_for_program(prog_id)
            prog_rows = [r for r in rows if str(r.get("programId", "")) == str(prog_id)] or rows
            try:
                sha_maps_by_program[prog_id] = correlate_executions_to_commits(prog_rows, branch=branch)
            except Exception:
                sha_maps_by_program[prog_id] = {}
    except Exception:
        return 0

    updated_by_program: Dict[str, List[tuple]] = defaultdict(list)
    for pred in pending:
        sha = pred.get("commit_sha", "")
        prog = pred.get("program_id", "")
        sha_map = sha_maps_by_program.get(prog, {})
        eid = next(
            (e for e, v in sha_map.items() if v.get("sha", "") == sha or v.get("sha", "").startswith(sha[:12])),
            None,
        )
        if eid:
            updated_by_program[prog].append((pred["id"], eid))

    updated = 0
    for prog_id, pairs in updated_by_program.items():
        id_map = dict(pairs)
        recs = load_all(prog_id)
        for r in recs:
            if r["id"] in id_map:
                r["execution_id"] = id_map[r["id"]]
                updated += 1
        _rewrite_tenant(prog_id, recs)
    return updated


def enrich_from_splunk(pipeline_df, failed_df, program_id: str = "") -> int:
    """
    Backfill execution IDs and resolve PENDING observations when Splunk shows
    a terminal pipeline status.
    """
    backfill_execution_ids(pipeline_df, program_id=program_id)

    exec_status: Dict[str, str] = {}
    exec_step: Dict[str, str] = {}
    exec_start: Dict[str, str] = {}

    if pipeline_df is not None and not getattr(pipeline_df, "empty", True):
        for _, row in pipeline_df.iterrows():
            eid = str(row.get("executionId", ""))
            st = str(row.get("Status", ""))
            if eid and st not in ("RUNNING", ""):
                exec_status[eid] = st
            if eid:
                exec_start[eid] = str(row.get("Deploy Start Time", "") or "")

    if failed_df is not None and not getattr(failed_df, "empty", True):
        if "firstFailedStep" in failed_df.columns:
            for _, row in failed_df.iterrows():
                eid = str(row.get("executionId", ""))
                step = str(row.get("firstFailedStep", "") or "")
                if eid and step and step != "nan":
                    exec_step[eid] = step

    resolved_count = 0
    for obs in load_pending(program_id):
        eid = obs.get("execution_id", "")
        if not eid or eid not in exec_status:
            continue
        status = exec_status[eid]
        if status in ("FINISHED", "FAILED", "ERROR"):
            resolve_observation(
                obs["id"],
                status,
                exec_step.get(eid, ""),
                exec_start.get(eid, ""),
            )
            resolved_count += 1
    return resolved_count


def dataset_stats(program_id: str = "") -> Dict[str, Any]:
    all_recs = load_all(program_id)
    resolved = [r for r in all_recs if r.get("status") == "RESOLVED"]
    pending = [r for r in all_recs if r.get("status") == "PENDING"]
    failures = [r for r in resolved if r.get("is_failure")]
    return {
        "total": len(all_recs),
        "pending": len(pending),
        "resolved": len(resolved),
        "failure_count": len(failures),
        "failure_rate_pct": round(len(failures) / len(resolved) * 100, 1) if resolved else None,
    }
