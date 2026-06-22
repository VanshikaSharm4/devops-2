"""HyDE — Hypothetical Document Embeddings for retrieval."""
from __future__ import annotations

import json
from typing import Optional


def generate_hypothetical_document(
    error_summary: str,
    root_cause: str = "",
    *,
    use_llm: bool = True,
) -> str:
    """
    Generate a hypothetical ideal fix document to guide vector search.
    """
    if not use_llm:
        return f"Root cause: {root_cause or error_summary}\nRecommended fix for AEM Cloud Manager pipeline failure."

    try:
        from agent.devops_agent import _call_llm, LLMCallConfig
        system = (
            "Write a short hypothetical incident resolution document (3-5 sentences) "
            "describing the root cause and fix for this CI/CD failure. "
            "Return JSON: {\"hypothetical_doc\": \"...\"}"
        )
        user = json.dumps({"error_summary": error_summary, "root_cause": root_cause})
        cfg = LLMCallConfig(max_tokens=512, temperature=0.3, json_mode=True)
        raw = _call_llm(system, user, cfg)
        data = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
        return data.get("hypothetical_doc") or error_summary
    except Exception:
        return f"Root cause: {root_cause or error_summary}. Fix applied successfully."


def hyde_search(
    error_summary: str,
    root_cause: str = "",
    *,
    step: str = "",
    pipeline: str = "",
    top_k: int = 60,
    use_llm: bool = True,
) -> list:
    """HyDE route: generate hypothetical doc then dense-search Chroma."""
    hypo = generate_hypothetical_document(error_summary, root_cause, use_llm=use_llm)
    try:
        from vector_store.store import _collection, _is_risk_prediction_meta
        col = _collection("failure_memory")
        if col.count() == 0:
            return []
        where_filter = None
        if pipeline:
            where_filter = {
                "$or": [
                    {"pipeline": {"$eq": pipeline}},
                    {"pipeline": {"$eq": ""}},
                ]
            }
        results = col.query(
            query_texts=[hypo],
            n_results=min(top_k, col.count()),
            include=["metadatas", "distances"],
            **({"where": where_filter} if where_filter else {}),
        )
        hits = []
        for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
            if _is_risk_prediction_meta(meta or {}):
                continue
            sim = round(1 - dist, 3)
            if sim < 0.2:
                continue
            h = dict(meta)
            h["similarity_score"] = sim
            h["route"] = "hyde"
            hits.append(h)
        return hits[:top_k]
    except Exception:
        return []
