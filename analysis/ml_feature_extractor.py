"""
Extract factual, ML-ready features from git diffs and Splunk history.

No LLM outputs, risk levels, or diagnosis text — only deterministic signals
suitable for future XGBoost training.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from analysis.aem_modules import get_changed_modules
from analysis.build_predictor import BuildPrediction, predict_build_failures
from analysis.diff_analyzer import DiffSignals, analyze_diff
from analysis.env_readiness import DEFAULT_PROD_PIPELINE, assess_environment_readiness


def _count_diff_lines(diff_text: str) -> tuple[int, int]:
    added = removed = 0
    for line in (diff_text or "").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def _has_snapshot_dep(signals: DiffSignals) -> bool:
    for dep in signals.maven_deps_added:
        ver = (dep.version or "").upper()
        if "SNAPSHOT" in ver:
            return True
    return False


def _structural_check_flags(findings: list) -> Dict[str, bool]:
    """One boolean per structural check type that fired."""
    flags: Dict[str, bool] = {}
    for f in findings:
        check = getattr(f, "check", None) or (f.get("check") if isinstance(f, dict) else "")
        if check:
            flags[f"check_{check}"] = True
    return flags


def _pipeline_history_features(
    pipeline_df,
    failed_df,
    pipeline_name: str = DEFAULT_PROD_PIPELINE,
    as_of_date: str = "",
) -> Dict[str, Any]:
    """Rolling 30-day pipeline failure stats from Splunk data only."""
    out = {
        "pipeline_30d_total": 0,
        "pipeline_30d_failures": 0,
        "pipeline_30d_fail_rate": 0.0,
        "days_since_last_success": -1,
    }
    if pipeline_df is None or getattr(pipeline_df, "empty", True):
        return out

    try:
        import pandas as pd
        from analysis.ingest import normalize_pipeline_df

        df = normalize_pipeline_df(pipeline_df)
        if pipeline_name and "pipelineName" in df.columns:
            scoped = df[df["pipelineName"] == pipeline_name]
            if not scoped.empty:
                df = scoped

        if as_of_date and "Deploy Start Time" in df.columns:
            cutoff = pd.to_datetime(as_of_date, utc=True, errors="coerce")
            if cutoff is not None and not pd.isna(cutoff):
                ts = pd.to_datetime(
                    df["Deploy Start Time"].astype(str).str.replace(
                        r"\s*(PDT|PST|UTC|GMT)$", "", regex=True
                    ),
                    utc=True,
                    errors="coerce",
                )
                df = df[ts <= cutoff].copy()
                ref = cutoff
            else:
                ref = pd.Timestamp.now(tz="UTC")
        else:
            ref = pd.Timestamp.now(tz="UTC")

        if "Deploy Start Time" in df.columns:
            ts = pd.to_datetime(
                df["Deploy Start Time"].astype(str).str.replace(
                    r"\s*(PDT|PST|UTC|GMT)$", "", regex=True
                ),
                utc=True,
                errors="coerce",
            )
            window_start = ref - pd.Timedelta(days=30)
            recent = df[(ts >= window_start) & (ts <= ref)]
        else:
            recent = df.tail(200)

        if recent.empty:
            return out

        statuses = recent["Status"].astype(str).str.upper() if "Status" in recent.columns else []
        terminal = recent[statuses.isin(["FINISHED", "FAILED", "ERROR"])] if len(statuses) else recent
        failures = terminal[terminal["Status"].astype(str).str.upper().isin(["FAILED", "ERROR"])]
        total = len(terminal)
        fail_n = len(failures)
        out["pipeline_30d_total"] = int(total)
        out["pipeline_30d_failures"] = int(fail_n)
        out["pipeline_30d_fail_rate"] = round(fail_n / total, 4) if total else 0.0

        finished = recent[recent["Status"].astype(str).str.upper() == "FINISHED"]
        if not finished.empty and "Deploy Start Time" in finished.columns:
            last_ts = pd.to_datetime(
                finished["Deploy Start Time"].astype(str).str.replace(
                    r"\s*(PDT|PST|UTC|GMT)$", "", regex=True
                ),
                utc=True,
                errors="coerce",
            ).max()
            if last_ts is not None and not pd.isna(last_ts):
                out["days_since_last_success"] = int((ref - last_ts).days)
    except Exception:
        pass

    return out


def _parse_days_since_success(last_success_ago: str) -> int:
    """Parse env_readiness last_success_ago like '2 days ago' → 2."""
    if not last_success_ago or last_success_ago == "unknown":
        return -1
    m = re.search(r"(\d+)\s*day", last_success_ago.lower())
    if m:
        return int(m.group(1))
    if "today" in last_success_ago.lower() or "just" in last_success_ago.lower():
        return 0
    return -1


def extract_ml_features(
    commit_sha: str,
    diff_text: str,
    changed_files: List[str],
    commit_title: str = "",
    commit_author: str = "",
    commit_date: str = "",
    repo_dir: str = "",
    pipeline_df=None,
    failed_df=None,
    pipeline_name: str = DEFAULT_PROD_PIPELINE,
    program_id: str = "",
    as_of_date: str = "",
    build_prediction: Optional[BuildPrediction] = None,
    submodule_diffs: Optional[Dict[str, str]] = None,
    java_upgrade_pending: bool = False,
) -> Dict[str, Any]:
    """
    Return a flat dict of factual features for one commit observation.
  Does not call the LLM.
    """
    diff_text = diff_text or ""
    changed_files = list(changed_files or [])
    signals = analyze_diff(diff_text, changed_files, title=commit_title)
    lines_added, lines_removed = _count_diff_lines(diff_text)
    modules = get_changed_modules(changed_files)

    if build_prediction is None:
        build_prediction = predict_build_failures(
            diff_text=diff_text,
            changed_files=changed_files,
            commit_title=commit_title,
            repo_dir=repo_dir,
            submodule_diffs=submodule_diffs,
            java_upgrade_pending=java_upgrade_pending,
        )

    findings = build_prediction.findings or []
    check_flags = _structural_check_flags(findings)
    high_count = sum(
        1 for f in findings
        if getattr(f, "severity", "") in ("HIGH", "CERTAIN")
        or (isinstance(f, dict) and f.get("severity") in ("HIGH", "CERTAIN"))
    )

    env = assess_environment_readiness(
        pipeline_df, failed_df,
        pipeline_name=pipeline_name or DEFAULT_PROD_PIPELINE,
        as_of_date=as_of_date or commit_date or None,
    )
    hist = _pipeline_history_features(
        pipeline_df, failed_df,
        pipeline_name=pipeline_name or DEFAULT_PROD_PIPELINE,
        as_of_date=as_of_date or commit_date or "",
    )

    days_since = hist["days_since_last_success"]
    if days_since < 0:
        days_since = _parse_days_since_success(env.get("last_success_ago", ""))

    features: Dict[str, Any] = {
        # git / diff
        "changed_files_count": len(changed_files),
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "has_pom_change": bool(signals.has_pom_change),
        "has_java_change": bool(signals.has_java_change),
        "has_npm_change": bool(signals.has_npm_change),
        "has_config_change": bool(signals.has_config_change),
        "dispatcher_changed": bool(signals.dispatcher_changed),
        "is_subtree_import": bool(signals.is_subtree_import),
        "is_deletion_only": bool(signals.is_deletion_only),
        "maven_deps_added_count": len(signals.maven_deps_added),
        "maven_deps_removed_count": len(signals.maven_deps_removed),
        "has_snapshot_dep": _has_snapshot_dep(signals),
        "osgi_new_reference_count": sum(
            1 for s in signals.osgi_signals if s.signal_type == "new_reference"
        ),
        "interface_changes_count": len(signals.interface_changes),
        "vault_filter_changes_count": len(signals.vault_filter_changes),
        "modules_touched": modules,
        "modules_touched_count": len(modules),
        # structural checks (rule-based)
        "structural_finding_count": len(findings),
        "structural_high_count": high_count,
        "structural_override_llm": bool(build_prediction.override_llm),
        **check_flags,
        # environment (Splunk-only)
        "env_status": env.get("status", "UNKNOWN"),
        "env_consecutive_failures": int(env.get("consecutive_failures", 0) or 0),
        "env_dominant_step": env.get("dominant_step", "") or "",
        "env_is_env_issue": bool(env.get("is_env_issue", False)),
        "env_step_failure_count": int(env.get("env_step_failure_count", 0) or 0),
        # pipeline history
        "pipeline_30d_total": hist["pipeline_30d_total"],
        "pipeline_30d_failures": hist["pipeline_30d_failures"],
        "pipeline_30d_fail_rate": hist["pipeline_30d_fail_rate"],
        "days_since_last_success": days_since,
        # commit metadata
        "commit_author": (commit_author or "")[:120],
        "commit_title_length": len(commit_title or ""),
        "commit_date": (commit_date or as_of_date or "")[:10],
    }

    return features
