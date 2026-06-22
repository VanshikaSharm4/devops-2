"""
Deterministic build failure predictor.

Runs structural checks on the git diff BEFORE calling the LLM.
These checks are rule-based, not probabilistic — they catch definite failures
with high confidence so the LLM doesn't have to guess.

Checks:
  1. New Maven dependencies added → verify they're not obviously broken
  2. New OSGi @Reference → check if service implementation exists in repo
  3. Changed Java interface → find callers that may not compile
  4. New npm packages → flag unknown packages
  5. Vault filter conflicts → detect overlapping JCR paths
  6. Subtree/bot commit → flag as low risk immediately
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from analysis.diff_analyzer import DiffSignals, analyze_diff
from analysis.repo_scanner import (
    check_osgi_service_exists,
    find_filter_xml_conflicts,
    find_java_callers,
)


# ── Result structures ─────────────────────────────────────────────────────────

@dataclass
class BuildFinding:
    check:       str    # which check fired
    step:        str    # build / deploy / securityTest
    severity:    str    # HIGH / MEDIUM / LOW
    confidence:  int    # 0-100
    title:       str    # one-line summary
    detail:      str    # specific file/class/package involved
    evidence:    str    # what in the diff triggered this


@dataclass
class BuildPrediction:
    predicted_step:    str              # most likely failure step
    predicted_risk:    str              # High / Medium / Low
    confidence:        int              # 0-100
    findings:          List[BuildFinding] = field(default_factory=list)
    is_structural:     bool = False     # True = deterministic finding, not probabilistic
    override_llm:      bool = False     # True = don't call LLM, this is certain enough
    summary:           str = ""


# ── Individual checks ─────────────────────────────────────────────────────────

def _check_subtree_or_bot(signals: DiffSignals, title: str) -> Optional[BuildFinding]:
    """Subtree imports and Jenkins commits are structurally low risk for build."""
    if signals.is_subtree_import:
        return BuildFinding(
            check="subtree_import", step="build", severity="LOW", confidence=90,
            title="Git subtree import — code pre-validated in source repo",
            detail="All changed files are under one new directory",
            evidence=f"Commit title matches subtree pattern: '{title[:60]}'"
        )
    return None


def _check_new_maven_deps(signals: DiffSignals) -> List[BuildFinding]:
    """New Maven dependencies — flag unusual or suspicious coordinates."""
    findings = []
    for dep in signals.maven_deps_added:
        gid = dep.group_id
        aid = dep.artifact_id

        # Flag snapshot dependencies in production code
        if dep.version and "SNAPSHOT" in dep.version.upper():
            findings.append(BuildFinding(
                check="maven_snapshot_dep", step="build", severity="HIGH", confidence=85,
                title=f"SNAPSHOT dependency added: {gid}:{aid}:{dep.version}",
                detail=f"{gid}:{aid}",
                evidence=f"SNAPSHOT versions are unstable and may not resolve on build servers"
            ))

        # Flag very unusual group IDs that don't match known Adobe/Apache/org patterns
        known_prefixes = (
            "com.adobe", "org.apache", "com.day", "org.osgi",
            "javax", "com.google", "org.slf4j", "com.fasterxml",
            "com.idfcfirstbank", "com.idfcfirstacademy", "com.hdfc"
        )
        if not any(gid.startswith(p) for p in known_prefixes):
            findings.append(BuildFinding(
                check="maven_unknown_group", step="build", severity="MEDIUM", confidence=55,
                title=f"Unfamiliar Maven group added: {gid}:{aid}",
                detail=f"{gid}:{aid}:{dep.version or 'unknown'}",
                evidence="Group ID not matching known Adobe/Apache ecosystem prefixes — verify it exists in Adobe repo"
            ))

    return findings


def _check_osgi_references(signals: DiffSignals, repo_dir: str) -> List[BuildFinding]:
    """New @Reference annotations — check if referenced service exists in repo."""
    findings = []
    for sig in signals.osgi_signals:
        if sig.signal_type != "new_reference":
            continue
        # Extract service interface name from detail
        service_name = sig.detail.replace("New @Reference to ", "").strip()
        if not service_name or service_name == "unknown service":
            continue

        exists = check_osgi_service_exists(repo_dir, service_name)
        if not exists:
            findings.append(BuildFinding(
                check="osgi_unresolved_reference", step="deploy", severity="HIGH", confidence=78,
                title=f"New @Reference to {service_name} — no implementation found",
                detail=f"File: {sig.file}",
                evidence=f"Bundle will fail to activate if {service_name} has no registered @Service implementation"
            ))
    return findings


def _check_interface_changes(signals: DiffSignals, repo_dir: str) -> List[BuildFinding]:
    """Changed interface methods — find callers that may break compilation."""
    findings = []
    for change in signals.interface_changes:
        if change.change_type != "method_removed":
            continue
        callers = find_java_callers(repo_dir, change.class_name)
        # Exclude the changed file itself
        callers = [c for c in callers if c != change.file]
        if callers:
            findings.append(BuildFinding(
                check="interface_method_removed", step="build", severity="HIGH", confidence=82,
                title=f"Method '{change.method_name}' removed from {change.class_name}",
                detail=f"Used by {len(callers)} other file(s): {', '.join(callers[:3])}",
                evidence="Removing a public method from an interface breaks all callers at compile time"
            ))
    return findings


def _check_vault_conflicts(signals: DiffSignals, repo_dir: str) -> List[BuildFinding]:
    """Vault filter changes — detect overlapping JCR paths."""
    findings = []
    seen_roots: set = set()
    for change in signals.vault_filter_changes:
        root = change.root
        if root in seen_roots:
            # Two filter.xml files in the same commit define the same root
            findings.append(BuildFinding(
                check="vault_filter_duplicate", step="deploy", severity="HIGH", confidence=80,
                title=f"Duplicate vault filter root: {root}",
                detail=f"File: {change.file}",
                evidence="Two content packages with the same JCR root will conflict during deployment"
            ))
        seen_roots.add(root)

        # Check if other existing filter.xml files define the same root
        conflicts = find_filter_xml_conflicts(repo_dir, root)
        conflicts = [c for c in conflicts if c != change.file]
        if conflicts:
            findings.append(BuildFinding(
                check="vault_filter_conflict", step="deploy", severity="MEDIUM", confidence=65,
                title=f"Vault filter root '{root}' also defined in other packages",
                detail=f"Conflicting files: {', '.join(conflicts[:3])}",
                evidence="Overlapping content package filters can cause install ordering issues"
            ))
    return findings


def _check_npm_changes(signals: DiffSignals) -> List[BuildFinding]:
    """New npm packages added — flag potentially risky additions."""
    findings = []
    for change in signals.npm_changes:
        if not change.added:
            continue
        # Flag packages with wildcard versions
        if change.version and change.version in ("*", "latest", "next"):
            findings.append(BuildFinding(
                check="npm_wildcard_version", step="build", severity="MEDIUM", confidence=70,
                title=f"npm package with unstable version: {change.package_name}@{change.version}",
                detail=change.package_name,
                evidence=f"Wildcard/latest versions can resolve to broken releases on build servers"
            ))
    return findings


# ── Main predictor ────────────────────────────────────────────────────────────

def predict_build_failures(
    diff_text:     str,
    changed_files: List[str],
    commit_title:  str,
    repo_dir:      str = "",
) -> BuildPrediction:
    """
    Run all deterministic checks on a git diff.
    Returns a BuildPrediction with findings and overall risk assessment.
    """
    signals = analyze_diff(diff_text, changed_files, title=commit_title)

    all_findings: List[BuildFinding] = []

    # Check 0: Subtree / bot — early exit with low risk
    subtree_finding = _check_subtree_or_bot(signals, commit_title)
    if subtree_finding:
        return BuildPrediction(
            predicted_step = "none",
            predicted_risk = "Low",
            confidence     = 88,
            findings       = [subtree_finding],
            is_structural  = True,
            override_llm   = True,
            summary        = "Git subtree import or automated commit — build risk is structurally low.",
        )

    # Check 1: Maven deps
    all_findings.extend(_check_new_maven_deps(signals))

    # Check 2: OSGi references (requires repo)
    if repo_dir:
        all_findings.extend(_check_osgi_references(signals, repo_dir))

    # Check 3: Interface changes (requires repo)
    if repo_dir:
        all_findings.extend(_check_interface_changes(signals, repo_dir))

    # Check 4: Vault filter conflicts (deduplicated by root)
    vault_findings = _check_vault_conflicts(signals, repo_dir)
    seen_vault_checks = set()
    for vf in vault_findings:
        key = f"{vf.check}:{vf.detail[:40]}"
        if key not in seen_vault_checks:
            seen_vault_checks.add(key)
            all_findings.append(vf)

    # Check 5: npm changes
    all_findings.extend(_check_npm_changes(signals))

    # Aggregate into overall prediction
    if not all_findings:
        return BuildPrediction(
            predicted_step = "unknown",
            predicted_risk = "Low",
            confidence     = 40,
            findings       = [],
            is_structural  = False,
            override_llm   = False,
            summary        = "No structural build failure signals detected. LLM analysis recommended.",
        )

    # Find highest severity finding
    severity_order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    top_finding = max(all_findings, key=lambda f: severity_order.get(f.severity, 0))

    risk_map  = {"HIGH": "High", "MEDIUM": "Medium", "LOW": "Low"}
    predicted_risk = risk_map.get(top_finding.severity, "Low")

    # Confidence = average of top 3 findings, weighted by severity
    high_confs = sorted(
        [f.confidence for f in all_findings if f.severity == "HIGH"],
        reverse=True
    )
    confidence = int(sum(high_confs[:3]) / max(len(high_confs[:3]), 1)) if high_confs else top_finding.confidence

    # override_llm only if we have HIGH confidence deterministic finding
    override = any(f.severity == "HIGH" and f.confidence >= 78 for f in all_findings)

    summary_parts = [f.title for f in all_findings[:3]]
    summary = ". ".join(summary_parts) + "."

    return BuildPrediction(
        predicted_step = top_finding.step,
        predicted_risk = predicted_risk,
        confidence     = confidence,
        findings       = all_findings,
        is_structural  = True,
        override_llm   = override,
        summary        = summary,
    )
