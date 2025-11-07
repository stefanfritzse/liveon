Here’s a pragmatic, high-level refactor plan to move your pipeline off Google Firestore and onto a local database (SQLite) on Windows 11 Home. It’s tailored to your codebase and the coupling points I see.

1) Identify the coupling points

The pipeline is wired to Firestore in the builder: FirestoreContentRepository + FirestorePublisher. Swap these at construction time. 

run_pipeline

The pipeline only needs a repo that can: look up an article by source URL (to avoid dupes) and a publisher that can persist an EditedArticle. You already have minimal Protocols for this pattern (e.g., SupportsSourceLookup in the pipeline; SupportsArticleRepository in the publisher). Leverage these to keep the change surface small. 

pipeline

 

publisher

2) Choose the local store

Use SQLite (file-based, zero setup, ships with Python). One DB file: e.g., %USERPROFILE%\liveon\data\content.db.

3) Define a minimal schema

Tables that mirror what you persist today (don’t overthink it—store arrays as JSON text):

articles(id TEXT PRIMARY KEY, title TEXT, summary TEXT, content_body TEXT, source_urls TEXT, tags TEXT, published_date TEXT)

tips(id TEXT PRIMARY KEY, title TEXT, summary TEXT, body TEXT, tags TEXT, published_date TEXT)
These fields line up with your Article/Tip models the Firestore repo writes today. 

firestore

4) Implement LocalSQLiteContentRepository

Create app/services/sqlite_repo.py with a class implementing the same surface your pipeline/publishers use:

Must implement (names identical to the Firestore version where used):

get_article(id: str) -> Article | None

save_article(article: Article) -> Article

find_article_by_source_url(url: str) -> Article | None

(Optionally) get_latest_articles, get_latest_tips, etc., if other parts of your app call them. 

firestore

Notes:

Use sqlite3 and create tables on first connect.

Serialize source_urls and tags as JSON strings; deserialize when returning models.

Add a tiny helper object with an id attribute for article_collection so existing publisher logic that reads repository.article_collection.id doesn’t explode (return "articles"). 

publisher

5) Introduce a DB-agnostic publisher (or adapt the current one cleanly)

You can do this in two ways:

A. New class: LocalDBPublisher(repository: SupportsArticleRepository) that copies the logic from FirestorePublisher but writes a filesystem path pointing at the SQLite DB (or simply returns a synthetic path like db://articles/<slug>). Keep _is_duplicate and slug resolution identical. This keeps concerns clean and avoids Firestore-specific path code. 

publisher

B. Minimal change: Keep using FirestorePublisher by ensuring your SQLite repo exposes:

get_article, save_article, and an article_collection object with .id == "articles".
This works because FirestorePublisher only uses those members and its duplicate detection is model-based (not Firestore API–based). If you go this route, adjust _build_firestore_path or leave it as a harmless cosmetic value. 

publisher

(#5A is cleaner; #5B is the fastest path.)

6) Make storage selectable via config

In run_pipeline._build_pipeline(), add a switch (env var like LIVEON_STORAGE=sqlite|firestore):

If sqlite → construct LocalSQLiteContentRepository(path=LIVEON_DB_PATH) and LocalDBPublisher(repo) (or FirestorePublisher(repo) if you chose #5B). Then feed them into ContentPipeline. 

run_pipeline

 

pipeline

Default to sqlite when no GCP project/env is detected (your code already detects “managed environments”; keep that logic). 

run_pipeline

7) Keep the agent code unchanged

Your summarizer/editor agents and prompts don’t care about storage. No changes needed. 

summarizer

 

editor

8) One-time data migration (optional)

If you have historic content in Firestore you want locally:

Write a tiny script that constructs both repos: fs_repo = FirestoreContentRepository() and sqlite_repo = LocalSQLiteContentRepository(), then pages Firestore articles/tips and writes them to SQLite with save_article/save_tip. Reuse existing model converters (Article.from_document & to_document equivalences). 

firestore

9) Testing plan

Repo unit tests: round-trip Article and Tip through SQLite (insert, fetch by id, find_article_by_source_url, exact-tag match). Mirror existing behaviors in the Firestore repo (e.g., array equality semantics). 

firestore

Pipeline integration: run the pipeline with LIVEON_STORAGE=sqlite and verify:

It selects a new aggregated item, produces a draft, revises it, and publishes once.

A second run skips duplicates (driven by find_article_by_source_url + publisher duplicate logic). 

pipeline

 

publisher

Windows check: ensure the DB path resolves on Windows (Path.home()), and file is created without admin rights.

10) Dev ergonomics on Windows

Add to .env (or user env vars):

LIVEON_STORAGE=sqlite

LIVEON_DB_PATH=C:\Users\<you>\liveon\data\content.db

Drop google-cloud-firestore from your dev requirements (keep it as an optional extra for prod), so local runs don’t pull GCP libs.

No background services needed; SQLite is embedded.

11) Risks & mitigations

Array queries: Firestore’s array_contains is replaced with a JSON LIKE query or a small client-side filter for exact set matching (as your repo currently does for tips). Document this so expectations match. 

firestore

Concurrent writes: SQLite handles single-writer, multi-reader fine for your use case; enable WAL mode if you later see lock contention.

Timezones: Keep storing published_at in UTC ISO 8601 (string). Your publishers already normalize to UTC; preserve that. 

publisher