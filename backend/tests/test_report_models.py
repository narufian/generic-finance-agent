import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from finance_agent.reports import OnePager


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_report.json"


@pytest.fixture
def report_data() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def find_finding(report_data: dict, finding_id: str) -> dict:
    return next(
        finding
        for section in report_data["sections"]
        for finding in section["findings"]
        if finding["id"] == finding_id
    )


def test_fixture_round_trips_as_json(report_data: dict) -> None:
    report = OnePager.model_validate(report_data)

    serialized = json.loads(report.model_dump_json())

    assert serialized == report_data
    assert report.company_name == "Redwood Systems"
    assert len(report.sections) == 5


def test_available_finding_requires_citation(report_data: dict) -> None:
    finding = find_finding(report_data, "finding-business-model")
    finding["citations"] = []
    finding["evidence_strength"]["source_count"] = 0

    with pytest.raises(ValidationError, match="at least one citation"):
        OnePager.model_validate(report_data)


def test_calculated_finding_requires_calculation(report_data: dict) -> None:
    finding = find_finding(report_data, "finding-net-leverage")
    finding["calculation"] = None

    with pytest.raises(ValidationError, match="require a calculation"):
        OnePager.model_validate(report_data)


def test_calculation_result_must_match_finding(report_data: dict) -> None:
    finding = find_finding(report_data, "finding-net-leverage")
    finding["calculation"]["result"] = "4.0"

    with pytest.raises(ValidationError, match="must equal the finding value"):
        OnePager.model_validate(report_data)


def test_estimated_finding_requires_assumption(report_data: dict) -> None:
    finding = find_finding(report_data, "finding-arr")
    finding["assumptions"] = []

    with pytest.raises(ValidationError, match="at least one assumption"):
        OnePager.model_validate(report_data)


def test_unavailable_numeric_finding_has_no_value_fields(report_data: dict) -> None:
    finding = find_finding(report_data, "finding-valuation")
    finding["value"] = "9.5"

    with pytest.raises(ValidationError, match="must not contain value fields"):
        OnePager.model_validate(report_data)


def test_report_rejects_unapproved_citation(report_data: dict) -> None:
    mutated = deepcopy(report_data)
    finding = find_finding(mutated, "finding-business-model")
    finding["citations"][0]["source_id"] = "source-not-approved"

    with pytest.raises(ValidationError, match="only approved sources"):
        OnePager.model_validate(mutated)


def test_categorical_value_must_belong_to_options(report_data: dict) -> None:
    finding = find_finding(report_data, "finding-ai-native")
    finding["value"] = "Exceptional"

    with pytest.raises(ValidationError, match="belong to its declared options"):
        OnePager.model_validate(report_data)


def test_conflict_flag_must_match_conflicts(report_data: dict) -> None:
    finding = find_finding(report_data, "finding-arr")
    finding["evidence_strength"]["has_conflicts"] = False

    with pytest.raises(ValidationError, match="conflict flag"):
        OnePager.model_validate(report_data)

