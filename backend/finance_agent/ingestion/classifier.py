"""AI agent that owns source-review recommendations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from pydantic_ai import Agent, RunContext

from finance_agent.prompts import load_prompt

from .models import (
    ClassificationBatch,
    ExclusionBasis,
    IntakeSession,
    Relevance,
    Sensitivity,
    SourceClassification,
    SourceFlag,
    SourceFlagKind,
)


class SourceClassifier(Protocol):
    async def classify(
        self, session: IntakeSession, previews: dict[str, str]
    ) -> list[SourceClassification]: ...


class PydanticSourceClassifier:
    """Give one agent the data-room triage goal and tools to inspect sources."""

    def __init__(self, model: str) -> None:
        self._agent = Agent(
            model,
            deps_type=IntakeTriageDependencies,
            output_type=ClassificationBatch,
            instructions=load_prompt("intake"),
            tools=[list_sources, read_source_preview],
            retries=3,
        )

    async def classify(
        self, session: IntakeSession, previews: dict[str, str]
    ) -> list[SourceClassification]:
        readable = [source for source in session.sources if source.readable]
        classifications: list[SourceClassification] = []
        batch_size = 30
        for offset in range(0, len(readable), batch_size):
            batch = readable[offset : offset + batch_size]
            scoped_session = session.model_copy(update={"sources": batch})
            scoped_previews = {
                source.id: previews[source.id]
                for source in batch
                if source.id in previews
            }
            batch_number = offset // batch_size + 1
            batch_count = (len(readable) + batch_size - 1) // batch_size
            batch_classifications = await self._classify_batch(
                scoped_session, scoped_previews, batch_number, batch_count
            )
            classifications.extend(
                enforce_conservative_inclusion(
                    batch_classifications, scoped_previews, subject_name=session.company_name
                )
            )
        return classifications

    async def _classify_batch(
        self,
        session: IntakeSession,
        previews: dict[str, str],
        batch_number: int,
        batch_count: int,
    ) -> list[SourceClassification]:
        """Classify one batch, re-asking for any sources the model skipped or duplicated.

        Sources still unclassified after the retries are included by default and say so, so a
        flaky model answer never fails the whole upload or silently drops a file.
        """
        by_id: dict[str, SourceClassification] = {}
        remaining = list(session.sources)
        for attempt in range(1, MAX_TRIAGE_ATTEMPTS + 1):
            scoped = session.model_copy(update={"sources": remaining})
            prompt = (
                f"Inspect batch {batch_number} of {batch_count} and return every listed source "
                "exactly once. Prefer consolidated evidence over duplicative raw records; "
                "individual contracts should usually be excluded when consolidated customer "
                "or financial evidence is available."
            )
            if attempt > 1:
                prompt += f" These {len(remaining)} sources were missing from the previous answer."
            result = await self._agent.run(
                prompt,
                deps=IntakeTriageDependencies(
                    session=scoped,
                    previews={source.id: previews[source.id] for source in remaining if source.id in previews},
                ),
            )
            wanted = {source.id for source in remaining}
            for item in result.output.files:
                if item.source_id in wanted and item.source_id not in by_id:
                    by_id[item.source_id] = item
            remaining = [source for source in remaining if source.id not in by_id]
            if not remaining:
                break
        for source in remaining:
            by_id[source.id] = SourceClassification(
                source_id=source.id,
                relevance=Relevance.MEDIUM,
                sensitivity=Sensitivity.CONFIDENTIAL,
                recommended_include=True,
                rationale=(
                    f"Triage returned no classification after {MAX_TRIAGE_ATTEMPTS} attempts; "
                    "included by default. Review before approving."
                ),
            )
        return [by_id[source.id] for source in session.sources]


MAX_TRIAGE_ATTEMPTS = 3


def enforce_conservative_inclusion(
    classifications: list[SourceClassification],
    previews: dict[str, str],
    *,
    subject_name: str | None = None,
) -> list[SourceClassification]:
    """Prevent uncertain or merely overlapping evidence from being silently dropped.

    AI triage ranks the corpus, but it is not allowed to exclude relevant readable
    evidence. Exact duplicate detection is deterministic so the model cannot call
    two complementary documents "duplicative."
    """
    normalized_previews: dict[str, str] = {
        source_id: " ".join(preview.casefold().split())
        for source_id, preview in previews.items()
    }
    first_source_by_content: dict[str, str] = {}
    exact_duplicates: set[str] = set()
    for source_id, content in normalized_previews.items():
        if not content:
            continue
        if content in first_source_by_content:
            exact_duplicates.add(source_id)
        else:
            first_source_by_content[content] = source_id

    guarded: list[SourceClassification] = []
    for item in classifications:
        preview = previews.get(item.source_id, "")
        item = _vet_model_flags(item, preview, subject_name)
        item = _flag_self_declared_supersession(item, preview)
        item = _flag_other_codename(item, preview, subject_name)
        if item.source_id in exact_duplicates:
            guarded.append(
                item.model_copy(
                    update={
                        "recommended_include": False,
                        "exclusion_basis": ExclusionBasis.DUPLICATE,
                        "rationale": "Exact content duplicate of another readable source.",
                    }
                )
            )
            continue
        allowed_exclusion = (
            item.exclusion_basis == ExclusionBasis.DIFFERENT_ENTITY
            and any(flag.kind == SourceFlagKind.DIFFERENT_ENTITY for flag in item.flags)
        ) or (
            item.exclusion_basis == ExclusionBasis.IRRELEVANT
            and item.relevance == Relevance.LOW
        ) or (
            item.exclusion_basis == ExclusionBasis.SENSITIVITY
            and item.sensitivity in {Sensitivity.RESTRICTED, Sensitivity.MNPI}
        )
        if not item.recommended_include and not allowed_exclusion:
            guarded.append(
                item.model_copy(
                    update={
                        "recommended_include": True,
                        "exclusion_basis": None,
                        "rationale": (
                            f"{item.rationale} Retained by the recall-first policy because "
                            "the proposed exclusion was not objectively supported."
                        )[:300],
                    }
                )
            )
            continue
        guarded.append(item)
    return guarded


@dataclass(frozen=True)
class IntakeTriageDependencies:
    session: IntakeSession
    previews: dict[str, str]


def list_sources(ctx: RunContext[IntakeTriageDependencies]) -> dict:
    """List source metadata in the uploaded data room."""
    session = ctx.deps.session
    return {
        "company_name": session.company_name,
        "user_context": session.user_context,
        "sources": [
            {
                "source_id": source.id,
                "name": source.name,
                "path": source.relative_path,
                "type": source.file_type,
                "size_bytes": source.size_bytes,
                "readable": source.readable,
                "exclusion_reason": source.exclusion_reason,
            }
            for source in session.sources
        ],
    }


def read_source_preview(
    ctx: RunContext[IntakeTriageDependencies], source_id: str
) -> str:
    """Read a compact locally extracted preview for one source."""
    if source_id not in ctx.deps.previews:
        return (
            "Preview unavailable: the source_id is unknown or unreadable. "
            "Call list_sources and retry with an exact readable source_id."
        )
    return ctx.deps.previews[source_id]


_SUPERSEDED = re.compile(r"\b(superseded|replaced by|no longer current|do not rely)\b", re.IGNORECASE)


def _flag_self_declared_supersession(
    item: SourceClassification, preview: str
) -> SourceClassification:
    """Flag a document whose own header says it is superseded, whatever the model returned."""
    if any(flag.kind == SourceFlagKind.SUPERSEDED for flag in item.flags):
        return item
    header = preview[:600]
    match = _SUPERSEDED.search(header)
    if not match:
        return item
    line = next(
        (candidate.strip(" *") for candidate in header.splitlines() if match.group(0) in candidate),
        match.group(0),
    )
    return item.model_copy(
        update={
            "flags": [
                *item.flags,
                SourceFlag(kind=SourceFlagKind.SUPERSEDED, note=f"Document header: {line}"[:200]),
            ]
        }
    )


_GENERIC_NAME_WORDS = {"project", "inc", "llc", "ltd", "corp", "company", "group", "holdings", "the", "and"}
_SUPERSESSION_LANGUAGE = re.compile(
    r"supersed|replaced by|newer version|no longer current|outdated",
    re.IGNORECASE,
)
_CODENAME = re.compile(r"\bproject ([a-z]{3,})\b", re.IGNORECASE)


def _subject_tokens(subject_name: str | None) -> set[str]:
    words = re.findall(r"[A-Za-z]{4,}", subject_name or "")
    return {word.casefold() for word in words if word.casefold() not in _GENERIC_NAME_WORDS}


def _mentions_subject(preview: str, subject_name: str | None) -> bool:
    tokens = _subject_tokens(subject_name)
    if not tokens:
        return True
    text = preview.casefold()
    return any(re.search(rf"\b{re.escape(token)}\b", text) for token in tokens)


def _vet_model_flags(
    item: SourceClassification, preview: str, subject_name: str | None
) -> SourceClassification:
    """Drop model flags the file's own text does not support.

    In testing the triage model flagged customer contracts as "different entity" (they name the
    customer) and current reports as "superseded". A wrong flag tells the report agent to
    distrust good evidence, so flags must be corroborated by the preview.
    """
    kept = []
    for flag in item.flags:
        if (
            flag.kind == SourceFlagKind.DIFFERENT_ENTITY
            and _subject_tokens(subject_name)
            and _mentions_subject(preview, subject_name)
        ):
            continue
        if flag.kind == SourceFlagKind.SUPERSEDED and not _SUPERSESSION_LANGUAGE.search(preview[:600]):
            continue
        kept.append(flag)
    if len(kept) == len(item.flags):
        return item
    update: dict = {"flags": kept}
    if item.exclusion_basis == ExclusionBasis.DIFFERENT_ENTITY and not any(
        flag.kind == SourceFlagKind.DIFFERENT_ENTITY for flag in kept
    ):
        update.update({"recommended_include": True, "exclusion_basis": None})
    return item.model_copy(update=update)


def _flag_other_codename(
    item: SourceClassification, preview: str, subject_name: str | None
) -> SourceClassification:
    """Flag a document titled with another deal codename that never mentions the subject."""
    if any(flag.kind == SourceFlagKind.DIFFERENT_ENTITY for flag in item.flags):
        return item
    match = _CODENAME.search(preview[:400])
    if not match or match.group(1).casefold() in _subject_tokens(subject_name):
        return item
    if _mentions_subject(preview, subject_name):
        return item
    return item.model_copy(
        update={
            "flags": [
                *item.flags,
                SourceFlag(
                    kind=SourceFlagKind.DIFFERENT_ENTITY,
                    note=f"Titled 'Project {match.group(1).title()}' and never mentions {subject_name}.",
                ),
            ]
        }
    )
