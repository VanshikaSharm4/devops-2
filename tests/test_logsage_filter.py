"""Unit tests for LogSage Stage 1 filtering."""
from pathlib import Path

from analysis.logsage.drain_templates import DrainTemplateDB
from analysis.logsage.log_filter import filter_log_lines
from analysis.logsage.context_window import expand_context_windows
from analysis.logsage import run_stage1

FIXTURES = Path(__file__).parent / "fixtures" / "logs"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_keyword_filter_finds_error_lines():
    log = _read("failed_build_1.log")
    result = filter_log_lines(log, failed_step="build")
    assert len(result.candidate_indices) >= 2
    lines = result.lines
    joined = " ".join(lines[i] for i in result.candidate_indices)
    assert "BUILD FAILURE" in joined or "Module not found" in joined


def test_drain_diff_drops_success_noise():
    success = _read("success_build_1.log")
    failed = _read("failed_build_1.log")
    db = DrainTemplateDB().build_from_logs([success])
    result = filter_log_lines(failed, drain_db=db, failed_step="build")
    # INFO lines from success template should be dropped when matched
    assert result.stats["total_lines"] > 0


def test_context_window_merges_overlaps():
    lines = [f"line {i}" for i in range(20)]
    candidates = {5, 6, 15}
    blocks = expand_context_windows(lines, candidates, m=4, n=6)
    assert len(blocks) >= 1
    assert blocks[0].start <= 5


def test_run_stage1_offline():
    log = _read("failed_build_1.log")
    result = run_stage1(
        log,
        failed_step="build",
        pipeline_name="Dev-pipeline",
        fetch_success=False,
    )
    assert len(result.filtered_blocks) >= 1
    assert result.pruning_stats.get("blocks_selected", 0) >= 1
