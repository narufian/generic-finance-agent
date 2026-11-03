You own triage of an uploaded company data room for a one-page investment-bank profile. Inspect the available files with your tools. Decide your own sequence.

For every readable source, assess relevance to business model, ARR/revenue, leverage, valuation, or AI-native differentiation; classify sensitivity; recommend inclusion; and give one concise evidence-based rationale. Use file contents, not only filenames. Return every readable source exactly once.

Optimize for recall: corroborating or overlapping evidence is useful and is not a duplicate. Recommend exclusion only for clearly low-relevance material, restricted material, or content-identical duplicates. Any exclusion must set `exclusion_basis` to `irrelevant`, `sensitivity`, or `duplicate`; included sources must leave `exclusion_basis` null.

Also flag two hazards in `flags`, with a short note. `different_entity`: the document is mainly about a company other than the subject company named by `list_sources` (for example a CIM for another deal); recommend exclusion with `exclusion_basis` `different_entity`. `superseded`: the document states it was replaced, or another file in the data room is clearly a newer version of it; keep it included but flag it.

Set `content_date` to the date the document's information is as of, read from its content (report date, meeting date, or "as of" date); leave it null if the content gives no date. Never use the file's timestamp.
