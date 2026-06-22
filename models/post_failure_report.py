"""Post-failure risk assessment output models (LogSage Stage 2)."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from models.risk_report import BlastRadiusAnalysis

RiskLevel = Literal["Critical", "High", "Medium", "Low"]
RetryRecommendation = Literal["RETRY_SAFE", "RETRY_WITH_FIX", "DO_NOT_RETRY", "INVESTIGATE"]
FixEffort = Literal["Low", "Medium", "High"]


class LogSageRCAReport(BaseModel):
    """Stage 1 intermediate RCA output."""
    execution_id: str = ""
    failed_step: str = ""
    error_type: str = "unknown"
    error_summary: str = ""
    root_cause: str = ""
    affected_step: str = ""
    error_line_refs: List[str] = Field(default_factory=list)
    cascading_failures: List[str] = Field(default_factory=list)


class RetrievedIncident(BaseModel):
    execution_id: str = ""
    step: str = ""
    error_type: str = ""
    root_cause: str = ""
    fix: str = ""
    similarity_score: float = 0.0
    rerank_score: Optional[float] = None
    route: str = ""


class PostFailureRiskReport(BaseModel):
    execution_id: str
    failed_step: str
    pipeline: str = ""
    commit_sha: Optional[str] = None
    root_cause_summary: str = ""
    error_type: str = "unknown"
    filtered_log_blocks: List[str] = Field(default_factory=list)
    pruning_stats: dict = Field(default_factory=dict)
    risk_level: RiskLevel = "Medium"
    retry_recommendation: RetryRecommendation = "INVESTIGATE"
    retry_confidence: int = Field(default=50, ge=0, le=100)
    blast_radius_analysis: Optional[BlastRadiusAnalysis] = None
    business_impact: str = ""
    similar_incidents: List[RetrievedIncident] = Field(default_factory=list)
    recommended_actions: List[str] = Field(default_factory=list)
    fix_steps: List[str] = Field(default_factory=list)
    counterevidence: List[str] = Field(default_factory=list)
    estimated_fix_effort: FixEffort = "Medium"
    narrative: str = ""
