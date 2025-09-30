"""Tests covering FastAPI routes and template rendering for the public site."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

import pytest
from fastapi.testclient import TestClient

from app.main import ContentRepository, app, get_repository
from app.models.content import Article, Tip


class StubContentRepository(ContentRepository):
    """In-memory repository used to exercise FastAPI dependency overrides."""

    def __init__(
        self,
        *,
        articles: Iterable[Article] | None = None,
        tips: Iterable[Tip] | None = None,
    ) -> None:
        self._articles = list(articles or [])
        self._tips = list(tips or [])

    def get_latest_articles(self, *, limit: int = 5) -> list[Article]:  # pragma: no cover - interface requirement
        return sorted(self._articles, key=lambda article: article.published_date, reverse=True)[:limit]

    def get_article(self, article_id: str) -> Article | None:  # pragma: no cover - interface requirement
        return next((article for article in self._articles if article.id == article_id), None)

    def get_latest_tips(self, *, limit: int = 5) -> list[Tip]:
        return sorted(self._tips, key=lambda tip: tip.published_date, reverse=True)[:limit]

    def get_latest_tip(self) -> Tip | None:
        ordered = self.get_latest_tips(limit=1)
        return ordered[0] if ordered else None


@pytest.fixture()
def client() -> Callable[[ContentRepository], TestClient]:
    """Provide a helper that returns a configured ``TestClient`` for a repository."""

    clients: list[TestClient] = []

    def _factory(repository: ContentRepository) -> TestClient:
        app.dependency_overrides[get_repository] = lambda: repository
        test_client = TestClient(app)
        clients.append(test_client)
        return test_client

    yield _factory

    for created_client in clients:
        created_client.close()
    app.dependency_overrides.pop(get_repository, None)


def _build_tip(identifier: str, published_offset_hours: int) -> Tip:
    published = datetime.now(timezone.utc) - timedelta(hours=published_offset_hours)
    return Tip(
        id=identifier,
        title=f"Tip {identifier}",
        content_body=f"Content for {identifier}",
        tags=["daily"],
        published_date=published,
    )


def test_homepage_context_includes_featured_tip(client: Callable[[ContentRepository], TestClient]) -> None:
    tips = [_build_tip("a", 1), _build_tip("b", 2)]
    repository = StubContentRepository(tips=tips)
    test_client = client(repository)

    response = test_client.get("/")

    assert response.status_code == 200
    assert response.template.name == "home.html"
    assert response.context["featured_tip"].id == "a"
    assert [tip.id for tip in response.context["recent_tips"]] == ["b"]


def test_homepage_handles_missing_tip(client: Callable[[ContentRepository], TestClient]) -> None:
    repository = StubContentRepository(tips=[])
    test_client = client(repository)

    response = test_client.get("/")

    assert response.status_code == 200
    assert response.context["featured_tip"] is None
    assert response.context["recent_tips"] == []


def test_latest_tip_endpoint_returns_tip_payload(
    client: Callable[[ContentRepository], TestClient]
) -> None:
    repository = StubContentRepository(tips=[_build_tip("current", 0)])
    test_client = client(repository)

    response = test_client.get("/api/tips/latest")

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "current"
    assert payload["title"] == "Tip current"
    assert payload["content_body"].startswith("Content for current")
    assert payload["tags"] == ["daily"]


def test_latest_tip_endpoint_handles_empty_repository(
    client: Callable[[ContentRepository], TestClient]
) -> None:
    repository = StubContentRepository(tips=[])
    test_client = client(repository)

    response = test_client.get("/api/tips/latest")

    assert response.status_code == 404
    assert response.json() == {"detail": "No tips available"}


def test_tips_page_splits_featured_and_recent(client: Callable[[ContentRepository], TestClient]) -> None:
    tips = [_build_tip("latest", 0), _build_tip("older", 4)]
    repository = StubContentRepository(tips=tips)
    test_client = client(repository)

    response = test_client.get("/tips")

    assert response.status_code == 200
    assert response.template.name == "tips/list.html"
    assert response.context["featured_tip"].id == "latest"
    assert [tip.id for tip in response.context["recent_tips"]] == ["older"]


def test_tips_page_empty_state(client: Callable[[ContentRepository], TestClient]) -> None:
    repository = StubContentRepository(tips=[])
    test_client = client(repository)

    response = test_client.get("/tips")

    assert response.status_code == 200
    assert response.context["featured_tip"] is None
    assert response.context["recent_tips"] == []
