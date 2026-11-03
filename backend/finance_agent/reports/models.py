"""Validated domain contract for evidence-backed company one-pagers."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DomainModel(BaseModel):
    """Base configuration shared by serialized domain models."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FindingStatus(StrEnum):
    REPORTED = "reported"
    CALCULATED = "calculated"
    ESTIMATED = "estimated"
    ASSESSED = "assessed"
    UNAVAILABLE = "unavailable"


class EvidenceLevel(StrEnum):
    STRONG = "strong"
    MODERATE = "moderate"
    LIMITED = "limited"


class EvidenceDirectness(StrEnum):
    DIRECT = "direct"
    DERIVED = "derived"
    INFERRED = "inferred"


class SectionType(StrEnum):
    BUSINESS_MODEL = "business_model"
    REVENUE = "revenue"
    CAPITALIZATION_AND_LEVERAGE = "capitalization_and_leverage"
    VALUATION = "valuation"
    AI_NATIVE_ASSESSMENT = "ai_native_assessment"


class SectionStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class ReportStatus(StrEnum):
    DRAFT = "draft"
    VERIFIED = "verified"
    PARTIAL = "partial"
    FAILED = "failed"


class IssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class IssueScope(StrEnum):
    REPORT = "report"
    SOURCE = "source"
    SECTION = "section"
    FINDING = "finding"


class SourceLocation(DomainModel):
    page: int | None = Field(default=None, ge=1)
    section: str | None = None
    sheet: str | None = None
    cell_range: str | None = None

    @model_validator(mode="after")
    def require_precise_location(self) -> Self:
        if not any((self.page, self.section, self.sheet, self.cell_range)):
            raise ValueError("a citation must include at least one precise source location")
        return self


class Citation(DomainModel):
    id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_name: str = Field(min_length=1)
    location: SourceLocation
    excerpt: str | None = None


class Assumption(DomainModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)


class Conflict(DomainModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    citation_ids: list[str] = Field(min_length=2)


class EvidenceStrength(DomainModel):
    level: EvidenceLevel
    directness: EvidenceDirectness
    source_count: int = Field(ge=0)
    has_conflicts: bool
    latest_source_date: date | None = None
    reasons: list[str] = Field(min_length=1)


class CalculationInput(DomainModel):
    name: str = Field(min_length=1)
    value: Decimal
    unit: str = Field(min_length=1)
    citation_ids: list[str] = Field(min_length=1)


class Calculation(DomainModel):
    artifact_id: str = Field(min_length=1)
    output_name: str = Field(min_length=1)
    expression: str = Field(min_length=1)
    inputs: list[CalculationInput] = Field(min_length=1)
    result: Decimal


class FindingBase(DomainModel):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    status: FindingStatus
    evidence_strength: EvidenceStrength
    citations: list[Citation] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    methodology: str | None = None
    conflicts: list[Conflict] = Field(default_factory=list)
    answers: list[str] = Field(default_factory=list)
    """IDs of the brief questions this finding helps answer."""

    @model_validator(mode="after")
    def validate_evidence_contract(self) -> Self:
        if self.status != FindingStatus.UNAVAILABLE and not self.citations:
            raise ValueError("available findings must contain at least one citation")
        if self.status == FindingStatus.ESTIMATED and not self.assumptions:
            raise ValueError("estimated findings must contain at least one assumption")
        if self.status in {FindingStatus.CALCULATED, FindingStatus.ASSESSED} and not self.methodology:
            raise ValueError(f"{self.status.value} findings must describe their methodology")

        distinct_sources = {citation.source_id for citation in self.citations}
        if self.evidence_strength.source_count != len(distinct_sources):
            raise ValueError("evidence source_count must match distinct cited sources")
        if self.evidence_strength.has_conflicts != bool(self.conflicts):
            raise ValueError("evidence conflict flag must match the finding conflicts")

        citation_ids = {citation.id for citation in self.citations}
        referenced_ids = {
            citation_id
            for conflict in self.conflicts
            for citation_id in conflict.citation_ids
        }
        if not referenced_ids.issubset(citation_ids):
            raise ValueError("conflicts may reference only citations on the finding")
        return self


class TextFinding(FindingBase):
    type: Literal["text"] = "text"
    value: str | None

    @model_validator(mode="after")
    def validate_value(self) -> Self:
        if self.status == FindingStatus.UNAVAILABLE and self.value is not None:
            raise ValueError("unavailable findings must not contain a value")
        if self.status != FindingStatus.UNAVAILABLE and not self.value:
            raise ValueError("available text findings must contain a value")
        return self


class NumericFinding(FindingBase):
    type: Literal["number"] = "number"
    value: Decimal | None
    display_value: str | None
    unit: str | None
    period: str | None = None
    calculation: Calculation | None = None

    @model_validator(mode="after")
    def validate_value_and_calculation(self) -> Self:
        if self.status == FindingStatus.UNAVAILABLE:
            if any((self.value is not None, self.display_value is not None, self.unit is not None)):
                raise ValueError("unavailable numeric findings must not contain value fields")
            return self

        if self.value is None or not self.display_value or not self.unit:
            raise ValueError("available numeric findings require value, display_value, and unit")
        if self.status == FindingStatus.CALCULATED:
            if self.calculation is None:
                raise ValueError("calculated numeric findings require a calculation")
            if self.calculation.result != self.value:
                raise ValueError("calculation result must equal the finding value")
        return self


class CategoricalFinding(FindingBase):
    type: Literal["categorical"] = "categorical"
    value: str | None
    options: list[str] = Field(min_length=2)
    rationale: str | None = Field(default=None, max_length=400)
    """One-line reason for the chosen option, shown next to the verdict."""

    @model_validator(mode="after")
    def validate_category(self) -> Self:
        if len(set(self.options)) != len(self.options):
            raise ValueError("categorical options must be unique")
        if self.status == FindingStatus.UNAVAILABLE:
            if self.value is not None:
                raise ValueError("unavailable categorical findings must not contain a value")
            return self
        if self.value not in self.options:
            raise ValueError("categorical value must belong to its declared options")
        return self


Finding = Annotated[
    TextFinding | NumericFinding | CategoricalFinding,
    Field(discriminator="type"),
]


class ReportSection(DomainModel):
    id: str = Field(min_length=1)
    section_type: str = Field(pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
    title: str = Field(min_length=1)
    summary: str | None = None
    status: SectionStatus
    order: int = Field(ge=0)
    findings: list[Finding] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_findings(self) -> Self:
        finding_ids = [finding.id for finding in self.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("finding IDs must be unique within a section")
        if self.status == SectionStatus.COMPLETE and not self.findings:
            raise ValueError("complete sections must contain at least one finding")
        return self


class AnalysisIssue(DomainModel):
    id: str = Field(min_length=1)
    severity: IssueSeverity
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    message: str = Field(min_length=1)
    scope_type: IssueScope
    scope_id: str | None = None
    recoverable: bool
    user_action: str | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.scope_type == IssueScope.REPORT and self.scope_id is not None:
            raise ValueError("report-scoped issues must not contain scope_id")
        if self.scope_type != IssueScope.REPORT and not self.scope_id:
            raise ValueError("non-report issues require scope_id")
        return self


class BriefQuestion(DomainModel):
    """An explicit question from the analyst's request that the report must answer."""

    id: str = Field(pattern=r"^q[0-9]+$")
    text: str = Field(min_length=1, max_length=300)
    section_id: str = Field(min_length=1)


class BottomLine(DomainModel):
    """The conclusion a reader needs if they read nothing else."""

    summary: str = Field(min_length=1, max_length=700)
    recommendation: str = Field(min_length=1, max_length=400)
    finding_ids: list[str] = Field(min_length=1)


class OnePager(DomainModel):
    id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    company_name: str = Field(min_length=1)
    source_snapshot_id: str = Field(min_length=1)
    approved_source_ids: list[str] = Field(min_length=1)
    generated_at: datetime
    status: ReportStatus
    sections: list[ReportSection] = Field(default_factory=list)
    issues: list[AnalysisIssue] = Field(default_factory=list)
    questions: list[BriefQuestion] = Field(default_factory=list)
    bottom_line: BottomLine | None = None

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at must include a timezone")
        if len(self.approved_source_ids) != len(set(self.approved_source_ids)):
            raise ValueError("approved source IDs must be unique")
        if self.status != ReportStatus.DRAFT and not self.sections:
            raise ValueError("completed reports must contain at least one section")

        section_ids = [section.id for section in self.sections]
        section_orders = [section.order for section in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("section IDs must be unique")
        if len(section_orders) != len(set(section_orders)):
            raise ValueError("section order values must be unique")

        finding_ids = [
            finding.id
            for section in self.sections
            for finding in section.findings
        ]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("finding IDs must be unique across the report")

        approved_sources = set(self.approved_source_ids)
        cited_sources = {
            citation.source_id
            for section in self.sections
            for finding in section.findings
            for citation in finding.citations
        }
        if not cited_sources.issubset(approved_sources):
            raise ValueError("findings may cite only approved sources")

        question_ids = [question.id for question in self.questions]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("question IDs must be unique")
        if not {question.section_id for question in self.questions}.issubset(section_ids):
            raise ValueError("questions must belong to a report section")
        answered = {
            question_id
            for section in self.sections
            for finding in section.findings
            for question_id in finding.answers
        }
        if not answered.issubset(question_ids):
            raise ValueError("findings may answer only questions defined on the report")
        if self.bottom_line and not set(self.bottom_line.finding_ids).issubset(finding_ids):
            raise ValueError("the bottom line may reference only findings in the report")
        return self

