# Application

Application source code for the Live On platform will live here.

## Firestore content module

Phase 2 introduces a Firestore-backed content system for articles and
coaching tips. The data access layer lives in `app/services/firestore.py`
with the corresponding domain models in `app/models/content.py`.

### Requirements

Install the application dependencies in a virtual environment:

```bash
pip install -r requirements.txt
```

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
