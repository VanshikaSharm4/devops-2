"""Wire devops-risk-ml predictions into Risk Assessment."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from connectors.cm_connector import get_commit_sha_from_execution
from connectors.ml_client import predict_risk
from models.risk_report import FeatureContribution, MLPrediction, RiskReport


def resolve_commit_from_dev_execution(
    dev_execution_id: str,
    program_id: Optional[str] = None,
    pipeline_id_dev: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> Optional[str]:
    program_id = program_id or os.getenv("PROGRAM_ID", "19905")
    pipeline_id_dev = pipeline_id_dev or os.getenv("PIPELINE_ID_DEV", "47202398")
    tenant_id = tenant_id or os.getenv("ML_TENANT_ID", "idfc")
    return get_commit_sha_from_execution(
        program_id, pipeline_id_dev, dev_execution_id, tenant_id=tenant_id
    )


def fetch_ml_prediction(
    commit_sha: str,
    dev_execution_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> Optional[MLPrediction]:
    raw = predict_risk(commit_sha, tenant_id=tenant_id, dev_execution_id=dev_execution_id)
    if not raw:
        return None
    top = [
        FeatureContribution(
            feature=f.get("feature", ""),
            value=float(f.get("value", 0)),
            shap=float(f.get("shap", 0)),
        )
        for f in raw.get("top_features", [])
    ]
    return MLPrediction(
        overall_risk_score=float(raw.get("overall_risk_score", 0)),
        step_probabilities=raw.get("step_probabilities", {}),
        risk_level=raw.get("risk_level", "Low"),
        promotion_recommendation=raw.get("promotion_recommendation", "HOLD"),
        top_features=top,
        model_version=raw.get("model_version", ""),
    )


def merge_ml_into_report(report: RiskReport, ml: MLPrediction) -> RiskReport:
    """Overlay ML score onto LLM report — ML drives level and recommendation."""
    data = report.model_dump()
    data["ml_prediction"] = ml.model_dump()
    data["risk_level"] = ml.risk_level
    data["confidence_score"] = int(round(ml.overall_risk_score * 100))
    if ml.promotion_recommendation == "NO_GO":
        data["recommended_actions"] = (
            [f"ML model recommends NO_GO (score {ml.overall_risk_score:.0%})"]
            + list(data.get("recommended_actions") or [])[:4]
        )
    return RiskReport.model_validate(data)


def run_assessment_with_ml(
    commit_sha: str,
    dev_execution_id: Optional[str] = None,
    use_llm: bool = True,
    bundle=None,
) -> Tuple[Any, Optional[RiskReport], str, Optional[MLPrediction]]:
    from analysis.risk_analyzer import run_pre_deploy_risk

    ml = fetch_ml_prediction(commit_sha, dev_execution_id=dev_execution_id)
    _, report, md = run_pre_deploy_risk(
        commit_sha=commit_sha,
        fetch_logs=False,
        use_llm=use_llm,
        bundle=bundle,
    )
    if ml and report:
        report = merge_ml_into_report(report, ml)
        md = md + f"\n\n## ML Risk Score\n\n- Overall: **{ml.overall_risk_score:.1%}**\n- Recommendation: **{ml.promotion_recommendation}**\n"
    return bundle, report, md, ml
