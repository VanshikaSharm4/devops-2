"""
Code Analyzer — two modes:

1. REACTIVE  : Given a failed execution's parsed log, pinpoint the exact
               file + line in the repo that caused it.

2. PROACTIVE : Scan the repo for patterns that historically cause failures
               in AEM Cloud Manager pipelines — before they actually break.
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

REPO_DIR = os.getenv("GIT_LOCAL_DIR", "/Users/vanshika/Downloads/idfc")

# ── Helpers ──────────────────────────────────────────────────

def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _find_files(pattern: str, root: str = REPO_DIR) -> List[str]:
    """Return all files matching a glob pattern under root."""
    return [str(p) for p in Path(root).rglob(pattern)
            if ".git" not in str(p)]


def _grep(text: str, pattern: str, flags: int = re.IGNORECASE) -> List[Dict]:
    """Return list of {line_no, line} for lines matching pattern."""
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        if re.search(pattern, line, flags):
            hits.append({"line_no": i, "line": line.rstrip()})
    return hits


def _rel(path: str) -> str:
    """Return path relative to REPO_DIR for clean display."""
    try:
        return str(Path(path).relative_to(REPO_DIR))
    except ValueError:
        return path


# ════════════════════════════════════════════════════════════
# PART 1 — REACTIVE: pinpoint what caused a specific failure
# ════════════════════════════════════════════════════════════

def pinpoint_failure(parsed_error, step: str = "") -> List[Dict[str, Any]]:
    """
    Given a parsed log error (LogParseResult model OR dict), search the repo
    for the exact file(s) and line(s) responsible.

    Returns list of findings:
      { file, line_no, line, reason, severity }
    """
    # Accept both LogParseResult Pydantic model and plain dict
    if hasattr(parsed_error, "model_dump"):
        parsed_error = parsed_error.model_dump()

    error_type = parsed_error.get("error_type", "unknown")
    message    = parsed_error.get("error_message", "")
    errors     = parsed_error.get("errors", [])
    key_lines  = parsed_error.get("key_lines", [])   # raw diagnostic log lines

    findings = []

    # ── Build failures ────────────────────────────────────────
    if step == "build" or error_type in ("build_failure", "maven_error", "npm_error",
                                          "missing_npm_module", "typescript_error",
                                          "java_compile_error", "npm_build_failed"):

        # NPM missing module → find package.json that should declare it
        # Matches both:
        #   parsed message: "Missing npm package: @scope/pkg"
        #   original log:   "Can't resolve '@scope/pkg' in '/path'"
        if error_type == "missing_npm_module":
            pkg_name = None
            m = re.search(r"Missing npm package:\s*([^\s]+)", message)
            if m:
                pkg_name = m.group(1)
            else:
                m = re.search(r"[Cc]an'?t resolve ['\"]([^'\"]+)['\"]", message)
                if m:
                    pkg_name = m.group(1).lstrip("./")
            # Also check key_lines for the original resolve error
            if not pkg_name:
                for kl in key_lines:
                    m = re.search(r"[Cc]an'?t resolve ['\"]([^'\"]+)['\"]", kl)
                    if m:
                        pkg_name = m.group(1).lstrip("./")
                        break

            if pkg_name:
                top_pkg = pkg_name.split("/")[0]  # @scope/name → @scope
                for pj in _find_files("package.json"):
                    content = _read(pj)
                    if top_pkg not in content:
                        findings.append({
                            "file": _rel(pj),
                            "line_no": None,
                            "line": f'"dependencies": {{ ... missing "{pkg_name}" ... }}',
                            "reason": f"package.json does not declare '{pkg_name}' — add it to dependencies",
                            "severity": "P1",
                        })

        # TypeScript error → extract file + line from message OR key_lines
        if error_type == "typescript_error":
            # Try message first (already cleaned by parser: "TS2339: Property X does not exist")
            # Then key_lines (contain original: "src/components/Hero.tsx(28,4): error TS2339")
            ts_match = None
            for source in [message] + key_lines:
                ts_match = re.search(r"([\w./\-]+\.tsx?)\((\d+),\d+\)", source)
                if ts_match:
                    break
            if ts_match:
                rel_path = ts_match.group(1).lstrip("./")
                line_no  = int(ts_match.group(2))
                matches  = _find_files(Path(rel_path).name)
                for mf in matches[:2]:
                    file_lines = _read(mf).splitlines()
                    bad_line   = file_lines[line_no - 1] if line_no <= len(file_lines) else ""
                    findings.append({
                        "file": _rel(mf),
                        "line_no": line_no,
                        "line": bad_line.strip(),
                        "reason": f"TypeScript error: {message[:200]}",
                        "severity": "P1",
                    })

        # Java / Maven compile error
        # Azure log format:  [ERROR] /workspace/core/src/main/java/com/idfc/MyClass.java:[42,5] cannot find symbol
        # Also try key_lines which contain the raw log lines
        if error_type in ("java_compile_error", "build_failure"):
            java_match = None
            for source in [message] + key_lines:
                # Pattern: path/to/File.java:[line,col] or path/to/File.java(line,col)
                java_match = re.search(
                    r"([\w/.\-]+\.java)[:\[(](\d+)[,\d]*[):\]]", source
                )
                if java_match:
                    break
            if java_match:
                src_file = java_match.group(1)
                line_no  = int(java_match.group(2))
                matches  = _find_files(Path(src_file).name)
                for mf in matches[:2]:
                    file_lines = _read(mf).splitlines()
                    bad_line   = file_lines[line_no - 1] if line_no <= len(file_lines) else ""
                    findings.append({
                        "file": _rel(mf),
                        "line_no": line_no,
                        "line": bad_line.strip(),
                        "reason": f"Java compile error at line {line_no}: {message[:200]}",
                        "severity": "P1",
                    })
            # Also look for "cannot find symbol" / "package does not exist" in key_lines
            # and find the symbol in the codebase
            if not findings:
                symbol = None
                for kl in key_lines:
                    m = re.search(r"symbol\s*:\s*(?:class|method|variable)\s+(\w+)", kl)
                    if m:
                        symbol = m.group(1)
                        break
                if symbol:
                    for java_file in _find_files("*.java"):
                        hits = _grep(_read(java_file), r"\b" + re.escape(symbol) + r"\b")
                        for h in hits[:1]:
                            findings.append({
                                "file": _rel(java_file),
                                "line_no": h["line_no"],
                                "line": h["line"].strip(),
                                "reason": f"References '{symbol}' which cannot be resolved",
                                "severity": "P1",
                            })

        # Maven dependency resolution failure
        # "Could not resolve / does not exist" → find the <dependency> in pom.xml
        if "does not exist" in message or "Could not resolve" in message or "Could not find artifact" in message:
            dep = re.search(r"([\w.\-]+):([\w.\-]+):([\w.\-]+)", message)
            if not dep:
                for kl in key_lines:
                    dep = re.search(r"([\w.\-]+):([\w.\-]+):([\w.\-]+)", kl)
                    if dep:
                        break
            if dep:
                artifact_id = dep.group(2)
                for pom in _find_files("pom.xml"):
                    content = _read(pom)
                    hits = _grep(content, re.escape(artifact_id))
                    for h in hits[:1]:
                        findings.append({
                            "file": _rel(pom),
                            "line_no": h["line_no"],
                            "line": h["line"].strip(),
                            "reason": f"Maven dependency {dep.group(0)} cannot be resolved — check version or repository config",
                            "severity": "P1",
                        })

    # ── Deploy / Dispatcher failures ──────────────────────────
    if step == "deploy" or error_type in ("apache_config_syntax_error", "dispatcher_error"):
        for err in errors:
            detail = err.get("detail", "") or err.get("message", "")

            # Undefined variable in Apache config
            var_match = re.search(r"Undefined variable.*?(\w+)", detail)
            if var_match:
                var_name = var_match.group(1)
                for conf in _find_files("*.conf") + _find_files("*.vhost"):
                    content = _read(conf)
                    hits = _grep(content, r"\$\{?" + re.escape(var_name))
                    for h in hits[:2]:
                        findings.append({
                            "file": _rel(conf),
                            "line_no": h["line_no"],
                            "line": h["line"],
                            "reason": f"Variable ${var_name} is used but never defined",
                            "severity": "P1",
                        })

            # Include path that doesn't exist
            include_match = re.search(r"(AH00526|cannot open).*(conf\.[^\s]+)", detail)
            if include_match:
                inc_path = include_match.group(2)
                findings.append({
                    "file": inc_path,
                    "line_no": None,
                    "line": f"Include {inc_path}",
                    "reason": f"Included config path does not exist on the dispatcher",
                    "severity": "P1",
                })

        # Direct grep in conf files for the error keyword
        if message:
            keyword = message.split(":")[0].strip()[:40]
            if keyword:
                for conf in _find_files("*.conf"):
                    content = _read(conf)
                    hits = _grep(content, re.escape(keyword))
                    for h in hits[:1]:
                        findings.append({
                            "file": _rel(conf),
                            "line_no": h["line_no"],
                            "line": h["line"],
                            "reason": f"Config line linked to deploy error: {message[:120]}",
                            "severity": "P1",
                        })

    # ── SecurityTest failures ─────────────────────────────────
    if step == "securityTest" or error_type in ("security_failure", "osgi_error"):
        # Find OSGi config XMLs that reference the failing bundle
        for err in errors:
            detail = err.get("detail", "") or err.get("bundle", "") or ""
            bundle_match = re.search(r"(com\.adobe\.[^\s,]+|com\.day\.[^\s,]+)", detail)
            if bundle_match:
                bundle_id = bundle_match.group(1)
                for xml_file in _find_files("*.xml"):
                    content = _read(xml_file)
                    if bundle_id in content:
                        hits = _grep(content, re.escape(bundle_id))
                        for h in hits[:1]:
                            findings.append({
                                "file": _rel(xml_file),
                                "line_no": h["line_no"],
                                "line": h["line"],
                                "reason": f"OSGi bundle reference to '{bundle_id}' — check it is active",
                                "severity": "P1",
                            })

    return findings


# ════════════════════════════════════════════════════════════
# PART 2 — PROACTIVE: scan entire repo for risk patterns
# ════════════════════════════════════════════════════════════

def _load_defined_vars() -> set:
    """
    Read ALL .vars files in the repo and return the set of variable names
    that are actually defined. These are safe to use in .conf/.vhost files.
    """
    defined = set()
    for vars_file in _find_files("*.vars"):
        for line in _read(vars_file).splitlines():
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            # Apache Define syntax:  Define VARNAME value
            m = re.match(r"Define\s+(\w+)", line)
            if m:
                defined.add(m.group(1))
    return defined


def scan_repo() -> List[Dict[str, Any]]:
    """
    Scan the IDFC repo for patterns that commonly cause Cloud Manager
    pipeline failures. Returns a list of risk findings.
    """
    risks = []
    risks += _scan_package_json()
    risks += _scan_pom_xml()
    risks += _scan_dispatcher_configs()
    risks += _scan_ui_config()
    return sorted(risks, key=lambda r: {"P1": 0, "P2": 1, "P3": 2}.get(r["severity"], 3))


# ── package.json scanners ─────────────────────────────────────

def _scan_package_json() -> List[Dict]:
    risks = []
    for pj_path in _find_files("package.json"):
        if "node_modules" in pj_path:
            continue
        try:
            data = json.loads(_read(pj_path))
        except Exception:
            continue

        all_deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        rel = _rel(pj_path)

        for pkg, ver in all_deps.items():
            # Wildcard versions — will pull unexpected breaking changes
            if ver in ("*", "latest", ""):
                risks.append({
                    "file": rel, "line_no": None,
                    "pattern": f'"{pkg}": "{ver}"',
                    "reason": f"Wildcard version '{ver}' for '{pkg}' — Cloud Manager build will "
                               "pull a different version each time, causing random failures",
                    "severity": "P2",
                    "fix": f"Pin to an exact version, e.g. \"1.2.3\"",
                })

            # Very old pinned webpack / babel (known breakage range)
            if pkg == "webpack" and ver.startswith(("^3.", "^2.", "3.", "2.")):
                risks.append({
                    "file": rel, "line_no": None,
                    "pattern": f'"webpack": "{ver}"',
                    "reason": "Webpack 2/3 is EOL. npm peer dependency conflicts will break the build",
                    "severity": "P2",
                    "fix": "Upgrade to webpack 5",
                })

        # Scripts that reference non-existent local files
        scripts = data.get("scripts", {})
        for name, cmd in scripts.items():
            # webpack configs referenced in scripts
            conf_refs = re.findall(r"--config\s+([\w./]+\.js)", cmd)
            for conf_ref in conf_refs:
                conf_abs = str(Path(pj_path).parent / conf_ref)
                if not Path(conf_abs).exists():
                    risks.append({
                        "file": rel, "line_no": None,
                        "pattern": f'"scripts.{name}": "{cmd[:80]}"',
                        "reason": f"Script '{name}' references '{conf_ref}' which does not exist",
                        "severity": "P1",
                        "fix": f"Create the file or fix the path in scripts.{name}",
                    })

    return risks


# ── pom.xml scanners ──────────────────────────────────────────

def _scan_pom_xml() -> List[Dict]:
    risks = []
    for pom_path in _find_files("pom.xml"):
        content = _read(pom_path)
        rel = _rel(pom_path)

        # Only flag SNAPSHOT inside <dependency> blocks — NOT the project's own <version>
        # Parse properly: look for <dependency>...</dependency> blocks that contain SNAPSHOT
        dep_blocks = re.findall(
            r"<dependency>(.*?)</dependency>", content, re.DOTALL
        )
        for block in dep_blocks:
            ver_match = re.search(r"<version>([^<]*SNAPSHOT[^<]*)</version>", block)
            if ver_match:
                art_match = re.search(r"<artifactId>([^<]+)</artifactId>", block)
                artifact = art_match.group(1) if art_match else "unknown"
                # Find the line number in the full file
                ver_line = ver_match.group(0)
                hits = _grep(content, re.escape(ver_line))
                line_no = hits[0]["line_no"] if hits else None
                risks.append({
                    "file": rel, "line_no": line_no,
                    "pattern": ver_line.strip(),
                    "reason": f"Dependency '{artifact}' uses a SNAPSHOT version — "
                               "the artifact can change without notice between builds, "
                               "causing non-reproducible failures",
                    "severity": "P2",
                    "fix": f"Pin '{artifact}' to a specific release version",
                })

    return risks


# ── Dispatcher / Apache config scanners ───────────────────────

def _scan_dispatcher_configs() -> List[Dict]:
    # Load all variables that are actually defined in the repo's .vars files
    defined_vars = _load_defined_vars()

    # AMS always provisions these via /etc/sysconfig/httpd — safe even if not in .vars files
    # Source: Adobe AMS Dispatcher documentation + standard AMS baseline configuration
    ams_infrastructure_vars = {
        # Log / debug
        "DISP_LOG_LEVEL", "REWRITE_LOG_LEVEL", "DISP_ID",
        # Publish tier
        "PUBLISH_IP", "PUBLISH_FQDN", "PUBLISH_DEFAULT_HOSTNAME",
        "PUBLISH_DOCROOT", "PUBLISH_PORT", "PUBLISH_FORCE_SSL",
        "PUBLISH_WHITELIST_ENABLED",
        # Author tier
        "AUTHOR_IP", "AUTHOR_FQDN", "AUTHOR_DEFAULT_HOSTNAME",
        "AUTHOR_DOCROOT", "AUTHOR_PORT", "AUTHOR_FORCE_SSL",
        "AUTHOR_WHITELIST_ENABLED",
        # LiveCycle (Adobe Forms)
        "LIVECYCLE_IP", "LIVECYCLE_PORT", "LIVECYCLE_DOCROOT",
        "LIVECYCLE_DEFAULT_HOSTNAME", "LIVECYCLE_WHITELIST_ENABLED", "LIVECYCLE_FORCE_SSL",
        # Misc AMS
        "EXPIRATION_TIME", "CRX_FILTER",
        # ENV is set by AMS to "stage" or "prod" — used to pick the right .vars file
        "ENV",
    }
    all_safe_vars = defined_vars | ams_infrastructure_vars

    risks = []
    conf_files = _find_files("*.conf") + _find_files("*.vhost") + _find_files("*.any")

    for conf_path in conf_files:
        if ".git" in conf_path:
            continue
        content = _read(conf_path)
        rel = _rel(conf_path)

        # Variables used but NOT defined anywhere in the repo OR by AMS infrastructure
        var_uses = re.findall(r"\$\{(\w+)\}", content)
        for var in set(var_uses):
            if var not in all_safe_vars:
                hits = _grep(content, r"\$\{" + re.escape(var) + r"\}")
                for h in hits[:1]:
                    risks.append({
                        "file": rel, "line_no": h["line_no"],
                        "pattern": h["line"].strip(),
                        "reason": f"Variable ${{{var}}} is used but not defined in any "
                                   ".vars file in the repo — deploy will fail with 'Undefined variable'",
                        "severity": "P1",
                        "fix": f"Add 'Define {var} <value>' to the appropriate "
                                ".vars file (stage/prod/ams_default)",
                    })

        # NOTE: /etc/httpd/ Include paths are standard AMS pattern — NOT flagged.
        # AMS provisions /etc/httpd/ on all dispatcher instances.

        # AllowOverride All inside a DocumentRoot (genuine security scan trigger)
        # but skip if it's clearly in a non-web-root context
        for h in _grep(content, r"AllowOverride\s+All"):
            # Only flag if it's inside a <Directory "/var/www" or similar web root
            context_start = max(0, h["line_no"] - 10)
            context = "\n".join(content.splitlines()[context_start:h["line_no"]])
            if re.search(r'<Directory\s+"?/var/www|<Directory\s+"?/mnt', context):
                risks.append({
                    "file": rel, "line_no": h["line_no"],
                    "pattern": h["line"].strip(),
                    "reason": "AllowOverride All on a web root directory — "
                               "triggers AMS security scan failure",
                    "severity": "P1",
                    "fix": "Change to 'AllowOverride None'",
                })

    return risks


# ── ui.config OSGi config scanners ────────────────────────────

def _scan_ui_config() -> List[Dict]:
    risks = []

    for xml_path in _find_files("*.xml"):
        if "ui.config" not in xml_path and "ui.apps" not in xml_path:
            continue
        content = _read(xml_path)
        rel = _rel(xml_path)

        # CRXDE / DavEx / WebDAV left enabled (securityTest failure)
        risky_bundles = {
            "com.adobe.granite.crxde-support": "CRXDE Lite should be disabled on prod",
            "org.apache.sling.jcr.davex":      "DavEx (WebDAV for JCR) should be disabled",
            "org.apache.sling.jcr.webdav":      "WebDAV should be disabled on prod",
        }
        for bundle_id, reason in risky_bundles.items():
            if bundle_id in content:
                hits = _grep(content, re.escape(bundle_id))
                # Only flag if it looks like it's being enabled (not just referenced)
                for h in hits:
                    if "enabled" in h["line"].lower() or "true" in h["line"].lower() \
                       or "active" in h["line"].lower():
                        risks.append({
                            "file": rel, "line_no": h["line_no"],
                            "pattern": h["line"].strip(),
                            "reason": reason + " — causes securityTest cancellation",
                            "severity": "P1",
                            "fix": f"Set the bundle to disabled/inactive in this OSGi config",
                        })

        # runmode mismatch — configs in wrong folder
        # e.g., a prod config accidentally in a dev runmode folder
        if "author" in xml_path and "publish" in content:
            risks.append({
                "file": rel, "line_no": None,
                "pattern": "(runmode mismatch)",
                "reason": "Config is in an 'author' runmode folder but references 'publish' — "
                           "check the runmode targeting is intentional",
                "severity": "P3",
                "fix": "Move to the correct runmode subfolder",
            })

    return risks


# ════════════════════════════════════════════════════════════
# Entry points used by CLI
# ════════════════════════════════════════════════════════════

def run_scan(use_llm: bool = True) -> tuple:
    """
    Run proactive repo scan. Attaches a code snippet to every finding
    so the LLM sees the actual problematic lines, not just file paths.
    Returns (risks_list, markdown_report).
    """
    from analysis.file_resolver import _window, _read

    risks = scan_repo()
    if not risks:
        return [], "# Code Scan\n\nNo risks found."

    # Attach code snippets to each finding — LLM sees real code, not just paths
    for r in risks:
        file_abs = str(Path(REPO_DIR) / r["file"]) if not Path(r["file"]).is_absolute() else r["file"]
        line_no  = r.get("line_no")
        if line_no and Path(file_abs).exists():
            content      = _read(file_abs)
            r["snippet"] = _window(content, line_no, w=8)
        elif Path(file_abs).exists():
            # No specific line — show a small excerpt around the pattern
            content = _read(file_abs)
            pattern = r.get("pattern", "")
            if pattern:
                for i, ln in enumerate(content.splitlines(), 1):
                    if pattern[:40] in ln:
                        r["snippet"] = _window(content, i, w=8)
                        r["line_no"] = i
                        break
            if "snippet" not in r:
                r["snippet"] = "\n".join(
                    f"   {i:4d} | {ln}"
                    for i, ln in enumerate(content.splitlines()[:20], 1)
                )

    if not use_llm:
        return risks, _format_scan_markdown(risks)

    from agent.devops_agent import run_scan_markdown
    return risks, run_scan_markdown(risks)


def run_pinpoint(
    execution_id: str,
    use_llm: bool = True,
    failed_df=None,
    share_map=None,
) -> tuple:
    """
    Given an execution ID, fetch its parsed error and pinpoint the code.

    Pipeline:
      1. Load execution row from Splunk data
      2. Fetch the Azure log for the failed step (cached to disk)
      3. Parse the log → extract error_type, key_lines
      4. file_resolver.resolve_snippets() → find exact file + line number,
         extract ±15 line window with >>> marker (replaces whole-file grepping)
      5. Pass only the targeted snippets to the LLM (saves ~95% tokens)

    Pass failed_df and share_map from the dashboard to avoid redundant load_data() call.
    Returns (findings_list, markdown_report).
    """
    from analysis.file_resolver import resolve_snippets, format_for_llm
    from connectors.azure_connector import get_log_for_execution
    from parsers.log_parser import parse_log

    # Only call load_data() if caller didn't pass the already-loaded dataframes
    if failed_df is None or share_map is None:
        from analysis.ingest import load_data
        _, failed_df, _, share_map = load_data()

    row = failed_df[failed_df["executionId"].astype(str) == str(execution_id)]
    if row.empty:
        return [], f"Execution {execution_id} not found in failed executions."

    step    = str(row.iloc[0].get("firstFailedStep", ""))
    share   = share_map.get(str(execution_id))
    parsed  = {"error_type": "unknown", "error_message": "No log fetched"}

    if share and step and step != "nan":
        log_text = get_log_for_execution(share, step)
        result   = parse_log(step, log_text)
        parsed   = result.model_dump() if hasattr(result, "model_dump") else result

    # ── Smart file resolution (replaces broad grep) ───────────────────────────
    # resolve_snippets() returns only the files and line windows that are
    # directly relevant to this error — ready to be pasted into the LLM prompt.
    snippets = resolve_snippets(parsed, step=step)
    snippet_text = format_for_llm(snippets)

    # Convert FileSnippet objects → plain dicts for the findings return value
    findings = [
        {
            "file":    s.file,
            "line_no": s.line_no,
            "line":    f"line {s.line_no}" if s.line_no else "",
            "reason":  s.reason,
            "snippet": s.snippet,
        }
        for s in snippets
    ]

    if not use_llm:
        return findings, _format_pinpoint_markdown(execution_id, step, parsed, findings)

    from agent.devops_agent import run_pinpoint_markdown
    return findings, run_pinpoint_markdown(
        execution_id, step, parsed, findings,
        snippet_text=snippet_text,   # exact code window for the LLM
    )


def _format_scan_markdown(risks: List[dict]) -> str:
    p1 = [r for r in risks if r["severity"] == "P1"]
    p2 = [r for r in risks if r["severity"] == "P2"]
    p3 = [r for r in risks if r["severity"] == "P3"]

    lines = [
        "# Proactive Code Risk Scan",
        f"",
        f"**{len(p1)} P1 (will break) | {len(p2)} P2 (likely to break) | {len(p3)} P3 (watch)**",
        "",
    ]
    for severity, group in [("P1 — Will break deployment", p1),
                              ("P2 — Likely to cause failure", p2),
                              ("P3 — Watch", p3)]:
        if group:
            lines.append(f"## {severity}")
            for r in group:
                loc = f"`{r['file']}`" + (f" line {r['line_no']}" if r.get('line_no') else "")
                lines.append(f"\n**{loc}**")
                lines.append(f"> `{r['pattern']}`")
                lines.append(f"- **Problem:** {r['reason']}")
                lines.append(f"- **Fix:** {r.get('fix', 'See reason above')}")
    return "\n".join(lines)


def _format_pinpoint_markdown(exec_id: str, step: str, parsed: dict, findings: List[dict]) -> str:
    lines = [
        f"# Root Cause: Execution {exec_id} ({step})",
        f"",
        f"**Error type:** `{parsed.get('error_type')}`",
        f"**Message:** {parsed.get('error_message', '')[:300]}",
        f"",
        f"## Code Location(s)",
    ]
    if not findings:
        lines.append("_Could not automatically locate the source. Check the error message above._")
    for f in findings:
        loc = f"`{f['file']}`" + (f" line {f['line_no']}" if f.get("line_no") else "")
        lines.append(f"\n**{loc}**")
        if f.get("line"):
            lines.append(f"```\n{f['line']}\n```")
        lines.append(f"- **Why:** {f['reason']}")
        if f.get("fix"):
            lines.append(f"- **Fix:** {f['fix']}")
    return "\n".join(lines)
