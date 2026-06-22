"""Lazy BM25 sparse index over Chroma failure_memory + scan_memory documents."""
from __future__ import annotations

import os
import pickle
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BM25_INDEX_PATH = os.getenv("BM25_INDEX_PATH", "data/cache/bm25_index.pkl")
BM25_TTL_MIN = int(os.getenv("BM25_INDEX_TTL_MIN", "60"))


def _tokenize(text: str) -> List[str]:
    import re
    return re.findall(r"[a-zA-Z0-9_./\-:]+", text.lower())


class BM25Index:
    def __init__(self, documents: List[str], metadatas: List[dict]):
        self.documents = documents
        self.metadatas = metadatas
        self._bm25 = None
        self._build()

    def _build(self) -> None:
        try:
            from rank_bm25 import BM25Okapi
            tokenized = [_tokenize(d) for d in self.documents]
            self._bm25 = BM25Okapi(tokenized) if tokenized else None
        except ImportError:
            self._bm25 = None

    def search(self, query: str, top_k: int = 60) -> List[dict]:
        if not self._bm25 or not self.documents:
            return []
        scores = self._bm25.get_scores(_tokenize(query))
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        hits = []
        for idx, score in ranked[:top_k]:
            if score <= 0:
                continue
            meta = dict(self.metadatas[idx])
            meta["bm25_score"] = float(score)
            meta["route"] = "bm25"
            meta["document"] = self.documents[idx][:500]
            hits.append(meta)
        return hits


def _load_chroma_docs() -> Tuple[List[str], List[dict]]:
    """Read all documents from failure_memory and scan_memory collections."""
    docs: List[str] = []
    metas: List[dict] = []
    try:
        from vector_store.store import _collection, _is_risk_prediction_meta
        for coll_name in ("failure_memory", "scan_memory"):
            col = _collection(coll_name)
            if col.count() == 0:
                continue
            data = col.get(include=["documents", "metadatas"])
            for doc, meta in zip(data.get("documents") or [], data.get("metadatas") or []):
                if coll_name == "failure_memory" and _is_risk_prediction_meta(meta or {}):
                    continue
                docs.append(doc or "")
                m = dict(meta or {})
                m["collection"] = coll_name
                metas.append(m)
    except Exception:
        pass
    return docs, metas


def _cache_fresh() -> bool:
    path = Path(BM25_INDEX_PATH)
    if not path.exists():
        return False
    age = (time.time() - path.stat().st_mtime) / 60
    return age < BM25_TTL_MIN


def build_bm25_index(force: bool = False) -> Optional[BM25Index]:
    """Lazy-build BM25 index from Chroma collections."""
    path = Path(BM25_INDEX_PATH)
    if not force and _cache_fresh():
        try:
            with open(path, "rb") as f:
                data = pickle.load(f)
            return BM25Index(data["documents"], data["metadatas"])
        except Exception:
            pass

    docs, metas = _load_chroma_docs()
    if not docs:
        return None

    index = BM25Index(docs, metas)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump({"documents": docs, "metadatas": metas}, f)
    return index


def bm25_search(query: str, top_k: int = 60) -> List[dict]:
    index = build_bm25_index()
    if not index:
        return []
    return index.search(query, top_k=top_k)
