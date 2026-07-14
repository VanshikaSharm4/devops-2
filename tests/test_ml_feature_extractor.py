"""Tests for ML feature extraction — factual signals only."""

from analysis.ml_feature_extractor import extract_ml_features


def test_extract_minimal_diff_features():
    diff = """diff --git a/core/pom.xml b/core/pom.xml
--- a/core/pom.xml
+++ b/core/pom.xml
@@ -1,3 +1,6 @@
+<dependency>
+  <version>1.0.0-SNAPSHOT</version>
+</dependency>
"""
    features = extract_ml_features(
        commit_sha="abc123def456",
        diff_text=diff,
        changed_files=["core/pom.xml"],
        commit_title="Update core dependency",
        commit_author="dev@example.com",
    )
    assert features["changed_files_count"] == 1
    assert features["has_pom_change"] is True
    assert features["commit_title_length"] == len("Update core dependency")
    assert "predicted_risk" not in features
    assert "narrative" not in features


def test_subtree_import_low_signal():
    features = extract_ml_features(
        commit_sha="deadbeef",
        diff_text="",
        changed_files=["ui.apps/src/main/content/jcr_root/.content.xml"],
        commit_title="subtree import from vendor",
    )
    assert features["is_subtree_import"] is True
