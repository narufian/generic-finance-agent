"""Local HTTP API and static UI for Generic Finance Agent."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread, current_thread
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydantic_ai.ui.vercel_ai import VercelAIAdapter

from finance_agent.analysis.models import (
    ReportJob,
    ReportJobStatus,
    RunEvent,
    RunEventKind,
    RunEventStatus,
)
from finance_agent.analysis.runner import (
    AnalysisRunError,
    LiveAnalysisRunner,
    persist_report,
    persist_run,
)
from finance_agent.chat import build_chat_agent
from finance_agent.config import load_live_settings
from finance_agent.ingestion.classifier import PydanticSourceClassifier, SourceClassifier
from finance_agent.ingestion.models import (
    ApprovalRequest,
    IntakeSession,
    IntakeStatus,
    ProfileSummary,
)
from finance_agent.ingestion.service import (
    IntakeNotFoundError,
    IntakeService,
    IntakeValidationError,
    MAX_ARCHIVE_BYTES,
)
from finance_agent.reports.models import OnePager


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = PROJECT_ROOT / "web"
CHAT_DIST = PROJECT_ROOT / "chat" / "dist"
service = IntakeService(PROJECT_ROOT / "data" / "intakes")
app = FastAPI(title="Generic Finance Agent", version="0.1.0")
report_workers: set[Thread] = set()


def _deleted_profiles_root() -> Path:
    # Resolved per call so a test that swaps `service` never writes into the real data folder.
    return service.storage_root.parent / "deleted_intakes"


class RestoreProfileRequest(BaseModel):
    undo_token: str


def _job_path(intake_id: str, job_id: str) -> Path:
    return service.storage_root / intake_id / "jobs" / f"{job_id}.json"


def _write_job(job: ReportJob) -> None:
    path = _job_path(job.intake_id, job.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(job.model_dump_json(indent=2))
    temporary.replace(path)


def _read_job(intake_id: str, job_id: str) -> ReportJob:
    path = _job_path(intake_id, job_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="report job not found")
    return ReportJob.model_validate_json(path.read_text())


def _latest_job(intake_root: Path) -> ReportJob | None:
    paths = sorted(
        intake_root.glob("jobs/job-*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in paths:
        try:
            return ReportJob.model_validate_json(path.read_text())
        except (OSError, ValueError):
            continue
    return None


def _valid_report_id(report_id: str) -> bool:
    return report_id.startswith("report-") and report_id[7:].isalnum()


async def _run_report_job(job: ReportJob) -> None:
    def record_event(event: RunEvent) -> None:
        job.events.append(event)
        job.updated_at = event.occurred_at
        _write_job(job)

    try:
        job.status = ReportJobStatus.RUNNING
        job.updated_at = datetime.now(timezone.utc)
        await asyncio.to_thread(_write_job, job)
        record_event(
            RunEvent(
                id=f"event-{uuid4().hex}",
                kind=RunEventKind.JOB,
                status=RunEventStatus.RUNNING,
                label="Report agent started",
                detail="The orchestrator is working from the approved source snapshot.",
                occurred_at=datetime.now(timezone.utc),
            )
        )
        session = service.get(job.intake_id)
        run = await asyncio.wait_for(
            analysis_runner_factory().run(
                session,
                service.approved_paths(session),
                event_sink=record_event,
            ),
            timeout=float(os.getenv("FINANCE_AGENT_JOB_TIMEOUT_SECONDS", "1800")),
        )
        report = run.report
        report_path = (
            service.storage_root
            / job.intake_id
            / "reports"
            / f"{report.id}.json"
        )
        await persist_report(report_path, report)
        await persist_run(report_path.with_name(f"{report.id}.run.json"), run)
        job.status = ReportJobStatus.COMPLETED
        job.report_id = report.id
        job.error = None
        record_event(
            RunEvent(
                id=f"event-{uuid4().hex}",
                kind=RunEventKind.JOB,
                status=RunEventStatus.COMPLETED,
                label="One-pager verified and saved",
                detail="The completed report and its review records are persisted.",
                occurred_at=datetime.now(timezone.utc),
            )
        )
    except Exception as exc:
        job.status = ReportJobStatus.FAILED
        job.error = str(exc)[:500] or "Report generation failed."
        record_event(
            RunEvent(
                id=f"event-{uuid4().hex}",
                kind=RunEventKind.JOB,
                status=RunEventStatus.FAILED,
                label="Report generation failed",
                detail=job.error,
                occurred_at=datetime.now(timezone.utc),
            )
        )
    finally:
        job.updated_at = datetime.now(timezone.utc)
        await asyncio.to_thread(_write_job, job)


def _start_report_worker(job: ReportJob) -> None:
    def run() -> None:
        try:
            asyncio.run(_run_report_job(job))
        finally:
            report_workers.discard(current_thread())

    worker = Thread(
        target=run,
        name=f"report-job-{job.id}",
        daemon=True,
    )
    report_workers.add(worker)
    worker.start()


def classifier_factory() -> SourceClassifier:
    return PydanticSourceClassifier(load_live_settings().triage_model)


def analysis_runner_factory() -> LiveAnalysisRunner:
    return LiveAnalysisRunner()


@app.get("/api/profiles", response_model=list[ProfileSummary])
def list_profiles() -> list[ProfileSummary]:
    profiles: list[ProfileSummary] = []
    if not service.storage_root.exists():
        return profiles
    for session_path in service.storage_root.glob("*/session.json"):
        try:
            session = IntakeSession.model_validate_json(session_path.read_text())
            report_paths = [
                path
                for path in session_path.parent.glob("reports/report-*.json")
                if not path.name.endswith(".run.json")
            ]
            reports = [
                OnePager.model_validate_json(path.read_text()) for path in report_paths
            ]
            latest = max(reports, key=lambda report: report.generated_at, default=None)
            latest_job = _latest_job(session_path.parent)
            updated_timestamp = max(
                [session_path.stat().st_mtime]
                + [path.stat().st_mtime for path in report_paths]
                + (
                    [_job_path(session.id, latest_job.id).stat().st_mtime]
                    if latest_job
                    else []
                )
            )
            active_job = (
                latest_job
                if latest_job
                and latest_job.status
                in {ReportJobStatus.QUEUED, ReportJobStatus.RUNNING}
                else None
            )
            profiles.append(
                ProfileSummary(
                    intake_id=session.id,
                    company_name=session.company_name,
                    user_context=session.user_context,
                    archive_name=session.archive_name,
                    created_at=session.created_at,
                    updated_at=datetime.fromtimestamp(updated_timestamp, timezone.utc),
                    status=session.status,
                    source_count=len(session.sources),
                    approved_source_count=len(session.approved_source_ids),
                    latest_report_id=latest.id if latest else None,
                    latest_report_generated_at=latest.generated_at if latest else None,
                    report_status=latest.status.value if latest else None,
                    active_job_id=active_job.id if active_job else None,
                    job_status=latest_job.status.value if latest_job else None,
                )
            )
        except (OSError, ValueError):
            continue
    return sorted(
        profiles,
        key=lambda profile: (
            profile.updated_at
            if profile.active_job_id
            else profile.latest_report_generated_at or profile.updated_at
        ),
        reverse=True,
    )


@app.get("/api/profiles/{intake_id}", response_model=ProfileSummary)
def get_profile_summary(intake_id: str) -> ProfileSummary:
    if not intake_id.isalnum():
        raise HTTPException(status_code=404, detail="profile not found")
    profile = next(
        (candidate for candidate in list_profiles() if candidate.intake_id == intake_id),
        None,
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return profile


@app.delete("/api/profiles/{intake_id}")
def delete_profile(intake_id: str) -> dict[str, str]:
    """Remove a profile from the workspace while keeping a recoverable local copy."""
    if not intake_id.isalnum():
        raise HTTPException(status_code=404, detail="profile not found")
    source = service.storage_root / intake_id
    if not (source / "session.json").is_file():
        raise HTTPException(status_code=404, detail="profile not found")
    latest_job = _latest_job(source)
    if latest_job and latest_job.status in {
        ReportJobStatus.QUEUED,
        ReportJobStatus.RUNNING,
    }:
        raise HTTPException(
            status_code=409,
            detail="This profile is still generating. Wait for it to finish before deleting it.",
        )
    undo_token = uuid4().hex
    _deleted_profiles_root().mkdir(parents=True, exist_ok=True)
    destination = _deleted_profiles_root() / f"{intake_id}-{undo_token}"
    source.replace(destination)
    return {"intake_id": intake_id, "undo_token": undo_token}


@app.post("/api/profiles/{intake_id}/restore")
def restore_profile(intake_id: str, request: RestoreProfileRequest) -> dict[str, str]:
    if not intake_id.isalnum() or not request.undo_token.isalnum():
        raise HTTPException(status_code=404, detail="deleted profile not found")
    source = _deleted_profiles_root() / f"{intake_id}-{request.undo_token}"
    destination = service.storage_root / intake_id
    if not (source / "session.json").is_file():
        raise HTTPException(status_code=404, detail="deleted profile not found")
    if destination.exists():
        raise HTTPException(status_code=409, detail="profile already exists")
    service.storage_root.mkdir(parents=True, exist_ok=True)
    source.replace(destination)
    return {"intake_id": intake_id, "status": "restored"}


@app.post("/api/intakes", response_model=IntakeSession)
async def create_intake(
    company_name: str = Form(...),
    user_context: str | None = Form(None),
    archive: UploadFile = File(...),
) -> IntakeSession:
    payload = await archive.read(MAX_ARCHIVE_BYTES + 1)
    try:
        session = service.create(
            company_name=company_name,
            user_context=user_context,
            archive_name=archive.filename or "upload.zip",
            archive_bytes=payload,
        )
        classifications = await classifier_factory().classify(
            session, service.previews(session)
        )
        return service.apply_classifications(session, classifications)
    except IntakeValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI source triage failed: {exc}") from exc


@app.get("/api/intakes/{intake_id}", response_model=IntakeSession)
def get_intake(intake_id: str) -> IntakeSession:
    try:
        return service.get(intake_id)
    except IntakeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/intakes/{intake_id}/approve", response_model=IntakeSession)
def approve_intake(intake_id: str, approval: ApprovalRequest) -> IntakeSession:
    try:
        return service.approve(
            intake_id,
            approval.source_ids,
            use_fast_finding_review=approval.use_fast_finding_review,
            report_agent_model=approval.report_agent_model,
        )
    except IntakeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IntakeValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/intakes/{intake_id}/reports", response_model=OnePager)
async def create_report(intake_id: str) -> OnePager:
    try:
        session = service.get(intake_id)
        run = await analysis_runner_factory().run(
            session, service.approved_paths(session)
        )
        report = run.report
        report_path = service.storage_root / intake_id / "reports" / f"{report.id}.json"
        await persist_report(report_path, report)
        await persist_run(report_path.with_name(f"{report.id}.run.json"), run)
        return report
    except IntakeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (IntakeValidationError, AnalysisRunError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Report generation failed: {exc}") from exc


@app.post(
    "/api/intakes/{intake_id}/report-jobs",
    response_model=ReportJob,
    status_code=202,
)
async def create_report_job(intake_id: str) -> ReportJob:
    try:
        session = service.get(intake_id)
        if session.status != IntakeStatus.APPROVED or not session.source_snapshot_id:
            raise IntakeValidationError(
                "the source set must be approved before analysis"
            )
        latest_job = _latest_job(service.storage_root / intake_id)
        if latest_job and latest_job.status in {
            ReportJobStatus.QUEUED,
            ReportJobStatus.RUNNING,
        }:
            return latest_job
        now = datetime.now(timezone.utc)
        job = ReportJob(
            id=f"job-{uuid4().hex}",
            intake_id=intake_id,
            status=ReportJobStatus.QUEUED,
            created_at=now,
            updated_at=now,
        )
        await asyncio.to_thread(_write_job, job)
        _start_report_worker(job)
        return job
    except IntakeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IntakeValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(
    "/api/intakes/{intake_id}/report-jobs/{job_id}",
    response_model=ReportJob,
)
def get_report_job(intake_id: str, job_id: str) -> ReportJob:
    if (
        not intake_id.isalnum()
        or not job_id.startswith("job-")
        or not job_id[4:].isalnum()
    ):
        raise HTTPException(status_code=404, detail="report job not found")
    return _read_job(intake_id, job_id)


@app.get("/api/intakes/{intake_id}/reports/{report_id}", response_model=OnePager)
def get_report(intake_id: str, report_id: str) -> OnePager:
    if not intake_id.isalnum() or not _valid_report_id(report_id):
        raise HTTPException(status_code=404, detail="report not found")
    path = service.storage_root / intake_id / "reports" / f"{report_id}.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="report not found")
    return OnePager.model_validate_json(path.read_text())


@app.get("/report/{profile_id}", include_in_schema=False)
def profile_page(profile_id: str) -> FileResponse:
    """Serve the SPA shell for a profile at its latest available workflow step."""
    if not profile_id.isalnum():
        raise HTTPException(status_code=404, detail="profile not found")
    try:
        service.get(profile_id)
    except IntakeNotFoundError as exc:
        raise HTTPException(status_code=404, detail="profile not found") from exc
    return FileResponse(WEB_ROOT / "index.html")


@app.post("/api/chat")
async def chat(request: Request) -> Response:
    """Stream a casual chat turn in the AI SDK v7 UI message protocol."""
    try:
        agent = build_chat_agent()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return await VercelAIAdapter.dispatch_request(request, agent=agent, sdk_version=7)


if CHAT_DIST.is_dir():
    app.mount("/chat", StaticFiles(directory=CHAT_DIST, html=True), name="chat")
else:

    @app.get("/chat", include_in_schema=False)
    def chat_not_built() -> HTMLResponse:
        return HTMLResponse(
            "<p>The chat UI is not built. Run <code>just chat-build</code>, "
            "or <code>just chat-dev</code> for the Vite dev server.</p>",
            status_code=503,
        )


app.mount("/", StaticFiles(directory=WEB_ROOT, html=True), name="web")
