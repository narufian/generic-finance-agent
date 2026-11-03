"""Deterministic grounding checks that run before any model review.

These checks do not judge meaning. They catch two cheap-to-detect failure modes:

* an excerpt that does not occur in the cited source (a misquote or invented quote); and
* a number in a finding that occurs in neither the cited sources nor a recorded calculation
  (an invented or mis-transcribed figure).

They also return the source text around each citation so the model reviewer can judge the
claim against its context instead of only against the excerpt the agent chose.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable

from finance_agent.reports.models import (
    CategoricalFinding,
    Citation,
    Finding,
    FindingStatus,
    NumericFinding,
    TextFinding,
)


CONTEXT_CHARS = 1_200
MIN_FRAGMENT_TOKENS = 3
EXCERPT_COVERAGE = 0.8
"""Share of an excerpt's words that must occur, close together, in the cited source.

Agents reformat table rows and trim words, so exact matching rejects honest excerpts. Every
number in an excerpt must still occur in that stretch of the source.
"""
_TOKEN = re.compile(r"\d+(?:[.,]\d+)*|[^\W\d_]+")

_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…|\[\.\.\.\]|\[…\])\s*")
_TRANSLATE = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        " ": " ",
    }
)

# A number with optional currency prefix, thousands separators, decimals and a unit suffix.
_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.\-/])"  # not part of an identifier such as RW-C-0083, Q4, v3.2, FY25
    r"(?P<approx>[~≈])?"
    r"(?P<cur>\$)?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<unit>\s?(?:%|x|bn|m|M|k|K|million|billion|thousand)(?![A-Za-z]))?",
    re.IGNORECASE,
)
_YEAR = re.compile(r"^(19|20)\d{2}$")


def tokenize(text: str) -> tuple[list[str], list[int]]:
    """Lower-cased words and numbers (thousands separators removed) with their offsets."""
    tokens: list[str] = []
    offsets: list[int] = []
    for match in _TOKEN.finditer(text.translate(_TRANSLATE).casefold()):
        token = match.group(0)
        if token[0].isdigit():
            token = token.replace(",", "")
        tokens.append(token)
        offsets.append(match.start())
    return tokens, offsets


def _best_window(source: list[str], fragment: list[str]) -> int | None:
    """Start index of a source window that contains the fragment, allowing light edits.

    A window of about 1.5x the fragment's length must contain at least EXCERPT_COVERAGE of the
    fragment's tokens (as a multiset) and every one of its numbers.
    """
    if not fragment or not source:
        return None
    wanted = Counter(fragment)
    numbers = {token for token in fragment if token[0].isdigit()}
    size = min(len(source), int(len(fragment) * 1.5) + 3)
    needed = EXCERPT_COVERAGE * len(fragment)
    window: Counter[str] = Counter(source[:size])
    covered = sum(min(count, window[token]) for token, count in wanted.items())
    best: tuple[int, int] | None = None
    for start in range(0, len(source) - size + 1):
        if start:
            outgoing, incoming = source[start - 1], source[start + size - 1]
            if outgoing in wanted and window[outgoing] <= wanted[outgoing]:
                covered -= 1
            window[outgoing] -= 1
            window[incoming] += 1
            if incoming in wanted and window[incoming] <= wanted[incoming]:
                covered += 1
        if covered >= needed and all(window[number] > 0 for number in numbers):
            if best is None or covered > best[1]:
                best = (start, covered)
                if covered == len(fragment):
                    break
    return best[0] if best else None


def _table_window(source: list[str], fragment: list[str]) -> int | None:
    """Looser match for excerpts rebuilt from table rows ("Dec-25: Expansion $2.05M").

    Every number must occur within a short stretch of the source that also contains at least
    half of the excerpt's words; column headers are often too far away for the strict match.
    """
    numbers = [token for token in fragment if token[0].isdigit()]
    if len(numbers) < 1:
        return None
    words = [token for token in fragment if not token[0].isdigit()]
    size = min(len(source), max(60, 3 * len(fragment)))
    for start in range(0, max(1, len(source) - size + 1), max(1, size // 4)):
        window = set(source[start : start + size])
        if all(number in window for number in numbers):
            if not words or sum(word in window for word in words) >= 0.5 * len(words):
                return start
    return None


@dataclass(frozen=True)
class CitationContext:
    citation_id: str
    source_id: str
    excerpt_found: bool | None
    """None when the source text is not available to check."""
    context: str | None


class SourceLibrary:
    """Full extracted text of the approved sources, keyed by source ID."""

    def __init__(self, texts: dict[str, str]) -> None:
        self._texts = {source_id: text for source_id, text in texts.items() if text.strip()}
        self._token_cache: dict[str, tuple[list[str], list[int]]] = {}
        self._numbers: dict[str, set[Decimal]] = {}

    def __bool__(self) -> bool:
        return bool(self._texts)

    def has(self, source_id: str) -> bool:
        return source_id in self._texts

    def locate(self, citation: Citation) -> CitationContext:
        text = self._texts.get(citation.source_id)
        if text is None:
            return CitationContext(citation.id, citation.source_id, None, None)
        if not citation.excerpt:
            return CitationContext(citation.id, citation.source_id, None, text[: CONTEXT_CHARS * 2])

        tokens, offsets = self._tokens(citation.source_id)
        fragments = [
            fragment_tokens
            for fragment_tokens in (
                tokenize(fragment)[0] for fragment in _ELLIPSIS.split(citation.excerpt)
            )
            if len(fragment_tokens) >= MIN_FRAGMENT_TOKENS
        ] or [tokenize(citation.excerpt)[0]]

        first_hit: int | None = None
        for fragment in fragments:
            hit = _best_window(tokens, fragment)
            if hit is None:
                hit = _table_window(tokens, fragment)
            if hit is None:
                return CitationContext(citation.id, citation.source_id, False, None)
            if first_hit is None:
                first_hit = hit
        assert first_hit is not None
        original = offsets[first_hit] if offsets else 0
        start = max(0, original - CONTEXT_CHARS)
        end = min(len(text), original + CONTEXT_CHARS + len(citation.excerpt))
        return CitationContext(citation.id, citation.source_id, True, text[start:end])

    def _tokens(self, source_id: str) -> tuple[list[str], list[int]]:
        if source_id not in self._token_cache:
            self._token_cache[source_id] = tokenize(self._texts[source_id])
        return self._token_cache[source_id]

    def numbers(self, source_id: str) -> set[Decimal]:
        if source_id not in self._numbers:
            self._numbers[source_id] = _number_set(self._texts.get(source_id, ""))
        return self._numbers[source_id]


@dataclass(frozen=True)
class NumberToken:
    raw: str
    value: Decimal
    unit: str | None


def extract_numbers(text: str) -> list[NumberToken]:
    """Material numbers in prose. Years, bare list ordinals and identifiers are skipped."""
    tokens: list[NumberToken] = []
    for match in _NUMBER.finditer(text.translate(_TRANSLATE)):
        digits = match.group("num").replace(",", "")
        unit = (match.group("unit") or "").strip() or None
        has_currency = bool(match.group("cur"))
        try:
            value = Decimal(digits)
        except InvalidOperation:
            continue
        if not unit and not has_currency:
            if _YEAR.match(digits):
                continue
            if "." not in digits and value < 13:
                continue  # list ordinals, "RISK 1", small counts are left to the model reviewer
        tokens.append(NumberToken(raw=match.group(0).strip(), value=value, unit=unit))
    return tokens


def _scaled(value: Decimal, unit: str | None) -> set[Decimal]:
    variants = {value}
    normalized = unit.casefold() if unit else None
    if normalized in {"m", "million"}:
        variants.add(value * Decimal(1_000_000))
    elif normalized in {"bn", "billion"}:
        variants.add(value * Decimal(1_000_000_000))
    elif normalized in {"k", "thousand"}:
        variants.add(value * Decimal(1_000))
    return variants


def _number_set(text: str) -> set[Decimal]:
    values: set[Decimal] = set()
    for match in _NUMBER.finditer(text.translate(_TRANSLATE)):
        try:
            value = Decimal(match.group("num").replace(",", ""))
        except InvalidOperation:
            continue
        unit = (match.group("unit") or "").strip() or None
        values |= _scaled(value, unit)
        if unit == "%":
            values.add(value / Decimal(100))
        # Values like 2,100,000 should also match "$2.1M".
        if value >= 1_000_000:
            values.add(value / Decimal(1_000_000))
        elif value >= 1_000:
            values.add(value / Decimal(1_000))
    return values


def number_matches(token: NumberToken, candidates: Iterable[Decimal]) -> bool:
    wanted = _scaled(token.value, token.unit)
    exponent = token.value.as_tuple().exponent
    places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    quantum = Decimal(1).scaleb(-places)
    wanted |= {-value for value in wanted}  # accounts write negatives as (0.9)
    for candidate in candidates:
        if candidate in wanted:
            return True
        # Allow a value stated at lower precision than its source (70.8 -> 71, 41.7 -> 42).
        try:
            if candidate.quantize(quantum) == token.value:
                return True
        except InvalidOperation:
            continue
    return False


def artifact_numbers(outputs: Iterable[object]) -> set[Decimal]:
    values: set[Decimal] = set()

    def visit(item: object) -> None:
        if isinstance(item, bool):
            return
        if isinstance(item, (int, float, Decimal)):
            values.add(Decimal(str(item)))
        elif isinstance(item, str):
            try:
                values.add(Decimal(item))
            except InvalidOperation:
                values.update(_number_set(item))
        elif isinstance(item, dict):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)

    for output in outputs:
        visit(output)
    return values


def finding_prose(finding: Finding) -> str:
    """The visible narrative of a finding whose numbers must be grounded."""
    if isinstance(finding, TextFinding):
        return finding.value or ""
    if isinstance(finding, CategoricalFinding):
        return " ".join(part for part in (finding.value, finding.rationale) if part)
    return ""


def ungrounded_numbers(
    finding: Finding,
    library: SourceLibrary,
    calculation_values: set[Decimal],
    verified_values: set[Decimal] = frozenset(),
) -> list[str]:
    """Numbers in the finding's prose found in neither its cited sources nor recorded outputs.

    Reported findings may use only their cited sources; other statuses may also use the
    outputs of recorded calculations. Any finding may restate a figure that an already accepted
    finding established (a risk summary repeating the covenant headroom, for example).
    """
    prose = finding_prose(finding)
    if not prose:
        return []
    cited = {citation.source_id for citation in finding.citations if library.has(citation.source_id)}
    if not cited:
        return []
    candidates: set[Decimal] = set()
    for source_id in cited:
        candidates |= library.numbers(source_id)
    if finding.status != FindingStatus.REPORTED:
        candidates |= calculation_values
    candidates |= set(verified_values)
    missing = [token.raw for token in extract_numbers(prose) if not number_matches(token, candidates)]
    return list(dict.fromkeys(missing))


def ungrounded_calculation_inputs(finding: Finding, library: SourceLibrary) -> list[str]:
    """Calculation inputs whose value does not occur in any of their cited sources."""
    if not isinstance(finding, NumericFinding) or finding.calculation is None:
        return []
    source_by_citation = {citation.id: citation.source_id for citation in finding.citations}
    problems: list[str] = []
    for item in finding.calculation.inputs:
        sources = {
            source_by_citation[citation_id]
            for citation_id in item.citation_ids
            if citation_id in source_by_citation and library.has(source_by_citation[citation_id])
        }
        if not sources:
            continue
        candidates: set[Decimal] = set()
        for source_id in sources:
            candidates |= library.numbers(source_id)
        token = NumberToken(raw=str(item.value), value=item.value, unit=None)
        if not number_matches(token, candidates):
            problems.append(f"{item.name} = {item.value} {item.unit}")
    return problems
