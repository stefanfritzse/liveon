# Application

Application source code for the Live On platform will live here..

## Phase status

Phases 1–3 from the implementation plan are complete and validated:

- **Phase 1 – Foundation:** The repository, container build, GKE manifests, and CI/CD plumbing are in place so code changes build and deploy automatically.
- **Phase 2 – Web experience:** The FastAPI frontend renders Firestore-backed content with graceful fallbacks for local development and includes navigation for the planned longevity resources.
- **Phase 3 – AI content agents:** Aggregator, summariser, and editor agents collaborate through the content pipeline to generate, refine, and publish longevity articles via the Firestore publisher. The pipeline can be executed locally or on the scheduled Kubernetes CronJob using deterministic local responders or live LLMs.

With the agent workflow operating end-to-end, the project is ready to proceed to Phase 4 tasks that integrate the generated content more deeply into the user-facing experience.

## FastAPI web application

Phase 2 introduces a user-facing FastAPI application that renders HTML
pages with Jinja2 templates. The app currently exposes the following
routes:

| Route | Description |
| --- | --- |
| `/` | Homepage with highlights from the latest articles and tips. |
| `/articles` | Listing of recent longevity articles. |
| `/articles/{id}` | Article detail page. |
| `/tips` | Listing of recent coaching tips. |
| `/coach` | Placeholder page for the upcoming interactive coach. |

The web layer fetches data from Firestore via the content repository. If
credentials are not configured during local development the application
falls back to in-memory sample data so that the UI remains accessible.

## Firestore content module

Phase 2 introduces a Firestore-backed content system for articles and
coaching tips. The data access layer lives in `app/services/firestore.py`
with the corresponding domain models in `app/models/content.py`.

## Aggregator and publisher agents

The AI-driven content pipeline has begun to take shape. The
`LongevityNewsAggregator` in `app/services/aggregator.py` pulls longevity
research updates from configured RSS/Atom feeds and normalises them into
`AggregatedContent` records (`app/models/aggregator.py`). The module is
testable in isolation thanks to injectable HTTP fetchers, ensuring the
future multi-agent workflow can rely on deterministic data during
development.

Once an article has been drafted and edited, publishers in
`app/services/publisher.py` persist the final content. The `GitPublisher`
converts the payload into Markdown with YAML front matter and commits it
to the repository, treating content as code. For dynamic Firestore-backed
deployments, the `FirestorePublisher` writes articles directly to the
`articles` collection, deduplicating updates so rerunning the pipeline
does not spam users with repeated posts. The corresponding tests in
`app/tests/test_publisher.py` cover both workflows: the Git path
initialises a temporary repository to validate commits, while the
Firestore path ensures articles are stored with deterministic slugs and
timestamps. This lays the groundwork for integrating the multi-agent
workflow with GitOps tooling and the live Firestore content store in
later milestones.

### Running the AI content pipeline

The pipeline can now be executed end-to-end using
`python -m app.scripts.run_pipeline`. The runner wires together the
aggregator, summariser, editor, and publisher agents and publishes the
resulting article directly to Firestore so it appears on the FastAPI
frontend.

By default the runner uses a deterministic local responder so it works
without external LLM access. To invoke Vertex AI or OpenAI models instead,
set the environment variables `LIVEON_SUMMARIZER_MODEL` and
`LIVEON_EDITOR_MODEL` to `vertex` or `openai` and provide the corresponding
credentials. Additional configuration options include:

| Variable | Purpose |
| --- | --- |
| `LIVEON_FEED_SOURCES` | JSON array overriding the default RSS/Atom feeds. |
| `LIVEON_FEED_LIMIT` | Number of entries to pull per feed (default: 5). |
| `LIVEON_LOG_LEVEL` | Logging level (default: `INFO`). |

When deployed to GKE, the `k8s/cronjob-pipeline.yaml` manifest schedules the
runner to execute daily so the site stays populated with fresh longevity
content.

### Requirements

Install the application dependencies in a virtual environment:

```bash
pip install -r requirements.txt
```

### Running the development server

Launch the FastAPI app with Uvicorn:

```bash
uvicorn app.main:app --reload
```

Set `GOOGLE_CLOUD_PROJECT` and point to a Firestore emulator (or provide
production credentials) to serve real content. Without credentials the
app displays seeded placeholder data.

### Seeding Firestore

The `app/scripts/seed_content.py` script populates the Firestore
collections with a starter article and tip. The script only inserts
documents when the `articles` or `tips` collection is empty, making it
safe to run multiple times during development.

```bash
export GOOGLE_CLOUD_PROJECT=<your-project-id>
python -m app.scripts.seed_content
```

Use the `FIRESTORE_EMULATOR_HOST` environment variable if you prefer to
test against the Firestore emulator instead of a live project.
