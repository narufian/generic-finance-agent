# Analysis workflow

## Purpose

The analysis agent is responsible for producing a valid one-pager from a human-approved set of files and user context.

## Structure

One orchestrator owns delivery of the report. It decides how to investigate the approved data and may spawn subagents to offload context-heavy work. Delegation is dynamic agent behavior, not a fixed agent-per-section pipeline.

The orchestrator first calls `define_report_outline`. The analyst's instructions determine the section titles, order, and emphasis; the UI is a generic renderer over the resulting ordered sections and typed findings.

```text
Approved files and user context
              ↓
        Orchestrator agent
        ↙       ↓        ↘
  Subagent   Tool use   Subagent
        ↘       ↓        ↙
         Reviewed findings
              ↓
        Validated one-pager
```

Subagents receive only the task and source context they need. They return compact results rather than their full working history.

## Tools

The orchestrator and its subagents may use:

- `glob`, `grep`, and `read` to inspect approved files;
- `write`, `edit`, and shell execution inside a scratch workspace;
- `spawn_subagent` for context-heavy delegated work;
- `define_report_outline` to establish the report structure from the analyst's request;
- `create_finding` as the only way to add or change report findings;
- `inspect_report` to read current report state; and
- `finalize_report` to validate and complete the one-pager.

Approved source files are read-only. Scratch files are writable. Direct file edits cannot mutate the report.

## Finding creation and review

`create_finding` validates every proposed finding before it enters the report. Cheap deterministic checks run first, so the model reviewer only sees findings that are mechanically grounded:

1. Validate the finding schema and that it cites only approved sources.
2. Structural limits: text findings are capped at 600 characters (one claim per finding), categorical findings need a rationale, and `answers` may reference only defined brief questions.
3. Calculated values must equal the recorded code-execution output.
4. Each citation excerpt must occur in its cited source. Matching tolerates light edits (whitespace, punctuation, a dropped word, table rows quoted without their headers) but every number in the excerpt must occur in the matched stretch of the source.
5. Every number in a narrative finding must occur in its cited sources, or, for non-reported findings, in a recorded calculation output. Calculation inputs must occur in the sources their citations point to.
6. A model reviewer judges meaning. It receives the finding plus the source text around each citation (not just the agent's chosen excerpt), so it can catch a figure attributed to the wrong entity or a dropped qualifier.
7. Persist the finding only when every check passes; otherwise return structured reasons.

A finding rejected three times under the same section and label is blocked and recorded as a `FINDING_UNVERIFIED` issue on the report, so a claim cannot be resubmitted until a stochastic reviewer happens to pass it.

Code-execution calls and their outputs are recorded as artifacts. Numerical findings reference the relevant artifacts so their values can be reproduced and checked without relying on agent memory.

## Brief questions and the bottom line

`define_report_outline` takes, for each section, the explicit questions from the analyst's request. They are stored on the report as `questions`; findings link to them through `answers`. `set_bottom_line` stores a short summary and recommendation that must reference existing findings, and every number in it must appear in those findings or a recorded output.

## Execution environment

PydanticAI manages the agent loop and typed tools. Anthropic provides the model. E2B provides one isolated workspace per analysis run.

Each E2B workspace contains:

- a read-only copy of the approved files;
- a writable scratch directory;
- the standard file and shell tools; and
- no report write access except through `create_finding`.

Anthropic and E2B credentials are provided through local environment variables and are never committed.

## Completion

Only the orchestrator can call `finalize_report`. It runs in this order and changes nothing until the last step:

1. Deterministic completeness: every section has a finding, every brief question has an answering finding, and a bottom line exists.
2. Whole-report review by a separate model (`FINANCE_AGENT_REPORT_REVIEW_MODEL`, defaulting to the orchestrator's model): contradictions between findings, reversed comparisons, unanswered questions, reliance on flagged sources. After two rejections the report completes anyway, with the unresolved points attached as `REPORT_REVIEW_UNRESOLVED` issues, so the job cannot loop forever.
3. A `STALE_SOURCES` issue is added when the newest approved source is more than 120 days older than the report.
4. Sections are marked complete or unavailable and the report becomes verified.

## POC boundaries

- No database; report and tool-call state are stored locally.
- No fixed sequence of specialist agents.
- No unrestricted host code execution.
- No production queue, retries, or durable orchestration yet.

## Local API integration

`POST /api/intakes/{intake-id}/report-jobs` starts Step 2 only after approval. It first persists a queued job, then an in-process worker seeds E2B with the approved snapshot and runs the orchestrator independently of the browser request. The UI polls `GET /api/intakes/{intake-id}/report-jobs/{job-id}` and can reconnect to an active job after refresh. Queued, running, completed, and failed states are stored under the intake's local `jobs/` directory.

At source approval, the user can opt into Haiku for the per-finding reviewer and rejection verifier. This is persisted on the intake and affects only finding-level judgment; deterministic grounding and the whole-report review remain unchanged.

The same approval step selects Sonnet or Opus for the report orchestrator. That choice is persisted per intake and controls report planning, delegation, and drafting. The final whole-report reviewer remains on the separately configured Sonnet model so generation and final challenge stay independent.

A completed job stores two durable files under the intake's local `reports/` directory: the verified one-pager JSON and a run record containing every recorded calculation artifact plus every deterministic or model review decision and its proposed finding. The original synchronous report endpoint remains available for compatibility and tests.

Each job also persists a structured event timeline while it runs. The timeline records public execution facts—tool starts and completions, subagent delegation, calculation execution, finding-review outcomes, and final job status. Safe tool metadata may identify the file, search, calculation, or finding being handled, but prompts, source contents, calculation code, and private model reasoning are excluded. The generation screen shows recent low-level activity while it is live; once a higher-level milestone arrives, those events remain available in a collapsed disclosure beneath the milestone. Refresh and reconnect preserve the same timeline.

`GET /api/profiles` scans persisted intake, job, and report records for the homepage. Reopening a generating row reconnects polling to its active job. Reopening another row restores its saved source-review state and, when present, its latest one-pager; users can move between those views without rerunning ingestion or analysis.

Each company profile has a stable customer-facing route at `/report/{profile-id}`. The SPA resolves the profile through `GET /api/profiles/{profile-id}` and opens the latest available workflow step: source review before generation, live progress for an active job, or the latest completed one-pager. Direct navigation and refresh require no query-string state.

The lightweight POC data room has been exercised through this flow with live Anthropic and E2B calls. The verified run persisted two calculation artifacts, 15 finding-review decisions (11 accepted and four rejected), and 68 structured events before reopening successfully from both its stable profile URL and the homepage.

