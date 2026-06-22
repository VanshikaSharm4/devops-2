"""BGE reranker with timeout/retry and offline fallback."""
from __future__ import annotations

import os
import signal
from contextlib import contextmanager
from typing import List, Optional


BGE_MODEL = os.getenv("BGE_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
BGE_TIMEOUT = int(os.getenv("BGE_RERANKER_TIMEOUT_SEC", "30"))
BGE_RETRIES = int(os.getenv("BGE_RERANKER_RETRIES", "2"))


class _Timeout(Exception):
    pass


@contextmanager
def _time_limit(seconds: int):
    def handler(signum, frame):
        raise _Timeout()

    if hasattr(signal, "SIGALRM"):
        old = signal.signal(signal.SIGALRM, handler)
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
    else:
        yield


def _candidate_text(c: dict) -> str:
    parts = [
        c.get("error_type", ""),
        c.get("error_message", ""),
        c.get("root_cause", ""),
        c.get("fix", ""),
        c.get("document", ""),
        c.get("pattern", ""),
        c.get("problem", ""),
    ]
    return " ".join(p for p in parts if p)[:1500]


def rerank_candidates(
    query: str,
    candidates: List[dict],
    *,
    top_k: int = 100,
    use_reranker: bool = True,
) -> List[dict]:
    """
    Rerank candidates with BGE. Falls back to route-level scores when disabled.
    """
    if not candidates:
        return []

    if not use_reranker:
        return _fallback_rank(candidates, top_k)

    for attempt in range(BGE_RETRIES + 1):
        try:
            with _time_limit(BGE_TIMEOUT):
                return _bge_rerank(query, candidates, top_k)
        except Exception:
            if attempt >= BGE_RETRIES:
                break
    return _fallback_rank(candidates, top_k)


def _fallback_rank(candidates: List[dict], top_k: int) -> List[dict]:
    def score(c: dict) -> float:
        return float(
            c.get("rerank_score")
            or c.get("similarity_score")
            or c.get("bm25_score")
            or c.get("match_score")
            or 0
        )

    ranked = sorted(candidates, key=score, reverse=True)
    for i, c in enumerate(ranked[:top_k]):
        c["rerank_score"] = score(c)
        c["rerank_rank"] = i + 1
    return ranked[:top_k]


def _bge_rerank(query: str, candidates: List[dict], top_k: int) -> List[dict]:
    try:
        from FlagEmbedding import FlagReranker
    except ImportError:
        return _fallback_rank(candidates, top_k)

    reranker = FlagReranker(BGE_MODEL, use_fp16=False)
    pairs = [[query[:2000], _candidate_text(c)] for c in candidates]
    scores = reranker.compute_score(pairs, normalize=True)
    if not isinstance(scores, list):
        scores = [scores]

    for c, s in zip(candidates, scores):
        c["rerank_score"] = float(s)

    ranked = sorted(candidates, key=lambda x: x.get("rerank_score", 0), reverse=True)
    for i, c in enumerate(ranked[:top_k]):
        c["rerank_rank"] = i + 1
    return ranked[:top_k]
