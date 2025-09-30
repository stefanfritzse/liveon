"""Firestore data access helpers for the Longevity Coach application."""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Final
from google.cloud import firestore
from google.cloud.firestore import Client, CollectionReference, DocumentReference

from app.models.content import Article, Tip

DEFAULT_ARTICLES_COLLECTION: Final[str] = "articles"
DEFAULT_TIPS_COLLECTION: Final[str] = "tips"


class FirestoreContentRepository:
    """Repository that encapsulates all Firestore access for content collections."""

    def __init__(
        self,
        client: Client | None = None,
        *,
        article_collection: str = DEFAULT_ARTICLES_COLLECTION,
        tip_collection: str = DEFAULT_TIPS_COLLECTION,
    ) -> None:
        self._client = client or firestore.Client()
        self._article_collection_name = article_collection
        self._tip_collection_name = tip_collection

    @property
    def client(self) -> Client:
        return self._client

    @property
    def article_collection(self) -> CollectionReference:
        return self._client.collection(self._article_collection_name)

    @property
    def tip_collection(self) -> CollectionReference:
        return self._client.collection(self._tip_collection_name)

    # ------------------------------------------------------------------
    # Article helpers
    # ------------------------------------------------------------------
    def get_latest_articles(self, *, limit: int = 5) -> list[Article]:
        """Return the newest articles sorted by published date descending."""
        query = (
            self.article_collection.order_by("published_date", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        return [Article.from_document(snapshot) for snapshot in query.stream()]

    def get_article(self, article_id: str) -> Article | None:
        """Retrieve a single article by its Firestore document ID."""
        snapshot = self.article_collection.document(article_id).get()
        if not snapshot.exists:
            return None
        return Article.from_document(snapshot)

    def save_article(self, article: Article) -> Article:
        """Persist an article to Firestore, returning the stored representation."""
        collection = self.article_collection
        document: DocumentReference
        if article.id:
            document = collection.document(article.id)
        else:
            document = collection.document()
        document.set(article.to_document())
        stored = document.get()
        return Article.from_document(stored)

    def find_article_by_source_url(self, url: str) -> Article | None:
        """Return the first article that references the given source URL, if any."""

        if not url:
            return None

        query = self.article_collection.where("source_urls", "array_contains", url).limit(1)
        for snapshot in query.stream():
            return Article.from_document(snapshot)
        return None

    # ------------------------------------------------------------------
    # Tip helpers
    # ------------------------------------------------------------------
    def get_latest_tips(self, *, limit: int = 5) -> list[Tip]:
        """Return the newest tips sorted by published date descending."""
        query = (
            self.tip_collection.order_by("published_date", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        return [Tip.from_document(snapshot) for snapshot in query.stream()]

    def get_latest_tip(self) -> Tip | None:
        """Return the single most recently published tip, if available."""

        query = (
            self.tip_collection.order_by("published_date", direction=firestore.Query.DESCENDING)
            .limit(1)
        )
        for snapshot in query.stream():
            return Tip.from_document(snapshot)
        return None

    def save_tip(self, tip: Tip) -> Tip:
        collection = self.tip_collection
        document: DocumentReference
        if tip.id:
            document = collection.document(tip.id)
        else:
            document = collection.document()
        document.set(tip.to_document())
        stored = document.get()
        return Tip.from_document(stored)

    def find_tip_by_title(self, title: str) -> Tip | None:
        """Return the first tip matching ``title`` if one exists."""

        if not title:
            return None

        query = self.tip_collection.where("title", "==", title.strip()).limit(1)
        for snapshot in query.stream():
            return Tip.from_document(snapshot)
        return None

    def find_tip_by_tags(self, tags: Sequence[str] | Iterable[str]) -> Tip | None:
        """Return a tip whose tags match the provided collection exactly."""

        if isinstance(tags, str):
            tag_list = [tags.strip()] if tags.strip() else []
        else:
            iterable: Iterable[str]
            if isinstance(tags, Sequence):
                iterable = tags
            else:
                iterable = list(tags)
            tag_list = [tag.strip() for tag in iterable if isinstance(tag, str) and tag.strip()]

        if not tag_list:
            return None

        query = self.tip_collection.where("tags", "array_contains", tag_list[0]).limit(20)
        sought = set(tag_list)

        for snapshot in query.stream():
            tip = Tip.from_document(snapshot)
            if set(filter(None, (tag.strip() for tag in tip.tags))) == sought:
                return tip
        return None

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------
    def seed_if_empty(
        self,
        *,
        articles: Iterable[Article] = (),
        tips: Iterable[Tip] = (),
    ) -> dict[str, list[str]]:
        """Seed the database with initial content if the collections are empty."""
        created_articles: list[str] = []
        created_tips: list[str] = []

        if self._is_collection_empty(self.article_collection):
            for article in articles:
                stored = self.save_article(article)
                created_articles.append(stored.id or "")

        if self._is_collection_empty(self.tip_collection):
            for tip in tips:
                stored = self.save_tip(tip)
                created_tips.append(stored.id or "")

        return {"articles": created_articles, "tips": created_tips}

    def _is_collection_empty(self, collection: CollectionReference) -> bool:
        query = collection.limit(1)
        return not any(query.stream())


def create_repository(**kwargs: Any) -> FirestoreContentRepository:
    """Factory helper that mirrors the default project-aware client creation."""
    return FirestoreContentRepository(**kwargs)
