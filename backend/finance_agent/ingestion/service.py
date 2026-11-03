"""Safe local ZIP extraction and durable POC intake state."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4
from zipfile import BadZipFile, ZipFile, ZipInfo

from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader
from pptx import Presentation

from .models import (
    IntakeSession,
    IntakeStatus,
    Relevance,
    ReportAgentModel,
    SourceClassification,
    SourceFile,
)


MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_FILES = 250
MAX_EXPANDED_BYTES = 500 * 1024 * 1024
READABLE_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".csv", ".txt"}


class IntakeValidationError(ValueError):
    pass


class IntakeNotFoundError(LookupError):
    pass


class IntakeService:
    def __init__(self, storage_root: Path) -> None:
        self.storage_root = storage_root

    def create(
        self,
        *,
        company_name: str,
        user_context: str | None,
        archive_name: str,
        archive_bytes: bytes,
    ) -> IntakeSession:
        company_name = company_name.strip()
        if not company_name:
            raise IntakeValidationError("company name is required")
        if not archive_name.lower().endswith(".zip"):
            raise IntakeValidationError("only .zip archives are accepted")
        if len(archive_bytes) > MAX_ARCHIVE_BYTES:
            raise IntakeValidationError("archive exceeds the 100 MB limit")

        intake_id = uuid4().hex
        intake_root = self.storage_root / intake_id
        source_root = intake_root / "sources"
        source_root.mkdir(parents=True, exist_ok=False)
        try:
            sources = self._extract(archive_bytes, source_root)
            session = IntakeSession(
                id=intake_id,
                company_name=company_name,
                user_context=user_context.strip() if user_context and user_context.strip() else None,
                archive_name=Path(archive_name).name,
                archive_size_bytes=len(archive_bytes),
                created_at=datetime.now(timezone.utc),
                status=IntakeStatus.AWAITING_APPROVAL,
                sources=sources,
            )
            self._save(session)
            return session
        except Exception:
            shutil.rmtree(intake_root, ignore_errors=True)
            raise

    def get(self, intake_id: str) -> IntakeSession:
        path = self._session_path(intake_id)
        if not path.is_file():
            raise IntakeNotFoundError("intake session not found")
        return IntakeSession.model_validate_json(path.read_text())

    def approve(
        self,
        intake_id: str,
        source_ids: list[str],
        *,
        use_fast_finding_review: bool | None = None,
        report_agent_model: ReportAgentModel | None = None,
    ) -> IntakeSession:
        session = self.get(intake_id)
        requested = list(dict.fromkeys(source_ids))
        by_id = {source.id: source for source in session.sources}
        unknown = [source_id for source_id in requested if source_id not in by_id]
        if unknown:
            raise IntakeValidationError("approval contains unknown source IDs")
        unreadable = [source_id for source_id in requested if not by_id[source_id].readable]
        if unreadable:
            raise IntakeValidationError("unreadable sources cannot be approved")
        if not requested:
            raise IntakeValidationError("approve at least one readable source")

        for source in session.sources:
            source.included = source.id in requested
        session.approved_source_ids = requested
        session.approved_at = datetime.now(timezone.utc)
        session.source_snapshot_id = hashlib.sha256(
            "\n".join(sorted(requested)).encode()
        ).hexdigest()[:24]
        session.status = IntakeStatus.APPROVED
        if use_fast_finding_review is not None:
            session.use_fast_finding_review = use_fast_finding_review
        if report_agent_model is not None:
            session.report_agent_model = report_agent_model
        self._save(session)
        return session

    def approved_paths(self, session: IntakeSession) -> dict[str, Path]:
        source_root = self.storage_root / session.id / "sources"
        return {
            source.id: source_root / source.relative_path
            for source in session.sources
            if source.id in session.approved_source_ids
        }

    def previews(self, session: IntakeSession, *, chars_per_file: int = 6_000) -> dict[str, str]:
        source_root = self.storage_root / session.id / "sources"
        return {
            source.id: _extract_preview(source_root / source.relative_path, chars_per_file)
            for source in session.sources
            if source.readable
        }

    def apply_classifications(
        self, session: IntakeSession, classifications: list[SourceClassification]
    ) -> IntakeSession:
        by_id = {classification.source_id: classification for classification in classifications}
        expected = {source.id for source in session.sources if source.readable}
        if set(by_id) != expected:
            raise IntakeValidationError("classification set does not match readable sources")
        for source in session.sources:
            if not source.readable:
                continue
            result = by_id[source.id]
            source.relevance = result.relevance
            source.relevance_reason = result.rationale
            source.sensitivity = result.sensitivity.value
            source.included = result.recommended_include
            source.content_date = result.content_date
            source.flags = list(result.flags)
        self._save(session)
        return session

    def _extract(self, archive_bytes: bytes, source_root: Path) -> list[SourceFile]:
        archive_path = source_root.parent / "upload.zip"
        archive_path.write_bytes(archive_bytes)
        try:
            with ZipFile(archive_path) as archive:
                members = [member for member in archive.infolist() if not member.is_dir()]
                if len(members) > MAX_FILES:
                    raise IntakeValidationError("archive contains more than 250 files")
                if sum(member.file_size for member in members) > MAX_EXPANDED_BYTES:
                    raise IntakeValidationError("expanded archive exceeds the 500 MB limit")
                sources = [self._extract_member(archive, member, source_root) for member in members]
        except BadZipFile as exc:
            raise IntakeValidationError("archive is not a valid ZIP file") from exc
        finally:
            archive_path.unlink(missing_ok=True)
        return sources

    def _extract_member(
        self, archive: ZipFile, member: ZipInfo, source_root: Path
    ) -> SourceFile:
        relative = PurePosixPath(member.filename)
        if relative.is_absolute() or ".." in relative.parts or not relative.name:
            raise IntakeValidationError("archive contains an unsafe file path")
        if stat.S_ISLNK(member.external_attr >> 16):
            raise IntakeValidationError("archive contains a symbolic link")

        extension = Path(relative.name).suffix.lower()
        readable = extension in READABLE_EXTENSIONS and not bool(member.flag_bits & 0x1)
        reason = None
        if member.flag_bits & 0x1:
            reason = "Password-protected file"
        elif extension not in READABLE_EXTENSIONS:
            reason = "Unsupported file type"

        if readable:
            target = source_root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as incoming, target.open("wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)

        source_id = hashlib.sha256(str(relative).encode()).hexdigest()[:16]
        document_date = datetime(*member.date_time, tzinfo=timezone.utc)
        return SourceFile(
            id=source_id,
            name=relative.name,
            relative_path=str(relative),
            file_type=extension.removeprefix(".").upper() or "UNKNOWN",
            size_bytes=member.file_size,
            document_date=document_date,
            relevance=Relevance.LOW,
            relevance_reason="Awaiting AI triage." if readable else reason or "Unreadable file.",
            sensitivity="Unclassified" if readable else "Unknown",
            readable=readable,
            included=False,
            exclusion_reason=reason,
        )

    def _session_path(self, intake_id: str) -> Path:
        if not intake_id.isalnum():
            raise IntakeNotFoundError("intake session not found")
        return self.storage_root / intake_id / "session.json"

    def _save(self, session: IntakeSession) -> None:
        path = self.storage_root / session.id / "session.json"
        path.write_text(json.dumps(session.model_dump(mode="json"), indent=2))


def _extract_preview(path: Path, limit: int) -> str:
    try:
        return extract_text(path, max_pages=12, max_slides=20, max_sheets=10, max_rows=40)[:limit]
    except Exception as exc:
        return f"[Preview extraction failed: {type(exc).__name__}]"


def extract_text(
    path: Path,
    *,
    max_pages: int | None = None,
    max_slides: int | None = None,
    max_sheets: int | None = None,
    max_rows: int | None = None,
) -> str:
    """Extract plain text from a readable source. Limits of None read the whole file."""
    extension = path.suffix.lower()
    if extension in {".txt", ".csv"}:
        return path.read_text(errors="replace")
    if extension == ".pdf":
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages[:max_pages])
    if extension == ".docx":
        return "\n".join(paragraph.text for paragraph in Document(path).paragraphs)
    if extension == ".pptx":
        presentation = Presentation(path)
        return "\n".join(
            shape.text
            for slide in list(presentation.slides)[:max_slides]
            for shape in slide.shapes
            if hasattr(shape, "text")
        )
    if extension in {".xlsx", ".xlsm"}:
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            lines = []
            for sheet in workbook.worksheets[:max_sheets]:
                lines.append(f"[{sheet.title}]")
                for row in sheet.iter_rows(max_row=max_rows, values_only=True):
                    lines.append(" | ".join("" if value is None else str(value) for value in row[:20]))
            return "\n".join(lines)
        finally:
            workbook.close()
    return ""
