"""
Log parser — Layer 2 of the data processing pipeline.
Converts raw Azure log text into structured LogParseResult objects.
key_lines = 5-10 most diagnostic lines — the ONLY log content sent to the LLM.
"""
from __future__ import annotations

import re
from typing import List

from parsers.models import ErrorEntry, LogParseResult


# ── Shared helpers ────────────────────────────────────────────────────────────

def _extract_key_lines(lines: List[str], patterns: List[str], max_lines: int = 10) -> List[str]:
    """Return up to max_lines unique lines that match any pattern."""
    seen: set = set()
    result: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        for pat in patterns:
            if re.search(pat, stripped, re.IGNORECASE):
                if stripped not in seen:
                    seen.add(stripped)
                    result.append(stripped)
                break
        if len(result) >= max_lines:
            break
    return result


# ── Build log ─────────────────────────────────────────────────────────────────

def parse_build_log(log_text: str) -> LogParseResult:
    lines = log_text.splitlines()
    key_lines = _extract_key_lines(lines, [
        r"\[ERROR\]", r"BUILD FAILURE", r"Cannot find module", r"Module not found",
        r"COMPILATION ERROR", r"cannot find symbol", r"npm ERR!", r"error TS\d+",
        r"OutOfMemoryError", r"error: package .+ does not exist", r"Failed to run task",
    ])

    # Missing npm module
    m = re.search(r"Module not found: Error: Can't resolve '([^']+)' in '([^']+)'", log_text)
    if m:
        return LogParseResult(step="build", error_type="missing_npm_module",
            error_message=f"Missing npm package: {m.group(1)}",
            module=m.group(2).split("/")[-1],
            key_lines=key_lines, raw_line_count=len(lines))

    # npm task failed
    m = re.search(r"Failed to run task: '(.+?)' failed", log_text)
    if m:
        return LogParseResult(step="build", error_type="npm_build_failed",
            error_message=f"npm task failed: {m.group(1)}",
            key_lines=key_lines, raw_line_count=len(lines))

    # TypeScript error
    m = re.search(r"error TS(\d+): (.+)", log_text)
    if m:
        return LogParseResult(step="build", error_type="typescript_error",
            error_message=f"TS{m.group(1)}: {m.group(2)[:200]}",
            key_lines=key_lines, raw_line_count=len(lines))

    # Java compile error
    m = re.search(r"COMPILATION ERROR|cannot find symbol|error: package (.+?) does not exist", log_text)
    if m:
        return LogParseResult(step="build", error_type="java_compile_error",
            error_message=m.group(0)[:200],
            key_lines=key_lines, raw_line_count=len(lines))

    # OOM
    if "OutOfMemoryError" in log_text:
        return LogParseResult(step="build", error_type="oom",
            error_message="Java OutOfMemoryError during build",
            key_lines=key_lines, raw_line_count=len(lines))

    # Generic BUILD FAILURE — grab [ERROR] line just before it
    for i, line in enumerate(lines):
        if "BUILD FAILURE" in line:
            for j in range(i - 1, max(i - 10, 0), -1):
                if "[ERROR]" in lines[j]:
                    return LogParseResult(step="build", error_type="build_failure",
                        error_message=lines[j].strip()[:300],
                        key_lines=key_lines, raw_line_count=len(lines))

    return LogParseResult(step="build", error_type="build_failure",
        error_message="BUILD FAILURE — could not extract specific error",
        key_lines=key_lines, raw_line_count=len(lines))


# ── Security test log ─────────────────────────────────────────────────────────

def parse_security_test_log(log_text: str) -> LogParseResult:
    lines = log_text.splitlines()
    key_lines = _extract_key_lines(lines, [r"- Failed", r"WARN -", r"ERROR"])

    failures: List[ErrorEntry] = []
    nodes: List[str] = []
    current_node = None

    for line in lines:
        s = line.strip()
        if not s:
            continue
        if (not s.startswith(" ") and not s.startswith("-")
                and not any(w in s for w in ("Failed", "Passed", "WARN", "INFO"))):
            current_node = s
            if current_node not in nodes:
                nodes.append(current_node)
        if "- Failed" in s:
            failures.append(ErrorEntry(type="security_check_failed",
                detail=s.replace("- Failed", "").strip()))
        if s.startswith("WARN -") and failures:
            reason = s.replace("WARN -", "").strip()
            f = failures[-1]
            failures[-1] = ErrorEntry(type=f.type, detail=f.detail, file_path=reason)

    check_counts: dict = {}
    for f in failures:
        check_counts[f.detail] = check_counts.get(f.detail, 0) + 1
    all_node_failures = [c for c, n in check_counts.items()
                         if len(nodes) > 0 and n == len(nodes)]

    msg = f"{len(failures)} security checks failed across {len(nodes)} nodes"
    if all_node_failures:
        msg += f". All-node failures: {', '.join(all_node_failures[:3])}"

    return LogParseResult(step="securityTest", error_type="security_failure",
        error_message=msg, errors=failures[:20],
        key_lines=key_lines, raw_line_count=len(lines))


# ── Deploy log ────────────────────────────────────────────────────────────────

def parse_deploy_log(log_text: str) -> LogParseResult:
    lines = log_text.splitlines()
    key_lines = _extract_key_lines(lines, [
        r"Syntax error", r"not defined", r"Failed to deploy",
        r"\[error\]", r"AH\d+", r"invalid command", r"undefined variable",
    ])

    errors: List[ErrorEntry] = []

    for m in re.finditer(r"Config variables are not defined: (\S+)", log_text):
        errors.append(ErrorEntry(type="missing_env_variable",
            detail=f"Undefined variable: {m.group(1)}"))

    for m in re.finditer(r"Syntax error on line (\d+) of ([^\n:]+): (.+)", log_text):
        errors.append(ErrorEntry(type="apache_config_syntax_error",
            detail=f"Line {m.group(1)} in {m.group(2).strip()}: {m.group(3).strip()}",
            file_path=m.group(2).strip(), line_no=int(m.group(1))))

    failed_instances = re.findall(r"Failed to deploy dispatcher on instance (\S+)", log_text)
    error_type = errors[0].type if errors else "deploy_failure"
    error_message = (errors[0].detail if errors
        else f"Deploy failed on: {', '.join(failed_instances)}" if failed_instances
        else "Deployment failed — no specific error extracted")

    return LogParseResult(step="deploy", error_type=error_type,
        error_message=error_message, errors=errors,
        key_lines=key_lines, raw_line_count=len(lines))


# ── Load test log ─────────────────────────────────────────────────────────────

def parse_load_test_log(log_text: str) -> LogParseResult:
    lines = log_text.splitlines()
    key_lines = _extract_key_lines(lines, [
        r"FAILED", r"threshold", r"p\d+\s*=", r"response time", r"error rate",
    ])

    errors: List[ErrorEntry] = []
    for m in re.finditer(r"FAILED\s+(.+?)\s+threshold", log_text, re.IGNORECASE):
        errors.append(ErrorEntry(type="threshold_exceeded",
            detail=f"Threshold failed: {m.group(1).strip()}"))
    m = re.search(r"p95\s*=\s*([\d.]+)ms", log_text)
    if m:
        errors.append(ErrorEntry(type="response_time_exceeded",
            detail=f"p95 response time: {m.group(1)}ms"))

    return LogParseResult(step="loadTest",
        error_type="load_test_failure" if errors else "load_test_unknown",
        error_message=errors[0].detail if errors else "Load test failed",
        errors=errors, key_lines=key_lines, raw_line_count=len(lines))


# ── Code quality log ──────────────────────────────────────────────────────────

def parse_code_quality_log(log_text: str) -> LogParseResult:
    lines = log_text.splitlines()
    key_lines = _extract_key_lines(lines, [
        r"ERROR", r"BLOCKER", r"CRITICAL", r"Quality Gate", r"violations", r"coverage",
    ])

    errors: List[ErrorEntry] = []
    m = re.search(r"Quality Gate (status|result)[:\s]+(\w+)", log_text, re.IGNORECASE)
    if m and m.group(2).upper() != "OK":
        errors.append(ErrorEntry(type="quality_gate_failure",
            detail=f"Quality Gate: {m.group(2)}"))
    for m in re.finditer(r"(\d+)\s+BLOCKER", log_text):
        errors.append(ErrorEntry(type="blocker_violation",
            detail=f"{m.group(1)} blocker violation(s)"))
    m = re.search(r"coverage.*?(\d+(?:\.\d+)?)\s*%.*?required.*?(\d+(?:\.\d+)?)\s*%",
                  log_text, re.IGNORECASE)
    if m:
        errors.append(ErrorEntry(type="coverage_insufficient",
            detail=f"Coverage {m.group(1)}% below required {m.group(2)}%"))

    return LogParseResult(step="codeQuality",
        error_type="code_quality_failure" if errors else "code_quality_unknown",
        error_message=errors[0].detail if errors else "Code quality check failed",
        errors=errors, key_lines=key_lines, raw_line_count=len(lines))


# ── Router ────────────────────────────────────────────────────────────────────

def parse_log(failed_step: str, log_text: str) -> LogParseResult:
    """Route to the correct parser. Always returns LogParseResult."""
    if not log_text or not log_text.strip():
        return LogParseResult(step=failed_step, error_type="no_log",
            error_message="Log text was empty or unavailable")

    if failed_step == "build":
        return parse_build_log(log_text)
    elif failed_step == "securityTest":
        return parse_security_test_log(log_text)
    elif failed_step == "deploy":
        return parse_deploy_log(log_text)
    elif failed_step == "loadTest":
        return parse_load_test_log(log_text)
    elif failed_step == "codeQuality":
        return parse_code_quality_log(log_text)
    else:
        clean = [l.strip() for l in log_text.splitlines() if l.strip()]
        return LogParseResult(step=failed_step, error_type="unparsed",
            error_message=log_text[:300], key_lines=clean[:5],
            raw_line_count=len(log_text.splitlines()))
