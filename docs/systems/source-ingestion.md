# Source ingestion

## Purpose

Source ingestion implements Steps 1 and 1.5: accept a company name, optional context, and one ZIP; create a file manifest; then stop until a human approves the exact sources the analysis agent may use.

## Flow

```text
Company + context + ZIP
          ↓
Validate and safely extract
          ↓
Intake agent inspects the data room
          ↓
Persist local intake + proposed source manifest
          ↓
Human includes/excludes files and confirms
          ↓
Immutable approved source snapshot ID
```

The report agent is not started by ingestion. Approval is an explicit API call and is the boundary between ingestion and analysis.

One lightweight intake agent owns the high-level triage goal. It receives tools to list sources and read compact, locally extracted previews, chooses its own inspection sequence, and returns one structured recommendation set. The application does not prescribe an agent or model call per file.

## Local data

POC state is stored under `data/intakes/<intake-id>/` and ignored by Git. Each intake contains a `session.json` manifest and a `sources/` directory. Only readable files are extracted; unsupported and encrypted entries remain visible in the manifest but cannot be approved.

## Limits

- ZIP only, at most 100 MB.
- At most 250 files and 500 MB expanded content.
- Absolute paths, parent traversal, and symbolic links are rejected.
- Readable types: PDF, DOCX, PPTX, XLSX, XLSM, CSV, and TXT.
- Restricted or MNPI-looking files are excluded by default but may be deliberately selected if readable.

## API

- `POST /api/intakes` accepts multipart company data and the ZIP and returns the review manifest.
- `GET /api/intakes/{id}` returns persisted intake state.
- `POST /api/intakes/{id}/approve` records the selected readable source IDs and returns an approved snapshot ID.

Relevance and sensitivity begin as AI classifications grounded in extracted file content. Inclusion is deliberately recall-first. Every proposed exclusion must carry a typed basis: clearly low relevance, restricted/MNPI sensitivity, duplication, or a document about a different company. Duplicate exclusion requires deterministic content equality; corroborating or overlapping documents are retained. Unsupported exclusions fail open and remain included. The human may still override any readable file, but correctness does not depend on careful manual review.

Triage also returns, per source, a `content_date` read from the document's content (not the ZIP timestamp) and `flags` for two hazards:

- `different_entity`: the document is about another company, such as a CIM for a different deal. Only a flagged file may be excluded on this basis.
- `superseded`: the document is replaced by a newer one. It stays included for audit trail, but the flag is shown on the review screen and passed to the report agent as a triage warning. A document whose own header says it is superseded is flagged deterministically, whatever the model returned.

## Fast local fixture

`backend/tests/fixtures/lightweight_dataroom.zip` is a sub-2 KB synthetic company data room covering the business model, ARR, financials, debt, AI-product evidence, and one restricted file. Its readable source files sit beside it under `lightweight_dataroom/`; rebuild the ZIP with `python scripts/build_lightweight_fixture.py` after editing them.
