"""LogSage token overflow pruning — weight assignment and greedy block packing."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Set

from analysis.logsage.constants import (
    BETA,
    FAILURE_PATTERN_WEIGHT,
    FAILURE_PATTERNS,
    GAMMA,
    HEADER_BOOST,
    RECALL_BOOST,
    TOKEN_LIMIT,
)
from analysis.logsage.context_window import LogBlock


def _get_encoder():
    try:
        import tiktoken
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def count_tokens(text: str) -> int:
    enc = _get_encoder()
    if enc:
        return len(enc.encode(text))
    # Fallback: ~4 chars per token
    return max(1, len(text) // 4)


def count_block_tokens(block: LogBlock) -> int:
    return count_tokens("\n".join(block.lines))


@dataclass
class PruneResult:
    selected_blocks: List[LogBlock] = field(default_factory=list)
    filtered_text_blocks: List[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _matches_failure_pattern(line: str) -> bool:
    import re
    for pat in FAILURE_PATTERNS:
        if re.search(pat, line, re.IGNORECASE):
            return True
    return False


def assign_weights(
    lines: List[str],
    candidate_indices: Set[int],
    blocks: List[LogBlock],
    *,
    beta: int = BETA,
) -> List[float]:
    """Assign per-line weight vector W."""
    n = len(lines)
    weights = [0.0] * n
    pool_size = len(candidate_indices)

    for j in candidate_indices:
        if pool_size <= beta:
            weights[j] = 3.0
        else:
            weights[j] = 1.0

    # Pattern-based enhancement
    for j in range(n):
        line = lines[j]
        if _matches_failure_pattern(line):
            weights[j] = float(FAILURE_PATTERN_WEIGHT)
        elif line.strip().startswith("#"):
            weights[j] += HEADER_BOOST
        elif j in candidate_indices and weights[j] < 2:
            weights[j] += RECALL_BOOST

    # Contextual expansion: lines in blocks inherit max neighbor weight >= theta
    theta = _compute_theta(weights, gamma=GAMMA)
    for block in blocks:
        block_max = max(weights[block.start : block.end + 1], default=0)
        if block_max >= theta:
            for j in range(block.start, block.end + 1):
                if weights[j] < 1:
                    weights[j] = max(weights[j], 1.0)

    return weights


def _compute_theta(weights: List[float], *, gamma: int = GAMMA) -> float:
    active = sum(1 for w in weights if w >= 1)
    max_w = max(weights) if weights else 0
    if max_w == 1 or active <= gamma:
        return 1.0
    return 3.0


def block_density(block: LogBlock, weights: List[float]) -> float:
    total = sum(weights[block.start : block.end + 1])
    length = block.end - block.start + 1
    return total / length if length else 0.0


def prune_blocks(
    blocks: List[LogBlock],
    weights: List[float],
    *,
    token_limit: int = TOKEN_LIMIT,
) -> PruneResult:
    """Greedy density-ranked block selection within token budget."""
    if not blocks:
        return PruneResult(stats={"tokens_before": 0, "tokens_after": 0, "blocks_selected": 0})

    scored = sorted(
        blocks,
        key=lambda b: block_density(b, weights),
        reverse=True,
    )

    selected: List[LogBlock] = []
    total_tokens = 0
    tokens_before = sum(count_block_tokens(b) for b in blocks)

    for block in scored:
        block_tokens = count_block_tokens(block)
        if total_tokens + block_tokens > token_limit:
            continue
        selected.append(block)
        total_tokens += block_tokens

    # If nothing selected, take highest-density block anyway
    if not selected and scored:
        selected = [scored[0]]
        total_tokens = count_block_tokens(scored[0])

    # Sort selected blocks by start line for readable output
    selected.sort(key=lambda b: b.start)
    text_blocks = ["\n".join(b.lines) for b in selected]

    return PruneResult(
        selected_blocks=selected,
        filtered_text_blocks=text_blocks,
        stats={
            "tokens_before": tokens_before,
            "tokens_after": total_tokens,
            "token_limit": token_limit,
            "blocks_total": len(blocks),
            "blocks_selected": len(selected),
        },
    )


def run_token_pruning(
    lines: List[str],
    candidate_indices: Set[int],
    blocks: List[LogBlock],
    *,
    token_limit: Optional[int] = None,
) -> PruneResult:
    weights = assign_weights(lines, candidate_indices, blocks)
    return prune_blocks(blocks, weights, token_limit=token_limit or TOKEN_LIMIT)
