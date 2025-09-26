"""Seed Firestore with placeholder content for Phase 2 development."""

from __future__ import annotations

from datetime import datetime, timezone

from app.models.content import Article, Tip
from app.services.firestore import FirestoreContentRepository


def main() -> None:
    repository = FirestoreContentRepository()

    seed_articles = [
        Article(
            title="Welcome to Live On",
            content_body=(
                "The Live On Longevity Coach keeps you informed about the science of living longer. "
                "This placeholder article is stored in Firestore so the web experience can be wired up "
                "before the AI pipeline goes live."
            ),
            summary="An introduction article used to validate Firestore integration.",
            source_urls=[
                "https://cloud.google.com/firestore/docs",  # Example citation for developer reference
            ],
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
