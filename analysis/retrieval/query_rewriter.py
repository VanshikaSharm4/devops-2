"""LLM query rewrite route for hybrid retrieval."""
from __future__ import annotations

import json
from typing import List


def rewrite_queries(base_query: str, *, use_llm: bool = True) -> List[str]:
    """
    Return up to 2 rewritten query variants for sparse/dense search.
    Falls back to truncated base query when LLM unavailable.
    """
    if not use_llm or not base_query.strip():
        return [base_query[:2000]] if base_query else []

    try:
        from agent.devops_agent import _call_llm, LLMCallConfig
        system = (
            "Rewrite the CI/CD failure query into 2 alternative search queries "
            "that would find similar historical incidents. Return JSON: "
            '{"variants": ["query1", "query2"]}'
        )
        user = f"Original query:\n{base_query[:2500]}"
        cfg = LLMCallConfig(max_tokens=512, temperature=0.2, json_mode=True)
        raw = _call_llm(system, user, cfg)
        data = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
        variants = data.get("variants") or []
        return [v for v in variants if v][:2] or [base_query[:2000]]
    except Exception:
        return [base_query[:2000]]
