"""End-to-end report generation from an approved intake snapshot."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable
from uuid import uuid4

from finance_agent.ingestion.models import IntakeSession, IntakeStatus, ReportAgentModel
from finance_agent.ingestion.service import extract_text
from finance_agent.reports.models import OnePager, ReportStatus

from pydantic_ai.usage import UsageLimits

from .agent import build_event_handler, build_live_agent
from .grounding import SourceLibrary
from .models import (
    AnalysisRequest,
    AnalysisRunRecord,
    ApprovedSource,
    RunEvent,
    RunEventKind,
    RunEventStatus,
)
from .state import AnalysisState, create_report_draft


class AnalysisRunError(RuntimeError):
    """Raised when the report agent does not deliver a valid report."""


class LiveAnalysisRunner:
    async def run(
        self,
        session: IntakeSession,
        approved_paths: dict[str, Path],
        *,
        event_sink: Callable[[RunEvent], None] | None = None,
    ) -> AnalysisRunRecord:
        if session.status != IntakeStatus.APPROVED or not session.source_snapshot_id:
            raise AnalysisRunError("the source set must be approved before analysis")

        source_by_id = {source.id: source for source in session.sources}
        request = AnalysisRequest(
            report_id=f"report-{uuid4().hex}",
            company_id=f"company-{session.id}",
            company_name=session.company_name,
            source_snapshot_id=session.source_snapshot_id,
            user_context=session.user_context,
            sources=[
                ApprovedSource(
                    id=source_id,
                    name=source_by_id[source_id].name,
                    local_path=path,
                    content_date=source_by_id[source_id].content_date,
                    flags=[
                        f"{flag.kind.value}: {flag.note}"
                        for flag in source_by_id[source_id].flags
                    ],
                )
                for source_id, path in approved_paths.items()
            ],
        )
        state = AnalysisState(
            report=create_report_draft(request),
            event_sink=event_sink,
            sources=await asyncio.to_thread(load_source_library, request.sources),
            source_notes={
                source.id: "; ".join(source.flags) for source in request.sources if source.flags
            },
            latest_content_date=max(
                (source.content_date for source in request.sources if source.content_date),
                default=None,
            ),
        )
        bundle, dependencies = build_live_agent(
            state,
            use_fast_reviewer=session.use_fast_finding_review,
            use_opus_report_agent=session.report_agent_model == ReportAgentModel.OPUS,
        )
        try:
            await dependencies.workspace.seed_sources(request.sources)
            state.emit_event(
                RunEventKind.JOB,
                RunEventStatus.COMPLETED,
                "Approved sources loaded",
                detail=f"{len(request.sources)} approved source(s) are available to the agent.",
            )
            result = await bundle.orchestrator.run(
                self._prompt(request),
                deps=dependencies,
                event_stream_handler=build_event_handler(state),
                # PydanticAI's default of 50 model requests is too low once findings are atomic
                # (one claim each) and rejected findings are revised.
                usage_limits=UsageLimits(request_limit=dependencies.request_limit),
            )
            if state.report.status != ReportStatus.VERIFIED:
                raise AnalysisRunError(
                    "the report agent stopped before successfully finalizing the report: "
                    + result.output
                )
            return AnalysisRunRecord(
                report=state.report,
                artifacts=list(state.artifacts.values()),
                reviews=state.reviews,
            )
        finally:
            await dependencies.workspace.close()

    @staticmethod
    def _prompt(request: AnalysisRequest) -> str:
        def describe(source: ApprovedSource) -> str:
            line = f"- {source.id}: /home/user/work/sources/{source.id}/{Path(source.name).name}"
            if source.content_date:
                line += f" (as of {source.content_date.isoformat()})"
            if source.flags:
                line += f" [TRIAGE WARNING: {'; '.join(source.flags)}]"
            return line

        source_lines = "\n".join(describe(source) for source in request.sources)
        return f"""
Produce the evidence-backed one-pager for {request.company_name}.

User context: {request.user_context or "No additional context."}

Approved sources:
{source_lines}

Define an ordered report outline that directly answers the user's request before creating any
findings. Treat the user's requested questions and emphasis as the report specification, not as
optional background. Resolve every chosen section with at least one reviewed finding. Use an
unavailable finding when the approved evidence cannot support an answer. Do not estimate valuation
from external knowledge.
For leverage, do not annualize partial-year EBITDA unless the source explicitly supports it;
report supported debt facts and clearly mark an unsupported leverage ratio unavailable.
Use source IDs exactly as listed above in citations. Cite precise text sections or CSV rows.
Call finalize_report, and do not stop until it accepts the report.
""".strip()


def load_source_library(sources: list[ApprovedSource]) -> SourceLibrary:
    """Extract the full text of every approved source for deterministic grounding checks."""
    texts: dict[str, str] = {}
    for source in sources:
        try:
            texts[source.id] = extract_text(Path(source.local_path))
        except Exception:  # an unreadable file simply gets no mechanical checks
            continue
    return SourceLibrary(texts)


async def persist_report(path: Path, report: OnePager) -> None:
    """Persist a completed report without blocking the event loop."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = report.model_dump_json(indent=2)
    await asyncio.to_thread(path.write_text, payload)


async def persist_run(path: Path, run: AnalysisRunRecord) -> None:
    """Persist calculation artifacts and every finding review decision."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = run.model_dump_json(indent=2)
    await asyncio.to_thread(path.write_text, payload)
