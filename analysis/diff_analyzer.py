"""
Structured signals extracted from a git diff.

Parses pom.xml changes, Java annotations, npm packages, vault filters,
and dispatcher configs — turning a raw diff into actionable build signals.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class MavenDependency:
    group_id:    str
    artifact_id: str
    version:     Optional[str]
    scope:       Optional[str]
    added:       bool   # True = added, False = removed


@dataclass
class OsgiSignal:
    file:        str
    signal_type: str   # "new_component", "new_reference", "new_service", "changed_interface"
    class_name:  str
    detail:      str   # e.g. referenced service name


@dataclass
class NpmChange:
    package_name: str
    version:      Optional[str]
    added:        bool


@dataclass
class VaultFilterChange:
    file:    str
    path:    str
    mode:    str   # "add", "remove", "modify"
    root:    str   # JCR root path


@dataclass
class JavaInterfaceChange:
    file:        str
    class_name:  str
    method_name: str
    change_type: str   # "signature_changed", "method_removed", "method_added"


@dataclass
class DiffSignals:
    """All structured signals extracted from a git diff."""
    maven_deps_added:     List[MavenDependency]   = field(default_factory=list)
    maven_deps_removed:   List[MavenDependency]   = field(default_factory=list)
    osgi_signals:         List[OsgiSignal]         = field(default_factory=list)
    npm_changes:          List[NpmChange]           = field(default_factory=list)
    vault_filter_changes: List[VaultFilterChange]  = field(default_factory=list)
    interface_changes:    List[JavaInterfaceChange] = field(default_factory=list)
    dispatcher_changed:   bool                     = False
    has_pom_change:       bool                     = False
    has_java_change:      bool                     = False
    has_npm_change:       bool                     = False
    has_config_change:    bool                     = False
    changed_files:        List[str]                = field(default_factory=list)
    is_subtree_import:    bool                     = False
    is_deletion_only:     bool                     = False


# ── Regex patterns ────────────────────────────────────────────────────────────

_POM_DEP_BLOCK = re.compile(
    r'([+-])\s*<groupId>([^<]+)</groupId>\s*\n'
    r'\s*[+-]?\s*<artifactId>([^<]+)</artifactId>'
    r'(?:\s*\n\s*[+-]?\s*<version>([^<]+)</version>)?'
    r'(?:\s*\n\s*[+-]?\s*<scope>([^<]+)</scope>)?',
    re.MULTILINE,
)
_OSGI_COMPONENT   = re.compile(r'^\+.*@Component', re.MULTILINE)
_OSGI_REFERENCE   = re.compile(r'^\+.*@Reference\s*(?:\([^)]*\))?\s*\n\s*.*?(\w+Service|\w+Repository|\w+Manager)', re.MULTILINE)
_OSGI_SERVICE     = re.compile(r'^\+.*@Service\s', re.MULTILINE)
_JAVA_INTERFACE   = re.compile(r'^\+.*(?:public|protected)\s+(?:abstract\s+)?(?:\w+\s+)+(\w+)\s*\(', re.MULTILINE)
_JAVA_CLASS_NAME  = re.compile(r'(?:class|interface)\s+(\w+)')
_NPM_DEP          = re.compile(r'([+-])\s*"([@\w/.-]+)":\s*"([^"]+)"')
_VAULT_FILTER     = re.compile(r'([+-])\s*<filter root="([^"]+)"(?:\s+mode="([^"]+)")?')
_FILE_HEADER      = re.compile(r'^diff --git a/(.+?) b/', re.MULTILINE)
_SUBTREE_TITLE    = re.compile(r"add ['\"]?\S+/['\"]? from commit", re.IGNORECASE)


# ── Main parser ───────────────────────────────────────────────────────────────

def analyze_diff(diff_text: str, changed_files: List[str], title: str = "") -> DiffSignals:
    """
    Parse a git diff into structured signals.
    """
    signals = DiffSignals(changed_files=changed_files)

    if not diff_text and not changed_files:
        return signals

    # Detect subtree imports — structurally low risk for build
    if _SUBTREE_TITLE.search(title or ""):
        signals.is_subtree_import = True

    # Detect deletion-only commits
    lines = diff_text.splitlines() if diff_text else []
    added_lines   = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    removed_lines = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    if removed_lines > 0 and added_lines == 0:
        signals.is_deletion_only = True

    # File type flags
    for f in changed_files:
        fl = f.lower()
        if "pom.xml" in fl:
            signals.has_pom_change = True
        if fl.endswith(".java"):
            signals.has_java_change = True
        if "package.json" in fl:
            signals.has_npm_change = True
        if any(ext in fl for ext in [".cfg", ".config", ".yaml", ".yml", ".properties", ".xml"]):
            signals.has_config_change = True
        if any(x in fl for x in ["dispatcher", ".vhost", ".any", ".farm", ".rules"]):
            signals.dispatcher_changed = True

    if not diff_text:
        return signals

    # Split diff into per-file sections
    file_sections = _split_by_file(diff_text)

    for filepath, section in file_sections.items():
        fl = filepath.lower()

        # Maven dependency changes
        if "pom.xml" in fl:
            _parse_pom_deps(section, filepath, signals)

        # Java/OSGi changes
        if fl.endswith(".java"):
            _parse_java(section, filepath, signals)

        # npm package.json changes
        if "package.json" in fl and "node_modules" not in fl:
            _parse_npm(section, signals)

        # Vault filter changes
        if "filter.xml" in fl:
            _parse_vault_filter(section, filepath, signals)

    return signals


def _split_by_file(diff_text: str) -> Dict[str, str]:
    """Split a unified diff into per-file sections."""
    result: Dict[str, str] = {}
    current_file = ""
    current_lines: List[str] = []

    for line in diff_text.splitlines(keepends=True):
        m = re.match(r'^diff --git a/(.+?) b/', line)
        if m:
            if current_file and current_lines:
                result[current_file] = "".join(current_lines)
            current_file = m.group(1)
            current_lines = []
        current_lines.append(line)

    if current_file and current_lines:
        result[current_file] = "".join(current_lines)

    return result


def _parse_pom_deps(section: str, filepath: str, signals: DiffSignals) -> None:
    """Extract added/removed Maven dependencies from a pom.xml diff section."""
    # Look for dependency blocks in the diff
    dep_pattern = re.compile(
        r'([+-])\s*<dependency>\s*\n'
        r'((?:[^<\n]*\n)*?)'
        r'\s*[+-]?\s*</dependency>',
        re.MULTILINE,
    )
    for m in dep_pattern.finditer(section):
        sign  = m.group(1)
        block = m.group(0)
        g = re.search(r'<groupId>([^<]+)</groupId>', block)
        a = re.search(r'<artifactId>([^<]+)</artifactId>', block)
        v = re.search(r'<version>([^<]+)</version>', block)
        s = re.search(r'<scope>([^<]+)</scope>', block)
        if g and a:
            dep = MavenDependency(
                group_id    = g.group(1).strip(),
                artifact_id = a.group(1).strip(),
                version     = v.group(1).strip() if v else None,
                scope       = s.group(1).strip() if s else None,
                added       = sign == "+",
            )
            if dep.added:
                signals.maven_deps_added.append(dep)
            else:
                signals.maven_deps_removed.append(dep)


def _parse_java(section: str, filepath: str, signals: DiffSignals) -> None:
    """Detect OSGi annotation additions and interface changes."""
    # Class name from filepath
    class_name = filepath.split("/")[-1].replace(".java", "")

    # New @Component
    if _OSGI_COMPONENT.search(section):
        signals.osgi_signals.append(OsgiSignal(
            file=filepath, signal_type="new_component",
            class_name=class_name, detail="New @Component registered"
        ))

    # New @Service
    if _OSGI_SERVICE.search(section):
        signals.osgi_signals.append(OsgiSignal(
            file=filepath, signal_type="new_service",
            class_name=class_name, detail="New @Service registered"
        ))

    # New @Reference (unresolved dependency risk)
    for m in _OSGI_REFERENCE.finditer(section):
        signals.osgi_signals.append(OsgiSignal(
            file=filepath, signal_type="new_reference",
            class_name=class_name,
            detail=f"New @Reference to {m.group(1) if m.lastindex else 'unknown service'}"
        ))

    # Changed public method signatures in interfaces
    if "interface" in filepath.lower() or "Interface" in filepath:
        added_methods   = [l for l in section.splitlines() if l.startswith("+") and "public " in l and "(" in l]
        removed_methods = [l for l in section.splitlines() if l.startswith("-") and "public " in l and "(" in l]
        for rm in removed_methods:
            m = re.search(r'(\w+)\s*\(', rm)
            if m:
                signals.interface_changes.append(JavaInterfaceChange(
                    file=filepath, class_name=class_name,
                    method_name=m.group(1), change_type="method_removed"
                ))


def _parse_npm(section: str, signals: DiffSignals) -> None:
    """Extract added/removed npm packages."""
    in_deps = False
    for line in section.splitlines():
        if any(k in line for k in ['"dependencies"', '"devDependencies"', '"peerDependencies"']):
            in_deps = True
        if in_deps and line.strip() == "}":
            in_deps = False
        if in_deps:
            m = _NPM_DEP.search(line)
            if m:
                signals.npm_changes.append(NpmChange(
                    package_name = m.group(2),
                    version      = m.group(3),
                    added        = m.group(1) == "+",
                ))


def _parse_vault_filter(section: str, filepath: str, signals: DiffSignals) -> None:
    """Extract vault filter path changes."""
    for m in _VAULT_FILTER.finditer(section):
        signals.vault_filter_changes.append(VaultFilterChange(
            file = filepath,
            path = m.group(2),
            mode = m.group(3) or "replace",
            root = m.group(2).split("/")[1] if m.group(2).count("/") >= 1 else m.group(2),
        ))
