import json
from pathlib import Path

import pytest

from finance_agent.analysis.models import ExecutionArtifact, FindingReview, ReportSectionPlan
from finance_agent.analysis.review import REVIEW_INSTRUCTIONS
from finance_agent.analysis.service import FindingService, finalize_report
from finance_agent.analysis.state import AnalysisState
from finance_agent.reports import OnePager


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_report.json"


class ApprovingReviewer:
    async def review(self, finding, contexts=(), calculations=()):
        return FindingReview(accepted=True)


class RejectingReviewer:
    async def review(self, finding, contexts=(), calculations=()):
        return FindingReview(accepted=False, reasons=["Citation does not support the value."])


def load_fixture() -> OnePager:
    return OnePager.model_validate(json.loads(FIXTURE_PATH.read_text()))


def test_reviewer_instructions_allow_grounded_calculations() -> None:
    assert "Calculated findings are explicitly allowed" in REVIEW_INSTRUCTIONS
    assert "material-misstatement gate" in REVIEW_INSTRUCTIONS
    assert "reasonable interpretation" in REVIEW_INSTRUCTIONS
    assert "Human oversight does not by itself count against AI nativeness" in REVIEW_INSTRUCTIONS
    assert "Do not confuse a debt maturity date" in REVIEW_INSTRUCTIONS
    assert "Judge only the exact finding" in REVIEW_INSTRUCTIONS


@pytest.mark.asyncio
async def test_calculated_finding_matches_recorded_artifact() -> None:
    report = load_fixture()
    leverage_section = next(
        section for section in report.sections if section.id == "section-leverage"
    )
    finding = leverage_section.findings.pop()
    events = []
    state = AnalysisState(report=report, event_sink=events.append)
    state.record_artifact(
        ExecutionArtifact(
            id="artifact-net-leverage",
            code="print calculation",
            stdout='{"net_leverage": 3.452380952380952380952380952}',
            stderr="",
            exit_code=0,
            outputs={"net_leverage": "3.452380952380952380952380952"},
        )
    )

    result = await FindingService(state, ApprovingReviewer()).create(
        "section-leverage", finding
    )

    assert result.accepted is True
    assert leverage_section.findings == [finding]
    assert events[-1].kind == "review"
    assert events[-1].status == "completed"
    assert events[-1].label.startswith("Finding accepted")


@pytest.mark.asyncio
async def test_calculated_finding_rejects_mismatched_artifact() -> None:
    report = load_fixture()
    leverage_section = next(
        section for section in report.sections if section.id == "section-leverage"
    )
    finding = leverage_section.findings.pop()
    state = AnalysisState(report=report)
    state.record_artifact(
        ExecutionArtifact(
            id="artifact-net-leverage",
            code="print calculation",
            stdout='{"net_leverage": 4.0}',
            stderr="",
            exit_code=0,
            outputs={"net_leverage": 4.0},
        )
    )

    result = await FindingService(state, ApprovingReviewer()).create(
        "section-leverage", finding
    )

    assert result.accepted is False
    assert "does not match" in result.reasons[0]
    assert leverage_section.findings == []


@pytest.mark.asyncio
async def test_reviewer_rejection_does_not_mutate_report() -> None:
    report = load_fixture()
    section = report.sections[0]
    finding = section.findings.pop()
    state = AnalysisState(report=report)

    result = await FindingService(state, RejectingReviewer()).create(section.id, finding)

    assert result.accepted is False
    assert section.findings == []


def test_finalize_requires_every_section_to_be_resolved() -> None:
    report = load_fixture()
    report.sections[0].findings = []
    state = AnalysisState(report=report)

    result = finalize_report(state)

    assert result.accepted is False
    assert "Business model" in result.reasons[0]


def test_outline_is_defined_from_plans_before_findings() -> None:
    report = load_fixture()
    report.sections = []
    report.status = "draft"
    state = AnalysisState(report=report)

    section_ids = state.define_outline(
        [
            ReportSectionPlan(
                key="revenue_quality",
                title="ARR, NRR, churn, and cohort quality",
                objective="Test whether recurring revenue quality is supported.",
            ),
            ReportSectionPlan(
                key="risks_and_mitigants",
                title="Key risks and mitigants",
                objective="Identify the largest credit risks and paths to comfort.",
            ),
        ]
    )

    assert section_ids == ["section-revenue-quality", "section-risks-and-mitigants"]
    assert [section.title for section in state.report.sections] == [
        "ARR, NRR, churn, and cohort quality",
        "Key risks and mitigants",
    ]

