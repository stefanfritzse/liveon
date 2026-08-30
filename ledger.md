# Ledger — Evidence Layer build

Handover document between sessions. [improvements.md](improvements.md) is the plan and does not
change as work proceeds; this file records what is *built*, what is *next*, and what a future session
would otherwise have to rediscover.

**Update this file at the end of every working session, before committing.**

---

## Current position

**Slice 1 (the spine) — core complete, 2026-08-30.** Test suite: 424 → 571 passing. Working tree
clean, `no_google` in sync with origin, `pyflakes` clean.

Slice 1 is done when a source can be discovered, acquired, extracted, stored, and cited by key, and
no claim with an unresolvable source or an unanchored number can be constructed. That property now
holds for the PubMed path and is covered by tests.

| Component | State | File |
|---|---|---|
| Evidence models (spans, `Extracted`, records, claims, bundles) | done | [app/models/evidence.py](app/models/evidence.py) |
| Research knowledge store (sources, aliases, bundles, usage) | done | [store.py](app/services/evidence/store.py) |
| Shared HTTP layer (cache, per-host rate limit, retries) | done | [research/http.py](app/services/research/http.py) |
| PubMed client and parser | done | [research/pubmed.py](app/services/research/pubmed.py) |
| Metadata classification (design/subject) | done | [classification.py](app/services/evidence/classification.py) |
| Span-anchored extractor (2a) | done | [extractor.py](app/services/evidence/extractor.py) |
| Gates G1, G2, G6, G10 | done | [gates.py](app/services/evidence/gates.py) |
| Evidence handles and allowlist (item 4) | done | [citations.py](app/services/evidence/citations.py) |
| Provenance on `Article`, `Tip`, `TipDraft`, `TipPublisher` | done | [content.py](app/models/content.py), [tip_publisher.py](app/services/tip_publisher.py) |
| Network guard in tests | done | [conftest.py](app/tests/conftest.py) |
| Europe PMC client | **deferred** | — |
| ClinicalTrials.gov client | **deferred** | — |
| News-as-signal wrapper | **deferred** | — |

### Read this before assuming anything works end to end

**Nothing calls the evidence layer yet.** The article and tip pipelines run exactly as they did
before this work: same aggregator, same summarizer, same presets. The new modules are complete and
tested in isolation but are not wired into any runtime path, and `LIVEON_EVIDENCE_PIPELINE` does not
exist yet — it arrives when there is a pipeline to gate. This is the intended shape of a slice, but
do not read "slice 1 complete" as "the site publishes evidence-backed content".

---

## How the pieces fit

```
PubMedClient.search_records(query)          research/pubmed.py
   → EvidenceRecord(state="acquired")       document_text assembled once, verbatim
   → EvidenceStore.upsert_record()          canonical key + every alias
   → ExtractorAgent.extract(record)         model quotes; code computes offsets
   → EvidenceStore.upsert_record()          state="extracted"
        … slice 2: synthesis, review, grading, state="approved" …
   → run_gates(bundle, records)             G1/G2/G6/G10 — pure, no model
   → EvidenceHandles.for_keys(...)          writers cite [E1], never URLs
```

Two properties hold at every step and should not be weakened:

1. **A value cannot outlive its evidence.** `EvidenceRecord.verified()` re-checks every span against
   `document_text` and demotes what no longer matches. It runs after extraction *and* on every load
   from the store, so hand-edited rows and format drift both fail closed.
2. **`document_text` is written once.** `upsert_record` refuses to blank it on a later metadata-only
   refresh, because every stored span indexes into it.

---

## Decisions taken during the build

Not in improvements.md; recorded so a later session does not relitigate them.

1. **DOI is the canonical key; PMID is the fallback.** Other sources also carry a DOI, so it is the
   identifier that will actually collapse duplicates. All identifiers are still stored as aliases and
   every one of them resolves.
2. **`document_text` format is fixed:** title, blank line, then each abstract section as
   `LABEL: text`, separated by blank lines. Changing it invalidates every stored span. If it must
   ever change, bump `SCHEMA_VERSION` and re-acquire rather than migrating in place.
3. **The model supplies a quote; code computes the offsets.** Models cannot count characters but can
   copy a phrase. A whitespace-normalised second attempt is allowed (models re-wrap what they copy);
   nothing beyond that, because anything more is repairing an invented quote.
4. **A number must appear inside the quote it cites**, not merely beside it in the reply. This is
   what catches the subtle failure where a genuine quote carries a figure the paper never reported.
5. **Classification is metadata-only.** Publication types and MeSH decide design and subject; the
   model is never asked. A mouse study with no design label is `preclinical`, not `unknown` — being
   unclassifiable and being preclinical are different things and only the first should block.
6. **Extraction cache lives on the record**, keyed by `(state, prompt_version, model_id)`, not in a
   separate table. Bumping `EXTRACTION_PROMPT_VERSION` re-extracts everything, which is the point.
7. **G1 requires `state == "approved"`** (overridable via `allowed_states`). Slice 2 must promote
   records to `approved` after review, or every bundle will fail G1.
8. **Retraction is orthogonal to lifecycle state.** A retracted record stays `approved`; G6 is what
   blocks it. Overwriting the lifecycle would lose the fact that it was reviewed and used.
9. **Years are exempt from G2.** A four-digit 1900–2100 integer identifies a study rather than
   quantifying a finding. Everything else must trace to a span.
10. **`allowlisted_sources` now delegates** to `allowlisted_evidence` with a URL normaliser, so the
    trailing-slash behaviour the editor relied on is preserved exactly. Existing tests cover it.
11. **The network guard allows loopback.** asyncio builds its event-loop self-pipe from a real socket
    pair on Windows; blocking every connection takes the async suite down with the network.
12. **`Tip` gained `source_urls` as well as the evidence fields.** Tips will keep citing plain URLs
    until the tip path moves onto bundles in slice 3, and losing them again in the meantime would
    re-introduce the exact defect this work exists to fix.
13. **`clamp_grade` exists already** even though the rubric does not. It is invariant I4 in one
    function — a model may lower a grade, never raise one, and an unrecognised grade name is treated
    as the floor. The reviewer in slice 2 must route its grade through it rather than re-deriving
    the rule.
14. **Unused by design, do not delete:** the `Literal` aliases in `evidence.py` (`SourceType`,
    `StudyDesign`, `Subject`, `RecordState`, `Grade`, …) are the documented value sets the field
    comments refer to, and `create_store` is the construction path slice 2 wires up. Everything else
    that was unreferenced has been removed — including a `GATES` tuple that duplicated the list
    `run_gates` actually calls, which would have let a future gate be registered and never run.

---

## Next session: start here

In order. Slice 2 is "Judgement" in the improvements.md delivery table.

**Trip hazard, first thing:** G1 requires `state == "approved"`, and nothing currently promotes a
record past `extracted`. Whatever wires the reviewer must call `store.set_state(key, "approved")`
after review, or every bundle will fail G1 and the pipeline will look broken when it is merely
closed. `g1_sources_resolve(..., allowed_states=...)` is the seam for testing around it.

1. **Europe PMC client** (`app/services/research/europepmc.py`) — same shape as `pubmed.py`, plus
   open-access full text where `isOpenAccess=Y`. Reuse `ResearchHttpClient`; the record shape and
   `document_text` rules are already settled.
2. **Synthesizer (2b)** — `EvidenceBundle` from a cluster of extracted records. It must never see raw
   `document_text`; it works from already-extracted fields, which is what stops it inventing numbers.
   Contradictions populate `Claim.contradicted_by` rather than being averaged.
3. **Remaining gates** — G3 (subject consistency), G4 (causal language by design), G5 (surrogate
   endpoints), G7 (sample-size floors), G8 (claim ceiling, `claim_policy.py`), G9 (repetition window;
   `store.last_used_at` is already there for it).
4. **Grade rubric** — `compute_grade(bundle, records) -> (Grade, rationale)`, deterministic, one test
   per row of the table in improvements.md item 3.
5. **Reviewer** — deterministic layers first, then the advisory LLM pass. A model-returned grade above
   the computed one is discarded, never honoured.
6. **`RunOutcome` and the scheduler policy table** (item 9), including the `retry_at` column added by
   `ALTER TABLE` guarded by `PRAGMA table_info`.
7. **Delete the tip presets from the runtime path** (item 9). This is the single highest-value change
   in the whole plan and it should not slip further than slice 2.

---

## Conventions this codebase has (follow them)

- Models are `@dataclass(slots=True)` in `app/models/`, with `to_document()` / `from_document()` and
  tolerant parsing of missing keys. Storage keeps that JSON verbatim in a `data` column.
- Services in `app/services/` are agents or repositories; pipelines talk to them through `Protocol`
  classes declared in [pipeline.py](app/services/pipeline.py).
- LLM calls go through `invoke_json_object` ([json_repair.py](app/utils/json_repair.py)), which
  handles code fences, prose preambles, Python literals, and one re-ask.
- Chat models come from `create_chat_model(agent_label=...)`
  ([llm_factory.py](app/services/llm_factory.py)); per-agent override is `LIVEON_<AGENT>_MODEL`.
- Structured logging is `logger.info("...", extra={"event": "namespace.thing"})`.
- Tests are plain pytest functions with hand-rolled stub LLMs (see
  [test_evidence_extractor.py](app/tests/test_evidence_extractor.py) for the pattern used here) and
  `httpx.MockTransport` for HTTP. No test may touch the network; `@pytest.mark.live` opts out and is
  excluded from CI.
- Env vars are `LIVEON_*`. Runtime target is Python 3.11 (Docker); the local venv is 3.14.
- Run the suite with `./.venv/Scripts/python.exe -m pytest -q`, and lint with
  `./.venv/Scripts/python.exe -m pyflakes <files>`.

---

## Known gaps

- **No runtime wiring.** See the warning above.
- **Europe PMC, ClinicalTrials.gov and the news-signal wrapper are not built.** PubMed alone is
  enough to exercise the spine, but item 1 is not complete without them.
- **`build_pubmed_client` has never run against the live API.** Every test uses fixture XML. A first
  live call should be made deliberately, with the cache on, before any scheduled job depends on it.
- **The corpus fixtures for item 11 do not exist yet.** Current fixtures are inline in the test
  modules, which is fine for unit tests and not sufficient for the benchmark.
