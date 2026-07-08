"""Tests for build_predictor structural rules."""

from analysis.build_predictor import (
    predict_build_failures,
    _check_dao_migration_perf_risk,
    _check_multi_submodule_reactor,
    _check_service_without_test_update,
    _is_merge_commit,
)
from analysis.diff_analyzer import analyze_diff
from analysis.risk_scorer import (
    CodeSignal,
    EnvSignal,
    HistoricalSignal,
    LlmSignal,
    compute_code_signal,
    compute_llm_signal,
    make_decision,
    code_recommendation,
)


def test_service_without_test_update_flags_surefire_npe_pattern():
    diff = """diff --git a/core/src/main/java/com/hdfc/ValidationServiceFatcaV2.java b/core/src/main/java/com/hdfc/ValidationServiceFatcaV2.java
--- a/core/src/main/java/com/hdfc/ValidationServiceFatcaV2.java
+++ b/core/src/main/java/com/hdfc/ValidationServiceFatcaV2.java
@@ -10,6 +10,7 @@ public class ValidationServiceFatcaV2 {
     public void doValidate() {
+        customerInfo.fetch();
     }
"""
    findings = _check_service_without_test_update(diff)
    assert findings
    assert findings[0].step == "build"
    assert findings[0].severity in ("HIGH", "MEDIUM")


def test_multi_submodule_bump_detected():
    parent_diff = """diff --git a/pom.xml b/pom.xml
--- a/pom.xml
+++ b/pom.xml
@@ -1,5 +1,6 @@
+    <module>hdfcbankcustomerinfo</module>
diff --git a/hdfcbankcustomerinfo b/hdfcbankcustomerinfo
--- a/hdfcbankcustomerinfo
+++ b/hdfcbankcustomerinfo
@@ -1 +1 @@
-Subproject commit abc123
+Subproject commit def456
diff --git a/hdfcbankformscommon b/hdfcbankformscommon
--- a/hdfcbankformscommon
+++ b/hdfcbankformscommon
@@ -1 +1 @@
-Subproject commit aaa111
+Subproject commit bbb222
"""
    signals = analyze_diff(parent_diff, ["pom.xml", ".gitmodules"])
    findings = _check_multi_submodule_reactor(parent_diff, ["pom.xml"], signals)
    assert any(f.check == "multi_submodule_bump" for f in findings)


def test_submodule_java_merged_for_analysis():
    parent_diff = """diff --git a/hdfcbankcustomerinfo b/hdfcbankcustomerinfo
--- a/hdfcbankcustomerinfo
+++ b/hdfcbankcustomerinfo
@@ -1 +1 @@
-Subproject commit old
+Subproject commit new
"""
    sm_diff = """diff --git a/core/src/main/java/com/hdfc/ValidationService.java b/core/src/main/java/com/hdfc/ValidationService.java
--- a/core/src/main/java/com/hdfc/ValidationService.java
+++ b/core/src/main/java/com/hdfc/ValidationService.java
@@ -1,5 +1,6 @@
 public class ValidationService {
+    void newLogic() {}
"""
    pred = predict_build_failures(
        parent_diff,
        ["hdfcbankcustomerinfo", "pom.xml"],
        "bump submodules",
        submodule_diffs={"hdfcbankcustomerinfo": sm_diff},
    )
    assert pred.findings
    assert pred.predicted_step == "build"
    assert pred.predicted_risk in ("Medium", "High")


def test_code_beats_env_ops_for_hdfc_pattern():
    """Medium code findings fire before env_ops — step should be build, not securityTest."""
    env = EnvSignal(
        status="NOT_READY", score=0.7, consecutive_failures=4,
        dominant_step="securityTest", last_success_ago="13 days ago",
        detail="", fix="", failure_probability=0.7, hold_threshold=3,
        is_persistent_infra=True,
    )
    code = CodeSignal(
        level="MEDIUM", score=0.50,
        detail="Submodule bump",
        findings=["[MEDIUM] 2 submodule pointer(s) bumped (62% likely)"],
        is_submodule_only=False, has_real_code=True,
    )
    hist = HistoricalSignal(
        score=0.36, match_count=4, dominant_step="build",
        fail_rate=0.3, detail="", examples=[],
    )
    rec, outcome, conf, basis, driver = make_decision(env, code, hist, llm=LlmSignal(
        score=0, dominant_step="", commit_risk_step="", dominant_level="LOW",
        step_scores={}, detail="", has_analysis=False,
    ))
    assert rec == "CAUTION"
    assert "build" in outcome
    assert driver == "code"
    assert "securityTest" not in outcome


def test_code_plus_env_ops_when_code_low_but_history_build():
    """When code is LOW but history points to build and env is ops noise."""
    env = EnvSignal(
        status="NOT_READY", score=0.7, consecutive_failures=4,
        dominant_step="securityTest", last_success_ago="13 days ago",
        detail="", fix="", failure_probability=0.7, hold_threshold=3,
        is_persistent_infra=True,
    )
    code = CodeSignal(
        level="LOW", score=0.25,
        detail="Java changes",
        findings=[],
        is_submodule_only=False, has_real_code=True,
    )
    hist = HistoricalSignal(
        score=0.36, match_count=4, dominant_step="build",
        fail_rate=0.3, detail="", examples=[],
    )
    rec, outcome, _, _, driver = make_decision(env, code, hist, llm=LlmSignal(
        score=0, dominant_step="", commit_risk_step="", dominant_level="LOW",
        step_scores={}, detail="", has_analysis=False,
    ))
    assert rec == "CAUTION"
    assert "build" in outcome
    assert driver == "code+environment_ops"


def test_llm_build_medium_overrides_env_securitytest():
    """LLM build=Medium + securityTest=High (env) → predict build, not securityTest."""
    env = EnvSignal(
        status="NOT_READY", score=0.7, consecutive_failures=4,
        dominant_step="securityTest", last_success_ago="13 days ago",
        detail="", fix="", failure_probability=0.7, hold_threshold=3,
        is_persistent_infra=True,
    )
    code = CodeSignal(
        level="LOW", score=0.25,
        detail="Java, pom.xml changes",
        findings=[],
        is_submodule_only=False, has_real_code=True,
    )
    hist = HistoricalSignal(
        score=0.36, match_count=4, dominant_step="build",
        fail_rate=0.3, detail="", examples=[],
    )
    llm = compute_llm_signal({
        "llm_step_risks": [
            {"step": "build", "level": "Medium",
             "rationale": "Reactor module list changed; submodule bumps may break build"},
            {"step": "securityTest", "level": "High",
             "rationale": "Environment NOT_READY — CRXDE/DavEx infra issue"},
            {"step": "deploy", "level": "Medium", "rationale": "Package ordering risk"},
        ],
        "llm_most_likely_step": "securityTest",
    }, env=env)

    assert llm.commit_risk_step == "build"
    rec, outcome, _, _, driver = make_decision(env, code, hist, llm=llm)
    assert "build" in outcome
    assert "securityTest" not in outcome.split("FAIL at ")[-1]
    assert driver in ("llm", "llm+code")
    assert rec in ("CAUTION", "HOLD")


def test_reactor_module_list_churn():
    pom_diff = """diff --git a/pom.xml b/pom.xml
--- a/pom.xml
+++ b/pom.xml
@@ -1,10 +1,10 @@
-\t\t<!--<module>hdfcformscommon-v2/core</module> -->
+\t\t<module>hdfcformscommon-v2/core</module>
-\t\t<module>hdfcbankloanforms/core</module>
+\t\t<!--<module>hdfcbankloanforms/core</module>-->
+\t\t<module>hdfcbankccunified/core</module>
-\t\t<module>hdfcformskycservice/core</module>
+\t\t<!--<module>hdfcformskycservice/core</module>-->
"""
    from analysis.diff_analyzer import analyze_diff
    signals = analyze_diff(pom_diff, ["pom.xml"])
    from analysis.build_predictor import _check_multi_submodule_reactor
    findings = _check_multi_submodule_reactor(pom_diff, ["pom.xml"], signals)
    assert any(f.check == "reactor_module_list_churn" for f in findings)
    env = EnvSignal(
        status="NOT_READY", score=0.7, consecutive_failures=4,
        dominant_step="securityTest", last_success_ago="13 days ago",
        detail="", fix="", failure_probability=0.7, hold_threshold=3,
        is_persistent_infra=True,
    )
    code = CodeSignal(
        level="HIGH", score=0.85,
        detail="ValidationService changed",
        findings=["[HIGH] ValidationService changed — no unit test updated (85% likely)"],
        is_submodule_only=False, has_real_code=True,
    )
    hist = HistoricalSignal(score=0.0, match_count=0, dominant_step="", fail_rate=0.0, detail="", examples=[])
    rec, outcome, _, _, driver = make_decision(env, code, hist, llm=LlmSignal(
        score=0, dominant_step="", commit_risk_step="", dominant_level="LOW",
        step_scores={}, detail="", has_analysis=False,
    ))
    assert rec == "HOLD"
    assert outcome == "FAIL at build"
    assert driver == "code"


def test_dao_migration_flags_loadtest_perf_risk():
    title = "Merge pull request #2339 from mhaem/49990-LTS-DAO-upgrade-bhavya-optimized"
    diff = """diff --git a/core/src/main/java/com/example/dao/CustomerDao.java b/core/src/main/java/com/example/dao/CustomerDao.java
--- a/core/src/main/java/com/example/dao/CustomerDao.java
+++ b/core/src/main/java/com/example/dao/CustomerDao.java
@@ -10,6 +10,9 @@ public class CustomerDao {
+    public List<Customer> findAllLts() {
+        return repository.fetchAll();
+    }
"""
    files = ["core/src/main/java/com/example/dao/CustomerDao.java", "core/pom.xml"]
    findings = _check_dao_migration_perf_risk(diff, files, title)
    assert any(f.check == "dao_migration_perf_risk" and f.step == "loadTest" for f in findings)

    pred = predict_build_failures(diff, files, title)
    assert any(f.step == "loadTest" for f in pred.findings)
    assert pred.predicted_step == "loadTest"


def test_dao_migration_beats_llm_build_on_healthy_env():
    """Malaysia-style case: LLM build HIGH + clean env + DAO migration → loadTest, capped confidence."""
    env = EnvSignal(
        status="READY", score=0.05, consecutive_failures=0,
        dominant_step="", last_success_ago="3 days ago",
        detail="", fix="", failure_probability=0.05, hold_threshold=3,
        is_persistent_infra=False,
    )
    code = CodeSignal(
        level="MEDIUM", score=0.50,
        detail="DAO/LTS migration changes core data-access layer",
        findings=["[MEDIUM] DAO/LTS migration changes core data-access layer (48% likely)"],
        is_submodule_only=False, has_real_code=True,
        code_caused_perf=True,
    )
    hist = HistoricalSignal(
        score=0.36, match_count=4, dominant_step="build",
        fail_rate=0.3, detail="", examples=[],
    )
    llm = compute_llm_signal({
        "llm_step_risks": [
            {"step": "build", "level": "High",
             "rationale": "core/pom.xml test dependency and test suite rewritten"},
        ],
        "llm_most_likely_step": "build",
    }, env=env)

    rec, outcome, conf, _, driver = make_decision(env, code, hist, llm=llm)
    assert rec == "CAUTION"
    assert "loadTest" in outcome
    assert "build" not in outcome.split("FAIL at ")[-1]
    assert driver == "code"
    assert conf <= 0.55


def test_llm_only_build_penalized_when_code_low_and_env_ready():
    env = EnvSignal(
        status="READY", score=0.05, consecutive_failures=0,
        dominant_step="", last_success_ago="3 days ago",
        detail="", fix="", failure_probability=0.05, hold_threshold=3,
        is_persistent_infra=False,
    )
    code = CodeSignal(
        level="LOW", score=0.25,
        detail="Java, pom.xml changes — no definite failure pattern detected",
        findings=[], is_submodule_only=False, has_real_code=True,
    )
    hist = HistoricalSignal(score=0.0, match_count=0, dominant_step="", fail_rate=0.0, detail="", examples=[])
    llm = compute_llm_signal({
        "llm_step_risks": [
            {"step": "build", "level": "High",
             "rationale": "pom.xml test dependency drift"},
        ],
        "llm_most_likely_step": "build",
    }, env=env)

    from analysis.risk_scorer import score_risk
    decision = score_risk(
        bundle_dict={
            "llm_step_risks": [
                {"step": "build", "level": "High", "rationale": "pom.xml test dependency drift"},
            ],
            "llm_most_likely_step": "build",
        },
        diff_text="diff --git a/core/pom.xml b/core/pom.xml\n+mockito",
        changed_files=["core/pom.xml"],
        commit_title="routine config tweak",
    )
    assert decision.primary_driver == "llm"
    assert decision.confidence <= 0.55
    assert decision.recommendation == "CAUTION"


def test_merge_commit_test_gap_does_not_hold_on_healthy_env():
    """Release merge with service changes but no tests in same diff → GO, not HOLD."""
    title = "Merge branch 'develop' into release-20260624-v-1.0.104"
    diff = """diff --git a/core/src/main/java/com/mh/core/services/flight/impl/AppFlightSearchServiceImpl.java b/core/src/main/java/com/mh/core/services/flight/impl/AppFlightSearchServiceImpl.java
--- a/core/src/main/java/com/mh/core/services/flight/impl/AppFlightSearchServiceImpl.java
+++ b/core/src/main/java/com/mh/core/services/flight/impl/AppFlightSearchServiceImpl.java
@@ -10,6 +10,7 @@ public class AppFlightSearchServiceImpl {
     public void search() {
+        assembleFacts();
     }
"""
    findings = _check_service_without_test_update(diff, title)
    assert findings
    assert findings[0].severity == "MEDIUM"
    assert findings[0].confidence == 48

    env = EnvSignal(
        status="READY", score=0.05, consecutive_failures=0,
        dominant_step="", last_success_ago="3 days ago",
        detail="", fix="", failure_probability=0.05, hold_threshold=3,
        is_persistent_infra=False,
    )
    code = CodeSignal(
        level="LOW", score=0.48,
        detail="AppFlightSearchServiceImpl changed — no unit test updated in same commit",
        findings=["[MEDIUM] AppFlightSearchServiceImpl changed — no unit test updated in same commit (48% likely)"],
        is_submodule_only=False, has_real_code=True,
        is_merge_commit=True,
    )
    hist = HistoricalSignal(score=0.39, match_count=6, dominant_step="build", fail_rate=0.3, detail="", examples=[])
    rec, outcome, conf, _, driver = make_decision(env, code, hist, llm=LlmSignal(
        score=0, dominant_step="", commit_risk_step="", dominant_level="LOW",
        step_scores={}, detail="", has_analysis=False,
    ))
    assert rec == "GO"
    assert driver == "code"
    assert "PASS" in outcome
    assert conf >= 0.45


def test_non_merge_test_gap_is_caution_not_hold():
    """Single-commit service change without test update → CAUTION, not HOLD."""
    env = EnvSignal(
        status="READY", score=0.05, consecutive_failures=0,
        dominant_step="", last_success_ago="3 days ago",
        detail="", fix="", failure_probability=0.05, hold_threshold=3,
        is_persistent_infra=False,
    )
    code = CodeSignal(
        level="HIGH", score=0.75,
        detail="ValidationService changed",
        findings=["[HIGH] ValidationService changed — no unit test updated in same commit (75% certain)"],
        is_submodule_only=False, has_real_code=True,
        is_merge_commit=False,
    )
    hist = HistoricalSignal(score=0.0, match_count=0, dominant_step="", fail_rate=0.0, detail="", examples=[])
    rec, outcome, conf, _, driver = make_decision(env, code, hist, llm=LlmSignal(
        score=0, dominant_step="", commit_risk_step="", dominant_level="LOW",
        step_scores={}, detail="", has_analysis=False,
    ))
    assert rec == "CAUTION"
    assert conf <= 0.58
    assert driver == "code"


# ── code_recommendation() tests ───────────────────────────────────────────────

def _make_code(level, score, findings=None, is_sub=False, has_real=True):
    return CodeSignal(
        level=level, score=score, detail="test",
        findings=findings or [],
        is_submodule_only=is_sub, has_real_code=has_real,
    )


def test_code_recommendation_low_returns_go():
    rec, conf, basis = code_recommendation(_make_code("LOW", 0.20, is_sub=True))
    assert rec == "GO"
    assert conf > 0.0
    assert "submodule" in basis.lower() or "structural" in basis.lower()


def test_code_recommendation_medium_returns_caution():
    rec, conf, basis = code_recommendation(
        _make_code("MEDIUM", 0.55, findings=["[MEDIUM] OSGi missing (70% certain)"])
    )
    assert rec == "CAUTION"
    assert conf > 0.0
    assert "structural" in basis.lower()


def test_code_recommendation_high_returns_hold():
    rec, conf, basis = code_recommendation(
        _make_code("HIGH", 0.85, findings=["[HIGH] Missing @Reference (85% certain)"])
    )
    assert rec == "HOLD"
    assert conf >= 0.50


def test_code_recommendation_certain_returns_hold():
    rec, conf, basis = code_recommendation(_make_code("CERTAIN", 0.92))
    assert rec == "HOLD"


def test_code_recommendation_independent_of_env():
    """GO verdict must not be affected by env state — code_recommendation is code-only."""
    low_code = _make_code("LOW", 0.18, is_sub=True)
    rec, conf, basis = code_recommendation(low_code)
    # Even with 6 consecutive env failures, code_recommendation returns GO for LOW code
    assert rec == "GO", "code_recommendation must return GO for LOW code regardless of env"


def test_code_recommendation_confidence_from_finding():
    """Confidence should be derived from finding percentage when available."""
    code = _make_code("HIGH", 0.80, findings=["[HIGH] Missing service (90% certain)"])
    rec, conf, basis = code_recommendation(code)
    assert rec == "HOLD"
    # 90% finding confidence * 0.85 discount = 0.765
    assert conf > 0.60


# ── _check_uncaught_sling_exceptions tests ────────────────────────────────────

from analysis.build_predictor import _check_uncaught_sling_exceptions


# Minimal Java source with the exact IDFC error pattern
_SERVLET_WITH_BUG = """\
package com.idfcfirstbanklimited.core.servlets;

import org.apache.sling.api.resource.ResourceResolverFactory;
import org.apache.sling.api.resource.ResourceResolver;
import org.osgi.service.component.annotations.Reference;

public class GetOfferPageDataServlet {
    @Reference
    private ResourceResolverFactory resolverFactory;

    public void doGet() {
        ResourceResolver resolver = resolverFactory.getServiceResourceResolver(null);
        resolver.close();
    }
}
"""

# Same but correctly wrapped in try-catch
_SERVLET_WITH_FIX = """\
package com.idfcfirstbanklimited.core.servlets;

import org.apache.sling.api.resource.LoginException;
import org.apache.sling.api.resource.ResourceResolverFactory;
import org.apache.sling.api.resource.ResourceResolver;

public class GetOfferPageDataServlet {
    private ResourceResolverFactory resolverFactory;

    public void doGet() {
        try {
            ResourceResolver resolver = resolverFactory.getServiceResourceResolver(null);
            resolver.close();
        } catch (LoginException e) {
            // handled
        }
    }
}
"""

# Same but declares throws
_SERVLET_WITH_THROWS = """\
public class GetOfferPageDataServlet {
    private ResourceResolverFactory resolverFactory;

    public void doGet() throws LoginException {
        ResourceResolver resolver = resolverFactory.getServiceResourceResolver(null);
    }
}
"""


def _make_diff(filepath: str, source: str) -> tuple:
    """Build a minimal git diff for a Java file."""
    diff = f"diff --git a/{filepath} b/{filepath}\n--- a/{filepath}\n+++ b/{filepath}\n"
    diff += "@@ -0,0 +1," + str(len(source.splitlines())) + " @@\n"
    diff += "\n".join("+" + l for l in source.splitlines()) + "\n"
    return diff, [filepath]


def test_uncaught_login_exception_detected():
    """Exact IDFC error: getServiceResourceResolver without try-catch.
    Without repo_dir (no javalang confirmation), emits LOW advisory — not MEDIUM/HIGH.
    This prevents false positives when try-catch is outside the scan window."""
    diff, files = _make_diff("core/src/main/java/GetOfferPageDataServlet.java", _SERVLET_WITH_BUG)
    findings = _check_uncaught_sling_exceptions(diff, files, repo_dir="")
    assert findings, "Should detect uncaught LoginException"
    assert findings[0].check == "uncaught_checked_exception"
    assert "LoginException" in findings[0].title
    # Regex-only → LOW advisory (not MEDIUM/HIGH — too many false positives otherwise)
    assert findings[0].severity == "LOW"
    assert findings[0].confidence == 45
    assert findings[0].step == "build"


def test_caught_login_exception_not_flagged():
    """Call wrapped in try-catch should not be flagged."""
    diff, files = _make_diff("core/src/main/java/GetOfferPageDataServlet.java", _SERVLET_WITH_FIX)
    findings = _check_uncaught_sling_exceptions(diff, files, repo_dir="")
    assert not findings, "Should NOT flag when exception is caught"


def test_throws_declaration_not_flagged():
    """Method that declares throws LoginException should not be flagged."""
    diff, files = _make_diff("core/src/main/java/GetOfferPageDataServlet.java", _SERVLET_WITH_THROWS)
    findings = _check_uncaught_sling_exceptions(diff, files, repo_dir="")
    assert not findings, "Should NOT flag when method declares throws"


def test_non_java_file_skipped():
    """Non-Java files should produce no findings."""
    findings = _check_uncaught_sling_exceptions(
        "+getServiceResourceResolver(null);\n", ["pom.xml"], repo_dir=""
    )
    assert not findings, "Should skip non-Java files"


def test_javalang_confirms_with_full_source(tmp_path):
    """Pass 2: when repo_dir has the file, javalang should confirm HIGH confidence."""
    filepath = "core/src/main/java/GetOfferPageDataServlet.java"
    full_path = tmp_path / "core" / "src" / "main" / "java"
    full_path.mkdir(parents=True)
    (full_path / "GetOfferPageDataServlet.java").write_text(_SERVLET_WITH_BUG)

    diff, files = _make_diff(filepath, _SERVLET_WITH_BUG)
    findings = _check_uncaught_sling_exceptions(diff, files, repo_dir=str(tmp_path))

    assert findings, "Should detect uncaught LoginException with javalang"
    # With javalang AST confirmation: HIGH severity, 85% confidence
    assert findings[0].severity == "HIGH"
    assert findings[0].confidence == 85
