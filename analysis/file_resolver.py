"""
File Resolver — maps a parsed pipeline error to the minimum set of
file snippets needed for root cause analysis.

Instead of passing whole files to the LLM, this module:
  1. Parses the error message + key_lines for exact file:line references
  2. Reads only those files from the local git clone
  3. Extracts a ±WINDOW line window around the error line
  4. Marks the exact error line with >>>

Token savings: ~95% vs sending whole files.
Accuracy gain: LLM sees the exact defective code, not irrelevant context.

Supported error types
---------------------
typescript_error          →  .tsx / .ts file + line from "file(line,col): error"
java_compile_error        →  .java file + line from "[ERROR] path/File.java:[42,5]"
build_failure             →  tries java + maven pom.xml dependency block
missing_npm_module        →  package.json dependencies + devDependencies sections
npm_error / npm_build_failed → same as missing_npm_module
maven_error               →  pom.xml <dependency> block for the failing artifact
apache_config_syntax_error → .conf / .vhost file + line from error detail
missing_env_variable      →  .conf / .vhost where the undefined var is used
dispatcher_error          →  .conf / .vhost containing the keyword
security_failure          →  ui.config / ui.apps XML with enabled risky bundle
osgi_error                →  same as security_failure
quality_gate_failure      →  pom.xml sonar / quality profile section
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import os

from dotenv import load_dotenv
load_dotenv()

WINDOW = 15   # lines above/below the error line


def _repo_dir() -> str:
    """Resolve repo dir at call time so .env is always respected."""
    return os.getenv("GIT_LOCAL_DIR", str(Path.home() / "idfc-repo"))


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class FileSnippet:
    file:     str            # path relative to REPO_DIR
    abs_path: str            # full filesystem path
    line_no:  Optional[int]  # exact error line (None = section-level)
    snippet:  str            # formatted excerpt, >>> on the error line
    reason:   str            # why this file was selected


# ── Internal helpers ──────────────────────────────────────────────────────────

def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _rel(path: str) -> str:
    try:
        return str(Path(path).relative_to(_repo_dir()))
    except ValueError:
        return path


def _find(pattern: str, root: str = "", exclude: tuple = (".git", "node_modules", "target")) -> List[str]:
    """Recursive glob, skipping build/vendor dirs."""
    search_root = root or _repo_dir()
    return [
        str(p) for p in Path(search_root).rglob(pattern)
        if not any(x in str(p) for x in exclude)
    ]


def _window(content: str, line_no: int, w: int = WINDOW) -> str:
    """
    Extract ±w lines around line_no with >>> marker on the error line.

    Output example:
       42 |   import { foo } from './bar';
    >>> 43 |   const x = missingThing.doSomething();
       44 |   return x;
    """
    lines = content.splitlines()
    if line_no < 1 or line_no > len(lines):
        # Clamp gracefully
        line_no = max(1, min(line_no, len(lines)))
    start = max(0, line_no - w - 1)
    end   = min(len(lines), line_no + w)
    out   = []
    for i, ln in enumerate(lines[start:end], start + 1):
        marker = ">>>" if i == line_no else "   "
        out.append(f"{marker} {i:4d} | {ln}")
    return "\n".join(out)


def _section(content: str, keyword: str, max_lines: int = 60) -> str:
    """
    Extract the JSON/XML block that contains keyword, with line numbers.
    Tracks brace/bracket depth to know when the block ends.
    """
    lines   = content.splitlines()
    started = False
    depth   = 0
    result  = []
    for i, ln in enumerate(lines, 1):
        if not started and keyword in ln:
            started = True
        if started:
            result.append(f"   {i:4d} | {ln}")
            depth += ln.count("{") + ln.count("[") - ln.count("}") - ln.count("]")
            if len(result) > 4 and depth <= 0:
                break
            if len(result) >= max_lines:
                result.append("          ... [section truncated]")
                break
    return "\n".join(result)


def _grep_line(content: str, pattern: str, flags: int = re.IGNORECASE) -> Optional[int]:
    """Return 1-based line number of first match, or None."""
    for i, ln in enumerate(content.splitlines(), 1):
        if re.search(pattern, ln, flags):
            return i
    return None


# ── Per-error-type resolvers ──────────────────────────────────────────────────

def _resolve_typescript(parsed: dict, key_lines: list) -> List[FileSnippet]:
    """
    TypeScript errors carry the file + line in the compiler output:
        src/components/Hero.tsx(28,4): error TS2339: Property 'x' does not exist
    We extract the file name and line number, find the file in the repo,
    and return a ±WINDOW window around line 28.
    """
    snippets: List[FileSnippet] = []
    sources = key_lines + [parsed.get("error_message", "")]

    for src in sources:
        m = re.search(r"([\w./\-]+\.tsx?)\((\d+),\d+\)", src)
        if not m:
            continue
        ts_rel  = m.group(1).lstrip("./")
        line_no = int(m.group(2))
        for found in _find(Path(ts_rel).name)[:2]:
            content = _read(found)
            snippets.append(FileSnippet(
                file=_rel(found), abs_path=found, line_no=line_no,
                snippet=_window(content, line_no),
                reason=f"TypeScript compiler error on line {line_no}: "
                       f"{parsed.get('error_message','')[:120]}",
            ))
        if snippets:
            break

    return snippets


def _resolve_java(parsed: dict, key_lines: list) -> List[FileSnippet]:
    """
    Maven/Java errors report the file + line like:
        [ERROR] /workspace/.../Foo.java:[42,5] cannot find symbol
    We also handle "symbol: class Bar" → search for Bar.java and show its declaration.
    """
    snippets: List[FileSnippet] = []
    sources = key_lines + [parsed.get("error_message", "")]

    # Primary: file:line pattern from Maven output
    for src in sources:
        m = re.search(r"([\w/.\-]+\.java)[:\[(](\d+)[,\d]*[):\]]", src)
        if not m:
            continue
        java_rel = m.group(1)
        line_no  = int(m.group(2))
        for found in _find(Path(java_rel).name)[:2]:
            content = _read(found)
            snippets.append(FileSnippet(
                file=_rel(found), abs_path=found, line_no=line_no,
                snippet=_window(content, line_no),
                reason=f"Java compile error at line {line_no}",
            ))
        if snippets:
            break

    # Fallback: "symbol: class FooBar" — show FooBar.java head (package+imports)
    if not snippets:
        for src in key_lines:
            m = re.search(r"symbol\s*:\s*(?:class|method|variable)\s+(\w+)", src)
            if not m:
                continue
            symbol = m.group(1)
            for found in _find(f"{symbol}.java")[:1]:
                content = _read(found)
                head = "\n".join(
                    f"   {i:4d} | {ln}"
                    for i, ln in enumerate(content.splitlines()[:40], 1)
                )
                snippets.append(FileSnippet(
                    file=_rel(found), abs_path=found, line_no=None,
                    snippet=head,
                    reason=f"Definition of unresolved symbol '{symbol}'",
                ))
            if snippets:
                break

    # Fallback 2: Maven artifact cannot be resolved → look in pom.xml
    if not snippets:
        snippets += _resolve_maven(parsed, key_lines)

    return snippets


def _resolve_npm(parsed: dict, key_lines: list) -> List[FileSnippet]:
    """
    For missing npm modules, return the dependencies + devDependencies
    sections from all relevant package.json files (not the whole file).
    """
    snippets: List[FileSnippet] = []

    # Determine the missing package name
    pkg: Optional[str] = None
    sources = [parsed.get("error_message", "")] + key_lines
    for src in sources:
        m = (re.search(r"Missing npm package:\s*([^\s]+)", src) or
             re.search(r"[Cc]an'?t resolve ['\"]([^'\"./][^'\"]+)['\"]", src) or
             re.search(r"Module not found[^'\"]*['\"]([^'\"]+)['\"]", src))
        if m:
            pkg = m.group(1)
            break

    for pj in _find("package.json"):
        content = _read(pj)
        try:
            data = json.loads(content)
        except Exception:
            continue
        # Only project root package.json files (have name or scripts)
        if "name" not in data and "scripts" not in data:
            continue

        sec_dep  = _section(content, '"dependencies"')
        sec_dev  = _section(content, '"devDependencies"')
        snippet  = ""
        if sec_dep:
            snippet += f"# dependencies\n{sec_dep}\n"
        if sec_dev:
            snippet += f"\n# devDependencies\n{sec_dev}"

        reason = (
            f"'{pkg}' is missing from dependencies — add it here"
            if pkg else "Check dependency declarations"
        )
        snippets.append(FileSnippet(
            file=_rel(pj), abs_path=pj, line_no=None,
            snippet=snippet.strip(), reason=reason,
        ))

    return snippets[:2]   # max 2 package.json files


def _resolve_maven(parsed: dict, key_lines: list) -> List[FileSnippet]:
    """
    For Maven resolution failures, find the pom.xml that declares the
    failing dependency and extract just that <dependency> block.
    """
    snippets: List[FileSnippet] = []
    artifact_id: Optional[str] = None

    sources = [parsed.get("error_message", "")] + key_lines
    for src in sources:
        m = re.search(r"[\w.\-]+:([\w.\-]+):[\w.\-]+", src)
        if m:
            artifact_id = m.group(1)
            break

    for pom in _find("pom.xml")[:6]:
        content = _read(pom)
        if artifact_id and artifact_id not in content:
            continue
        keyword = artifact_id or "dependency"
        sec = _section(content, keyword)
        if not sec:
            continue
        snippets.append(FileSnippet(
            file=_rel(pom), abs_path=pom, line_no=None,
            snippet=sec,
            reason=(f"pom.xml declares '{artifact_id}' — pin to a release version (no SNAPSHOT)"
                    if artifact_id else "pom.xml dependency block"),
        ))

    return snippets[:2]


def _resolve_apache(parsed: dict, key_lines: list) -> List[FileSnippet]:
    """
    Apache config errors include the conf/vhost file and line number in the
    error message. We extract them and show the surrounding lines.

    Also handles 'Undefined variable' — finds every .conf/.vhost that uses
    ${VAR_NAME} and shows that line in context.
    """
    snippets: List[FileSnippet] = []
    errors   = parsed.get("errors", [])

    # ── Undefined variable ──────────────────────────────────────
    for err in errors:
        detail = err.get("detail", "") or err.get("message", "")
        var_m  = re.search(r"Undefined variable.*?(\w+)", detail)
        if var_m:
            var_name = var_m.group(1)
            pattern  = r"\$\{?" + re.escape(var_name) + r"\}?"
            for conf in _find("*.conf") + _find("*.vhost"):
                content = _read(conf)
                line_no = _grep_line(content, pattern)
                if line_no:
                    snippets.append(FileSnippet(
                        file=_rel(conf), abs_path=conf, line_no=line_no,
                        snippet=_window(content, line_no, w=8),
                        reason=(f"${{{var_name}}} is used here but not defined in any "
                                f".vars file — add 'Define {var_name} <value>' to your .vars file"),
                    ))

    # ── Explicit file path in the error ─────────────────────────
    for err in errors:
        detail = err.get("detail", "") or err.get("message", "")
        file_m = re.search(r"([\w/.\-]+\.(conf|vhost|any))(?::(\d+))?", detail)
        if file_m:
            name    = Path(file_m.group(1)).name
            line_no = int(file_m.group(3)) if file_m.group(3) else None
            for found in _find(name)[:1]:
                content = _read(found)
                snip = _window(content, line_no, w=10) if line_no else content[:800]
                snippets.append(FileSnippet(
                    file=_rel(found), abs_path=found, line_no=line_no,
                    snippet=snip,
                    reason=f"Apache config error: {detail[:120]}",
                ))

    # ── Key lines with file:line pattern ────────────────────────
    for kl in key_lines:
        m = re.search(r"([\w/.\-]+\.(conf|vhost|any)):(\d+)", kl)
        if m:
            name    = Path(m.group(1)).name
            line_no = int(m.group(3))
            for found in _find(name)[:1]:
                content = _read(found)
                snippets.append(FileSnippet(
                    file=_rel(found), abs_path=found, line_no=line_no,
                    snippet=_window(content, line_no, w=10),
                    reason=f"Apache config syntax error at line {line_no}",
                ))

    return snippets[:3]


def _resolve_security(parsed: dict, key_lines: list) -> List[FileSnippet]:
    """
    Security test failures are typically caused by risky OSGi bundles being
    enabled. We find the XML config file and show the lines that enable them.
    """
    snippets: List[FileSnippet] = []
    risky = {
        "com.adobe.granite.crxde-support": "CRXDE Lite is enabled — must be disabled on prod",
        "org.apache.sling.jcr.davex":      "DavEx (JCR WebDAV) is enabled — disable it",
        "org.apache.sling.jcr.webdav":     "WebDAV is enabled — disable it",
    }
    for xml_path in _find("*.xml"):
        if "ui.config" not in xml_path and "ui.apps" not in xml_path:
            continue
        content = _read(xml_path)
        for bundle_id, label in risky.items():
            if bundle_id not in content:
                continue
            line_no = _grep_line(content, re.escape(bundle_id))
            if line_no:
                snippets.append(FileSnippet(
                    file=_rel(xml_path), abs_path=xml_path, line_no=line_no,
                    snippet=_window(content, line_no, w=8),
                    reason=f"{label} — this causes securityTest to cancel the pipeline",
                ))
    return snippets[:3]


def _resolve_quality_gate(parsed: dict, key_lines: list) -> List[FileSnippet]:
    """
    Quality gate failures — show sonar / quality profile section in root pom.xml.
    """
    snippets: List[FileSnippet] = []
    for pom in _find("pom.xml")[:1]:   # root pom only
        content = _read(pom)
        sec = _section(content, "sonar") or _section(content, "quality")
        if sec:
            snippets.append(FileSnippet(
                file=_rel(pom), abs_path=pom, line_no=None,
                snippet=sec,
                reason="Sonar/quality gate configuration in root pom.xml",
            ))
    return snippets


# ── Dispatcher variable whitelist (same as code_analyzer) ────────────────────

_AMS_VARS = {
    "DISP_LOG_LEVEL", "REWRITE_LOG_LEVEL", "DISP_ID",
    "PUBLISH_IP", "PUBLISH_FQDN", "PUBLISH_DEFAULT_HOSTNAME",
    "PUBLISH_DOCROOT", "PUBLISH_PORT", "PUBLISH_FORCE_SSL",
    "PUBLISH_WHITELIST_ENABLED",
    "AUTHOR_IP", "AUTHOR_FQDN", "AUTHOR_DEFAULT_HOSTNAME",
    "AUTHOR_DOCROOT", "AUTHOR_PORT", "AUTHOR_FORCE_SSL",
    "AUTHOR_WHITELIST_ENABLED",
    "LIVECYCLE_IP", "LIVECYCLE_PORT", "LIVECYCLE_DOCROOT",
    "LIVECYCLE_DEFAULT_HOSTNAME", "LIVECYCLE_WHITELIST_ENABLED", "LIVECYCLE_FORCE_SSL",
    "EXPIRATION_TIME", "CRX_FILTER", "ENV",
}


# ── Public API ────────────────────────────────────────────────────────────────

_DISPATCH: Dict[str, any] = {
    "typescript_error":           _resolve_typescript,
    "java_compile_error":         _resolve_java,
    "build_failure":              _resolve_java,
    "maven_error":                _resolve_maven,
    "missing_npm_module":         _resolve_npm,
    "npm_build_failed":           _resolve_npm,
    "npm_error":                  _resolve_npm,
    "apache_config_syntax_error": _resolve_apache,
    "missing_env_variable":       _resolve_apache,
    "dispatcher_error":           _resolve_apache,
    "security_failure":           _resolve_security,
    "osgi_error":                 _resolve_security,
    "quality_gate_failure":       _resolve_quality_gate,
}

# Step-level fallbacks when error_type is generic / unknown
_STEP_FALLBACK: Dict[str, str] = {
    "build":        "build_failure",
    "deploy":       "apache_config_syntax_error",
    "securityTest": "security_failure",
    "codeQuality":  "quality_gate_failure",
}


def resolve_snippets(parsed_error: dict | object, step: str = "") -> List[FileSnippet]:
    """
    Main entry point.

    Given a parsed error dict (or LogParseResult Pydantic model) and the
    pipeline step name, return the minimal list of FileSnippet objects
    that the LLM needs to identify and fix the root cause.

    Each FileSnippet contains:
      - file       : relative path from repo root
      - line_no    : exact error line number (if determinable)
      - snippet    : ±WINDOW lines of code, >>> marks the error line
      - reason     : human-readable explanation of why this file was selected
    """
    if hasattr(parsed_error, "model_dump"):
        parsed_error = parsed_error.model_dump()

    error_type = parsed_error.get("error_type", "")
    key_lines  = parsed_error.get("key_lines", [])

    resolver = _DISPATCH.get(error_type)

    # If error_type is unknown or not in dispatch, try step-level fallback
    if resolver is None:
        fallback_type = _STEP_FALLBACK.get(step)
        if fallback_type:
            resolver = _DISPATCH.get(fallback_type)

    if resolver is None:
        return []

    return resolver(parsed_error, key_lines)


def format_for_llm(snippets: List[FileSnippet]) -> str:
    """
    Render snippets into a compact, clearly labeled block for the LLM prompt.

    Example output:
    ──────────────────────────────────────────────────────
    FILE: ui.apps/src/main/content/.../config.xml  [line 42]
    WHY: CRXDE Lite is enabled — causes securityTest failure
    ```xml
       40 |   <property name="enabled">
       41 |     <value>true</value>
    >>> 42 |   </property>
       43 |   <property name="pid">
    ```
    ──────────────────────────────────────────────────────
    """
    if not snippets:
        return (
            "(No specific file location could be determined from the error output. "
            "Analyse the error message and key log lines above to identify the root cause.)"
        )

    parts = []
    for s in snippets:
        loc  = f"line {s.line_no}" if s.line_no else "relevant section"
        lang = _guess_lang(s.file)
        parts.append(
            f"FILE: {s.file}  [{loc}]\n"
            f"WHY SELECTED: {s.reason}\n"
            f"```{lang}\n{s.snippet}\n```"
        )
    return "\n\n" + ("\n\n" + "─" * 60 + "\n\n").join(parts)


def _guess_lang(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {
        ".java": "java", ".ts": "typescript", ".tsx": "typescript",
        ".xml":  "xml",  ".conf": "apache",   ".vhost": "apache",
        ".json": "json", ".any":  "apache",   ".vars":  "apache",
    }.get(ext, "")
