"""Seed the local database with placeholder content for development."""

from __future__ import annotations

from datetime import datetime, timezone

from app.models.content import Article, Tip
from app.services.sqlite_repo import LocalSQLiteContentRepository


def main() -> None:
    repository = LocalSQLiteContentRepository()

    seed_articles = [
        Article(
            title="Welcome to Live On",
            content_body=(
                "The Live On Longevity Coach keeps you informed about the science of living longer. "
                "This placeholder article is stored in the database so the web experience can be wired up "
                "before the AI pipeline goes live."
            ),
            summary="An introduction article used to validate database integration.",
            source_urls=[],
            tags=["introduction", "platform"],
            published_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        ),
    ]

    seed_tips = [
        Tip(
            title="Hydration Reminder",
            content_body="Staying hydrated supports cellular health and overall longevity.",
            tags=["habit", "daily"],
            published_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
        ),
    ]

    created_documents = repository.seed_if_empty(articles=seed_articles, tips=seed_tips)

    for collection, identifiers in created_documents.items():
        if identifiers:
            print(f"Created {collection}: {', '.join(identifiers)}")
        else:
            print(f"Collection '{collection}' already contained documents; nothing created.")


if __name__ == "__main__":
    main()
