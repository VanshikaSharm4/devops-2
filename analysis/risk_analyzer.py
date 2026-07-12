"""Feature 2: Pre-deployment risk analysis."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Tuple

from analysis.aem_modules import get_changed_modules
from analysis.ingest import build_base_bundle
from analysis.risk_rules import compute_rule_scores
from connectors.git_connector import get_commit_diff
from models.bundle import AnalysisBundle, GitContext
from models.risk_report import RiskReport


def _risk_level(level: str) -> str:
    return {
        "LOW": "Low",
        "MEDIUM": "Medium",
        "HIGH": "High",
        "CRITICAL": "Critical",
    }.get((level or "").upper(), "Low")


def attach_git_context(
    bundle: AnalysisBundle,
    commit_sha: Optional[str] = None,
    repo: Optional[str] = None,
    pr_number: Optional[int] = None,  # kept for CLI compat, ignored (no GitHub PRs)
) -> AnalysisBundle:
    """
    Attach git context from Cloud Manager Git (git.cloudmanager.adobe.com).
    Use --commit SHA — PRs are not supported (no GitHub API).
    """
    bundle.repo = os.getenv("CM_GIT_REPO_URL", "")

    if not commit_sha:
        # No SHA — run without git context (pipeline history features only)
        bundle.git_context = GitContext(commit_sha="", changed_files=[], aem_modules_touched=[])
        bundle.rule_scores = compute_rule_scores(bundle)
        return bundle

    repo_dir = (
        repo
        or bundle.__dict__.get("git_local_dir")
        or os.getenv("GIT_LOCAL_DIR", "")
        or None
    )

    try:
        data = get_commit_diff(repo_dir, commit_sha)
    except Exception:
        data = {}
    bundle.git_context = GitContext(
        commit_sha=commit_sha,
        title=data.get("title", ""),
        body=data.get("body", ""),
        author=data.get("author", ""),
        commit_date=data.get("commit_date", ""),
        changed_files=data.get("changed_files", []),
        aem_modules_touched=get_changed_modules(data.get("changed_files", [])),
        diff_excerpt=data.get("diff_excerpt", ""),
    )

    bundle.rule_scores = compute_rule_scores(bundle)
    return bundle


def _resolve_submodule_customer_name(bundle, program_id: str = "") -> str:
    """Map bundle / program_id to repo_config.json customer key for submodule fetch."""
    import os as _os
    from connectors.submodule_connector import get_customer_config, load_repo_config

    candidates = [
        bundle.__dict__.get("customer_name", ""),
        _os.getenv("CUSTOMER_NAME", ""),
    ]
    for name in candidates:
        if name and get_customer_config(name):
            return name

    pid = str(
        program_id
        or bundle.__dict__.get("program_id", "")
        or _os.getenv("PROGRAM_ID", "")
    )
    if pid:
        for cfg_name, cfg in load_repo_config().items():
            if str(cfg.get("program_id", "")) == pid:
                return cfg_name
        try:
            from analysis.paths import customer_config_path as _ccp
            _env_cfg = _os.getenv("CUSTOMER_CONFIG_PATH", "")
            _cfg_path = Path(_env_cfg) if _env_cfg else _ccp()
            if _cfg_path.exists():
                import json as _json
                for cfg_name, cfg in _json.loads(_cfg_path.read_text()).items():
                    if str(cfg.get("program_id", "")) == pid:
                        return cfg_name
        except Exception:
            pass
    return candidates[0] if candidates else ""


def run_pre_deploy_risk(
    pr_number: Optional[int] = None,
    commit_sha: Optional[str] = None,
    fetch_logs: bool = True,
    use_llm: bool = True,
    bundle: Optional[AnalysisBundle] = None,
    as_of_date: Optional[str] = None,
    pipeline_name: Optional[str] = None,
) -> Tuple[AnalysisBundle, Optional[RiskReport], str]:
    """
    Run full pre-deploy risk pipeline.
    Pass bundle from the dashboard to avoid rebuilding it on every click.
    Returns (bundle, structured_report, markdown).
    """
    if bundle is None:
        bundle, _, _, _ = build_base_bundle(fetch_logs=fetch_logs)
    bundle = attach_git_context(bundle, pr_number=pr_number, commit_sha=commit_sha)

    # Inject caller-supplied as_of_date and pipeline_name so downstream
    # env assessment uses the right historical window (e.g. for batch evaluation
    # of historical SHAs we don't want to use today's env state).
    if as_of_date:
        bundle.__dict__["execution_date"] = as_of_date
    if pipeline_name:
        bundle.__dict__["pipeline_name"] = pipeline_name

    # ── Load Splunk data ONCE — reused by all signals below ───────────────────
    # Previously load_data() was called 3-4 times independently, each potentially
    # triggering a full Splunk fetch when the disk cache was stale.
    _program_id = os.getenv("PROGRAM_ID", "")
    try:
        from analysis.ingest import load_data as _load_once
        _pdf_main, _fdf_main, _fsfdf_main, _share_main = _load_once()
    except Exception:
        _pdf_main = _fdf_main = _fsfdf_main = None
        _share_main = {}
    if commit_sha and _program_id and use_llm and not os.getenv("ARGUS_DISABLE_CACHE"):
        try:
            from analysis.assessment_cache import get_cached
            # load_data already called once above as _pdf_main, _fdf_main
            from models.risk_report import RiskReport as _RR
            _pdf_c, _fdf_c = _pdf_main, _fdf_main
            # Get current consecutive failures to validate cache freshness
            from analysis.env_readiness import assess_environment_readiness as _aer, DEFAULT_PROD_PIPELINE
            _env_c = _aer(_pdf_c, _fdf_c, pipeline_name=DEFAULT_PROD_PIPELINE)
            _consec = _env_c.get("consecutive_failures", -1) if _env_c else -1
            from analysis.assessment_cache import get_cached_llm_fields
            _cached_llm = get_cached_llm_fields(_program_id, commit_sha, _consec)
            if _cached_llm:
                # Restore LLM fields into a RiskReport shell
                _report = _RR(**{k: v for k, v in _cached_llm.items()
                                 if k in _RR.model_fields})
                # Always rerun scorer — scorer fields are NEVER cached
                # This ensures risk_level/confidence/step reflect current env + scorer version
                bundle.__dict__["_from_llm_cache"] = True
                # Fall through to scorer block below (don't return early)
                # Store cached report so scorer can enrich it
                bundle.__dict__["_cached_report"] = _report
        except Exception:
            pass  # cache miss or error — proceed normally

    if not use_llm:
        from models.risk_report import RiskReport, StepRisk

        rs = bundle.rule_scores
        step_levels = {
            "build": rs.build,
            "securityTest": rs.securityTest,
            "deploy": rs.deploy,
        }
        level_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
        strongest_step = max(
            step_levels,
            key=lambda step: (
                level_order.get(step_levels.get(step, "LOW"), 0),
                bundle.failure_history.by_step.get(step, 0),
            ),
        )
        strongest_level = step_levels.get(strongest_step, "LOW")
        confidence_by_level = {
            "LOW": 30,
            "MEDIUM": 55,
            "HIGH": 75,
            "CRITICAL": 90,
        }
        report = RiskReport(
            risk_level=_risk_level(strongest_level),
            confidence_score=confidence_by_level.get(strongest_level, 30),
            commit_sha=commit_sha,
            modules_at_risk=bundle.git_context.aem_modules_touched if bundle.git_context else [],
            most_likely_failure_step=strongest_step,
            step_risks=[
                StepRisk(
                    step="build",
                    level=_risk_level(rs.build),
                    historical_failure_count=bundle.failure_history.by_step.get("build", 0),
                    rationale="; ".join(rs.reasons[:3]),
                ),
                StepRisk(
                    step="securityTest",
                    level=_risk_level(rs.securityTest),
                    historical_failure_count=bundle.failure_history.by_step.get("securityTest", 0),
                    rationale="Rule-based score",
                ),
                StepRisk(
                    step="deploy",
                    level=_risk_level(rs.deploy),
                    historical_failure_count=bundle.failure_history.by_step.get("deploy", 0),
                    rationale="Rule-based score",
                ),
            ],
            recommended_actions=rs.reasons[:5],
            estimated_duration_min=int(bundle.failure_history.avg_success_duration_min or 0) or None,
            narrative="Rule-based risk assessment (no LLM).",
        )
        md = _rules_only_markdown(bundle, report)
        return bundle, report, md

    # ── Submodule diff enrichment — fetch actual code before structural analysis ──
    # If the parent diff only shows submodule pointer changes, fetch the real code
    # from the submodule repos so the LLM has something meaningful to analyze.
    try:
        from connectors.submodule_connector import get_submodule_diffs, summarize_submodule_diffs
        import os as _os2
        _git_ctx_pre = bundle.git_context
        _customer = _resolve_submodule_customer_name(bundle, _program_id)
        if _git_ctx_pre and _git_ctx_pre.diff_excerpt and _customer:
            _sm_diffs = get_submodule_diffs(_git_ctx_pre.diff_excerpt, _customer)
            if _sm_diffs:
                # Limit submodules to LLM — large diffs cause JSON truncation → retries → timeout
                # When total diff is large (>10KB), only send the most changed submodule
                # Each submodule is capped at 3KB in summarize_submodule_diffs
                _total_sm_size = sum(len(v) for v in _sm_diffs.values())
                _sm_limit = 1 if _total_sm_size > 10_000 else 2
                _sm_top = dict(
                    sorted(_sm_diffs.items(), key=lambda x: len(x[1]), reverse=True)[:_sm_limit]
                )
                _sm_summary = summarize_submodule_diffs(_sm_top)
                bundle.__dict__["submodule_diffs"] = _sm_diffs  # keep all for scorer
                bundle.__dict__["submodule_summary"] = _sm_summary
                # Only send top 3 to LLM
                _git_ctx_pre.diff_excerpt = (
                    (_git_ctx_pre.diff_excerpt or "") + "\n\n" + _sm_summary
                )
    except Exception:
        pass  # submodule enrichment is optional — never block main flow

    # ── Structural analysis first (deterministic, high confidence) ──────────────
    try:
        from analysis.build_predictor import predict_build_failures
        import os as _os
        git_ctx = bundle.git_context
        if git_ctx:
            repo_dir = bundle.__dict__.get("git_local_dir") or _os.getenv("GIT_LOCAL_DIR", "")
            structural = predict_build_failures(
                diff_text       = git_ctx.diff_excerpt or "",
                changed_files   = git_ctx.changed_files or [],
                commit_title    = git_ctx.title or "",
                repo_dir        = repo_dir,
                submodule_diffs = bundle.__dict__.get("submodule_diffs") or None,
            )
            # If structural analysis is certain enough, inject findings into bundle context
            if structural.is_structural and structural.findings:
                # Add structural findings as a pre-computed signal for the LLM
                bundle.__dict__["structural_findings"] = [
                    {"check": f.check, "step": f.step, "severity": f.severity,
                     "confidence": f.confidence, "title": f.title, "detail": f.detail}
                    for f in structural.findings
                ]
                # If override — skip LLM for build, but ALWAYS check environment for securityTest
                if structural.override_llm:
                    from models.risk_report import RiskReport, StepRisk

                    # Check environment state — securityTest risk is independent of code
                    sec_level    = "Low"
                    sec_rationale = "No environment signals detected."
                    env_actions: list = []
                    final_risk = structural.predicted_risk
                    final_step = structural.predicted_step

                    try:
                        from analysis.env_readiness import assess_environment_readiness
                        env = assess_environment_readiness(
                            bundle.execution_summary.__class__.__name__ and None or None,
                            None
                        )
                    except Exception:
                        env = None

                    # Also check pipeline_df/failed_df from bundle context
                    try:
                        from analysis.env_readiness import assess_environment_readiness, ENV_STEPS, DEFAULT_PROD_PIPELINE
                        _pdf, _fdf = _pdf_main, _fdf_main
                        env = assess_environment_readiness(
                            _pdf, _fdf, pipeline_name=DEFAULT_PROD_PIPELINE
                        )
                        if env and env["status"] in ("NOT_READY", "CAUTION"):
                            _dom = env.get("dominant_step", "")
                            if _dom in ENV_STEPS:
                                _win_count = env.get("env_step_failure_count", 0)
                                _consec_env = env.get("consecutive_failures", 0)
                                # High when: NOT_READY, OR CAUTION with window failures (even if consecutive=0)
                                # Medium when: CAUTION with 1+ consecutive
                                # Low when: CAUTION with 0 consecutive and 0 window failures
                                if env["status"] == "NOT_READY":
                                    sec_level = "High"
                                elif _consec_env > 0 or _win_count > 0:
                                    sec_level = "High" if _win_count >= 3 else "Medium"
                                else:
                                    sec_level = "Low"
                                sec_rationale = env["recommendation"]
                                env_actions = [
                                    f"Environment issue detected: {env['recommendation']}",
                                    f"Dominant failing step: {_dom} ({env['consecutive_failures']} consecutive failures)",
                                    f"Last success: {env['last_success_ago']}",
                                ]
                                # Escalate overall risk if environment is bad.
                                # IMPORTANT: do NOT escalate Low → Medium when the
                                # env failure is infrastructure (securityTest/deploy/loadTest)
                                # and the code risk is Low. These are unrelated — the
                                # commit didn't cause the infra failures. Escalating here
                                # causes ~87% false positive rate on FINISHED runs.
                                # Only escalate when the env failure is at build/codeQuality
                                # (which IS code-caused) or overall risk is already Medium+.
                                _is_infra_env_step = _dom in ENV_STEPS  # securityTest/deploy/loadTest
                                if sec_level == "High" and not _is_infra_env_step:
                                    final_risk = "High"
                                    final_step = _dom
                                elif final_risk != "Low" and sec_level == "High":
                                    # Already medium/high — env adds weight, don't suppress
                                    final_risk = "High"
                                    final_step = _dom
                                elif final_risk == "Low" and not _is_infra_env_step:
                                    # Only escalate Low→Medium when env fails at build (code-caused)
                                    final_risk = "Medium"
                                    final_step = _dom
                                # else: Low code risk + infra env step → keep Low, advisory only
                    except Exception:
                        pass

                    all_actions = [f.title for f in structural.findings[:3]] + env_actions
                    step_risks = [
                        StepRisk(step="build", level=structural.predicted_risk,
                                 historical_failure_count=0, rationale=structural.summary),
                        StepRisk(step="securityTest", level=sec_level,
                                 historical_failure_count=0, rationale=sec_rationale),
                    ]

                    report = RiskReport(
                        risk_level=final_risk,
                        confidence_score=structural.confidence,
                        commit_sha=commit_sha or "",
                        most_likely_failure_step=final_step,
                        modules_at_risk=git_ctx.aem_modules_touched or [],
                        step_risks=step_risks,
                        recommended_actions=all_actions[:5],
                        narrative=(
                            f"{structural.summary} "
                            + (f"Additionally: {sec_rationale}" if sec_level != "Low" else "")
                        ),
                        reasoning=_build_structural_reasoning(structural),
                    )

                    # Attach code-only recommendation + signals so dashboard hero
                    # uses correct confidence (not the structural.confidence which is
                    # detection confidence, not prediction confidence)
                    try:
                        from analysis.risk_scorer import (
                            code_recommendation as _cr_fn, CodeSignal as _CS,
                            EnvSignal as _ES,
                        )
                        # Normalise predicted_risk to uppercase for consistent comparison
                        # BuildPrediction uses "High"/"Medium"/"Low" mixed case
                        _pr_upper = structural.predicted_risk.upper() if structural.predicted_risk else "LOW"
                        _code_for_rec = _CS(
                            level=_pr_upper if _pr_upper in ("HIGH", "CERTAIN", "MEDIUM") else "LOW",
                            # confidence > 1 means it's a percentage (e.g. 65), divide by 100
                            # confidence ≤ 1 means it's already a ratio (e.g. 0.65), use directly
                            score=structural.confidence / 100.0 if structural.confidence > 1 else structural.confidence,
                            detail=structural.summary,
                            findings=[f"[{f.severity}] {f.title} ({f.confidence}% certain)" for f in structural.findings],
                            is_submodule_only=False, has_real_code=True,
                        )
                        _cr, _cc, _cb = _cr_fn(_code_for_rec)
                        report.__dict__["_code_recommendation"]   = _cr
                        report.__dict__["_code_confidence"]       = _cc
                        report.__dict__["_code_confidence_basis"] = _cb
                        # Attach env signal so dashboard env advisory uses window data
                        if env:
                            report.__dict__["_env_signal_raw"] = env
                        # Attach _code_signal for display
                        report.__dict__["_code_signal_override"] = {
                            "level":    _code_for_rec.level,
                            "score":    _code_for_rec.score,
                            "detail":   structural.summary,
                            "findings": [f"[{f.severity}] {f.title} ({f.confidence}% certain)"
                                         for f in structural.findings],
                        }
                    except Exception:
                        pass

                    return bundle, report, _structural_markdown(bundle, report, structural)
    except Exception:
        pass  # structural analysis is additive — never block LLM path

    # ── Always run environment check — inject into bundle before LLM ──────────
    # This ensures the LLM always sees environment state regardless of code analysis
    try:
        from analysis.env_readiness import assess_environment_readiness, ENV_STEPS, DEFAULT_PROD_PIPELINE
        _pdf, _fdf = _pdf_main, _fdf_main
        _env = assess_environment_readiness(
            _pdf, _fdf, pipeline_name=DEFAULT_PROD_PIPELINE
        )
        if _env:
            bundle.__dict__["environment_readiness"] = {
                "status":               _env["status"],
                "consecutive_failures": _env["consecutive_failures"],
                "dominant_step":        _env["dominant_step"],
                "is_env_issue":         _env["is_env_issue"],
                "last_success_ago":     _env["last_success_ago"],
                "recommendation":       _env["recommendation"],
            }
    except Exception:
        pass

    from agent.devops_agent import run_risk_analysis

    bundle_dict = bundle.model_dump(mode="json")
    if bundle.__dict__.get("submodule_diffs"):
        bundle_dict["submodule_diffs"] = bundle.__dict__["submodule_diffs"]
    # Java upgrade validation flag — feeds perf-risk checks and LLM context
    if bundle.__dict__.get("java_upgrade_pending"):
        bundle_dict["java_upgrade_pending"] = True
    else:
        from analysis.risk_scorer import infer_java_upgrade_pending as _infer_jdk
        bundle_dict["java_upgrade_pending"] = _infer_jdk(
            bundle_dict,
            _pdf_main,
            str(bundle.__dict__.get("dev_execution_id") or ""),
        )
    bundle_dict["dev_execution_id"] = str(bundle.__dict__.get("dev_execution_id") or "")
    # Carry env_readiness and structural_findings into bundle_dict (not in Pydantic model)
    if "environment_readiness" in bundle.__dict__:
        bundle_dict["environment_readiness"] = bundle.__dict__["environment_readiness"]
    if "structural_findings" in bundle.__dict__:
        bundle_dict["structural_findings"] = bundle.__dict__["structural_findings"]

    # ── Run ChromaDB lookup BEFORE LLM so scorer gets hits independently ────────
    # Long-term fix: historical signal should not depend on the LLM call at all.
    # We fetch similar incidents here, inject into bundle_dict, then pass to both
    # the LLM (for context) and score_risk() (for Signal 3).
    try:
        from agent.devops_agent import _enrich_risk_with_memory_with_hits
        from analysis.context_builder import build_risk_context
        _pre_context = bundle_dict if "commit_profile" in bundle_dict else build_risk_context(bundle_dict)
        from agent.devops_agent import _build_user_message
        _, _pre_hits = _enrich_risk_with_memory_with_hits(_build_user_message(_pre_context), _pre_context)
        if _pre_hits:
            bundle_dict["similar_incidents"] = _pre_hits
    except Exception:
        pass

    # ── Early score_risk() — skip LLM if signal is already certain ───────────────
    # Run a quick pre-score before the LLM call.
    # If env is NOT_READY with high confidence (≥80%), the LLM adds no value
    # for the decision — it only adds latency and cost.
    # Still call LLM for narrative, but skip if we already have a certain HOLD.
    _pre_score = None
    try:
        from analysis.risk_scorer import score_risk as _pre_score_risk
        _pdf_pre, _fdf_pre = _pdf_main, _fdf_main
        _pre_score = _pre_score_risk(
            bundle_dict=bundle_dict,
            diff_text=bundle.git_context.diff_excerpt if bundle.git_context else "",
            changed_files=bundle.git_context.changed_files if bundle.git_context else [],
            commit_title=bundle.git_context.title if bundle.git_context else "",
            pipeline_df=_pdf_pre, failed_df=_fdf_pre,
            program_id=_program_id,
        )
    except Exception:
        pass

    # Use cached LLM output if available — skip expensive LLM call
    if bundle.__dict__.get("_from_llm_cache") and bundle.__dict__.get("_cached_report"):
        report = bundle.__dict__["_cached_report"]
    else:
        # Always run the LLM — even when environment has 13 consecutive failures.
        #
        # Previously: skipped LLM when pre-score was HOLD ≥80%. This was wrong:
        # 1. The developer's CODE may also have issues — they need to know even if
        #    the environment is broken. Run 14 might pass the env issue but hit a
        #    code bug we never told them about.
        # 2. Environment signals are noisy (cancelled runs, infra spikes). Run 14
        #    can pass after 13 failures. Skipping LLM means no code analysis at all.
        # 3. The environment advisory is shown separately on the dashboard — it
        #    doesn't need to suppress code analysis to be visible.
        #
        # The LLM sees the environment signal via environment_readiness in the prompt
        # and will naturally weight it. The dashboard shows env as a separate card.
        print(f"  [risk] Running LLM analysis (pre-score: {_pre_score.recommendation if _pre_score else 'n/a'})")
        report = run_risk_analysis(bundle_dict)

    # ── score_risk() overrides: risk level, confidence, step ──────────────────
    # LLM provides narrative + technical_failure_hypotheses only.
    # score_risk() drives: risk_level, confidence_score, most_likely_failure_step
    try:
        from analysis.risk_scorer import score_risk as _score_risk
        _pdf3, _fdf3 = _pdf_main, _fdf_main
        _git_ctx3 = bundle.git_context

        # Inject similar_incidents from LLM report into bundle_dict so scorer sees them
        if report.__dict__.get("similar_incidents_raw"):
            bundle_dict["similar_incidents"] = report.__dict__["similar_incidents_raw"]

        # Use enriched diff — includes submodule summary appended by enrichment step
        # bundle.git_context.diff_excerpt was mutated in-place with submodule content
        _enriched_diff    = (_git_ctx3.diff_excerpt if _git_ctx3 else "") or ""
        _enriched_files   = list((_git_ctx3.changed_files if _git_ctx3 else []) or [])

        # If submodule diffs were fetched, extract their changed files so
        # compute_code_signal sees real Java/pom/config files, not just .gitmodules
        _sm_diffs = bundle.__dict__.get("submodule_diffs") or {}
        if _sm_diffs:
            import re as _re_sm
            for _sm_name, _sm_diff in _sm_diffs.items():
                for _m in _re_sm.finditer(r'^diff --git a/(.+?) b/', _sm_diff, _re_sm.MULTILINE):
                    _enriched_files.append(f"{_sm_name}/{_m.group(1)}")
            bundle_dict["submodule_diffs"] = _sm_diffs

        # LLM step risks — use as fallback when structural rules miss (e.g. reactor churn)
        if report.step_risks:
            bundle_dict["llm_step_risks"] = [
                {
                    "step": sr.step,
                    "level": sr.level.value if hasattr(sr.level, "value") else str(sr.level),
                    "rationale": sr.rationale or "",
                }
                for sr in report.step_risks
            ]
        if report.most_likely_failure_step:
            bundle_dict["llm_most_likely_step"] = report.most_likely_failure_step
        if hasattr(report, "risk_level"):
            bundle_dict["llm_risk_level"] = (
                report.risk_level.value if hasattr(report.risk_level, "value") else str(report.risk_level)
            )

        _exec_id = (
            bundle.__dict__.get("dev_execution_id")
            or bundle_dict.get("execution_id")
            or ""
        )
        bundle_dict["execution_id"] = _exec_id
        from analysis.risk_scorer import infer_java_upgrade_pending
        bundle_dict["java_upgrade_pending"] = infer_java_upgrade_pending(
            bundle_dict, _pdf3, str(_exec_id),
        )

        _decision = _score_risk(
            bundle_dict           = bundle_dict,
            diff_text             = _enriched_diff,
            changed_files         = _enriched_files,
            commit_title          = (_git_ctx3.title if _git_ctx3 else "") or "",
            repo_dir              = bundle.__dict__.get("git_local_dir", "") or "",
            pipeline_df           = _pdf3,
            failed_df             = _fdf3,
            program_id            = _program_id or bundle_dict.get("program_id", "") or os.getenv("PROGRAM_ID", ""),
            dev_execution_status  = bundle.__dict__.get("dev_execution_status", ""),
            pipeline_name         = bundle.__dict__.get("pipeline_name") or "Production Pipeline",
            # Use execution_date ONLY when explicitly set (from Splunk execution selection).
            # Do NOT fall back to commit_date — for live assessments the developer wants
            # to know today's env state, not the state as of when they wrote the commit.
            # Using commit_date causes "12 consecutive failures" to appear even when the
            # most recent pipeline passed (the success happened after the commit date).
            as_of_date            = bundle.__dict__.get("execution_date", "") or "",
        )
        # Map GO/CAUTION/HOLD → risk level
        _risk_map = {"GO": "Low", "CAUTION": "Medium", "HOLD": "High"}
        report.risk_level            = _risk_map.get(_decision.recommendation, report.risk_level)
        report.confidence_score      = int(_decision.confidence * 100)
        from analysis.risk_scorer import _pick_failure_step
        _raw_step = _pick_failure_step(
            code       = _decision.code,
            historical = _decision.historical,
            env        = _decision.env,
            llm        = _decision.llm,
            llm_step   = report.most_likely_failure_step or "",
        )
        # Dev pipeline passed → build + unit tests already validated.
        # If we predicted "build" as failure step, that's proven wrong — shift to deploy/securityTest.
        _dev_passed_flag = bundle.__dict__.get("dev_execution_status", "") == "FINISHED"
        if _dev_passed_flag and _raw_step in ("build", "codeQuality"):
            # Build already passed — production risk is deploy/securityTest
            _raw_step = _decision.env.dominant_step or "deploy"
        report.most_likely_failure_step = _raw_step
        # Store decision on report for dashboard display
        report.__dict__["risk_decision"] = _decision
        report.__dict__["env_signal"]    = _decision.env
        report.__dict__["code_signal"]   = _decision.code
        report.__dict__["hist_signal"]   = _decision.historical
        report.__dict__["llm_signal"]    = _decision.llm
        # Persist code-only recommendation so build hero never shows env-blended verdict
        try:
            from analysis.risk_scorer import code_recommendation as _code_rec_fn
            _cr, _cc, _cb = _code_rec_fn(_decision.code)
            report.__dict__["_code_recommendation"]       = _cr
            report.__dict__["_code_confidence"]           = _cc
            report.__dict__["_code_confidence_basis"]     = _cb
        except Exception:
            pass
    except Exception:
        pass  # scorer is additive — never block output

    # ── Save to SHA cache ─────────────────────────────────────────────────────
    if commit_sha and _program_id and use_llm:
        try:
            from analysis.assessment_cache import save_cached
            _consec_save = getattr(report.__dict__.get("env_signal"), "consecutive_failures", -1)
            save_cached(_program_id, commit_sha, report.model_dump(mode="json"), _consec_save)
        except Exception:
            pass

    md = _report_to_markdown(bundle, report)
    return bundle, report, md


def _build_structural_reasoning(structural) -> str:
    """Build a human-readable reasoning string from structural findings."""
    if not structural.findings:
        return "No structural signals found."

    high = [f for f in structural.findings if f.severity == "HIGH"]
    med  = [f for f in structural.findings if f.severity == "MEDIUM"]

    parts = []

    # Explain the confidence score
    parts.append(
        f"Confidence {structural.confidence}% is based on {len(structural.findings)} "
        f"structural check(s): {len(high)} HIGH and {len(med)} MEDIUM severity finding(s)."
    )

    # Explain each HIGH finding
    for f in high[:3]:
        check_explanations = {
            "vault_filter_duplicate": (
                f"Two content packages in this commit define the same JCR root path ({f.detail}). "
                "When both install, they conflict — one will overwrite the other or fail entirely."
            ),
            "vault_filter_conflict": (
                f"An existing package already owns this JCR path ({f.detail}). "
                "Installing another package with the same root causes deployment ordering failures."
            ),
            "interface_method_removed": (
                f"A public method was removed from an interface ({f.detail}). "
                "All Java classes that implement or call this method will fail to compile."
            ),
            "osgi_unresolved_reference": (
                f"A new @Reference annotation points to a service that has no @Service implementation in the repo ({f.detail}). "
                "The OSGi bundle will fail to activate at deployment time."
            ),
            "maven_snapshot_dep": (
                f"A SNAPSHOT dependency was added ({f.detail}). "
                "SNAPSHOT versions are unstable — build servers may resolve a different or broken version each time."
            ),
        }
        explanation = check_explanations.get(f.check, f.evidence)
        parts.append(f"① {f.title}: {explanation}")

    for f in med[:2]:
        parts.append(f"② {f.title}: {f.evidence}")

    # Note confidence level honestly — only javalang-confirmed findings are near-certain.
    # Regex-only findings (LOW severity) are advisory hints, not guaranteed failures.
    _has_certain = any(f.severity in ("CERTAIN", "HIGH") for f in structural.findings)
    parts.append(
        "This assessment is based on structural analysis of the code diff. "
        + (
            "HIGH/CERTAIN severity findings are AST-confirmed and very likely to fail compilation. "
            "LOW severity findings are heuristic hints — verify by running a local build."
            if _has_certain else
            "These are heuristic findings — run a local build to confirm before concluding the pipeline will fail."
        )
    )

    return " ".join(parts)


def _structural_markdown(bundle: AnalysisBundle, report: RiskReport, structural) -> str:
    lines = [
        f"# Pre-Deployment Risk Report (Structural Analysis)",
        f"**Risk Level:** {report.risk_level}  |  **Confidence:** {report.confidence_score}%",
        f"**Most Likely Failure:** {report.most_likely_failure_step}",
        "",
        "## Structural Findings",
    ]
    for f in structural.findings:
        lines.append(f"- **[{f.severity}]** {f.title}")
        lines.append(f"  - {f.detail}")
        lines.append(f"  - *Evidence: {f.evidence}*")
    lines += ["", "## Recommended Actions"]
    for i, a in enumerate(report.recommended_actions or [], 1):
        lines.append(f"{i}. {a}")
    return "\n".join(lines)


def _report_to_markdown(bundle: AnalysisBundle, report: RiskReport) -> str:
    lines = [
        "# Pre-Deployment Risk Report",
        "",
        f"**Risk Level:** {report.risk_level}",
        f"**Most Likely Failure Step:** {report.most_likely_failure_step}",
        "",
    ]
    if bundle.git_context:
        ctx = bundle.git_context
        lines.append(f"**Change:** PR #{ctx.pr_number or ''} / commit `{ctx.commit_sha or ''}`")
        lines.append(f"**Modules touched:** {', '.join(ctx.aem_modules_touched or [])}")
        lines.append("")
    if report.estimated_duration_min:
        lines.append(f"**Estimated Duration:** {report.estimated_duration_min} min")
        lines.append("")
    lines.append("## Step Risks")
    for sr in report.step_risks:
        lines.append(f"### {sr.step} — {sr.level}")
        lines.append(f"- Historical failures: {sr.historical_failure_count}")
        lines.append(f"- {sr.rationale}")
        lines.append("")
    lines.append("## Recommended Actions")
    for i, action in enumerate(report.recommended_actions, 1):
        lines.append(f"{i}. {action}")
    lines.append("")
    if report.narrative:
        lines.append("## Analysis")
        lines.append(report.narrative)
    return "\n".join(lines)


def _rules_only_markdown(bundle: AnalysisBundle, report: RiskReport) -> str:
    lines = [
        f"# Pre-Deployment Risk Report (rules only)",
        f"",
        f"**Risk Level:** {report.risk_level}",
        f"**PR/Commit:** {bundle.git_context.pr_number or bundle.git_context.commit_sha}",
        f"",
        f"**Modules touched:** {', '.join(bundle.git_context.aem_modules_touched or [])}",
        f"",
    ]
    for sr in report.step_risks:
        lines.append(f"- **{sr.step}:** {sr.level} ({sr.historical_failure_count} historical)")
    lines.append("")
    lines.append("## Rule reasons")
    for r in bundle.rule_scores.reasons if bundle.rule_scores else []:
        lines.append(f"- {r}")
    return "\n".join(lines)


def save_risk_report(
    report: RiskReport,
    markdown: str,
    pr_number: Optional[int] = None,
    commit_sha: Optional[str] = None,
    out_dir: str = "reports",
    program_id: str = "",
    customer: str = "",
) -> Tuple[str, str]:
    """
    Persist a risk report to SQLite (primary) and optionally to file (legacy fallback).

    SQLite storage: keyed by (program_id, sha) — full customer isolation.
    File storage: kept for backward-compat and local dev debugging only.
    On Eris production: set ARGUS_DB_PATH to a persistent volume path.
    """
    sha = commit_sha or ""
    pid = program_id or os.getenv("PROGRAM_ID", "unknown")

    # Primary: SQLite — safe for multi-user server, customer-isolated
    # Set ARGUS_DISABLE_CACHE=1 to skip caching during development/testing
    if not os.getenv("ARGUS_DISABLE_CACHE"):
        try:
            from db.report_store import save_risk_report as _db_save
            _report_dict = report.model_dump(mode="json")
            _db_save(
                program_id=pid,
                sha=sha,
                report_json=_report_dict,
                md_text=markdown,
                customer=customer,
            )
        except Exception as _e:
            import warnings
            warnings.warn(f"[report_store] SQLite write failed: {_e}", stacklevel=2)

    # Legacy: file-based (kept for local dev debugging, harmless on server)
    os.makedirs(out_dir, exist_ok=True)
    suffix = f"PR-{pr_number}" if pr_number else f"commit-{sha[:8]}"
    md_path   = os.path.join(out_dir, f"risk_{suffix}.md")
    json_path = os.path.join(out_dir, f"risk_{suffix}.json")
    try:
        Path(md_path).write_text(markdown, encoding="utf-8")
        Path(json_path).write_text(
            json.dumps(report.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass  # File write failure is non-fatal when SQLite succeeded

    return md_path, json_path
