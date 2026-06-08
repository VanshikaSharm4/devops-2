"""
DevOps Agent — Layer 7 of the data processing pipeline.
Calls the LLM and returns structured Pydantic output.

Flow per feature:
  context_builder  →  prompt (Layer 6)  →  LLM (Layer 7)  →  output model (Layer 8)
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Type, TypeVar

from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

load_dotenv()

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "azure_openai").lower()
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

T = TypeVar("T", bound=BaseModel)


# ── Per-feature LLM configuration ────────────────────────────────────────────

@dataclass
class LLMCallConfig:
    """
    Temperature and token settings tuned per feature.
    Low temperature = deterministic, factual output (risk, pinpoint).
    Higher temperature = more expressive summaries (report, narrative).
    """
    max_tokens: int = 4096
    temperature: float = 0.2
    json_mode: bool = True       # force json_object response format where supported


# Tuned per feature
CONFIGS: dict[str, LLMCallConfig] = {
    "report":    LLMCallConfig(max_tokens=4096, temperature=0.3, json_mode=True),
    "risk":      LLMCallConfig(max_tokens=4096, temperature=0.1, json_mode=True),
    "compare":   LLMCallConfig(max_tokens=2048, temperature=0.1, json_mode=True),
    "correlate": LLMCallConfig(max_tokens=2048, temperature=0.1, json_mode=True),
    "scan":      LLMCallConfig(max_tokens=3072, temperature=0.1, json_mode=True),
    "pinpoint":  LLMCallConfig(max_tokens=2048, temperature=0.1, json_mode=True),
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _redact_secrets(text: str) -> str:
    patterns = [
        (r"(?i)(api[_-]?key|password|secret|token)\s*[=:]\s*\S+", r"\1=***"),
        (r"DefaultEndpointsProtocol=https;AccountKey=[^;]+", "AccountKey=***"),
    ]
    for pat, repl in patterns:
        text = re.sub(pat, repl, text)
    return text


def _build_user_message(context: dict) -> str:
    """Wrap a compressed context dict in a standard user message."""
    return _redact_secrets(
        "Analyze the following data and return the JSON report as instructed.\n\n"
        + json.dumps(context, indent=2, default=str)
    )


def _extract_json(text: str) -> dict:
    """
    Strip markdown fences if present, then parse JSON.
    If parsing fails (e.g. truncated response), attempt to close open
    braces/brackets so we can recover a partial but structurally valid object.
    """
    text = text.strip()
    # Remove ```json ... ``` or ``` ... ```
    m = re.match(r"^```(?:json)?\s*([\s\S]+?)\s*```$", text, re.DOTALL)
    if m:
        text = m.group(1).strip()

    # First attempt — clean parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Truncation recovery: count unclosed braces/brackets, ignoring those
    # inside strings, then append the required closing chars.
    depth_brace = 0
    depth_bracket = 0
    in_string = False
    escape = False
    for ch in text:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace = max(0, depth_brace - 1)
        elif ch == "[":
            depth_bracket += 1
        elif ch == "]":
            depth_bracket = max(0, depth_bracket - 1)

    # Strip any trailing incomplete token (e.g. a cut-off string or value)
    # by trimming back to the last comma or colon boundary we can close cleanly.
    tail = text.rstrip()
    # Remove a dangling incomplete string or value at the very end
    tail = re.sub(r',\s*"[^"]*$', '', tail)   # cut trailing incomplete key/string
    tail = re.sub(r':\s*"[^"]*$', ': ""', tail)  # close incomplete string value
    tail = re.sub(r':\s*\[([^\]]*?)$', r': [\1]', tail)  # close incomplete array

    closing = "]" * depth_bracket + "}" * depth_brace
    recovered = tail + closing
    return json.loads(recovered)


def _schema_hint(model_class: Type[T]) -> str:
    """Return a compact JSON schema hint for retry messages."""
    try:
        schema = model_class.model_json_schema()
        props = list(schema.get("properties", {}).keys())
        required = schema.get("required", [])
        return f"Required fields: {required}. All fields: {props}"
    except Exception:
        return f"Must match {model_class.__name__} schema"


# ── LLM provider calls ────────────────────────────────────────────────────────

def _call_anthropic(system: str, user_message: str, cfg: LLMCallConfig) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=cfg.max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def _call_gemini(system: str, user_message: str, cfg: LLMCallConfig) -> str:
    import google.generativeai as genai
    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
    model = genai.GenerativeModel(
        model_name="gemini-1.5-pro",
        system_instruction=system,
    )
    response = model.generate_content(user_message)
    return response.text


def _call_azure_openai(system: str, user_message: str, cfg: LLMCallConfig) -> str:
    from openai import OpenAI
    client = OpenAI(
        base_url=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_KEY"),
    )
    kwargs: dict[str, Any] = dict(
        model=os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-nano"),
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ],
        max_completion_tokens=cfg.max_tokens,
        temperature=cfg.temperature,
    )
    # json_object mode forces valid JSON — only supported by Azure OpenAI
    if cfg.json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    response = client.chat.completions.create(**kwargs)
    return response.choices[0].message.content


def _call_llm(system: str, user_message: str, cfg: LLMCallConfig) -> str:
    print(f"  [{LLM_PROVIDER}] max_tokens={cfg.max_tokens} temp={cfg.temperature} json={cfg.json_mode}")
    if LLM_PROVIDER == "anthropic":
        return _call_anthropic(system, user_message, cfg)
    if LLM_PROVIDER == "gemini":
        return _call_gemini(system, user_message, cfg)
    if LLM_PROVIDER == "azure_openai":
        return _call_azure_openai(system, user_message, cfg)
    raise ValueError(f"Unknown LLM_PROVIDER: {LLM_PROVIDER}")


# ── Core structured runner ────────────────────────────────────────────────────

def _enrich_with_memory(
    user_message: str,
    error_type: str,
    error_message: str,
    key_lines: list,
    step: str = "",
    pipeline: str = "",
) -> str:
    """
    Query the vector store for similar past failures and prepend them
    to the user message so the LLM knows: 'this happened before, here is the fix.'
    Silently skips if vector store is unavailable or empty.
    Only returns records from the same pipeline — prevents Dev-pipeline
    failures bleeding into Production Pipeline predictions.
    """
    try:
        from vector_store.store import find_similar_failures
        hits = find_similar_failures(
            error_type=error_type,
            error_message=error_message,
            key_lines=key_lines,
            step=step,
            top_k=3,
            pipeline=pipeline,
        )
        if not hits:
            return user_message

        memory_block = "\n\nSIMILAR PAST FAILURES (from memory — use these to improve your answer):\n"
        for i, h in enumerate(hits, 1):
            memory_block += (
                f"\n[{i}] Execution {h.get('execution_id')} | "
                f"step:{h.get('step')} | {h.get('error_type')} | "
                f"similarity:{h.get('similarity_score')}\n"
                f"  Root cause: {h.get('root_cause', '')}\n"
                f"  Fix applied: {h.get('fix', '')}\n"
            )
        return user_message + memory_block
    except Exception:
        return user_message   # vector store optional — never crash the main flow


def run_structured(
    system: str,
    user_message: str,
    model_class: Type[T],
    cfg: LLMCallConfig,
    retries: int = 2,
) -> T:
    """
    Call LLM → parse JSON → validate against Pydantic model.
    On failure: retry up to `retries` times, adding schema hint each time.
    """
    last_err: Exception | None = None
    current_message = user_message

    for attempt in range(retries + 1):
        raw = _call_llm(system, current_message, cfg)
        try:
            data = _extract_json(raw)
            return model_class.model_validate(data)
        except json.JSONDecodeError as e:
            last_err = e
            hint = _schema_hint(model_class)
            current_message = (
                user_message
                + f"\n\n[Retry {attempt + 1}] Previous response was not valid JSON: {e}\n"
                + f"Schema hint: {hint}\n"
                + "Return ONLY a valid JSON object, no markdown fences."
            )
        except ValidationError as e:
            last_err = e
            # Summarise which fields were wrong
            missing = [err["loc"] for err in e.errors()]
            hint = _schema_hint(model_class)
            current_message = (
                user_message
                + f"\n\n[Retry {attempt + 1}] JSON parsed but failed validation. Missing/wrong fields: {missing}\n"
                + f"Schema hint: {hint}\n"
                + "Fix the fields and return ONLY valid JSON."
            )

    raise ValueError(f"LLM response could not be parsed after {retries + 1} attempts: {last_err}")


# ── Feature 1: Failure analysis report ───────────────────────────────────────

def run_analysis(context: dict, pipeline_df=None, failed_df=None) -> "FailureReport":
    """
    Feature 1: 30-day failure analysis report.
    context is the output of build_report_context() from context_builder.
    pipeline_df / failed_df are the raw Splunk DataFrames used to compute
    estimated_hours_wasted and business_impact — the LLM never generates these.
    """
    from models.output_models import FailureReport
    from analysis.context_builder import build_report_context

    # Accept both raw bundle and pre-built context dict
    if "top_failure_patterns" not in context and "failure_patterns" in context:
        context = build_report_context(context)

    system = _load_prompt("failure_report.md")
    report = run_structured(system, _build_user_message(context), FailureReport, CONFIGS["report"])

    # Compute wasted hours and per-finding business_impact from Splunk data
    _fill_wasted_hours(report, pipeline_df, failed_df)

    # Auto-store findings in vector memory — tagged with the dominant pipeline name
    try:
        from vector_store.store import ingest_failure_report
        # Extract the most common pipeline name from context so records are
        # properly scoped and never bleed across pipelines during RAG queries
        _pipeline_name = ""
        try:
            _patterns = context.get("top_failure_patterns", [])
            if _patterns:
                from collections import Counter
                _pipeline_name = Counter(
                    p.get("pipeline", "") for p in _patterns if p.get("pipeline")
                ).most_common(1)[0][0]
        except Exception:
            pass
        ingest_failure_report(report, pipeline_name=_pipeline_name)
    except Exception:
        pass

    return report


def _fill_wasted_hours(report, pipeline_df, failed_df) -> None:
    """Compute estimated_hours_wasted and business_impact from Splunk data."""
    import pandas as pd

    if pipeline_df is None:
        return

    try:
        fa = pipeline_df[pipeline_df["Status"].isin(["FAILED", "ERROR"])].copy()

        # Attach firstFailedStep from failed_df (deduplicated to avoid double-counting)
        if failed_df is not None and "firstFailedStep" in failed_df.columns and "firstFailedStep" not in fa.columns:
            step_map = failed_df.drop_duplicates("executionId")[["executionId", "firstFailedStep"]]
            fa = fa.merge(step_map, on="executionId", how="left")

        report.estimated_hours_wasted = round(fa["Duration (Min)"].fillna(0).sum() / 60, 1)

        for finding in report.critical_findings + report.recurring_findings:
            if "firstFailedStep" in fa.columns:
                sr = fa[fa["firstFailedStep"] == finding.step]
            else:
                sr = pd.DataFrame()
            total_min = round(sr["Duration (Min)"].fillna(0).sum())
            hrs = round(total_min / 60, 1)
            avg = round(total_min / max(1, len(sr)))
            finding.business_impact = (
                f"{len(sr)} failed {finding.step} executions "
                f"× ~{avg} min avg = {hrs}h of wasted pipeline time"
            )
    except Exception:
        pass


def run_analysis_markdown(context: dict) -> str:
    """Feature 1: render FailureReport as human-readable markdown."""
    report = run_analysis(context)
    lines = [
        f"# Failure Analysis Report — Program {report.program_id}",
        f"**Period:** Last {report.window_days} days  |  "
        f"**Total Executions:** {report.total_executions}  |  "
        f"**Success Rate:** {report.success_rate_pct:.1f}%",
        f"**Estimated Hours Wasted:** {report.estimated_hours_wasted:.1f}h",
        "",
        "## Executive Summary",
    ]
    for bullet in report.executive_summary:
        lines.append(f"- {bullet}")
    lines += ["", "## Critical Findings"]
    for f in report.critical_findings:
        lines += [
            f"### {f.step} — {f.error_type} (×{f.occurrence_count})",
            f"**Root Cause:** {f.root_cause}",
            f"**Impact:** {f.business_impact}",
            f"**Fix:** {f.recommended_fix}",
            "",
        ]
    if report.recurring_findings:
        lines.append("## Recurring Findings")
        for f in report.recurring_findings:
            lines += [
                f"### {f.step} — {f.error_type} (×{f.occurrence_count})",
                f"**Root Cause:** {f.root_cause}",
                f"**Fix:** {f.recommended_fix}",
                "",
            ]
    lines.append("## Top Recommended Actions")
    for action in report.top_recommended_actions:
        lines.append(f"- {action}")
    return "\n".join(lines)


# ── Feature 2: Pre-deployment risk analysis ───────────────────────────────────

def _enrich_risk_with_memory(user_message: str, context: dict) -> str:
    """
    Query the vector store for semantically similar past incidents, using commit
    profile signals (changed files, added dependencies, modules, anti-patterns)
    as the query — not just step names.

    Results are labelled by similarity score and the signal that drove the match.
    Deduplicates by execution_id across all module queries.

    Silently skips if the vector store is unavailable or empty.
    """
    try:
        from vector_store.store import find_similar_failures

        commit_profile: dict = context.get("commit_profile") or {}
        rule_scores: dict    = context.get("rule_scores") or {}

        changed_files: list  = commit_profile.get("changed_files") or []
        modules: list        = commit_profile.get("modules_touched") or []
        added_deps: list     = commit_profile.get("added_dependencies") or []
        anti_patterns: list  = commit_profile.get("anti_patterns") or []
        env_vars: list       = commit_profile.get("env_vars_referenced") or []

        # Build a rich query signal from commit content
        query_parts: list[str] = []
        if changed_files:
            query_parts.append(" ".join(changed_files[:10]))
        if added_deps:
            query_parts.append("dependency change: " + " ".join(added_deps[:5]))
        if anti_patterns:
            query_parts.append("anti-patterns: " + "; ".join(anti_patterns[:3]))
        if env_vars:
            query_parts.append("env vars: " + " ".join(env_vars[:3]))

        if not modules and not query_parts:
            return user_message

        # Query per module (not per step) for richer semantic matching
        seen_ids: set[str] = set()
        all_hits: list[dict] = []

        # Pipeline name from context — filters search to same pipeline only
        pipeline_name = context.get("pipeline_name", "") or context.get(
            "execution_summary", {}
        ).get("pipeline_name", "")

        for module in (modules or ["build"]):
            file_signal = " ".join(changed_files[:8])
            module_query = f"{module} {file_signal} {' '.join(added_deps[:3])}"

            hits = find_similar_failures(
                error_type=module,
                error_message=module_query,
                key_lines=changed_files[:5],
                step=module,
                top_k=3,
                pipeline=pipeline_name,
            )
            for h in hits:
                if (
                    h.get("pipeline") == "risk_analysis"
                    or str(h.get("error_type", "")).startswith("risk_prediction_")
                    or str(h.get("execution_id", "")).startswith("risk-")
                ):
                    continue
                uid = h.get("execution_id", "") + h.get("error_type", "")
                if uid not in seen_ids:
                    seen_ids.add(uid)
                    # Use metadata signal_weight if available, else fall back to similarity_score
                    sig_weight = h.get("signal_weight") or h.get("similarity_score", 0.5)
                    all_hits.append({
                        **h,
                        "_queried_module": module,
                        "signal_weight": sig_weight,
                    })

        if not all_hits:
            return user_message

        memory_block = (
            "\n\n## SEMANTICALLY SIMILAR PAST INCIDENTS (weighted by relevance, not recurrence)\n"
            "Matched against: changed files, added dependencies, modules touched, and anti-patterns.\n"
            "Use similarity_score and signal_weight to calibrate confidence — "
            "high similarity to same module+error = strong evidence.\n"
        )
        for i, h in enumerate(all_hits, 1):
            memory_block += (
                f"\n[{i}] Execution {h.get('execution_id')} | "
                f"module: {h.get('_queried_module')} | "
                f"step: {h.get('step')} | type: {h.get('error_type')} | "
                f"similarity: {h.get('similarity_score')} | "
                f"signal_weight: {h.get('signal_weight')}\n"
                f"  Root cause : {h.get('root_cause', '')}\n"
                f"  Fix applied: {h.get('fix', '')}\n"
            )

        return user_message + memory_block

    except Exception:
        return user_message   # vector store is optional — never crash the main flow


def run_risk_analysis(bundle_or_context: Any) -> "RiskReport":
    """
    Feature 2: pre-deployment risk analysis.
    Accepts a raw AnalysisBundle (Pydantic model or model_dump dict) or a
    pre-compressed context dict (output of build_risk_context).
    Detection: compressed context has 'failure_by_step'; raw bundle has 'failure_history'.
    """
    from models.risk_report import RiskReport
    from analysis.context_builder import build_risk_context

    # Already-compressed risk context has 'commit_profile'. Raw bundle
    # (Pydantic or dict) has 'git_context' / 'failure_history'.
    is_compressed = (
        isinstance(bundle_or_context, dict)
        and "commit_profile" in bundle_or_context
        and "failure_history" not in bundle_or_context
    )
    if not is_compressed:
        context = build_risk_context(bundle_or_context)
    else:
        context = bundle_or_context

    system  = _load_prompt("pre_deploy_risk.md")
    user_msg = _enrich_risk_with_memory(_build_user_message(context), context)
    report   = run_structured(system, user_msg, RiskReport, CONFIGS["risk"])

    # Risk predictions are not observed failures. Keep them out of failure_memory
    # by default so future analyses do not learn from their own guesses.
    if os.getenv("INGEST_RISK_PREDICTIONS", "").lower() not in ("1", "true", "yes"):
        return report

    try:
        from vector_store.store import ingest_risk_report
        # Build enriched metadata from commit_profile for future semantic retrieval
        commit_profile: dict = context.get("commit_profile") or {}
        enriched_context = {
            **context,
            "commit_modules": commit_profile.get("modules_touched") or [],
            "commit_anti_patterns": commit_profile.get("anti_patterns") or [],
            "confidence_score": getattr(report, "confidence_score", None),
            "criticality_score": commit_profile.get("criticality_score"),
        }
        ingest_risk_report(report, enriched_context)
    except Exception:
        pass

    return report


def run_risk_analysis_markdown(bundle_or_context: Any) -> str:
    """Feature 2: render RiskReport as markdown."""
    report = run_risk_analysis(bundle_or_context)
    lines = [
        f"# Pre-Deployment Risk Report",
        f"**Overall Risk:** {report.risk_level}",
        f"**Most Likely Failure:** {report.most_likely_failure_step}",
    ]
    if getattr(report, "commit_sha", None):
        lines.append(f"**Commit:** {report.commit_sha}")
    if getattr(report, "modules_at_risk", None):
        lines.append(f"**Modules at Risk:** {', '.join(report.modules_at_risk)}")
    if report.estimated_duration_min:
        lines.append(f"**Estimated Duration:** {report.estimated_duration_min} min")
    lines += ["", "## Step Risks"]
    for sr in report.step_risks:
        lines += [
            f"### {sr.step} — {sr.level}",
            f"- Historical failures: {sr.historical_failure_count}",
            f"- {sr.rationale}",
            "",
        ]
    lines.append("## Recommended Actions")
    for action in report.recommended_actions:
        lines.append(f"- {action}")
    if report.narrative:
        lines += ["", "## Analysis", report.narrative]
    return "\n".join(lines)


# ── Feature 3: Cross-execution comparison ────────────────────────────────────

def run_compare_analysis(data: dict) -> "ComparisonReport":
    """
    Feature 3: compare two pipeline executions.
    data is the raw dict from deploy_compare.compare_executions().
    """
    from models.output_models import ComparisonReport
    from analysis.context_builder import build_compare_context

    context = build_compare_context(data)
    system = _load_prompt("compare_deployments.md")
    return run_structured(system, _build_user_message(context), ComparisonReport, CONFIGS["compare"])


def run_compare_markdown(data: dict) -> str:
    """Feature 3: render ComparisonReport as markdown."""
    report = run_compare_analysis(data)
    reg = "Yes" if report.regression_introduced else "No"
    lines = [
        f"# Deployment Comparison",
        f"**Execution A:** {report.execution_a.id} ({report.execution_a.status})",
        f"**Execution B:** {report.execution_b.id} ({report.execution_b.status})",
        f"**Regression Introduced:** {reg}",
        f"**Confidence:** {report.confidence}",
        "",
        f"## Likely Cause",
        report.likely_cause,
        "",
        f"## Duration Delta",
        report.duration_delta_explanation,
        "",
    ]
    if report.changed_files_relevant:
        lines += ["## Relevant Changed Files"]
        for f in report.changed_files_relevant:
            lines.append(f"- `{f}`")
        lines.append("")
    lines += ["## Recommended Action", report.recommended_action]
    return "\n".join(lines)


# ── Feature 4: Log-to-code correlation ───────────────────────────────────────

def run_correlate_analysis(
    execution_id: str,
    failed_step: str,
    parse_result: Any,
    code_hits: list,
    file_snippets: list,
) -> "CorrelationReport":
    """
    Feature 4: correlate a log error to source code.
    parse_result is a LogParseResult (Pydantic model or dict).
    """
    from models.output_models import CorrelationReport
    from analysis.context_builder import build_correlate_context

    context = build_correlate_context(
        execution_id=execution_id,
        failed_step=failed_step,
        parse_result=parse_result,
        code_hits=code_hits,
        file_snippets=file_snippets,
    )
    system = _load_prompt("code_correlation.md")
    return run_structured(system, _build_user_message(context), CorrelationReport, CONFIGS["correlate"])


def run_correlate_markdown(
    execution_id: str,
    failed_step: str,
    parse_result: Any,
    code_hits: list,
    file_snippets: list,
) -> str:
    """Feature 4: render CorrelationReport as markdown."""
    report = run_correlate_analysis(
        execution_id, failed_step, parse_result, code_hits, file_snippets
    )
    lines = [
        f"# Code Correlation — {report.execution_id}",
        f"**Failed Step:** {report.failed_step}  |  **Error Type:** {report.error_type}",
        f"**Confidence:** {report.confidence}",
        "",
        f"## Error Summary",
        report.error_summary,
        "",
        f"## Root Cause",
        report.root_cause,
        "",
        f"## Fix",
        report.fix,
        "",
        "## Source Files",
    ]
    for hit in report.source_files:
        loc = f":{hit.line_no}" if hit.line_no else ""
        lines.append(f"- `{hit.file_path}{loc}` — {hit.relevance_explanation}")
        if hit.code_snippet:
            lines += [f"  ```", f"  {hit.code_snippet}", "  ```"]
    return "\n".join(lines)


# ── Feature 5a: Proactive repo scan ──────────────────────────────────────────

def run_code_scan_analysis(findings: list, repo_dir: Optional[str] = None) -> "ScanReport":
    """
    Feature 5a: proactive code scan.
    findings is the raw list from code_analyzer.scan_repo(), each item now
    includes a 'snippet' key with the ±8 line window around the bad line.
    The LLM receives the real code, not just file paths.
    """
    from models.output_models import ScanReport
    from analysis.context_builder import build_scan_context

    context = build_scan_context(findings, repo_dir)

    # Append formatted code snippets for P1/P2 findings so LLM sees real code
    p1p2 = [f for f in findings if f.get("severity") in ("P1", "P2") and f.get("snippet")]
    if p1p2:
        snippet_block = "\n\n## CODE EXCERPTS (exact lines from repository)\n"
        snippet_block += "Each block shows ±8 lines of context. >>> marks the problematic line.\n\n"
        for f in p1p2[:10]:   # cap at 10 to stay within token budget
            loc  = f"line {f['line_no']}" if f.get("line_no") else "relevant section"
            lang = {"java": "java", "xml": "xml", "conf": "apache",
                    "vhost": "apache", "json": "json"}.get(
                f["file"].rsplit(".", 1)[-1].lower(), ""
            )
            snippet_block += (
                f"FILE: {f['file']}  [{loc}]\n"
                f"ISSUE: {f['reason']}\n"
                f"```{lang}\n{f['snippet']}\n```\n\n"
            )
        user_msg = _build_user_message(context) + snippet_block
    else:
        user_msg = _build_user_message(context)

    system = _load_prompt("code_scan.md")
    report = run_structured(system, user_msg, ScanReport, CONFIGS["scan"])

    # Auto-store scan findings in vector memory
    try:
        from vector_store.store import ingest_scan_report
        ingest_scan_report(report, repo=context.get("repo", ""))
    except Exception:
        pass

    return report


def run_scan_markdown(findings: list, repo_dir: Optional[str] = None) -> str:
    """Feature 5a: render ScanReport as markdown."""
    report = run_code_scan_analysis(findings, repo_dir)
    lines = [
        f"# Code Scan Report — {report.repo_scanned}",
        f"**Deployment Readiness:** {report.deployment_readiness}",
        f"**Findings:** {report.total_findings} total  |  "
        f"P1: {report.p1_count}  |  P2: {report.p2_count}  |  P3: {report.p3_count}",
        "",
        f"## Summary",
        report.summary,
        "",
    ]
    p1 = [f for f in report.findings if f.severity == "P1"]
    p2 = [f for f in report.findings if f.severity == "P2"]
    if p1:
        lines.append("## P1 — Critical (will break pipeline)")
        for f in p1:
            loc = f":{f.line_no}" if f.line_no else ""
            lines += [
                f"### `{f.file}{loc}`",
                f"**Pattern:** {f.pattern}",
                f"**Why it breaks Cloud Manager:** {f.problem_explanation}",
                f"**Fix:** {f.fix_code_example}",
                f"**Owner:** {f.owning_team}",
                "",
            ]
    if p2:
        lines.append("## P2 — High (likely to break under conditions)")
        for f in p2:
            loc = f":{f.line_no}" if f.line_no else ""
            lines += [
                f"### `{f.file}{loc}`",
                f"**Pattern:** {f.pattern}",
                f"**Why it matters:** {f.problem_explanation}",
                f"**Fix:** {f.fix_code_example}",
                f"**Owner:** {f.owning_team}",
                "",
            ]
    return "\n".join(lines)


# ── Feature 5b: Root cause pinpointing ───────────────────────────────────────

def run_pinpoint_analysis(
    execution_id: str,
    failed_step: str,
    parse_result: Any,
    code_findings: list,
    snippet_text: str = "",
) -> "PinpointReport":
    """
    Feature 5b: pinpoint the exact file/line that caused a failure.

    parse_result  — LogParseResult Pydantic model or dict from parse_log()
    code_findings — list of {file, line_no, snippet, reason} from file_resolver
    snippet_text  — pre-formatted code windows (±15 lines, >>> on error line)
                    produced by file_resolver.format_for_llm(). When present,
                    appended to the user message so the LLM sees real code lines
                    instead of just file paths.

    Automatically stores the result in vector memory for future lookups.
    """
    from models.output_models import PinpointReport
    from analysis.context_builder import build_pinpoint_context

    # Normalise parse_result to dict
    if hasattr(parse_result, "model_dump"):
        parse_dict = parse_result.model_dump()
    else:
        parse_dict = parse_result or {}

    context = build_pinpoint_context(
        execution_id=execution_id,
        failed_step=failed_step,
        parse_result=parse_dict,
        code_findings=code_findings,
    )

    base_msg = _build_user_message(context)

    # Append exact code snippets when available — this is the key improvement:
    # instead of the LLM guessing from file paths, it sees the real lines
    if snippet_text:
        base_msg += (
            "\n\n## EXACT CODE LOCATION\n"
            "The following are the precise file excerpts extracted from the repository.\n"
            "The line marked with >>> is where the error originates.\n"
            "Use these to fill in primary_cause_file, primary_cause_line_no, "
            "primary_cause_line, fix_before, and fix_after.\n"
            + snippet_text
        )

    # Enrich with similar past failures from vector memory
    user_msg = _enrich_with_memory(
        base_msg,
        error_type=parse_dict.get("error_type", ""),
        error_message=parse_dict.get("error_message", ""),
        key_lines=parse_dict.get("key_lines", []),
        step=failed_step,
    )

    system = _load_prompt("pinpoint_failure.md")
    report = run_structured(system, user_msg, PinpointReport, CONFIGS["pinpoint"])

    # Auto-store result in vector memory so future queries can find it
    try:
        from vector_store.store import ingest_pinpoint_report
        ingest_pinpoint_report(report)
    except Exception:
        pass

    return report


def run_pinpoint_markdown(
    execution_id: str,
    failed_step: str,
    parse_result: Any,
    code_findings: list,
    snippet_text: str = "",
) -> str:
    """Feature 5b: render PinpointReport as markdown."""
    report = run_pinpoint_analysis(
        execution_id, failed_step, parse_result, code_findings,
        snippet_text=snippet_text,
    )
    loc = f":{report.primary_cause_line_no}" if report.primary_cause_line_no else ""
    lines = [
        f"# Root Cause Pinpoint — {report.execution_id}",
        f"**Failed Step:** {report.failed_step}  |  **Error Type:** {report.error_type}",
        f"**Confidence:** {report.confidence}",
        "",
        f"## Primary Cause",
        f"`{report.primary_cause_file}{loc}`",
    ]
    if report.primary_cause_line:
        lines += [f"```", report.primary_cause_line, "```"]
    lines += ["", report.explanation, ""]
    if report.fix_before and report.fix_after:
        lines += [
            "## Fix",
            "**Before:**",
            "```",
            report.fix_before,
            "```",
            "**After:**",
            "```",
            report.fix_after,
            "```",
            "",
        ]
    lines += [
        "## Prevention",
        report.prevention,
    ]
    if report.alternative_causes:
        lines += ["", "## Alternative Causes"]
        for alt in report.alternative_causes:
            alt_loc = f":{alt.get('line_no')}" if alt.get("line_no") else ""
            lines.append(f"- `{alt.get('file','')}{alt_loc}` — {alt.get('reason','')}")
    return "\n".join(lines)
