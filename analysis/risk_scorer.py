"""
Three-signal risk scorer.

Computes environment health, code risk, and historical match
independently — each backed by real data, no LLM guessing.

Signal 1: Environment Health  → Splunk history
Signal 2: Code Risk           → Structural analysis of git diff
Signal 3: Historical Match    → ChromaDB similarity search

Combined into a final recommendation: GO / CAUTION / HOLD
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# Step categories — used for LLM vs env conflict resolution
_ENV_STEPS       = {"securityTest", "deploy", "loadTest", "activation", "reportPerformanceTest"}
_CODE_STEPS      = {"build", "codeQuality"}
_CODE_PERF_STEPS = {"loadTest", "reportPerformanceTest"}  # code-caused when DAO/migration pattern fires

_LLM_LEVEL_SCORE = {"low": 0.25, "medium": 0.55, "high": 0.80, "critical": 0.90}


def _is_infra_env_step(step: str, code_caused_perf: bool = False) -> bool:
    """loadTest is infra-only unless a deterministic perf regression pattern matched."""
    if step in _CODE_PERF_STEPS and code_caused_perf:
        return False
    return step in _ENV_STEPS


def infer_java_upgrade_pending(
    bundle_dict: Optional[dict] = None,
    pipeline_df=None,
    execution_id: str = "",
) -> bool:
    """Detect Cloud Manager Java-version upgrade validation warnings."""
    import re as _re_jdk

    bundle_dict = bundle_dict or {}
    if bundle_dict.get("java_upgrade_pending"):
        return True
    notes = str(bundle_dict.get("pipeline_validation_notes") or "")
    if _re_jdk.search(r"higher\s+java\s+version|java\s+version.*upgrad", notes, _re_jdk.I):
        return True
    if pipeline_df is not None and execution_id:
        try:
            import pandas as _pd
            if not isinstance(pipeline_df, _pd.DataFrame) or pipeline_df.empty:
                return False
            if "executionId" not in pipeline_df.columns:
                return False
            row = pipeline_df[pipeline_df["executionId"].astype(str) == str(execution_id)]
            if row.empty:
                return False
            blob = " ".join(str(v) for v in row.iloc[0].values)
            return bool(_re_jdk.search(
                r"higher\s+java\s+version|java\s+version.*upgrad|upgrading\s+to\s+a\s+higher\s+java",
                blob, _re_jdk.I,
            ))
        except Exception:
            return False
    return False

@dataclass
class EnvSignal:
    status: str               # READY / CAUTION / NOT_READY / UNKNOWN
    score: float              # 0.0 (healthy) → 1.0 (broken)
    consecutive_failures: int
    dominant_step: str
    last_success_ago: str
    detail: str               # human-readable one-liner
    fix: str                  # what to do
    failure_probability: float  # P(this step fails) based on history
    hold_threshold: int = 3   # per-tenant calibrated threshold for HOLD
    is_persistent_infra: bool = False  # True = CRXDE/DavEx persistent noise, not a code issue
    is_env_issue: bool = False        # True = dominant failures are env steps (securityTest/deploy/loadTest), not build
    env_step_failure_count: int = 0   # raw count of env-step failures in recent window


@dataclass
class CodeSignal:
    level: str                # LOW / MEDIUM / HIGH / CERTAIN
    score: float              # 0.0 → 1.0
    detail: str               # what changed
    findings: List[str]       # specific findings with confidence
    is_submodule_only: bool
    has_real_code: bool       # False = pointer-only, True = actual code changed
    code_caused_perf: bool = False  # True = DAO/migration pattern → loadTest risk
    is_merge_commit: bool = False   # True = git merge — aggregated multi-commit diff


@dataclass
class HistoricalSignal:
    score: float              # 0.0 → 1.0 (avg similarity of matches)
    match_count: int
    dominant_step: str        # step that failed most in matches
    fail_rate: float          # % of matches that actually failed
    detail: str
    examples: List[dict]      # top matching incidents


@dataclass
class LlmSignal:
    """Structured signal from LLM step_risks — 4th input to the scorer."""
    score: float              # 0-1, strength of commit-relevant risk
    dominant_step: str        # highest-scoring step overall
    commit_risk_step: str     # step most relevant to THIS commit (may differ from dominant)
    dominant_level: str       # LOW / MEDIUM / HIGH
    step_scores: Dict[str, float]
    detail: str
    has_analysis: bool
    most_likely_step: str = ""


@dataclass
class RiskDecision:
    recommendation: str       # GO / CAUTION / HOLD
    expected_outcome: str     # "PASS", "FAIL at securityTest", etc.
    confidence: float         # 0-1, how certain we are
    confidence_basis: str     # what drives the confidence number
    env: EnvSignal
    code: CodeSignal
    historical: HistoricalSignal
    llm: LlmSignal
    narrative: str            # LLM narrative (set later)
    primary_driver: str       # which signal is driving the decision


# ── Signal 1: Environment Health ─────────────────────────────────────────────

def _get_tenant_calibration(program_id: str) -> dict:
    """
    Learn per-tenant calibration from resolved predictions.

    Returns calibrated thresholds:
    - consecutive_hold_threshold: consecutive failures before HOLD (default 3)
    - env_base_fail_rate: historical P(fail) for this tenant's env issues

    IDFC has ~50% fail rate despite persistent CRXDE → threshold should be higher
    HDFC has ~30% fail rate from infra → threshold should be different
    """
    try:
        from analysis.prediction_store import load_resolved
        resolved = load_resolved(program_id)
        if len(resolved) < 5:  # not enough data, use defaults
            return {"consecutive_hold_threshold": 3, "env_base_fail_rate": 0.6}

        # Compute actual fail rate for env-driven predictions
        env_driven = [r for r in resolved if r.get("primary_driver") in ("environment", "environment+history")]
        if not env_driven:
            return {"consecutive_hold_threshold": 3, "env_base_fail_rate": 0.6}

        actual_fails = sum(1 for r in env_driven if r.get("actual_status") in ("FAILED", "ERROR"))
        env_fail_rate = actual_fails / len(env_driven)

        # Calibrate threshold: if fail rate < 60%, raise threshold to reduce false positives
        if env_fail_rate < 0.40:
            threshold = 5  # very noisy env, need strong signal before HOLD
        elif env_fail_rate < 0.60:
            threshold = 4
        else:
            threshold = 3  # env failures are real, trust sooner

        return {
            "consecutive_hold_threshold": threshold,
            "env_base_fail_rate": env_fail_rate,
            "sample_size": len(env_driven),
        }
    except Exception:
        return {"consecutive_hold_threshold": 3, "env_base_fail_rate": 0.6}


def compute_env_signal(
    pipeline_df,
    failed_df,
    program_id: str = "",
    pipeline_name: Optional[str] = None,
    as_of_date: Optional[str] = None,
) -> EnvSignal:
    """
    Pure Splunk — no LLM, no guessing.
    Computes environment health from pipeline execution history.
    """
    from analysis.env_readiness import DEFAULT_PROD_PIPELINE, assess_environment_readiness

    if pipeline_name is None:
        pipeline_name = DEFAULT_PROD_PIPELINE

    try:
        env = assess_environment_readiness(
            pipeline_df, failed_df, pipeline_name=pipeline_name, as_of_date=as_of_date
        )
    except Exception:
        return EnvSignal(
            status="UNKNOWN", score=0.0, consecutive_failures=0,
            dominant_step="", last_success_ago="unknown",
            detail="Could not assess environment — no Splunk data available.",
            fix="Load pipeline data from the sidebar.",
            failure_probability=0.0,
        )

    status    = env["status"]
    consec    = env["consecutive_failures"]
    dom_step  = env["dominant_step"]
    last_ok   = env["last_success_ago"]
    is_env    = env["is_env_issue"]
    env_count = env.get("env_step_failure_count", 0)  # raw window failure count

    # Per-tenant calibration — learn threshold from resolved predictions
    _calib = _get_tenant_calibration(program_id) if program_id else {}
    _hold_threshold = _calib.get("consecutive_hold_threshold", 3)

    # Calibrated failure probability.
    # When consecutive=0 (last run passed) but window shows many env failures,
    # use the window rate — not 5% which implies "very unlikely to fail" even
    # when 10/10 recent runs failed at securityTest.
    # Formula: window_rate * 0.6 discount (last run passed, so slight recovery signal)
    if consec == 0:
        if env_count > 0:
            _window_rate = env_count / 10.0  # recent_n=10 default
            fail_prob = max(0.15, min(_window_rate * 0.6, 0.80))
        else:
            fail_prob = 0.05
        score = fail_prob
    elif consec == 1:
        fail_prob = 0.35
        score     = 0.35
    elif consec == 2:
        fail_prob = 0.55
        score     = 0.55
    elif consec < 5:
        fail_prob = 0.70
        score     = 0.70
    else:
        fail_prob = 0.85 + min(consec - 5, 10) * 0.01  # cap at 95%
        score     = min(fail_prob, 0.95)

    # Build detail string
    step_label = dom_step or "unknown step"
    if status == "READY":
        detail = f"Environment healthy — last success {last_ok}"
        fix    = "No action needed."
    elif status == "CAUTION":
        detail = f"{consec} recent failure(s) at {step_label} — last success {last_ok}"
        fix    = env["recommendation"]
    elif status == "NOT_READY":
        detail = f"{consec} consecutive failures at {step_label} — last success {last_ok}"
        fix    = env["recommendation"]
    else:
        detail = "Environment state unknown"
        fix    = "Load pipeline data."

    # Classify whether env issue is persistent infra noise (CRXDE/DavEx every run)
    # vs transient (one-off failure). Only persistent issues drive HOLD.
    _is_persistent_infra = (
        is_env and consec >= 3 and
        dom_step in ("securityTest",) and
        # If dominant step is securityTest and consecutive >= 3, likely CRXDE/DavEx
        True
    )

    return EnvSignal(
        status=status,
        score=score,
        consecutive_failures=consec,
        dominant_step=dom_step,
        last_success_ago=last_ok,
        detail=detail,
        fix=fix,
        failure_probability=fail_prob,
        hold_threshold=_hold_threshold,
        is_persistent_infra=_is_persistent_infra,
        is_env_issue=is_env,
        env_step_failure_count=env_count,
    )


# ── Signal 2: Code Risk ───────────────────────────────────────────────────────

def compute_code_signal(
    diff_text: str,
    changed_files: List[str],
    commit_title: str,
    repo_dir: str = "",
    submodule_diffs: Optional[dict] = None,
    java_upgrade_pending: bool = False,
) -> CodeSignal:
    """
    Pure structural analysis — no LLM.
    What in the code diff will definitely break.
    """
    from analysis.build_predictor import (
        merge_submodule_analysis_inputs,
        predict_build_failures,
        submodule_diffs_contain_java,
        _is_submodule_release_bump,
    )
    from analysis.diff_analyzer import analyze_diff

    merged_diff, merged_files = merge_submodule_analysis_inputs(
        diff_text, changed_files, submodule_diffs
    )
    signals = analyze_diff(merged_diff, merged_files, title=commit_title)
    pred = predict_build_failures(
        merged_diff, merged_files, commit_title, repo_dir,
        submodule_diffs=submodule_diffs,
        java_upgrade_pending=java_upgrade_pending,
    )
    has_sub_java = submodule_diffs_contain_java(submodule_diffs)
    is_sub = _is_submodule_release_bump(signals) and not has_sub_java and not signals.has_java_change
    has_real = bool(
        (merged_diff and merged_files and not is_sub)
        or has_sub_java
        or signals.has_java_change
    )

    code_caused_perf = any(
        f.check in ("dao_migration_perf_risk", "java_upgrade_perf_risk")
        for f in pred.findings
    )

    findings: List[str] = []
    for f in pred.findings:
        conf_label = f"{f.confidence}% certain" if f.confidence >= 70 else f"{f.confidence}% likely"
        # Include plain-English detail if available — makes the finding self-explanatory
        # without needing to read the technical evidence field
        if f.detail and f.detail not in ("pom.xml reactor", "pom.xml <module> entries",
                                          "pom.xml + submodule pointer", ".gitmodules / pom.xml",
                                          "parent pom / .gitmodules"):
            findings.append(f"[{f.severity}] {f.title} — {f.detail} ({conf_label})")
        else:
            findings.append(f"[{f.severity}] {f.title} ({conf_label})")

    from analysis.build_predictor import _is_merge_commit

    is_merge = _is_merge_commit(commit_title)

    # Score from severity + confidence — but subtree imports are always LOW
    # regardless of confidence (88% certain it IS a subtree import ≠ 88% code risk)
    if signals.is_subtree_import:
        score = 0.15
        level = "LOW"
    elif pred.findings:
        # Use severity to set risk band, confidence to calibrate within band
        sev_map = {"HIGH": 0.85, "MEDIUM": 0.50, "LOW": 0.15, "CERTAIN": 0.92}
        top_sev_score = max(sev_map.get(f.severity, 0.10) for f in pred.findings)
        # Confidence refines within the severity band — doesn't escape it
        top_conf = max(f.confidence for f in pred.findings) / 100.0
        score = top_sev_score * 0.7 + top_conf * 0.3 * (top_sev_score / 0.85)
        score = min(score, top_sev_score * 1.1)  # cap: can't exceed 10% above severity max
        level = "HIGH" if top_sev_score >= 0.80 else ("MEDIUM" if top_sev_score >= 0.40 else "LOW")
    elif is_sub:
        score = 0.20
        level = "LOW"
    elif not changed_files:
        score = 0.02
        level = "LOW"
    else:
        score = 0.25
        level = "LOW"

    # Detail
    if is_sub:
        detail = "Submodule pointer only — no app code changed in parent repo"
    elif not changed_files:
        detail = "Empty diff — no files changed"
    elif pred.findings:
        # Prefer perf finding in detail when it is the dominant code-caused risk
        perf_first = [f for f in pred.findings if f.step in _CODE_PERF_STEPS]
        detail = (perf_first[0].title if perf_first and code_caused_perf
                  else pred.findings[0].title)
    else:
        changed_types = []
        if signals.has_java_change:   changed_types.append("Java")
        if signals.has_pom_change:    changed_types.append("pom.xml")
        if signals.has_npm_change:    changed_types.append("npm")
        if signals.has_config_change: changed_types.append("config")
        detail = f"{', '.join(changed_types) or 'misc'} changes — no definite failure pattern detected"

    return CodeSignal(
        level=level,
        score=score,
        detail=detail,
        findings=findings,
        is_submodule_only=is_sub,
        has_real_code=has_real,
        code_caused_perf=code_caused_perf,
        is_merge_commit=is_merge,
    )


def code_recommendation(code: "CodeSignal") -> tuple:
    """
    Build-only verdict — completely independent of env/historical signals.

    Returns (recommendation, confidence, basis) where:
    - recommendation: GO / CAUTION / HOLD based solely on code structure
    - confidence: float 0-1 derived from finding confidence, not blended scorer
    - basis: human-readable string describing what drove the verdict

    This is what the BUILD FAILURE ASSESSMENT hero card should display.
    It never returns HOLD/CAUTION due to env failures — only code findings.
    """
    mapping = {"LOW": "GO", "MEDIUM": "CAUTION", "HIGH": "HOLD", "CERTAIN": "HOLD"}
    rec = mapping.get(code.level, "CAUTION")

    # Confidence: parse top finding confidence if available, else level-based default
    level_defaults = {"LOW": 0.70, "MEDIUM": 0.65, "HIGH": 0.75, "CERTAIN": 0.90}
    conf = level_defaults.get(code.level, 0.60)

    if code.findings:
        # Try to extract numeric confidence from finding strings like "(85% certain)"
        import re as _re
        _conf_nums = []
        for f in code.findings:
            _m = _re.search(r'\((\d+)%', f)
            if _m:
                _conf_nums.append(int(_m.group(1)) / 100.0)
        if _conf_nums:
            conf = max(_conf_nums) * 0.85  # slight discount — single finding, not corroborated

    # Basis string — honest about what drove it
    if not code.findings and code.level == "LOW":
        if code.is_submodule_only:
            basis = "structural diff analysis — submodule pointer only, no app code changed"
        elif not code.has_real_code:
            basis = "structural diff analysis — no compiled code changed"
        else:
            basis = "structural diff analysis — no OSGi/build failure patterns detected"
    elif code.findings:
        top = code.findings[0]
        basis = f"structural diff analysis — {top[:80]}"
    else:
        basis = "structural diff analysis"

    return rec, round(conf, 3), basis


# ── Signal 3: Historical Match ────────────────────────────────────────────────

def compute_historical_signal(bundle_dict: dict) -> HistoricalSignal:
    """
    ChromaDB similarity + Splunk failure_by_step.
    What happened in the past when similar commits were deployed.
    """
    # ChromaDB similar incidents (added by _enrich_risk_with_memory)
    similar = bundle_dict.get("similar_incidents") or []

    if not similar:
        # No ChromaDB matches — historical score is 0.0
        # Pipeline failure rate belongs to env signal, not historical match.
        # Using it here would inflate every prediction on a historically unstable pipeline.
        return HistoricalSignal(
            score=0.0,
            match_count=0,
            dominant_step="",
            fail_rate=0.0,
            detail="No similar past incidents found in failure database.",
            examples=[],
        )

    # Have ChromaDB matches
    scores     = [s.get("similarity_score", 0) for s in similar]
    avg_sim    = sum(scores) / len(scores) if scores else 0
    max_sim    = max(scores) if scores else 0

    # Which step failed most in similar incidents
    step_counts: dict = {}
    for s in similar:
        step = s.get("step", "") or ""
        if step:
            step_counts[step] = step_counts.get(step, 0) + 1
    dom_step = max(step_counts, key=step_counts.get) if step_counts else ""

    # Failure rate among similar incidents
    fail_rate = min(avg_sim * 0.9, 0.95)  # calibrated: high similarity → high fail probability

    detail = (f"{len(similar)} past incidents matched — "
              f"avg {int(avg_sim*100)}% similarity, "
              f"most failed at {dom_step or 'unknown step'}")

    return HistoricalSignal(
        score=avg_sim,
        match_count=len(similar),
        dominant_step=dom_step,
        fail_rate=fail_rate,
        detail=detail,
        examples=similar[:3],
    )


# ── Signal 4: LLM step risks ──────────────────────────────────────────────────

def _llm_commit_risk_step(llm: LlmSignal, env: EnvSignal) -> str:
    """
    Step most relevant to commit-caused risk.
    When env flags securityTest (infra noise), prefer LLM's build/deploy assessment.
    """
    if not llm.step_scores:
        return llm.dominant_step or llm.most_likely_step or ""

    if env.dominant_step in _ENV_STEPS or env.is_persistent_infra:
        code_step_scores = {
            s: sc for s, sc in llm.step_scores.items()
            if s in _CODE_STEPS or s in _CODE_PERF_STEPS
        }
        if code_step_scores:
            best = max(code_step_scores, key=code_step_scores.get)
            if code_step_scores[best] >= 0.50:
                return best

    return max(llm.step_scores, key=llm.step_scores.get)


def compute_llm_signal(bundle_dict: dict, env: Optional[EnvSignal] = None) -> LlmSignal:
    """
    Parse LLM step_risks into a structured signal the scorer can combine with env/code/history.
    """
    empty = LlmSignal(
        score=0.0, dominant_step="", commit_risk_step="", dominant_level="LOW",
        step_scores={}, detail="", has_analysis=False,
    )
    step_risks = bundle_dict.get("llm_step_risks") or []
    most_likely = str(bundle_dict.get("llm_most_likely_step") or "").strip()

    if not step_risks and not most_likely:
        return empty

    step_scores: Dict[str, float] = {}
    rationales: Dict[str, str] = {}
    for sr in step_risks:
        step = str(sr.get("step", "")).strip()
        if not step:
            continue
        lvl = str(sr.get("level", "")).lower()
        sc = _LLM_LEVEL_SCORE.get(lvl, 0.30)
        step_scores[step] = max(step_scores.get(step, 0.0), sc)
        if sr.get("rationale"):
            rationales[step] = str(sr["rationale"])[:140]

    if not step_scores and most_likely:
        step_scores[most_likely] = 0.50

    if not step_scores:
        return empty

    dominant_step = max(
        step_scores,
        key=lambda s: (step_scores[s], 1 if s in _CODE_STEPS else 0),
    )
    dominant_score = step_scores[dominant_step]
    dominant_level = (
        "HIGH" if dominant_score >= 0.75
        else ("MEDIUM" if dominant_score >= 0.50 else "LOW")
    )

    _env_stub = env or EnvSignal(
        status="UNKNOWN", score=0, consecutive_failures=0, dominant_step="",
        last_success_ago="", detail="", fix="", failure_probability=0,
    )
    commit_step = _llm_commit_risk_step(
        LlmSignal(
            score=dominant_score, dominant_step=dominant_step, commit_risk_step="",
            dominant_level=dominant_level, step_scores=step_scores, detail="",
            has_analysis=True, most_likely_step=most_likely,
        ),
        _env_stub,
    )
    commit_score = step_scores.get(commit_step, dominant_score)

    detail = rationales.get(commit_step) or rationales.get(dominant_step) or (
        f"LLM flags {commit_step} as {dominant_level}"
    )

    return LlmSignal(
        score=commit_score,
        dominant_step=dominant_step,
        commit_risk_step=commit_step,
        dominant_level=dominant_level,
        step_scores=step_scores,
        detail=detail,
        has_analysis=True,
        most_likely_step=most_likely,
    )


def _llm_overrides_env_for_commit(env: EnvSignal, llm: LlmSignal, code: CodeSignal) -> bool:
    """
    True when LLM identifies commit-caused risk at a code step while env shows infra noise.
    Example: LLM build=Medium, env securityTest=NOT_READY (CRXDE).
    """
    if not llm.has_analysis or llm.score < 0.50:
        return False
    commit_step = llm.commit_risk_step or llm.dominant_step
    if commit_step not in _CODE_STEPS and commit_step not in _CODE_PERF_STEPS:
        return False
    if not _is_infra_env_step(env.dominant_step, code.code_caused_perf) and not env.is_persistent_infra:
        return False
    env_llm_score = llm.step_scores.get(env.dominant_step, 0.0)
    # LLM explicitly rated env step as low → trust commit step
    if env_llm_score <= 0.30 and llm.score >= 0.50:
        return True
    # Commit step rated medium+ and at least as important as env step in LLM view
    if llm.score >= 0.50 and llm.score >= env_llm_score * 0.85:
        return True
    if code.has_real_code and llm.score >= 0.50:
        return True
    return False


# ── Combine into final decision ───────────────────────────────────────────────

def _findings_are_test_gap_only(findings: List[str]) -> bool:
    """True when findings are only same-commit test-coverage gaps (not compile/OSGi)."""
    if not findings:
        return False
    _gap_markers = (
        "no unit test updated",
        "no test files updated",
        "service_without_test",
        "core_java_without_test",
    )
    _hard_markers = (
        "autowired", "snapshot", "reactor", "osgi", "vault",
        "interface", "classpath", "duplicate",
    )
    for f in findings:
        fl = f.lower()
        if not any(m in fl for m in _gap_markers):
            return False
        if any(m in fl for m in _hard_markers):
            return False
    return True


def _pick_failure_step(
    code: CodeSignal,
    historical: HistoricalSignal,
    env: EnvSignal,
    llm: Optional[LlmSignal] = None,
    llm_step: str = "",
) -> str:
    """
    Priority-ordered step prediction.
    Code findings win over env signal — env.dominant_step must not drown out
    deterministic code findings or LLM commit-risk assessment.
    """
    llm = llm or LlmSignal(
        score=0, dominant_step="", commit_risk_step="", dominant_level="LOW",
        step_scores={}, detail="", has_analysis=False,
    )

    # 0. Code-caused performance regression (DAO/LTS migration) — before build/LLM
    if code.code_caused_perf:
        return "loadTest"

    # 1. Deterministic code finding — most certain
    if code.level in ("HIGH", "CERTAIN") and code.findings:
        _finding_text = " ".join(code.findings).lower()
        if any(k in _finding_text for k in ("loadtest", "performance", "dao/lts", "dao migration")):
            return "loadTest"
        if any(k in _finding_text for k in (
            "deploy", "vault", "filter", "osgi", "activation",
            "vhost", "dispatcher", "jcr", "content.xml", "package install", "clientlib"
        )):
            return "deploy"
        return "build"

    # 2. Medium code risk with step-specific findings
    if code.level == "MEDIUM" and code.findings and env.status == "READY":
        _ft = " ".join(code.findings).lower()
        if any(k in _ft for k in ("loadtest", "performance", "dao/lts", "dao migration", "data-access")):
            return "loadTest"
        if any(k in _ft for k in (
            "deploy", "vault", "filter", "vhost", "dispatcher",
            "jcr", "content.xml", "package install", "clientlib"
        )):
            return "deploy"
        if any(k in _ft for k in ("submodule", "reactor", "service", "test", "surefire", "build")):
            return "build"

    # 3. LLM commit-risk step — before env when LLM disagrees with infra step
    if llm.has_analysis and _llm_overrides_env_for_commit(env, llm, code):
        return llm.commit_risk_step or llm.dominant_step

    if llm.has_analysis and llm.score >= 0.55 and llm.commit_risk_step:
        if _is_infra_env_step(env.dominant_step, code.code_caused_perf) and (
            llm.commit_risk_step in _CODE_STEPS or llm.commit_risk_step in _CODE_PERF_STEPS
        ):
            return llm.commit_risk_step

    # 4. Historical match — lower bar when real code/submodule changes present
    if code.has_real_code and historical.dominant_step:
        if historical.dominant_step == "build" and historical.match_count >= 1:
            return "build"
    if historical.score >= 0.65 and historical.dominant_step:
        return historical.dominant_step

    # 5. Code has real changes — prefer build over env securityTest noise
    # Only when code is MEDIUM+ — LOW findings (e.g. subtree import) should NOT
    # override a strong env signal pointing to securityTest.
    if code.has_real_code and _is_infra_env_step(env.dominant_step, code.code_caused_perf):
        if code.code_caused_perf:
            return "loadTest"
        if code.level in ("MEDIUM", "HIGH"):  # removed: "or code.findings" — LOW findings don't override env
            return "build"

    # 6. LLM overall most-likely step (weaker signal)
    if llm.has_analysis and llm.score >= 0.50:
        return llm.commit_risk_step or llm.most_likely_step or llm.dominant_step

    # 7. Environment signal
    if env.status in ("NOT_READY", "CAUTION") and env.dominant_step:
        return env.dominant_step

    # 8. LLM / legacy fallback
    return llm.most_likely_step or llm_step or "unknown"


def make_decision(
    env: EnvSignal,
    code: CodeSignal,
    historical: HistoricalSignal,
    llm: Optional[LlmSignal] = None,
) -> Tuple[str, str, float, str, str]:
    """
    Returns (recommendation, expected_outcome, confidence, basis, primary_driver).

    Decision logic — code and LLM commit-risk are evaluated BEFORE env overrides.
    """
    llm = llm or LlmSignal(
        score=0, dominant_step="", commit_risk_step="", dominant_level="LOW",
        step_scores={}, detail="", has_analysis=False,
    )

    # Priority 0: LOW code risk + env issue is known infrastructure problem → GO with advisory
    # This is the most common false-positive source: a LOW-risk commit (submodule pointer bump,
    # pom version update, etc.) during a period where securityTest/deploy/loadTest is failing
    # due to a standing env issue (CRXDE/DavEx active, AEM config, etc.).
    # The pipeline PASSED on those runs — the env issue was either resolved before the run,
    # or it's intermittent. Predicting CAUTION for every LOW-risk commit during env issues
    # causes 87% false positive rate. Fix: if code is genuinely LOW and env failure is
    # infrastructure (not code-caused), default to GO with an advisory note.
    _is_genuinely_low_code = (
        code.score <= 0.25
        and code.level in ("LOW",)
        and not code.findings  # no structural code findings
    )
    _env_is_infra_only = (
        env.is_env_issue  # securityTest/deploy/loadTest dominant — not build
        and not env.dominant_step in ("build", "codeQuality")
    )
    if _is_genuinely_low_code and _env_is_infra_only and historical.score < 0.55:
        _step_note = f" (env has recurring {env.dominant_step} failures — infrastructure issue, not caused by this commit)" if env.dominant_step else ""
        return (
            "GO",
            f"PASS — code risk is LOW{_step_note}",
            max(0.60, 1.0 - env.score * 0.4),
            (
                f"Code risk is LOW (score={code.score:.2f}). "
                f"Recurring {env.dominant_step} failures are an infrastructure issue unrelated to this commit. "
                f"Env advisory: verify {env.dominant_step} config before triggering if in doubt."
            ),
            "code",
        )

    # Priority 1: Certain structural code failure
    if code.level == "CERTAIN" or (code.level == "HIGH" and code.score >= 0.80):
        _code_step = _pick_failure_step(code, historical, env, llm)
        return (
            "HOLD",
            f"FAIL at {_code_step}",
            code.score,
            f"Deterministic structural finding: {code.findings[0] if code.findings else code.detail}",
            "code",
        )

    # Priority 1b: Merge commit + test-gap only + healthy env — not blocking
    if (
        code.is_merge_commit
        and env.status == "READY"
        and code.findings
        and _findings_are_test_gap_only(code.findings)
    ):
        return (
            "GO",
            "PASS — merge commit; tests likely updated on source branch",
            max(0.45, 1.0 - code.score),
            (
                f"Merge commit test-gap advisory only: {code.findings[0]}. "
                f"Same-commit test absence is not predictive for aggregated merges."
            ),
            "code",
        )

    # Priority 1c: Test-gap only on healthy env — advisory CAUTION, never HOLD
    if (
        env.status == "READY"
        and code.level == "HIGH"
        and code.findings
        and _findings_are_test_gap_only(code.findings)
    ):
        _code_step = _pick_failure_step(code, historical, env, llm)
        return (
            "CAUTION",
            f"Possible FAIL at {_code_step}",
            min(code.score, 0.58),
            f"Test coverage gap (advisory): {code.findings[0]}",
            "code",
        )

    # Priority 2: Medium structural code risk
    if code.level == "MEDIUM" and code.score >= 0.50 and code.findings:
        _code_step = _pick_failure_step(code, historical, env, llm)
        _driver = "code"
        _basis = f"Code risk: {code.findings[0]}"
        if code.code_caused_perf:
            _basis = f"Performance regression risk: {code.detail}"
        return (
            "CAUTION",
            f"FAIL at {_code_step}",
            code.score,
            _basis,
            _driver,
        )

    # Priority 2b: LLM commit-risk overrides env infra step (build Medium + securityTest env)
    if _llm_overrides_env_for_commit(env, llm, code):
        _step = _pick_failure_step(code, historical, env, llm)
        _conf = max(llm.score, code.score, historical.score * 0.5 if historical.match_count else 0)
        _rec = "HOLD" if _conf >= 0.75 or llm.dominant_level == "HIGH" else "CAUTION"
        _driver = "llm+code" if (code.findings or code.has_real_code) else "llm"
        _env_note = (
            f" Env advisory: {env.dominant_step} has persistent infra issues (not caused by commit)."
            if env.is_persistent_infra or env.dominant_step in _ENV_STEPS else ""
        )
        return (
            _rec,
            f"FAIL at {_step}",
            min(_conf, 0.85),
            f"LLM analysis: {llm.detail}.{_env_note}",
            _driver,
        )

    # Priority 3: Environment is broken
    if env.status == "NOT_READY" and env.dominant_step:
        confidence = env.failure_probability
        if env.consecutive_failures >= env.hold_threshold:
            if env.is_persistent_infra:
                # Real code changes + history at build → env is ops advisory, code drives step
                if code.has_real_code and (
                    code.findings
                    or historical.dominant_step == "build"
                    or code.level == "MEDIUM"
                    or _llm_overrides_env_for_commit(env, llm, code)
                ):
                    _step = _pick_failure_step(code, historical, env, llm)
                    _conf = max(code.score, historical.score * 0.75 if historical.match_count else 0, 0.55)
                    return (
                        "CAUTION",
                        f"FAIL at {_step}",
                        min(_conf, 0.72),
                        (
                            f"Code/submodule changes may fail at {_step}. "
                            f"Separate persistent env issue at {env.dominant_step} (CRXDE/DavEx — ops advisory, not caused by commit)."
                        ),
                        "code+environment_ops",
                    )
                return (
                    "CAUTION",
                    f"Possible FAIL at {env.dominant_step} (persistent env issue)",
                    confidence * 0.80,
                    "Persistent environment issue (CRXDE/DavEx active). Ops team should fix — not caused by this commit. Deployment may proceed with awareness.",
                    "environment_ops",
                )
            return (
                "HOLD",
                f"FAIL at {env.dominant_step}",
                confidence,
                f"{env.consecutive_failures} consecutive failures at {env.dominant_step} — {int(confidence*100)}% probability based on history",
                "environment",
            )
        else:
            return (
                "CAUTION",
                f"Possible FAIL at {env.dominant_step}",
                confidence * 0.75,
                f"{env.consecutive_failures} recent failure(s) at {env.dominant_step} — environment degraded but not consistently failing",
                "environment",
            )

    # Priority 5: Env CAUTION + strong historical match
    if env.status == "CAUTION" and historical.score >= 0.65:
        confidence = (env.score + historical.score) / 2
        return (
            "HOLD",
            f"FAIL at {env.dominant_step or historical.dominant_step}",
            confidence,
            f"Degraded environment + {int(historical.score*100)}% similarity to past failures",
            "environment+history"
        )

    # Priority 6: Env CAUTION — only escalate if at least one other signal supports it.
    # A single past failure (consecutive=1) with LOW code risk and no history
    # should NOT block a deploy. HDFC regularly has 1 infra failure followed by
    # multiple successful deploys. Requiring corroboration reduces false positives.
    if env.status == "CAUTION" and (code.score > 0.20 or historical.score > 0.30 or env.consecutive_failures >= 2):
        return (
            "CAUTION",
            f"Possible FAIL at {env.dominant_step}" if env.dominant_step else "Possible failure",
            env.score * 0.8,
            f"{env.consecutive_failures} recent failures at {env.dominant_step}",
            "environment"
        )

    # Priority 7: Strong historical match
    if historical.score >= 0.70:
        return (
            "CAUTION",
            f"Possible FAIL at {historical.dominant_step}" if historical.dominant_step else "Possible failure",
            historical.score * 0.75,
            f"{historical.match_count} similar past incidents, avg {int(historical.score*100)}% match",
            "history"
        )

    # Priority 8: Medium historical + high code risk
    if historical.score >= 0.50 and code.score >= 0.50:
        confidence = (historical.score + code.score) / 2
        return (
            "CAUTION",
            "Possible failure — review before promoting",
            confidence,
            "Combination of code risk and historical pattern",
            "code+history"
        )

    # Priority 8b: LLM-only commit risk
    # Normally only elevate when at least one data signal also shows elevated risk.
    # Exception: when LLM is very confident (≥0.75) on a real code issue.
    #
    # Hard constraints — LLM cannot override these:
    # 1. Bot/automated commits with LOW code risk: the commit itself has no risk regardless
    #    of env state. Env failures are standing ops issues, not caused by this commit.
    # 2. Persistent infra env (CRXDE/DavEx) + LOW code: env is a standing advisory,
    #    not a per-commit HOLD trigger. Every commit would get HOLD which is useless.
    _data_signals_clear = (
        env.status == "READY" and
        code.score <= 0.30 and
        historical.score < 0.45
    )
    _bot_commit_low_code = code.is_submodule_only and code.score <= 0.20
    _persistent_infra_low_code = env.is_persistent_infra and code.score <= 0.20
    # Also block LLM when env failure is any infrastructure step (not just persistent CRXDE)
    # and code is genuinely LOW. The LLM sees submodule diffs and guesses Medium/55 —
    # but if structural analysis shows no real risk and env fails at securityTest/deploy,
    # the LLM is just noise-matching. Block it from issuing CAUTION.
    _env_infra_low_code = env.is_env_issue and code.score <= 0.25 and not code.findings
    # Block LLM when code has no real findings and is submodule-only.
    # This is the "conf=55 default" problem: LLM sees submodule SHA bumps, can't tell
    # what changed inside them, guesses Medium. Structural analysis found nothing.
    # The LLM is noise-matching — there's nothing actionable it can tell us.
    # Only override if LLM is VERY confident (≥0.75) with a specific rationale.
    _submodule_no_findings = (
        code.is_submodule_only
        and not code.findings
        and not code.has_real_code
    )
    _llm_very_confident = llm.has_analysis and llm.score >= 0.75
    # LLM blocked from HOLD/CAUTION when code is genuinely LOW on bot/infra-noise commits
    _llm_blocked = _bot_commit_low_code or _persistent_infra_low_code or _env_infra_low_code
    # For submodule-only with no findings, cap LLM score at LOW threshold (0.45)
    # so it can't trigger CAUTION even if it returned Medium — unless very confident
    _llm_score_eff = (
        min(llm.score, 0.45)
        if _submodule_no_findings and not _llm_very_confident
        else llm.score
    )
    if llm.has_analysis and _llm_score_eff >= 0.55 and llm.commit_risk_step and (
        not _data_signals_clear or _llm_very_confident
    ) and not _llm_blocked:
        _step = llm.commit_risk_step
        return (
            "CAUTION",
            f"FAIL at {_step}",
            _llm_score_eff,
            f"LLM analysis: {llm.detail}",
            "llm",
        )

    # Default: GO
    confidence = 1.0 - max(env.score, code.score, historical.score * 0.5, llm.score * 0.3)
    return (
        "GO",
        "PASS — low risk based on available signals",
        max(confidence, 0.30),
        "No strong failure signals detected",
        "none"
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def score_risk(
    bundle_dict: dict,
    diff_text: str = "",
    changed_files: List[str] = None,
    commit_title: str = "",
    repo_dir: str = "",
    pipeline_df=None,
    failed_df=None,
    program_id: str = "",
    dev_execution_status: str = "",
    pipeline_name: Optional[str] = None,
    as_of_date: Optional[str] = None,
) -> RiskDecision:
    """
    Compute all three signals and return a final RiskDecision.
    program_id enables per-tenant calibration of thresholds.
    LLM narrative is filled in separately (set decision.narrative after LLM call).
    """
    # Infer program_id from bundle if not passed explicitly
    if not program_id:
        program_id = str(bundle_dict.get("program_id", "") or "")

    # Dev pipeline outcome is ground truth — no model needed
    # If dev pipeline failed, code has a real problem regardless of other signals
    # If dev pipeline passed, code risk is reduced (but env risk remains)
    _empty_llm = LlmSignal(
        score=0.0, dominant_step="", commit_risk_step="", dominant_level="LOW",
        step_scores={}, detail="", has_analysis=False,
    )

    if dev_execution_status == "FAILED":
        return RiskDecision(
            recommendation="HOLD",
            expected_outcome="FAIL at build/codeQuality — confirmed by dev pipeline",
            confidence=0.85,
            confidence_basis="Dev pipeline FAILED — this is ground truth, not a prediction",
            env=compute_env_signal(
                pipeline_df, failed_df, program_id=program_id, pipeline_name=pipeline_name
            ),
            code=CodeSignal(level="HIGH", score=0.85, detail="Dev pipeline failed — code has confirmed issue",
                           findings=["Dev pipeline FAILED"], is_submodule_only=False, has_real_code=True),
            historical=HistoricalSignal(score=0.0, match_count=0, dominant_step="", fail_rate=0.0,
                                        detail="Dev failure is ground truth", examples=[]),
            llm=_empty_llm,
            narrative="",
            primary_driver="dev_pipeline",
        )

    # Dev pipeline passed → build + unit tests already validated by ground truth.
    # Do not predict build failure for a commit that already passed build.
    # Inject this as a signal that suppresses build-step risk in _pick_failure_step.
    _dev_passed = dev_execution_status == "FINISHED"

    # Signal 1: Environment — use as_of_date to avoid using future failures
    # when assessing a historical SHA (e.g. SHA from June 17 should not see
    # June 25 failures that happened after it was deployed)
    env = compute_env_signal(
        pipeline_df, failed_df, program_id=program_id, pipeline_name=pipeline_name,
        as_of_date=as_of_date
    )

    # Signal 2: Code
    _java_upgrade = infer_java_upgrade_pending(
        bundle_dict,
        pipeline_df,
        str(bundle_dict.get("execution_id") or bundle_dict.get("dev_execution_id") or ""),
    )
    code = compute_code_signal(
        diff_text=diff_text or "",
        changed_files=changed_files or [],
        commit_title=commit_title or "",
        repo_dir=repo_dir or "",
        submodule_diffs=bundle_dict.get("submodule_diffs") or None,
        java_upgrade_pending=_java_upgrade,
    )

    # Signal 3: Historical
    historical = compute_historical_signal(bundle_dict)

    # Signal 4: LLM step risks (parsed after env so commit_risk_step can deprioritize infra)
    llm = compute_llm_signal(bundle_dict, env=env)

    # Decision
    rec, outcome, conf, basis, driver = make_decision(env, code, historical, llm)

    # ── Confidence calibration ─────────────────────────────────────────────────
    # Tie confidence to the specific driver, not a generic blended score.

    if driver == "code" and code.findings:
        # Structural finding confidence comes from BuildFinding.confidence (78-90%)
        # Parse from finding string like "[HIGH] ... (85% certain)"
        import re as _re_conf
        _m = _re_conf.search(r'\((\d+)%', code.findings[0] if code.findings else "")
        conf = int(_m.group(1)) / 100 if _m else code.score

    elif driver in ("environment", "environment+history", "environment_ops"):
        conf = env.failure_probability

    elif driver in ("llm", "llm+code"):
        conf = max(llm.score, code.score * 0.5)

    elif driver == "code+environment_ops":
        conf = max(code.score, env.failure_probability * 0.5)

    elif driver == "history":
        # History confidence = similarity score × 0.9, max 85%
        conf = min(historical.score * 0.9, 0.85)

    elif driver == "none":
        # GO — confidence = 1 - max signal, capped 30-70%
        conf = max(0.30, min(0.70, 1.0 - max(env.score, code.score, historical.score * 0.5)))

    # Submodule-only with no execution linked → hard cap 35%
    if code.is_submodule_only and not historical.match_count:
        conf = min(conf, 0.35)

    # Penalize conflicting signals: env says HOLD but code says LOW (or vice versa)
    # Must cover both "environment" and "environment_ops" drivers
    if rec in ("HOLD", "CAUTION") and code.level == "LOW" and driver in ("environment", "environment_ops"):
        conf *= 0.75
        basis = f"{basis} [confidence reduced: env risk but code LOW — ops issue may not affect this commit]"
    elif rec == "HOLD" and code.level in ("HIGH", "CERTAIN") and env.status == "READY":
        conf *= 0.85  # code finding strong but env is healthy — slight uncertainty

    # LLM-only build risk when structural analysis is clean — reduce overconfidence
    if (
        rec in ("HOLD", "CAUTION")
        and driver == "llm"
        and code.level == "LOW"
        and not code.findings
        and env.status == "READY"
        and not code.code_caused_perf
    ):
        conf = min(conf, 0.55)
        basis = (
            f"{basis} [confidence reduced: LLM flags build risk but structural "
            f"analysis found no patterns]"
        )

    # Code-caused perf regression — honest confidence cap
    if code.code_caused_perf and driver == "code":
        conf = min(conf, 0.55)

    conf = max(0.10, min(0.95, conf))

    return RiskDecision(
        recommendation=rec,
        expected_outcome=outcome,
        confidence=round(conf, 3),
        confidence_basis=basis,
        env=env,
        code=code,
        historical=historical,
        llm=llm,
        narrative="",
        primary_driver=driver,
    )
