"""
Vector Store — failure memory layer.

Stores every past failure + its fix as an embedding.
On new failures, retrieves the top-k most similar past incidents
so the LLM can say "this happened before, here's what fixed it."

Collections:
  failure_memory  — one doc per ErrorDetail / PinpointReport
  scan_memory     — one doc per ScanFinding (proactive patterns)
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

def _chroma_dir() -> str:
    _env = os.getenv("CHROMA_DIR", "")
    if _env:
        return _env
    from analysis.paths import chroma_dir
    return str(chroma_dir())


def _client():
    import chromadb
    return chromadb.PersistentClient(path=_chroma_dir())


def _collection(name: str):
    return _client().get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
    )


def _doc_id(text: str) -> str:
    """Stable ID from content hash."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _is_risk_prediction_meta(meta: dict) -> bool:
    """True for AI-generated risk predictions, not observed pipeline failures."""
    return (
        meta.get("pipeline") == "risk_analysis"
        or str(meta.get("error_type", "")).startswith("risk_prediction_")
        or str(meta.get("execution_id", "")).startswith("risk-")
    )


# ── Store a failure + fix ─────────────────────────────────────────────────────

def store_failure(
    execution_id: str,
    step: str,
    error_type: str,
    error_message: str,
    key_lines: list[str],
    root_cause: str,
    fix: str,
    pipeline: str = "",
    extra_meta: Optional[dict] = None,
) -> str:
    """
    Embed and store one failure record.
    The text that gets embedded combines error signal + root cause + fix
    so similarity search finds failures that are both similar AND have known fixes.
    Returns the doc_id.
    """
    # Build the text to embed — rich signal for similarity
    embed_text = (
        f"step:{step} error_type:{error_type}\n"
        f"error: {error_message}\n"
        f"log lines: {' | '.join(key_lines[:5])}\n"
        f"root cause: {root_cause}\n"
        f"fix: {fix}"
    )

    doc_id = _doc_id(f"{execution_id}:{step}:{error_type}")

    metadata = {
        "execution_id":  execution_id,
        "step":          step,
        "error_type":    error_type,
        "error_message": error_message[:500],
        "root_cause":    root_cause[:500],
        "fix":           fix[:500],
        "pipeline":      pipeline,
        **(extra_meta or {}),
    }

    # Enforce tenant tag — records without a verified tenant cannot be stored.
    # This prevents the "untagged record leaks to all customers" bug.
    _tid = metadata.get("tenant_id", "") or metadata.get("program_id", "")
    if not _tid or _tid in ("unverified", "unknown"):
        import warnings
        warnings.warn(
            f"store_failure called without tenant_id/program_id for execution {execution_id}. "
            f"Record NOT stored — all ChromaDB records must be tenant-tagged.",
            stacklevel=2,
        )
        return doc_id  # skip storage, return doc_id for caller compatibility

    col = _collection("failure_memory")
    col.upsert(
        ids=[doc_id],
        documents=[embed_text],
        metadatas=[metadata],
    )
    return doc_id


def store_scan_finding(
    file: str,
    line_no: Optional[int],
    pattern: str,
    problem: str,
    fix: str,
    severity: str,
    repo: str = "",
) -> str:
    """Store one proactive scan finding for future similarity lookup."""
    embed_text = (
        f"severity:{severity} file:{file}\n"
        f"pattern: {pattern}\n"
        f"problem: {problem}\n"
        f"fix: {fix}"
    )
    doc_id = _doc_id(f"{file}:{line_no}:{pattern}")
    metadata = {
        "file":     file,
        "line_no":  str(line_no or ""),
        "pattern":  pattern[:300],
        "problem":  problem[:300],
        "fix":      fix[:300],
        "severity": severity,
        "repo":     repo,
    }
    col = _collection("scan_memory")
    col.upsert(ids=[doc_id], documents=[embed_text], metadatas=[metadata])
    return doc_id


# ── Retrieve similar past failures ───────────────────────────────────────────

def find_similar_failures(
    error_type: str,
    error_message: str,
    key_lines: list[str],
    step: str = "",
    top_k: int = 3,
    include_risk_predictions: bool = False,
    pipeline: str = "",
    tenant_id: str = "",
    program_id: str = "",
) -> list[dict]:
    """
    Query the failure memory for the most similar past incidents.

    Tenant isolation (P0 for Adobe enterprise):
    - If tenant_id or program_id provided, only records from that tenant are
      returned. IDFC failures must never influence HDFC predictions.
    - Pipeline filter is applied within the tenant scope.
    - Records with no tenant tag are included for backward compat (legacy data).

    Returns list of dicts with keys: execution_id, step, error_type,
    error_message, root_cause, fix, similarity_score.
    """
    query = (
        f"step:{step} error_type:{error_type}\n"
        f"error: {error_message}\n"
        f"log lines: {' | '.join(key_lines[:5])}"
    )
    col = _collection("failure_memory")
    if col.count() == 0:
        return []

    n_results = min(max(top_k * 5, top_k), col.count())

    # Build ChromaDB where filter — tenant isolation is the outermost constraint
    conditions = []

    # Tenant isolation — strict: only this tenant's records.
    # 'unverified' records have unknown origin and must NEVER be served to any tenant.
    # Empty/untagged records are also blocked when a tenant is specified.
    _tenant = tenant_id or program_id
    if _tenant:
        # Only return records explicitly tagged for this tenant
        conditions.append({
            "$or": [
                {"tenant_id":  {"$eq": _tenant}},
                {"program_id": {"$eq": _tenant}},
            ]
        })
    else:
        # No tenant specified (admin/global) — still exclude unverified records
        conditions.append({
            "$and": [
                {"tenant_id": {"$ne": "unverified"}},
                {"program_id": {"$ne": "unverified"}},
            ]
        })

    # Pipeline isolation within tenant
    if pipeline:
        conditions.append({
            "$or": [
                {"pipeline": {"$eq": pipeline}},
                {"pipeline": {"$eq": ""}},
            ]
        })

    where_filter = None
    if len(conditions) == 1:
        where_filter = conditions[0]
    elif len(conditions) > 1:
        where_filter = {"$and": conditions}

    results = col.query(
        query_texts=[query],
        n_results=n_results,
        include=["metadatas", "distances"],
        **({"where": where_filter} if where_filter else {}),
    )

    hits = []
    seen_fingerprints: list = []

    def _rc_fingerprint(text: str) -> str:
        """Short fingerprint of root cause to detect near-duplicate records."""
        import re as _re_fp
        words = _re_fp.findall(r'\b[a-z]{5,}\b', (text or "").lower())
        top = sorted(set(words), key=words.count, reverse=True)[:5]
        return " ".join(sorted(top))

    for meta, dist in zip(
        results["metadatas"][0],
        results["distances"][0],
    ):
        if not include_risk_predictions and _is_risk_prediction_meta(meta):
            continue
        similarity = round(1 - dist, 3)
        if similarity < 0.3:
            continue

        # Deduplicate near-identical root causes at query time
        # This prevents 5 records all saying "dispatcher address not specified"
        fp = _rc_fingerprint(meta.get("root_cause", "") + " " + meta.get("step", ""))
        is_dup = any(
            sum(1 for w in fp.split() if w in seen_fp.split()) / max(len(fp.split()), 1) > 0.6
            for seen_fp in seen_fingerprints
        )
        if is_dup:
            continue
        seen_fingerprints.append(fp)

        hits.append({**meta, "similarity_score": similarity})
        if len(hits) >= top_k:
            break
    return hits


def find_similar_scan_findings(pattern: str, file: str = "", top_k: int = 3) -> list[dict]:
    """Query scan memory for similar past findings."""
    col = _collection("scan_memory")
    if col.count() == 0:
        return []
    results = col.query(
        query_texts=[f"pattern: {pattern} file: {file}"],
        n_results=min(top_k, col.count()),
        include=["metadatas", "distances"],
    )
    hits = []
    for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
        similarity = round(1 - dist, 3)
        if similarity < 0.3:
            continue
        hits.append({**meta, "similarity_score": similarity})
    return hits


def purge_risk_prediction_records() -> int:
    """
    Delete AI-generated risk predictions from failure_memory.

    These are useful as saved reports, but they should not live in the same
    vector collection as observed pipeline failures because they can reinforce
    earlier guesses as "historical matches".
    """
    col = _collection("failure_memory")
    if col.count() == 0:
        return 0

    data = col.get(include=["metadatas"])
    ids = data.get("ids") or []
    metas = data.get("metadatas") or []
    delete_ids = [
        doc_id
        for doc_id, meta in zip(ids, metas)
        if _is_risk_prediction_meta(meta or {})
    ]
    if delete_ids:
        col.delete(ids=delete_ids)
    return len(delete_ids)


# ── Bulk ingest from existing reports ────────────────────────────────────────

def ingest_failure_report(report: Any, pipeline_name: str = "", tenant_id: str = "", program_id: str = "") -> int:
    """
    Ingest a FailureReport (Pydantic model) into the vector store.
    Stores every critical + recurring finding.

    pipeline_name: the actual pipeline name (e.g. "Production Pipeline",
    "Dev-pipeline"). Stored as metadata so future queries can filter to the
    same pipeline. If omitted, the record is untagged and will appear in all
    pipeline queries (backward-compatible with legacy data).

    Returns number of records stored.
    """
    _pid = program_id or str(getattr(report, "program_id", ""))
    _tid = tenant_id or _pid
    count = 0
    findings = list(getattr(report, "critical_findings", []) or []) + \
               list(getattr(report, "recurring_findings", []) or [])
    for i, f in enumerate(findings):
        store_failure(
            execution_id=f"report:{getattr(report, 'program_id', 'unknown')}:{i}",
            step=getattr(f, "step", ""),
            error_type=getattr(f, "error_type", ""),
            error_message="",
            key_lines=[],
            root_cause=getattr(f, "root_cause", ""),
            fix=getattr(f, "recommended_fix", ""),
            pipeline=pipeline_name,  # actual pipeline name, not program_id
            extra_meta={"tenant_id": _tid, "program_id": _pid} if _pid or _tid else None,
        )
        count += 1
    return count


def ingest_pinpoint_report(report: Any, tenant_id: str = "", program_id: str = "") -> str:
    """Ingest a PinpointReport into the vector store."""
    _pid = program_id or str(getattr(report, "program_id", ""))
    _tid = tenant_id or _pid
    return store_failure(
        execution_id=getattr(report, "execution_id", "unknown"),
        step=getattr(report, "failed_step", ""),
        error_type=getattr(report, "error_type", ""),
        error_message="",
        key_lines=[],
        root_cause=getattr(report, "explanation", ""),
        fix=f"{getattr(report, 'fix_after', '') or getattr(report, 'prevention', '')}",
        extra_meta={"tenant_id": _tid, "program_id": _pid} if _pid or _tid else None,
    )


def ingest_risk_report(report: Any, context: Optional[dict] = None) -> int:
    """
    Ingest a RiskReport prediction into failure_memory so future risk analyses
    can retrieve it as a relevant past example.

    One record is stored per step_risk entry that has a historical_failure_count > 0
    or a level of High / Medium.  This lets the similarity search surface concrete
    step-level evidence rather than one opaque risk-report blob.
    """
    context = context or {}
    commit_sha: str = (
        getattr(report, "commit_sha", None)
        or context.get("commit_sha", "")
        or "risk-prediction"
    )
    commit_profile = context.get("commit_profile") or {}
    changed_files: list = (
        context.get("changed_files")
        or commit_profile.get("changed_files")
        or []
    )
    file_signal = " | ".join(changed_files[:6])

    count = 0
    for sr in getattr(report, "step_risks", []) or []:
        level = getattr(sr, "level", "")
        hist  = getattr(sr, "historical_failure_count", 0) or 0
        step  = getattr(sr, "step", "")
        if level not in ("High", "Medium") and hist == 0:
            continue   # Low / no history — not worth storing

        store_failure(
            execution_id=f"risk-{commit_sha[:8]}-{step}",
            step=step,
            error_type=f"risk_prediction_{step}",
            error_message=file_signal,
            key_lines=changed_files[:5],
            root_cause=getattr(sr, "rationale", "")[:400],
            fix="; ".join(getattr(report, "recommended_actions", [])[:3])[:400],
            pipeline="risk_analysis",
        )
        count += 1
    return count


def ingest_scan_report(report: Any, repo: str = "") -> int:
    """Ingest all findings from a ScanReport into the scan memory."""
    count = 0
    for f in getattr(report, "findings", []) or []:
        store_scan_finding(
            file=getattr(f, "file", ""),
            line_no=getattr(f, "line_no", None),
            pattern=getattr(f, "pattern", ""),
            problem=getattr(f, "problem_explanation", ""),
            fix=getattr(f, "fix_code_example", ""),
            severity=getattr(f, "severity", ""),
            repo=repo,
        )
        count += 1
    return count


# ── Auto-ingest from live Splunk + Azure ──────────────────────────────────────

def ingest_live_failures(
    failed_df: Any,
    share_map: dict,
    pipeline_name: str = "",
    max_per_run: int = 20,
    tenant_id: str = "",
    program_id: str = "",
) -> int:
    """
    For every failure in failed_df that has an Azure share and hasn't been
    ingested yet, fetch the real log, parse the actual error, and store it.

    Azure shares are retained for extended periods — ingest while executions are fresh.
    Skips executions already in the store (idempotent).
    Returns number of NEW records stored.
    """
    import pandas as pd
    from connectors.azure_connector import get_log_for_execution
    from parsers.log_parser import parse_log

    if failed_df is None or (hasattr(failed_df, "empty") and failed_df.empty):
        return 0

    col = _collection("failure_memory")
    # Get already-ingested execution IDs to avoid duplicates
    try:
        existing = set(col.get(include=[])["ids"])
    except Exception:
        existing = set()

    stored = 0
    for _, row in failed_df.iterrows():
        if stored >= max_per_run:
            break

        eid   = str(row.get("executionId", ""))
        step  = str(row.get("firstFailedStep", "") or "")
        pname = str(row.get("pipelineName", "") or pipeline_name)

        if not eid or not step or step == "nan":
            continue

        doc_id = _doc_id(f"{eid}:{step}:live")
        if doc_id in existing:
            continue  # already ingested

        share = share_map.get(eid)
        if not share:
            continue

        # Fetch the real log — skip if share has expired
        try:
            log_text = get_log_for_execution(share, step)
            if not log_text or "ShareNotFound" in log_text or "Could not fetch" in log_text:
                continue
        except Exception:
            continue

        # Parse actual error from log
        try:
            result = parse_log(step, log_text)
            error_type    = result.error_type if hasattr(result, "error_type") else "unknown"
            error_message = result.error_message if hasattr(result, "error_message") else ""
            key_lines     = list(result.key_lines) if hasattr(result, "key_lines") else []
        except Exception:
            error_type, error_message, key_lines = "unknown", "", []

        if not error_message and not key_lines:
            continue  # empty log — not useful

        # Build rich embed text from actual log content
        embed_text = (
            f"pipeline:{pname} step:{step} error_type:{error_type}\n"
            f"error: {error_message}\n"
            f"log lines: {' | '.join(key_lines[:8])}"
        )

        _meta: dict = {
            "execution_id":  eid,
            "step":          step,
            "error_type":    error_type,
            "error_message": error_message[:500],
            "root_cause":    f"{pname} {step} failed: {error_message[:200]}",
            "fix":           f"Check {step} logs for execution {eid}",
            "pipeline":      pname,
            "source":        "live_log",
        }
        if tenant_id or program_id:
            _meta["tenant_id"]  = tenant_id or program_id
            _meta["program_id"] = program_id or tenant_id
        col.upsert(
            ids=[doc_id],
            documents=[embed_text],
            metadatas=[_meta],
        )
        stored += 1

    return stored


# ── Post-failure assessment ingest (additive) ─────────────────────────────────

def ingest_post_failure_report(report: Any) -> str:
    """Ingest a PostFailureRiskReport into failure_memory for future RAG."""
    fix_text = "; ".join(getattr(report, "fix_steps", []) or [])[:400]
    actions = "; ".join(getattr(report, "recommended_actions", []) or [])[:200]
    return store_failure(
        execution_id=getattr(report, "execution_id", "unknown"),
        step=getattr(report, "failed_step", ""),
        error_type=getattr(report, "error_type", "post_failure"),
        error_message=getattr(report, "root_cause_summary", "")[:500],
        key_lines=(getattr(report, "filtered_log_blocks", []) or [])[:5],
        root_cause=getattr(report, "root_cause_summary", "")[:500],
        fix=fix_text or actions,
        pipeline=getattr(report, "pipeline", ""),
        extra_meta={"source": "post_failure_assessment"},
    )


# ── Stats ─────────────────────────────────────────────────────────────────────

def memory_stats() -> dict:
    """Return counts for the dashboard."""
    try:
        client = _client()
        cols = {c.name: c.count() for c in client.list_collections()}
        return {
            "failure_memory": cols.get("failure_memory", 0),
            "scan_memory":    cols.get("scan_memory", 0),
            "db_path":        CHROMA_DIR,
        }
    except Exception as e:
        return {"failure_memory": 0, "scan_memory": 0, "error": str(e)}
