# Live On — Improvement Backlog

**Scope of review:** README intent vs. current implementation, focused on *stability*, *user
friendliness*, *UX design*, and *AI functionality*.
**Reviewed at:** commit `a73df50` (branch `no_google`), 2026-08-29.
**Verification performed:** full test suite run (`52 passed`), event-loop blocking reproduced,
repository construction cost measured, 404 behaviour probed. Findings marked *(measured)* were
reproduced, not inferred.

The README's promise is a locally-hosted, offline-friendly longevity coach: agentic content
pipelines feeding a public site, plus a conversational coach on a local Ollama model. The
building blocks are all there and the agent code is thoughtfully structured. What is missing is
mostly the layer between "it runs on my machine once" and "it survives a user session": the
coach blocks the whole server, tips are effectively static, and the admin console is open to
anyone who can reach the site.

---

## Status

**P0 (items 1–5): implemented and verified — 2026-08-29.** The test suite went from 52 to 101
passing; the two headline regression tests were each confirmed to fail when their fix is reverted.
Verified live against a real Ollama server: during a 24.2-second `qwen2.5:14b` generation, 46/46
concurrent `/healthz` checks were served at sub-millisecond latency and full pages kept rendering.

One finding emerged during the work and is folded into item 2 below: `langchain-community` passes
its `timeout` to a *streaming* `requests.post`, where it bounds the gap between tokens rather than
total duration — so it alone is not a ceiling. A wall-clock deadline in the endpoint supplies the
real bound.

P1–P3 remain open and unchanged.

---

## P0 — Stability and safety. Fix before anyone else uses this.

### 1. The entire site freezes while the coach is generating *(measured)* — ✅ FIXED

[main.py:335-354](app/main.py#L335-L354) declares `ask_coach_endpoint` as `async def` but calls
the synchronous `agent.ask(question)` directly on the event loop. Nothing else is served until
the model finishes.

Reproduced: with a stub agent that takes 3 s, a `GET /healthz` issued 0.5 s into the request
could not even be *dispatched* until t=3.01 s. With `phi3:14b` on CPU a real answer is 30–120 s,
so every other visitor sees a hung site for the duration — and the Kubernetes `livenessProbe`
([deployment.yaml:86-91](deployment.yaml#L86-L91), default `timeoutSeconds: 1`,
`failureThreshold: 3`, `periodSeconds: 20`) will fail three checks and **restart the pod
mid-answer**.

**Fix:** `answer = await asyncio.to_thread(agent.ask, question)` (or drop `async`, letting
Starlette use its threadpool). Then raise `timeoutSeconds` on the probe. This one change is the
single largest stability win available.

**Fixed.** The handler now runs the model call in a worker thread under a wall-clock deadline, and
the probes carry explicit `timeoutSeconds`/`failureThreshold`. Verified live with `qwen2.5:14b`:
during a 24.2 s generation, 46/46 concurrent `/healthz` checks were served at sub-millisecond
latency and `/tips` kept rendering. The regression test in
`app/tests/test_coach_endpoint_resilience.py` was confirmed to fail when the fix is reverted.

### 2. No timeout on the coach model call — ✅ FIXED

The LangChain path builds `ChatOllama(model=model, base_url=base_url)` with no timeout
([coach.py:240](app/services/coach.py#L240)). A wedged or swapping Ollama daemon holds the
request — and, per #1, the whole server — indefinitely. The fallback HTTP client
([coach.py:160](app/services/coach.py#L160)) hard-codes 30 s, which is *too short* for the
default 14B model. The two paths fail in opposite directions.

**Fix:** one `LIVEON_LLM_TIMEOUT` (suggest 180 s) applied to both paths; map expiry to `504`
with a "the local model is still warming up" message rather than a generic failure.

**Fixed.** `LIVEON_LLM_TIMEOUT` (default 180 s) now feeds both client paths, and — because the
`langchain-community` timeout only bounds the gap between streamed tokens — a wall-clock deadline
in the endpoint (`_run_with_deadline`) supplies the real ceiling. `asyncio.wait_for` is unusable
here: it awaits the cancelled task, and an uninterruptible thread makes that a full wait. The
worker is abandoned instead. Expiry returns `504`. Verified live: a 2 s ceiling cut a ~10 s
generation at 2.24 s and the server stayed healthy.

### 3. The admin console is public, unauthenticated, and destructive — ✅ FIXED

`GET /admin` plus `POST /admin/{articles,tips}/{id}/delete`
([main.py:448-495](app/main.py#L448-L495)) have no authentication, no CSRF token, and no
confirmation step — and [base.html:110](app/templates/base.html#L110) links "Admin" in the
global nav for every visitor. Any crawler that follows a form, or any visitor who clicks, can
permanently delete content. `delete_article` also cascades `article_sources`, so the URL
de-duplication history goes with it and the pipeline will happily re-publish the deleted story.

**Fix:** put the console behind auth (HTTP Basic from an env-set credential is enough for this
deployment), drop the nav link, add a confirm step, and consider soft-delete so a mistake is
recoverable.

**Fixed.** HTTP Basic auth from `LIVEON_ADMIN_USER`/`LIVEON_ADMIN_PASSWORD`, compared with
`secrets.compare_digest` (both fields always, so timing reveals nothing). **The console is disabled
— `503` — until a password is set**, so an unconfigured deployment is off rather than open. Deletes
additionally require a same-origin `Origin`/`Referer` (Basic credentials are cached by the browser,
so cross-site POSTs are the real risk) and a confirmation dialog, and are logged with the actor.
The nav link is gone. 20 tests in `app/tests/test_admin_security.py`.

### 4. Only `RuntimeError` is handled around the model call — ✅ FIXED

[main.py:355](app/main.py#L355) catches `RuntimeError`, but a down Ollama raises
`httpx.ConnectError` / LangChain transport errors. Those escape as an unstyled `500 Internal
Server Error` — the most common real-world failure mode is the one path that is *not* handled,
and the user is told nothing actionable.

**Fix:** catch `Exception`, and distinguish connection refused (`503`, "coach offline — is
`ollama serve` running?") from timeout (`504`) from everything else (`500`).

**Fixed.** `CoachAgent.ask` now funnels every client failure through `classify_llm_error`, which
walks the exception chain and matches on the naming conventions `httpx` and `requests` share —
avoiding a hard dependency on either hierarchy. Connection failures → `503`, timeouts → `504`,
other model failures → `503`, genuine bugs → `500`. Verified against the real stack: with Ollama
unreachable the endpoint returns `503` where it previously returned an unstyled `500`.

### 5. Internal exception text is shipped to the browser and rendered in the chat — ✅ FIXED

`_build_debug_detail` ([main.py:104](app/main.py#L104)) puts the exception `type` and `message`
into the HTTP response, and the coach UI prints them into the transcript under "Debug
information:" ([coach.html:554](app/templates/coach.html#L554)). This leaks internal
paths/hostnames, and from a user's perspective a wellness coach answering a sleep question with
a Python exception is alarming.

**Fix:** log server-side with a short correlation id, return the id to the user, and gate the
debug payload behind `LIVEON_DEBUG_ERRORS=1`.

**Fixed.** Responses now carry `message` and a 12-character `reference`; the exception and full
traceback go to the server log under that same reference. Confirmed end-to-end: the browser payload
contains no traceback, host, port, or library name, while the log holds the complete chain.
`LIVEON_DEBUG_ERRORS=1` restores inline details for development. The coach UI shows the reference
instead of a Python exception.

---

## P1 — AI functionality. This is what makes the product feel like a coach.

### 6. The coach has no conversation memory

The UI is a transcript, so users will naturally ask "and what about after 50?" — but every
`/api/ask` builds a fresh two-message prompt from the single question
([coach.py:107-131](app/services/coach.py#L107-L131)). `CoachQuestion.include_history` exists
([models/coach.py:14](app/models/coach.py#L14)) and is never read anywhere in the codebase. The
interface promises continuity the model cannot deliver; follow-ups get confidently wrong answers.

**Fix:** accept an optional `history` array in `AskCoachRequest`, cap it server-side (last ~6
turns / N characters), and fold it into the prompt. The client already holds the transcript.

### 7. No streaming — 30–120 seconds of dead air

The only feedback is the submit button changing to "Sending…"
([coach.html:394-404](app/templates/coach.html#L394-L404)). There is no typing indicator in the
transcript, no elapsed timer, no cancel, and no client-side `fetch` timeout — a dropped
connection leaves the button disabled forever. Most first-time users will assume it is broken
and reload.

**Fix (highest UX leverage after P0):** Ollama supports `stream: true`; expose `/api/ask/stream`
as SSE and render tokens as they arrive. If that is too large a step, ship the cheap version
first: a pulsing "Coach is thinking…" bubble, an elapsed-seconds counter, an `AbortController`
with a Cancel button, and a client timeout.

### 8. Health answers ship with an empty disclaimer

`_DEFAULT_DISCLAIMER = ""` ([coach.py:29](app/services/coach.py#L29)), while
`AskCoachResponse.disclaimer` is documented as the "Safety disclaimer appended to every
response" ([main.py:180](app/main.py#L180)). In practice the field is empty unless the model
happens to emit the literal token `Disclaimer:`, so the styled disclaimer block in the
transcript almost never renders. For a product giving health guidance the per-answer disclaimer
should not be luck-of-the-draw — the site footer is not the same thing.

**Fix:** set a real default (one sentence: educational only, consult a professional).

### 9. `_separate_disclaimer` can truncate a real answer

[coach.py:247-260](app/services/coach.py#L247-L260) does `lower.rfind("disclaimer:")` anywhere
in the text and discards everything after it. An answer that legitimately mentions "…the
supplement label disclaimer: …" loses its tail into the disclaimer box.

**Fix:** only split on a `Disclaimer:` that starts its own line near the end — or better, ask the
model for JSON and stop scraping prose.

### 10. "Tips" are three hard-coded presets on a 3-day rotation

`DailyTipContextProvider` picks `presets[today.toordinal() % 3]`
([tip_context.py:16-94](app/services/tip_context.py#L16-L94)) from three literal dicts. The tip
pipeline therefore sees identical research notes every third day and never touches news
aggregation at all. Two consequences:

- The README states both pipelines "share the same aggregation pool" — they do not. The site's
  "Tip of the Day" is, at best, a three-item carousel.
- The `TipEditorAgent` rubric explicitly rejects repetition
  ([tip_editor.py:19-25](app/services/tip_editor.py#L19-L25)) while the generator is fed the same
  three note sets. Once a handful of tips exist, runs should increasingly burn all three attempts
  and fail — the editor-in-the-loop is fighting its own input.

**Fix:** feed the tip generator from `LongevityNewsAggregator` (the article pipeline already does
the URL-dedup work), keep the presets only as an offline fallback, and pass recent tip *bodies* —
not just titles ([tip_editor.py:154-170](app/services/tip_editor.py#L154-L170)) — into the
novelty check.

### 11. Default cadences contradict the product

[pipeline_scheduler.py:245-246](app/services/pipeline_scheduler.py#L245-L246): articles every
**7 days**, tips every **1 month** — under a homepage that says "Tip of the Day". Worse,
`ensure_initialized` ([pipeline_scheduler.py:111](app/services/pipeline_scheduler.py#L111))
stamps `last_run = now` on first boot, so a fresh install generates *nothing* for a week
(articles) or a month (tips). The scheduler is on by default and undocumented, so this is the
out-of-box experience.

**Fix:** tips daily, articles daily or every other day; on first boot run once immediately rather
than starting the clock; document the knobs; add a "Run now" button to the (now-authenticated)
admin console so the first run isn't a CLI ritual.

### 12. The tip CLI can't reach Ollama, and one env var breaks it outright

- `--model-provider` allows only `["local", "openai", "gpt"]`
  ([run_tip_pipeline.py:82](app/scripts/run_tip_pipeline.py#L82)) although `_create_tip_llm`
  implements an `ollama` branch — the README's "you can still select OpenAI/Ollama providers" is
  not reachable from the CLI.
- The argparse *default* comes from `LIVEON_TIP_MODEL` **or `LIVEON_SUMMARIZER_MODEL`**
  ([run_tip_pipeline.py:73-75](app/scripts/run_tip_pipeline.py#L73-L75)). Setting
  `LIVEON_SUMMARIZER_MODEL=ollama` — exactly what you do to configure the article pipeline —
  makes the tip CLI abort with `invalid choice: 'ollama'`.
- `--allow-local-llm` is threaded into `_create_tip_llm(allow_local_stub=…)` and never read: the
  flag documented in the README does nothing.
- The tip Ollama branch imports the deprecated `langchain_community.chat_models.ChatOllama`
  directly and sets neither `format="json"` nor `temperature`, unlike the article path
  ([run_pipeline.py:160-186](app/scripts/run_pipeline.py#L160-L186)) which carefully does both.
  The tip agents demand strict JSON while running at Ollama's default temperature of 0.8.

**Fix:** add `ollama` to `choices`, share one `_create_llm` helper between both scripts, and
either implement or delete `--allow-local-llm`.

### 13. Four copies of the JSON-repair code, and no retry when it fails

`_parse_payload` / `_strip_code_fence` / `_scan_for_object` / `_try_parse_mapping` are duplicated
near-verbatim in [summarizer.py](app/services/summarizer.py#L98-L160),
[editor.py](app/services/editor.py#L131-L204),
[tip_generator.py](app/services/tip_generator.py#L178-L237) and
[tip_editor.py](app/services/tip_editor.py#L218-L279) — four places to fix the next parser bug.
And when parsing does fail, the article pipeline gives up on the whole run: one malformed response
from a small local model discards all the aggregation work.

**Fix:** extract `app/utils/json_repair.py`, and add one bounded re-ask ("your last reply was not
valid JSON, return only the object") before failing — the tip pipeline already proves the
feedback-loop pattern works.

---

## P2 — User friendliness and UX design

### 14. Broken links show raw JSON to the user *(measured)*

`GET /articles/does-not-exist` returns `application/json` `{"detail":"Article not found"}`, and
any unknown path returns `{"detail":"Not Found"}` — no header, no nav, no way back.

**Fix:** an `HTTPException` handler that renders a styled 404/500 template for HTML requests and
keeps JSON for `/api/*`.

### 15. Stale copy contradicting shipped features

- Home hero: "and **soon** chat with an AI longevity coach" ([home.html:9](app/templates/home.html#L9)) — it shipped.
- Admin: "Removal actions will be available soon" ([admin.html:6](app/templates/admin.html#L6)) sits directly above working Delete buttons.
- `/coach` route docstring: "placeholder page for the future interactive coach experience" ([main.py:434](app/main.py#L434)).

### 16. Chat rendering emits invalid HTML and duplicates the server's markdown

[coach.html:478](app/templates/coach.html#L478) assigns block-level markup (`<p>`, `<ul>`, `<ol>`)
into `body.innerHTML` where `body` is a `<p>` element — browsers silently close the paragraph and
the nesting and spacing degrade. Separately, the hand-rolled `renderMarkdown`
([coach.html:331](app/templates/coach.html#L331)) is a second, weaker markdown implementation that
will drift from the server's `markdown_to_html` filter.

**Fix:** render into a `<div class="coach-message-body">`; consider having the API return
pre-rendered, sanitized HTML so there is one renderer.

### 17. The conversation is disposable

Reload and the transcript is gone. There is no Clear, no Copy, no Retry, no way to keep a useful
answer. For a coach whose answers take a minute to produce, losing them to an accidental refresh
is a genuine frustration.

**Fix:** `sessionStorage` for the transcript, plus copy/clear controls. (Named sessions are the
natural follow-on once #6 lands.)

### 18. Offline-first claim broken by a CDN stylesheet

[base.html:14-17](app/templates/base.html#L14-L17) loads Pico CSS from jsdelivr. The README's
whole pitch is "iterate offline while keeping sensitive data on the developer machine" — on a
plane or an air-gapped host the site renders unstyled. No dark mode either, despite Pico
supporting it and the audience (evening sleep questions) wanting it.

**Fix:** vendor Pico into `app/static/`, add a `prefers-color-scheme` palette.

### 19. Content browsing has no depth

`/articles` and `/tips` are hard-capped at 20 with no pagination, no tag filter, and no search
([main.py:381](app/main.py#L381), [main.py:414](app/main.py#L414)) — item 21 is unreachable
forever. Tags are stored and displayed but not clickable. The article detail page
([articles/detail.html](app/templates/articles/detail.html)) shows no summary, no key-takeaways
block, and no link back to the list.

### 20. Deleting content takes one click, with no confirmation and no undo

See #3. Even after auth is added, this needs a confirm step.

### 21. Question length is unbounded

`question: str = Field(...)` ([main.py:163](app/main.py#L163)) has no `max_length`. A pasted
novel becomes a multi-minute generation that (per #1) freezes the site for everyone. Add
`max_length` (~2000) with a live character counter on the textarea, plus simple per-IP rate
limiting on `/api/ask`.

---

## P3 — Correctness, hygiene, and documentation

### 22. The article pipeline hides publisher failures and exits 0

[pipeline.py:173](app/services/pipeline.py#L173) has `errors.append(f"Publisher failed: {exc}")`
commented out in favour of a bare `logging.exception`. The result is a `PipelineResult` with no
publication *and no errors*, so `run()` reports "Pipeline finished without producing content" and
**returns exit code 0** ([run_pipeline.py:378-383](app/scripts/run_pipeline.py#L378-L383)). A
CronJob or the in-app scheduler treats a broken publisher as a successful no-op — and the
scheduler then stamps `last_run`, silently skipping the next window. Restore the append.

### 23. Every page view opens a new database *(measured)*

`get_repository()` ([main.py:272](app/main.py#L272)) is a per-request FastAPI dependency that
constructs a `LocalSQLiteContentRepository`, which `mkdir`s, opens a connection, sets two PRAGMAs
and re-runs six `CREATE TABLE/INDEX IF NOT EXISTS` statements — per request
([sqlite_repo.py:143-200](app/services/sqlite_repo.py#L143-L200)). Measured at **~100× the cost of
a shared connection** (1.08 ms vs 0.010 ms per query locally; worse on the Kubernetes PVC).

**Fix:** build one repository at startup, store it on `app.state`, and have the dependency return
it.

### 24. Rendered markdown is never sanitized

`markdown_to_html` returns `Markup(...)` unescaped
([utils/text.py:57-64](app/utils/text.py#L57-L64)) over content whose two upstreams are RSS feeds
and an LLM. The `markdown` library passes raw HTML through by default, so a `<script>` in a feed
item or a hallucinated `<img onerror=…>` becomes stored XSS on the article page.

**Fix:** a `bleach`/`nh3` allowlist pass before `Markup`.

### 25. Deprecated APIs that will break on the next upgrade

- `@app.on_event("startup"/"shutdown")` ([main.py:113-130](app/main.py#L113-L130)) — FastAPI already warns; migrate to `lifespan`.
- `datetime.utcnow()` ([sqlite_repo.py:120](app/services/sqlite_repo.py#L120)) — deprecated in 3.12, and returns a *naive* timestamp into a codebase that is otherwise carefully tz-aware.
- `from langchain_community.chat_models import ChatOllama` ([coach.py:14](app/services/coach.py#L14)) — deprecated in favour of `langchain-ollama`, which the README already tells users to install but which the coach never imports. `run_pipeline._resolve_chat_ollama` gets this right; copy it.

### 26. Dependency and packaging gaps

`app/requirements.txt` pins everything except `langchain-community` (unpinned — a breaking release
lands silently), omits `langchain-ollama` despite the README recommending it, and ships `pytest`
into the production image ([Dockerfile:29](Dockerfile#L29)). Split dev requirements out.

### 27. Repository hygiene

Tracked build/run artifacts: `uvicorn.log`, `data/results.db`,
`data/embeddings/**/ad_full.npy`, and `controller_manifest.json` (which embeds absolute local
paths under `C:\Users\stefa\`). `controller_manifest.json.bak` is untracked clutter. `.gitignore`
is missing a trailing newline, so its last line reads `refactor_plan.mdapp/tests/__pycache__/` —
**both** of those patterns are inert. Add `.venv/`, `__pycache__/`, `.pytest_cache/`, `*.db`,
`*.log`.

### 28. README drift

Beyond #10 and #12: the README says the PVC mounts at `/root/liveon/data` while
[deployment.yaml:73](deployment.yaml#L73) mounts `/home/appuser/liveon/data`; `LIVEON_STORAGE` is
documented as accepting `memory` but [main.py:274](app/main.py#L274) only special-cases `sqlite`
(memory works, but only via the "unsupported storage type" warning path); and roughly a dozen live
environment variables are undocumented — `LIVEON_ROOT_PATH`, `LIVEON_COACH_PROMPTS`,
`LIVEON_ENABLE_SCHEDULER`, `LIVEON_DISABLE_SCHEDULER`, `LIVEON_ARTICLE_INTERVAL_DAYS`,
`LIVEON_TIP_INTERVAL_MONTHS`, `LIVEON_PIPELINE_CHECK_INTERVAL_SEC`, `LIVEON_FEED_LIMIT`,
`LIVEON_FEED_SOURCES`, `LIVEON_FEED_HEADERS`, `LIVEON_FEED_TIMEOUT`, `LIVEON_MODEL_TEMPERATURE`,
`LIVEON_OLLAMA_FORMAT`, `LIVEON_TIP_CONTEXT_PRESETS`, `LIVEON_LOG_LEVEL`. The in-app scheduler is
not mentioned in the README at all, which matters since it is on by default.

### 29. Test coverage gaps

52 tests pass, but nothing covers the admin routes (including the destructive ones),
`LocalSQLiteContentRepository`, `PipelineScheduler`, the `/articles` routes, or the coach's
non-`RuntimeError` failure path. Note that `scheduler_enabled()` returns `False` under
`PYTEST_CURRENT_TEST` ([pipeline_scheduler.py:172](app/services/pipeline_scheduler.py#L172)) —
convenient, but it means the default-on production behaviour is never exercised.

### 30. Smaller items

- `LongevityNewsAggregator` creates an `httpx.Client` it never closes ([aggregator.py:60](app/services/aggregator.py#L60)); no context manager, no `close()`.
- `_TRACKING_PARAM_PREFIXES` / `_TRACKING_PARAM_NAMES` ([aggregator.py:23-24](app/services/aggregator.py#L23-L24)) are dead — only the `utm_` prefix is actually stripped, so `fbclid`/`gclid` variants of the same link still slip past de-duplication.
- `SupportsTipGeneration.generate(items, feedback)` ([pipeline.py:197](app/services/pipeline.py#L197)) no longer matches the real call `generate(context=…, feedback=…)` ([pipeline.py:314](app/services/pipeline.py#L314)) — a stale protocol a type checker would flag.
- The scheduler is per-process: running uvicorn with `--workers N` gives N schedulers racing on the same SQLite `pipeline_schedule` row. Guard with a lock or move to a CronJob.
- `run_pipeline` logs `PIPELINE_START` at *import* time ([run_pipeline.py:56](app/scripts/run_pipeline.py#L56)), so the web app logs a pipeline start whenever the scheduler imports the module.
- `ContentPipeline.run` publishes exactly one article per invocation regardless of `--feed-limit`, which only widens the candidate pool. Worth documenting, or making it explicit with a `--max-articles`.
- No `/api/articles` endpoint although `/api/tips/latest` exists — an asymmetric public API.

---

## Suggested sequence

**Week 1 — make it survivable. ✅ Done** for #1–#5 (see Status above); #1 alone changed the app
from single-user to multi-user. **#22 (stop hiding pipeline failures) is still outstanding** — it
was grouped here because it is a one-line fix with the same "silent failure" character, but it sits
in the pipeline rather than the request path, so it was not part of the P0 set.

**Week 2 — make it feel like a coach.** #7 (streaming, or at minimum a thinking indicator and
cancel) → #6 (conversation memory) → #8 (real disclaimer) → #14 and #15 (404 page, stale copy).

**Week 3 — make the content real.** #10 (feed tips from the aggregator) → #11 (sane cadences and
first-run behaviour) → #12 and #13 (unify the LLM setup, add a JSON re-ask) → #23 (shared
repository).

Then P2/P3 as capacity allows. #24 (sanitization) should jump the queue if the site is ever
exposed beyond localhost.
