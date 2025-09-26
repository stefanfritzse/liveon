# Application

Application source code for the Live On platform will live here.

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

Once an article has been drafted and edited, the `GitPublisher` in
`app/services/publisher.py` converts the payload into Markdown with YAML
front matter and commits it to the repository, treating content as code.
The corresponding tests in `app/tests/test_publisher.py` initialise a
temporary Git repository to validate that files are created correctly,
front matter stays structured, and Git commits are produced with the
expected metadata. This lays the groundwork for integrating the
multi-agent workflow with GitOps tooling in later milestones.

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
