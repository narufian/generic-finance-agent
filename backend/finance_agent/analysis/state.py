"""In-memory state for the local proof-of-concept analysis run."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable
from uuid import uuid4

from finance_agent.reports.models import (
    AnalysisIssue,
    BriefQuestion,
    Finding,
    OnePager,
    ReportSection,
    ReportStatus,
    SectionStatus,
)

from .grounding import SourceLibrary
from .models import (
    AnalysisRequest,
    ExecutionArtifact,
    FindingReviewRecord,
    RunEvent,
    RunEventKind,
    RunEventStatus,
    ReportSectionPlan,
)


def create_report_draft(request: AnalysisRequest) -> OnePager:
    return OnePager(
        id=request.report_id,
        company_id=request.company_id,
        company_name=request.company_name,
        source_snapshot_id=request.source_snapshot_id,
        approved_source_ids=[source.id for source in request.sources],
        generated_at=datetime.now(timezone.utc),
        status=ReportStatus.DRAFT,
        sections=[],
        issues=[],
    )


@dataclass
class AnalysisState:
    report: OnePager
    artifacts: dict[str, ExecutionArtifact] = field(default_factory=dict)
    reviews: list[FindingReviewRecord] = field(default_factory=list)
    event_sink: Callable[[RunEvent], None] | None = None
    sources: SourceLibrary = field(default_factory=lambda: SourceLibrary({}))
    """Full source text for deterministic grounding. Empty disables source checks (tests)."""
    source_notes: dict[str, str] = field(default_factory=dict)
    """Triage warnings per source ID, e.g. superseded or about a different company."""
    latest_content_date: date | None = None
    rejections: dict[str, int] = field(default_factory=dict)
    report_review_rejections: int = 0
    last_report_review_reasons: list[str] = field(default_factory=list)

    def emit_event(
        self,
        kind: RunEventKind,
        status: RunEventStatus,
        label: str,
        *,
        detail: str | None = None,
        tool_name: str | None = None,
        agent_name: str | None = None,
    ) -> RunEvent:
        event = RunEvent(
            id=f"event-{uuid4().hex}",
            kind=kind,
            status=status,
            label=label,
            detail=detail,
            occurred_at=datetime.now(timezone.utc),
            tool_name=tool_name,
            agent_name=agent_name,
        )
        if self.event_sink is not None:
            self.event_sink(event)
        return event

    def record_artifact(self, artifact: ExecutionArtifact) -> None:
        self.artifacts[artifact.id] = artifact

    def record_review(self, review: FindingReviewRecord) -> None:
        self.reviews.append(review)

    def add_issue(self, issue: AnalysisIssue) -> None:
        self.report.issues = [
            existing for existing in self.report.issues if existing.id != issue.id
        ] + [issue]

    def define_outline(self, plans: list[ReportSectionPlan]) -> list[str]:
        if not 1 <= len(plans) <= 8:
            raise ValueError("the report outline must contain between 1 and 8 sections")
        if any(section.findings for section in self.report.sections):
            raise ValueError("the report outline cannot change after findings are created")
        keys = [plan.key for plan in plans]
        if len(keys) != len(set(keys)):
            raise ValueError("report section keys must be unique")
        self.report.sections = [
            ReportSection(
                id=f"section-{plan.key.replace('_', '-')}",
                section_type=plan.key,
                title=plan.title,
                status=SectionStatus.PARTIAL,
                order=order,
                findings=[],
            )
            for order, plan in enumerate(plans)
        ]
        questions: list[BriefQuestion] = []
        for section, plan in zip(self.report.sections, plans):
            for text in plan.questions:
                questions.append(
                    BriefQuestion(id=f"q{len(questions) + 1}", text=text, section_id=section.id)
                )
        self.report.questions = questions
        return [section.id for section in self.report.sections]

    def upsert_finding(self, section_id: str, finding: Finding) -> None:
        section = next(
            (candidate for candidate in self.report.sections if candidate.id == section_id),
            None,
        )
        if section is None:
            raise ValueError(f"unknown section: {section_id}")
        section.findings = [
            existing for existing in section.findings if existing.id != finding.id
        ]
        section.findings.append(finding)

