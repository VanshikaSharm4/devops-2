"""
Compressor — Layer 4 of the data processing pipeline.
Removes noise from data before it reaches the LLM.
~80% token reduction without losing diagnostic signal.

Rule: the LLM should only see key_lines from logs, never full log text.
"""
from __future__ import annotations

from typing import Any, Dict, List


# ── Feature 1: Failure report ─────────────────────────────────────────────────

def compress_error_details(error_details: List[Any], max_per_step: int = 2) -> List[dict]:
    """
    Group errors by step, keep max_per_step representative examples each.
    Strips full log text — only keeps key_lines (max 10 lines per entry).
    """
    by_step: Dict[str, List[Any]] = {}
    for ed in error_details:
        # ed can be ErrorDetail (Pydantic) or dict
        step = _get(ed, "failed_step") or "unknown"
        by_step.setdefault(step, []).append(ed)

    compressed = []
    for step, entries in by_step.items():
        for ed in entries[:max_per_step]:
            parsed = _get(ed, "parsed_error") or {}
            # If parsed_error is a LogParseResult / dict, extract key fields only
            compressed.append({
                "step": step,
                "execution_id": _get(ed, "execution_id"),
                "pipeline": _get(ed, "pipeline"),
                "error_type": _get(parsed, "error_type") or "unknown",
                "error_message": (_get(parsed, "error_message") or "")[:300],
                # key_lines — the only log content that reaches the LLM
                "key_lines": (_get(parsed, "key_lines") or [])[:10],
                # top 3 structured errors (no file content)
                "top_errors": [
                    {"type": _get(e, "type") or "", "detail": (_get(e, "detail") or "")[:200]}
                    for e in (_get(parsed, "errors") or [])[:3]
                ],
            })
    return compressed


def compress_failure_patterns(patterns: List[dict], top_n: int = 5) -> List[dict]:
    """Keep top N patterns by occurrence count, drop noise fields."""
    sorted_p = sorted(patterns, key=lambda x: x.get("count", 0), reverse=True)
    return [
        {
            "pipeline": p.get("pipelineName", ""),
            "step": p.get("firstFailedStep", ""),
            "status": p.get("Status", ""),
            "count": p.get("count", 0),
        }
        for p in sorted_p[:top_n]
    ]


def compress_stuck_executions(stuck: List[dict], top_n: int = 5) -> List[dict]:
    """Keep top N stuck executions by duration."""
    sorted_s = sorted(stuck, key=lambda x: x.get("Duration (Min)", 0), reverse=True)
    return [
        {
            "execution_id": s.get("executionId"),
            "pipeline": s.get("pipelineName"),
            "duration_min": s.get("Duration (Min)"),
            "started": s.get("Deploy Start Time"),
        }
        for s in sorted_s[:top_n]
    ]


# ── Feature 2: Risk analysis ──────────────────────────────────────────────────

def compress_bundle_for_risk(bundle_dict: dict) -> dict:
    """
    Strip the bundle down to only what a risk analysis needs.

    New approach (commit-aware, semantically-grounded):
    - Builds a full CommitProfile from git context
    - Classifies error_details into signal classes
    - Separates high-signal failures (weight >= 0.5) from infra noise
    - Sends CommitProfile + classified failures to the LLM
    - Sends historical_baseline as an operational prior, separated from
      commit-causal evidence

    Similar incidents are added later by _enrich_risk_with_memory.
    """
    from analysis.commit_analyzer import analyze_commit, infer_failure_modes, infer_change_intent
    from analysis.failure_classifier import classify_error_details, filter_high_signal_failures

    git_ctx = bundle_dict.get("git_context") or {}
    diff = git_ctx.get("diff_excerpt") or ""
    changed_files = (git_ctx.get("changed_files") or [])[:30]

    # Build CommitProfile
    commit_sha = git_ctx.get("commit_sha") or ""
    title = git_ctx.get("title") or ""
    inferred_failure_modes: list = []
    inferred_change_intent: str = "unknown"
    try:
        profile = analyze_commit(changed_files, diff, commit_sha=commit_sha, title=title)
        # Merge pre-computed aem_modules_touched if available
        if git_ctx.get("aem_modules_touched"):
            merged = sorted(
                set(profile.modules_touched) | set(git_ctx["aem_modules_touched"])
            )
            profile.modules_touched = merged
            profile.blast_radius = len(merged)
        inferred_failure_modes = infer_failure_modes(profile, diff)
        inferred_change_intent = infer_change_intent(title, profile)
        commit_profile_dict = {
            **profile.__dict__,
            "inferred_failure_modes": inferred_failure_modes,
            "change_intent": inferred_change_intent,
        }
    except Exception:
        # Fallback: minimal profile from git context
        commit_profile_dict = {
            "commit_sha": commit_sha,
            "title": title,
            "changed_files": changed_files,
            "modules_touched": git_ctx.get("aem_modules_touched") or [],
            "inferred_failure_modes": inferred_failure_modes,
            "change_intent": inferred_change_intent,
        }

    # ── Detect low-risk commit patterns and inject hard flags ────────────────
    # These override LLM reasoning for patterns where risk is structurally low
    import re as _re

    _title_lower = title.lower()
    _files_str   = " ".join(changed_files)

    # Git subtree import: "Add 'X/' from commit 'Y'" or files all under one new dir
    _is_subtree = (
        bool(_re.search(r"add ['\"]?\S+/['\"]? from commit", _title_lower))
        or bool(_re.search(r"git subtree (add|merge|push)", _title_lower))
        or bool(_re.search(r"merge commit ['\"]?[0-9a-f]{7,}", _title_lower))
        # Also detect by file pattern: all files under one new top-level dir
        or (len(changed_files) > 50 and len({f.split("/")[0] for f in changed_files}) <= 2)
    )

    # Jenkins/bot auto-commit
    _author_lower = (git_ctx.get("author") or "").lower()
    _is_bot_commit = any(b in _author_lower for b in
                         ["jenkins cicd", "jenkins", "automated", "bot@", "ci-bot"])

    # Zero-file commit
    _is_empty = len(changed_files) == 0

    # Inject flags into commit_profile so the LLM sees them explicitly
    if _is_subtree:
        commit_profile_dict["is_subtree_import"] = True
        commit_profile_dict["change_intent"]     = "subtree_import"
        commit_profile_dict["build_risk_override"] = (
            "LOW — git subtree import of externally tested code. "
            "Build risk is low by definition; code compiled in source repo. "
            "Focus only on OSGi integration and package filter conflicts."
        )
    if _is_bot_commit:
        commit_profile_dict["is_automated_commit"] = True
        commit_profile_dict["build_risk_override"] = (
            "VERY LOW — automated CI/CD commit (Jenkins or bot). "
            "No meaningful developer code change. Max confidence: 20%."
        )
    if _is_empty:
        commit_profile_dict["is_empty_commit"] = True
        commit_profile_dict["build_risk_override"] = (
            "NEAR ZERO — no files changed. No commit-caused build risk possible."
        )

    # Classify error_details
    error_details = bundle_dict.get("error_details") or []
    try:
        all_classified = classify_error_details(error_details)
        high_signal = filter_high_signal_failures(all_classified, min_weight=0.5)
        infra_noise = [c for c in all_classified if c["signal_weight"] < 0.5]

        def _slim(c: dict) -> dict:
            """Remove the 'original' Pydantic model from classified record."""
            return {
                "error_type": c["error_type"],
                "step": c["step"],
                "occurrence_count": c["occurrence_count"],
                "failure_class": c["failure_class"],
                "signal_weight": c["signal_weight"],
            }

        high_signal_slim = [_slim(c) for c in high_signal[:5]]
        infra_noise_slim = [_slim(c) for c in infra_noise[:3]]
    except Exception:
        high_signal_slim = []
        infra_noise_slim = []

    failure_history = bundle_dict.get("failure_history") or {}
    execution_summary = bundle_dict.get("execution_summary") or {}
    historical_baseline = {
        "total_executions": execution_summary.get("total_executions"),
        "success_rate_pct": execution_summary.get("success_rate_pct"),
        "failed_or_error": execution_summary.get("failed_or_error"),
        "cancelled": execution_summary.get("cancelled"),
        "failure_by_step": failure_history.get("by_step") or {},
        "failure_patterns": (failure_history.get("by_pipeline_step") or [])[:5],
        "known_root_causes": (failure_history.get("known_root_causes") or [])[:5],
    }

    return {
        "commit_profile": commit_profile_dict,
        "historical_baseline": historical_baseline,
        "high_signal_failures": high_signal_slim,   # max 5, weight >= 0.5
        "infra_noise_failures": infra_noise_slim,    # max 3, labelled as low-signal
        "rule_scores": bundle_dict.get("rule_scores", {}),  # verbatim — ground truth
        "diff_excerpt": diff[:2500] + ("..." if len(diff) > 2500 else ""),
        "inferred_failure_modes": inferred_failure_modes,
        "change_intent": inferred_change_intent,
        "avg_success_duration_min": failure_history.get("avg_success_duration_min"),
        # similar_incidents will be added by _enrich_risk_with_memory
    }


# ── Feature 3: Comparison ─────────────────────────────────────────────────────

def compress_comparison(exec_a: dict, exec_b: dict, git_diff: dict | None) -> dict:
    """
    Build the structural delta between two executions.
    Only keeps what changed, not full data for each.
    """
    diff_files = (git_diff or {}).get("changed_files", [])[:20]
    diff_excerpt = (git_diff or {}).get("diff_excerpt", "")[:500]

    def snap(e: dict) -> dict:
        parsed = e.get("parsed_error") or {}
        return {
            "id": e.get("id"),
            "status": e.get("status"),
            "pipeline": e.get("pipeline"),
            "duration_min": e.get("duration_min"),
            "failed_step": e.get("first_failed_step") or "",
            "error_type": _get(parsed, "error_type") or "none",
            "error_message": (_get(parsed, "error_message") or "")[:200],
            "key_lines": (_get(parsed, "key_lines") or [])[:5],
        }

    return {
        "execution_a": snap(exec_a),
        "execution_b": snap(exec_b),
        "duration_delta_min": exec_a.get("duration_min", 0) - exec_b.get("duration_min", 0),
        "same_step_failed": (
            exec_a.get("first_failed_step") == exec_b.get("first_failed_step")
        ),
        "changed_files": diff_files,
        "diff_excerpt": diff_excerpt,
    }


# ── Feature 4: Correlation ────────────────────────────────────────────────────

def compress_correlate_context(
    execution_id: str,
    failed_step: str,
    parse_result: Any,
    code_hits: List[dict],
    file_snippets: List[dict],
) -> dict:
    """
    Assemble the minimal context needed to correlate a log error to code.
    Strips everything the LLM doesn't need.
    """
    return {
        "execution_id": execution_id,
        "failed_step": failed_step,
        "error_type": _get(parse_result, "error_type") or "unknown",
        "error_message": (_get(parse_result, "error_message") or "")[:300],
        "key_lines": (_get(parse_result, "key_lines") or [])[:5],
        # top 3 code search hits
        "code_search_results": [
            {"path": h.get("path", ""), "url": h.get("url", "")}
            for h in code_hits[:3]
        ],
        # file snippets — max 50 lines each, max 3 files
        "file_snippets": [
            {
                "path": s.get("path", ""),
                "excerpt": (s.get("excerpt") or "")[:1500],
            }
            for s in file_snippets[:3]
        ],
    }


# ── Feature 5a: Scan ──────────────────────────────────────────────────────────

def compress_scan_findings(findings: List[dict], max_p1: int = 10, max_p2: int = 5) -> dict:
    """
    P1: keep all up to max_p1 (will break — always show)
    P2: keep top max_p2 by historical_occurrences
    P3: suppress from LLM (too noisy), count only
    """
    p1 = [f for f in findings if f.get("severity") == "P1"][:max_p1]
    p2 = sorted(
        [f for f in findings if f.get("severity") == "P2"],
        key=lambda x: x.get("historical_occurrences", 0),
        reverse=True,
    )[:max_p2]
    p3_count = len([f for f in findings if f.get("severity") == "P3"])

    def trim(f: dict) -> dict:
        return {
            "severity": f.get("severity"),
            "file": f.get("file"),
            "line_no": f.get("line_no"),
            "pattern": (f.get("pattern") or "")[:120],
            "reason": (f.get("reason") or "")[:250],
            "fix": (f.get("fix") or "")[:200],
            "historical_occurrences": f.get("historical_occurrences", 0),
        }

    return {
        "p1_findings": [trim(f) for f in p1],
        "p2_findings": [trim(f) for f in p2],
        "p3_suppressed_count": p3_count,
    }


# ── Feature 5b: Pinpoint ──────────────────────────────────────────────────────

def compress_pinpoint_context(
    execution_id: str,
    failed_step: str,
    parse_result: Any,
    code_findings: List[dict],
) -> dict:
    """Minimal context: error identity + top 3 code location candidates."""
    return {
        "execution_id": execution_id,
        "failed_step": failed_step,
        "error_type": _get(parse_result, "error_type") or "unknown",
        "error_message": (_get(parse_result, "error_message") or "")[:300],
        "key_lines": (_get(parse_result, "key_lines") or [])[:5],
        "code_locations": [
            {
                "file": f.get("file"),
                "line_no": f.get("line_no"),
                "line": (f.get("line") or "")[:200],
                "reason": (f.get("reason") or "")[:200],
                "severity": f.get("severity"),
            }
            for f in (code_findings or [])[:3]
        ],
    }


# ── Internal helper ───────────────────────────────────────────────────────────

def _get(obj: Any, key: str) -> Any:
    """Get attr from Pydantic model or dict key."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)
