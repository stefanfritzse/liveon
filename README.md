# Live On – Longevity Coach Platform

Live On is an experimental platform that combines agentic content pipelines with a FastAPI web front-end. It aggregates longevity news, summarizes drafts, polishes the copy, and publishes both long-form articles and coaching tips. A conversational coach interface lets users ask questions that are answered with a locally hosted Ollama model, making it easy to iterate offline while keeping sensitive data on the developer machine.

## Key Components

- **FastAPI application (`app/main.py`)** – Serves the public site (home, articles, tips) plus the `/coach` interface and JSON APIs (`/api/ask`, `/api/tips/latest`, `/healthz`).
- **SQLite content repository (`app/services/sqlite_repo.py`)** – Stores articles and tips locally, mirroring the Firestore surface used in production. Falls back to in-memory seed data if a database is unavailable.
- **Agent pipelines (`app/services/pipeline.py`, `app/scripts/run_pipeline.py`, `app/scripts/run_tip_pipeline.py`)** – Article runs follow the Summarizer ➜ Editor ➜ Publisher chain, while the tip workflow adds a TipEditor gate that enforces a generate-review-refine loop before persisting to SQLite.
- **Coach agent (`app/services/coach.py`)** – Wraps LangChain + Ollama (or a direct HTTP client) to generate answers with the configured local model.
- **Deployment scripts (`deploy.ps1`, `deployment.yaml`, `service.yaml`)** – Automate build + apply steps for a Minikube cluster, including port-forwarding and health checks.

## Requirements

- Python 3.11
- pip / virtual environment of your choice
- SQLite (bundled with Python) for local storage
- [Ollama](https://ollama.com/) running locally (default: `http://127.0.0.1:11434`) for the coaching model
- Optional: Docker + Minikube if you want to run `deploy.ps1`

Install dependencies:

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r app/requirements.txt
```

`app/requirements.txt` holds the runtime dependencies, including the LangChain and
Ollama clients. To run the tests as well, install the development set instead — it
pulls in the runtime requirements and adds `pytest`:

```powershell
python -m pip install -r requirements-dev.txt
```

## Running the Web App

1. Ensure Ollama is running (`ollama serve`) and has the desired model pulled (default: `phi3:14b-medium-4k-instruct-q4_K_M`).
2. Start the API:

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

3. Visit `http://localhost:8080/` for the site or `http://localhost:8080/coach` for the conversational UI.

### Environment Variables

**Storage**

| Variable | Purpose | Default |
| --- | --- | --- |
| `LIVEON_STORAGE` | `sqlite` or `memory` (in-memory seed content, nothing persisted) | `sqlite` |
| `LIVEON_DB_PATH` | SQLite file path | `~/liveon/data/content.db` |

**Language model**

| Variable | Purpose | Default |
| --- | --- | --- |
| `LIVEON_LLM_PROVIDER` | `ollama`, `openai`, or `local` | `ollama` |
| `LIVEON_OLLAMA_MODEL` | Ollama model name | `phi3:14b-medium-4k-instruct-q4_K_M` |
| `LIVEON_OLLAMA_URL` | Ollama base URL (`OLLAMA_HOST` is also honoured) | `http://127.0.0.1:11434` |
| `LIVEON_OLLAMA_FORMAT` | Structured-output format for pipeline agents | `json` |
| `LIVEON_MODEL_TEMPERATURE` | Sampling temperature for pipeline agents | `0.2` |
| `LIVEON_LLM_TIMEOUT` | Seconds one coach answer may take before a `504` | `180` |
| `OPENAI_MODEL` | Model id when the provider is `openai` | `gpt-4o-mini` |

Each agent can override the shared choice with `LIVEON_<AGENT>_MODEL`,
`LIVEON_<AGENT>_OLLAMA_MODEL`, or `LIVEON_<AGENT>_OPENAI_MODEL`, where `<AGENT>` is
`SUMMARIZER`, `EDITOR`, or `TIP`.

**Coach**

| Variable | Purpose | Default |
| --- | --- | --- |
| `LIVEON_COACH_HISTORY_TURNS` | Earlier turns replayed into each prompt (0 disables memory) | `6` |
| `LIVEON_COACH_PROMPTS` | JSON list of conversation starters shown on `/coach` | built-in set |
| `LIVEON_ASK_RATE_LIMIT` | Questions allowed per client per minute (0 disables) | `30` |

**Admin console**

| Variable | Purpose | Default |
| --- | --- | --- |
| `LIVEON_ADMIN_USER` | Admin username | `admin` |
| `LIVEON_ADMIN_PASSWORD` | Admin password. **The console stays disabled until this is set.** | _(unset)_ |

**Scheduling and pipelines**

| Variable | Purpose | Default |
| --- | --- | --- |
| `LIVEON_ENABLE_SCHEDULER` | Run the pipelines inside the web process | `1` |
| `LIVEON_DISABLE_SCHEDULER` | Set to any value to turn the scheduler off | _(unset)_ |
| `LIVEON_ARTICLE_INTERVAL_DAYS` | Days between article runs, until an operator picks a cadence in the console | `1` |
| `LIVEON_TIP_INTERVAL_DAYS` | Days between tip runs, until an operator picks a cadence in the console | `1` |
| `LIVEON_TIP_INTERVAL_MONTHS` | Months between tip runs; overrides the daily setting when > 0 | `0` |
| `LIVEON_PIPELINE_CHECK_INTERVAL_SEC` | How often the scheduler checks for due jobs | `3600` |
| `LIVEON_MAX_ARTICLES` | Articles published per article run | `1` |
| `LIVEON_ALLOW_LOCAL_LLM` | Permit the deterministic tip stub to publish | `0` |
| `LIVEON_TIP_USE_PRESETS` | Force offline tip presets instead of live news | `0` |
| `LIVEON_TIP_CONTEXT_PRESETS` | JSON list replacing the built-in offline tip presets | built-in set |
| `LIVEON_TIP_MODEL_NAME` | Model id for the tip agents (OpenAI) | _(unset)_ |
| `LIVEON_TIP_PUBLISHED_AT` | ISO-8601 override for a tip's publication time | _(unset)_ |

**Feeds**

| Variable | Purpose | Default |
| --- | --- | --- |
| `LIVEON_FEED_SOURCES` | JSON list of `{name, url, topic}` replacing the default feeds | Google News searches |
| `LIVEON_FEED_LIMIT` | Items fetched per feed | `5` |
| `LIVEON_FEED_HEADERS` | JSON object of extra HTTP headers for feed requests | _(unset)_ |
| `LIVEON_FEED_TIMEOUT` | Feed request timeout in seconds | `10` |

**Serving and diagnostics**

| Variable | Purpose | Default |
| --- | --- | --- |
| `LIVEON_ROOT_PATH` | Mount the app under a path prefix (e.g. `/liveon` behind a proxy) | _(none)_ |
| `LIVEON_LOG_LEVEL` | Log level for the CLI entry points | `INFO` |
| `LIVEON_DEBUG_ERRORS` | Include exception type/message in API errors. Development only. | `0` |

When the Ollama daemon is bound to `0.0.0.0`, still point `LIVEON_OLLAMA_URL` (or the pipeline command's environment) at a reachable host such as `http://127.0.0.1:11434` so local clients can connect successfully.

### Admin Console

`/admin` lists stored articles and tips and can permanently delete them, so it is protected:

- **It is disabled unless `LIVEON_ADMIN_PASSWORD` is set.** An unconfigured deployment answers `503` rather than exposing delete buttons to anyone who finds the URL.
- Access uses HTTP Basic auth, so the browser prompts for the credentials.
- Deletions require a same-origin submission and a confirmation dialog, and each one is logged with the acting username.
- The console is deliberately not linked from the site navigation; browse to `/admin` directly.

```powershell
$env:LIVEON_ADMIN_PASSWORD = "choose-a-password"
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

For Kubernetes, `deployment.yaml` reads the password from an optional secret:

```powershell
kubectl create secret generic liveon-admin --from-literal=password='choose-a-password'
```

### Talking to the Coach

`/coach` streams its answer as it is produced. Two endpoints back it:

| Endpoint | Shape |
| --- | --- |
| `POST /api/ask/stream` | Server-sent events: `chunk` per fragment, then `done` with the final answer and disclaimer, or `error`. Used by the web UI. |
| `POST /api/ask` | A single JSON response. Used as the fallback and for integrations. |

Both accept the same body. `history` carries the earlier turns so follow-up questions
resolve against what was already discussed — the coach itself holds no session state:

```json
{
  "question": "And how should that change after 65?",
  "history": [
    {"role": "user", "text": "What is a good weekly strength routine?"},
    {"role": "coach", "text": "Two full-body sessions..."}
  ]
}
```

History is bounded server-side (`LIVEON_COACH_HISTORY_TURNS`, plus character budgets),
so a client cannot grow the prompt without limit. Questions are capped at 2000 characters
and each client gets `LIVEON_ASK_RATE_LIMIT` questions per minute. Every answer carries a
safety disclaimer; if the model supplies its own trailing `Disclaimer:` line it replaces
the default.

Responses include both the raw Markdown (`answer`) and rendered, sanitised HTML
(`answer_html`). The browser displays the HTML, which keeps the server as the single
authoritative renderer — the small client-side renderer only previews tokens as they
stream in. The transcript is kept in `sessionStorage` for the life of the tab, with
copy and clear controls on the page.

### Coach Error Responses

`/api/ask` distinguishes the ways the coach can fail so the UI can say something useful:

| Status | Meaning |
| --- | --- |
| `503` | The model server could not be reached, or could not complete the request. |
| `504` | The answer exceeded `LIVEON_LLM_TIMEOUT`. |
| `500` | An unexpected server-side error. |

Error payloads carry a `message` and a short `reference` id. The full exception and traceback are written to the server log against that same reference, keeping internal details out of the browser. Set `LIVEON_DEBUG_ERRORS=1` while developing to inline them in the response instead.

## JSON API

| Endpoint | Returns |
| --- | --- |
| `GET /api/articles` | A page of articles; accepts the same `q`, `tag`, and `page` parameters as the HTML listing |
| `GET /api/articles/{id}` | One article |
| `GET /api/tips/latest` | The most recent tip |
| `POST /api/ask` | A coach answer as JSON |
| `POST /api/ask/stream` | A coach answer as server-sent events |
| `GET /healthz` | Liveness probe |

## Browsing the Content

`/articles` and `/tips` support the same query parameters:

| Parameter | Effect |
| --- | --- |
| `q` | Free-text search across title, summary, body, and tags |
| `tag` | Exact (case-insensitive) tag match; tags are clickable throughout the site |
| `page` | 1-based page number, 10 items per page |

They combine: `/articles?q=sleep&tag=longevity&page=2`.

## Presentation

- **Offline first.** Pico CSS is vendored at `app/static/vendor/pico.min.css` rather than
  loaded from a CDN, so the site is styled on a plane or an air-gapped host. Refresh it by
  downloading the same version over that file.
- **Dark mode.** The palette is a set of `--liveon-*` custom properties in
  `base.html`, flipped by `prefers-color-scheme`. New components should use those tokens
  rather than literal colours, or they will not follow the theme.
- **Error pages.** Browsers get a styled page with navigation; `/api/*` and `/healthz`
  keep returning JSON. Content negotiation is handled by `_wants_json`.
- **Sanitised rendering.** Articles, tips, and coach answers all pass through
  `sanitize_html`, an allowlist over the Markdown output. Feed items and model output are
  untrusted, and python-markdown passes raw HTML through, so scripts, event handlers, and
  `javascript:` URLs are stripped before anything reaches a page.

## Scheduled Content Generation

The web process runs both pipelines on a timer (disable with `LIVEON_DISABLE_SCHEDULER=1`).
Defaults are daily for articles and tips, matching the "Tip of the Day" the homepage
promises. A pipeline that has never run is due immediately, so a fresh install produces
content on first boot rather than after the first full interval.

The authenticated admin console lists each pipeline with its last run and next due time
and offers a **Run now** button, so the first run does not have to be a CLI ritual. Only
successful runs record a timestamp — a failed run is retried on the next check.

### Choosing how often each pipeline runs

The console has a cadence selector per pipeline. Articles and tips are set independently:

| Choice | Interval |
| --- | --- |
| Daily | every day |
| Weekly | every 7 days |
| Every two weeks | every 14 days |
| Monthly | same day each month, clamped to short months |

The choice is stored in the database, so it survives restarts and takes precedence over
`LIVEON_ARTICLE_INTERVAL_DAYS` / `LIVEON_TIP_INTERVAL_DAYS` / `LIVEON_TIP_INTERVAL_MONTHS`.
Those environment variables set the starting point for a fresh deployment; once an
operator picks a cadence in the console, that is what applies.

The next run is recalculated from the last *successful* run, so shortening an interval can
make a pipeline due straight away — which is what switching from monthly to daily is
asking for. An environment interval with no name in the list (say `LIVEON_ARTICLE_INTERVAL_DAYS=3`)
is shown as a custom setting rather than silently rounded to the nearest option.

## Content Generation Pipelines

Live On ships with two parallel content flows that share the same aggregation pool but optimise for different outputs:

- **Articles:** `LongevityNewsAggregator` → `SummarizerAgent` → `EditorAgent` → `Publisher`. The summariser drafts an article, the editor polishes tone / citations, and the publisher writes to either SQLite or a Git repo depending on configuration.
- **Tips:** `LongevityNewsAggregator` → `DailyTipContextProvider` → `TipGenerator` → `TipEditorAgent` → `TipPublisher`. The context provider distils the same aggregated news pool the article pipeline uses into research notes; a curated set of offline presets is the fallback when no feed is reachable (or when `LIVEON_TIP_USE_PRESETS=1`). Each generated `TipDraft` is reviewed against novelty, conciseness, and actionability. If the editor rejects the draft, its `TipReviewResult` feedback goes back to the generator, which also sees the recently published tips and leads the retry with a different source story so it does not re-derive the rejected tip. The final `TipPipelineResult` records the execution context, `generation_attempts`, and cumulative `editor_feedback`.

Both pipelines log warnings for soft failures (e.g., duplicate publications) and surface fatal errors so you can tune prompts or feeds as needed.

## Running the Content Pipelines

### Running the Article Pipeline

The article pipeline CLI (`app/scripts/run_pipeline.py`) can be executed to aggregate feeds and publish new content:

```powershell
python -m app.scripts.run_pipeline --feed-limit 5
```

`--feed-limit` controls how many items are *fetched* per feed, which only widens the
candidate pool; `--max-articles` (default 1) decides how many are actually published:

```powershell
python -m app.scripts.run_pipeline --feed-limit 10 --max-articles 3
```

This command respects the same storage environment variables, so ensure `LIVEON_DB_PATH` points to the SQLite file you want to populate. The project also ships with a Git publisher for writing Markdown into a repository, making it easy to sync finished articles elsewhere.

### Running the Tip Pipeline

The tip runner now uses the `DailyTipContextProvider` plus the editor-in-the-loop review cycle described above. No RSS feeds are required for local development:

```powershell
python -m app.scripts.run_tip_pipeline --model-provider ollama
```

`--model-provider` accepts `ollama`, `openai`, or `local`. The `local` provider returns a
deterministic stub rather than generated content, so publishing from it requires an
explicit `--allow-local-llm` (or `LIVEON_ALLOW_LOCAL_LLM=1`):

```powershell
python -m app.scripts.run_tip_pipeline --model-provider local --allow-local-llm
```

With no `--model-provider`, the setting is read from `LIVEON_TIP_MODEL`,
`LIVEON_SUMMARIZER_MODEL`, then `LIVEON_LLM_PROVIDER` — so a deployment configured for
Ollama generates real tips instead of silently publishing stub content. The CLI logs
telemetry such as `generation_attempts`, `editor_feedback`, and rejection reasons, and
prints the final JSON payload.

## Deployment (Minikube)

`deploy.ps1` automates the local Kubernetes workflow:

1. Builds the Docker image using Minikube’s Docker daemon.
2. Applies `pvc.yaml`, `deployment.yaml`, and `service.yaml`.
3. Waits for rollout, cleans old port-forward jobs, and establishes a new `kubectl port-forward` to `http://127.0.0.1:8080`.

Run it from PowerShell:

```powershell
pwsh ./deploy.ps1
```

Ensure Minikube (Docker driver) and kubectl are available. The script forwards proxy environment variables automatically and sets custom DNS entries to avoid registry resolution issues.

The Kubernetes deployment mounts a PVC (`liveon-data`) at `/home/appuser/liveon/data` — matching the non-root user the image runs as — so the SQLite DB persists across pod restarts. An init container runs `app.scripts.seed_content` to seed the database if it is empty.

## Testing

Unit tests live under `app/tests`. Use pytest from the repo root:

```powershell
pytest
```

The test suite focuses on models, pipeline orchestration, tip publishing, and FastAPI routes. Extend these tests when adding new agents, storage backends, or API endpoints.

## Repository Structure

```
├── app/
│   ├── main.py                 # FastAPI entrypoint
│   ├── models/                 # Domain models (content, coach, editor, tips, etc.)
│   ├── services/               # Pipeline, publishers, repositories, coach agent
│   ├── scripts/                # CLI utilities (run_pipeline, run_tip_pipeline)
│   ├── templates/              # Jinja2 templates for the web UI
│   └── tests/                  # Pytest suites
├── deploy.ps1 / deployment.yaml / service.yaml
├── Dockerfile
├── deploy_patch.json (optional PVC patch)
└── README.md
```

## Support & Next Steps

- Adjust `app/services/coach.py` if you want different prompts, or point to remote LLM providers.
- Extend `app/services/sqlite_repo.py` for alternate storage (e.g., Postgres) while keeping the repository interface consistent.
- Integrate CI/CD pipelines (GitHub Actions templates live under `.github/`) to automate content runs or deployments.

Feel free to fork and tailor the agents, feeds, or storage layer to match your longevity coaching workflows.
