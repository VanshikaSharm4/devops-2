"""Asymmetric context window expansion and block merging."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Set, Tuple

from analysis.logsage.constants import CONTEXT_AFTER, CONTEXT_BEFORE


@dataclass
class LogBlock:
    start: int   # inclusive line index
    end: int     # inclusive line index
    lines: List[str]

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def expand_context_windows(
    lines: List[str],
    candidate_indices: Set[int],
    *,
    m: int = CONTEXT_BEFORE,
    n: int = CONTEXT_AFTER,
) -> List[LogBlock]:
    """Extract [i-m, i+n] around each candidate and merge overlaps."""
    if not lines or not candidate_indices:
        return []

    ranges: List[Tuple[int, int]] = []
    max_idx = len(lines) - 1
    for i in sorted(candidate_indices):
        start = max(0, i - m)
        end = min(max_idx, i + n)
        ranges.append((start, end))

    ranges.sort()
    merged: List[Tuple[int, int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    blocks: List[LogBlock] = []
    for start, end in merged:
        blocks.append(
            LogBlock(
                start=start,
                end=end,
                lines=lines[start : end + 1],
            )
        )
    return blocks
