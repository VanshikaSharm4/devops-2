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

    try:
        data = get_commit_diff(repo, commit_sha)
    except Exception:
        data = {}
    bundle.git_context = GitContext(
        commit_sha=commit_sha,
        title=data.get("title", ""),
        body=data.get("body", ""),
        author=data.get("author", ""),
        changed_files=data.get("changed_files", []),
        aem_modules_touched=get_changed_modules(data.get("changed_files", [])),
        diff_excerpt=data.get("diff_excerpt", ""),
    )

    bundle.rule_scores = compute_rule_scores(bundle)
    return bundle


def run_pre_deploy_risk(
    pr_number: Optional[int] = None,
    commit_sha: Optional[str] = None,
    fetch_logs: bool = True,
    use_llm: bool = True,
    bundle: Optional[AnalysisBundle] = None,
) -> Tuple[AnalysisBundle, Optional[RiskReport], str]:
    """
    Run full pre-deploy risk pipeline.
    Pass bundle from the dashboard to avoid rebuilding it on every click.
    Returns (bundle, structured_report, markdown).
    """
    if bundle is None:
        bundle, _, _, _ = build_base_bundle(fetch_logs=fetch_logs)
    bundle = attach_git_context(bundle, pr_number=pr_number, commit_sha=commit_sha)

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

    # ── Structural analysis first (deterministic, high confidence) ──────────────
    try:
        from analysis.build_predictor import predict_build_failures
        import os as _os
        git_ctx = bundle.git_context
        if git_ctx:
            repo_dir = _os.getenv("GIT_LOCAL_DIR", "")
            structural = predict_build_failures(
                diff_text     = git_ctx.diff_excerpt or "",
                changed_files = git_ctx.changed_files or [],
                commit_title  = git_ctx.title or "",
                repo_dir      = repo_dir,
            )
            # If structural analysis is certain enough, inject findings into bundle context
            if structural.is_structural and structural.findings:
                # Add structural findings as a pre-computed signal for the LLM
                bundle.__dict__["structural_findings"] = [
                    {"check": f.check, "step": f.step, "severity": f.severity,
                     "confidence": f.confidence, "title": f.title, "detail": f.detail}
                    for f in structural.findings
                ]
                # If override — skip LLM entirely for build prediction
                if structural.override_llm:
                    from models.risk_report import RiskReport, StepRisk
                    report = RiskReport(
                        risk_level=structural.predicted_risk,
                        confidence_score=structural.confidence,
                        commit_sha=commit_sha or "",
                        most_likely_failure_step=structural.predicted_step,
                        modules_at_risk=git_ctx.aem_modules_touched or [],
                        step_risks=[
                            StepRisk(step="build", level=structural.predicted_risk,
                                     historical_failure_count=0, rationale=structural.summary),
                        ],
                        recommended_actions=[f.title for f in structural.findings[:4]],
                        narrative=structural.summary,
                        reasoning=_build_structural_reasoning(structural),
                    )
                    return bundle, report, _structural_markdown(bundle, report, structural)
    except Exception:
        pass  # structural analysis is additive — never block LLM path

    from agent.devops_agent import run_risk_analysis

    bundle_dict = bundle.model_dump(mode="json")
    report = run_risk_analysis(bundle_dict)
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

    parts.append(
        "This assessment is deterministic — it is based on structural analysis of the code diff, "
        "not probabilistic pattern matching. The findings above are certain failure conditions "
        "unless the code is corrected before deployment."
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
) -> Tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    suffix = f"PR-{pr_number}" if pr_number else f"commit-{commit_sha[:8]}"
    md_path = os.path.join(out_dir, f"risk_{suffix}.md")
    json_path = os.path.join(out_dir, f"risk_{suffix}.json")
    Path(md_path).write_text(markdown, encoding="utf-8")
    Path(json_path).write_text(
        json.dumps(report.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    return md_path, json_path
