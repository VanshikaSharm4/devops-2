"""Drain3-based success log template database for noise filtering."""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import List, Optional, Set

from analysis.logsage.constants import DRAIN_CACHE_DIR

try:
    from drain3 import TemplateMiner
    from drain3.template_miner_config import TemplateMinerConfig

    _DRAIN_AVAILABLE = True
except ImportError:
    _DRAIN_AVAILABLE = False


def _miner() -> "TemplateMiner":
    config = TemplateMinerConfig()
    config.drain_depth = 4
    config.drain_max_children = 100
    config.drain_sim_th = 0.4
    return TemplateMiner(config=config)


def _normalize_line(line: str) -> str:
    """Mask variable tokens for template matching."""
    s = line.strip()
    if not s:
        return ""
    s = re.sub(r"\b\d+\b", "<NUM>", s)
    s = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        "<UUID>",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"0x[0-9a-f]+", "<HEX>", s, flags=re.IGNORECASE)
    return s


class DrainTemplateDB:
    """Template database built from recent successful pipeline logs."""

    def __init__(self, templates: Optional[Set[str]] = None, template_ids: Optional[dict] = None):
        self.templates: Set[str] = templates or set()
        self.template_ids: dict[str, str] = template_ids or {}

    def build_from_logs(self, log_texts: List[str]) -> "DrainTemplateDB":
        if not _DRAIN_AVAILABLE or not log_texts:
            return self

        miner = _miner()
        for log_text in log_texts:
            for line in log_text.splitlines():
                stripped = line.strip()
                if not stripped or len(stripped) < 8:
                    continue
                result = miner.add_log_message(stripped)
                if result and "template_mined" in (result.get("change_type") or ""):
                    tmpl = result.get("template_mined") or ""
                    if tmpl:
                        norm = _normalize_line(tmpl)
                        self.templates.add(norm)
                        self.template_ids[norm] = hashlib.md5(norm.encode()).hexdigest()[:12]
                cluster_id = result.get("cluster_id") if result else None
                if cluster_id is not None:
                    cluster = miner.drain.id_to_cluster.get(cluster_id)
                    if cluster:
                        norm = _normalize_line(cluster.get_template())
                        if norm:
                            self.templates.add(norm)
                            self.template_ids[norm] = str(cluster_id)

        return self

    def is_noise_line(self, line: str) -> bool:
        """True if line matches an established success template."""
        if not self.templates:
            return False
        norm = _normalize_line(line)
        if not norm:
            return False
        if norm in self.templates:
            return True
        # Fuzzy: substring match on normalized template
        for tmpl in self.templates:
            if len(tmpl) > 20 and tmpl in norm:
                return True
            if len(norm) > 20 and norm in tmpl:
                return True
        return False

    def template_id(self, line: str) -> str:
        norm = _normalize_line(line)
        return self.template_ids.get(norm, hashlib.md5(norm.encode()).hexdigest()[:12])

    def to_dict(self) -> dict:
        return {
            "templates": list(self.templates),
            "template_ids": self.template_ids,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DrainTemplateDB":
        return cls(
            templates=set(data.get("templates") or []),
            template_ids=data.get("template_ids") or {},
        )


def cache_path(pipeline: str, step: str) -> Path:
    safe = re.sub(r"[^\w\-]", "_", f"{pipeline}_{step}")
    return Path(DRAIN_CACHE_DIR) / f"{safe}.json"


def load_cached_templates(pipeline: str, step: str, ttl_min: int = 30) -> Optional[DrainTemplateDB]:
    path = cache_path(pipeline, step)
    if not path.exists():
        return None
    age_min = (time.time() - path.stat().st_mtime) / 60
    if age_min > ttl_min:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return DrainTemplateDB.from_dict(data)
    except Exception:
        return None


def save_cached_templates(pipeline: str, step: str, db: DrainTemplateDB) -> None:
    path = cache_path(pipeline, step)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(db.to_dict(), indent=2), encoding="utf-8")
