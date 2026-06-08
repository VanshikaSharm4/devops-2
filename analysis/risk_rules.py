"""
Evidence-weighted pre-deploy risk scoring.

Replaces the old frequency-counting approach: risk is now derived from
WHAT CHANGED in the commit, not how often the pipeline failed before.
Historical failures are used only as supporting evidence, and only when
they are high-signal (not infra/flaky noise).
"""
from __future__ import annotations

from typing import List

from analysis.aem_modules import get_changed_modules, modules_to_steps
from analysis.commit_analyzer import analyze_commit
from analysis.failure_classifier import classify_error_details, filter_high_signal_failures
from models.bundle import AnalysisBundle, RuleScores


# ── Internal helpers ───────────────────────────────────────────────────────────

def _max_level(a: str, b: str) -> str:
    order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    return a if order.get(a, 0) >= order.get(b, 0) else b


# ── Public API ─────────────────────────────────────────────────────────────────

def compute_rule_scores(bundle: AnalysisBundle) -> RuleScores:
    """
    Score build / securityTest / deploy risk from WHAT CHANGED, not history counts.

    Steps
    -----
    1. Bail early if no git context — return LOW for all.
    2. Build CommitProfile from the git context.
    3. Classify error_details, filter to high-signal failures only.
    4. Score each step based on commit evidence.
    5. Annotate reasons with causal explanations, not raw counts.
    """
    git = bundle.git_context
    reasons: List[str] = []

    build_risk = "LOW"
    security_risk = "LOW"
    deploy_risk = "LOW"

    # ── Step 1: no git context → no commit-level signal ───────────────────────
    if not git:
        return RuleScores(
            build=build_risk,
            securityTest=security_risk,
            deploy=deploy_risk,
            reasons=["No git context available — commit-level analysis not possible"],
        )

    # ── Step 2: CommitProfile ─────────────────────────────────────────────────
    changed_files = git.changed_files or []
    diff = git.diff_excerpt or ""
    commit_sha = git.commit_sha or ""
    title = git.title or ""

    profile = analyze_commit(changed_files, diff, commit_sha=commit_sha, title=title)

    # If aem_modules_touched was pre-populated (e.g. from context builder), prefer it
    if git.aem_modules_touched:
        profile.modules_touched = sorted(set(profile.modules_touched) | set(git.aem_modules_touched))
        profile.blast_radius = len(profile.modules_touched)

    modules = profile.modules_touched
    dep_files_lower = [f.lower() for f in profile.dependency_files]
    has_package_dep_file = any(
        name.endswith("package.json") or name.endswith("package-lock.json")
        for name in dep_files_lower
    )
    reactor_only_change = (
        profile.has_reactor_module_changes
        and not profile.added_dependencies
        and not profile.removed_dependencies
        and not has_package_dep_file
    )

    # ── Step 3: classify and filter failures ─────────────────────────────────
    all_classified = classify_error_details(bundle.error_details or [])
    high_signal = filter_high_signal_failures(all_classified, min_weight=0.5)
    infra_noise = [c for c in all_classified if c["signal_weight"] < 0.5]

    high_signal_steps = {c["step"] for c in high_signal}
    high_signal_build = [c for c in high_signal if c["step"] == "build"]
    history_by_step = bundle.failure_history.by_step if bundle.failure_history else {}
    module_steps = modules_to_steps(modules)
    success_rate = (
        bundle.execution_summary.success_rate_pct
        if bundle.execution_summary is not None
        else 100.0
    )

    def _apply_historical_prior(step: str, touched_modules: list[str]) -> None:
        nonlocal build_risk, security_risk, deploy_risk
        count = int(history_by_step.get(step, 0) or 0)
        if count < 5 or not touched_modules:
            return
        if (
            reactor_only_change
            and step == "build"
            and profile.removed_reactor_modules
            and not profile.added_reactor_modules
        ):
            return
        module_list = ", ".join(touched_modules[:3])
        reasons.append(
            f"{step.upper()} MEDIUM+: commit touches {module_list}, and {step} "
            f"has {count} recent failures — historical baseline raises the prior; "
            "validate this step before deployment"
        )
        if step == "build":
            build_risk = _max_level(build_risk, "MEDIUM")
        elif step == "securityTest":
            security_risk = _max_level(security_risk, "MEDIUM")
        elif step == "deploy":
            deploy_risk = _max_level(deploy_risk, "MEDIUM")

    for _step, _modules in module_steps.items():
        _apply_historical_prior(_step, _modules)

    if success_rate < 50:
        security_count = int(history_by_step.get("securityTest", 0) or 0)
        if security_count >= 5:
            security_level = "HIGH" if success_rate < 20 else "MEDIUM"
            security_risk = _max_level(security_risk, security_level)
            reasons.append(
                f"SECURITYTEST {security_level}: pipeline success rate is {success_rate}% and "
                f"securityTest has {security_count} recent failures/cancellations — "
                "operational stage risk, not necessarily commit-caused"
            )

    # ── Step 4a: BUILD RISK ───────────────────────────────────────────────────

    # Reactor module-list toggles affect packaging/deploy shape, but should not
    # be treated like dependency upgrades unless dependency entries also change.
    if reactor_only_change:
        if profile.added_reactor_modules:
            build_risk = _max_level(build_risk, "MEDIUM")
        if profile.removed_reactor_modules:
            deploy_risk = _max_level(deploy_risk, "MEDIUM")
        changed_modules = ", ".join(
            (profile.added_reactor_modules + profile.removed_reactor_modules)[:5]
        )
        affected_steps = "BUILD/DEPLOY" if profile.added_reactor_modules else "DEPLOY"
        reasons.append(
            f"{affected_steps} MEDIUM: reactor module list changed ({changed_modules}) — "
            "validate intended build parameters and generated package contents"
        )

    # Dependency file changes → HIGH (dep mismatch is a build-time risk)
    if profile.has_dependency_changes and not reactor_only_change:
        build_risk = "HIGH"
        dep_files = ", ".join(profile.dependency_files[:3])
        reasons.append(
            f"BUILD HIGH: dependency files modified ({dep_files}) — "
            "version mismatch or missing artifact can break the build"
        )
        if profile.added_dependencies:
            reasons.append(
                f"  Added deps: {', '.join(profile.added_dependencies[:5])} — "
                "ensure these resolve in the Cloud Manager Maven repo"
            )
        if profile.removed_dependencies:
            reasons.append(
                f"  Removed deps: {', '.join(profile.removed_dependencies[:5])} — "
                "verify no transitive consumers remain"
            )

    # core module + high-signal build failures → HIGH
    if "core" in modules and high_signal_build:
        build_risk = "HIGH"
        reasons.append(
            f"BUILD HIGH: core module modified and {len(high_signal_build)} high-signal "
            "build failure(s) in history — code-level regression likely"
        )

    # ui.frontend → at least MEDIUM (npm/webpack build is fragile)
    if "ui.frontend" in modules:
        build_risk = _max_level(build_risk, "MEDIUM")
        reasons.append(
            "BUILD MEDIUM+: ui.frontend modified — npm install / webpack build "
            "is sensitive to node_modules state"
        )

    # Large churn → MEDIUM
    if profile.lines_changed > 300:
        build_risk = _max_level(build_risk, "MEDIUM")
        reasons.append(
            f"BUILD MEDIUM+: {profile.lines_changed} lines changed — "
            "large changesets increase integration risk"
        )

    # Anti-pattern: pom.xml without tests → HIGH
    if "pom.xml modified without test changes" in profile.anti_patterns and not reactor_only_change:
        build_risk = "HIGH"
        reasons.append(
            "BUILD HIGH: pom.xml modified without accompanying test changes — "
            "dependency updates untested, breakage likely undetected until pipeline"
        )

    if "reactor module list changed without validation" in profile.anti_patterns:
        reasons.append(
            "BUILD/DEPLOY MEDIUM: reactor module list changed without validation — "
            "confirm the omitted/added module is intentional for this target branch"
        )

    # ── Step 4b: DEPLOY RISK ──────────────────────────────────────────────────

    # Config file changes → HIGH
    if profile.has_config_changes:
        deploy_risk = "HIGH"
        cfg_files = ", ".join(profile.config_files[:3])
        reasons.append(
            f"DEPLOY HIGH: config files modified ({cfg_files}) — "
            "Apache/dispatcher config syntax errors fail the deploy step"
        )

    # Dispatcher module → HIGH
    if "dispatcher" in modules:
        deploy_risk = "HIGH"
        reasons.append(
            "DEPLOY HIGH: dispatcher module modified — "
            "any invalid rewrite rule or vhost config will block deployment"
        )

    # Env var references → HIGH (may be missing in target env)
    if profile.env_vars_referenced:
        deploy_risk = "HIGH"
        env_list = ", ".join(profile.env_vars_referenced[:5])
        reasons.append(
            f"DEPLOY HIGH: commit references env vars ({env_list}) — "
            "these must be configured in Cloud Manager environment variables"
        )

    # CI/CD script changes → MEDIUM
    if profile.has_cicd_changes:
        deploy_risk = _max_level(deploy_risk, "MEDIUM")
        reasons.append(
            "DEPLOY MEDIUM+: CI/CD pipeline scripts modified — "
            "changes to Jenkinsfile or Docker can alter deployment behavior"
        )

    # ── Step 4c: SECURITY TEST RISK ───────────────────────────────────────────

    # Security-sensitive paths → HIGH
    if profile.has_security_changes:
        security_risk = "HIGH"
        sec_files = ", ".join(profile.security_sensitive_files[:3])
        reasons.append(
            f"SECURITYTEST HIGH: security-sensitive files modified ({sec_files}) — "
            "auth/ACL/OAuth changes may expose new attack surface"
        )

    # ui.config module → MEDIUM
    if "ui.config" in modules:
        security_risk = _max_level(security_risk, "MEDIUM")
        reasons.append(
            "SECURITYTEST MEDIUM+: ui.config modified — "
            "OSGi configuration changes can affect security bundle state"
        )

    # Infra-class security failures: acknowledge but do NOT elevate score
    infra_security = [
        c for c in infra_noise
        if c["step"] == "securityTest" or c["error_type"] in ("security_failure", "osgi_error")
    ]
    if infra_security:
        n = len(infra_security)
        reasons.append(
            f"Note: securityTest has {n} historical failure(s) but these are "
            "environment-level (CRXDE/DavEx) and do not reflect risk from this commit. "
            "They are classified as infra_failure (signal_weight=0.1) and excluded from scoring."
        )

    # ── Step 5: Anti-pattern summary ─────────────────────────────────────────
    if profile.anti_patterns:
        reasons.append(
            f"Anti-patterns detected: {'; '.join(profile.anti_patterns)}"
        )

    if not reasons:
        reasons.append(
            f"Commit touches modules: {', '.join(modules) or 'none'} — "
            "no elevated risk signals detected from commit analysis"
        )

    return RuleScores(
        build=build_risk,
        securityTest=security_risk,
        deploy=deploy_risk,
        reasons=reasons,
    )
