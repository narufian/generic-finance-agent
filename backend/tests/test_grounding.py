"""Deterministic grounding, retry caps, completeness checks, and triage flags."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from finance_agent.analysis.agent import ORCHESTRATOR_TOOLS, WORKER_TOOLS, finalize_report as finalize_tool
from finance_agent.analysis.grounding import NumberToken, SourceLibrary, extract_numbers, number_matches
from finance_agent.analysis.models import (
    AnalysisRequest,
    ApprovedSource,
    ExecutionArtifact,
    FindingReview,
    ReportReview,
    ReportSectionPlan,
)
from finance_agent.analysis.service import (
    MAX_FINDING_ATTEMPTS,
    FindingService,
    check_finalization,
    finalize_report,
    set_bottom_line,
)
from finance_agent.analysis.state import AnalysisState, create_report_draft
from finance_agent.ingestion.classifier import enforce_conservative_inclusion
from finance_agent.ingestion.models import (
    ExclusionBasis,
    Relevance,
    Sensitivity,
    SourceClassification,
    SourceFlag,
    SourceFlagKind,
)
from finance_agent.reports.models import (
    CategoricalFinding,
    Citation,
    EvidenceDirectness,
    EvidenceLevel,
    EvidenceStrength,
    FindingStatus,
    SourceLocation,
    TextFinding,
)


QOE = (
    "4.6 Customer concentration. At 31 December 2025, the largest customer (Meridian Property "
    "Group) represented $2.1M or 11.5% of ARR; the second largest (Harborview Commercial "
    "Partners) $1.4M or 7.7%. The top ten customers represented 34.1% of ARR.\n"
)
KPI = (
    "Month     Open ARR   New Logo   Expansion   Churn    Close ARR\n"
    + "".join(f"Row-{i}     1{i}.00      0.10       0.05       0.00      1{i}.15\n" for i in range(30))
    + "Dec-25     15.89      0.26       2.05       0.00      18.20\n"
    "FY25 total            3.10       2.60      (0.90)\n"
)


class Approve:
    def __init__(self) -> None:
        self.calls = 0
        self.contexts = []

    async def review(self, finding, contexts=(), calculations=()):
        self.calls += 1
        self.contexts = list(contexts)
        return FindingReview(accepted=True)


class Reject:
    async def review(self, finding, contexts=(), calculations=()):
        return FindingReview(accepted=False, reasons=["Not supported."])


def make_state(**overrides) -> AnalysisState:
    request = AnalysisRequest(
        report_id="report-test",
        company_id="company-test",
        company_name="Test Co",
        source_snapshot_id="snapshot-test",
        sources=[
            ApprovedSource(id="qoe", name="qoe.txt", local_path="qoe.txt"),
            ApprovedSource(id="kpi", name="kpi.txt", local_path="kpi.txt"),
        ],
    )
    state = AnalysisState(
        report=create_report_draft(request),
        sources=SourceLibrary({"qoe": QOE, "kpi": KPI}),
        **overrides,
    )
    state.define_outline(
        [
            ReportSectionPlan(
                key="revenue",
                title="Revenue quality",
                objective="Test revenue quality.",
                questions=["Is the ARR story credible?"],
            ),
            ReportSectionPlan(key="ai", title="AI", objective="Assess AI claims."),
        ]
    )
    return state


def evidence(source_count: int = 1) -> EvidenceStrength:
    return EvidenceStrength(
        level=EvidenceLevel.STRONG,
        directness=EvidenceDirectness.DIRECT,
        source_count=source_count,
        has_conflicts=False,
        reasons=["Test."],
    )


def cite(source_id: str, excerpt: str, citation_id: str = "c1") -> Citation:
    return Citation(
        id=citation_id,
        source_id=source_id,
        source_name=f"{source_id}.txt",
        location=SourceLocation(section="test"),
        excerpt=excerpt,
    )


def text_finding(value: str, *citations: Citation, status=FindingStatus.REPORTED, **extra) -> TextFinding:
    return TextFinding(
        id=extra.pop("id", "f1"),
        label=extra.pop("label", "Concentration"),
        status=status,
        value=value,
        citations=list(citations),
        evidence_strength=evidence(len({c.source_id for c in citations})),
        methodology=None if status == FindingStatus.REPORTED else "Assessed from the cited evidence.",
        **extra,
    )


def test_extract_numbers_skips_years_ordinals_and_identifiers() -> None:
    tokens = [t.raw for t in extract_numbers("RISK 1: In 2025 Q4 RW-C-0083 grew 11.5% to $2.1M, 3.25x, ~42%")]
    assert tokens == ["11.5%", "$2.1M", "3.25x", "~42%"]


def test_number_matching_normalizes_written_scale_words() -> None:
    library = SourceLibrary({"cim": "Management reported $12.0 million of ARR."})

    assert number_matches(
        NumberToken(raw="12000000", value=Decimal("12000000"), unit=None),
        library.numbers("cim"),
    )


def test_excerpt_tolerates_light_edits_and_table_rows() -> None:
    library = SourceLibrary({"qoe": QOE, "kpi": KPI})
    paraphrased = cite("qoe", "Meridian Property Group represented $2.1M or 11.5% of ARR")
    table_row = cite("kpi", "Dec-25: Expansion 2.05... FY25 total expansion: 2.60")
    assert library.locate(paraphrased).excerpt_found is True
    assert library.locate(table_row).excerpt_found is True
    assert "Harborview" in library.locate(paraphrased).context


def test_excerpt_from_another_source_is_not_found() -> None:
    library = SourceLibrary({"qoe": QOE, "kpi": KPI})
    assert library.locate(cite("kpi", "the largest customer represented 11.5% of ARR")).excerpt_found is False


@pytest.mark.asyncio
async def test_misquoted_excerpt_is_rejected_before_model_review() -> None:
    state = make_state()
    reviewer = Approve()
    finding = text_finding(
        "Meridian is 11.5% of ARR.",
        cite("qoe", "Meridian Property Group is a pilot customer with a termination right"),
    )
    result = await FindingService(state, reviewer).create("section-revenue", finding)
    assert not result.accepted
    assert "do not occur in their cited source" in result.reasons[0]
    assert reviewer.calls == 0


@pytest.mark.asyncio
async def test_number_missing_from_cited_source_is_rejected() -> None:
    state = make_state()
    finding = text_finding(
        "Meridian is 12.5% of ARR.",
        cite("qoe", "Meridian Property Group) represented $2.1M or 11.5% of ARR"),
    )
    result = await FindingService(state, Approve()).create("section-revenue", finding)
    assert not result.accepted
    assert "12.5%" in result.reasons[0]


@pytest.mark.asyncio
async def test_assessed_finding_may_use_recorded_calculation_outputs() -> None:
    state = make_state()
    state.record_artifact(
        ExecutionArtifact(id="a1", code="x", stdout="", stderr="", exit_code=0, outputs={"top_two_pct": 19.2})
    )
    reviewer = Approve()
    finding = text_finding(
        "The two largest customers are 19.2% of ARR, led by Meridian at 11.5%.",
        cite("qoe", "Meridian Property Group) represented $2.1M or 11.5% of ARR"),
        status=FindingStatus.ASSESSED,
        answers=["q1"],
    )
    result = await FindingService(state, reviewer).create("section-revenue", finding)
    assert result.accepted, result.reasons
    assert reviewer.contexts and "Harborview" in reviewer.contexts[0].context


@pytest.mark.asyncio
async def test_long_text_findings_must_be_split() -> None:
    state = make_state()
    finding = text_finding("x" * 601, cite("qoe", "the largest customer (Meridian Property Group)"))
    result = await FindingService(state, Approve()).create("section-revenue", finding)
    assert not result.accepted
    assert "Split it" in result.reasons[0]


@pytest.mark.asyncio
async def test_categorical_finding_needs_rationale() -> None:
    state = make_state()
    finding = CategoricalFinding(
        id="ai",
        label="AI claim",
        status=FindingStatus.ASSESSED,
        value="Partly real",
        options=["Real", "Partly real", "Marketing"],
        citations=[cite("qoe", "the largest customer (Meridian Property Group)")],
        evidence_strength=evidence(),
        methodology="Assessed.",
    )
    result = await FindingService(state, Approve()).create("section-ai", finding)
    assert not result.accepted
    assert "rationale" in result.reasons[0]


@pytest.mark.asyncio
async def test_repeated_rejections_block_the_finding_and_record_an_issue() -> None:
    state = make_state()
    service = FindingService(state, Reject())
    finding = text_finding(
        "Meridian is 11.5% of ARR.",
        cite("qoe", "Meridian Property Group) represented $2.1M or 11.5% of ARR"),
    )
    for _ in range(MAX_FINDING_ATTEMPTS):
        assert not (await service.create("section-revenue", finding)).accepted
    blocked = await FindingService(state, Approve()).create("section-revenue", finding)
    assert not blocked.accepted
    assert "blocked" in blocked.reasons[0]
    assert [issue.code for issue in state.report.issues] == ["FINDING_UNVERIFIED"]
    assert state.reviews[-1].attempt == MAX_FINDING_ATTEMPTS


@pytest.mark.asyncio
async def test_finalize_requires_answers_and_bottom_line_then_flags_stale_sources() -> None:
    state = make_state(latest_content_date=date(2026, 5, 20))
    state.report.generated_at = datetime(2026, 10, 19, tzinfo=timezone.utc)
    service = FindingService(state, Approve())
    unanswered = text_finding(
        "Meridian is 11.5% of ARR.",
        cite("qoe", "Meridian Property Group) represented $2.1M or 11.5% of ARR"),
    )
    assert (await service.create("section-revenue", unanswered)).accepted
    ai = text_finding(
        "The AI claim is not assessed in these sources.",
        cite("qoe", "the largest customer (Meridian Property Group)"),
        id="f2",
        label="AI",
    )
    assert (await service.create("section-ai", ai)).accepted

    reasons = check_finalization(state).reasons
    assert any("q1" in reason for reason in reasons)
    assert any("set_bottom_line" in reason for reason in reasons)

    state.report.sections[0].findings[0].answers = ["q1"]
    rejected = set_bottom_line(state, "Concentration is 15.0%.", "Proceed.", ["f1"])
    assert not rejected.accepted and "15.0%" in rejected.reasons[0]
    assert set_bottom_line(state, "Meridian is 11.5% of ARR.", "Proceed with conditions.", ["f1"]).accepted

    assert finalize_report(state).accepted
    assert state.report.status == "verified"
    assert [issue.code for issue in state.report.issues] == ["STALE_SOURCES"]


class RejectReport:
    def __init__(self) -> None:
        self.calls = 0

    async def review(self, report, source_notes=None):
        self.calls += 1
        return ReportReview(accepted=False, reasons=["Findings f1 and f2 disagree."])


@pytest.mark.asyncio
async def test_report_review_rejections_are_capped_and_surfaced() -> None:
    state = make_state()
    service = FindingService(state, Approve())
    finding = text_finding(
        "Meridian is 11.5% of ARR.",
        cite("qoe", "Meridian Property Group) represented $2.1M or 11.5% of ARR"),
        answers=["q1"],
    )
    assert (await service.create("section-revenue", finding)).accepted
    other = text_finding(
        "Harborview is 7.7% of ARR.",
        cite("qoe", "Harborview Commercial Partners) $1.4M or 7.7%"),
        id="f2",
        label="Harborview",
    )
    assert (await service.create("section-ai", other)).accepted
    assert set_bottom_line(state, "Meridian is 11.5% of ARR.", "Proceed.", ["f1"]).accepted

    reviewer = RejectReport()
    ctx = SimpleNamespace(deps=SimpleNamespace(state=state, report_reviewer=reviewer))
    from finance_agent.analysis.agent import MAX_REPORT_REVIEW_REJECTIONS

    for _ in range(MAX_REPORT_REVIEW_REJECTIONS):
        assert not (await finalize_tool(ctx)).accepted
    assert state.report.sections[0].status == "partial"  # nothing marked complete while rejected
    final = await finalize_tool(ctx)
    assert final.accepted and reviewer.calls == MAX_REPORT_REVIEW_REJECTIONS
    assert [issue.code for issue in state.report.issues] == ["REPORT_REVIEW_UNRESOLVED"]


def test_only_the_orchestrator_can_finalize() -> None:
    assert finalize_tool not in WORKER_TOOLS
    assert finalize_tool in ORCHESTRATOR_TOOLS


def classification(**overrides) -> SourceClassification:
    values = dict(
        source_id="s1",
        relevance=Relevance.HIGH,
        sensitivity=Sensitivity.CONFIDENTIAL,
        recommended_include=True,
        rationale="Relevant.",
    )
    values.update(overrides)
    return SourceClassification(**values)


def test_self_declared_superseded_document_is_flagged_but_kept() -> None:
    [result] = enforce_conservative_inclusion(
        [classification()],
        {"s1": "*** SUPERSEDED - replaced by the May 2026 data pack. ***\nARR 17.4"},
    )
    assert result.recommended_include
    assert [flag.kind for flag in result.flags] == [SourceFlagKind.SUPERSEDED]


def test_different_entity_document_may_be_excluded() -> None:
    [result] = enforce_conservative_inclusion(
        [
            classification(
                recommended_include=False,
                exclusion_basis=ExclusionBasis.DIFFERENT_ENTITY,
                flags=[SourceFlag(kind=SourceFlagKind.DIFFERENT_ENTITY, note="CIM for another company.")],
            )
        ],
        {"s1": "Project Sequoia CIM"},
    )
    assert not result.recommended_include


def test_unflagged_different_entity_exclusion_is_overridden() -> None:
    [result] = enforce_conservative_inclusion(
        [classification(recommended_include=False, exclusion_basis=ExclusionBasis.DIFFERENT_ENTITY)],
        {"s1": "Something"},
    )
    assert result.recommended_include


@pytest.mark.asyncio
async def test_triage_retries_skipped_sources_then_includes_leftovers() -> None:
    from finance_agent.ingestion.classifier import PydanticSourceClassifier
    from finance_agent.ingestion.models import ClassificationBatch, IntakeSession, IntakeStatus, SourceFile

    sources = [
        SourceFile(id=f"s{i}", name=f"f{i}.txt", relative_path=f"f{i}.txt", file_type="TXT", size_bytes=1,
                   relevance=Relevance.LOW, relevance_reason="x", sensitivity="x", readable=True, included=False)
        for i in range(3)
    ]
    session = IntakeSession(id="i", company_name="Co", archive_name="a.zip", archive_size_bytes=1,
                            created_at=datetime.now(timezone.utc), status=IntakeStatus.AWAITING_APPROVAL, sources=sources)
    calls = []

    class FakeAgent:
        async def run(self, prompt, deps):
            ids = [source.id for source in deps.session.sources]
            calls.append(ids)
            # First answer skips s2 and duplicates s0; later answers never return s2.
            files = [classification(source_id=i) for i in ids if i != "s2"] + [classification(source_id="s0")]
            return SimpleNamespace(output=ClassificationBatch(files=files))

    classifier = PydanticSourceClassifier.__new__(PydanticSourceClassifier)
    classifier._agent = FakeAgent()
    result = await classifier.classify(session, {s.id: f"text {s.id}" for s in sources})
    assert [item.source_id for item in result] == ["s0", "s1", "s2"]
    assert calls == [["s0", "s1", "s2"], ["s2"], ["s2"]]
    assert result[2].recommended_include and "no classification" in result[2].rationale


def test_only_blocking_review_issues_reject() -> None:
    from finance_agent.analysis.models import ReviewIssue

    minor = FindingReview(issues=[ReviewIssue(blocking=False, problem="Could quote the threshold.")])
    blocking = FindingReview(
        issues=[
            ReviewIssue(blocking=False, problem="Wording."),
            ReviewIssue(blocking=True, problem="11.5% is Meridian's whole account, not the addendum."),
        ]
    )
    assert minor.accepted and minor.reasons == ["Minor (not required): Could quote the threshold."]
    assert not blocking.accepted and blocking.reasons[0].startswith("Blocking: 11.5%")



def test_unsupported_model_flags_are_dropped_and_other_codenames_flagged() -> None:
    contract = classification(
        flags=[SourceFlag(kind=SourceFlagKind.DIFFERENT_ENTITY, note="About Meridian, a customer.")]
    )
    report = classification(
        source_id="s2", flags=[SourceFlag(kind=SourceFlagKind.SUPERSEDED, note="Draft.")]
    )
    other_deal = classification(source_id="s3")
    results = enforce_conservative_inclusion(
        [contract, report, other_deal],
        {
            "s1": "REDWOOD SOFTWARE INC. CONTRACT ABSTRACT. Customer: Meridian Property Group",
            "s2": "PROJECT REDWOOD - QUALITY OF EARNINGS (DRAFT v0.3)",
            "s3": "PROJECT SEQUOIA - CONFIDENTIAL INFORMATION MEMORANDUM. Sequoia Health Systems",
        },
        subject_name="Project Redwood",
    )
    assert [r.flags for r in results[:2]] == [[], []]
    assert [f.kind for f in results[2].flags] == [SourceFlagKind.DIFFERENT_ENTITY]


@pytest.mark.asyncio
async def test_rejection_is_reverified_before_it_stands() -> None:
    from finance_agent.analysis.models import ReviewIssue
    from finance_agent.analysis.review import PydanticFindingReviewer

    class Fixed:
        def __init__(self, review):
            self.review, self.prompts = review, []

        async def run(self, prompt):
            self.prompts.append(prompt)
            return SimpleNamespace(output=self.review)

    reviewer = PydanticFindingReviewer.__new__(PydanticFindingReviewer)
    reviewer._agent = Fixed(FindingReview(issues=[ReviewIssue(blocking=True, problem="19.3% stated as 19%.")]))
    reviewer._verifier = Fixed(FindingReview(issues=[ReviewIssue(blocking=False, problem="Rounding.")]))
    finding = text_finding("Discounts averaged 19%.", cite("qoe", "the largest customer (Meridian Property Group)"))

    result = await reviewer.review(finding)

    assert result.accepted
    assert "19.3% stated as 19%" in reviewer._verifier.prompts[0]


@pytest.mark.asyncio
async def test_answer_labels_are_normalized_to_question_ids() -> None:
    state = make_state()
    finding = text_finding(
        "Meridian is 11.5% of ARR.",
        cite("qoe", "Meridian Property Group) represented $2.1M or 11.5% of ARR"),
        answers=["q1: Is the ARR story credible?"],
    )
    result = await FindingService(state, Approve()).create("section-revenue", finding)
    assert result.accepted, result.reasons
    assert state.report.sections[0].findings[0].answers == ["q1"]


def test_parenthesised_negatives_match_signed_inputs() -> None:
    from finance_agent.analysis.grounding import NumberToken, number_matches

    library = SourceLibrary({"kpi": KPI})
    assert number_matches(NumberToken(raw="-0.9", value=Decimal("-0.9"), unit=None), library.numbers("kpi"))


@pytest.mark.asyncio
async def test_numbered_findings_are_ordered_at_finalize() -> None:
    state = make_state()
    service = FindingService(state, Approve())
    for idx, label in [("f3", "Risk 3: Harborview"), ("f1", "Risk 1: Meridian"), ("f2", "Risk 2: Harborview")]:
        quote = "Meridian Property Group) represented $2.1M or 11.5% of ARR"
        assert (await service.create("section-ai", text_finding("Meridian is 11.5% of ARR.", cite("qoe", quote), id=idx, label=label))).accepted
    assert (await service.create("section-revenue", text_finding(
        "Meridian is 11.5% of ARR.", cite("qoe", "Meridian Property Group) represented $2.1M or 11.5% of ARR"), id="f0", label="Concentration", answers=["q1"]))).accepted
    assert set_bottom_line(state, "Meridian is 11.5% of ARR.", "Proceed.", ["f0"]).accepted
    assert finalize_report(state).accepted
    assert [f.id for f in state.report.sections[1].findings] == ["f1", "f2", "f3"]


@pytest.mark.asyncio
async def test_restating_a_finding_under_a_new_id_is_rejected() -> None:
    state = make_state()
    service = FindingService(state, Approve())
    quote = "Meridian Property Group) represented $2.1M or 11.5% of ARR"
    first = text_finding("Meridian is 11.5% of ARR.", cite("qoe", quote), id="a", label="Risk 1: Meridian pilot could fail")
    again = text_finding("Meridian is 11.5% of ARR.", cite("qoe", quote), id="b", label="Risk 1: Meridian pilot plus churn")
    other = text_finding("Meridian is 11.5% of ARR.", cite("qoe", quote), id="c", label="Risk 2: Concentration")
    assert (await service.create("section-ai", first)).accepted
    result = await service.create("section-ai", again)
    assert not result.accepted and "resubmit with id 'a'" in result.reasons[0]
    assert (await service.create("section-ai", other)).accepted
    revised = text_finding("Meridian is 11.5% of ARR.", cite("qoe", quote), id="a", label="Risk 1: Meridian pilot plus churn")
    assert (await service.create("section-ai", revised)).accepted


@pytest.mark.asyncio
async def test_a_finding_may_restate_a_figure_another_finding_established() -> None:
    state = make_state()
    service = FindingService(state, Approve())
    quote = "Meridian Property Group) represented $2.1M or 11.5% of ARR"
    assert (await service.create("section-revenue", text_finding("Meridian is 11.5% of ARR.", cite("qoe", quote), id="a"))).accepted
    summary = text_finding(
        "Risk 1: losing Meridian (11.5% of ARR) matters.",
        cite("kpi", "FY25 total 3.10 2.60 (0.90)"),
        id="b",
        label="Risk 1: Meridian",
    )
    assert (await service.create("section-ai", summary)).accepted
