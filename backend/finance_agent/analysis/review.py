"""Model-based review of proposed findings and of the assembled report.

Deterministic grounding (see grounding.py) runs first. The model reviewers judge meaning:
whether the claim is what the source says, in context.
"""

from __future__ import annotations

import json
from typing import Protocol, Sequence

from pydantic_ai import Agent

from finance_agent.reports.models import Finding, OnePager
from finance_agent.prompts import load_prompt

from .grounding import CitationContext
from .models import FindingReview, ReportReview


REVIEW_INSTRUCTIONS = load_prompt("finding-review")
REPORT_REVIEW_INSTRUCTIONS = load_prompt("report-review")
VERIFY_INSTRUCTIONS = load_prompt("finding-verifier")


class FindingReviewer(Protocol):
    async def review(
        self,
        finding: Finding,
        contexts: Sequence[CitationContext] = (),
        calculations: Sequence[dict] = (),
    ) -> FindingReview: ...


class ReportReviewer(Protocol):
    async def review(
        self, report: OnePager, source_notes: dict[str, str] | None = None
    ) -> ReportReview: ...


def finding_review_prompt(
    finding: Finding, contexts: Sequence[CitationContext], calculations: Sequence[dict] = ()
) -> str:
    payload = {
        "finding": finding.model_dump(mode="json"),
        "source_context": [
            {
                "citation_id": context.citation_id,
                "source_id": context.source_id,
                "context": context.context,
            }
            for context in contexts
            if context.context
        ],
        "recorded_calculations": list(calculations),
    }
    return json.dumps(payload, ensure_ascii=False)


def report_review_prompt(report: OnePager, source_notes: dict[str, str] | None) -> str:
    payload = {
        "report": report.model_dump(mode="json"),
        "source_triage_warnings": source_notes or {},
    }
    return json.dumps(payload, ensure_ascii=False)


class PydanticFindingReviewer:
    """Review a finding; a rejection is re-checked once by a verifier before it stands.

    In testing a single review rejected many correct findings over rounding, section labels or
    facts the reviewer misread. The second pass only sees the claimed blocking issues and must
    confirm each one against the evidence, which cuts false rejections without letting a
    confirmed error through.
    """

    def __init__(self, model: str) -> None:
        self._agent = Agent(
            model,
            output_type=FindingReview,
            retries=3,
            instructions=REVIEW_INSTRUCTIONS,
        )
        self._verifier = Agent(
            model,
            output_type=FindingReview,
            retries=3,
            instructions=VERIFY_INSTRUCTIONS,
        )

    async def review(
        self,
        finding: Finding,
        contexts: Sequence[CitationContext] = (),
        calculations: Sequence[dict] = (),
    ) -> FindingReview:
        packet = finding_review_prompt(finding, contexts, calculations)
        first = (await self._agent.run(packet)).output
        if first.accepted:
            return first
        claimed = [reason for reason in first.reasons if reason.startswith("Blocking")] or first.reasons
        second = (
            await self._verifier.run(
                packet + "\n\nClaimed blocking issues:\n" + "\n".join(f"- {r}" for r in claimed)
            )
        ).output
        return second


class PydanticReportReviewer:
    def __init__(self, model: str) -> None:
        self._agent = Agent(
            model,
            output_type=ReportReview,
            retries=3,
            instructions=REPORT_REVIEW_INSTRUCTIONS,
        )

    async def review(
        self, report: OnePager, source_notes: dict[str, str] | None = None
    ) -> ReportReview:
        result = await self._agent.run(report_review_prompt(report, source_notes))
        return result.output
