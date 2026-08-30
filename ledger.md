# Ledger — Evidence Layer build

Handover document between sessions. [improvements.md](improvements.md) is the plan and does not
change as work proceeds; this file records what is *built*, what is *next*, and what a future session
would otherwise have to rediscover.

**Update this file at the end of every working session, before committing.**

---

## Current position

**Slice 3 (one pipeline) — complete, 2026-08-30.** Test suite: 725 → 802 passing. Working tree
clean, `no_google` in sync with origin, `pyflakes` clean on all new modules.

Slices 1 and 2 completed earlier the same day, minus the deferrals listed below.

Slice 3 is done when articles and tips are generated from the same reviewed bundles and the ranking
tests pass. Both hold: `EvidencePipeline` runs acquire → extract → cluster → rank → synthesise →
review → write → re-check → publish, and the two products differ only in their writer.

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
| **Slice 2** | | |
| Claim ceiling / G8 | done | [claim_policy.py](app/services/evidence/claim_policy.py) |
| Gates G3, G4, G5, G7, G9 + severity table | done | [gates.py](app/services/evidence/gates.py) |
| Grade rubric and reader-facing wording | done | [grading.py](app/services/evidence/grading.py) |
| Evidence reviewer (code-first, model advisory) | done | [reviewer.py](app/services/evidence/reviewer.py) |
| Synthesizer (2b) | done | [synthesizer.py](app/services/evidence/synthesizer.py) |
| `RunOutcome` + scheduler policy and backoff | done | [run_outcome.py](app/models/run_outcome.py), [pipeline_scheduler.py](app/services/pipeline_scheduler.py) |
| **Tip presets deleted from the runtime path** | done | [tip_context.py](app/services/tip_context.py) |
| End-to-end flow test | done | [test_evidence_flow.py](app/tests/test_evidence_flow.py) |
| **Slice 3** | | |
| Topic clustering | done | [clustering.py](app/services/evidence/clustering.py) |
| Ranking (item 7) | done | [ranking.py](app/services/evidence/ranking.py) |
| Article and tip writers | done | [writers.py](app/services/evidence/writers.py) |
| Code-rendered citation URLs | done | [citations.py](app/services/evidence/citations.py) |
| Post-edit re-check (item 6) | done | [postedit.py](app/services/evidence/postedit.py) |
| Pipeline orchestration | done | [evidence_pipeline.py](app/services/evidence_pipeline.py) |
| Job seam and scheduler dispatch | done | [evidence_jobs.py](app/services/evidence_jobs.py) |
| Europe PMC client | **deferred** | — |
| ClinicalTrials.gov client | **deferred** | — |
| News-as-signal wrapper | **deferred** | — |

### Read this before assuming anything works end to end

**The evidence pipeline now generates content, but only behind a flag.** Setting
`LIVEON_EVIDENCE_PIPELINE=1` switches both scheduled jobs from the legacy prose path to the evidence
path. With the flag unset — the default — nothing about the running system changes except the two
slice-2 items below.

**It has never run against the live PubMed API.** Every test uses fixture XML and stub models. The
first real run should be deliberate, with the cache on and the flag set for one job only, and it
should be *watched*: a local model writing to prompts that have never seen its output is the part of
this design with the least evidence behind it.

**Live in the running system regardless of the flag** (from slice 2):

1. **The tip presets are gone.** A run with no reachable research publishes nothing instead of
   falling back to hard-coded claims.
2. **The scheduler acts on run outcomes.** "Nothing new" satisfies the cadence; an outage backs off
   exponentially instead of retrying hourly.

---

## How the pieces fit

```
PubMedClient.search_records(query)          research/pubmed.py
   → EvidenceRecord(state="acquired")       document_text assembled once, verbatim
   → EvidenceStore.upsert_record()          canonical key + every alias
   → ExtractorAgent.extract(record)         model quotes; code computes offsets
   → EvidenceStore.upsert_record()          state="extracted"
   → store.set_state(key, "approved")       required by G1
   → cluster_records(approved)              grouped by intervention
   → rank_clusters(clusters)                strength · novelty · recency · priority − redundancy
   → SynthesizerAgent.synthesize(records)   extracts only; code attaches NumberRefs
   → EvidenceReviewer.review(bundle, ...)   gates -> grade -> advisory model
        ├─ approved / downgraded  -> ArticleWriter / TipWriter
        ├─ regenerate             -> the prose was wrong; try again
        └─ rejected               -> the evidence was wrong; stop
   → recheck_published_text(body)           G2/G4/G8 again, on the edited text
   → publish + store.record_usage(...)      what G9 reads on the next run
```

The reviewer runs the gates in two passes on purpose: G8 permits certainty language only in a
`high` bundle, and the grade is not known until the other gates have run. `run_gates(skip_gates=...)`
exists for that ordering and nothing else.

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
7. **G1 requires `state == "approved"`** (overridable via `allowed_states`). Whatever wires the
   pipeline must call `store.set_state(key, "approved")`, as the flow test does.
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


### Slice 2

15. **Gate severity is a table, not a convention.** `GATE_SEVERITY` says which gates refuse and which
    only cap the grade, and `grading.compute_grade` reads it. G3 rejects rather than downgrades: a
    claim that says "people" about mouse evidence is not a weaker claim, it is a different and untrue
    one, and the fix is to rewrite it.
16. **Refusals split into "regenerate" and "rejected".** G6, G9 and G10 are facts about the evidence
    that no rewrite can change, so they end the attempt. The rest are about the prose, so the writer
    gets another go (capped by `LIVEON_MAX_REGENERATIONS`).
17. **"high" needs pooled *randomised* evidence.** A systematic review tells us it pooled something,
    not what. Unless the bundle also cites trials, or the record is indexed as randomised, a lone
    review grades `moderate`. Ambiguity resolves downward, here as everywhere.
18. **An unclassified endpoint is not a clinical endpoint.** `_has_clinical_endpoint` requires an
    explicit `is_surrogate == False`, keeping the optimistic reading out of the grade.
19. **The advisory model never sees a document or a URL** — only design, subject, sample size and the
    claims. It is judging the writing, not re-reading the paper, and text it cannot see is text it
    cannot quote.
20. **A review that cannot run is a refusal.** If the advisory model raises, the decision is
    `regenerate`, never "approved because the reviewer was down".
21. **Backoff lives in its own `pipeline_retry` table**, not as a `retry_at` column on
    `pipeline_schedule` as improvements.md item 9 suggested. That table requires `last_run_at`, and a
    job whose very first run failed has not run — writing a row there would fake a successful run and
    push the next attempt a whole cadence away. Same behaviour, different location.
22. **One notion of "the same number", shared.** `gates.normalise_number` canonicalises `18.0` and
    "18" to the same string, and both the extractor and G2 use it. They disagreed at first, which
    silently stripped references from honest claims — the flow test caught it.
23. **Tip presets live on as `app/tests/fixtures/tip_presets.py`.** Deleted outright they would have
    taken some useful test inputs with them; left in the runtime they were a way to publish
    mis-sourced health claims during an outage.

### Slice 3

24. **Clustering is by intervention, not by intervention+outcome.** A reader asks "what does
    time-restricted eating do?", so one cluster gathers everything about one intervention whatever
    each study measured. The narrower `topic_key` (intervention|outcome) is still what G9 uses for
    the repetition window.
25. **A record with no extracted intervention clusters by title words.** That groups less, which is
    the safe direction: a split cluster produces two narrow articles, an over-merged one produces a
    single article claiming two unrelated things.
26. **Ranking needs a grade before review exists**, so `provisional_grade` runs the same rubric over
    the records alone. Candidates are therefore ranked on the grade they will actually receive, and
    the only later movement comes from what the claims turn out to say.
27. **Ranking weights are a stated position, not a tuned parameter.** There is no ground truth to fit
    them to, so they encode the editorial claim directly: strength counts twice recency, and a topic
    covered last week is worth almost nothing however strong.
28. **The writers never see a URL.** `citation_url` derives links from the canonical identifier at
    render time, and `strip_handles` removes the `[E1]` markers from the body. A model that has never
    seen a link cannot mangle one into the prose.
29. **The post-edit re-check is deliberately coarser than G4.** Once prose is rewritten, the mapping
    from sentence to source is gone, so it falls back to the strongest design in the bundle. The
    precise version already ran before the editor touched it; this one catches drift, not nuance.
30. **A failed re-check is editorial, not scientific.** The bundle stands and the writer retries up
    to `LIVEON_MAX_REGENERATIONS`; only if the prose keeps drifting does the run end empty.
31. **The run works down the ranking.** A topic refused for repetition or thin evidence does not end
    the run while better-supported candidates are waiting — but a model or publisher failure does,
    because those will fail the same way for every candidate.
32. **Extraction promotes records to `approved` directly.** That is the deterministic layer's verdict
    on the *record* (acquired from a primary source, classified from indexed metadata, spans
    anchored). Whether anything may be *said* with it is the reviewer's question, one stage later.

---

## Next session: start here

**Slice 4 — surface.** Make the evidence visible, make a run reconstructable, and put the benchmark
in CI. This is the slice that ends with the flag flipped on.

1. **Publication surface** (item 8). [articles/detail.html](app/templates/articles/detail.html) gains
   an evidence block: grade, the one-line summary, study types, primary sources, limitations. List
   views and tips get a compact `Evidence: Moderate` badge. `Article.evidence_assessed` already
   distinguishes legacy rows; they render "not assessed" rather than being retro-graded.
   `describe_grade` already produces the wording.
2. **Observability** (item 10) — `run_id` threaded through every stage, `pipeline_runs` and
   `run_events` tables, and the three timestamps kept apart (`source_published_at` vs `retrieved_at`
   vs `published_date`). `EvidencePipelineResult` already carries most of what the log needs.
3. **The benchmark** (item 11) — the corpus under `app/tests/fixtures/corpus/`, one directory per
   case, and the thirteen invariants as named tests. Most of the machinery exists; what is missing is
   the corpus and the CI gate.
4. **Then flip `LIVEON_EVIDENCE_PIPELINE` to 1** — but not before a watched live run (see the warning
   above).

Still outstanding from slice 1: **Europe PMC**, **ClinicalTrials.gov**, and the **news-as-signal
wrapper**. Europe PMC matters most: it is where open-access full text would come from, and full text
is what would let extraction fill the fields abstracts leave `not_reported`.

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

- **The evidence path has never run live.** See the warning above.
- **The editors are not yet in the evidence path.** `ArticleWriter` and `TipWriter` produce the final
  text directly; the existing `EditorAgent` and `TipEditorAgent` (and the tip editor's good rubric)
  are not called. The post-edit re-check exists and is wired, so inserting them is a small change —
  but it is a change, and until then "post-edit" means "post-writer".
- **A pre-existing duplicate test name**: `test_an_unknown_cadence_is_refused` is defined twice in
  [test_pipeline_cadence.py](app/tests/test_pipeline_cadence.py) (lines 125 and 330), so the first
  definition never runs. Found by pyflakes during slice 2; left alone as unrelated to this work.
- **G8 is lexical only.** improvements.md 0.2 pairs it with an LLM paraphrase classifier in the
  advisory pass. The lexical layer is authoritative and shipped; the classifier is not written.
- **Europe PMC, ClinicalTrials.gov and the news-signal wrapper are not built.** PubMed alone is
  enough to exercise the spine, but item 1 is not complete without them.
- **`build_pubmed_client` has never run against the live API.** Every test uses fixture XML. A first
  live call should be made deliberately, with the cache on, before any scheduled job depends on it.
- **The corpus fixtures for item 11 do not exist yet.** Current fixtures are inline in the test
  modules, which is fine for unit tests and not sufficient for the benchmark.
