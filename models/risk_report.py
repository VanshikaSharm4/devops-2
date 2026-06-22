from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


RiskLevel = Literal["Critical", "High", "Medium", "Low"]


class TechnicalFailureHypothesis(BaseModel):
    failure_type: str          # e.g. "osgi_activation_failure", "classpath_conflict", "dependency_injection_failure",
                               # "api_contract_mismatch", "serialization_failure", "auth_regression",
                               # "deployment_ordering_issue", "cache_invalidation", "resource_resolver_leak",
                               # "config_propagation_issue", "integration_timeout", "schema_mismatch"
    trigger_mechanism: str     # what specific change introduced this — cite the file/dep
    runtime_impact: str        # what fails at runtime and how
    deployment_stage: str      # "build" | "deploy" | "securityTest" | "activation" | "codeQuality"
    likelihood: Literal["High", "Medium", "Low"]
    confidence: int = Field(ge=0, le=100)
    supporting_evidence: List[str] = Field(default_factory=list)
    counterevidence: List[str] = Field(default_factory=list)
    verification_steps: List[str] = Field(default_factory=list)


class BlastRadiusAnalysis(BaseModel):
    affected_modules: List[str] = Field(default_factory=list)
    downstream_consumers: List[str] = Field(default_factory=list)
    deployment_scope: Literal["isolated", "service-wide", "platform-wide"] = "isolated"
    rollback_complexity: Literal["Low", "Medium", "High"] = "Low"
    user_facing_impact: str = ""


class RiskDriver(BaseModel):
    driver: str                    # what drives the risk
    signal_strength: Literal["HIGH", "MEDIUM", "LOW"]
    evidence_type: str             # "code_analysis" | "semantic_match" | "anti_pattern" | "dependency_change"
    detail: str                    # specific detail
    related_file: Optional[str] = None


class FailureMode(BaseModel):
    mode: str                      # "dependency_mismatch" | "integration_regression" | "config_syntax_error" etc
    likelihood: Literal["High", "Medium", "Low"]
    explanation: str               # causal explanation, not frequency


class EvidenceItem(BaseModel):
    source: str                    # "commit_analysis" | "semantic_retrieval" | "anti_pattern" | "rule_engine"
    detail: str
    signal_weight: float = 1.0    # 0-1, how much this evidence matters
    execution_id: Optional[str] = None


# Backward-compatible alias (models/__init__.py and dashboard import Evidence)
Evidence = EvidenceItem


class StepRisk(BaseModel):
    step: str
    level: RiskLevel
    rationale: str = ""
    evidence: List[EvidenceItem] = Field(default_factory=list)
    # Kept for backward compatibility with dashboard reads sr["historical_failure_count"]
    historical_failure_count: Optional[int] = None


class FeatureContribution(BaseModel):
    feature: str
    value: float = 0.0
    shap: float = 0.0


class MLPrediction(BaseModel):
    overall_risk_score: float = Field(ge=0, le=1)
    step_probabilities: Dict[str, float] = Field(default_factory=dict)
    risk_level: str = "Low"
    promotion_recommendation: str = "HOLD"  # GO | HOLD | NO_GO
    top_features: List[FeatureContribution] = Field(default_factory=list)
    model_version: str = ""


class RiskReport(BaseModel):
    risk_level: RiskLevel
    confidence_score: int = Field(ge=0, le=100)   # 0-100
    commit_sha: Optional[str] = None
    modules_at_risk: List[str] = Field(default_factory=list)
    most_likely_failure_step: str
    primary_risk_drivers: List[RiskDriver] = Field(default_factory=list)
    step_risks: List[StepRisk] = Field(default_factory=list)
    likely_failure_modes: List[FailureMode] = Field(default_factory=list)
    evidence_used: List[EvidenceItem] = Field(default_factory=list)
    counterevidence: List[str] = Field(default_factory=list)   # why prediction might be wrong
    recommended_actions: List[str] = Field(default_factory=list)
    estimated_duration_min: Optional[int] = None
    narrative: str = ""
    reasoning: str = ""
    change_intent: str = ""        # "feature_addition"|"refactor"|"dependency_upgrade"|"hotfix"|"config_change"|"migration"|"security_patch"|"unknown"
    technical_failure_hypotheses: List[TechnicalFailureHypothesis] = Field(default_factory=list)
    blast_radius_analysis: Optional[BlastRadiusAnalysis] = None
    risk_contributions: Dict[str, float] = Field(default_factory=dict)
    ml_prediction: Optional[MLPrediction] = None
    # risk_contributions keys: dependency_risk, config_risk, security_risk, blast_radius_risk,
    #                          regression_similarity, deployment_complexity, infra_instability
    # values: 0.0–1.0
