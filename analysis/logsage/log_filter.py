"""LogSage Stage 1 — four parallel filtering strategies."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Set

from analysis.logsage.constants import K_ERROR, STEP_ERROR_PATTERNS
from analysis.logsage.drain_templates import DrainTemplateDB


@dataclass
class FilterResult:
    lines: List[str]
    candidate_indices: Set[int] = field(default_factory=set)
    stats: dict = field(default_factory=dict)


def _keyword_match(line: str, extra_patterns: Optional[List[str]] = None) -> bool:
    lower = line.lower()
    for kw in K_ERROR:
        if kw.lower() in lower:
            return True
    patterns = extra_patterns or []
    for pat in patterns:
        if re.search(pat, line, re.IGNORECASE):
            return True
    return False


def _tail_score(line_idx: int, total_lines: int) -> float:
    """Positional bias: lines nearer EOF score higher (0-1)."""
    if total_lines <= 1:
        return 1.0
    return line_idx / (total_lines - 1)


def filter_log_lines(
    log_text: str,
    drain_db: Optional[DrainTemplateDB] = None,
    failed_step: str = "",
    *,
    tail_bias_threshold: float = 0.85,
) -> FilterResult:
    """
    Apply four parallel filters and return candidate line indices (0-based).
    """
    raw_lines = log_text.splitlines()
    lines = [ln for ln in raw_lines if ln.strip()]
    n = len(lines)
    if n == 0:
        return FilterResult(lines=[], candidate_indices=set(), stats={"total_lines": 0})

    step_patterns = STEP_ERROR_PATTERNS.get(failed_step, [])
    candidates: Set[int] = set()
    seen_templates: Set[str] = set()

    keyword_hits = 0
    tail_hits = 0
    drain_dropped = 0
    dedup_dropped = 0

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Drain diff — skip noise lines entirely
        if drain_db and drain_db.is_noise_line(stripped):
            drain_dropped += 1
            continue

        matched = False

        # 1. Keyword matching
        if _keyword_match(stripped, step_patterns):
            matched = True
            keyword_hits += 1

        # 2. Log tail prioritization — last 15% of log
        if _tail_score(i, n) >= tail_bias_threshold:
            if _keyword_match(stripped, []) or re.search(
                r"error|fail|exception|fatal", stripped, re.IGNORECASE
            ):
                matched = True
                tail_hits += 1

        if not matched:
            continue

        # 4. Template deduplication
        tmpl_id = drain_db.template_id(stripped) if drain_db else stripped[:80]
        if tmpl_id in seen_templates:
            dedup_dropped += 1
            continue
        seen_templates.add(tmpl_id)
        candidates.add(i)

    # If filtering was too aggressive, add last 20 non-empty lines as fallback
    if len(candidates) <= 2:
        for i in range(max(0, n - 20), n):
            if drain_db and drain_db.is_noise_line(lines[i].strip()):
                continue
            candidates.add(i)

    return FilterResult(
        lines=lines,
        candidate_indices=candidates,
        stats={
            "total_lines": n,
            "keyword_hits": keyword_hits,
            "tail_hits": tail_hits,
            "drain_dropped": drain_dropped,
            "dedup_dropped": dedup_dropped,
            "candidate_count": len(candidates),
        },
    )
