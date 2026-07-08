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

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

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

_APP_CODE_EXTENSIONS = {".java", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".less", ".scss"}
_APP_CODE_PATHS      = {"filter.xml", "ui.frontend", "ui.apps", "ui.content", "ui.config", "dispatcher"}


def _parse_diff_file_sections(diff_text: str) -> Dict[str, List[str]]:
    """Split a unified diff into per-file hunks."""
    file_sections: Dict[str, List[str]] = {}
    current_file = ""
    for line in (diff_text or "").splitlines():
        m = re.match(r"^diff --git a/(.+?) b/", line)
        if m:
            current_file = m.group(1)
            file_sections[current_file] = []
        elif current_file:
            file_sections[current_file].append(line)
    return file_sections


def _section_has_substantive_java_changes(lines: List[str]) -> bool:
    for line in lines:
        if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
            continue
        body = line.lstrip("+-").strip()
        if body.startswith(("import ", "package ", "@import", "//", "*")):
            continue
        if body:
            return True
    return False


def merge_submodule_analysis_inputs(
    diff_text: str,
    changed_files: List[str],
    submodule_diffs: Optional[Dict[str, str]] = None,
) -> tuple[str, List[str]]:
    """
    Merge parent diff with raw submodule diffs so structural checks see real Java.
    Parent-only pointer bumps hide submodule code from build_predictor without this.
    """
    merged_diff = diff_text or ""
    merged_files = list(changed_files or [])
    if not submodule_diffs:
        return merged_diff, merged_files
    for sm_name, sm_diff in submodule_diffs.items():
        if not sm_diff:
            continue
        merged_diff += f"\n\n### submodule:{sm_name}\n{sm_diff}"
        for m in re.finditer(r"^diff --git a/(.+?) b/", sm_diff, re.MULTILINE):
            merged_files.append(f"{sm_name}/{m.group(1)}")
    return merged_diff, merged_files


def submodule_diffs_contain_java(submodule_diffs: Optional[Dict[str, str]]) -> bool:
    if not submodule_diffs:
        return False
    return any(re.search(r"\.java\b", d or "") for d in submodule_diffs.values())


def _is_submodule_release_bump(signals: DiffSignals) -> bool:
    """
    Returns True if the commit only touches submodule pointers + pom.xml — no app code.
    Routine release bumps: consistently pass in production. No deployment_ordering_issue observed.
    """
    if not signals.changed_files:
        return False
    for f in signals.changed_files:
        fl = f.lower()
        if "pom.xml" in fl or ".gitmodules" in fl:
            continue
        if any(fl.endswith(ext) for ext in _APP_CODE_EXTENSIONS):
            return False
        if any(p in fl for p in _APP_CODE_PATHS):
            return False
    has_infra = any("pom.xml" in f.lower() or ".gitmodules" in f.lower() for f in signals.changed_files)
    return has_infra




def _check_aem_build_plugin_versions(diff_text: str, changed_files: List[str]) -> List[BuildFinding]:
    """
    Detect changes to AEM content-package build extensions/plugins in pom.xml.

    These plugins provide the 'content-package' packaging type used by all AEM
    ui.apps, ui.content, ui.config, all modules. If the version referenced doesn't
    exist in Adobe's Nexus repo, Maven can't read ANY of those modules' pom.xml files,
    causing: "The build could not read N projects" and "Unknown packaging: content-package".

    This is a HIGH certainty build failure — Cloud Manager's Maven cache may not
    have a specific plugin version even if it exists locally.
    """
    findings = []
    pom_files = [f for f in changed_files if "pom.xml" in f.lower()]
    if not pom_files:
        return findings

    _AEM_BUILD_PLUGINS = {
        "filevault-package-maven-plugin": ("org.apache.jackrabbit", "content-package"),
        "content-package-maven-plugin":   ("com.day.jcr.vault",     "content-package"),
        "aemanalyser-maven-plugin":       ("com.adobe.aem",         "aem"),
    }

    lines = diff_text.splitlines()
    in_plugin_block = False
    current_artifact = ""

    for i, line in enumerate(lines):
        stripped = line.lstrip("+ -")
        if "<extension>" in stripped or "<plugin>" in stripped:
            in_plugin_block = True
            current_artifact = ""
        if "</extension>" in stripped or "</plugin>" in stripped:
            in_plugin_block = False
            current_artifact = ""

        if in_plugin_block and "<artifactId>" in stripped:
            _m = re.search(r'<artifactId>(.+?)</artifactId>', stripped)
            if _m:
                current_artifact = _m.group(1).strip()

        if (line.startswith("+") and not line.startswith("+++")
                and "<version>" in stripped
                and current_artifact in _AEM_BUILD_PLUGINS):
            _m = re.search(r'<version>(.+?)</version>', stripped)
            if _m:
                new_ver = _m.group(1).strip()
                group_id, pkg_type = _AEM_BUILD_PLUGINS[current_artifact]
                old_ver = ""
                for j in range(max(0, i - 5), i):
                    if lines[j].startswith("-") and "<version>" in lines[j]:
                        _mv = re.search(r'<version>(.+?)</version>', lines[j])
                        if _mv:
                            old_ver = _mv.group(1).strip()
                            break
                change_desc = f"→ {new_ver}" if not old_ver else f"{old_ver} → {new_ver}"
                findings.append(BuildFinding(
                    check="aem_build_plugin_version_change",
                    step="build",
                    severity="HIGH",
                    confidence=82,
                    title=f"AEM build plugin version changed: {current_artifact} {change_desc}",
                    detail=f"{group_id}:{current_artifact}:{new_ver}",
                    evidence=(
                        f"'{current_artifact}' provides '{pkg_type}' packaging. "
                        f"If version {new_ver} is missing from repo.adobe.com/nexus, Maven cannot read "
                        f"ANY module using <packaging>{pkg_type}</packaging>, causing "
                        f"'build could not read N projects' and 'Unknown packaging: {pkg_type}'. "
                        f"Verify this version exists in Adobe Nexus before pushing."
                    ),
                ))
    return findings


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


def _check_npm_install_risks(diff_text: str, changed_files: List[str]) -> List[BuildFinding]:
    """
    Detect npm install failure risks from package.json changes.

    Catches:
    1. package.json changed but package-lock.json NOT updated → npm install may fail
       with conflicting peer dependencies
    2. file: or workspace: protocol references → won't resolve in CI (different filesystem)
    3. package.json JSON syntax error in diff → npm install fails immediately

    Cannot detect:
    - npm registry network failures (infrastructure)
    - Transitive dependency conflicts (need to run npm install)
    - Packages removed from registry (need live check)
    """
    findings = []

    pkg_json_files  = [f for f in changed_files if f.endswith("package.json")
                       and "node_modules" not in f]
    lock_files      = [f for f in changed_files if "package-lock.json" in f
                       or "yarn.lock" in f or "pnpm-lock.yaml" in f]

    if not pkg_json_files:
        return findings

    lines = diff_text.splitlines()

    for pkg_path in pkg_json_files:
        # Extract added lines for this package.json
        in_file = False
        added_lines = []
        for line in lines:
            if line.startswith("+++ b/") and pkg_path in line:
                in_file = True
                added_lines = []
            elif in_file:
                if line.startswith("diff --git"):
                    break
                if line.startswith("+") and not line.startswith("+++"):
                    added_lines.append(line[1:])

        if not added_lines:
            continue

        content = "\n".join(added_lines)
        fname = pkg_path.split("/")[-2] + "/package.json"

        # Check 1: package.json changed without lockfile update
        module_has_lockfile = any(
            lock_f.startswith(pkg_path.rsplit("/", 1)[0])
            for lock_f in lock_files
        )
        if not module_has_lockfile and len(added_lines) > 5:
            findings.append(BuildFinding(
                check="npm_lockfile_missing",
                step="build", severity="MEDIUM", confidence=72,
                title=f"{fname} changed but no package-lock.json update — npm install may fail",
                detail=pkg_path,
                evidence=(
                    "package.json was updated but package-lock.json was not committed in the same diff. "
                    "npm install in CI will resolve fresh versions which may conflict with what was tested locally. "
                    "Run 'npm install' locally, commit the updated package-lock.json."
                ),
            ))

        # Check 2: file: or workspace: protocol references (won't work in CI)
        import re as _re_npm
        for proto_match in _re_npm.finditer(r'"(file:|workspace:)[^"]*"', content):
            findings.append(BuildFinding(
                check="npm_local_path_reference",
                step="build", severity="HIGH", confidence=85,
                title=f"{fname} uses local path reference: {proto_match.group(0)[:60]}",
                detail=pkg_path,
                evidence=(
                    f"'{proto_match.group(0)}' uses a local filesystem reference. "
                    f"This works on your machine but fails in Cloud Manager CI because "
                    f"the relative path doesn't exist in the build container. "
                    f"Use a published npm package version instead."
                ),
            ))

        # Check 3: package.json JSON syntax error
        try:
            import json as _json
            _json.loads(content)
        except _json.JSONDecodeError as _je:
            findings.append(BuildFinding(
                check="npm_package_json_syntax",
                step="build", severity="HIGH", confidence=95,
                title=f"{fname} has JSON syntax error — npm install will fail immediately",
                detail=pkg_path,
                evidence=(
                    f"package.json has invalid JSON syntax: {str(_je)[:120]}. "
                    f"npm install cannot parse package.json and exits with error code 1."
                ),
            ))

    return findings


_MERGE_COMMIT_TITLE_RE = re.compile(
    r"^merge\s+(branch|pull request|commit|remote-tracking|tag)\b", re.IGNORECASE
)


def _is_merge_commit(title: str) -> bool:
    """True for git merge commits — aggregated diff spans many prior commits."""
    return bool(_MERGE_COMMIT_TITLE_RE.match((title or "").strip()))


def _check_cloudmanager_java_version(changed_files: List[str], diff_text: str, repo_dir: str = "") -> List[BuildFinding]:
    """
    .cloudmanager/java-version change — significant build environment shift.

    Changing the Java version in Cloud Manager (.cloudmanager/java-version) affects:
    - JIT compilation behaviour → tests may pass but run much slower (timeout risk)
    - TLS/SSL library changes → external call tests may fail
    - API compatibility → Java 17→21 has removed/deprecated APIs
    - Maven toolchain requirement changes

    This is HIGH risk regardless of how small the diff looks.
    Real-world evidence: Java 21 migration caused DynamicDropdownModelImplTest to run
    10x slower (601s vs 53s), triggering a CM build timeout despite 0 test failures.
    """
    findings = []
    for f in changed_files:
        fl = f.lower()
        if ".cloudmanager" in fl and "java-version" in fl:
            # Extract old and new Java version from diff if available
            import re as _re_java
            old_v = new_v = ""
            if diff_text:
                _old = _re_java.search(r'^-(\d+)', diff_text, _re_java.MULTILINE)
                _new = _re_java.search(r'^\+(\d+)', diff_text, _re_java.MULTILINE)
                old_v = _old.group(1) if _old else ""
                new_v = _new.group(1) if _new else ""

            version_info = f"{old_v} → {new_v}" if old_v and new_v else "version changed"

            # Check if the team has properly configured Java toolchains.
            # Priority: read the actual repo files (most accurate).
            # Fallback: check the diff for toolchain config changes.
            import os as _os_java
            _has_toolchain_config = False
            _toolchain_evidence = ""

            # Check 1: read pom.xml from repo — look for toolchain plugin + compiler settings
            # This is more reliable than checking the diff (config may have been added earlier)
            _pom_path = _os_java.path.join(repo_dir, "pom.xml") if repo_dir else ""
            if _pom_path and _os_java.path.isfile(_pom_path):
                try:
                    _pom_content = open(_pom_path, encoding="utf-8", errors="ignore").read()
                    _has_toolchain_plugin = "maven-toolchains-plugin" in _pom_content
                    _has_compiler_21 = bool(re.search(
                        r'<(?:source|target|release)>\s*21\s*</(?:source|target|release)>',
                        _pom_content
                    ))
                    _has_vendor_cfg = bool(re.search(
                        r'<vendor>\s*(?:oracle|openjdk|adoptium)\s*</vendor>',
                        _pom_content, re.IGNORECASE
                    ))
                    if _has_toolchain_plugin and (_has_compiler_21 or _has_vendor_cfg):
                        _has_toolchain_config = True
                        _toolchain_evidence = (
                            f"pom.xml has maven-toolchains-plugin configured"
                            + (" with Java 21 compiler settings" if _has_compiler_21 else "")
                        )
                except Exception:
                    pass

            # Check 2: .mvn/toolchains.xml or toolchains.xml in repo root
            if not _has_toolchain_config and repo_dir:
                for _tc_path in [
                    _os_java.path.join(repo_dir, ".mvn", "toolchains.xml"),
                    _os_java.path.join(repo_dir, "toolchains.xml"),
                ]:
                    if _os_java.path.isfile(_tc_path):
                        try:
                            _tc_content = open(_tc_path, encoding="utf-8", errors="ignore").read()
                            if re.search(r'<version>\s*21', _tc_content) or \
                               re.search(r'jdk-21|jdk21', _tc_content, re.IGNORECASE):
                                _has_toolchain_config = True
                                _toolchain_evidence = f"toolchains.xml defines JDK 21"
                                break
                        except Exception:
                            pass

            # Check 3: fallback to diff content if repo not available
            if not _has_toolchain_config:
                _has_toolchain_config = bool(re.search(
                    r'toolchain|<vendor>|<jdkVersion>|maven-toolchains-plugin',
                    diff_text or "", re.IGNORECASE
                ))
                if _has_toolchain_config:
                    _toolchain_evidence = "toolchain config updated in this diff"

            if _has_toolchain_config:
                # Team updated toolchain config alongside java-version → prepared migration
                sev, conf = "MEDIUM", 55
                evidence = (
                    f"Java version changed to {new_v or 'new version'} AND toolchain config updated "
                    f"in same commit — team has prepared for this migration. "
                    f"Risk: JIT performance (tests may be slower), any remaining API incompatibilities. "
                    f"Verify build time stays within Cloud Manager limits (~45 min)."
                )
            else:
                sev, conf = "HIGH", 80
                evidence = (
                    f"Changing .cloudmanager/java-version alters the entire build/test runtime. "
                    f"Risks: JIT performance regression (tests may timeout), API removal (Java 11→17→21 "
                    f"removes deprecated APIs), TLS library changes breaking external calls in tests. "
                    f"Even if tests PASS, they may run 10x slower and trigger CM build timeouts."
                )

            findings.append(BuildFinding(
                check="cloudmanager_java_version_change", step="build",
                severity=sev, confidence=conf,
                title=f"Cloud Manager Java version changed: {version_info}",
                detail=f,
                evidence=evidence,
            ))
    return findings


def _check_service_without_test_update(
    diff_text: str,
    commit_title: str = "",
) -> List[BuildFinding]:
    """
    Production service/validation Java changed with no *Test.java updates in the
    same commit — common cause of surefire NullPointerException (HDFC submodule pattern).

    Merge commits are downgraded: tests are often updated in earlier commits on the
    source branch, so same-commit test absence is not predictive.
    """
    findings: List[BuildFinding] = []
    if not diff_text:
        return findings

    is_merge = _is_merge_commit(commit_title)

    # Large migration/upgrade/refactor commits: many services change intentionally.
    # Tests exist but weren't co-committed (they'll still pass). Reduce confidence
    # to avoid false HIGH findings on coordinated codebase changes.
    _migration_keywords = ("migration", "migrate", "upgrade", "lts", "java21",
                           "java 21", "refactor", "restructure", "reorg")
    _is_migration_commit = any(k in commit_title.lower() for k in _migration_keywords)

    sections = _parse_diff_file_sections(diff_text)
    prod_services: List[tuple[str, str]] = []
    test_files_changed: List[str] = []

    for filepath, lines in sections.items():
        fl = filepath.lower()
        if not fl.endswith(".java") or not _section_has_substantive_java_changes(lines):
            continue
        basename = filepath.split("/")[-1]
        if basename.endswith("Test.java") or "/test/" in fl or "/test/java/" in fl:
            test_files_changed.append(basename)
            continue
        if any(k in fl for k in ("service", "validation", "processor", "handler", "manager")):
            class_name = basename.replace(".java", "")
            prod_services.append((filepath, class_name))

    if not prod_services:
        return findings

    for filepath, class_name in prod_services:
        class_lower = class_name.lower()
        has_test_update = any(
            class_lower in tf.lower() or tf.lower().startswith(class_lower[:12])
            for tf in test_files_changed
        )
        if not has_test_update:
            # Config/orchestration classes rarely have unit tests and rarely break build
            # when changed — they're Spring @Configuration/@Component beans, not service logic.
            # Impl classes with business logic are the real risk.
            _is_config_class = any(k in class_lower for k in ("config", "orchestration", "properties", "settings", "factory"))
            _is_impl_class   = class_lower.endswith("impl") or any(k in class_lower for k in ("serviceimpl", "processorimpl"))

            if _is_migration_commit:
                # Migration/upgrade/refactor PRs: many services change but tests still pass.
                # Developer intentionally touched many files — existing tests cover them.
                # Advisory LOW only — don't alarm on every service in a Java 21 migration.
                sev, conf = "LOW", 30
                evidence = (
                    f"{class_name} changed in a migration/upgrade commit. "
                    f"Tests likely still pass — this is a coordinated change, not a regression. "
                    f"Advisory: verify existing tests cover the updated logic."
                )
            elif is_merge:
                sev, conf = "MEDIUM", 45
                evidence = (
                    f"{class_name} changed in a merge commit without *Test.java in this "
                    f"diff — tests may have been updated on the source branch already."
                )
            elif _is_config_class:
                # Config/orchestration classes — lower risk, they rarely break build
                sev, conf = "MEDIUM", 45
                evidence = (
                    f"{class_name} is a config/orchestration class. Changes to @Configuration "
                    f"or @Component beans rarely break builds directly, but may cause runtime "
                    f"wiring issues if new bean dependencies aren't provided. Advisory only."
                )
            elif _is_impl_class:
                # Analyse what kind of change was made to predict failure type
                section_text = "\n".join(sections.get(filepath, []))
                _methods_removed = re.findall(r'^-\s+(?:public|private|protected)\s+\S+\s+(\w+)\s*\(', section_text, re.MULTILINE)
                _methods_added   = re.findall(r'^\+\s+(?:public|private|protected)\s+\S+\s+(\w+)\s*\(', section_text, re.MULTILINE)
                _return_changed  = bool(re.search(r'^-\s+return\s+', section_text, re.MULTILINE) and
                                        re.search(r'^\+\s+return\s+', section_text, re.MULTILINE))

                if _methods_removed:
                    failure_types = "Mockito verification failure ('wanted but not invoked') — tests that verify removed/renamed methods will fail"
                    if _return_changed:
                        failure_types += " AND assertion failure ('expected X but was Y') — return value changed"
                elif _return_changed:
                    failure_types = "assertion failure ('expected X but was Y') — a return value or condition changed"
                else:
                    failure_types = "NullPointerException (new dependency not mocked) or assertion failure (logic changed)"

                sev, conf = "HIGH", 72
                evidence = (
                    f"{class_name} implementation changed but no *Test.java updated in same commit. "
                    f"Likely runtime failure: {failure_types}. "
                    f"Maven Surefire will report test failures — code compiles, tests run, then fail."
                )
            else:
                sev, conf = "MEDIUM", 55
                evidence = (
                    f"{class_name} changed but no matching *Test.java updated. "
                    f"Tests may fail at runtime with assertion errors or NullPointerException."
                )
            findings.append(BuildFinding(
                check="service_without_test_update",
                step="build",
                severity=sev,
                confidence=conf,
                title=f"{class_name} changed — tests may fail at runtime (assertion/Mockito verification/NPE)",
                detail=filepath,
                evidence=evidence,
            ))

    # Broader: core Java changed, zero test files touched in entire diff
    if not test_files_changed:
        core_java = [
            fp for fp, lines in sections.items()
            if fp.endswith(".java")
            and _section_has_substantive_java_changes(lines)
            and "/core/" in fp.lower()
            and not fp.endswith("Test.java")
        ]
        if core_java and not any(f.check == "service_without_test_update" for f in findings):
            findings.append(BuildFinding(
                check="core_java_without_test_update",
                step="build",
                severity="MEDIUM",
                confidence=65,
                title=f"{len(core_java)} core Java file(s) changed — tests may fail at runtime",
                detail=core_java[0],
                evidence=(
                    "Core module Java changed without any *Test.java updates in this commit. "
                    "Existing tests may throw NullPointerException, NoClassDefFoundError, or "
                    "ExceptionInInitializerError at runtime if the changed code breaks test assumptions. "
                    "Maven Surefire will report the failures — the code compiles, the tests run, then fail."
                ),
            ))

    return findings


def _check_multi_submodule_reactor(
    diff_text: str,
    changed_files: List[str],
    signals: DiffSignals,
) -> List[BuildFinding]:
    """Multiple submodule bumps or reactor module list changes — HDFC daily-deploy pattern."""
    findings: List[BuildFinding] = []
    text = diff_text or ""

    sub_bumps = len(re.findall(r"^[-+]\s*Subproject commit", text, re.MULTILINE))
    module_line_changes = len(re.findall(r"^[-+].*<module>", text, re.MULTILINE))
    has_gitmodules = any(".gitmodules" in f for f in (changed_files or []))

    # Detect "restoration" pattern: module was commented out, now uncommented.
    # This is restoring an existing module, not adding a new one — much lower risk.
    # Pattern: removed "<!--<module>X</module>-->" and added "<module>X</module>"
    _uncomments = re.findall(r"^-\s*<!--.*?<module>(.+?)</module>.*?-->", text, re.MULTILINE)
    _new_enables = re.findall(r"^\+\s*<module>(.+?)</module>", text, re.MULTILINE)
    _restored_modules = set(_uncomments) & set(_new_enables)

    # Also detect inverse: commenting out a previously active module (lower risk)
    _was_active = re.findall(r"^-\s*<module>(.+?)</module>", text, re.MULTILINE)
    _now_commented = re.findall(r"^\+\s*<!--.*?<module>(.+?)</module>.*?-->", text, re.MULTILINE)
    _deactivated_modules = set(_was_active) & set(_now_commented)

    # Net NEW module additions (truly new, not restorations)
    _truly_new = [m for m in _new_enables if m not in _restored_modules]

    # If ALL module changes are restorations/deactivations — very low risk
    _is_restore_only = bool(_restored_modules or _deactivated_modules) and not _truly_new

    if _is_restore_only:
        # Restore/toggle of existing module — base risk is LOW,
        # BUT the re-enabled module may contain Java syntax errors or other issues.
        # Scan its Java files before declaring it safe.
        _module_names = sorted(_restored_modules | _deactivated_modules)[:3]
        _module_label = " and ".join(_module_names) if len(_module_names) <= 2 else ", ".join(_module_names)
        _action = "re-enabled" if _restored_modules else "removed from"
        findings.append(BuildFinding(
            check="reactor_module_toggle",
            step="build",
            severity="LOW",
            confidence=30,
            title=f"pom.xml turns {_action} the build: {_module_label}",
            detail=(
                f"{_module_label} {'was' if len(_module_names) == 1 else 'were'} previously disabled in pom.xml "
                f"and {'is' if len(_module_names) == 1 else 'are'} now re-added. "
                f"Run a local build to confirm {'it compiles' if len(_module_names) == 1 else 'they compile'} cleanly."
            ),
            evidence="pom.xml reactor module toggle",
        ))
        return findings  # syntax check for re-enabled modules is done separately (Check 0)

    if module_line_changes >= 3:
        findings.append(BuildFinding(
            check="reactor_module_list_churn",
            step="build",
            severity="HIGH",
            confidence=70,
            title=f"pom.xml changes which {module_line_changes} modules get built — build order may break",
            detail=(
                "Changing many modules at once reorders what Maven compiles first. "
                "If a module that's needed early is now compiled later, the build fails "
                "with 'artifact not found' or compilation errors."
            ),
            evidence="pom.xml reactor module list churn",
        ))

    if sub_bumps >= 2:
        # Submodule pointer bumps are a routine HDFC/multi-module operation.
        # The BUILD step risk is LOW — parent compiles fine since no app code changed.
        # The real risk is at DEPLOY (bundle ordering, package compatibility).
        # Only raise to MEDIUM if there are also reactor/module list changes.
        _sm_severity = "MEDIUM" if module_line_changes >= 2 else "LOW"
        _sm_step     = "deploy" if _sm_severity == "LOW" else "build"
        findings.append(BuildFinding(
            check="multi_submodule_bump",
            step=_sm_step,
            severity=_sm_severity,
            confidence=55 if _sm_severity == "LOW" else 62,
            title=f"{sub_bumps} submodules updated at once — verify submodule compatibility",
            detail=(
                f"This commit bumps {sub_bumps} submodule versions simultaneously. "
                "Parent repo compiles fine (no app code changed here). "
                "Risk: submodule contents may have incompatible changes that surface at deploy time."
                if _sm_severity == "LOW" else
                f"This commit bumps {sub_bumps} submodule versions simultaneously. "
                "If any submodule has compilation errors, this build will fail."
            ),
            evidence="multiple submodule pointer bumps",
        ))
    elif sub_bumps >= 1 and (module_line_changes >= 1 or signals.has_pom_change):
        findings.append(BuildFinding(
            check="submodule_reactor_change",
            step="build",
            severity="MEDIUM",
            confidence=58,
            title="Submodule update combined with pom.xml module changes",
            detail=(
                "A submodule version was bumped at the same time as the reactor module list changed. "
                "These together can break the build if the new submodule version isn't "
                "compatible with the updated module structure."
            ),
            evidence="submodule bump + pom.xml reactor change",
        ))
    elif module_line_changes >= 2:
        findings.append(BuildFinding(
            check="reactor_module_list_change",
            step="build",
            severity="MEDIUM",
            confidence=55,
            title="pom.xml adds or removes modules from the build",
            detail=(
                "Modules were added or removed from the Maven build. "
                "If a removed module is still depended on by another, the build will fail. "
                "If a newly added module has code issues, they surface here."
            ),
            evidence="pom.xml module list changed",
        ))
    elif has_gitmodules and signals.has_pom_change and sub_bumps == 0:
        gitlink_changes = len(re.findall(r"^[-+]\s*Subproject commit", text, re.MULTILINE))
        if gitlink_changes >= 1:
            findings.append(BuildFinding(
                check="submodule_pointer_with_pom",
                step="build",
                severity="MEDIUM",
                confidence=55,
                title="Submodule version updated alongside pom.xml changes",
                detail=(
                    "A submodule was updated to a new commit version while pom.xml also changed. "
                    "Check that the submodule's new version is compatible with the rest of the build."
                ),
                evidence="submodule pointer + pom.xml change",
            ))

    return findings


def _check_autowired_test_mocks(diff_text: str, changed_files: List[str]) -> List[BuildFinding]:
    """
    Detect new @Autowired/@Inject fields added to service classes where
    test files are NOT updated. A new field injected into a service but
    not mocked in tests causes NullPointerException at test runtime.

    Pattern:
      - Added @Autowired / @Inject line in a Service/Component class
      - No corresponding change in a *Test.java file for the same class
    """
    import re
    findings = []
    if not diff_text:
        return findings

    # Find files where @Autowired was added
    file_sections: dict = {}
    current_file = ""
    for line in diff_text.splitlines():
        m = re.match(r'^diff --git a/(.+?) b/', line)
        if m:
            current_file = m.group(1)
            file_sections[current_file] = []
        elif current_file:
            file_sections[current_file].append(line)

    service_files_with_new_autowired = []
    test_files_changed = set()

    for filepath, lines in file_sections.items():
        fl = filepath.lower()
        if not fl.endswith(".java"):
            continue

        if "test" in fl or "spec" in fl:
            test_files_changed.add(fl)
            continue

        section = "\n".join(lines)
        # New @Autowired or @Inject added
        new_autowired = re.findall(r'^\+\s*@(Autowired|Inject)\b', section, re.MULTILINE)
        if new_autowired:
            # Only flag for service/component classes likely to have tests
            if any(k in fl for k in ["service", "component", "validation", "processor", "handler", "manager"]):
                class_name = filepath.split("/")[-1].replace(".java", "")
                service_files_with_new_autowired.append((filepath, class_name, len(new_autowired)))

    for filepath, class_name, count in service_files_with_new_autowired:
        # Check if a corresponding test file was also changed
        test_name = class_name + "test"
        has_test_update = any(test_name in tf for tf in test_files_changed)
        if not has_test_update:
            findings.append(BuildFinding(
                check="autowired_missing_test_mock", step="build", severity="HIGH", confidence=72,
                title=f"New @Autowired field in {class_name} — test mock may be missing",
                detail=filepath,
                evidence=(
                    f"{count} new @Autowired/@Inject field(s) added to {class_name} "
                    f"but no corresponding *Test.java was updated. "
                    f"Tests that use @InjectMocks on this class will get NullPointerException "
                    f"if the new dependency is not added to the test's @Mock setup."
                )
            ))

    return findings


def _check_pom_structural_errors(diff_text: str, changed_files: List[str], repo_dir: str = "") -> List[BuildFinding]:
    """
    Scan pom.xml files for structural errors that cause deterministic Maven failures.

    Checks (all >99% accuracy when found):
    1. Duplicate <dependency> entries — Maven errors: "must be unique"
    2. <parent> version missing or set to LATEST/RELEASE — resolution failure
    3. Conflicting dependency scopes (same artifact in compile + test) — classpath conflict
    """
    findings: List[BuildFinding] = []
    import os as _os

    def _scan_pom(content: str, filepath: str) -> List[BuildFinding]:
        local_findings = []
        lines = content.splitlines()

        # ── Check 1: Duplicate <dependency> entries ───────────────────────────
        # Maven error: "dependencies.dependency.(groupId:artifactId:type:classifier) must be unique"
        seen_coords: dict = {}  # (groupId, artifactId, type) → (line_no, scope)
        in_dep = False
        current_gid = current_aid = current_type = current_scope = ""
        dep_start_line = 0

        for i, line in enumerate(lines):
            s = line.strip()
            if "<dependency>" in s and not "<dependencies>" in s:
                in_dep = True
                current_gid = current_aid = current_type = current_scope = ""
                dep_start_line = i
            elif "</dependency>" in s and in_dep:
                in_dep = False
                if current_gid and current_aid:
                    key = (current_gid, current_aid, current_type or "jar")
                    if key in seen_coords:
                        prev_scope = seen_coords[key]["scope"]
                        curr_scope = current_scope or "compile"
                        if prev_scope == curr_scope:
                            local_findings.append(BuildFinding(
                                check="pom_duplicate_dependency",
                                step="build", severity="HIGH", confidence=99,
                                title=f"Duplicate dependency: {current_gid}:{current_aid} appears twice in {filepath.split('/')[-1]}",
                                detail=filepath,
                                evidence=(
                                    f"Maven requires each dependency coordinate to be unique. "
                                    f"'{current_gid}:{current_aid}' is declared twice with scope '{curr_scope}'. "
                                    f"Maven will error: 'must be unique'. Remove the duplicate."
                                ),
                            ))
                        else:
                            local_findings.append(BuildFinding(
                                check="pom_conflicting_scope",
                                step="build", severity="MEDIUM", confidence=85,
                                title=f"Conflicting scopes for {current_gid}:{current_aid} ({prev_scope} + {curr_scope})",
                                detail=filepath,
                                evidence=(
                                    f"Same artifact declared with different scopes: "
                                    f"'{prev_scope}' and '{curr_scope}'. "
                                    f"Maven may use the wrong version at compile or test time."
                                ),
                            ))
                    else:
                        seen_coords[key] = {"scope": current_scope or "compile", "line": dep_start_line}
            elif in_dep:
                if "<groupId>" in s:
                    m = re.search(r'<groupId>(.+?)</groupId>', s)
                    if m: current_gid = m.group(1).strip()
                elif "<artifactId>" in s:
                    m = re.search(r'<artifactId>(.+?)</artifactId>', s)
                    if m: current_aid = m.group(1).strip()
                elif "<type>" in s:
                    m = re.search(r'<type>(.+?)</type>', s)
                    if m: current_type = m.group(1).strip()
                elif "<scope>" in s:
                    m = re.search(r'<scope>(.+?)</scope>', s)
                    if m: current_scope = m.group(1).strip()

        # ── Check 2: <parent> version missing or LATEST/RELEASE ──────────────
        in_parent = False
        parent_version = ""
        for line in lines:
            s = line.strip()
            if "<parent>" in s: in_parent = True
            elif "</parent>" in s:
                in_parent = False
                if not parent_version:
                    local_findings.append(BuildFinding(
                        check="pom_parent_version_missing",
                        step="build", severity="HIGH", confidence=98,
                        title=f"<parent> version missing in {filepath.split('/')[-1]}",
                        detail=filepath,
                        evidence=(
                            "Maven requires an explicit version in <parent>. "
                            "Without it, builds fail with 'Non-resolvable parent POM'. "
                            "Add <version>x.y.z</version> inside the <parent> block."
                        ),
                    ))
                elif parent_version.upper() in ("LATEST", "RELEASE"):
                    local_findings.append(BuildFinding(
                        check="pom_parent_version_unstable",
                        step="build", severity="HIGH", confidence=95,
                        title=f"<parent> uses unstable version '{parent_version}' in {filepath.split('/')[-1]}",
                        detail=filepath,
                        evidence=(
                            f"LATEST and RELEASE are deprecated Maven resolvers that fail on "
                            f"Cloud Manager's Maven settings. Use an explicit version number."
                        ),
                    ))
                parent_version = ""
            elif in_parent and "<version>" in s:
                m = re.search(r'<version>(.+?)</version>', s)
                if m: parent_version = m.group(1).strip()

        return local_findings

    # Scan changed pom.xml files from diff
    pom_files_in_diff = [f for f in changed_files if "pom.xml" in f.lower()]
    for filepath in pom_files_in_diff:
        # Try to read from repo
        source = ""
        if repo_dir:
            candidates = [
                _os.path.join(repo_dir, filepath),
            ]
            for c in candidates:
                if _os.path.isfile(c):
                    try:
                        source = open(c, encoding="utf-8", errors="ignore").read()
                        break
                    except Exception:
                        pass
        # Fallback: extract from diff
        if not source:
            lines = diff_text.splitlines()
            in_file = False
            content_lines = []
            for line in lines:
                if line.startswith("+++ b/") and filepath in line:
                    in_file = True
                    content_lines = []
                elif in_file:
                    if line.startswith("diff --git"):
                        break
                    if line.startswith("+") and not line.startswith("+++"):
                        content_lines.append(line[1:])
            if content_lines:
                source = "\n".join(content_lines)

        if source:
            findings.extend(_scan_pom(source, filepath))

    return findings


def _check_missing_symbol_references(
    diff_text: str,
    changed_files: List[str],
    repo_dir: str = "",
) -> List[BuildFinding]:
    """
    Detect "cannot find symbol" compilation errors from diff analysis.

    Scans added Java files for:
    1. Import of a class that doesn't exist in the diff OR repo
       → "cannot find symbol: class X" error
    2. Method call with wrong argument count vs interface definition in diff
       → "actual and formal argument lists differ in length" error
    3. Reference to a constant that was removed from an interface in the diff
       → "cannot find symbol: variable X" error

    Works best for subtree imports where the full file content is in the diff.
    For partial diffs (modifications), accuracy is lower.
    """
    findings: List[BuildFinding] = []
    import os as _os

    lines = diff_text.splitlines()

    # Build a set of all class names defined in this diff (new files)
    # Also build a set of all class names available in the repo
    classes_in_diff: set = set()
    constants_in_diff: set = set()  # interface.CONSTANT patterns
    method_sigs: dict = {}  # className.methodName → param_count

    # Pass 1: collect defined symbols from added files
    current_file = ""
    current_content = []
    file_contents: dict = {}

    for line in lines:
        if line.startswith("+++ b/"):
            if current_file and current_content:
                file_contents[current_file] = "\n".join(current_content)
            current_file = line[6:].strip()
            current_content = []
        elif current_file:
            if line.startswith("diff --git"):
                if current_content:
                    file_contents[current_file] = "\n".join(current_content)
                current_file = ""
                current_content = []
            elif line.startswith("+") and not line.startswith("+++"):
                current_content.append(line[1:])
    if current_file and current_content:
        file_contents[current_file] = "\n".join(current_content)

    # Extract class/interface/enum names defined in diff
    for filepath, content in file_contents.items():
        if not filepath.endswith(".java"):
            continue
        # Class/interface/enum name from filename
        classname = filepath.split("/")[-1].replace(".java", "")
        classes_in_diff.add(classname)

        # Extract interface constants (public static final or interface fields)
        interface_match = re.search(r'\binterface\s+(\w+)', content)
        if interface_match:
            iface_name = interface_match.group(1)
            # Find constants (UPPER_CASE identifiers)
            for const in re.findall(r'\b([A-Z][A-Z0-9_]{2,})\b', content):
                constants_in_diff.add(f"{iface_name}.{const}")

        # Extract method signatures from interfaces
        for method_match in re.finditer(
            r'(?:public\s+)?(?:\w+)\s+(\w+)\s*\(([^)]*)\)\s*(?:throws[^;{]+)?[;{]',
            content
        ):
            method_name = method_match.group(1)
            params = method_match.group(2).strip()
            param_count = len([p for p in params.split(",") if p.strip()]) if params else 0
            # Store for interface methods (no body)
            if method_match.group(0).endswith(";"):
                classname_for_method = filepath.split("/")[-1].replace(".java", "")
                method_sigs[f"{classname_for_method}.{method_name}"] = param_count

    # Also collect classes from repo
    classes_in_repo: set = set()
    if repo_dir:
        for root, _, files in _os.walk(repo_dir):
            for f in files:
                if f.endswith(".java"):
                    classes_in_repo.add(f.replace(".java", ""))

    # Pass 2: check for references to missing symbols
    for filepath, content in file_contents.items():
        if not filepath.endswith(".java"):
            continue
        filename = filepath.split("/")[-1]

        # Detect this file's root package (e.g. "com.idfcfirstbanklimited")
        _pkg_match = re.search(r'^\s*package\s+([\w.]+)\s*;', content, re.MULTILINE)
        _file_root_pkg = ""
        if _pkg_match:
            _parts = _pkg_match.group(1).split(".")
            _file_root_pkg = ".".join(_parts[:2]) if len(_parts) >= 2 else _parts[0]

        # Check 1: only flag imports from the SAME project package namespace.
        # Framework imports (org.apache.*, javax.*, java.*, com.day.*, com.adobe.*)
        # are available via AEM SDK and should never be flagged.
        # Only custom project code (same root package) can be "missing".
        for imp_match in re.finditer(r'^\s*import\s+([\w.]+)\.(\w+)\s*;', content, re.MULTILINE):
            import_pkg = imp_match.group(1)
            imported_class = imp_match.group(2)

            # Only check same-project imports (e.g. com.idfcfirstbanklimited.*)
            if not _file_root_pkg or not import_pkg.startswith(_file_root_pkg):
                continue

            if (imported_class not in classes_in_diff
                    and imported_class not in classes_in_repo):
                findings.append(BuildFinding(
                    check="missing_class_reference",
                    step="build", severity="HIGH", confidence=80,
                    title=f"{filename} imports '{imported_class}' — class not found in diff or repo",
                    detail=filepath,
                    evidence=(
                        f"'import ...{imported_class}' in {filename} but {imported_class}.java "
                        f"was not found in the added files or the local repository. "
                        f"This will cause 'cannot find symbol: class {imported_class}' at compile time. "
                        f"Ensure {imported_class}.java is included in the same package."
                    ),
                ))

        # Check 2: method calls with wrong argument count
        # Method signature mismatch check disabled — too many false positives from
        # overloaded methods, constructor calls, and inherited methods.
        # Only re-enable when we can scope it to specific interface methods in the diff.
        # TODO: re-enable with javalang AST for precise interface method matching

    return findings


def _check_test_infra_errors(diff_text: str, changed_files: List[str], repo_dir: str = "") -> List[BuildFinding]:
    """
    Detect test infrastructure errors that cause deterministic Surefire failures.

    Checks (all >95% accuracy):
    1. @InjectMocks on abstract class — Mockito can't instantiate abstract classes
    2. @InjectMocks field count vs @Mock field count mismatch — NPE likely
    3. JUnit 5 @Test without @ExtendWith — tests won't run / class not found
    4. OSGi Import-Package references package not in any pom.xml dependency
    5. ui.config OSGi config targets a class PID that doesn't exist in repo
    """
    findings: List[BuildFinding] = []
    import os as _os

    sections = _parse_diff_file_sections(diff_text)

    for filepath, section_lines in sections.items():
        fl = filepath.lower()
        if not fl.endswith(".java"):
            continue
        section_text = "\n".join(section_lines)
        added = "\n".join(l[1:] for l in section_lines if l.startswith("+") and not l.startswith("+++"))
        filename = filepath.split("/")[-1]
        is_test = "test" in fl or filename.endswith("Test.java")

        if is_test:
            # ── Check 1: @InjectMocks on abstract class ───────────────────────
            new_inject_mocks = re.findall(r'^\+\s*@InjectMocks\s*\n\s*\+?\s*\w.*?(\w+)\s+(\w+)\s*;', section_text, re.MULTILINE)
            for _, field_name in new_inject_mocks:
                # Try to find if the type is abstract in repo
                if repo_dir:
                    for root, _, files in _os.walk(repo_dir):
                        for f in files:
                            if f == f"{field_name}.java":
                                try:
                                    src = open(_os.path.join(root, f), encoding="utf-8", errors="ignore").read()
                                    if re.search(r'\bpublic\s+abstract\s+class\b|\babstract\s+public\s+class\b', src):
                                        findings.append(BuildFinding(
                                            check="injectmocks_abstract_class",
                                            step="build", severity="HIGH", confidence=97,
                                            title=f"{filename}: @InjectMocks on abstract class {field_name} — Mockito cannot instantiate",
                                            detail=filepath,
                                            evidence=(
                                                f"{field_name} is an abstract class. Mockito's @InjectMocks requires "
                                                f"a concrete instantiable class. This will throw "
                                                f"'Cannot instantiate abstract class' at test startup."
                                            ),
                                        ))
                                except Exception:
                                    pass

            # ── Check 2: JUnit 5 @Test without @ExtendWith ───────────────────
            has_junit5_test = bool(re.search(r'^\+\s*@Test\b', section_text, re.MULTILINE))
            has_extend_with = bool(re.search(r'@ExtendWith', section_text))
            has_runner      = bool(re.search(r'@RunWith', section_text))
            uses_mockito    = bool(re.search(r'@Mock\b|@InjectMocks|MockitoAnnotations', section_text))
            if has_junit5_test and uses_mockito and not has_extend_with and not has_runner:
                findings.append(BuildFinding(
                    check="junit5_missing_extend_with",
                    step="build", severity="HIGH", confidence=90,
                    title=f"{filename}: JUnit 5 @Test with Mockito but missing @ExtendWith(MockitoExtension.class)",
                    detail=filepath,
                    evidence=(
                        "@Mock and @InjectMocks require Mockito's JUnit 5 extension to initialise. "
                        "Without @ExtendWith(MockitoExtension.class), mocks are null and tests "
                        "fail with NullPointerException on first mock access."
                    ),
                ))

    # ── Check 3: OSGi Import-Package references package not in dependencies ──
    # Find pom.xml changes that add explicit Import-Package constraints
    for filepath, section_lines in sections.items():
        if "pom.xml" not in filepath.lower():
            continue
        section_text = "\n".join(section_lines)
        # Look for added Import-Package lines in bnd / maven-bundle-plugin config
        import_pkgs = re.findall(
            r'^\+.*<Import-Package>(.*?)</Import-Package>',
            section_text, re.MULTILINE | re.DOTALL
        )
        for pkg_list in import_pkgs:
            # Extract package names from the import list
            for pkg in re.split(r'[,\n]', pkg_list):
                pkg = pkg.strip().strip('"').strip("'")
                pkg_clean = re.sub(r';.*', '', pkg).strip()  # remove ;version=... etc
                if not pkg_clean or pkg_clean.startswith('!') or '*' in pkg_clean:
                    continue
                # Flag packages that look custom (not standard Java/OSGi) for manual review
                if not any(pkg_clean.startswith(p) for p in (
                    "java.", "javax.", "org.osgi.", "org.apache.", "com.adobe.",
                    "com.day.", "org.slf4j", "com.google", "com.fasterxml",
                )):
                    findings.append(BuildFinding(
                        check="osgi_import_custom_package",
                        step="build", severity="MEDIUM", confidence=70,
                        title=f"OSGi Import-Package adds custom package '{pkg_clean}' — verify it's in dependencies",
                        detail=filepath,
                        evidence=(
                            f"'{pkg_clean}' is added to Import-Package constraints. "
                            f"If this package isn't provided by any dependency in pom.xml, "
                            f"the OSGi bundle will fail to resolve at deploy time. "
                            f"Run `mvn dependency:tree | grep {pkg_clean.split('.')[-1]}` to verify."
                        ),
                    ))

    # ── Check 4: ui.config OSGi config targets non-existent PID ─────────────
    config_files = [f for f in changed_files if "ui.config" in f.lower() and f.endswith(".config") or
                    ("ui.config" in f.lower() and f.endswith(".xml") and "osgiconfig" in f.lower())]
    for filepath in config_files:
        # Extract PID from filename: com.example.MyService.config → com.example.MyService
        fname = filepath.split("/")[-1]
        pid_match = re.match(r'(.+?)(?:-\w+)?\.(?:config|xml)$', fname)
        if not pid_match:
            continue
        pid = pid_match.group(1)
        # Check if this class exists in the repo
        class_name = pid.split(".")[-1]
        if repo_dir and class_name:
            found = False
            for root, _, files in _os.walk(_os.path.join(repo_dir, "core") if _os.path.isdir(_os.path.join(repo_dir, "core")) else repo_dir):
                if f"{class_name}.java" in files:
                    found = True
                    break
            if not found and len(class_name) > 3 and not class_name.startswith("com."):
                findings.append(BuildFinding(
                    check="osgi_config_missing_pid",
                    step="build", severity="MEDIUM", confidence=75,
                    title=f"OSGi config targets '{class_name}' — class not found in core module",
                    detail=filepath,
                    evidence=(
                        f"The config file targets PID '{pid}' but '{class_name}.java' "
                        f"was not found in the core module. If the class was renamed or removed, "
                        f"this config will be silently ignored or cause activation warnings."
                    ),
                ))

    return findings


def _check_test_runtime_failures(
    diff_text: str,
    changed_files: List[str],
) -> List[BuildFinding]:
    """
    Detect patterns in test files that cause RUNTIME failures (not compile errors).

    Distinguishes between:
    - Test DISCOVERY failures: tests can't be found/configured (JUnit setup issues)
    - Test RUNTIME failures: tests run but throw exceptions (NPE, NoClassDefFound, etc.)

    The four most common runtime surefire failures in AEM projects:
    1. NullPointerException     — @InjectMocks missing a @Mock for a new dependency
    2. ExceptionInInitializerError — static initializer uses AEM/Sling APIs without a context
    3. NoClassDefFoundError     — dependency available at compile time but not test runtime
    4. ParameterResolutionException — JUnit 5 @ParameterizedTest missing extension/provider
    """
    findings: List[BuildFinding] = []
    if not diff_text:
        return findings

    sections = _parse_diff_file_sections(diff_text)

    for filepath, section_lines in sections.items():
        fl = filepath.lower()
        if not fl.endswith(".java"):
            continue

        section_text = "\n".join(section_lines)
        added = "\n".join(l[1:] for l in section_lines if l.startswith("+") and not l.startswith("+++"))
        filename = filepath.split("/")[-1]

        # ── Pattern 1: New @InjectMocks without corresponding new @Mock ──────────
        # When a service gets new dependencies (@Autowired/@Reference), the test
        # using @InjectMocks on that service needs a matching @Mock — otherwise NPE.
        is_test_file = "test" in fl or filename.endswith("Test.java")
        new_inject_mocks = re.findall(r'^\+\s*@InjectMocks', section_text, re.MULTILINE)
        new_mocks        = re.findall(r'^\+\s*@Mock\b',        section_text, re.MULTILINE)
        if is_test_file and new_inject_mocks and not new_mocks:
            findings.append(BuildFinding(
                check="injectmocks_without_mock",
                step="build",
                severity="MEDIUM",
                confidence=65,
                title=f"{filename}: @InjectMocks added but no @Mock fields — test may throw NullPointerException at runtime",
                detail=filepath,
                evidence=(
                    "@InjectMocks was added without corresponding @Mock fields. "
                    "Mockito will inject null for any unresolved dependency, causing NPE "
                    "when the service method accesses it. Add @Mock for each injected field."
                ),
            ))

        # ── Pattern 2: Static field referencing AEM/Sling context without @BeforeEach ──
        # Static initializers that use ResourceResolverFactory, SlingContext, etc.
        # fail with ExceptionInInitializerError if the AEM mock framework isn't set up.
        new_static_aem = re.findall(
            r'^\+\s*(?:private\s+|public\s+|protected\s+)?static\s+\S*(?:ResourceResolver|SlingContext|AemContext|Session|PageManager)\S*',
            section_text, re.MULTILINE
        )
        if new_static_aem:
            findings.append(BuildFinding(
                check="static_aem_field",
                step="build",
                severity="MEDIUM",
                confidence=58,
                title=f"{filename}: static AEM/Sling field — may cause ExceptionInInitializerError in tests",
                detail=filepath,
                evidence=(
                    "Static fields referencing AEM/Sling objects (ResourceResolver, SlingContext) "
                    "can fail with ExceptionInInitializerError if the AEM mock context "
                    "isn't initialized before the class is loaded. Use @BeforeEach instance fields instead."
                ),
            ))

        # ── Pattern 3: New test dependency added (possible NoClassDefFoundError) ──
        # When a test imports a class from a new/optional dependency, if that
        # dependency isn't in the test classpath at runtime → NoClassDefFoundError.
        new_test_imports = re.findall(
            r'^\+\s*import\s+(org\.mockito\.junit|io\.wcm|com\.day\.cq|org\.apache\.sling\.testing)',
            section_text, re.MULTILINE
        )
        if is_test_file and new_test_imports and len(new_test_imports) >= 2:
            findings.append(BuildFinding(
                check="new_test_framework_imports",
                step="build",
                severity="LOW",
                confidence=40,
                title=f"{filename}: new test framework imports — verify test dependencies in pom.xml",
                detail=filepath,
                evidence=(
                    f"{len(new_test_imports)} new test framework imports added. "
                    "If the corresponding test-scope dependency isn't in pom.xml, "
                    "the test will compile but fail at runtime with NoClassDefFoundError."
                ),
            ))

        # ── Pattern 3b: Existing verify() calls in unchanged test files ─────────
        # When a service method is renamed/removed, tests that call verify(mock).oldMethod()
        # will fail with "wanted but not invoked" — Mockito verification failure.
        # Detect: test file NOT in diff (not updated) but service it tests WAS changed.
        # We can only check test files that ARE in the diff here — if the test wasn't
        # updated and it had verify() calls, the service change may break them.
        if is_test_file:
            new_verify = re.findall(r'^\+\s*verify\s*\(', section_text, re.MULTILINE)
            removed_verify = re.findall(r'^-\s*verify\s*\(', section_text, re.MULTILINE)
            if removed_verify and not new_verify:
                findings.append(BuildFinding(
                    check="mockito_verify_removed",
                    step="build",
                    severity="MEDIUM",
                    confidence=65,
                    title=f"{filename}: verify() assertions removed — may indicate service behaviour changed",
                    detail=filepath,
                    evidence=(
                        f"{len(removed_verify)} Mockito verify() call(s) removed from test. "
                        "If the service method being verified was also renamed or removed, "
                        "other tests that still call verify() on it will fail with "
                        "'wanted but not invoked'."
                    ),
                ))

        # ── Pattern 4: @ParameterizedTest without @MethodSource or @ValueSource ──
        new_parameterized = re.findall(r'^\+\s*@ParameterizedTest', section_text, re.MULTILINE)
        has_source = bool(re.search(r'@(?:MethodSource|ValueSource|CsvSource|EnumSource)', section_text))
        if new_parameterized and not has_source:
            findings.append(BuildFinding(
                check="parameterized_without_source",
                step="build",
                severity="MEDIUM",
                confidence=70,
                title=f"{filename}: @ParameterizedTest without @MethodSource — ParameterResolutionException at runtime",
                detail=filepath,
                evidence=(
                    "@ParameterizedTest requires a source annotation (@MethodSource, @ValueSource, etc.). "
                    "Without one, JUnit 5 throws ParameterResolutionException and the test fails at runtime, "
                    "not at compile time — Maven will report surefire failure."
                ),
            ))

    return findings


def _check_dispatcher_changes(signals: DiffSignals) -> List[BuildFinding]:
    """
    Dispatcher config changes — flag both deploy AND securityTest risk.

    In Cloud Manager:
    - Deploy step: dispatcher config is APPLIED to the dispatcher instances.
      A malformed .vhost (wrong ServerName, missing DocumentRoot, bad env-var ref)
      causes deploy to fail before securityTest even runs.
    - Security Testing: CQ Dispatcher Configuration check validates the config.

    Previously only flagged securityTest — this missed deploy failures from
    dispatcher changes, which are more common and more severe.
    """
    findings = []
    if not signals.dispatcher_changed:
        return findings
    for f in signals.changed_files:
        fl = f.lower()
        fname = f.split("/")[-1]
        if ".vhost" in fl:
            # vhost changes → primarily a DEPLOY risk (applied during deploy step)
            findings.append(BuildFinding(
                check="dispatcher_vhost_change", step="deploy", severity="MEDIUM", confidence=70,
                title=f"Dispatcher vhost changed: {fname}",
                detail=f,
                evidence=(
                    f"'{fname}' is applied during the Cloud Manager deploy step. "
                    f"A malformed ServerName, missing DocumentRoot, or bad env-var reference "
                    f"(e.g. unset LTS_PUBLISH_DEFAULT_HOSTNAME) causes deploy to fail. "
                    f"Validate vhost syntax locally and confirm all referenced env vars are "
                    f"configured in Cloud Manager before triggering."
                )
            ))
        elif any(ext in fl for ext in [".any", ".farm", ".rules"]):
            # .any/.farm → securityTest (Dispatcher Optimizer) + possible deploy risk
            findings.append(BuildFinding(
                check="dispatcher_config_change", step="deploy", severity="MEDIUM", confidence=65,
                title=f"Dispatcher config changed: {fname}",
                detail=f,
                evidence=(
                    f"Dispatcher filter/cache/rewrite rules in '{fname}' are validated by "
                    f"Cloud Manager's Dispatcher Optimizer at codeQuality and applied at deploy. "
                    f"Syntax errors or invalid directives can fail either step."
                )
            ))
    return findings


def _check_uiapps_deploy_risks(diff_text: str, changed_files: List[str]) -> List[BuildFinding]:
    """
    Detect ui.apps and ui.frontend changes that cause DEPLOY failures, not build failures.

    In AEM, the deploy step installs JCR content packages. If a package has:
    - A .content.xml missing jcr:primaryType → package install fails
    - A component node with wrong node type → package validation fails
    - A clientlib referencing a file that webpack didn't produce → runtime 404s

    These all pass build (maven compile succeeds) but fail at deploy.

    Checks:
    1. .content.xml in jcr_root/apps/ missing jcr:primaryType → deploy HIGH
    2. ui.apps changed + ui.frontend changed in same commit → possible clientlib gap
    3. HTL component changes without matching .content.xml → possible missing node
    """
    findings = []

    uiapps_files  = [f for f in changed_files if "ui.apps" in f.lower() and "jcr_root" in f.lower()]
    uifrontend    = any("ui.frontend" in f.lower() for f in changed_files)
    htl_changed   = [f for f in changed_files if f.endswith(".html") and "jcr_root/apps" in f.lower()]
    content_xmls  = [f for f in changed_files if f.endswith(".content.xml") and "jcr_root" in f.lower()]

    # ── Check 1: .content.xml missing jcr:primaryType ────────────────────────
    lines = diff_text.splitlines()
    for filepath in content_xmls:
        # Find added lines for this file in the diff
        in_file = False
        file_lines = []
        for line in lines:
            if line.startswith("+++ b/") and filepath in line:
                in_file = True
                file_lines = []
            elif in_file:
                if line.startswith("diff --git"):
                    break
                if line.startswith("+") and not line.startswith("+++"):
                    file_lines.append(line[1:])

        if file_lines:
            content = "\n".join(file_lines)
            has_primary_type = "jcr:primaryType" in content
            has_jcr_root_tag = "<jcr:root" in content

            if has_jcr_root_tag and not has_primary_type:
                findings.append(BuildFinding(
                    check="content_xml_missing_primary_type",
                    step="deploy", severity="HIGH", confidence=90,
                    title=f"Missing jcr:primaryType in {filepath.split('/')[-2]}/{filepath.split('/')[-1]}",
                    detail=filepath,
                    evidence=(
                        "AEM content packages require jcr:primaryType on every node. "
                        "Without it, the package installer rejects the node and the "
                        "deploy step fails with 'InvalidItemStateException' or 'ConstraintViolationException'. "
                        "Add jcr:primaryType=\"cq:Component\" (or correct type) to the <jcr:root> tag."
                    ),
                ))

    # ── Check 2: HTL changed without .content.xml → possible missing component node ──
    for htl_file in htl_changed:
        # Find the component directory: path up to the .html file
        parts = htl_file.split("/")
        component_dir = "/".join(parts[:-1])
        # Check if there's a matching .content.xml for this component
        has_content_xml = any(
            f.startswith(component_dir) and f.endswith(".content.xml")
            for f in changed_files
        )
        if not has_content_xml and uiapps_files:
            findings.append(BuildFinding(
                check="htl_without_content_xml",
                step="deploy", severity="MEDIUM", confidence=65,
                title=f"HTL component changed without .content.xml update: {parts[-1]}",
                detail=htl_file,
                evidence=(
                    f"'{parts[-1]}' changed but no matching .content.xml in same commit. "
                    f"If this is a new component or the node structure changed, "
                    f"the JCR package may fail to install at deploy. "
                    f"Verify the component definition in jcr_root/apps/ is complete."
                ),
            ))

    # ── Check 3: ui.frontend + ui.apps both changed → clientlib dependency risk ──
    if uifrontend and htl_changed:
        findings.append(BuildFinding(
            check="frontend_uiapps_clientlib_gap",
            step="deploy", severity="LOW", confidence=45,
            title="ui.frontend and ui.apps HTL changed together — verify clientlib references",
            detail="ui.frontend + ui.apps/jcr_root",
            evidence=(
                "When ui.frontend JS/CSS changes alongside HTL component changes, "
                "new clientlib categories or file references in HTL must match what "
                "webpack produces. A mismatch causes 404s or missing styles at runtime "
                "even though build succeeds. Run `npm run build` locally and verify "
                "the expected clientlib output files are present."
            ),
        ))

    return findings


def _check_osgi_config_changes(signals: DiffSignals) -> List[BuildFinding]:
    """ui.config OSGi runmode changes — can cause config not to apply on correct env."""
    findings = []
    for f in signals.changed_files:
        fl = f.lower()
        if "ui.config" in fl and fl.endswith(".cfg"):
            # Runmode mismatch: config file in wrong runmode folder
            parts = f.split("/")
            for part in parts:
                if "." in part and any(mode in part for mode in ["author", "publish", "prod", "stage", "dev"]):
                    findings.append(BuildFinding(
                        check="osgi_runmode_config", step="deploy", severity="MEDIUM", confidence=60,
                        title=f"OSGi config with runmode: {part}",
                        detail=f,
                        evidence="Runmode-specific OSGi configs may not apply on target environment if runmode doesn't match"
                    ))
                    break
    return findings


def _check_package_lock_drift(signals: DiffSignals) -> List[BuildFinding]:
    """package-lock.json changed without package.json — indicates manual edit or drift."""
    findings = []
    has_lock   = any("package-lock.json" in f for f in signals.changed_files)
    has_pkg    = any(f.endswith("package.json") and "package-lock" not in f and "node_modules" not in f for f in signals.changed_files)
    if has_lock and not has_pkg:
        findings.append(BuildFinding(
            check="package_lock_drift", step="build", severity="MEDIUM", confidence=60,
            title="package-lock.json changed without package.json",
            detail="ui.frontend",
            evidence="Manual package-lock.json edits without package.json change can cause npm install to fail or install unexpected versions"
        ))
    return findings


_DAO_MIGRATION_TITLE_RE = re.compile(
    r"\b(dao|lts|repository|data.?access|migration)\b", re.IGNORECASE
)
_DAO_PATH_RE = re.compile(
    r"\b(dao|repository|dataaccess|data-access|persistence)\b", re.IGNORECASE
)


def _has_core_production_java(diff_text: str, changed_files: List[str]) -> bool:
    """True when non-test Java under core/ has substantive changes."""
    sections = _parse_diff_file_sections(diff_text)
    for filepath, lines in sections.items():
        fl = filepath.lower()
        if not fl.endswith(".java"):
            continue
        if "test" in fl or filepath.split("/")[-1].endswith("Test.java"):
            continue
        if "/core/" in fl or fl.startswith("core/"):
            if _section_has_substantive_java_changes(lines):
                return True
    for f in changed_files or []:
        fl = f.lower()
        if fl.endswith(".java") and "/core/" in fl and "test" not in fl:
            return True
    return False


def _check_build_antipatterns(diff_text: str, changed_files: List[str], repo_dir: str = "") -> List[BuildFinding]:
    """
    Detect common AEM/Maven build anti-patterns that cause deterministic failures.

    Checks:
    1. skipTests / surefire skip in pom.xml (HIGH/92%)
    2. HTL/HTML data-sly-* syntax errors (HIGH/88%)
    3. bnd.bnd Export-Package/Import-Package namespace mismatch (MEDIUM/75%)
    4. Cross-module version skew in pom.xml (HIGH/88%)
    5. Dispatcher .any file invalid rule syntax (MEDIUM/75%)
    6. New OSGi config without matching runmode folder (MEDIUM/72%)
    7. AEM SDK / uber-jar version bump (MEDIUM/65%)
    8. Content-package type mismatch in ui.apps/ui.content (MEDIUM/70%)
    """
    import re as _re
    import os as _os

    findings: List[BuildFinding] = []
    sections = _parse_diff_file_sections(diff_text)

    # ── Check 1: skipTests or surefire skip in pom.xml ──────────────────────
    _skip_patterns = [
        r"<skipTests>\s*true\s*</skipTests>",
        r"<maven\.test\.skip>\s*true\s*</maven\.test\.skip>",
    ]
    # <skip>true</skip> only inside a surefire block — collect context lines
    for filepath, section_lines in sections.items():
        if "pom.xml" not in filepath.lower():
            continue
        added_lines = [l[1:] for l in section_lines if l.startswith("+") and not l.startswith("+++")]
        added_text = "\n".join(added_lines)
        for pat in _skip_patterns:
            if _re.search(pat, added_text, _re.IGNORECASE):
                findings.append(BuildFinding(
                    check="build_antipatterns",
                    step="maven-test",
                    severity="HIGH",
                    confidence=0.92,
                    title="Test execution bypassed via skipTests in pom.xml",
                    detail=(
                        f"{filepath} adds a Maven property that skips all test execution. "
                        "This will cause the CI gate to pass without running any tests, "
                        "masking regressions and breaking the deploy safety net."
                    ),
                    evidence=[m for m in _re.findall(pat, added_text, _re.IGNORECASE)],
                ))
                break
        # <skip>true</skip> inside a surefire plugin block
        if "<skip>true</skip>" in added_text.lower():
            # Confirm it is inside a surefire context
            full_added = added_text.lower()
            surefire_idx = full_added.find("maven-surefire-plugin")
            skip_idx = full_added.find("<skip>true</skip>")
            if surefire_idx != -1 and skip_idx > surefire_idx:
                findings.append(BuildFinding(
                    check="build_antipatterns",
                    step="maven-test",
                    severity="HIGH",
                    confidence=0.92,
                    title="Surefire plugin skip=true found in pom.xml",
                    detail=(
                        f"{filepath} enables <skip>true</skip> inside the maven-surefire-plugin "
                        "configuration. All unit tests will be skipped during the build."
                    ),
                    evidence=["<skip>true</skip> inside maven-surefire-plugin block"],
                ))

    # ── Check 2: HTL/HTML data-sly-* syntax errors ──────────────────────────
    for filepath, section_lines in sections.items():
        fl = filepath.lower()
        if not (fl.endswith(".html") or fl.endswith(".htm")):
            continue
        added_lines = [l[1:] for l in section_lines if l.startswith("+") and not l.startswith("+++")]
        for lineno, line in enumerate(added_lines, 1):
            # Unclosed ${...} — odd number of unmatched braces
            if "${" in line:
                open_count = line.count("{")
                close_count = line.count("}")
                if open_count != close_count:
                    findings.append(BuildFinding(
                        check="build_antipatterns",
                        step="htl-compile",
                        severity="HIGH",
                        confidence=0.88,
                        title="Unclosed HTL expression ${...} in HTML file",
                        detail=(
                            f"{filepath} line ~{lineno} has a `${{...}}` expression with unmatched "
                            "braces. AEM's HTL compiler will reject this file at build time."
                        ),
                        evidence=[line.strip()[:120]],
                    ))
                    break
            # data-sly-* attribute missing closing ="
            dsly_match = _re.search(r'data-sly-\w+(?!=)', line)
            if dsly_match:
                findings.append(BuildFinding(
                    check="build_antipatterns",
                    step="htl-compile",
                    severity="HIGH",
                    confidence=0.88,
                    title="data-sly-* attribute missing assignment in HTML file",
                    detail=(
                        f"{filepath} line ~{lineno}: `{dsly_match.group()}` appears without an "
                        '`="..."` assignment. HTL requires `data-sly-attribute="value"` syntax.'
                    ),
                    evidence=[line.strip()[:120]],
                ))
                break
            # data-sly-use referencing a class name without a dot (likely typo)
            use_match = _re.search(r'data-sly-use\.\w+="([^"]+)"', line)
            if use_match:
                ref_val = use_match.group(1)
                if "." not in ref_val and "/" not in ref_val:
                    findings.append(BuildFinding(
                        check="build_antipatterns",
                        step="htl-compile",
                        severity="HIGH",
                        confidence=0.88,
                        title="data-sly-use references class without package path",
                        detail=(
                            f"{filepath} line ~{lineno}: `data-sly-use` value `{ref_val}` has no "
                            "dot-separated package or slash-separated path. HTL cannot resolve bare "
                            "class names — use the fully qualified class name or a relative script path."
                        ),
                        evidence=[line.strip()[:120]],
                    ))
                    break

    # ── Check 3: bnd.bnd Export-Package/Import-Package namespace mismatch ───
    for filepath, section_lines in sections.items():
        if not filepath.lower().endswith("bnd.bnd"):
            continue
        added_lines = [l[1:] for l in section_lines if l.startswith("+") and not l.startswith("+++")]
        added_text = "\n".join(added_lines)
        export_match = _re.search(r"Export-Package\s*[:=]\s*(.+)", added_text)
        import_match = _re.search(r"Import-Package\s*[:=]\s*(.+)", added_text)
        if export_match and import_match:
            export_val = export_match.group(1)
            import_val = import_match.group(1)
            # Flag javax vs jakarta namespace conflict
            has_javax_export = "javax." in export_val
            has_jakarta_export = "jakarta." in export_val
            has_javax_import = "javax." in import_val
            has_jakarta_import = "jakarta." in import_val
            if (has_javax_export and has_jakarta_import) or (has_jakarta_export and has_javax_import):
                findings.append(BuildFinding(
                    check="build_antipatterns",
                    step="osgi-bundle",
                    severity="MEDIUM",
                    confidence=0.75,
                    title="bnd.bnd javax/jakarta namespace mismatch between Export and Import",
                    detail=(
                        f"{filepath} mixes `javax.*` and `jakarta.*` namespaces across "
                        "Export-Package and Import-Package. This causes OSGi wiring failures at "
                        "bundle activation time."
                    ),
                    evidence=[
                        f"Export-Package: {export_val[:80]}",
                        f"Import-Package: {import_val[:80]}",
                    ],
                ))

    # ── Check 4: Cross-module version skew in pom.xml ───────────────────────
    _version_changed: list = []
    _non_property_version: list = []
    for filepath, section_lines in sections.items():
        if "pom.xml" not in filepath.lower():
            continue
        added_lines = [l[1:] for l in section_lines if l.startswith("+") and not l.startswith("+++")]
        for line in added_lines:
            stripped = line.strip()
            ver_match = _re.match(r"<version>(.+?)</version>", stripped)
            if ver_match:
                ver_val = ver_match.group(1)
                if _re.match(r"\d+\.\d+", ver_val):
                    _version_changed.append((filepath, ver_val))
                if not ver_val.startswith("${"):
                    _non_property_version.append((filepath, ver_val))
    # If both a numeric version bump AND a hard-coded (non-property) version exist across different files
    if _version_changed and _non_property_version:
        files_bumped = {fp for fp, _ in _version_changed}
        files_hardcoded = {fp for fp, _ in _non_property_version}
        if files_bumped != files_hardcoded or len(files_bumped) > 1:
            findings.append(BuildFinding(
                check="build_antipatterns",
                step="maven-build",
                severity="HIGH",
                confidence=0.88,
                title="Cross-module version skew detected in pom.xml changes",
                detail=(
                    "A <version> was changed in one pom.xml while another module references a "
                    "hard-coded version string not using ${project.version}. The version bump "
                    "may not have been propagated to all child modules, causing reactor build failures."
                ),
                evidence=[f"{fp}: {v}" for fp, v in (_version_changed + _non_property_version)[:6]],
            ))

    # ── Check 5: Dispatcher .any file invalid rule syntax ───────────────────
    for filepath, section_lines in sections.items():
        if not filepath.lower().endswith(".any"):
            continue
        added_lines = [l[1:] for l in section_lines if l.startswith("+") and not l.startswith("+++")]
        brace_depth = 0
        suspicious: list = []
        for lineno, line in enumerate(added_lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            brace_depth += stripped.count("{") - stripped.count("}")
            # Lines that are not comments, not a brace, not starting with /
            if (
                not stripped.startswith("/")
                and "{" not in stripped
                and "}" not in stripped
                and not stripped.startswith('"')
                and not stripped.startswith("'")
            ):
                suspicious.append(f"line ~{lineno}: {stripped[:80]}")
        if brace_depth != 0 or suspicious:
            findings.append(BuildFinding(
                check="build_antipatterns",
                step="dispatcher-config",
                severity="MEDIUM",
                confidence=0.75,
                title="Dispatcher .any file has potential syntax error",
                detail=(
                    f"{filepath} may have unmatched braces (net depth={brace_depth}) or lines "
                    "that do not match valid Dispatcher rule syntax. Invalid .any files cause "
                    "Dispatcher to refuse to load the configuration."
                ),
                evidence=(
                    [f"Unmatched brace depth: {brace_depth}"] if brace_depth != 0 else []
                ) + suspicious[:3],
            ))

    # ── Check 6: New OSGi config without matching runmode folder ─────────────
    _known_runmodes = {
        "config", "config.author", "config.publish",
        "config.prod", "config.dev", "config.stage",
        "config.author.prod", "config.author.dev",
        "config.publish.prod", "config.publish.dev",
    }
    for filepath in changed_files:
        fl = filepath.lower()
        if not (fl.endswith(".config") or fl.endswith(".xml")):
            continue
        if "osgiconfig" not in fl and "osgi-config" not in fl:
            continue
        parts = filepath.replace("\\", "/").split("/")
        # Find the folder directly containing the config file
        if len(parts) >= 2:
            parent_folder = parts[-2]
            if parent_folder not in _known_runmodes:
                findings.append(BuildFinding(
                    check="build_antipatterns",
                    step="osgi-config",
                    severity="MEDIUM",
                    confidence=0.72,
                    title=f"OSGi config in unrecognised runmode folder '{parent_folder}'",
                    detail=(
                        f"{filepath} is placed in folder `{parent_folder}` which is not a "
                        "recognised AEM runmode folder. The configuration will never be applied "
                        "in any environment. Expected folders: config, config.author, "
                        "config.publish, config.prod, config.dev, etc."
                    ),
                    evidence=[f"Parent folder: {parent_folder}"],
                ))

    # ── Check 7: AEM SDK / uber-jar version bump ─────────────────────────────
    _sdk_artifacts = [
        "com.adobe.aem:aem-sdk-api",
        "com.adobe.cq:uber-jar",
    ]
    for filepath, section_lines in sections.items():
        if "pom.xml" not in filepath.lower():
            continue
        added_lines = [l[1:] for l in section_lines if l.startswith("+") and not l.startswith("+++")]
        added_text = "\n".join(added_lines)
        for artifact in _sdk_artifacts:
            group_id, artifact_id = artifact.split(":")
            if group_id in added_text and artifact_id in added_text:
                # Look for a <version> tag nearby
                if _re.search(r"<version>[^<]+</version>", added_text):
                    findings.append(BuildFinding(
                        check="build_antipatterns",
                        step="maven-build",
                        severity="MEDIUM",
                        confidence=0.65,
                        title=f"AEM SDK/uber-jar version bumped: {artifact_id}",
                        detail=(
                            f"{filepath} changes the version of `{artifact}`. This may break "
                            "build compatibility if the new SDK version introduces API changes, "
                            "and can cause loadTest regressions if the AEM runtime behaviour changes."
                        ),
                        evidence=[f"Artifact: {artifact} — version changed in {filepath}"],
                    ))
                    break

    # ── Check 8: Content-package type mismatch ───────────────────────────────
    for filepath, section_lines in sections.items():
        if "pom.xml" not in filepath.lower():
            continue
        # Only look at ui.apps modules
        fp_lower = filepath.lower()
        if "ui.apps" not in fp_lower and "ui.content" not in fp_lower:
            continue
        added_lines = [l[1:] for l in section_lines if l.startswith("+") and not l.startswith("+++")]
        added_text = "\n".join(added_lines)
        pkg_type_match = _re.search(r"<packageType>([^<]+)</packageType>", added_text, _re.IGNORECASE)
        if pkg_type_match:
            pkg_type = pkg_type_match.group(1).strip().lower()
            if "ui.apps" in fp_lower and pkg_type == "content":
                findings.append(BuildFinding(
                    check="build_antipatterns",
                    step="content-package",
                    severity="MEDIUM",
                    confidence=0.70,
                    title="ui.apps module sets packageType=content (should be application)",
                    detail=(
                        f"{filepath} sets `<packageType>content</packageType>` for a `ui.apps` "
                        "module. AEM Cloud Service's package validator requires ui.apps modules to "
                        "use `application` package type. This will fail the package validation step."
                    ),
                    evidence=[f"<packageType>{pkg_type_match.group(1)}</packageType> in {filepath}"],
                ))
            elif "ui.content" in fp_lower and pkg_type == "application":
                findings.append(BuildFinding(
                    check="build_antipatterns",
                    step="content-package",
                    severity="MEDIUM",
                    confidence=0.70,
                    title="ui.content module sets packageType=application (should be content)",
                    detail=(
                        f"{filepath} sets `<packageType>application</packageType>` for a `ui.content` "
                        "module. AEM Cloud Service's package validator requires ui.content modules to "
                        "use `content` package type. This will fail the package validation step."
                    ),
                    evidence=[f"<packageType>{pkg_type_match.group(1)}</packageType> in {filepath}"],
                ))

    return findings


def _check_dao_migration_perf_risk(
    diff_text: str,
    changed_files: List[str],
    commit_title: str,
    java_upgrade_pending: bool = False,
) -> List[BuildFinding]:
    """
    DAO/LTS/repository migrations change runtime query latency — build may pass
    while Cloud Manager performance testing (loadTest) fails on KPI thresholds.
    """
    findings: List[BuildFinding] = []
    title = commit_title or ""
    migration_signal = (
        bool(_DAO_MIGRATION_TITLE_RE.search(title))
        or any(_DAO_PATH_RE.search(f or "") for f in (changed_files or []))
        or bool(_DAO_PATH_RE.search(diff_text or ""))
    )
    has_core_java = _has_core_production_java(diff_text, changed_files)

    if migration_signal and has_core_java:
        conf = 55 if java_upgrade_pending else 48
        findings.append(BuildFinding(
            check="dao_migration_perf_risk",
            step="loadTest",
            severity="MEDIUM",
            confidence=conf,
            title="DAO/LTS migration changes core data-access layer",
            detail=title[:100] if _DAO_MIGRATION_TITLE_RE.search(title) else "core Java data-access files changed",
            evidence=(
                "DAO/repository migrations alter query paths and response latency — "
                "performance KPIs may regress at loadTest even when build and unit tests pass"
            ),
        ))
        return findings

    if java_upgrade_pending and has_core_java:
        findings.append(BuildFinding(
            check="java_upgrade_perf_risk",
            step="loadTest",
            severity="LOW",
            confidence=40,
            title="Target environment upgrading Java version with core code changes",
            detail="Cloud Manager validation noted a pending Java version upgrade",
            evidence=(
                "JDK upgrades can shift JVM GC and response-time characteristics — "
                "review loadTest KPI margins when core Java changed"
            ),
        ))
    return findings


def _check_reactor_version_bump(signals: DiffSignals) -> List[BuildFinding]:
    """Reactor pom.xml version bump — can break module resolution if partial."""
    findings = []
    for dep in signals.maven_deps_added:
        # If version changed but no code files changed — likely a reactor version bump
        if dep.version and not signals.has_java_change and not signals.has_npm_change:
            findings.append(BuildFinding(
                check="reactor_version_bump", step="build", severity="LOW", confidence=45,
                title=f"Reactor version bump only: {dep.artifact_id} → {dep.version}",
                detail=f"{dep.group_id}:{dep.artifact_id}:{dep.version}",
                evidence="Version-only pom.xml changes without code changes — verify all modules align to new version"
            ))
    return findings


# ── Uncaught checked exception check ─────────────────────────────────────────

# Sling/AEM API methods that throw checked exceptions developers forget to handle.
# Key: method name fragment. Value: (exception class, import hint)
_SLING_CHECKED_EXCEPTIONS: Dict[str, tuple] = {
    "getServiceResourceResolver":        ("LoginException",        "org.apache.sling.api.resource.LoginException"),
    "getAdministrativeResourceResolver": ("LoginException",        "org.apache.sling.api.resource.LoginException"),
    "getResourceResolver":               ("LoginException",        "org.apache.sling.api.resource.LoginException"),
    "getNode":                           ("RepositoryException",   "javax.jcr.RepositoryException"),
    "getProperty":                       ("RepositoryException",   "javax.jcr.RepositoryException"),
    "getSession":                        ("RepositoryException",   "javax.jcr.RepositoryException"),
    "checkin":                           ("RepositoryException",   "javax.jcr.RepositoryException"),
    "checkout":                          ("RepositoryException",   "javax.jcr.RepositoryException"),
    "save":                              ("RepositoryException",   "javax.jcr.RepositoryException"),
    "getItem":                           ("RepositoryException",   "javax.jcr.RepositoryException"),
}

_TRY_PATTERN     = re.compile(r'\btry\s*\{')
_CATCH_PATTERN   = re.compile(r'\bcatch\s*\(')
_THROWS_PATTERN  = re.compile(r'\bthrows\b')
_METHOD_PATTERN  = re.compile(r'(public|private|protected)\s+\S+\s+\w+\s*\(')


def _is_in_try_catch(lines: List[str], target_idx: int, exception: str) -> bool:
    """
    Pass 1 (regex): check if the call at target_idx is inside a try block.
    Scans ±40 lines (was ±20 — widened to catch try-catch at top of longer methods).
    """
    window = lines[max(0, target_idx - 40): target_idx + 40]
    text = "\n".join(window)
    if not _TRY_PATTERN.search(text):
        return False
    # Check for catch of this specific exception, its parent, or bare Exception/Throwable
    catch_patterns = [exception, exception.split(".")[-1], "Exception", "Throwable"]
    return any(f"catch ({p}" in text or f"catch({p}" in text for p in catch_patterns)


def _method_declares_throws(lines: List[str], target_idx: int, exception: str) -> bool:
    """
    Check if the enclosing method declaration declares the exception in its throws clause.
    Walks backwards up to 80 lines to find the method signature (handles long methods).
    """
    exc_short = exception.split(".")[-1]
    for i in range(target_idx, max(0, target_idx - 80), -1):
        line = lines[i]
        if _METHOD_PATTERN.search(line):
            # Collect the full method signature (may span multiple lines)
            sig_lines = []
            for j in range(i, min(len(lines), i + 5)):
                sig_lines.append(lines[j])
                if "{" in lines[j]:
                    break
            combined = " ".join(sig_lines)
            if _THROWS_PATTERN.search(combined):
                # Method declares throws — check for this exception or a parent
                if (exc_short in combined or exception in combined
                        or "Exception" in combined or "Throwable" in combined):
                    return True
            return False  # found method but no throws
    return False


def _check_uncaught_sling_exceptions(
    diff_text: str,
    changed_files: List[str],
    repo_dir: str = "",
) -> List[BuildFinding]:
    """
    Two-pass check for uncaught Sling/AEM checked exceptions.

    Pass 1 — Regex (fast, ~0ms):
        Scan added lines in the diff for known risky method calls.
        Check ±20 lines of context for try-catch or throws declaration.
        If suspicious: flag as MEDIUM confidence.

    Pass 2 — javalang AST (accurate, ~0.3s per file):
        For each file flagged in Pass 1, read the full Java source
        from the local git repo and parse the AST.
        Confirm the call site genuinely lacks exception handling.
        Upgrade to HIGH confidence if confirmed.

    Returns empty list if javalang is unavailable (graceful degradation).
    """
    findings: List[BuildFinding] = []

    # Only applies to Java files
    java_changed = [f for f in changed_files if f.endswith(".java")]
    if not java_changed:
        return findings

    # ── Pass 1: Regex scan of diff ────────────────────────────────────────────
    lines = diff_text.splitlines()
    added_lines = [(i, l[1:]) for i, l in enumerate(lines) if l.startswith("+") and not l.startswith("+++")]

    # Track which files have suspicious calls for Pass 2
    suspicious: Dict[str, List[tuple]] = {}  # filepath → [(method, exception, line_idx)]

    for idx, (line_no, line) in enumerate(added_lines):
        for method, (exception, full_exc) in _SLING_CHECKED_EXCEPTIONS.items():
            if f"{method}(" not in line:
                continue
            # Reconstruct surrounding context from full lines list
            context_lines = [l[1:] if l.startswith("+") else l for l in lines]
            if _is_in_try_catch(context_lines, line_no, exception):
                continue
            if _method_declares_throws(context_lines, line_no, exception):
                continue

            # Find which file this line belongs to
            filepath = ""
            for fl in lines[:line_no]:
                if fl.startswith("+++ b/"):
                    filepath = fl[6:].strip()
            if not filepath:
                filepath = java_changed[0] if java_changed else "unknown"

            suspicious.setdefault(filepath, []).append((method, exception, full_exc, line_no))

    if not suspicious:
        return findings

    # ── Pass 2: javalang AST confirmation ────────────────────────────────────
    _javalang_available = False
    try:
        import javalang as _jl
        _javalang_available = True
    except ImportError:
        pass

    for filepath, calls in suspicious.items():
        for method, exception, full_exc, line_no in calls:
            confirmed = False
            ast_used  = False

            if _javalang_available and repo_dir:
                import os as _os
                # Try to find the file in the local repo
                candidates = [
                    _os.path.join(repo_dir, filepath),
                    _os.path.join(repo_dir, "idfc-ams", filepath),
                ]
                for candidate in candidates:
                    if not _os.path.isfile(candidate):
                        continue
                    try:
                        source = open(candidate, encoding="utf-8", errors="ignore").read()
                        tree   = _jl.parse.parse(source)
                        # Find all method declarations in the file
                        for _, node in tree.filter(_jl.tree.MethodDeclaration):
                            # Check if any statement in this method calls our method
                            method_source = source.split("\n")
                            # Find line numbers of this method (approximate via name search)
                            call_in_method = any(
                                f"{method}(" in l for l in method_source
                                if method_source.index(l) >= (node.position.line - 1)
                                if abs(method_source.index(l) - (node.position.line - 1)) < 100
                            ) if hasattr(node, "position") and node.position else False

                            if not call_in_method:
                                continue

                            # Check throws declaration on this method
                            throws_it = any(
                                exception in str(t) or full_exc in str(t)
                                for t in (node.throws or [])
                            )
                            if throws_it:
                                break  # properly declared — not a problem

                            # Check for try-catch enclosing the call
                            has_try = any(
                                isinstance(stmt, _jl.tree.TryStatement)
                                for stmt in (node.body or [])
                            ) if node.body else False

                            if not has_try:
                                confirmed = True
                                ast_used  = True
                        break
                    except Exception:
                        break  # javalang parse failure — fall back to regex result

            # Confidence and severity depend on confirmation method:
            #
            # javalang AST confirmed → HIGH/85%: near-certain compile failure
            # regex only (no javalang or no repo) → LOW/45%: advisory only
            #   Regex has a high false positive rate for long methods — the try-catch
            #   may be outside the scan window. Do NOT emit MEDIUM/HIGH from regex alone.
            #   If javalang is available and didn't confirm → skip entirely (false positive)
            #
            if _javalang_available and repo_dir and not confirmed:
                # javalang was available, checked the file, and did NOT confirm the issue.
                # The method likely has proper exception handling. Skip — false positive.
                continue

            if confirmed and ast_used:
                # javalang confirmed: genuine uncaught exception → HIGH
                confidence = 85
                severity   = "HIGH"
                evidence_text = f"AST-confirmed (javalang): call site in {filepath} has no try-catch or throws for {exception}"
            else:
                # Regex only (javalang unavailable or repo not set): advisory LOW
                # Cannot verify without full file — may be a false positive
                confidence = 45
                severity   = "LOW"
                evidence_text = (
                    f"Regex heuristic: {method}() added in diff, no try/catch or throws found "
                    f"in ±40-line window. Run `mvn -pl <module> compile` to verify."
                )

            findings.append(BuildFinding(
                check    = "uncaught_checked_exception",
                step     = "build",
                severity = severity,
                confidence = confidence,
                title    = (
                    f"Possible uncaught {exception} — {method}() may need try-catch"
                    if severity == "LOW" else
                    f"Uncaught {exception} — {method}() called without try-catch"
                ),
                detail   = (
                    f"{filepath}: {method}() throws {full_exc}. "
                    + ("Verify exception handling before promoting."
                       if severity == "LOW" else
                       "No catch block or throws declaration found — will not compile.")
                ),
                evidence = evidence_text,
            ))

    return findings


# ── Java syntax validation ────────────────────────────────────────────────────

def _extract_added_file_content(diff_text: str, filepath: str) -> str:
    """
    Extract the full content of a newly-added Java file from a git diff.
    Works for files that appear as entirely new (all + lines after +++ header).
    Returns empty string if file is not fully present in diff.
    """
    lines = diff_text.splitlines()
    in_file = False
    content_lines = []
    for line in lines:
        if line.startswith("+++ b/") and line[6:].strip() == filepath:
            in_file = True
            content_lines = []
            continue
        if in_file:
            if line.startswith("+++ b/") or line.startswith("diff --git"):
                break
            if line.startswith("+") and not line.startswith("+++"):
                content_lines.append(line[1:])
            elif line.startswith("@@"):
                continue  # hunk header
            elif line.startswith("-"):
                # If there are removed lines, it's a modification not a full add
                # Full syntax check only makes sense for fully-added files
                return ""
    return "\n".join(content_lines)


def _find_reenabled_modules(diff_text: str) -> set:
    """Extract the names of reactor modules being re-enabled in pom.xml."""
    text = diff_text or ""
    _uncomments = re.findall(r"^-\s*<!--.*?<module>(.+?)</module>.*?-->", text, re.MULTILINE)
    _new_enables = re.findall(r"^\+\s*<module>(.+?)</module>", text, re.MULTILINE)
    return set(_uncomments) & set(_new_enables)


def _check_reenabled_module_frontend(
    diff_text: str,
    repo_dir: str = "",
) -> List[BuildFinding]:
    """
    When pom.xml re-enables a reactor module, check if it has a ui.frontend
    that will run npm run build — and scan it statically for issues.

    A module may have been disabled BECAUSE its frontend was broken.
    Re-enabling it brings the broken npm build back into the Maven reactor.

    Static checks (no npm required):
    1. module has ui.frontend + package.json → flag for manual verification
    2. package.json is malformed JSON → HIGH: npm install will fail immediately
    3. No lockfile (package-lock.json / yarn.lock) → MEDIUM: version drift in CI
    4. Build script references webpack but no webpack.config.js exists → HIGH
    5. Missing local JS imports (./path that doesn't exist) → HIGH
    6. Prior failure history in ChromaDB for this module → escalate to HIGH
    """
    import os as _os
    import json as _json

    findings: List[BuildFinding] = []
    restored = _find_reenabled_modules(diff_text)
    if not restored or not repo_dir:
        return findings

    for module in sorted(restored):
        module_clean = module.strip().rstrip("/")
        frontend_path = ""

        # Find the ui.frontend directory for this module
        for candidate in [
            _os.path.join(repo_dir, module_clean, "ui.frontend"),
            _os.path.join(repo_dir, "idfc-ams", module_clean, "ui.frontend"),
        ]:
            if _os.path.isdir(candidate):
                frontend_path = candidate
                break

        if not frontend_path:
            continue  # no ui.frontend — Java-only module, already checked

        pkg_path = _os.path.join(frontend_path, "package.json")
        if not _os.path.isfile(pkg_path):
            continue

        pkg = {}

        # ── Check 1: flag that npm run build will execute ─────────────────────
        findings.append(BuildFinding(
            check="reenabled_module_has_frontend",
            step="build", severity="MEDIUM", confidence=80,
            title=f"Re-enabled module '{module_clean}' has ui.frontend — npm run build will execute",
            detail=pkg_path,
            evidence=(
                f"Re-enabling '{module_clean}' brings its ui.frontend/package.json into the Maven "
                f"reactor. frontend-maven-plugin will run npm run build. If the frontend was broken "
                f"when the module was disabled, the build will fail here. "
                f"Verify the frontend compiles cleanly: cd {frontend_path} && npm install && npm run build"
            ),
        ))

        # ── Check 2: package.json validity ───────────────────────────────────
        try:
            with open(pkg_path, encoding="utf-8", errors="ignore") as _f:
                pkg = _json.load(_f)
        except _json.JSONDecodeError as _je:
            findings.append(BuildFinding(
                check="frontend_pkg_json_invalid",
                step="build", severity="HIGH", confidence=95,
                title=f"'{module_clean}/ui.frontend/package.json' is malformed — npm install will fail",
                detail=pkg_path,
                evidence=f"JSON parse error: {str(_je)[:120]}. npm cannot read the package manifest.",
            ))
            continue  # no point checking further

        # ── Check 3: missing lockfile ─────────────────────────────────────────
        _has_lock = (
            _os.path.isfile(_os.path.join(frontend_path, "package-lock.json"))
            or _os.path.isfile(_os.path.join(frontend_path, "yarn.lock"))
            or _os.path.isfile(_os.path.join(frontend_path, "pnpm-lock.yaml"))
        )
        if not _has_lock:
            findings.append(BuildFinding(
                check="frontend_no_lockfile",
                step="build", severity="MEDIUM", confidence=72,
                title=f"'{module_clean}/ui.frontend' has no lockfile — npm versions may drift in CI",
                detail=frontend_path,
                evidence=(
                    "No package-lock.json or yarn.lock found. In Cloud Manager CI, npm install "
                    "will resolve the latest versions matching semver ranges, which may differ "
                    "from what was tested locally. Add a lockfile."
                ),
            ))

        # ── Check 4: webpack config missing ──────────────────────────────────
        _build_script = pkg.get("scripts", {}).get("build", "")
        if "webpack" in _build_script:
            _webpack_configs = [
                f for f in (_os.listdir(frontend_path) if _os.path.isdir(frontend_path) else [])
                if "webpack" in f.lower() and f.endswith((".js", ".ts", ".cjs", ".mjs"))
            ]
            if not _webpack_configs:
                findings.append(BuildFinding(
                    check="frontend_webpack_config_missing",
                    step="build", severity="HIGH", confidence=88,
                    title=f"'{module_clean}/ui.frontend' build script uses webpack but no webpack.config.js found",
                    detail=frontend_path,
                    evidence=(
                        f"package.json build script: '{_build_script}' references webpack, "
                        f"but no webpack config file was found in {frontend_path}. "
                        f"The build will fail with 'webpack not found' or 'config not found'."
                    ),
                ))

        # ── Check 5: missing local JS imports ────────────────────────────────
        _src_dir = _os.path.join(frontend_path, "src")
        if _os.path.isdir(_src_dir):
            for _root, _, _files in _os.walk(_src_dir):
                for _fname in _files:
                    if not _fname.endswith((".js", ".ts", ".jsx", ".tsx")):
                        continue
                    _fpath = _os.path.join(_root, _fname)
                    try:
                        _content = open(_fpath, encoding="utf-8", errors="ignore").read()
                        # Find relative imports: import X from './something'
                        _imports = re.findall(
                            r"""(?:import|require)\s*(?:.*?\s+from\s+)?['"](\.{1,2}/[^'"]+)['"]""",
                            _content
                        )
                        for _imp in _imports:
                            _imp_base = _os.path.join(_root, _imp)
                            # Check if file exists with any common extension
                            _exists = any(
                                _os.path.isfile(_imp_base + ext)
                                for ext in ("", ".js", ".ts", ".jsx", ".tsx", "/index.js", "/index.ts")
                            )
                            if not _exists:
                                findings.append(BuildFinding(
                                    check="frontend_missing_import",
                                    step="build", severity="HIGH", confidence=75,
                                    title=f"Missing import '{_imp}' in {_fname} — webpack will fail",
                                    detail=_fpath,
                                    evidence=(
                                        f"'{_fname}' imports '{_imp}' but the file doesn't exist. "
                                        f"webpack/bundler will fail with 'Module not found'. "
                                        f"This is a definite build failure if this file is in the build graph."
                                    ),
                                ))
                                break  # one finding per file to avoid noise
                    except Exception:
                        continue

        # ── Check 6: prior failure history (ChromaDB) ────────────────────────
        try:
            from vector_store.store import find_similar_failures
            _hist = find_similar_failures(
                error_type="build",
                error_message=f"npm run build {module_clean} frontend",
                key_lines=[module_clean, "npm", "frontend"],
                step="build", top_k=2,
            )
            if _hist and any(h.get("similarity_score", 0) >= 0.65 for h in _hist):
                _best = max(_hist, key=lambda h: h.get("similarity_score", 0))
                findings.append(BuildFinding(
                    check="reenabled_module_has_prior_failures",
                    step="build", severity="HIGH", confidence=78,
                    title=f"'{module_clean}' has prior build failure history — re-enabling may reintroduce",
                    detail=frontend_path,
                    evidence=(
                        f"ChromaDB shows {len(_hist)} past build failures for similar module patterns "
                        f"(best match: {int(_best.get('similarity_score',0)*100)}%). "
                        f"The module may have been disabled because of known failures. "
                        f"Root cause: {(_best.get('root_cause','unknown'))[:100]}"
                    ),
                ))
        except Exception:
            pass

    return findings


def _check_syntax_in_reenabled_modules(
    diff_text: str,
    repo_dir: str,
) -> List[BuildFinding]:
    """
    When pom.xml re-enables a reactor module, scan its Java files for syntax errors.

    A module may have been disabled precisely because it was broken. Re-enabling it
    brings its code back into the build without any code change in the commit diff.
    The syntax errors won't appear in the diff — only in the repo files.

    Example: pom.xml re-enables idfc-ams → IdfcDMServiceImpl.java inside it has a
    missing brace → 19 cascading compiler errors → BUILD FAILURE.
    """
    try:
        import javalang as _jl
    except ImportError:
        return []

    import os as _os
    restored = _find_reenabled_modules(diff_text)
    if not restored or not repo_dir:
        return []

    findings: List[BuildFinding] = []

    for module in sorted(restored):
        module_clean = module.strip().rstrip("/")
        # Module name in pom.xml can be "idfc-ams", "idfcfirst-academy", or a path like "core"
        search_dirs = [
            _os.path.join(repo_dir, module_clean),
            _os.path.join(repo_dir, "idfc-ams", module_clean),  # subtree subdir
        ]

        for module_dir in search_dirs:
            if not _os.path.isdir(module_dir):
                continue

            # Find Java source files in this module (cap at 30 to avoid huge scans)
            java_files: List[str] = []
            for root, _dirs, files in _os.walk(module_dir):
                for fname in files:
                    if fname.endswith(".java"):
                        java_files.append(_os.path.join(root, fname))
                if len(java_files) >= 30:
                    break

            for java_file in java_files[:30]:
                try:
                    source = open(java_file, encoding="utf-8", errors="ignore").read()
                    if len(source) < 10:
                        continue
                    try:
                        _jl.parse.parse(source)
                    except (_jl.parser.JavaSyntaxError, _jl.tokenizer.LexerError) as e:
                        rel = _os.path.relpath(java_file, repo_dir)
                        findings.append(BuildFinding(
                            check      = "java_syntax_error_in_reenabled_module",
                            step       = "build",
                            severity   = "CERTAIN",
                            confidence = 95,
                            title      = (
                                f"Syntax error in re-enabled module '{module_clean}': "
                                f"{_os.path.basename(java_file)} — will not compile"
                            ),
                            detail     = f"{rel}: {str(e)[:120]}",
                            evidence   = (
                                f"pom.xml re-enables '{module_clean}' but that module contains "
                                f"invalid Java syntax. Re-enabling brings broken code back into "
                                f"the reactor build. Fix the syntax error before re-enabling."
                            ),
                        ))
                except Exception:
                    continue
            break  # found the module dir, don't check other candidates

    return findings


def _check_java_syntax(
    diff_text: str,
    changed_files: List[str],
    repo_dir: str = "",
) -> List[BuildFinding]:
    """
    Parse added Java files with javalang to catch syntax errors before javac.

    A syntax error (missing brace, malformed annotation, incomplete class) causes
    cascading 'illegal start of expression' errors and 100% certain build failure.

    Works on:
    1. Fully-added files in the diff (subtree imports, new files)
    2. Full files read from local repo when available

    javalang.parser.JavaSyntaxError / javalang.tokenizer.LexerError → syntax error detected.
    """
    try:
        import javalang as _jl
    except ImportError:
        return []

    findings: List[BuildFinding] = []
    java_files = [f for f in changed_files if f.endswith(".java")]
    if not java_files:
        return findings

    checked: set = set()

    for filepath in java_files:
        if filepath in checked:
            continue

        source = ""

        # Try 1: extract from diff (works for fully-added files — subtree imports)
        source = _extract_added_file_content(diff_text, filepath)

        # Try 2: read full file from local repo
        if not source and repo_dir:
            import os as _os
            candidates = [
                _os.path.join(repo_dir, filepath),
                _os.path.join(repo_dir, "idfc-ams", filepath),
            ]
            for c in candidates:
                if _os.path.isfile(c):
                    try:
                        source = open(c, encoding="utf-8", errors="ignore").read()
                        break
                    except Exception:
                        pass

        if not source or len(source) < 20:
            continue

        # Only attempt to parse complete Java files — not diff fragments.
        # A complete file has a class/interface/enum declaration.
        # Fragments from modified files (just method bodies) would always fail
        # javalang and produce false positive syntax errors.
        _has_class_decl = any(
            kw in source for kw in ("class ", "interface ", "enum ", "@interface ")
        )
        if not _has_class_decl:
            continue

        checked.add(filepath)

        try:
            _jl.parse.parse(source)
            # Parse succeeded — no syntax errors
        except (_jl.parser.JavaSyntaxError, _jl.tokenizer.LexerError) as e:
            # Syntax error detected — 100% certain build failure
            err_str = str(e)[:120]
            filename = filepath.split("/")[-1]
            findings.append(BuildFinding(
                check      = "java_syntax_error",
                step       = "build",
                severity   = "CERTAIN",
                confidence = 95,
                title      = f"Java syntax error in {filename} — will not compile",
                detail     = f"{filepath}: {err_str}",
                evidence   = (
                    "javalang AST parse failed — file has invalid Java syntax "
                    "(missing brace, malformed annotation, or incomplete class). "
                    "This causes 'illegal start of expression' errors in javac."
                ),
            ))
        except Exception:
            pass  # other javalang failures — not a syntax error, skip

    return findings


# ── Main predictor ────────────────────────────────────────────────────────────

def predict_build_failures(
    diff_text:       str,
    changed_files:   List[str],
    commit_title:    str,
    repo_dir:        str = "",
    submodule_diffs: Optional[Dict[str, str]] = None,
    java_upgrade_pending: bool = False,
) -> BuildPrediction:
    """
    Run all deterministic checks on a git diff.
    Returns a BuildPrediction with findings and overall risk assessment.
    submodule_diffs: raw per-submodule diffs — merged for analysis when parent is pointer-only.
    """
    diff_text, changed_files = merge_submodule_analysis_inputs(
        diff_text, changed_files, submodule_diffs
    )
    signals = analyze_diff(diff_text, changed_files, title=commit_title)
    has_submodule_java = submodule_diffs_contain_java(submodule_diffs)

    all_findings: List[BuildFinding] = []

    # Check 0 (pre): Missing symbol references — cannot find symbol errors
    # Runs before everything including subtree short-circuit since subtree imports
    # can bring in code that references non-existent classes/methods (new or deleted)
    _missing_sym = _check_missing_symbol_references(diff_text, changed_files, repo_dir)
    if _missing_sym:
        # If missing symbols found in a subtree import, override the LOW verdict
        _top_ms = _missing_sym[0]
        return BuildPrediction(
            predicted_step="build",
            predicted_risk="High",
            confidence=_top_ms.confidence,
            findings=_missing_sym,
            is_structural=True,
            override_llm=True,
            summary=(
                f"Compilation error detected: {_top_ms.title}. "
                f"Fix before triggering the pipeline."
            ),
        )

    # Check 0: Java syntax validation — runs before all other checks including subtree.
    # Also scans re-enabled reactor modules from the local repo — they may contain
    # syntax errors that aren't visible in the diff (broken before this commit).
    _syntax_findings = _check_java_syntax(diff_text, changed_files, repo_dir)
    if not _syntax_findings:
        _syntax_findings = _check_syntax_in_reenabled_modules(diff_text, repo_dir)
    # Also check for frontend build failures in re-enabled modules
    _frontend_findings = _check_reenabled_module_frontend(diff_text, repo_dir)
    if _frontend_findings:
        # Frontend failures are separate from syntax — don't gate on syntax
        # But if syntax errors exist, they take priority (fail before frontend even runs)
        if not _syntax_findings:
            # No Java syntax errors — frontend is the likely failure
            _high_fe = [f for f in _frontend_findings if f.severity in ("HIGH", "CERTAIN")]
            if _high_fe:
                _syntax_findings = _frontend_findings  # treat as build blocker
    if _syntax_findings:
        _top_s = _syntax_findings[0]
        return BuildPrediction(
            predicted_step = "build",
            predicted_risk = "High",
            confidence     = _top_s.confidence,
            findings       = _syntax_findings,
            is_structural  = True,
            override_llm   = True,
            summary        = (
                f"Java syntax error detected: {_top_s.title}. "
                f"Fix the syntax error before triggering the pipeline."
            ),
        )

    # Check 0a: True subtree import — code was pre-validated in source repo.
    # EXCEPTION: run uncaught-exception check first — the source repo may itself
    # have a compilation error (e.g. LoginException not caught). The subtree import
    # faithfully reproduces that bug into this repo.
    if signals.is_subtree_import:
        _exc_findings = _check_uncaught_sling_exceptions(diff_text, changed_files, repo_dir)
        if _exc_findings:
            # Subtree import contains compilation errors — not safe
            _top = _exc_findings[0]
            return BuildPrediction(
                predicted_step = "build",
                predicted_risk = "High",
                confidence     = _top.confidence,
                findings       = _exc_findings,
                is_structural  = True,
                override_llm   = True,
                summary        = (
                    f"Git subtree import contains a compilation error: {_top.title}. "
                    f"Fix the error in the source repository before re-importing."
                ),
            )
        # No compilation errors found — safe subtree import
        subtree_finding = BuildFinding(
            check="subtree_import", step="build", severity="LOW", confidence=88,
            title="Git subtree import — code pre-validated in source repo",
            detail="All changed files are under one new directory",
            evidence=f"Commit title matches subtree pattern: '{commit_title[:60]}'"
        )
        return BuildPrediction(
            predicted_step = "none",
            predicted_risk = "Low",
            confidence     = 88,
            findings       = [subtree_finding],
            is_structural  = True,
            override_llm   = True,
            summary        = "Git subtree import — code pre-validated in source repo. Build risk is structurally low.",
        )

    # Check 0b: Submodule pointer bump — skip only when parent is pointer-only AND
    # we have no submodule Java to analyze. Otherwise run full checks on merged diff.
    if _is_submodule_release_bump(signals) and not has_submodule_java and not signals.has_java_change:
        pointer_finding = BuildFinding(
            check="submodule_pointer_bump", step="build", severity="LOW", confidence=35,
            title="Submodule pointer bump — no app code changed in parent repo",
            detail="Only submodule pointers and pom.xml changed",
            evidence="Build risk cannot be assessed from parent diff alone.",
        )
        # Still run submodule/reactor checks — they apply to pointer-only parent diffs
        sub_findings = _check_multi_submodule_reactor(diff_text, changed_files, signals)
        if sub_findings:
            all_findings.extend(sub_findings)
        else:
            return BuildPrediction(
                predicted_step="unknown",
                predicted_risk="Low",
                confidence=35,
                findings=[pointer_finding],
                is_structural=False,
                override_llm=False,
                summary="Submodule pointer bump. No submodule code available for analysis.",
            )

    # Extract parent-only diff (strip submodule content) — used by multiple checks below
    _sm_marker_early = "## Submodule Code Changes"
    _sm_marker_nl = "\n\n## Submodule Code Changes"
    if _sm_marker_nl in diff_text:
        _parent_only_diff_early = diff_text[:diff_text.index(_sm_marker_nl)]
    elif _sm_marker_early in diff_text:
        _parent_only_diff_early = diff_text[:diff_text.index(_sm_marker_early)]
    else:
        _parent_only_diff_early = diff_text

    # Check 0.4: pom.xml structural errors — duplicate deps, missing parent version, scope conflicts
    # These are deterministic Maven failures (>99% accuracy). Run before everything.
    all_findings.extend(_check_pom_structural_errors(diff_text, changed_files, repo_dir))
    if submodule_diffs:
        for _sm_n, _sm_d in (submodule_diffs or {}).items():
            if _sm_d:
                all_findings.extend(_check_pom_structural_errors(
                    _sm_d, [f for f in changed_files if _sm_n in f] or ["pom.xml"], repo_dir
                ))

    # Check 0.5: AEM build plugin version changes — must run before all other checks.
    # A wrong plugin version prevents Maven from reading pom.xml files entirely,
    # causing "build could not read N projects" before any compilation starts.
    all_findings.extend(_check_aem_build_plugin_versions(diff_text, changed_files))
    if submodule_diffs:
        for _sm_n, _sm_d in submodule_diffs.items():
            if _sm_d:
                _sm_plugin_findings = _check_aem_build_plugin_versions(
                    _sm_d, [f for f in changed_files if _sm_n in f] or ["pom.xml"]
                )
                all_findings.extend(_sm_plugin_findings)

    # Check 1: Maven deps
    all_findings.extend(_check_new_maven_deps(signals))

    # Check 2: OSGi references (requires repo)
    # _parent_only_diff strips submodule content. Re-parse parent-only signals
    # to correctly detect submodule-only commits — enriched changed_files include
    # submodule Java paths which would make _is_submodule_release_bump() return False.
    # @Reference annotations in submodule code → implementations in OTHER submodules
    # → always false positives when searched only in parent/b86 repo.
    _parent_only_signals = analyze_diff(_parent_only_diff_early, [], title=commit_title)
    _is_submodule_only_parent = _parent_only_diff_early.strip() == "" or _is_submodule_release_bump(_parent_only_signals)
    if repo_dir and not _is_submodule_only_parent:
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

    # Check 5: npm changes (wildcard versions)
    all_findings.extend(_check_npm_changes(signals))

    # Check 5b: npm install risks (lockfile drift, local paths, JSON syntax)
    all_findings.extend(_check_npm_install_risks(diff_text, changed_files))

    # Check 6: Dispatcher config — flags deploy (primary) and securityTest risks
    all_findings.extend(_check_dispatcher_changes(signals))

    # Check 6b: ui.apps deploy risks — JCR package install failures
    # These pass build but fail at deploy: missing jcr:primaryType, HTL/content.xml gap, clientlib
    all_findings.extend(_check_uiapps_deploy_risks(diff_text, changed_files))

    # Check 7: OSGi runmode configs
    all_findings.extend(_check_osgi_config_changes(signals))

    # Check 8: package-lock drift
    all_findings.extend(_check_package_lock_drift(signals))

    # Check 9: Reactor version bump (pom only, no code)
    all_findings.extend(_check_reactor_version_bump(signals))

    # Separate parent diff from submodule content for checks that shouldn't cross boundaries
    # The exact header written by summarize_submodule_diffs() is:
    #   "## Submodule Code Changes (N submodule(s) changed)\n"
    # It is appended to diff_excerpt with a leading "\n\n" in risk_analyzer.py.
    # We try both forms; if neither is found (no submodule content was appended),
    # the whole diff_text is already parent-only.
    _sm_marker = "## Submodule Code Changes"
    _sm_marker_with_newlines = "\n\n## Submodule Code Changes"
    if _sm_marker_with_newlines in diff_text:
        _parent_only_diff = diff_text[:diff_text.index(_sm_marker_with_newlines)]
    elif _sm_marker in diff_text:
        _parent_only_diff = diff_text[:diff_text.index(_sm_marker)]
    else:
        # No submodule content appended — diff_text is already parent-only
        _parent_only_diff = diff_text

    # Check 10: @Autowired fields added without corresponding test mock update
    # Only on parent repo diff — submodule code has its own CI
    all_findings.extend(_check_autowired_test_mocks(_parent_only_diff, changed_files))

    # Check 11: Service Java changed without test file updates (surefire NPE pattern)
    # Run on parent diff AND each raw submodule diff.
    # For HDFC-style commits the service changes are in submodule diffs. The merged
    # diff_text contains a SUMMARY (not proper git diff format), so _parse_diff_file_sections
    # can't find service files in it. Must scan raw submodule_diffs directly.
    if _parent_only_diff.strip():
        all_findings.extend(_check_service_without_test_update(_parent_only_diff, commit_title))
    # Scan submodule diffs for service-without-test when:
    # 1. Parent has real code changes (not pointer-only) → always scan
    # 2. Parent IS pointer-only BUT only 1 submodule changed substantially
    #    (deliberate single-submodule change, not routine release bump)
    #    Threshold: diff > 500 lines = intentional change, not just version tag
    # Skip when multiple submodules bumped simultaneously (routine release) to avoid
    # false positives from services that were already tested in their own CI.
    # Scan submodule content when any submodule has >500 lines changed.
    # "Exactly 1" was too conservative — missed failures when 2 submodules changed
    # and the failing one was not the only large one (e.g. hdfcbankalforms + hdfcformskycservice).
    # Keep confidence capped at 55% for submodule findings to limit false positives.
    _has_substantial_submodule = False
    if _is_submodule_only_parent and submodule_diffs:
        _has_substantial_submodule = any(
            sm_diff and len(sm_diff.splitlines()) > 500
            for sm_diff in submodule_diffs.values()
        )

    if submodule_diffs and (not _is_submodule_only_parent or _has_substantial_submodule):
        for _sm_name, _sm_raw_diff in submodule_diffs.items():
            if _sm_raw_diff and _sm_raw_diff.strip():
                _sm_findings = _check_service_without_test_update(_sm_raw_diff, commit_title)
                for _smf in _sm_findings:
                    _smf = BuildFinding(
                        check=_smf.check, step=_smf.step,
                        severity=_smf.severity, confidence=min(_smf.confidence, 55),
                        title=_smf.title,
                        detail=_smf.detail,
                        evidence=f"[submodule: {_sm_name}] {_smf.evidence}",
                    )
                    all_findings.append(_smf)
                all_findings.extend(_check_test_runtime_failures(
                    _sm_raw_diff, [f for f in changed_files if _sm_name in f]
                ))

    # Check 12: Cloud Manager Java version change
    all_findings.extend(_check_cloudmanager_java_version(changed_files, diff_text, repo_dir))

    # Check 12: Multiple submodule bumps / reactor module list changes
    all_findings.extend(_check_multi_submodule_reactor(diff_text, changed_files, signals))

    # Check 13: DAO/LTS migration → code-caused loadTest regression risk
    all_findings.extend(_check_dao_migration_perf_risk(
        diff_text, changed_files, commit_title, java_upgrade_pending=java_upgrade_pending,
    ))

    # Check 13b: Build anti-patterns — skipTests, HTL syntax, version skew, dispatcher syntax etc.
    all_findings.extend(_check_build_antipatterns(diff_text, changed_files, repo_dir))

    # Check 14: Uncaught Sling/AEM checked exceptions (two-pass: regex + javalang AST)
    all_findings.extend(_check_uncaught_sling_exceptions(diff_text, changed_files, repo_dir))

    # Check 15b: Test infrastructure errors (abstract @InjectMocks, missing @ExtendWith, OSGi config PID)
    all_findings.extend(_check_test_infra_errors(diff_text, changed_files, repo_dir))
    if submodule_diffs and (not _is_submodule_only_parent or _has_substantial_submodule):
        for _sm_n, _sm_d in (submodule_diffs or {}).items():
            if _sm_d:
                all_findings.extend(_check_test_infra_errors(
                    _sm_d, [f for f in changed_files if _sm_n in f], repo_dir
                ))

    # Check 15: Test runtime failure patterns on parent diff
    # (submodule diffs are handled per-submodule in Check 11 above)
    all_findings.extend(_check_test_runtime_failures(_parent_only_diff, changed_files))

    # Deduplicate findings — same check + same title can appear when helper functions
    # are called from multiple code paths (e.g. reactor check called in both
    # Check 0b submodule path and Check 12 main path)
    _seen_finding_keys: set = set()
    _deduped_findings: List[BuildFinding] = []
    for _f in all_findings:
        _fkey = f"{_f.check}:{_f.title[:60]}"
        if _fkey not in _seen_finding_keys:
            _seen_finding_keys.add(_fkey)
            _deduped_findings.append(_f)
    all_findings = _deduped_findings

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

    # Prioritise findings — test runtime failures first, then structural/reactor noise.
    # For HDFC multi-submodule commits, reactor/pom findings score HIGH but are background
    # noise. The test failure findings (service_without_test_update, autowired_missing_test_mock,
    # static_aem_field, injectmocks_without_mock) are the IMMEDIATE build failure cause.
    _test_failure_checks = {
        "service_without_test_update", "autowired_missing_test_mock",
        "static_aem_field", "injectmocks_without_mock",
        "core_java_without_test_update", "uncaught_checked_exception",
        "java_syntax_error", "java_syntax_error_in_reenabled_module",
        "mockito_verify_removed", "parameterized_without_source",
    }
    _reactor_checks = {
        "reactor_module_toggle", "reactor_module_list_churn",
        "multi_submodule_bump", "submodule_reactor_change",
        "reactor_module_list_change", "submodule_pointer_with_pom",
    }
    _test_findings    = [f for f in all_findings if f.check in _test_failure_checks]
    _reactor_findings = [f for f in all_findings if f.check in _reactor_checks]
    _other_findings   = [f for f in all_findings if f.check not in _test_failure_checks and f.check not in _reactor_checks]

    # When test failure findings exist, downgrade reactor findings to MEDIUM max.
    # Reactor/pom changes are structural background risk, not the immediate surefire cause.
    if _test_findings:
        for _rf in _reactor_findings:
            if _rf.severity == "HIGH":
                _rf = BuildFinding(
                    check=_rf.check, step=_rf.step, severity="MEDIUM",
                    confidence=min(_rf.confidence, 55),
                    title=_rf.title,
                    detail=_rf.detail,
                    evidence=_rf.evidence + " (downgraded: test failures are the primary build risk)",
                )

    # Sorted order: test failures first (immediate cause), then others, then reactor noise
    all_findings = _test_findings + _other_findings + _reactor_findings

    # Find highest severity finding
    severity_order = {"CERTAIN": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
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
