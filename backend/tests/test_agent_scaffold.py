import pytest
from pydantic_ai.models.test import TestModel

from finance_agent.analysis.agent import build_agent_bundle, tool_event_detail
from finance_agent.analysis.workspace import E2BAnalysisWorkspace


def test_agent_bundle_builds_without_live_credentials() -> None:
    bundle = build_agent_bundle(TestModel())

    assert bundle.orchestrator.name == "report_orchestrator"
    assert bundle.worker.name == "analysis_worker"


def test_tool_event_detail_exposes_safe_context_not_contents() -> None:
    assert tool_event_detail(
        "read_file", {"path": "/home/user/work/sources/source-1/qoe.txt"}
    ) == 'read_file("/home/user/work/sources/source-1/qoe.txt")'
    assert tool_event_detail(
        "create_finding", {"finding": {"label": "Recurring ARR", "value": 17.4}}
    ) == 'create_finding("Recurring ARR", <finding payload hidden>)'
    assert tool_event_detail("run_code", {"code": "secret source content"}) == (
        "run_code(<calculation code hidden>)"
    )


@pytest.mark.parametrize(
    "path",
    [
        "../sources/approved.pdf",
        "/home/user/work/scratch/../../sources/approved.pdf",
        "/etc/passwd",
    ],
)
def test_scratch_writes_reject_path_escape(path: str) -> None:
    with pytest.raises(ValueError, match="scratch directory"):
        E2BAnalysisWorkspace._checked_scratch_path(path)

