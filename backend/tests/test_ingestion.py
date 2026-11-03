from io import BytesIO
from pathlib import Path
import time
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

import finance_agent.api as api
from finance_agent.ingestion.classifier import enforce_conservative_inclusion
from finance_agent.analysis.models import (
    AnalysisRequest,
    AnalysisRunRecord,
    ApprovedSource,
    ReportSectionPlan,
)
from finance_agent.analysis.service import finalize_report
from finance_agent.analysis.state import AnalysisState, create_report_draft
from finance_agent.ingestion.models import (
    ExclusionBasis,
    IntakeStatus,
    Relevance,
    Sensitivity,
    SourceClassification,
)
from finance_agent.ingestion.service import IntakeService, IntakeValidationError
from finance_agent.reports.models import (
    BottomLine,
    EvidenceDirectness,
    EvidenceLevel,
    EvidenceStrength,
    FindingStatus,
    TextFinding,
)


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


LIGHTWEIGHT_ZIP = Path(__file__).parent / "fixtures" / "lightweight_dataroom.zip"


def synthetic_classifications(session):
    results = []
    for source in session.sources:
        if not source.readable:
            continue
        is_board = "Board" in source.name
        results.append(
            SourceClassification(
                source_id=source.id,
                relevance=Relevance.MEDIUM if is_board else Relevance.HIGH,
                sensitivity=Sensitivity.RESTRICTED if is_board else Sensitivity.CONFIDENTIAL,
                recommended_include=not is_board,
                rationale=(
                    "Restricted governance material; exclude pending human review."
                    if is_board
                    else "Contains core company, financial, debt, or product evidence."
                ),
            )
        )
    return results


class SyntheticClassifier:
    async def classify(self, session, previews):
        assert previews
        return synthetic_classifications(session)


class SyntheticReportRunner:
    async def run(self, session, approved_paths, *, event_sink=None):
        request = AnalysisRequest(
            report_id="report-synthetic",
            company_id="company-synthetic",
            company_name=session.company_name,
            source_snapshot_id=session.source_snapshot_id,
            user_context=session.user_context,
            sources=[
                ApprovedSource(id=source_id, name=path.name, local_path=path)
                for source_id, path in approved_paths.items()
            ],
        )
        state = AnalysisState(report=create_report_draft(request))
        state.define_outline(
            [
                ReportSectionPlan(
                    key=key,
                    title=title,
                    objective=f"Answer the analyst's question about {title.lower()}.",
                )
                for key, title in (
                    ("business_model", "Business model"),
                    ("revenue", "Revenue and ARR"),
                    ("capitalization_and_leverage", "Capitalization and leverage"),
                    ("valuation", "Valuation"),
                    ("ai_native_assessment", "AI-native assessment"),
                )
            ]
        )
        for section in state.report.sections:
            section.findings.append(
                TextFinding(
                    id=f"finding-{section.section_type}",
                    label=section.title,
                    status=FindingStatus.UNAVAILABLE,
                    value=None,
                    evidence_strength=EvidenceStrength(
                        level=EvidenceLevel.LIMITED,
                        directness=EvidenceDirectness.INFERRED,
                        source_count=0,
                        has_conflicts=False,
                        reasons=["Synthetic report fixture."],
                    ),
                )
            )
        state.report.bottom_line = BottomLine(
            summary="Synthetic report: no findings are supported.",
            recommendation="Do not rely on this synthetic report.",
            finding_ids=[state.report.sections[0].findings[0].id],
        )
        assert finalize_report(state).accepted
        return AnalysisRunRecord(report=state.report)


def test_create_and_approve_intake(tmp_path: Path) -> None:
    service = IntakeService(tmp_path / "intakes")
    session = service.create(
        company_name="Acme",
        user_context="Focus on leverage",
        archive_name="dataroom.zip",
        archive_bytes=make_zip(
            {
                "financials/ARR.csv": b"month,revenue\nJan,10\n",
                "board/Board Minutes.pdf": b"placeholder",
                "logo.png": b"not parsed",
            }
        ),
    )

    assert session.status == IntakeStatus.AWAITING_APPROVAL
    assert len(session.sources) == 3
    arr = next(source for source in session.sources if source.name == "ARR.csv")
    board = next(source for source in session.sources if source.name == "Board Minutes.pdf")
    logo = next(source for source in session.sources if source.name == "logo.png")
    session = service.apply_classifications(session, synthetic_classifications(session))
    arr = next(source for source in session.sources if source.name == "ARR.csv")
    board = next(source for source in session.sources if source.name == "Board Minutes.pdf")
    assert arr.relevance == Relevance.HIGH
    assert arr.included is True
    assert board.sensitivity == "Restricted"
    assert board.included is False
    assert logo.readable is False

    approved = service.approve(session.id, [arr.id])

    assert approved.status == IntakeStatus.APPROVED
    assert approved.approved_source_ids == [arr.id]
    assert approved.source_snapshot_id
    assert service.approved_paths(approved)[arr.id].read_bytes().startswith(b"month")


def test_rejects_path_traversal(tmp_path: Path) -> None:
    service = IntakeService(tmp_path / "intakes")

    with pytest.raises(IntakeValidationError, match="unsafe file path"):
        service.create(
            company_name="Acme",
            user_context=None,
            archive_name="dataroom.zip",
            archive_bytes=make_zip({"../escape.txt": b"nope"}),
        )


def test_rejects_unknown_or_unreadable_approval(tmp_path: Path) -> None:
    service = IntakeService(tmp_path / "intakes")
    session = service.create(
        company_name="Acme",
        user_context=None,
        archive_name="dataroom.zip",
        archive_bytes=make_zip({"logo.png": b"nope"}),
    )

    with pytest.raises(IntakeValidationError, match="unreadable"):
        service.approve(session.id, [session.sources[0].id])


def test_api_pauses_until_explicit_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "service", IntakeService(tmp_path / "intakes"))
    monkeypatch.setattr(api, "classifier_factory", lambda: SyntheticClassifier())
    client = TestClient(api.app)

    created = client.post(
        "/api/intakes",
        data={"company_name": "Acme", "user_context": "Focus on ARR"},
        files={"archive": ("dataroom.zip", make_zip({"ARR.csv": b"arr\n10\n"}), "application/zip")},
    )

    assert created.status_code == 200
    intake = created.json()
    assert intake["status"] == "awaiting_approval"
    source_id = intake["sources"][0]["id"]

    blocked_job = client.post(f"/api/intakes/{intake['id']}/report-jobs")
    assert blocked_job.status_code == 422

    approved = client.post(
        f"/api/intakes/{intake['id']}/approve",
        json={
            "source_ids": [source_id],
            "use_fast_finding_review": True,
            "report_agent_model": "opus",
        },
    )

    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["approved_source_ids"] == [source_id]
    assert approved.json()["use_fast_finding_review"] is True
    assert approved.json()["report_agent_model"] == "opus"

    monkeypatch.setattr(api, "analysis_runner_factory", lambda: SyntheticReportRunner())
    generated = client.post(f"/api/intakes/{intake['id']}/reports")

    assert generated.status_code == 200
    assert generated.json()["company_name"] == "Acme"
    assert generated.json()["status"] == "verified"
    assert len(generated.json()["sections"]) == 5
    report_id = generated.json()["id"]
    assert (tmp_path / "intakes" / intake["id"] / "reports" / f"{report_id}.json").is_file()
    assert (tmp_path / "intakes" / intake["id"] / "reports" / f"{report_id}.run.json").is_file()

    profiles = client.get("/api/profiles")
    assert profiles.status_code == 200
    assert profiles.json()[0]["latest_report_id"] == report_id
    assert (
        profiles.json()[0]["latest_report_generated_at"]
        == generated.json()["generated_at"]
    )

    profile = client.get(f"/api/profiles/{intake['id']}")
    assert profile.status_code == 200
    assert profile.json()["latest_report_id"] == report_id
    assert profile.json()["latest_report_generated_at"] == generated.json()["generated_at"]

    report_page = client.get(f"/report/{intake['id']}")
    assert report_page.status_code == 200
    assert "<!doctype html>" in report_page.text.lower()

    started = client.post(f"/api/intakes/{intake['id']}/report-jobs")
    assert started.status_code == 202
    job = started.json()
    assert job["status"] in {"queued", "running"}

    for _ in range(100):
        status = client.get(
            f"/api/intakes/{intake['id']}/report-jobs/{job['id']}"
        )
        assert status.status_code == 200
        job = status.json()
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)

    assert job["status"] == "completed"
    assert job["report_id"] == "report-synthetic"
    assert [event["label"] for event in job["events"]] == [
        "Report agent started",
        "One-pager verified and saved",
    ]
    assert (
        tmp_path / "intakes" / intake["id"] / "jobs" / f"{job['id']}.json"
    ).is_file()
    profiles = client.get("/api/profiles").json()
    assert profiles[0]["job_status"] == "completed"
    assert profiles[0]["active_job_id"] is None

    deleted = client.delete(f"/api/profiles/{intake['id']}")
    assert deleted.status_code == 200
    assert client.get(f"/api/profiles/{intake['id']}").status_code == 404
    assert client.get(f"/report/{intake['id']}").status_code == 404
    assert not client.get("/api/profiles").json()

    restored = client.post(
        f"/api/profiles/{intake['id']}/restore",
        json={"undo_token": deleted.json()["undo_token"]},
    )
    assert restored.status_code == 200
    assert client.get(f"/api/profiles/{intake['id']}").status_code == 200


def test_lightweight_fixture_exercises_review_signals(tmp_path: Path) -> None:
    assert LIGHTWEIGHT_ZIP.stat().st_size < 10_000
    service = IntakeService(tmp_path / "intakes")
    session = service.create(
        company_name="Acme Technologies",
        user_context="Focus on leverage and AI differentiation",
        archive_name=LIGHTWEIGHT_ZIP.name,
        archive_bytes=LIGHTWEIGHT_ZIP.read_bytes(),
    )

    session = service.apply_classifications(session, synthetic_classifications(session))
    assert len(session.sources) == 5
    assert sum(source.included for source in session.sources) == 4
    board = next(source for source in session.sources if "Board" in source.name)
    assert board.sensitivity == "Restricted"
    assert board.included is False


def test_triage_cannot_drop_relevant_source_as_merely_duplicative() -> None:
    cim_id = "source-cim"
    ai_overview_id = "source-ai-overview"
    classifications = [
        SourceClassification(
            source_id=cim_id,
            relevance=Relevance.HIGH,
            sensitivity=Sensitivity.CONFIDENTIAL,
            recommended_include=True,
            rationale="Core company overview.",
        ),
        SourceClassification(
            source_id=ai_overview_id,
            relevance=Relevance.LOW,
            sensitivity=Sensitivity.CONFIDENTIAL,
            recommended_include=False,
            exclusion_basis=ExclusionBasis.DUPLICATE,
            rationale="Duplicative of the company overview.",
        ),
    ]

    guarded = enforce_conservative_inclusion(
        classifications,
        {
            cim_id: "Acme sells an AI-assisted procurement platform.",
            ai_overview_id: "Models are fine-tuned and every recommendation requires approval.",
        },
    )

    assert guarded[1].recommended_include is True
    assert guarded[1].exclusion_basis is None
    assert "recall-first policy" in guarded[1].rationale


def test_triage_still_removes_exact_content_duplicates() -> None:
    classifications = [
        SourceClassification(
            source_id=source_id,
            relevance=Relevance.HIGH,
            sensitivity=Sensitivity.CONFIDENTIAL,
            recommended_include=True,
            rationale="Relevant evidence.",
        )
        for source_id in ("source-original", "source-copy")
    ]

    guarded = enforce_conservative_inclusion(
        classifications,
        {
            "source-original": "Same content\nwith spacing.",
            "source-copy": "  same CONTENT with   spacing. ",
        },
    )

    assert guarded[0].recommended_include is True
    assert guarded[1].recommended_include is False
    assert guarded[1].exclusion_basis == ExclusionBasis.DUPLICATE
    assert guarded[1].rationale.startswith("Exact content duplicate")
