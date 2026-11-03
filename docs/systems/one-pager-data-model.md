# One-pager data model

## Purpose

The report domain defines the stable contract between the analysis workflow and its one-pager UI. It describes what the backend produces without prescribing how documents are parsed or which AI model produces the analysis.

## Boundary

This system owns:

- one-pagers and their ordered sections;
- evidence-backed findings;
- citations, calculations, assumptions, and conflicts;
- evidence-strength summaries; and
- analysis issues that affect a report, source, section, or finding.

It does not own uploaded files, document parsing, agent prompts, model-provider configuration, job execution, or UI layout.

## Aggregate

```text
OnePager
├── ReportSection[]
│   └── Finding[]
│       ├── EvidenceStrength
│       ├── Citation[]
│       ├── Assumption[]
│       ├── Conflict[]
│       └── Calculation?       # number findings only
└── AnalysisIssue[]
```

`OnePager` is the aggregate root. Findings are embedded in sections because they are authored, verified, and presented in section context.

## Sections

A section is a domain concept as well as a UI grouping. Before creating findings, the report orchestrator defines an ordered outline from the analyst's request. Each section receives a stable, agent-defined `section_type` slug and presentation `title`; the application does not prescribe a fixed list of analytical questions.

Sections have explicit order and completion status. The UI iterates over the persisted section and finding models, so a request for revenue quality, key risks, or another question changes the rendered report without requiring a new hardcoded component.

## Brief questions and bottom line

`questions` lists the explicit questions from the analyst's request, each tied to a section. A finding's `answers` names the questions it helps answer; finalization requires every question to be answered. `bottom_line` holds a summary (at most 700 characters), a recommendation (at most 400) and the IDs of the supporting findings. Both fields are optional on the model so reports saved before they existed still load; finalization enforces them for new reports.

Categorical findings carry an optional `rationale` (at most 400 characters) shown next to the verdict; `create_finding` requires it for available verdicts.

## Findings

A finding is one evidence-backed answer within a section. Its type describes the shape of its value:

| Type | Value | Example |
| --- | --- | --- |
| `text` | prose | The company sells workflow software through annual contracts. |
| `number` | numeric value, unit, and optional period | ARR is $84 million. |
| `categorical` | member of a defined set | AI-native maturity is Moderate. |

A finding's status independently describes how the answer was established:

- `reported`: stated directly by a source;
- `calculated`: derived deterministically from cited inputs;
- `estimated`: inferred using explicit assumptions;
- `assessed`: an evidence-backed model judgment, displayed to users as **AI assessment**; or
- `unavailable`: the approved evidence does not support an answer.

This separation is intentional. For example, an AI-native result is `categorical` with status `assessed`, while ARR may be a `number` with status `reported`, `calculated`, or `estimated`.

## Evidence strength

Evidence strength is user-facing and categorical: `strong`, `moderate`, or `limited`. It is not presented as a model probability.

The summary records:

- whether the evidence is direct, derived, or inferred;
- the number of distinct supporting sources;
- whether material conflicts exist;
- the newest relevant source date; and
- short reasons for the classification.

The UI may show only the level on the one-pager and expose the reasons in the evidence drawer.

## Citations and calculations

Every available finding must have at least one citation. A citation identifies a source and a precise location such as a page, section, sheet, or cell range.

A calculated numeric finding must include:

- the recorded code-execution artifact and named output used;
- a human-readable expression;
- named inputs with values and units;
- a result matching the finding value; and
- methodology describing the choice of formula and periods.

Arithmetic is performed and checked by application code, not delegated to an AI model.

## Assumptions and conflicts

Estimated findings require at least one explicit assumption. Conflicts belong to the affected finding and identify disagreements in the evidence. The evidence-strength conflict flag must agree with the finding's conflict list.

## Issues and errors

Operational and analytical issues are not findings. `AnalysisIssue` represents scoped information, warnings, and errors using a stable machine-readable code plus a user-facing message.

Examples include:

- `SOURCE_UNREADABLE`
- `CONFLICTING_VALUES`
- `INSUFFICIENT_EVIDENCE`
- `MODEL_TIMEOUT`

Issues may target the whole report or a specific source, section, or finding. Recoverable issues may also provide a suggested user action.

## Invariants

- IDs are unique within a one-pager.
- Section order values are unique.
- Available findings contain at least one citation.
- Unavailable findings contain no value.
- Calculated numeric findings contain a calculation whose result equals the finding value.
- Estimated findings contain an assumption.
- Assessed findings describe their methodology.
- Citation source count and conflict state agree with the evidence-strength summary.
- Categorical values belong to their declared options.
- Generation timestamps include a timezone.

## Persistence for the POC

There is no database. Pydantic models serialize to ordinary JSON files. Model IDs and `source_snapshot_id` are retained in the contract so local JSON can later move to relational and object storage without changing the frontend payload.

## Deferred decisions

- User-defined report templates and section types
- Currency conversion policy
- Time-series findings
- Evidence-strength scoring thresholds
- Report and finding revision history
- Database schema and migrations

