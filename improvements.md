# Live On — Evidence Layer

**Goal:** replace the RSS→prose content pipeline with an evidence-first research pipeline, so that
every published claim traces to a real scientific source and its strength is stated honestly.

**Baseline:** commit `f078224` (branch `no_google`), 2026-08-30, 362 tests passing.
**Runtime:** local Ollama (`qwen2.5:14b` class), offline-friendly, no cloud or Google dependency.
**Operating constraint:** the pipeline is **fully autonomous**. Nothing is held for human approval,
so every gate in this plan must be executable code, covered by tests, and safe when it fails.
**Dependencies:** none new. `httpx` (already pinned) plus stdlib `xml.etree` covers every research
API used here; `feedparser` stays for news discovery.

Numbering follows the original backlog (items 1–12) so the two documents can be read side by side.
Item 2 is split into 2a/2b, and item 13 (the coach) is new.

---

## 0. Invariants

These are the properties the system must hold at all times. Every item below exists to establish or
protect one of them, and each has at least one test in item 11 that fails when it is violated.

| | Invariant |
|---|---|
| **I1** | LLMs interpret and communicate evidence. They never define what the evidence *is*. |
| **I2** | Every published quantitative claim resolves to a verbatim span in a stored source document. |
| **I3** | `unknown` is a value, not a gap to be filled. Absence is never inferred away. |
| **I4** | Evidence grades are computed in code. A model may downgrade a grade; it may never raise one. |
| **I5** | Unverifiable means unpublished. No failure mode degrades into "publish anyway". |
| **I6** | A gate that exists only inside a prompt is not a gate. |

## 0.1 The autonomy budget

There is no human reviewer, so the controls that would normally sit with one have to be paid for
elsewhere. This is where:

1. **Deterministic gates carry the weight.** The scientific review (item 3) is primarily Python. The
   model is consulted only for the judgement that cannot be computed, and only after the computable
   checks have already passed.
2. **The model is bounded by construction.** It cites opaque evidence handles it cannot invent
   (item 4), it is graded by a rubric it cannot influence upward (item 3), and every number it writes
   must already exist in an extracted span (I2).
3. **Silence is the default failure.** Every named failure state (item 9) ends in "publish nothing".
   The offline presets that let a feed outage turn into a confident health claim are deleted.
4. **A claim ceiling applies regardless of evidence.** Some classes of statement are never published
   autonomously, however strong the study (section 0.2).
5. **The benchmark is the release gate.** Item 11 runs in CI. A prompt or model change that lowers
   scientific integrity fails the build instead of reaching readers.
6. **Publication is revocable.** Item 12 can retract or annotate already-published content. Without a
   human in the loop, automatic correction is the only correction mechanism there is.

## 0.2 Claim ceiling

Enforced by `app/services/evidence/claim_policy.py` as gate **G8**, over both the drafted and the
*edited* text. A draft that trips one is regenerated once and then abandoned; it is never rewritten
into compliance, because the rewrite would be the model arguing with the gate.

- **Dosing and protocol specifics** for supplements or drugs — amount, frequency, duration.
- **Diagnosis or individualised medical advice** — anything phrased as instruction to a reader with a
  named condition.
- **Cure / prevent / reverse / treat** applied to a named disease.
- **Discontinuation or substitution of medical care**, including "instead of" and "you do not need".
- **Superlative certainty** — proven, guaranteed, definitive — outside a `high` grade bundle.

The check is a lexical rule set plus a small LLM classifier for paraphrase. The lexical half is
authoritative: when it fires, no model opinion overrides it.

---

## 1. Where the code actually stands

Verified against the tree at the baseline commit, because several items in the original backlog
describe work that is partly done, and one describes a mechanism that does not exist.

| Area | Today | Implication |
|---|---|---|
| Discovery | [aggregator.py](app/services/aggregator.py) parses RSS only; the default feeds are Google News search RSS. No page body is ever fetched. | There is no acquisition layer to extend. Item 1 is new construction. |
| Article provenance | [allowlisted_sources](app/models/editor.py#L115) already rejects model-invented URLs and keeps the feed spelling. | Item 4 is a generalisation (URL allowlist → evidence IDs), not a rewrite. |
| Tip provenance | [TipPublisher.publish](app/services/tip_publisher.py#L57) builds `Tip(...)` with no source field; [Tip](app/models/content.py#L145) has none. | `context.sources` and `TipDraft.metadata` are discarded at persistence. Confirmed. |
| Tip fallback | [_DEFAULT_PRESETS](app/services/tip_context.py) ships quantitative claims ("20-30g of protein … curb cravings for the next 6 hours") behind a link that does not support them. | A feed outage currently produces a confident, mis-sourced health claim. Most urgent single fix here. |
| Article selection | [ContentPipeline.run](app/services/pipeline.py) calls `summarize([item])` per candidate, newest-first. | Item 7 is real; the `Sequence` interface already exists, unused. |
| Storage | [sqlite_repo.py](app/services/sqlite_repo.py) stores `to_document()` JSON in a `data` column and `from_document` tolerates missing keys. | New model fields are additive; no migration needed. Item 8 is template work. |
| Scheduling | Runners return `bool`; [_execute](app/services/pipeline_scheduler.py#L384) stamps `last_run` only on success, on an hourly tick. | "Nothing new today" advances the cadence; "feeds are down" retries hourly forever. Item 9 must change this signature. |
| Coach | [coach.py](app/services/coach.py) has no retrieval — system prompt plus history. | The most safety-sensitive surface sits entirely outside the evidence layer. Item 13. |
| Tests | 362 passing; [conftest.py](app/tests/conftest.py) has no network guard. | New HTTP clients will silently reach the network in CI unless one is added (item 11). |

---

## 2. Target architecture

```
                         DISCOVERY
        ┌────────────────────┼────────────────────┐
        │                    │                    │
   PubMed / EPMC     ClinicalTrials.gov      News / RSS
    (primary)           (primary)          (signal only)
        │                    │                    │
        └────────────────────┼────────────────────┘
                             ▼
                    ACQUISITION  (code)
        canonical ID · verbatim document · metadata
                             │
                             ▼
              ┌──── RESEARCH KNOWLEDGE STORE ────┐
              │   evidence_sources · aliases     │
              │   bundles · usage · runs         │
              └──────────────┬───────────────────┘
                             ▼
              2a  EXTRACTOR    (LLM, span-anchored, per source)
                             ▼
              2b  SYNTHESIZER  (LLM, per topic cluster)
                             ▼
              3   EVIDENCE REVIEWER
                  ├─ G1–G10 gates      (code, authoritative)
                  ├─ grade rubric      (code, downgrade-only)
                  └─ residual review   (LLM, advisory)
                             │
                     approved bundle
                    ┌────────┴────────┐
                    ▼                 ▼
              ARTICLE WRITER      TIP WRITER
                    │                 │
              ARTICLE EDITOR       TIP EDITOR     ← editorial only, claim set frozen
                    └────────┬────────┘
                             ▼
                POST-EDIT RE-CHECK (G2, G4, G8)
                             ▼
                         PUBLISHER
```

The post-edit re-check is not decoration. Editorial rewriting is exactly where "was associated with"
becomes "reduces", so the gates covering causal language and the claim ceiling run again over the
text that will actually be published.

---

# P0 — The evidence spine

## 1. Discovery: query the literature directly; demote news to a signal

**Problem.** The original plan asked the aggregator to follow a news story back to its underlying
study. That resolution chain — follow the Google News redirect, fetch publisher HTML, find a DOI,
search PubMed, disambiguate — is the highest-failure-rate component in the whole design, and it fails
*silently* into "no evidence found". It also inverts the difficulty: the literature APIs already
offer structured search over structured metadata, which is exactly what we need.

**Change.** Primary discovery runs against the research APIs. News becomes a topicality and novelty
signal that is never itself evidence.

New package `app/services/research/`:

| Module | Responsibility |
|---|---|
| `pubmed.py` | E-utilities `esearch`/`efetch`. Returns PMID, DOI, title, abstract (verbatim), journal, dates, publication types, MeSH descriptors, retraction links. |
| `europepmc.py` | Europe PMC REST. Same shape; adds open-access full text where `isOpenAccess=Y`. |
| `trials.py` | ClinicalTrials.gov v2. NCT ID, phase, enrolment, status, primary outcomes. |
| `news.py` | Wraps the existing aggregator; emits `NewsSignal`, never `EvidenceRecord`. |
| `http.py` | Shared politeness layer: rate limiting, retries with backoff, on-disk cache. |

Rules that make this safe and neighbourly:

- **Rate limits.** NCBI allows 3 req/s unauthenticated, 10 with a key (`LIVEON_NCBI_API_KEY`). Send
  `tool=liveon` and `email=` on every E-utilities call. The limiter lives in `http.py` and is shared,
  not per-client.
- **Cache.** Responses land under `cache/research/<source>/<sha256-of-request>.json` with a TTL
  (`LIVEON_RESEARCH_CACHE_TTL_HOURS`, default 168). The cache is what makes the item 11 benchmark
  runnable offline and keeps re-extraction free.
- **Queries** come from `LIVEON_RESEARCH_QUERIES` (JSON list of `{name, query, source, max_results}`),
  defaulting to a small set filtered by publication type and date window, e.g.
  `("longevity"[tiab] OR "healthy aging"[tiab]) AND (randomized controlled trial[pt] OR meta-analysis[pt] OR systematic review[pt])`.
- **News resolution is opt-in and lossy by design.** `LIVEON_NEWS_RESOLUTION` defaults to `0`. When
  enabled, `news.py` reads `citation_doi` / `dc.identifier` / `prism.doi` meta tags from the fetched
  page and looks the DOI up. A signal that does not resolve is **dropped**, never promoted.

**Acceptance.** With every news feed returning 500, the pipeline still discovers, acquires and
publishes. With the research APIs unreachable, it publishes nothing (item 9).

## 2a. Extraction: per source, span-anchored, cached

**Problem.** The evidence model in the original plan assumes access to sample size, effect size with
uncertainty, limitations, and funding/COI. Abstracts frequently omit all four, and full text is
paywalled outside the Europe PMC open-access subset. Handed a field it cannot fill, a 14B local model
will fill it anyway. This is the single largest confabulation risk in the design.

**Change.** Extraction is per source, produces only span-anchored values, and is separated from
synthesis (2b) so it can be cached and replayed.

```python
# app/models/evidence.py
T = TypeVar("T")

@dataclass(slots=True, frozen=True)
class Span:
    """Anchor into the stored verbatim document. This is what makes I2 checkable."""
    quote: str
    start: int
    end: int

    def verify(self, document_text: str) -> bool:
        return document_text[self.start : self.end] == self.quote


@dataclass(slots=True)
class Extracted(Generic[T]):
    value: T | None
    status: Literal["extracted", "not_reported", "not_extractable"]
    span: Span | None = None      # required when status == "extracted"
```

- `extracted` — present in the document, with a span that verifies.
- `not_reported` — the document genuinely does not state it.
- `not_extractable` — the model could not locate it, or its span failed verification.

`ExtractorAgent.extract(record)` returns a record whose every extracted field carries a span. **Any
span that does not verify byte-for-byte against `document_text` is discarded and the field is
demoted to `not_extractable`.** The model never gets a second chance to "correct" the offsets; a
wrong span is a signal that the value was invented.

Results are cached in the store keyed by `(source_key, prompt_version, model_id)`, so re-runs and the
benchmark do not re-invoke the model.

## 2b. Synthesis: per topic cluster, across sources

`SynthesizerAgent.synthesize(records) -> EvidenceBundle`. It reasons across an already-extracted
cluster; it never reads raw documents, so it cannot introduce an unanchored number.

```python
@dataclass(slots=True)
class NumberRef:
    text: str                      # as it will appear in prose, e.g. "23%"
    source_key: str
    span: Span

@dataclass(slots=True)
class Claim:
    text: str
    claim_type: Literal["descriptive", "associative", "causal", "recommendation"]
    evidence_keys: list[str]       # source_keys, in the store
    numbers: list[NumberRef]
    population_scope: str
    applicability: str
    limitations: list[str]
    contradicted_by: list[str] = field(default_factory=list)

@dataclass(slots=True)
class EvidenceBundle:
    bundle_id: str
    topic_key: str
    claims: list[Claim]
    grade: Grade                   # computed in item 3, never by the model
    grade_rationale: list[str]
    review: ReviewDecision | None
    schema_version: int
    created_at: datetime
```

Contradiction is represented, not averaged: when two records disagree in direction on the same
outcome, both are kept and `contradicted_by` is populated. Item 12 turns those into article material.

**Acceptance.** A synthesis whose claim contains a number absent from every `NumberRef` fails G2 and
never reaches an editor.

## 3. Evidence Reviewer: code first, model last

**Problem.** As originally written, the reviewer was a prompt — one local model grading another local
model, and the load-bearing gate for the entire system. With no human behind it, that is not enough
independence to be worth the name (I6).

**Change.** `app/services/evidence/reviewer.py` runs three layers in order. Each gate is a pure
function over `(bundle, store)` returning `list[Violation]`, individually tested.

### Layer 1 — deterministic gates (authoritative)

| Gate | Check | On failure |
|---|---|---|
| **G1** | Every `evidence_key` resolves to an `approved`-or-better record in the store. | reject |
| **G2** | Every number in every claim matches a `NumberRef` whose span verifies. | reject claim; reject bundle if it was load-bearing |
| **G3** | Subject consistency: a claim citing only animal/in-vitro records may not use human-population language. Subject comes from PubMed publication types and MeSH (`Animals`, `Humans`), **not** from the model. | downgrade to `preliminary` + rewrite scope, else reject |
| **G4** | Causal language requires an RCT or a meta-analysis of RCTs. Observational designs permit associative verbs only. | reject claim |
| **G5** | Surrogate endpoints may not be stated as clinical benefit. | downgrade |
| **G6** | No cited record has `retraction_state` in `{retracted, concern}`. | reject |
| **G7** | Sample-size floors: human `n < 30`, or `not_reported`, caps the grade at `preliminary`. | cap |
| **G8** | Claim ceiling (section 0.2), lexical layer authoritative. | reject |
| **G9** | Novelty: the topic cluster has not been published within `LIVEON_TOPIC_COOLDOWN_DAYS` (default 30) — checked against `evidence_usage`. | reject |
| **G10** | Unknown ceiling: `design` or `subject` `not_extractable` on a load-bearing record → `insufficient`. | reject |

### Layer 2 — grade rubric (code, downgrade-only)

```python
Grade = Literal["high", "moderate", "low", "preliminary", "insufficient"]
```

| Grade | Requires |
|---|---|
| `high` | A systematic review or meta-analysis of human RCTs, or ≥2 independent human RCTs agreeing in direction, no unresolved contradiction, clinical endpoints. |
| `moderate` | One human RCT, n ≥ 100, clinical endpoint; or a systematic review of prospective cohorts. |
| `low` | Human observational only; or an RCT with surrogate endpoints or n < 100. |
| `preliminary` | Animal, in-vitro, in-silico, preprint, n < 30, or single exploratory result. |
| `insufficient` | Anything tripping G1, G2, G6 or G10. **Never publishes.** |

`compute_grade(bundle, records) -> (Grade, rationale)` is deterministic and unit-tested per row.

### Layer 3 — residual review (LLM, advisory)

The model is asked only what code cannot compute: does the draft overstate its sources, is important
contradicting evidence ignored, is the stated applicability honest. Its output is constrained:

```python
@dataclass(slots=True)
class ReviewDecision:
    status: Literal["approved", "downgraded", "regenerate", "rejected"]
    grade: Grade                   # must be <= the computed grade; a higher value is discarded
    violations: list[Violation]
    notes: str
    reviewed_at: datetime
    model_id: str
    prompt_version: str
```

A returned grade above the computed one is dropped with a warning, not honoured (I4). Regeneration is
capped at `LIVEON_MAX_REGENERATIONS` (default 2), after which the outcome is `REVIEW_REJECTED`.

## 4. Provenance as identity, not prose

**Problem, corrected.** Articles are already protected: `allowlisted_sources` keeps model-invented
URLs out. Tips are not — `TipPublisher` never persists sources, so `context.sources` dies at the
database boundary. The work is to generalise the article mechanism and extend it to tips.

**Change.**

1. **Writers never see URLs.** Prompts carry opaque handles `[E1] … [En]`; the handle→`source_key` map
   lives in application code. `allowlisted_evidence(allowed_keys, model_keys)` replaces
   `allowlisted_sources`; the old function stays as a thin wrapper during the migration.
2. **Unknown handle = dropped claim.** A citation the map does not contain is a G1 violation.
3. **Rendering is code.** Citations are formatted by the publisher from stored records. The model
   never writes a URL, a DOI, a journal name, or an author list into the body.
4. **Models gain evidence fields** (additive JSON, no migration):

```python
# Article and Tip
evidence_bundle_id: str | None = None
evidence_keys: list[str] = field(default_factory=list)
evidence_grade: str | None = None      # "high" … "preliminary"
evidence_summary: str | None = None    # "one human RCT plus supporting cohort evidence"
```

Both are `slots=True` dataclasses: add the fields to the class, `to_document`, and `from_document`
together, defaulting to empty so existing rows keep loading.

**Acceptance.** Given a stored article or tip, `published claim → reviewed claim → evidence record →
source_key → original document span` resolves entirely from the database, with no LLM in the path.

---

# P1 — One research pipeline, several products

## 5. The Research Knowledge Store

`app/services/evidence/store.py`, in the existing SQLite database, following the repository
conventions already in `sqlite_repo.py` (JSON `data` column, denormalised columns for lookup).

```sql
CREATE TABLE evidence_sources (
  source_key       TEXT PRIMARY KEY,   -- canonical: "doi:10.1001/x", "pmid:12345678", "nct:NCT01234567"
  source_type      TEXT NOT NULL,
  state            TEXT NOT NULL,      -- discovered|acquired|extracted|reviewed|approved|rejected
  retraction_state TEXT NOT NULL DEFAULT 'none',   -- none|concern|corrected|retracted
  superseded_by    TEXT,               -- source_key, nullable
  document_text    TEXT,               -- verbatim; spans index into this and it is never rewritten
  data             TEXT NOT NULL,      -- EvidenceRecord.to_document()
  first_seen_at    TEXT NOT NULL,
  retrieved_at     TEXT,
  updated_at       TEXT NOT NULL,
  schema_version   INTEGER NOT NULL
);
CREATE TABLE evidence_aliases (      -- doi ↔ pmid ↔ pmcid ↔ nct ↔ canonical url
  alias      TEXT PRIMARY KEY,
  source_key TEXT NOT NULL REFERENCES evidence_sources(source_key) ON DELETE CASCADE
);
CREATE TABLE evidence_bundles (
  bundle_id TEXT PRIMARY KEY, topic_key TEXT NOT NULL, grade TEXT NOT NULL,
  review_status TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL, run_id TEXT
);
CREATE TABLE bundle_sources (
  bundle_id TEXT NOT NULL, source_key TEXT NOT NULL,
  role TEXT NOT NULL,                 -- primary|supporting|contradicting
  PRIMARY KEY (bundle_id, source_key)
);
CREATE TABLE evidence_usage (         -- usage is NOT a lifecycle state
  source_key TEXT NOT NULL, bundle_id TEXT, content_type TEXT NOT NULL,  -- article|tip
  content_id TEXT NOT NULL, used_at TEXT NOT NULL,
  PRIMARY KEY (source_key, content_type, content_id)
);
```

**Correction to the original lifecycle.** `used` was listed as a terminal state, but a record is used
many times over its life. Usage is a join table; `state` stays a pure acquisition/review lifecycle,
and `retraction_state` / `superseded_by` are orthogonal flags. Without this split, item 5 deduplication
and item 12 maintenance fight the state machine.

**Deduplication** is by `source_key` after alias resolution: a DOI, its PMID, its PMCID and the
publisher URL all collapse to one record. `topic_key` clusters by normalised
intervention + outcome + population.

## 6. Scientific review is not editorial review

- **Evidence Reviewer** (item 3) — is it scientifically justified?
- **Editor / Tip Editor** — is it clear, useful, concise, non-repetitive? The existing five-point tip
  rubric is right for this stage and stays.

Editors receive a **frozen claim set**. They may cut a claim or soften wording; they may not add a
claim, a number, or a source. After editing, G2, G4 and G8 run again over the edited text (see the
architecture diagram). A re-check failure is treated as an editorial rejection, not a scientific one:
regenerate the edit, keep the bundle.

## 7. Ranking, in code

Replace newest-first selection with an explicit, testable score in
`app/services/evidence/ranking.py`:

```python
score = (W_STRENGTH   * grade_weight          # high 1.0 … preliminary 0.2
       + W_NOVELTY    * novelty               # 0 if topic used within cooldown
       + W_RECENCY    * recency_decay         # half-life LIVEON_RECENCY_HALFLIFE_DAYS (default 21)
       + W_IMPORTANCE * topic_priority        # from LIVEON_TOPIC_PRIORITIES
       - W_REDUNDANCY * overlap_with_recent)
```

Weights are module constants with a docstring justifying each, and a test asserts the property the
original document asked for: *a human meta-analysis outranks a mouse study published six hours later.*
Ties break on grade, then on source count, then on `source_key` for determinism.

## 8. Show the evidence

Extend the publication surface rather than flattening back to prose plus URLs.

- **Article detail** ([app/templates/articles/detail.html](app/templates/articles/detail.html)) gains an
  evidence block: grade, one-line summary, study types, primary sources with links, and the material
  limitations carried from the bundle.
- **List views and tips** get a compact badge — `Evidence: Moderate`.
- Reader-facing wording is generated in code from the bundle, not by a model:
  `"Moderate — one human RCT plus supporting observational evidence"`.

**Legacy content decision.** Everything published before this work has no evidence record. Rather than
hide the archive, grandfather it: content with no `evidence_bundle_id` renders
`Evidence: not assessed — published before evidence review`, and is excluded from item 13 retrieval.
`LIVEON_HIDE_LEGACY=1` hides it entirely for anyone who prefers that.

## 9. Fail closed, and mean it

**Problem.** `_run_article_pipeline` returns `True` when nothing was published and `False` only on
error; `_execute` stamps `last_run` only on success, on an hourly tick. So "nothing new today"
advances the cadence, and "feeds are down" retries every hour forever. Six named states cannot be
expressed through one boolean.

**Change.** Runners return an outcome, and the scheduler holds an explicit policy table.

```python
class RunOutcome(StrEnum):
    PUBLISHED             = "published"
    NO_NEW_EVIDENCE       = "no_new_evidence"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    REVIEW_REJECTED       = "review_rejected"
    RETRIEVAL_FAILED      = "retrieval_failed"
    SOURCE_UNAVAILABLE    = "source_unavailable"
    MODEL_FAILED          = "model_failed"
```

| Outcome | Stamp `last_run`? | Retry | Notes |
|---|---|---|---|
| `PUBLISHED` | yes | next cadence | |
| `NO_NEW_EVIDENCE` | yes | next cadence | A quiet day is a successful day. |
| `EVIDENCE_INSUFFICIENT` | yes | next cadence | Fail-closed working as designed, logged at INFO. |
| `REVIEW_REJECTED` | yes | next cadence | Counted; `k` consecutive raises a WARN (possible prompt/model regression). |
| `RETRIEVAL_FAILED` | no | backoff | |
| `SOURCE_UNAVAILABLE` | no | backoff | |
| `MODEL_FAILED` | no | backoff | |

`JobConfig.runner` becomes `Callable[[datetime], RunOutcome]`, with a `bool` adapter so existing tests
and any external caller keep working. Backoff is exponential from 15 minutes, capped at the cadence
interval, stored as a new nullable `retry_at` column on `pipeline_schedule` — added by an
`ALTER TABLE` guarded by `PRAGMA table_info`, since `CREATE TABLE IF NOT EXISTS` will not add it to an
existing database. Without the backoff a failing job hammers NCBI hourly, which is exactly the
behaviour their rate-limit policy exists to prevent.

**Presets are deleted from the runtime path.** `_DEFAULT_PRESETS` and
`DailyTipContextProvider._build_from_presets` move to `app/tests/fixtures/tip_presets.py`. A tip run
with no usable evidence returns `NO_NEW_EVIDENCE` and publishes nothing. This is the fix for the
"20-30g of protein … next 6 hours" claim, and it is the one change that should ship first if anything
here ships alone.

---

# P2 — Making integrity measurable

## 10. Observability and reproducibility

A `run_id` (UUID) is created at the top of every pipeline run and threaded through every stage. The
codebase already logs with `extra={"event": ...}`; this adds structure and persistence.

```sql
CREATE TABLE pipeline_runs (
  run_id TEXT PRIMARY KEY, job TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  outcome TEXT, model_id TEXT, prompt_versions TEXT, data TEXT
);
CREATE TABLE run_events (
  run_id TEXT NOT NULL, seq INTEGER NOT NULL, stage TEXT NOT NULL,
  event TEXT NOT NULL, data TEXT, at TEXT NOT NULL, PRIMARY KEY (run_id, seq)
);
```

Recorded per run: sources considered, candidate ranking with scores, extraction results, every gate
violation, the computed grade and the reviewer decision, dropped claims, model and prompt versions,
the publication decision, and errors.

**Separate the three timestamps** the code currently conflates: `source_published_at` (when the study
appeared), `retrieved_at` (when we fetched it), and `published_date` (when Live On published). The
article pipeline currently reuses the feed timestamp for its own publication date.

Retention: `LIVEON_RUN_RETENTION_DAYS` (default 365), pruned on scheduler start.

## 11. The evidence benchmark

**Test hygiene first, or none of this is trustworthy:**

- Add an autouse `conftest.py` fixture that fails any test making a real network call (patch the
  `httpx` transport). Without it, 362 existing tests plus new research clients will quietly hit the
  internet in CI.
- The corpus is **checked-in offline fixtures** — real PubMed/Europe PMC payloads under
  `app/tests/fixtures/corpus/`, one directory per case, each with the record and its expected labels.
- Invariants are asserted with **stub LLMs**, so they are deterministic. Model-in-the-loop runs are a
  separate, optional marker (`@pytest.mark.live`), excluded from CI.

Corpus cases: strong human RCT · observational association · animal study · in-vitro · systematic
review · meta-analysis · tiny exploratory study (n<30) · preprint · surrogate-endpoint study ·
contradictory pair · retracted paper · corrected paper · news article overstating its primary source ·
statistically significant but trivially small effect · a model attempting to invent a citation.

Required invariants — each one test, each named for the gate it protects:

1. A `source_key` absent from the store never reaches publication. (G1)
2. Every number in published text resolves to a verifying span. (G2, I2)
3. An animal study is labelled animal evidence and cannot carry human-population language. (G3)
4. Observational evidence never becomes causal, including after editing. (G4, post-edit re-check)
5. A retracted record blocks publication and retro-flags anything already using it. (G6, item 12)
6. `not_extractable` design or subject yields `insufficient` and no publication. (G10, I3)
7. A reviewer rejection prevents publication. (I5)
8. A model-returned grade above the computed grade is discarded. (I4)
9. Tip provenance survives persistence and reload. (item 4)
10. Contradictions are surfaced in the bundle, not averaged away. (item 2b)
11. Claim-ceiling constructions are rejected at any grade. (G8)
12. A meta-analysis outranks a newer mouse study. (item 7)
13. Every failure state publishes nothing. (item 9, parametrised over `RunOutcome`)

**CI gate.** `pytest -m "not live"` must pass, and the benchmark is a required check. A prompt edit
that lowers integrity fails the build.

---

# P3 — Continuous maintenance

## 12. Keep published claims true over time

With no human in the loop, this is the only correction mechanism the system has, so it is not
optional garnish.

- **Retraction and correction sweep** (weekly job): re-query every `source_key` used by published
  content for `RetractionIn`, `ErratumIn`, or expression-of-concern links. On a hit, set
  `retraction_state`, find affected content via `evidence_usage`, and either unpublish or stamp a
  visible correction notice, according to `LIVEON_RETRACTION_POLICY` (default `annotate`).
- **Supersession:** a newer systematic review on the same `topic_key` sets `superseded_by` on the
  bundles it replaces and lowers their ranking weight.
- **Consensus drift:** when new records reverse the direction of a published claim, the topic is
  queued as a candidate article — a contradiction is itself the story.
- **Repetition control:** `evidence_usage` plus `topic_key` prevents re-reporting the same finding
  under a new headline.

## 13. Bound the coach (new)

[coach.py](app/services/coach.py) does no retrieval — it is a system prompt plus conversation history,
answering personalised health questions live. It is the highest-risk surface in the product and the
original document mentioned it once, in passing. Building a rigorous gate for passive reading while
the interactive channel stays ungoverned is the wrong order of work.

Minimum scope for this phase:

1. **Retrieval over approved evidence.** Coach answers draw on approved bundles and published,
   evidence-backed content; legacy content is excluded.
2. **The claim ceiling applies** (section 0.2), enforced on the streamed response by the same lexical
   rules used at publication.
3. **Uncertainty is stated, not smoothed.** With no supporting bundle above `low`, the coach says so
   and declines to specify. "I do not have good evidence on that" is a correct answer.
4. **Refusal paths** for dosing, diagnosis and medication questions, routed to a standing referral
   line rather than improvised per conversation.

Sequenced in slice 5, but it is P1 in priority: it should not be the last thing built.

---

# Delivery sequence

Eleven items is a pipeline rewrite, and a big-bang cutover would leave the site unpublishable for the
duration. Each slice ships behind `LIVEON_EVIDENCE_PIPELINE` (default `0` until slice 4), with the
existing prose path intact and passing its tests throughout.

| Slice | Contents | Done when |
|---|---|---|
| **1 — Spine** | `app/models/evidence.py`, store (item 5 schema), `research/` clients with cache and rate limiting, extractor (2a), G1/G2/G6/G10, ID-based provenance (item 4). | A record can be discovered, acquired, extracted, stored, and cited by key; no claim with an unresolvable source or unanchored number can be built. |
| **2 — Judgement** | Synthesizer (2b), full G1–G10, grade rubric, reviewer (item 3), outcome states and scheduler policy (item 9), **preset deletion**. | The tip pipeline runs end to end on real evidence and publishes nothing when evidence is missing. |
| **3 — One pipeline** | Clustering, ranking (item 7), article path on the same store, editorial separation and post-edit re-check (item 6). | Articles and tips are generated from the same reviewed bundles; ranking tests pass. |
| **4 — Surface** | Publication fields and templates (item 8), observability (item 10), benchmark in CI (item 11). Flip `LIVEON_EVIDENCE_PIPELINE` to `1`. | A reader can see the grade; a run is fully reconstructable; CI enforces the invariants. |
| **5 — Upkeep** | Maintenance sweeps (item 12), coach binding (item 13). | A retraction upstream changes what the site says without anyone intervening. |

**Latency budget.** Everything runs on one local Ollama instance. The tip loop is already up to three
generate+review cycles; adding extraction and evidence review to each could push a run past the
scheduler lock TTL (`max(check_interval*2, 3600)`). Therefore:

- Extraction is cached per `(source_key, prompt_version, model_id)` and never repeated within a run.
- Per-run wall-clock budget `LIVEON_RUN_BUDGET_SEC` (default 1800); on expiry the run ends as
  `MODEL_FAILED` and backs off rather than holding the lock.
- Agent models are configured separately via the existing `LIVEON_<AGENT>_MODEL` convention in
  [llm_factory.py](app/services/llm_factory.py). Extraction and review are JSON-structured and should
  use `json_mode=True` with the largest model the host can run; editorial work can use a smaller one.

---

# Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LIVEON_EVIDENCE_PIPELINE` | `0` → `1` at slice 4 | Master switch for the new path. |
| `LIVEON_RESEARCH_QUERIES` | built-in set | JSON list of literature queries. |
| `LIVEON_NCBI_API_KEY` | unset | Raises the E-utilities limit from 3 to 10 req/s. |
| `LIVEON_NCBI_EMAIL` | unset | Required contact for E-utilities politeness. |
| `LIVEON_RESEARCH_CACHE_TTL_HOURS` | `168` | Research response cache lifetime. |
| `LIVEON_NEWS_RESOLUTION` | `0` | Opt-in news→DOI resolution. |
| `LIVEON_TOPIC_COOLDOWN_DAYS` | `30` | G9 repetition window. |
| `LIVEON_MAX_REGENERATIONS` | `2` | Reviewer regeneration cap. |
| `LIVEON_RECENCY_HALFLIFE_DAYS` | `21` | Ranking recency decay. |
| `LIVEON_TOPIC_PRIORITIES` | unset | JSON map of topic → importance weight. |
| `LIVEON_RUN_BUDGET_SEC` | `1800` | Per-run wall-clock ceiling. |
| `LIVEON_RUN_RETENTION_DAYS` | `365` | Run-log retention. |
| `LIVEON_RETRACTION_POLICY` | `annotate` | `annotate` or `unpublish`. |
| `LIVEON_HIDE_LEGACY` | `0` | Hide pre-evidence content instead of badging it. |

---

# Decisions taken, so implementation does not stall

1. **PubMed/Europe PMC are primary; news is a signal.** News resolution is off by default and an
   unresolved signal is dropped.
2. **Abstract-only is the normal case.** Missing fields are `not_reported` / `not_extractable` and cap
   the grade. They are never inferred.
3. **The reviewer is code first.** The LLM layer is advisory and downgrade-only.
4. **No human in the loop, by design.** The compensating controls are section 0.1; the claim ceiling
   in 0.2 is the price of that decision.
5. **Legacy content is grandfathered with a badge**, not hidden or retro-graded.
6. **Usage is a join table, not a lifecycle state.**
7. **Presets are deleted from the runtime path**, retained only as test fixtures.
8. **No new runtime dependencies**; `httpx` plus stdlib covers every API used here.
9. **The benchmark runs offline with stub models** and gates CI; live-model runs are opt-in.
