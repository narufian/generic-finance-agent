"""Data contracts for ZIP intake and source approval."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class IngestionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IntakeStatus(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"


class Relevance(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Sensitivity(StrEnum):
    PUBLIC = "Public"
    CONFIDENTIAL = "Confidential"
    RESTRICTED = "Restricted"
    MNPI = "MNPI"


class ReportAgentModel(StrEnum):
    SONNET = "sonnet"
    OPUS = "opus"


class ExclusionBasis(StrEnum):
    IRRELEVANT = "irrelevant"
    DUPLICATE = "duplicate"
    SENSITIVITY = "sensitivity"
    DIFFERENT_ENTITY = "different_entity"


class SourceFlagKind(StrEnum):
    DIFFERENT_ENTITY = "different_entity"
    """The document is about a company other than the subject (e.g. another deal)."""
    SUPERSEDED = "superseded"
    """The document says, or another source shows, that a newer version replaces it."""


class SourceFlag(IngestionModel):
    kind: SourceFlagKind
    note: str = Field(min_length=1, max_length=200)


class SourceFile(IngestionModel):
    id: str
    name: str
    relative_path: str
    file_type: str
    size_bytes: int = Field(ge=0)
    document_date: datetime | None = None
    relevance: Relevance
    relevance_reason: str
    sensitivity: str
    readable: bool
    included: bool
    exclusion_reason: str | None = None
    content_date: date | None = None
    """The date the document's information is as of, read from its content (not the file time)."""
    flags: list[SourceFlag] = Field(default_factory=list)


class IntakeSession(IngestionModel):
    id: str
    company_name: str = Field(min_length=1)
    user_context: str | None = None
    archive_name: str
    archive_size_bytes: int = Field(ge=0)
    created_at: datetime
    status: IntakeStatus
    sources: list[SourceFile]
    approved_source_ids: list[str] = Field(default_factory=list)
    approved_at: datetime | None = None
    source_snapshot_id: str | None = None
    use_fast_finding_review: bool = False
    report_agent_model: ReportAgentModel = ReportAgentModel.SONNET


class ProfileSummary(IngestionModel):
    intake_id: str
    company_name: str
    user_context: str | None = None
    archive_name: str
    created_at: datetime
    updated_at: datetime
    status: IntakeStatus
    source_count: int
    approved_source_count: int
    latest_report_id: str | None = None
    latest_report_generated_at: datetime | None = None
    report_status: str | None = None
    active_job_id: str | None = None
    job_status: str | None = None


class ApprovalRequest(IngestionModel):
    source_ids: list[str] = Field(min_length=1)
    use_fast_finding_review: bool | None = None
    report_agent_model: ReportAgentModel | None = None


class SourceClassification(IngestionModel):
    source_id: str
    relevance: Relevance
    sensitivity: Sensitivity
    recommended_include: bool
    exclusion_basis: ExclusionBasis | None = None
    rationale: str = Field(min_length=1, max_length=300)
    content_date: date | None = None
    flags: list[SourceFlag] = Field(default_factory=list)


class ClassificationBatch(IngestionModel):
    files: list[SourceClassification]
