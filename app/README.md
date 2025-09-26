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
