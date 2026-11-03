"""Contracts used by the analysis agent and its tools."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from finance_agent.reports.models import OnePager


class AnalysisModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ApprovedSource(AnalysisModel):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    name: str = Field(min_length=1)
    local_path: Path
    content_date: date | None = None
    flags: list[str] = Field(default_factory=list)
    """Human-readable triage warnings, e.g. "superseded: replaced by the May data pack"."""


class AnalysisRequest(AnalysisModel):
    report_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    company_name: str = Field(min_length=1)
    source_snapshot_id: str = Field(min_length=1)
    user_context: str | None = None
    sources: list[ApprovedSource] = Field(min_length=1)


class ExecutionArtifact(AnalysisModel):
    id: str = Field(min_length=1)
    code: str = Field(min_length=1)
    stdout: str
    stderr: str
    exit_code: int
    outputs: dict[str, JsonValue] = Field(default_factory=dict)


class ReviewIssue(AnalysisModel):
    blocking: bool
    """True only for a material misstatement a reader would be misled by."""
    problem: str = Field(min_length=1)


class FindingReview(AnalysisModel):
    """A reviewer's verdict. Only blocking issues reject; minor ones are returned as advice."""

    issues: list[ReviewIssue] = Field(default_factory=list)
    accepted: bool = True
    reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def derive_verdict(self) -> "FindingReview":
        if self.issues:
            self.accepted = not any(issue.blocking for issue in self.issues)
            self.reasons = [
                ("Blocking: " if issue.blocking else "Minor (not required): ") + issue.problem
                for issue in sorted(self.issues, key=lambda issue: not issue.blocking)
            ]
        return self


class ReportReview(AnalysisModel):
    accepted: bool
    reasons: list[str] = Field(default_factory=list)


class FindingCreationResult(AnalysisModel):
    accepted: bool
    finding_id: str
    reasons: list[str] = Field(default_factory=list)


class ReportSectionPlan(AnalysisModel):
    key: str = Field(pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
    title: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=1, max_length=500)
    questions: list[str] = Field(default_factory=list, max_length=6)
    """Explicit questions from the analyst's request that this section must answer."""


class OutlineCreationResult(AnalysisModel):
    accepted: bool
    section_ids: list[str] = Field(default_factory=list)
    question_ids: list[str] = Field(default_factory=list)
    """'q1: question text' for each brief question; put the bare ID (q1) in a finding's answers."""
    reasons: list[str] = Field(default_factory=list)


class FindingReviewRecord(AnalysisModel):
    section_id: str
    finding_id: str
    accepted: bool
    reasons: list[str] = Field(default_factory=list)
    decision_type: str
    attempt: int = 1
    reviewed_at: datetime
    proposed_finding: dict[str, JsonValue]


class AnalysisRunRecord(AnalysisModel):
    report: OnePager
    artifacts: list[ExecutionArtifact] = Field(default_factory=list)
    reviews: list[FindingReviewRecord] = Field(default_factory=list)


class ReportJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RunEventKind(StrEnum):
    JOB = "job"
    TOOL = "tool"
    SUBAGENT = "subagent"
    REVIEW = "review"
    CALCULATION = "calculation"


class RunEventStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    WARNING = "warning"
    FAILED = "failed"


class RunEvent(AnalysisModel):
    id: str
    kind: RunEventKind
    status: RunEventStatus
    label: str
    detail: str | None = None
    occurred_at: datetime
    tool_name: str | None = None
    agent_name: str | None = None


class ReportJob(AnalysisModel):
    id: str
    intake_id: str
    status: ReportJobStatus
    created_at: datetime
    updated_at: datetime
    report_id: str | None = None
    error: str | None = None
    events: list[RunEvent] = Field(default_factory=list)


class BottomLineResult(AnalysisModel):
    accepted: bool
    reasons: list[str] = Field(default_factory=list)


class FinalizationResult(AnalysisModel):
    accepted: bool
    reasons: list[str] = Field(default_factory=list)

