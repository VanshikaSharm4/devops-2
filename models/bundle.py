from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ExecutionSummary(BaseModel):
    total_executions: int
    finished: int
    failed_or_error: int
    cancelled: int
    success_rate_pct: float


class ErrorDetail(BaseModel):
    execution_id: str
    failed_step: str
    pipeline: str = ""
    parsed_error: Dict[str, Any] = Field(default_factory=dict)


class StuckExecution(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    executionId: str
    pipelineName: str = ""
    duration_min: float = Field(alias="Duration (Min)", default=0)


class KnownRootCause(BaseModel):
    error_type: str
    detail: str
    affected_files: List[str] = Field(default_factory=list)
    failed_step: str = ""
    occurrence_count: int = 1


class FailureHistory(BaseModel):
    by_step: Dict[str, int] = Field(default_factory=dict)
    by_module: Dict[str, int] = Field(default_factory=dict)
    by_pipeline_step: List[Dict[str, Any]] = Field(default_factory=list)
    known_root_causes: List[KnownRootCause] = Field(default_factory=list)
    avg_success_duration_min: Optional[float] = None


class GitContext(BaseModel):
    pr_number: Optional[int] = None
    commit_sha: Optional[str] = None
    title: str = ""
    body: str = ""
    author: str = ""
    changed_files: List[str] = Field(default_factory=list)
    aem_modules_touched: List[str] = Field(default_factory=list)
    diff_excerpt: str = ""


class RuleScores(BaseModel):
    build: str = "LOW"
    securityTest: str = "LOW"
    deploy: str = "LOW"
    reasons: List[str] = Field(default_factory=list)


class AnalysisBundle(BaseModel):
    program_id: str = "19905"
    repo: str = ""
    window_days: int = 30
    execution_summary: ExecutionSummary
    failure_patterns: List[Dict[str, Any]] = Field(default_factory=list)
    error_details: List[ErrorDetail] = Field(default_factory=list)
    stuck_executions: List[Dict[str, Any]] = Field(default_factory=list)
    failure_history: FailureHistory = Field(default_factory=FailureHistory)
    git_context: Optional[GitContext] = None
    rule_scores: Optional[RuleScores] = None

    def to_findings_dict(self) -> dict:
        """Backward-compatible dict for legacy run_analysis()."""
        return {
            "summary": self.execution_summary.model_dump(),
            "patterns": self.failure_patterns,
            "error_details": [e.model_dump() for e in self.error_details],
            "stuck_executions": self.stuck_executions,
            "failure_history": self.failure_history.model_dump(),
            "git_context": self.git_context.model_dump() if self.git_context else None,
            "rule_scores": self.rule_scores.model_dump() if self.rule_scores else None,
        }
