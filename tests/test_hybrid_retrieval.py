"""Unit tests for hybrid retrieval (offline paths)."""
from analysis.retrieval.hybrid_retriever import normalize_query, _dedup_key, _route_relational
from analysis.retrieval.reranker import rerank_candidates


def test_normalize_query_dedup():
    rca = {
        "error_summary": "npm build failed missing package",
        "root_cause": "npm build failed missing package",
        "error_type": "missing_npm_module",
    }
    q = normalize_query(rca, ["[ERROR] Module not found"])
    assert "npm" in q.lower()


def test_dedup_key():
    assert _dedup_key({"execution_id": "e1", "error_type": "build"}) == "e1:build"


def test_relational_route():
    history = {
        "known_root_causes": [
            {"step": "build", "error_type": "missing_npm_module", "description": "bad dep", "fix": "add pkg"},
        ]
    }
    hits = _route_relational(history, "build", "missing_npm_module")
    assert len(hits) == 1
    assert hits[0]["route"] == "relational"


def test_reranker_fallback():
    candidates = [
        {"execution_id": "a", "error_type": "x", "similarity_score": 0.9},
        {"execution_id": "b", "error_type": "y", "similarity_score": 0.5},
    ]
    ranked = rerank_candidates("build failure", candidates, use_reranker=False)
    assert ranked[0]["execution_id"] == "a"
