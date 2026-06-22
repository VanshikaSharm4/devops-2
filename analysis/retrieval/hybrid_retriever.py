"""Eight-route hybrid retrieval orchestrator."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from analysis.logsage.constants import QUERY_TOKEN_LIMIT
from analysis.logsage.token_pruner import count_tokens
from analysis.retrieval.bm25_index import bm25_search
from analysis.retrieval.hyde import hyde_search
from analysis.retrieval.query_rewriter import rewrite_queries
from analysis.retrieval.reranker import rerank_candidates


def _jaccard_tokens(a: str, b: str) -> float:
    ta = set(re.findall(r"\w+", a.lower()))
    tb = set(re.findall(r"\w+", b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def normalize_query(
    rca: dict,
    filtered_blocks: List[str],
    *,
    overlap_threshold: float = 0.8,
    max_tokens: int = QUERY_TOKEN_LIMIT,
) -> str:
    """Build deduplicated search query capped at max_tokens."""
    parts = [
        rca.get("error_summary", ""),
        rca.get("root_cause", ""),
        rca.get("error_type", ""),
        "\n".join(filtered_blocks[:3]),
    ]
    unique: List[str] = []
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        if any(_jaccard_tokens(p, u) >= overlap_threshold for u in unique):
            continue
        unique.append(p)

    query = "\n".join(unique)
    enc_tokens = count_tokens(query)
    if enc_tokens > max_tokens:
        # Truncate by chars proportional to token limit
        ratio = max_tokens / max(enc_tokens, 1)
        query = query[: int(len(query) * ratio)]
    return query


def _dedup_key(c: dict) -> str:
    eid = str(c.get("execution_id", ""))
    etype = str(c.get("error_type", c.get("pattern", "")))
    return f"{eid}:{etype}"


def _merge_candidates(pool: List[dict], seen: Set[str], limit: int = 60) -> List[dict]:
    out = []
    for c in pool:
        key = _dedup_key(c)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= limit:
            break
    return out


def _route_relational(
    failure_history: dict,
    step: str,
    error_type: str,
    limit: int = 60,
) -> List[dict]:
    causes = failure_history.get("known_root_causes") or []
    hits = []
    for c in causes:
        if step and c.get("step") and c.get("step") != step:
            continue
        et = c.get("error_type", "")
        if error_type and et and error_type not in et and et not in error_type:
            continue
        hits.append({
            "execution_id": f"history:{c.get('step', '')}",
            "step": c.get("step", step),
            "error_type": et or error_type,
            "root_cause": c.get("description", c.get("root_cause", "")),
            "fix": c.get("fix", ""),
            "route": "relational",
            "match_score": 0.6,
        })
        if len(hits) >= limit:
            break
    return hits


def _route_document_patterns(
    failure_history: dict,
    pipeline: str,
    step: str,
    limit: int = 60,
) -> List[dict]:
    patterns = failure_history.get("by_pipeline_step") or []
    hits = []
    for p in patterns:
        if pipeline and p.get("pipelineName") and pipeline not in str(p.get("pipelineName", "")):
            continue
        if step and p.get("firstFailedStep") and p.get("firstFailedStep") != step:
            continue
        hits.append({
            "execution_id": f"pattern:{pipeline}:{step}",
            "step": p.get("firstFailedStep", step),
            "error_type": "recurring_pattern",
            "root_cause": f"Recurring failure at {p.get('firstFailedStep')} ({p.get('count', 0)}x)",
            "fix": "",
            "route": "document_pattern",
            "match_score": min(1.0, (p.get("count", 1) or 1) / 10),
        })
        if len(hits) >= limit:
            break
    return hits


def _route_module_scoped(
    changed_files: List[str],
    modules: List[str],
    step: str,
    pipeline: str,
    limit: int = 60,
) -> List[dict]:
    hits = []
    try:
        from vector_store.store import find_similar_failures
        for mod in modules[:5]:
            mod_hits = find_similar_failures(
                error_type=f"module:{mod}",
                error_message=" ".join(changed_files[:5]),
                key_lines=changed_files[:5],
                step=step,
                top_k=10,
                pipeline=pipeline,
            )
            for h in mod_hits:
                h["route"] = "module_scoped"
                hits.append(h)
            if len(hits) >= limit:
                break
    except Exception:
        pass
    return hits[:limit]


def hybrid_retrieve(
    query: str,
    *,
    rca: Optional[dict] = None,
    step: str = "",
    pipeline: str = "",
    error_type: str = "",
    failure_history: Optional[dict] = None,
    changed_files: Optional[List[str]] = None,
    modules: Optional[List[str]] = None,
    use_llm_routes: bool = True,
    use_reranker: bool = True,
    routes_per_query: int = 60,
) -> List[dict]:
    """
    Run 8 retrieval routes, merge up to 480 candidates, BGE rerank to top 100.
    """
    rca = rca or {}
    failure_history = failure_history or {}
    seen: Set[str] = set()
    all_candidates: List[dict] = []

    # Route 1: BM25
    all_candidates.extend(_merge_candidates(bm25_search(query, top_k=routes_per_query), seen, routes_per_query))

    # Route 2: Dense KNN
    try:
        from vector_store.store import find_similar_failures
        dense = find_similar_failures(
            error_type=error_type or rca.get("error_type", ""),
            error_message=rca.get("error_summary", query)[:500],
            key_lines=(rca.get("error_line_refs") or [])[:5],
            step=step,
            top_k=routes_per_query,
            pipeline=pipeline,
        )
        for h in dense:
            h["route"] = "dense_knn"
        all_candidates.extend(_merge_candidates(dense, seen, routes_per_query))
    except Exception:
        pass

    # Route 3: Relational
    all_candidates.extend(
        _merge_candidates(
            _route_relational(failure_history, step, error_type or rca.get("error_type", "")),
            seen,
            routes_per_query,
        )
    )

    # Route 4: Document patterns
    all_candidates.extend(
        _merge_candidates(
            _route_document_patterns(failure_history, pipeline, step),
            seen,
            routes_per_query,
        )
    )

    # Route 5: Query rewrite → BM25 + dense
    for variant in rewrite_queries(query, use_llm=use_llm_routes):
        all_candidates.extend(_merge_candidates(bm25_search(variant, top_k=20), seen, 20))
        try:
            from vector_store.store import find_similar_failures
            rw = find_similar_failures(
                error_type=error_type,
                error_message=variant[:500],
                key_lines=[],
                step=step,
                top_k=20,
                pipeline=pipeline,
            )
            for h in rw:
                h["route"] = "query_rewrite"
            all_candidates.extend(_merge_candidates(rw, seen, 20))
        except Exception:
            pass

    # Route 6: HyDE
    all_candidates.extend(
        _merge_candidates(
            hyde_search(
                rca.get("error_summary", query),
                rca.get("root_cause", ""),
                step=step,
                pipeline=pipeline,
                top_k=routes_per_query,
                use_llm=use_llm_routes,
            ),
            seen,
            routes_per_query,
        )
    )

    # Route 7: Module-scoped
    if modules or changed_files:
        all_candidates.extend(
            _merge_candidates(
                _route_module_scoped(
                    changed_files or [],
                    modules or [],
                    step,
                    pipeline,
                    routes_per_query,
                ),
                seen,
                routes_per_query,
            )
        )

    # Route 8: Scan memory
    try:
        from vector_store.store import find_similar_scan_findings
        pattern = error_type or rca.get("error_type", "failure")
        scan_hits = find_similar_scan_findings(pattern, top_k=routes_per_query)
        for h in scan_hits:
            h["route"] = "scan_memory"
            h.setdefault("error_type", "scan_finding")
            h.setdefault("root_cause", h.get("problem", ""))
            h.setdefault("fix", h.get("fix", ""))
        all_candidates.extend(_merge_candidates(scan_hits, seen, routes_per_query))
    except Exception:
        pass

    return rerank_candidates(query, all_candidates, top_k=100, use_reranker=use_reranker)
