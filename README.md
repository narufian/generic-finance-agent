# Generic Finance Agent

Generic Finance Agent is a proof of concept for producing evidence-backed company one-pagers from a human-approved set of source documents.

Upload a ZIP data room, approve the sources an AI triage step proposes, and an orchestrator agent writes a report whose every claim is checked against quoted source text and recorded calculations. See `docs/architecture-one-pager.md`.

The web app is served from `web/` and backed by the Python services under `backend/`.

## Local development

The frontend is served by the same process as the local API. Using `just`:

```bash
just dev       # foreground with auto-reload
just start     # background
just status
just logs
just stop
```

Or run the combined web app and API directly:

```bash
uv run uvicorn finance_agent.api:app --app-dir backend --host 127.0.0.1 --port 3000
```

Install the backend dependencies and run the model tests:

```bash
uv sync --dev
uv run pytest
```

The representative serialized report is at `backend/tests/fixtures/sample_report.json`.

The agent scaffold uses PydanticAI (Anthropic by default; any PydanticAI provider via the `FINANCE_AGENT_*_MODEL` settings), delegates bounded work to a context-isolated worker, and runs file and code tools in E2B. Copy `.env.example` to `.env` and add local credentials before the first live run. Live agent construction loads that file automatically; shell exports take precedence.

Report generation runs as a persisted in-process background job. The UI polls job state and reconnects after a browser refresh; restarting the local server does not resume an interrupted job in this POC.

