"""Application services behind the analysis agent's typed tools."""

from __future__ import annotations

import hashlib
from difflib import SequenceMatcher
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from finance_agent.reports.models import (
    AnalysisIssue,
    BottomLine,
    CategoricalFinding,
    Finding,
    FindingStatus,
    IssueScope,
    IssueSeverity,
    NumericFinding,
    ReportStatus,
    SectionStatus,
    TextFinding,
)

from .grounding import (
    artifact_numbers,
    extract_numbers,
    finding_prose,
    ungrounded_calculation_inputs,
    number_matches,
    ungrounded_numbers,
)
from .models import (
    BottomLineResult,
    FinalizationResult,
    FindingCreationResult,
    FindingReviewRecord,
    RunEventKind,
    RunEventStatus,
)
from .review import FindingReviewer
from .state import AnalysisState


MAX_TEXT_FINDING_CHARS = 600
"""Long narrative findings hide unchecked claims; split them into atomic findings instead."""

MAX_FINDING_ATTEMPTS = 3
"""Rejected submissions allowed per finding before it is blocked, so review is not retried until it passes."""

STALE_SOURCE_DAYS = 120

_ORDINAL = re.compile(r"^(?:[A-Za-z]+\s+)?#?(\d+)[:.)]\s")


def _attempt_key(section_id: str, finding: Finding) -> str:
    return f"{section_id}::{' '.join(finding.label.casefold().split())}"


class FindingService:
    def __init__(self, state: AnalysisState, reviewer: FindingReviewer) -> None:
        self._state = state
        self._reviewer = reviewer

    async def create(self, section_id: str, finding: Finding) -> FindingCreationResult:
        key = _attempt_key(section_id, finding)
        previous_rejections = self._state.rejections.get(key, 0)
        if previous_rejections >= MAX_FINDING_ATTEMPTS:
            return FindingCreationResult(
                accepted=False,
                finding_id=finding.id,
                reasons=[
                    f"This finding was rejected {previous_rejections} times and is now blocked. "
                    "Do not resubmit it. Either submit a narrower claim under a different label "
                    "that the evidence clearly supports, or record it as unavailable and name the "
                    "gap."
                ],
            )

        deterministic = self._deterministic_reasons(section_id, finding)
        if deterministic:
            return self._reject(section_id, finding, deterministic, "deterministic", key)

        contexts = [self._state.sources.locate(citation) for citation in finding.citations]
        review = await self._reviewer.review(finding, contexts, self._relevant_calculations(finding))
        if not review.accepted:
            return self._reject(section_id, finding, review.reasons, "model", key)

        try:
            self._state.upsert_finding(section_id, finding)
        except ValueError as error:
            return self._reject(section_id, finding, [str(error)], "deterministic", key)
        result = FindingCreationResult(accepted=True, finding_id=finding.id)
        self._record(section_id, finding, result, "model", previous_rejections + 1)
        return result

    def _deterministic_reasons(self, section_id: str, finding: Finding) -> list[str]:
        approved_sources = set(self._state.report.approved_source_ids)
        cited_sources = {citation.source_id for citation in finding.citations}
        if not cited_sources.issubset(approved_sources):
            return ["Finding cites a source outside the approved snapshot."]

        reasons: list[str] = []
        if isinstance(finding, TextFinding) and finding.value and len(finding.value) > MAX_TEXT_FINDING_CHARS:
            reasons.append(
                f"Text findings are limited to {MAX_TEXT_FINDING_CHARS} characters "
                f"(this one has {len(finding.value)}). Split it into separate findings, one claim "
                "each, so every claim is reviewed on its own."
            )
        if (
            isinstance(finding, CategoricalFinding)
            and finding.status != FindingStatus.UNAVAILABLE
            and not finding.rationale
        ):
            reasons.append("Categorical findings need a one-line rationale for the chosen option.")
        duplicate = self._near_duplicate(section_id, finding)
        if duplicate is not None:
            return [
                f"Section already has a finding on this ({duplicate.id}: '{duplicate.label}'). "
                f"To revise it, resubmit with id '{duplicate.id}'; otherwise make a different claim."
            ]
        question_ids = {question.id for question in self._state.report.questions}
        # Agents often echo the outline's "q2: question text" label; keep the bare ID.
        finding.answers = [
            match.group(1) if (match := re.match(r"\s*(q\d+)\b", answer, re.IGNORECASE)) else answer
            for answer in finding.answers
        ]
        finding.answers = [answer.lower() if re.fullmatch(r"Q\d+", answer) else answer for answer in finding.answers]
        unknown_questions = [qid for qid in finding.answers if qid not in question_ids]
        if unknown_questions:
            reasons.append(f"Unknown question IDs in answers: {', '.join(unknown_questions)}.")
        if reasons:
            return reasons

        grounding_error = self._validate_numeric_grounding(finding)
        if grounding_error:
            return [grounding_error]

        library = self._state.sources
        if not library:
            return []
        missing_excerpts = [
            context.citation_id
            for context in (library.locate(citation) for citation in finding.citations)
            if context.excerpt_found is False
        ]
        if missing_excerpts:
            reasons.append(
                "These citation excerpts do not occur in their cited source: "
                f"{', '.join(missing_excerpts)}. Quote the source exactly (use '...' between "
                "non-adjacent passages) or cite the correct source."
            )
        missing_numbers = ungrounded_numbers(
            finding,
            library,
            artifact_numbers(a.outputs for a in self._state.artifacts.values()),
            verified_values=self._accepted_values(exclude=finding.id),
        )
        if missing_numbers:
            scope = (
                "its cited sources"
                if finding.status == FindingStatus.REPORTED
                else "its cited sources or any recorded calculation output"
            )
            reasons.append(
                f"These numbers appear in neither {scope}: {', '.join(missing_numbers)}. "
                "Cite the source that states them, compute them with run_code, or remove them."
            )
        bad_inputs = ungrounded_calculation_inputs(finding, library)
        if bad_inputs:
            reasons.append(
                "These calculation inputs do not occur in the sources their citations point to: "
                f"{'; '.join(bad_inputs)}."
            )
        return reasons

    def _accepted_values(self, *, exclude: str) -> set[Decimal]:
        """Numbers already established by accepted findings (prose, values and calculations)."""
        values: list[object] = []
        for section in self._state.report.sections:
            for existing in section.findings:
                if existing.id == exclude:
                    continue
                values.append(finding_prose(existing))
                if isinstance(existing, NumericFinding):
                    values.extend([existing.value, existing.display_value])
        return artifact_numbers(values)

    def _near_duplicate(self, section_id: str, finding: Finding) -> Finding | None:
        """An existing finding in the section that this one restates under a new ID.

        In full runs the agent resubmitted revised findings under fresh IDs, so the report showed
        the same fact twice ("Risk 1: ..." twice, covenant headroom twice).
        """
        section = next((s for s in self._state.report.sections if s.id == section_id), None)
        if section is None:
            return None
        label = finding.label.casefold()
        ordinal = _ORDINAL.match(finding.label)
        for existing in section.findings:
            if existing.id == finding.id:
                continue
            other = _ORDINAL.match(existing.label)
            if ordinal is not None and other is not None:
                if other.group(1) == ordinal.group(1):  # "Risk 1" twice
                    return existing
                continue  # differently numbered items are distinct by construction
            if SequenceMatcher(None, label, existing.label.casefold()).ratio() >= 0.65:
                return existing
        return None

    def _relevant_calculations(self, finding: Finding) -> list[dict]:
        """Recorded outputs the finding relies on, so the reviewer can see how they were computed."""
        wanted: dict[str, dict] = {}
        if isinstance(finding, NumericFinding) and finding.calculation is not None:
            artifact = self._state.artifacts.get(finding.calculation.artifact_id)
            if artifact is not None:
                wanted[artifact.id] = {finding.calculation.output_name: artifact.outputs.get(finding.calculation.output_name)}
        if finding.status != FindingStatus.REPORTED:
            tokens = extract_numbers(finding_prose(finding))
            for artifact in self._state.artifacts.values():
                for name, value in artifact.outputs.items():
                    values = artifact_numbers([value])
                    if any(number_matches(token, values) for token in tokens):
                        wanted.setdefault(artifact.id, {})[name] = value
        return [
            {
                "artifact_id": artifact_id,
                "outputs": outputs,
                "code": self._state.artifacts[artifact_id].code[:4000],
            }
            for artifact_id, outputs in wanted.items()
        ]

    def _reject(
        self,
        section_id: str,
        finding: Finding,
        reasons: list[str],
        decision_type: str,
        key: str,
    ) -> FindingCreationResult:
        attempts = self._state.rejections.get(key, 0) + 1
        self._state.rejections[key] = attempts
        reasons = list(reasons)
        if attempts >= MAX_FINDING_ATTEMPTS:
            reasons.append(
                f"Attempt {attempts} of {MAX_FINDING_ATTEMPTS}: this finding is now blocked and "
                "has been recorded as an unverified claim on the report."
            )
            self._state.add_issue(
                AnalysisIssue(
                    id=f"issue-unverified-{hashlib.sha1(key.encode()).hexdigest()[:12]}",
                    severity=IssueSeverity.WARNING,
                    code="FINDING_UNVERIFIED",
                    message=(
                        f"Could not verify '{finding.label}' after {attempts} attempts: "
                        + "; ".join(reasons[:-1])
                    )[:1000],
                    scope_type=IssueScope.SECTION,
                    scope_id=section_id,
                    recoverable=True,
                    user_action="Check this claim against the sources before relying on it.",
                )
            )
        else:
            reasons.append(f"Attempt {attempts} of {MAX_FINDING_ATTEMPTS}.")
        result = FindingCreationResult(accepted=False, finding_id=finding.id, reasons=reasons)
        self._record(section_id, finding, result, decision_type, attempts)
        return result

    def _record(
        self,
        section_id: str,
        finding: Finding,
        result: FindingCreationResult,
        decision_type: str,
        attempt: int,
    ) -> None:
        self._state.record_review(
            FindingReviewRecord(
                section_id=section_id,
                finding_id=finding.id,
                accepted=result.accepted,
                reasons=result.reasons,
                decision_type=decision_type,
                attempt=attempt,
                reviewed_at=datetime.now(timezone.utc),
                proposed_finding=finding.model_dump(mode="json"),
            )
        )
        detail = "; ".join(result.reasons) if result.reasons else finding.label
        self._state.emit_event(
            RunEventKind.REVIEW,
            RunEventStatus.COMPLETED if result.accepted else RunEventStatus.WARNING,
            f"Finding {'accepted' if result.accepted else 'returned for revision'}: {finding.label}",
            detail=detail[:500],
            tool_name="create_finding",
        )

    def _validate_numeric_grounding(self, finding: Finding) -> str | None:
        if not isinstance(finding, NumericFinding) or finding.calculation is None:
            return None

        calculation = finding.calculation
        artifact = self._state.artifacts.get(calculation.artifact_id)
        if artifact is None:
            return "Calculation references an unknown execution artifact."
        if artifact.exit_code != 0:
            return "Calculation references a failed execution artifact."
        if calculation.output_name not in artifact.outputs:
            available = ", ".join(sorted(artifact.outputs)) or "none (print one JSON object as the last line)"
            return (
                f"Calculation output '{calculation.output_name}' is missing from artifact "
                f"{artifact.id}. Recorded outputs: {available}."
            )
        try:
            recorded_value = Decimal(str(artifact.outputs[calculation.output_name]))
        except (InvalidOperation, ValueError):
            return "Recorded calculation output is not numeric."
        if recorded_value != finding.value or recorded_value != calculation.result:
            return "Finding value does not match the recorded calculation output."
        return None


def set_bottom_line(
    state: AnalysisState, summary: str, recommendation: str, finding_ids: list[str]
) -> BottomLineResult:
    """Validate and store the report's bottom line."""
    findings = {
        finding.id: finding
        for section in state.report.sections
        for finding in section.findings
    }
    unknown = [finding_id for finding_id in finding_ids if finding_id not in findings]
    if unknown:
        return BottomLineResult(
            accepted=False, reasons=[f"Unknown finding IDs: {', '.join(unknown)}."]
        )
    try:
        bottom_line = BottomLine(
            summary=summary, recommendation=recommendation, finding_ids=finding_ids
        )
    except ValueError as error:
        return BottomLineResult(accepted=False, reasons=[str(error)])

    # Every number in the bottom line must come from a referenced finding or a recorded output.
    candidates = artifact_numbers(a.outputs for a in state.artifacts.values())
    for finding_id in finding_ids:
        finding = findings[finding_id]
        candidates |= artifact_numbers([finding_prose(finding)])
        if isinstance(finding, NumericFinding):
            candidates |= artifact_numbers([finding.value, finding.display_value])
    missing = [
        token.raw
        for token in extract_numbers(f"{summary} {recommendation}")
        if not number_matches(token, candidates)
    ]
    if missing:
        return BottomLineResult(
            accepted=False,
            reasons=[
                "These numbers do not appear in the referenced findings: "
                f"{', '.join(dict.fromkeys(missing))}. Reference the finding that states each one."
            ],
        )
    state.report.bottom_line = bottom_line
    return BottomLineResult(accepted=True)


def check_finalization(state: AnalysisState) -> FinalizationResult:
    """Deterministic completeness checks. Does not change the report."""
    if not state.report.sections:
        return FinalizationResult(
            accepted=False,
            reasons=["Define the report outline before finalizing."],
        )
    reasons: list[str] = []
    empty_sections = [section.title for section in state.report.sections if not section.findings]
    if empty_sections:
        reasons.append(f"Missing findings for: {', '.join(empty_sections)}.")
    answered = {
        question_id
        for section in state.report.sections
        for finding in section.findings
        for question_id in finding.answers
    }
    unanswered = [
        f"{question.id} ({question.text})"
        for question in state.report.questions
        if question.id not in answered
    ]
    if unanswered:
        reasons.append(
            "These brief questions have no finding that answers them: "
            + "; ".join(unanswered)
            + ". Link an answering finding through its answers field, or add an unavailable "
            "finding that names the gap."
        )
    if state.report.bottom_line is None:
        reasons.append("Call set_bottom_line before finalizing.")
    return FinalizationResult(accepted=not reasons, reasons=reasons)


def finalize_report(state: AnalysisState) -> FinalizationResult:
    """Run the deterministic checks and, if they pass, mark the report verified."""
    check = check_finalization(state)
    if not check.accepted:
        return check

    if state.latest_content_date is not None:
        age = (state.report.generated_at.date() - state.latest_content_date).days
        if age > STALE_SOURCE_DAYS:
            state.add_issue(
                AnalysisIssue(
                    id="issue-stale-sources",
                    severity=IssueSeverity.WARNING,
                    code="STALE_SOURCES",
                    message=(
                        f"The most recent approved source is dated "
                        f"{state.latest_content_date.isoformat()}, {age} days before this report "
                        "was generated. Facts that may have changed since then are presented as "
                        "of that date."
                    ),
                    scope_type=IssueScope.REPORT,
                    recoverable=True,
                    user_action="Refresh the data room or confirm open items before relying on the report.",
                )
            )

    for section in state.report.sections:
        # Findings submitted concurrently arrive in any order; keep "Risk 1..5" style labels in
        # their numbered order.
        numbers = [_ORDINAL.match(finding.label) for finding in section.findings]
        if len(section.findings) > 1 and all(numbers):
            section.findings.sort(key=lambda finding: int(_ORDINAL.match(finding.label).group(1)))
        if all(finding.status.value == "unavailable" for finding in section.findings):
            section.status = SectionStatus.UNAVAILABLE
        else:
            section.status = SectionStatus.COMPLETE
    state.report.status = ReportStatus.VERIFIED
    state.report = state.report.model_validate(state.report.model_dump())
    return FinalizationResult(accepted=True)
