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
        # Check if the subtree includes risky files that can fail even if source compiled.
        # pom.xml: dependency conflicts with destination reactor.
        # dispatcher/conf.d: vhost/config syntax may differ per environment.
        # ui.config: OSGi configs may not activate in destination AEM version.
        _risky_subtree = any(
            any(r in f.lower() for r in ("pom.xml", "dispatcher", "conf.d", "ui.config",
                                          "filter.xml", "package-lock", "webpack"))
            for f in changed_files
        )
        if _risky_subtree:
            commit_profile_dict["build_risk_override"] = (
                "Git subtree import — code was compiled in the source repo. "
                "However this import includes integration-sensitive files "
                "(dispatcher config, pom.xml, OSGi/ui.config, or webpack). "
                "These can fail in the destination repo even if the source compiled: "
                "pom.xml may conflict with the destination reactor, dispatcher configs "
                "may have environment-specific syntax, OSGi configs may not activate "
                "under the destination AEM version. "
                "Use historical data and structural findings to determine actual build risk — "
                "do not assume LOW just because this is a subtree import."
            )
        else:
            commit_profile_dict["build_risk_override"] = (
                "Git subtree import of code with no integration-sensitive files. "
                "Code was compiled in the source repo. "
                "Build risk is likely low, but use historical data and structural findings "
                "to confirm — do not override evidence from ChromaDB or environment signals."
            )
    if _is_bot_commit:
        commit_profile_dict["is_automated_commit"] = True
        commit_profile_dict["build_risk_override"] = (
            "Automated CI/CD commit (Jenkins or bot) — no meaningful developer code change. "
            "Build risk is very likely low. Confidence should be low (under 25%)."
        )
    if _is_empty:
        commit_profile_dict["is_empty_commit"] = True
        commit_profile_dict["build_risk_override"] = (
            "No files changed in this commit — no commit-caused build risk is possible."
        )

    # Submodule pointer-only commit (parent has no app code)
    _is_submodule_pointer_only = (
        all(
            "pom.xml" in f.lower() or ".gitmodules" in f.lower() or
            not any(f.lower().endswith(ext) for ext in [".java", ".js", ".ts", ".jsx", ".tsx", ".css", ".scss"])
            for f in changed_files
        )
        and len(changed_files) > 0
        and not _is_subtree
    )

    # Reactor module enable/disable — NOT low risk.
    # When a module moves from commented-out to active in the reactor pom.xml,
    # CM now compiles its code including ui.frontend. This can fail with build errors
    # (npm/webpack issues, missing polyfills, Java errors) that were never caught
    # because the module wasn't being built before.
    # Detect by scanning the diff for lines that add/remove module references in pom.xml.
    _reactor_module_changed = False
    if _is_submodule_pointer_only and diff:
        import re as _re_reactor
        # Lines added (+) or removed (-) that reference <module> in pom.xml context
        _added_modules   = _re_reactor.findall(r'^\+\s*<module>([^<]+)</module>', diff, _re_reactor.MULTILINE)
        _removed_modules = _re_reactor.findall(r'^-\s*<module>([^<]+)</module>', diff, _re_reactor.MULTILINE)
        # Also catch commented-out → uncommented pattern:
        # -  <!--<module>foo</module>-->  →  +  <module>foo</module>
        _uncommented = _re_reactor.findall(r'^\+\s*<module>([^<]+)</module>', diff, _re_reactor.MULTILINE)
        _commented_out = _re_reactor.findall(r'^-\s*<!--\s*<module>', diff, _re_reactor.MULTILINE)
        if _added_modules or _removed_modules or _uncommented or _commented_out:
            _reactor_module_changed = True
            commit_profile_dict["reactor_modules_changed"] = {
                "enabled":  _added_modules[:10],
                "disabled": _removed_modules[:10],
            }

    if _is_submodule_pointer_only and _reactor_module_changed:
        # Reactor module enable/disable — override low-risk assumption.
        # Enabling a module means its code is now compiled: ui.frontend npm build,
        # Java compilation, surefire tests — any of which can fail.
        commit_profile_dict["is_reactor_module_change"] = True
        commit_profile_dict["build_risk_override"] = (
            "MEDIUM — reactor module list changed in pom.xml (module enabled or disabled). "
            "When a module is added to the reactor, CM compiles its ui.frontend (npm run build) "
            "and Java sources. This can fail with: webpack/polyfill errors (Node.js built-ins like "
            "'crypto' not available in webpack 5), missing npm dependencies, or Java compile errors "
            "that were never caught while the module was skipped. "
            "Do NOT use LOW override for reactor module changes — the parent does compile new code. "
            "Set build to MEDIUM and check the enabled module's ui.frontend for npm issues."
        )
    elif _is_submodule_pointer_only:
        commit_profile_dict["is_submodule_pointer_only"] = True
        # Check if structural_findings already found service/test issues in submodule scan
        _structural_findings = bundle_dict.get("structural_findings") or []
        _has_service_findings = any(
            sf.get("check", "") in (
                "service_without_test_update", "autowired_missing_test_mock",
                "injectmocks_without_mock", "core_java_without_test_update",
                "test_deleted_on_service_change",
                "submodule_diff_unavailable",      # bumped but diff not fetched
            )
            for sf in _structural_findings
        )
        _has_deleted_test_findings = any(
            sf.get("check", "") == "test_deleted_on_service_change"
            for sf in _structural_findings
        )
        _has_unfetched_submodule = any(
            sf.get("check", "") == "submodule_diff_unavailable"
            for sf in _structural_findings
        )
        _unfetched_names = [
            sf.get("title", "").split(" was bumped")[0]
            for sf in _structural_findings
            if sf.get("check", "") == "submodule_diff_unavailable"
        ]

        if _has_deleted_test_findings:
            # Tests were deleted alongside service changes — HIGH build risk.
            # Remaining tests can still fail against the changed service logic.
            # This is NOT "reduced coverage" — it is a surefire failure pattern.
            commit_profile_dict["build_risk_override"] = (
                "HIGH — submodule service code changed AND matching test assertions were DELETED. "
                "This is the surefire failure pattern: tests that remain will run against changed "
                "service logic and may fail with NPE, assertion error, or Mockito verification failure. "
                "Deleting tests does NOT prevent build failure — it removes tests that WOULD catch the regression. "
                "Set build to High. Do NOT treat deleted tests as 'reduced coverage only'."
            )
        elif _has_unfetched_submodule:
            # One or more submodules were bumped but their diffs could not be fetched.
            # Build risk is UNKNOWN — cannot verify whether tests pass or fail.
            # Do NOT default to LOW when you have no visibility into what changed.
            _names_str = ", ".join(_unfetched_names) if _unfetched_names else "one or more submodules"
            commit_profile_dict["build_risk_override"] = (
                f"MEDIUM — {_names_str} pointer was updated but the actual code diff could not be "
                f"fetched (submodule not in repo_config, credentials unavailable, or fetch failed). "
                f"Build risk is UNKNOWN: the submodule may contain service changes that break "
                f"surefire tests, but Argus cannot verify this without the diff. "
                f"Treat as MEDIUM — do not assume LOW just because the parent repo has no app code. "
                f"The real code lives in the submodule and is invisible here."
            )
        elif _has_service_findings:
            # Structural scan found service changes without test updates → build risk MEDIUM
            commit_profile_dict["build_risk_override"] = (
                "MEDIUM — parent bumps submodule pointer, but structural analysis found "
                "service implementations changed without test updates inside the submodule. "
                "The most common HDFC build failure mode: surefire tests fail at runtime "
                "(NPE, assertion failure, Mockito verification failure) because service logic "
                "changed but existing tests weren't updated. Set build to Medium, not Low."
            )
        else:
            commit_profile_dict["build_risk_override"] = (
                "LOW — parent repo only bumps submodule pointer SHAs and pom.xml. "
                "No app code compiled from parent. Build risk is LOW. "
                "Real risk is at DEPLOY (bundle ordering) not BUILD. "
                "Do NOT rate build step as High or Medium based on submodule content alone."
            )

    # Classify error_details
    error_details = bundle_dict.get("error_details") or []
    try:
        all_classified = classify_error_details(error_details)
        # Deduplicate correlated failures before scoring — prevents double-counting
        # when the same root cause (e.g. CRXDE active) appears at multiple steps.
        from analysis.failure_classifier import deduplicate_correlated_failures
        all_classified = deduplicate_correlated_failures(all_classified)
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

    # ── Environment state — always include, highest priority signal ──────────────
    env_readiness = bundle_dict.get("environment_readiness") or {}

    return {
        # SIGNAL PRIORITY ORDER — LLM should weight these top-down:
        # 1. Environment state (persistent infra issues — CRXDE/DavEx/scaling)
        # 2. Historical failure patterns from Splunk
        # 3. ChromaDB similar incidents (added later)
        # 4. Structural code analysis (advisory, not override)
        "environment_readiness": env_readiness,          # #1 — env state drives securityTest risk
        "pipeline_validation": {
            "java_upgrade_pending": bool(bundle_dict.get("java_upgrade_pending")),
        },
        "historical_baseline": historical_baseline,      # #2 — Splunk failure history per step
        "high_signal_failures": high_signal_slim,        # #2b — classified error signals
        "infra_noise_failures": infra_noise_slim,        # noise — label as infra, not code risk
        "commit_profile": commit_profile_dict,           # #4 — structural (advisory)
        "structural_findings": bundle_dict.get("structural_findings") or [],  # deterministic checks
        "rule_scores": bundle_dict.get("rule_scores", {}),
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
