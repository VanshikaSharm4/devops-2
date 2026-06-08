"""
Offline smoke tests.

These tests must run without Splunk, Azure, Cloud Manager Git, or LLM
credentials. They validate the CSV fallback, parser contracts, Pydantic models,
failure history, and deterministic risk rules.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from analysis.aem_modules import detect_env_references_in_diff, get_changed_modules
from analysis.failure_history import build_failure_history
from analysis.ingest import build_base_bundle
from analysis.risk_rules import compute_rule_scores
from connectors.splunk_csv_reader import (
    get_failed_executions,
    load_failed_steps,
    load_pipeline_list,
    summarize_failures,
)
from models.bundle import AnalysisBundle, ErrorDetail, ExecutionSummary, GitContext
from models.risk_report import RiskReport
from parsers.log_parser import parse_build_log, parse_deploy_log, parse_security_test_log


PIPELINE_CSV = ROOT / "data/splunk_exports/pipelines-list.csv"
FAILED_STEP_CSV = ROOT / "data/splunk_exports/first-failed-steps.csv"

SAMPLE_BUILD_LOG = """
Module not found: Error: Can't resolve '@react-pdf-viewer/core' in '/build_root/build/idfc/idfcfirst-academy/ui.frontend/src/component/NewComicIframe'
BUILD FAILURE
"""

SAMPLE_SECURITY_LOG = """author1
  CRXDE Support - Failed
    WARN - The com.adobe.granite.crx-explorer bundle is active.
"""

SAMPLE_DEPLOY_LOG = """
Config variables are not defined: PUBLISH_IDFC_HOSTNAME_NEW
Syntax error on line 2039 of /etc/httpd/conf.d/rewrites/rewrite-onpremises-migration.conf: RewriteRule: bad flag delimiters
"""


def _load_csv_bundle():
    pipeline_df = load_pipeline_list(str(PIPELINE_CSV))
    failed_steps_df = load_failed_steps(str(FAILED_STEP_CSV))
    merged_df = get_failed_executions(pipeline_df, failed_steps_df)
    summary = summarize_failures(merged_df)
    return pipeline_df, failed_steps_df, merged_df, summary


def test_csv_fallback_has_execution_data():
    pipeline_df, failed_steps_df, merged_df, summary = _load_csv_bundle()

    assert len(pipeline_df) > 0
    assert len(failed_steps_df) > 0
    assert len(merged_df) > 0
    assert len(summary) > 0
    assert "executionId" in pipeline_df.columns


def test_log_parsers_return_pydantic_models():
    build = parse_build_log(SAMPLE_BUILD_LOG)
    assert build.error_type == "missing_npm_module"
    assert "react-pdf-viewer" in build.error_message

    security = parse_security_test_log(SAMPLE_SECURITY_LOG)
    assert security.error_type == "security_failure"
    assert len(security.errors) > 0

    deploy = parse_deploy_log(SAMPLE_DEPLOY_LOG)
    assert deploy.error_type == "missing_env_variable"
    assert len(deploy.errors) >= 1


def test_pydantic_models_accept_minimal_valid_payloads():
    bundle = AnalysisBundle(
        execution_summary=ExecutionSummary(
            total_executions=100,
            finished=7,
            failed_or_error=16,
            cancelled=77,
            success_rate_pct=7.0,
        ),
        failure_patterns=[],
    )
    assert bundle.program_id == "19905"
    assert "summary" in bundle.to_findings_dict()

    report = RiskReport(
        risk_level="High",
        confidence_score=70,
        most_likely_failure_step="build",
        recommended_actions=["Fix npm deps"],
    )
    assert report.risk_level == "High"


def test_aem_module_and_env_detection():
    files = [
        "ui.frontend/src/component/NewComicIframe/index.js",
        "dispatcher/src/conf.d/rewrite-onpremises-migration.conf",
    ]
    modules = get_changed_modules(files)

    assert "ui.frontend" in modules
    assert "dispatcher" in modules
    assert "PUBLISH_IDFC_HOSTNAME_NEW" in detect_env_references_in_diff(
        "PUBLISH_IDFC_HOSTNAME_NEW"
    )


def test_failure_history_and_rule_scores():
    pipeline_df, _, merged_df, summary = _load_csv_bundle()
    errors = [
        ErrorDetail(
            execution_id="1",
            failed_step="build",
            parsed_error={
                "error_type": "missing_npm_module",
                "error_message": "Missing npm package: @react-pdf-viewer/core",
                "module": "NewComicIframe",
            },
        )
    ]
    history = build_failure_history(merged_df, summary, errors, pipeline_df)

    bundle = AnalysisBundle(
        execution_summary=ExecutionSummary(
            total_executions=113,
            finished=8,
            failed_or_error=16,
            cancelled=89,
            success_rate_pct=7.0,
        ),
        failure_history=history,
        git_context=GitContext(
            commit_sha="offline-smoke",
            changed_files=["ui.frontend/src/component/NewComicIframe/index.js"],
            aem_modules_touched=["ui.frontend"],
            diff_excerpt="import from '@react-pdf-viewer/core'",
        ),
    )
    scores = compute_rule_scores(bundle)

    assert scores.build in ("HIGH", "MEDIUM", "LOW")
    assert scores.deploy in ("HIGH", "MEDIUM", "LOW")
    assert len(scores.reasons) > 0


def test_base_bundle_uses_csv_without_logs():
    bundle, _, _, _ = build_base_bundle(
        fetch_logs=False,
        include_history=True,
        force_csv=True,
    )

    assert bundle.execution_summary.total_executions > 0
    assert bundle.failure_history is not None


def test_git_connector_uses_first_parent_for_merge_diff():
    import connectors.git_connector as git_connector

    calls = []
    original_git = git_connector._git

    def fake_git(*args, cwd=None):
        calls.append(args)
        if args == ("rev-list", "--parents", "-n", "1", "merge-sha"):
            return "merge-sha parent-one parent-two\n"
        if args == (
            "diff",
            "--find-renames",
            "--name-only",
            "parent-one",
            "merge-sha",
        ):
            return "ui.apps/src/main/content/example.js\ncore/pom.xml\n"
        raise AssertionError(f"unexpected git call: {args}")

    try:
        git_connector._git = fake_git
        files = git_connector._changed_files_for_commit("merge-sha")
    finally:
        git_connector._git = original_git

    assert files == [
        "ui.apps/src/main/content/example.js",
        "core/pom.xml",
    ]
    assert (
        "diff",
        "--find-renames",
        "--name-only",
        "parent-one",
        "merge-sha",
    ) in calls


def test_rules_only_risk_report_is_valid():
    import analysis.risk_analyzer as risk_analyzer

    original_get_commit_diff = risk_analyzer.get_commit_diff

    def fake_get_commit_diff(repo, sha):
        return {
            "title": "Upgrade core dependency",
            "body": "",
            "author": "Test Author",
            "changed_files": ["core/pom.xml"],
            "diff_excerpt": (
                "- <artifactId>old-lib</artifactId>\n"
                "+ <artifactId>new-lib</artifactId>\n"
            ),
        }

    bundle = AnalysisBundle(
        execution_summary=ExecutionSummary(
            total_executions=10,
            finished=8,
            failed_or_error=2,
            cancelled=0,
            success_rate_pct=80.0,
        ),
        failure_patterns=[],
    )

    try:
        risk_analyzer.get_commit_diff = fake_get_commit_diff
        _, report, _ = risk_analyzer.run_pre_deploy_risk(
            commit_sha="abc123",
            use_llm=False,
            bundle=bundle,
        )
    finally:
        risk_analyzer.get_commit_diff = original_get_commit_diff

    assert report is not None
    assert report.confidence_score > 0
    assert report.commit_sha == "abc123"
    assert report.risk_level == "High"
    assert report.most_likely_failure_step == "build"


def test_reactor_module_toggle_is_medium_packaging_risk():
    from analysis.commit_analyzer import (
        analyze_commit,
        infer_change_intent,
        infer_failure_modes,
    )

    diff = """diff --git a/pom.xml b/pom.xml
@@ -32,7 +32,7 @@
-<module>idfcfirst-academy</module>
+<!-- <module>idfcfirst-academy</module> -->
"""
    profile = analyze_commit(
        ["pom.xml"],
        diff,
        commit_sha="reactor-toggle",
        title="Updated pom.xml file as per build parameters.",
    )

    bundle = AnalysisBundle(
        execution_summary=ExecutionSummary(
            total_executions=113,
            finished=8,
            failed_or_error=30,
            cancelled=75,
            success_rate_pct=7.1,
        ),
        failure_history=build_failure_history(
            merged_df=_load_csv_bundle()[2],
            failure_patterns=_load_csv_bundle()[3],
            error_details=[],
            pipeline_df=_load_csv_bundle()[0],
        ),
        git_context=GitContext(
            commit_sha="reactor-toggle",
            changed_files=["pom.xml"],
            aem_modules_touched=["core"],
            diff_excerpt=diff,
            title="Updated pom.xml file as per build parameters.",
        ),
    )
    scores = compute_rule_scores(bundle)

    assert profile.has_reactor_module_changes
    assert profile.removed_reactor_modules == ["idfcfirst-academy"]
    assert profile.added_dependencies == []
    assert infer_change_intent(profile.title, profile) == "config_change"
    assert infer_failure_modes(profile, diff) == ["deployment_ordering_issue"]
    assert scores.build == "LOW"
    assert scores.deploy == "MEDIUM"
    assert scores.securityTest == "HIGH"


def test_failure_memory_excludes_risk_predictions_by_default():
    from vector_store.store import _is_risk_prediction_meta

    assert _is_risk_prediction_meta({
        "execution_id": "risk-8d024d54-build",
        "pipeline": "risk_analysis",
        "error_type": "risk_prediction_build",
    })
    assert not _is_risk_prediction_meta({
        "execution_id": "8161494",
        "pipeline": "Production Pipeline",
        "error_type": "security_failure",
    })


def main() -> None:
    tests = [
        test_csv_fallback_has_execution_data,
        test_log_parsers_return_pydantic_models,
        test_pydantic_models_accept_minimal_valid_payloads,
        test_aem_module_and_env_detection,
        test_failure_history_and_rule_scores,
        test_base_bundle_uses_csv_without_logs,
        test_git_connector_uses_first_parent_for_merge_diff,
        test_rules_only_risk_report_is_valid,
        test_reactor_module_toggle_is_medium_packaging_risk,
        test_failure_memory_excludes_risk_predictions_by_default,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print("ALL OFFLINE SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
