# Application

Application source code for the Live On platform will live here.

## Phase status

Phases 1–4 from the implementation plan are complete and validated:

- **Phase 1 – Foundation:** The repository, container build, GKE manifests, and CI/CD plumbing are in place so code changes build and deploy automatically.
- **Phase 2 – Web experience:** The FastAPI frontend renders Firestore-backed content with graceful fallbacks for local development and includes navigation for the planned longevity resources.
- **Phase 3 – AI content agents:** Aggregator, summariser, and editor agents collaborate through the content pipeline to generate, refine, and publish longevity articles via the Firestore publisher. The pipeline can be executed locally or on the scheduled Kubernetes CronJob using deterministic local responders or live LLMs.
- **Phase 4 – Web integration & automation:** The FastAPI experience now consumes the pipeline output directly from Firestore, and the `run_pipeline` CronJob keeps articles fresh by running on an automated schedule with idempotent updates.

With the automated pipeline populating the site, the project is ready to proceed to Phase 5 tasks that deliver the interactive Longevity Coach experience.

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

### Automated content refresh

The `app/scripts/run_pipeline.py` entry point orchestrates the aggregator,
summariser, editor, and Firestore publisher so that fresh articles land in the
database without manual intervention. When deployed, the
`k8s/cronjob-pipeline.yaml` manifest schedules this script to execute
periodically, and the pipeline skips previously processed items to avoid
duplicates. Because the FastAPI views read directly from Firestore, any new
articles are automatically surfaced on the homepage and `/articles` listing.

### Operations runbooks

#### Validate scheduled executions

- **Kubernetes CronJobs:**
  1. Determine the namespace from the Helm release or Terraform outputs (`K8S_NAMESPACE`).
  2. Inspect both CronJobs with `kubectl get cronjobs -n <namespace> run-pipeline run-tip-pipeline`.
  3. Review recent executions with `kubectl get jobs -n <namespace> --sort-by=.metadata.creationTimestamp` and
     `kubectl describe cronjob <name>` to confirm next run times and last successful completions.
  4. When work queues stall, tail the most recent Job logs with `kubectl logs job/<job-name> -n <namespace>` to surface agent
     stack traces.
- **Cloud Scheduler:** If the deployment relies on Scheduler instead of CronJobs, validate both `run_pipeline` and
  `run_tip_pipeline` with `gcloud scheduler jobs describe <job-id> --location=<region>` and
  `gcloud scheduler jobs executions list <job-id>`. These commands confirm HTTP responses and retry counts for each job.

#### Troubleshoot metric dashboards

- The FastAPI endpoint `/api/metrics/run-pipeline` now aggregates telemetry for both the article and tip automations. The
  response includes a `pipelines.articles` block (for `run_pipeline`) and a `pipelines.tips` block (for `run_tip_pipeline`).
  When either pipeline falls back to sample data, the corresponding payload sets `using_sample_data` to `true` and the merged
  `logs` array prefixes each section with `=== Content pipeline` or `=== Tip pipeline` so you can copy diagnostics directly
  into incident tickets.
- During local development without GCP credentials the service provides synthetic metrics for both jobs, ensuring dashboards
  stay interactive. Production issues generally indicate one of three root causes:
  1. **Missing Cloud Monitoring permissions:** both payloads contain errors resembling `Permission denied`. Grant
     `roles/monitoring.viewer` to the runtime service account.
  2. **Incorrect project or job IDs:** the `job_id` field in each payload highlights the queried resource; verify it matches
     the Scheduler job or CronJob name and that Terraform state exports the correct project ID.
  3. **Stalled tip pipeline CronJob:** the `pipelines.tips.active_runs` counter and `recent_jobs` history should increment at
     least daily. If they remain empty, re-run `kubectl describe cronjob run-tip-pipeline` (or review the Scheduler execution
     logs) and redeploy the CronJob manifest with updated secrets or resource limits.
- For Kubernetes deployments, set both `K8S_CRONJOB_NAME` and `K8S_TIP_CRONJOB_NAME` so the monitoring endpoint can resolve
  the appropriate CronJob metadata. Missing variables trigger `Missing required Kubernetes configuration` warnings in the
  API response.

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

#### Overriding feed sources

The default pipeline configuration now targets curated Google News RSS
queries focused on longevity research, healthy aging policy, and lifestyle
guidance. To point the aggregator at different sources without editing the
codebase, set the `LIVEON_FEED_SOURCES` environment variable to a JSON array of
objects containing `name`, `url`, and optional `topic` keys. The runner checks
this environment variable before falling back to the baked-in defaults.

Example payload:

```json
[
  {
    "name": "Your Feed Label",
    "url": "https://news.google.com/rss/search?q=precision+longevity&hl=en-US&gl=US&ceid=US:en",
    "topic": "research"
  },
  {
    "name": "Clinic Updates",
    "url": "https://news.google.com/rss/search?q=geroscience+clinical+trial&hl=en-US&gl=US&ceid=US:en",
    "topic": "clinical"
  }
]
```

For ad-hoc runs you can export the variable and invoke the runner in a single
command:

```bash
LIVEON_FEED_SOURCES='[{"name": "Local Feed", "url": "https://example.com/rss"}]' \
python -m app.scripts.run_pipeline
```

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
