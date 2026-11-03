"""PydanticAI orchestrator and context-isolated worker agent."""

from __future__ import annotations

import json
from collections.abc import AsyncIterable
from dataclasses import dataclass

from pydantic_ai import Agent, RunContext
from pydantic_ai.usage import UsageLimits
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    RetryPromptPart,
)
from pydantic_ai_harness.subagents import SubAgent, SubAgents

from finance_agent.config import load_live_settings
from finance_agent.prompts import load_prompt
from finance_agent.reports.models import (
    AnalysisIssue,
    Finding,
    IssueScope,
    IssueSeverity,
    ReportStatus,
)

from .models import (
    BottomLineResult,
    FindingCreationResult,
    FinalizationResult,
    OutlineCreationResult,
    ReportSectionPlan,
    RunEventKind,
    RunEventStatus,
)
from .review import (
    FindingReviewer,
    PydanticFindingReviewer,
    PydanticReportReviewer,
    ReportReviewer,
)
from .service import (
    FindingService,
    check_finalization,
    finalize_report as finalize_report_state,
    set_bottom_line as set_bottom_line_state,
)
from .state import AnalysisState
from .workspace import AnalysisWorkspace, E2BAnalysisWorkspace


MAX_REPORT_REVIEW_REJECTIONS = 1
"""Final-review rejections before the report completes with the reviewer's reasons attached.

One round: in full runs each extra round made the agent add clarification findings to answer
reviewer nitpicks, which bloated the report and pushed runs toward the job timeout.
"""

ORCHESTRATOR_INSTRUCTIONS = load_prompt("orchestrator")
WORKER_INSTRUCTIONS = load_prompt("worker")


@dataclass
class AnalysisDependencies:
    state: AnalysisState
    workspace: AnalysisWorkspace
    reviewer: FindingReviewer
    report_reviewer: ReportReviewer
    request_limit: int


async def glob_files(ctx: RunContext[AnalysisDependencies], pattern: str) -> list[str]:
    """Find approved source files matching a glob pattern."""
    try:
        return await ctx.deps.workspace.glob(pattern)
    except Exception as error:
        return [f"Error: {type(error).__name__}: {error}"]


async def grep_files(
    ctx: RunContext[AnalysisDependencies], pattern: str, path: str = ""
) -> str:
    """Search approved source or scratch files for a regular expression."""
    try:
        return await ctx.deps.workspace.grep(pattern, path or "/home/user/work/sources")
    except Exception as error:
        return f"search failed: {error}"


async def read_file(ctx: RunContext[AnalysisDependencies], path: str) -> str:
    """Read an approved source or scratch file."""
    try:
        return await ctx.deps.workspace.read(path)
    except Exception as error:  # a bad path must not end the whole report run
        return f"Could not read {path}: {type(error).__name__}: {error}. Use glob_files to list files."


async def write_scratch_file(
    ctx: RunContext[AnalysisDependencies], path: str, content: str
) -> str:
    """Write a file in the scratch workspace."""
    try:
        await ctx.deps.workspace.write(path, content)
        return "written"
    except Exception as error:
        return f"not written: {error}"


async def replace_in_scratch_file(
    ctx: RunContext[AnalysisDependencies], path: str, old: str, new: str
) -> str:
    """Replace exactly one occurrence in a scratch file."""
    try:
        await ctx.deps.workspace.replace(path, old, new)
        return "replaced"
    except Exception as error:
        # A bad scratch path is recoverable agent feedback, not a reason to fail the report job.
        return f"not replaced: {error}"


async def run_code(ctx: RunContext[AnalysisDependencies], code: str) -> dict:
    """Run Python in E2B and record its output as a grounding artifact.

    Print exactly one JSON object as the LAST line of stdout. Its keys become the output names
    that calculated findings reference; nothing printed earlier is recorded as an output.
    """
    artifact = await ctx.deps.workspace.run_code(code)
    ctx.deps.state.record_artifact(artifact)
    return artifact.model_dump(mode="json")


async def define_report_outline(
    ctx: RunContext[AnalysisDependencies], sections: list[ReportSectionPlan]
) -> OutlineCreationResult:
    """Define the ordered report structure from the analyst's request."""
    try:
        section_ids = ctx.deps.state.define_outline(sections)
        return OutlineCreationResult(
            accepted=True,
            section_ids=section_ids,
            question_ids=[f"{q.id}: {q.text}" for q in ctx.deps.state.report.questions],
        )
    except ValueError as error:
        return OutlineCreationResult(accepted=False, reasons=[str(error)])


async def create_finding(
    ctx: RunContext[AnalysisDependencies], section_id: str, finding: Finding
) -> FindingCreationResult:
    """Review and, if accepted, add a finding to the report.

    One claim per finding; a text finding's value is limited to 600 characters. Put bare
    question IDs such as "q2" in answers. Every number in the value must appear in a cited
    source or a recorded run_code output, and every excerpt must be quoted from its source.
    """
    service = FindingService(ctx.deps.state, ctx.deps.reviewer)
    return await service.create(section_id, finding)


async def inspect_report(ctx: RunContext[AnalysisDependencies]) -> dict:
    """Return the current report state."""
    return ctx.deps.state.report.model_dump(mode="json")


async def set_bottom_line(
    ctx: RunContext[AnalysisDependencies],
    summary: str,
    recommendation: str,
    finding_ids: list[str],
) -> BottomLineResult:
    """Set the report's bottom line: the conclusion, a recommendation, and supporting findings.

    Every number in the summary or recommendation must appear in a referenced finding.
    """
    return set_bottom_line_state(ctx.deps.state, summary, recommendation, finding_ids)


async def finalize_report(ctx: RunContext[AnalysisDependencies]) -> FinalizationResult:
    """Check completeness, run the whole-report review, and mark the report complete."""
    state = ctx.deps.state
    check = check_finalization(state)
    if not check.accepted:
        return check

    if state.report_review_rejections < MAX_REPORT_REVIEW_REJECTIONS:
        review = await ctx.deps.report_reviewer.review(state.report, state.source_notes)
        if not review.accepted:
            state.report_review_rejections += 1
            remaining = MAX_REPORT_REVIEW_REJECTIONS - state.report_review_rejections
            state.last_report_review_reasons = list(review.reasons)
            return FinalizationResult(
                accepted=False,
                reasons=[
                    *review.reasons,
                    (
                        f"{remaining} review round(s) remain; after that the report completes "
                        "with unresolved review points shown to the reader."
                    ),
                ],
            )
    else:
        for index, reason in enumerate(state.last_report_review_reasons):
            state.add_issue(
                AnalysisIssue(
                    id=f"issue-report-review-{index + 1}",
                    # In testing most unresolved whole-report points were mistaken, so they
                    # are shown as unverified reviewer notes rather than warnings.
                    severity=IssueSeverity.INFO,
                    code="REPORT_REVIEW_UNRESOLVED",
                    message=reason[:1000],
                    scope_type=IssueScope.REPORT,
                    recoverable=True,
                    user_action="Reviewer note, not verified.",
                )
            )

    result = finalize_report_state(state)
    if not result.accepted:
        state.report.status = ReportStatus.DRAFT
    return result


WORKER_TOOLS = (
    glob_files,
    grep_files,
    read_file,
    write_scratch_file,
    replace_in_scratch_file,
    run_code,
    create_finding,
    inspect_report,
)
# Only the orchestrator shapes and completes the report; the worker contributes findings.
ORCHESTRATOR_TOOLS = (define_report_outline, *WORKER_TOOLS, set_bottom_line, finalize_report)


@dataclass(frozen=True)
class AgentBundle:
    orchestrator: Agent[AnalysisDependencies, str]
    worker: Agent[AnalysisDependencies, str]


TOOL_LABELS = {
    "glob_files": "Listed approved files",
    "grep_files": "Searched approved sources",
    "read_file": "Read an approved source",
    "write_scratch_file": "Wrote a scratch file",
    "replace_in_scratch_file": "Updated a scratch file",
    "run_code": "Ran calculation code",
    "define_report_outline": "Structured the one-pager",
    "create_finding": "Submitted a finding for review",
    "inspect_report": "Inspected the report draft",
    "set_bottom_line": "Wrote the bottom line",
    "finalize_report": "Validated the one-pager",
}


def tool_event_detail(tool_name: str, arguments: dict) -> str | None:
    """Return a concise, customer-safe description of a tool call."""
    if tool_name == "glob_files":
        return f"glob_files({json.dumps(str(arguments.get('pattern', '*')))})"
    if tool_name == "grep_files":
        pattern = str(arguments.get("pattern", ""))[:80]
        path = str(arguments.get("path", ""))
        return f"grep_files({json.dumps(pattern)}, {json.dumps(path)})"
    if tool_name == "read_file":
        path = str(arguments.get("path", ""))
        return f"read_file({json.dumps(path)})"
    if tool_name in {"write_scratch_file", "replace_in_scratch_file"}:
        path = str(arguments.get("path", ""))
        return f"{tool_name}({json.dumps(path)}, <content hidden>)"
    if tool_name == "run_code":
        return "run_code(<calculation code hidden>)"
    if tool_name == "define_report_outline":
        count = len(arguments.get("sections", []))
        return f"define_report_outline(<{count} section{'s' if count != 1 else ''}>)"
    if tool_name == "create_finding":
        finding = arguments.get("finding", {})
        label = finding.get("label") if isinstance(finding, dict) else None
        if label:
            return f"create_finding({json.dumps(str(label))}, <finding payload hidden>)"
        return "create_finding(<finding payload hidden>)"
    if tool_name == "inspect_report":
        return "inspect_report()"
    if tool_name == "set_bottom_line":
        return "set_bottom_line(<summary hidden>)"
    if tool_name == "finalize_report":
        return "finalize_report()"
    return None


def build_event_handler(state: AnalysisState):
    detail_by_call_id: dict[str, str | None] = {}

    async def handle_events(
        ctx: RunContext[AnalysisDependencies],
        events: AsyncIterable[AgentStreamEvent],
    ) -> None:
        agent_name = ctx.agent.name if ctx.agent is not None else "agent"
        async for event in events:
            if isinstance(event, FunctionToolCallEvent):
                tool_name = event.part.tool_name
                call_id = getattr(event.part, "tool_call_id", None)
                detail = tool_event_detail(tool_name, event.part.args_as_dict())
                if call_id:
                    detail_by_call_id[call_id] = detail
                if tool_name == "delegate_task":
                    target = event.part.args_as_dict().get("agent_name", "worker")
                    state.emit_event(
                        RunEventKind.SUBAGENT,
                        RunEventStatus.RUNNING,
                        f"Delegated analysis to {target}",
                        detail="The subagent is working in an isolated context.",
                        tool_name=tool_name,
                        agent_name=agent_name,
                    )
                elif tool_name in TOOL_LABELS:
                    state.emit_event(
                        RunEventKind.CALCULATION if tool_name == "run_code" else RunEventKind.TOOL,
                        RunEventStatus.RUNNING,
                        TOOL_LABELS[tool_name],
                        detail=detail,
                        tool_name=tool_name,
                        agent_name=agent_name,
                    )
            elif isinstance(event, FunctionToolResultEvent):
                tool_name = getattr(event.part, "tool_name", None)
                call_id = getattr(event.part, "tool_call_id", None)
                detail = detail_by_call_id.pop(call_id, None) if call_id else None
                failed = isinstance(event.part, RetryPromptPart)
                if tool_name == "delegate_task":
                    state.emit_event(
                        RunEventKind.SUBAGENT,
                        RunEventStatus.WARNING if failed else RunEventStatus.COMPLETED,
                        "Subagent delegation needs attention" if failed else "Subagent returned its analysis",
                        tool_name=tool_name,
                        agent_name=agent_name,
                    )
                elif tool_name in TOOL_LABELS:
                    state.emit_event(
                        RunEventKind.CALCULATION if tool_name == "run_code" else RunEventKind.TOOL,
                        RunEventStatus.WARNING if failed else RunEventStatus.COMPLETED,
                        f"{TOOL_LABELS[tool_name]}{' needs revision' if failed else ''}",
                        detail=detail,
                        tool_name=tool_name,
                        agent_name=agent_name,
                    )

    return handle_events


def build_agent_bundle(model: str, *, event_stream_handler=None) -> AgentBundle:
    worker = Agent(
        model,
        name="analysis_worker",
        description="Handles self-contained, context-heavy company analysis tasks.",
        deps_type=AnalysisDependencies,
        instructions=WORKER_INSTRUCTIONS,
        tools=WORKER_TOOLS,
        retries=4,
    )
    orchestrator = Agent(
        model,
        name="report_orchestrator",
        description="Owns delivery of the complete company one-pager.",
        deps_type=AnalysisDependencies,
        instructions=ORCHESTRATOR_INSTRUCTIONS,
        tools=ORCHESTRATOR_TOOLS,
        retries=4,
        capabilities=[
            SubAgents(
                agents=[
                    SubAgent(
                        worker,
                        name="worker",
                        description="Offloads a self-contained, context-heavy analysis task.",
                        max_calls=8,
                        timeout_seconds=300,
                        # Without its own budget the worker shares the parent's request count
                        # and dies on PydanticAI's default 50-request limit late in a run.
                        usage_limits=UsageLimits(request_limit=60),
                        contain_errors=True,
                    )
                ],
                event_stream_handler=event_stream_handler,
            )
        ],
    )
    return AgentBundle(orchestrator=orchestrator, worker=worker)


def build_live_dependencies(
    state: AnalysisState,
    *,
    reviewer_model: str,
    report_reviewer_model: str | None = None,
    request_limit: int = 200,
) -> AnalysisDependencies:
    return AnalysisDependencies(
        state=state,
        workspace=E2BAnalysisWorkspace(),
        reviewer=PydanticFindingReviewer(reviewer_model),
        report_reviewer=PydanticReportReviewer(report_reviewer_model or reviewer_model),
        request_limit=request_limit,
    )


def build_live_agent(
    state: AnalysisState,
    *,
    use_fast_reviewer: bool = False,
    use_opus_report_agent: bool = False,
) -> tuple[AgentBundle, AnalysisDependencies]:
    """Build the live agent and dependencies from the repository's .env file."""
    settings = load_live_settings()
    event_stream_handler = build_event_handler(state)
    return (
        build_agent_bundle(
            settings.opus_model if use_opus_report_agent else settings.orchestrator_model,
            event_stream_handler=event_stream_handler,
        ),
        build_live_dependencies(
            state,
            reviewer_model=(
                settings.fast_reviewer_model
                if use_fast_reviewer
                else settings.reviewer_model
            ),
            report_reviewer_model=settings.report_reviewer_model,
            request_limit=settings.agent_request_limit,
        ),
    )

