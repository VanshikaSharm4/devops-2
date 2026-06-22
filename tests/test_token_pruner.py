"""Unit tests for LogSage token pruner."""
from analysis.logsage.context_window import LogBlock, expand_context_windows
from analysis.logsage.token_pruner import (
    assign_weights,
    block_density,
    prune_blocks,
    count_tokens,
)


def test_failure_pattern_gets_max_weight():
    lines = [
        "INFO ok",
        "FAIL: test runtime failure",
        "some context",
    ]
    candidates = {1, 2}
    blocks = expand_context_windows(lines, candidates, m=1, n=1)
    weights = assign_weights(lines, candidates, blocks)
    assert weights[1] == 10.0


def test_greedy_pruning_respects_limit():
    long_line = "x" * 400
    lines = [long_line] * 200
    candidates = set(range(200))
    blocks = expand_context_windows(lines, candidates, m=0, n=0)
    weights = assign_weights(lines, candidates, blocks)
    result = prune_blocks(blocks, weights, token_limit=500)
    assert result.stats["tokens_after"] <= 500 or result.stats["blocks_selected"] == 1


def test_count_tokens_positive():
    assert count_tokens("hello world") >= 1
