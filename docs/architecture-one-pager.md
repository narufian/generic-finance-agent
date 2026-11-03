# Generic Finance Agent — architecture

**Purpose.** Turn a company name, analyst instructions, and a ZIP data room into an evidence-backed investment-committee brief. The analyst's request determines the report structure; the web UI renders the resulting ordered sections and typed findings rather than imposing a fixed template.

```text
Company + instructions + ZIP
            │
            ▼
 STEP 1 — AI source triage      Validate and extract → agent proposes source set
            │                 → deterministic safety checks → human approval
            │
            ▼ approved, immutable source snapshot
 STEP 2 — Report agent         Orchestrator ↔ scoped subagents ↔ sandboxed tools
            │                 → reviewed findings → whole-report QA
            ▼
 Persisted report + evidence + calculations + reviews + execution events
```

## Step 1 — file triage and approval

1. `POST /api/intakes` accepts the company name, optional analyst instructions, and one ZIP. The service enforces archive limits, blocks path traversal and links, safely extracts supported files, and creates a local intake record.
2. A lightweight PydanticAI intake agent receives the whole data-room triage goal. Using only `list_sources` and `read_source_preview`, it chooses what to inspect and returns structured relevance, sensitivity, inclusion, date, and warning metadata for every file.
3. Deterministic policy is layered over the model: exclusions need a typed reason; duplicates require content equality; self-declared superseded files are flagged; uncertain exclusions fail open. This favors recall so a plausible source is not silently lost.
4. The user can amend the proposal and approve it. `POST /api/intakes/{id}/approve` persists the exact approved source IDs and creates the immutable boundary for Step 2. The report agent cannot read outside that snapshot.

## Step 2 — agent-directed analysis

`POST /api/intakes/{id}/report-jobs` first persists a queued job, then starts an in-process background worker. The browser polls job state, so refreshes reconnect without restarting analysis.

A PydanticAI orchestrator owns report delivery. It receives the analyst instructions and approved files in an isolated E2B workspace, defines a 3–8 section outline, and may delegate context-heavy tasks to a worker subagent. Sections are not hardcoded: the questions and emphasis in the analyst request shape the stored report and therefore the rendered UI.

The report lifecycle is represented by controlled state transitions:

`define_report_outline` → `create_finding` → `inspect_report` → `set_bottom_line` → `finalize_report`

Only these typed tools mutate report state. Filesystem edits cannot. A successful run persists the report, calculation artifacts, every finding-review decision, and a customer-safe event timeline; private model reasoning is never stored or displayed.

## Agent toolkits

| Actor | Toolkit | Boundary |
|---|---|---|
| Intake agent | `list_sources`, `read_source_preview` | Recommends the source set; cannot start analysis |
| Orchestrator | `glob_files`, `grep_files`, `read_file`; scratch `write`/`replace`; sandboxed `run_code`; all five report lifecycle tools; `delegate_task` | Owns outline, conclusion, and finalization |
| Worker subagent | Same file, scratch, calculation, `create_finding`, and `inspect_report` tools | Context-isolated contribution; cannot reshape or finalize the report |

Approved sources are read-only, scratch space is writable, and flexible Python execution occurs inside E2B rather than on the host machine.

## QA and anti-hallucination controls

- **Typed data contracts:** findings are narrative, numeric, or categorical, with independent reported/calculated/estimated/AI-assessment status.
- **Citation enforcement:** every available finding cites only approved sources and identifies a precise location. Quoted evidence must match the underlying source text.
- **Numeric grounding:** every visible number must exist in cited evidence or a recorded calculation output. Calculated values must equal the referenced artifact result, and calculation inputs must be present in their cited sources.
- **Semantic review:** a separate lightweight model reviews each proposal against surrounding source context, catching wrong entities, periods, qualifiers, and unsupported interpretations. Rejections are returned to the orchestrator; repeated failures are blocked and surfaced as issues.
- **Whole-report review:** finalization checks section and question coverage, bottom-line support, contradictions, reversed comparisons, source warnings, and staleness. Missing evidence becomes an explicit unavailable finding—never an invented answer.

## Local API surface

| API | Role |
|---|---|
| `GET /api/profiles` / `GET /api/profiles/{id}` | List and reopen persisted work at the latest available step |
| `DELETE /api/profiles/{id}` / `POST /api/profiles/{id}/restore` | Recoverable local deletion and undo |
| `POST /api/intakes` / `GET /api/intakes/{id}` | Run triage and retrieve the persisted source manifest |
| `POST /api/intakes/{id}/approve` | Freeze the approved source snapshot |
| `POST /api/intakes/{id}/report-jobs` | Start or reuse a background report job |
| `GET /api/intakes/{id}/report-jobs/{jobId}` | Poll status and structured execution events |
| `GET /api/intakes/{id}/reports/{reportId}` | Retrieve the verified one-pager |

**POC stack and boundary.** FastAPI + local filesystem persistence, PydanticAI + Anthropic, and E2B. Jobs survive browser refreshes but not a server restart; production would replace the in-process worker and local storage with a durable queue and database/object store.
