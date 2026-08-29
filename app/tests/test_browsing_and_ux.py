"""Tests for the P2 user-experience work.

Covers styled error pages, content browsing (search, tag filter, pagination), the
offline stylesheet, question limits and rate limiting, and the single-renderer
guarantee for coach answers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pytest
from fastapi.testclient import TestClient

from app.main import (
    ITEMS_PER_PAGE,
    MAX_QUESTION_CHARS,
    ContentRepository,
    _paginate_in_memory,
    _wants_json,
    app,
    coach_rate_limiter,
    get_coach_agent,
    get_repository,
)
from app.models.coach import CoachAnswer
from app.models.content import Article, ContentPage, Tip
from app.services.sqlite_repo import LocalSQLiteContentRepository

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)


class BrowsableRepository(ContentRepository):
    """In-memory repository backed by the same paginator the app uses."""

    def __init__(self, articles: list[Article], tips: list[Tip]) -> None:
        self._articles = articles
        self._tips = tips

    def get_latest_articles(self, *, limit: int = 5) -> list[Article]:
        return self._articles[:limit]

    def get_article(self, article_id: str) -> Article | None:
        return next((a for a in self._articles if a.id == article_id), None)

    def get_latest_tips(self, *, limit: int = 5) -> list[Tip]:
        return self._tips[:limit]

    def get_latest_tip(self) -> Tip | None:
        return self._tips[0] if self._tips else None

    def delete_article(self, article_id: str) -> bool:
        return False

    def delete_tip(self, tip_id: str) -> bool:
        return False

    def browse_articles(self, **kwargs: object) -> ContentPage:
        return _paginate_in_memory(list(self._articles), **kwargs)

    def browse_tips(self, **kwargs: object) -> ContentPage:
        return _paginate_in_memory(list(self._tips), **kwargs)


def _articles(count: int = 25) -> list[Article]:
    return [
        Article(
            id=f"article-{i}",
            title=f"Sleep study {i}" if i % 2 == 0 else f"Strength study {i}",
            content_body="Body text",
            summary=f"Summary {i}",
            tags=["sleep"] if i % 2 == 0 else ["strength"],
            published_date=NOW - timedelta(days=i),
        )
        for i in range(count)
    ]


def _tips(count: int = 25) -> list[Tip]:
    return [
        Tip(
            id=f"tip-{i}",
            title=f"Hydration tip {i}" if i % 2 == 0 else f"Mobility tip {i}",
            content_body="Tip body",
            tags=["hydration"] if i % 2 == 0 else ["mobility"],
            published_date=NOW - timedelta(days=i),
        )
        for i in range(count)
    ]


@pytest.fixture()
def client() -> Iterable[TestClient]:
    repository = BrowsableRepository(_articles(), _tips())
    app.dependency_overrides[get_repository] = lambda: repository
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_repository, None)


# ----------------------------------------------------------------------
# #14 Error pages
# ----------------------------------------------------------------------


def test_missing_article_renders_a_page_not_json(client: TestClient) -> None:
    """A mistyped article URL used to answer with raw JSON and no way back."""

    response = client.get("/articles/does-not-exist")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Page not found" in response.text
    # The reader is offered somewhere to go.
    assert "/articles" in response.text
    assert "Ask the coach" in response.text


def test_unknown_path_renders_a_page(client: TestClient) -> None:
    response = client.get("/no-such-page")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")


def test_api_paths_still_answer_with_json(client: TestClient) -> None:
    response = client.get("/api/tips/latest")

    assert response.headers["content-type"].startswith("application/json")


def test_api_errors_stay_json(client: TestClient) -> None:
    repository = BrowsableRepository([], [])
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        response = client.get("/api/tips/latest")
    finally:
        app.dependency_overrides.pop(get_repository, None)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "No tips available"}


def test_error_page_keeps_response_headers(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401 must still carry WWW-Authenticate so the browser prompts."""

    monkeypatch.setenv("LIVEON_ADMIN_PASSWORD", "secret")

    response = client.get("/admin")

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


@pytest.mark.parametrize(
    ("path", "accept", "expected"),
    [
        ("/api/ask", "text/html", True),
        ("/healthz", "text/html", True),
        ("/articles", "text/html,application/xhtml+xml", False),
        ("/articles", "application/json", True),
        ("/articles", "*/*", False),
    ],
)
def test_content_negotiation(path: str, accept: str, expected: bool) -> None:
    class _Req:
        def __init__(self) -> None:
            self.url = type("U", (), {"path": path})()
            self.headers = {"accept": accept}

    assert _wants_json(_Req()) is expected


# ----------------------------------------------------------------------
# #19 Browsing
# ----------------------------------------------------------------------


def test_articles_are_paginated(client: TestClient) -> None:
    response = client.get("/articles")

    assert response.status_code == 200
    results = response.context["results"]
    assert len(results.items) == ITEMS_PER_PAGE
    assert results.total == 25
    assert results.total_pages == 3
    assert "Page 1 of 3" in response.text


def test_second_page_shows_different_articles(client: TestClient) -> None:
    first = client.get("/articles").context["results"].items
    second = client.get("/articles?page=2").context["results"].items

    assert [a.id for a in first] != [a.id for a in second]
    assert not set(a.id for a in first) & set(a.id for a in second)


def test_last_page_has_no_next(client: TestClient) -> None:
    results = client.get("/articles?page=3").context["results"]

    assert results.has_next is False
    assert results.has_previous is True


def test_articles_can_be_searched(client: TestClient) -> None:
    response = client.get("/articles?q=Strength")

    results = response.context["results"]
    assert results.total == 12
    assert all("Strength" in a.title for a in results.items)


def test_articles_can_be_filtered_by_tag(client: TestClient) -> None:
    results = client.get("/articles?tag=sleep").context["results"]

    assert results.total == 13
    assert all("sleep" in a.tags for a in results.items)


def test_tag_filter_is_case_insensitive(client: TestClient) -> None:
    assert client.get("/articles?tag=SLEEP").context["results"].total == 13


def test_search_and_tag_combine(client: TestClient) -> None:
    results = client.get("/articles?q=Sleep%20study&tag=sleep").context["results"]

    assert results.total == 13


def test_no_matches_offers_a_way_back(client: TestClient) -> None:
    response = client.get("/articles?q=zzzznotfound")

    assert response.status_code == 200
    assert "Nothing matched that search" in response.text
    assert "Show all articles" in response.text


def test_pagination_links_preserve_the_search(client: TestClient) -> None:
    response = client.get("/articles?q=Strength")

    assert "q=Strength" in response.text


def test_article_tags_are_clickable(client: TestClient) -> None:
    response = client.get("/articles")

    assert 'href="/articles?tag=sleep"' in response.text


def test_tips_are_paginated_and_searchable(client: TestClient) -> None:
    listing = client.get("/tips")
    assert listing.context["results"].total == 25
    assert listing.context["featured_tip"] is not None

    filtered = client.get("/tips?tag=mobility")
    assert filtered.context["results"].total == 12
    # A filtered view presents every match equally rather than featuring one.
    assert filtered.context["featured_tip"] is None


def test_tips_second_page_has_no_featured_slot(client: TestClient) -> None:
    assert client.get("/tips?page=2").context["featured_tip"] is None


def test_article_detail_shows_summary_and_a_way_back(client: TestClient) -> None:
    response = client.get("/articles/article-0")

    assert response.status_code == 200
    assert "Summary 0" in response.text
    assert "Back to all articles" in response.text
    assert 'href="/articles?tag=sleep"' in response.text


# ----------------------------------------------------------------------
# Repository-level browsing
# ----------------------------------------------------------------------


@pytest.fixture()
def sqlite_repo(tmp_path: Path) -> LocalSQLiteContentRepository:
    repo = LocalSQLiteContentRepository(db_path=tmp_path / "browse.db")
    for article in _articles(12):
        repo.save_article(article)
    for tip in _tips(12):
        repo.save_tip(tip)
    return repo


def test_sqlite_browse_paginates(sqlite_repo: LocalSQLiteContentRepository) -> None:
    page = sqlite_repo.browse_articles(page=1, per_page=5)

    assert len(page.items) == 5
    assert page.total == 12
    assert page.total_pages == 3


def test_sqlite_browse_filters_by_exact_tag(sqlite_repo: LocalSQLiteContentRepository) -> None:
    page = sqlite_repo.browse_articles(tag="sleep", per_page=50)

    assert page.total == 6
    assert all("sleep" in article.tags for article in page.items)


def test_sqlite_browse_searches_text(sqlite_repo: LocalSQLiteContentRepository) -> None:
    page = sqlite_repo.browse_articles(query="Strength", per_page=50)

    assert page.total == 6


def test_sqlite_browse_reports_available_tags(sqlite_repo: LocalSQLiteContentRepository) -> None:
    assert set(sqlite_repo.browse_articles().available_tags) == {"sleep", "strength"}


def test_sqlite_browse_tips(sqlite_repo: LocalSQLiteContentRepository) -> None:
    page = sqlite_repo.browse_tips(tag="hydration", per_page=50)

    assert page.total == 6


def test_a_tag_substring_does_not_match(sqlite_repo: LocalSQLiteContentRepository) -> None:
    """The SQL pre-filter is coarse; the exact match happens in Python."""

    assert sqlite_repo.browse_articles(tag="slee", per_page=50).total == 0


def test_page_beyond_the_end_is_empty(sqlite_repo: LocalSQLiteContentRepository) -> None:
    page = sqlite_repo.browse_articles(page=99, per_page=5)

    assert page.items == []
    assert page.first_index == 0


# ----------------------------------------------------------------------
# #18 Offline styling
# ----------------------------------------------------------------------


def test_stylesheet_is_served_locally(client: TestClient) -> None:
    """The offline-first promise is broken by a CDN dependency."""

    response = client.get("/")

    assert "cdn.jsdelivr.net" not in response.text
    assert "/static/vendor/pico.min.css" in response.text


def test_vendored_stylesheet_is_present_and_served(client: TestClient) -> None:
    assert (Path("app/static/vendor/pico.min.css")).exists()

    response = client.get("/static/vendor/pico.min.css")

    assert response.status_code == 200
    assert len(response.content) > 10000


def test_pages_declare_a_dark_palette(client: TestClient) -> None:
    response = client.get("/")

    assert "prefers-color-scheme: dark" in response.text
    assert 'content="light dark"' in response.text


# ----------------------------------------------------------------------
# #15 Stale copy
# ----------------------------------------------------------------------


def test_home_page_does_not_promise_the_coach_as_future_work(client: TestClient) -> None:
    response = client.get("/")

    assert "soon chat with an AI longevity coach" not in response.text


def test_admin_copy_matches_the_shipped_feature(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEON_ADMIN_PASSWORD", "secret")

    response = client.get("/admin", auth=("admin", "secret"))

    assert "Removal actions will be available soon" not in response.text


# ----------------------------------------------------------------------
# #17 Transcript controls
# ----------------------------------------------------------------------


def test_coach_page_offers_copy_and_clear(client: TestClient) -> None:
    response = client.get("/coach")

    assert 'id="coach-copy"' in response.text
    assert 'id="coach-clear"' in response.text
    assert "sessionStorage" in response.text
    assert "liveon.coach.transcript" in response.text


# ----------------------------------------------------------------------
# #21 Question limits and rate limiting
# ----------------------------------------------------------------------


class _EchoAgent:
    default_disclaimer = "Educational only."

    def ask(self, question):  # type: ignore[no-untyped-def]
        return CoachAnswer(message="Answer.", disclaimer="Educational only.")

    def stream(self, question):  # type: ignore[no-untyped-def]
        yield "Answer."


@pytest.fixture()
def coach_client() -> Iterable[TestClient]:
    app.dependency_overrides[get_coach_agent] = lambda: _EchoAgent()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_coach_agent, None)


def test_an_overlong_question_is_rejected(coach_client: TestClient) -> None:
    response = coach_client.post(
        "/api/ask", json={"question": "x" * (MAX_QUESTION_CHARS + 1)}
    )

    assert response.status_code == 422


def test_a_question_at_the_limit_is_accepted(coach_client: TestClient) -> None:
    response = coach_client.post("/api/ask", json={"question": "x" * MAX_QUESTION_CHARS})

    assert response.status_code == 200


def test_the_textarea_advertises_the_limit(client: TestClient) -> None:
    response = client.get("/coach")

    assert f'maxlength="{MAX_QUESTION_CHARS}"' in response.text
    assert 'id="coach-charcount"' in response.text


def test_rapid_questions_are_rate_limited(coach_client: TestClient) -> None:
    coach_rate_limiter.limit = 3
    try:
        codes = [
            coach_client.post("/api/ask", json={"question": "Hello"}).status_code
            for _ in range(5)
        ]
    finally:
        coach_rate_limiter.limit = 30
        coach_rate_limiter.reset()

    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429]


def test_a_rate_limited_response_says_when_to_retry(coach_client: TestClient) -> None:
    coach_rate_limiter.limit = 1
    try:
        coach_client.post("/api/ask", json={"question": "Hello"})
        response = coach_client.post("/api/ask", json={"question": "Hello"})
    finally:
        coach_rate_limiter.limit = 30
        coach_rate_limiter.reset()

    assert response.status_code == 429
    assert "Retry-After" in response.headers
    assert "moment" in response.json()["detail"]["message"]


def test_the_stream_endpoint_is_rate_limited_too(coach_client: TestClient) -> None:
    coach_rate_limiter.limit = 1
    try:
        coach_client.post("/api/ask/stream", json={"question": "Hello"})
        response = coach_client.post("/api/ask/stream", json={"question": "Hello"})
    finally:
        coach_rate_limiter.limit = 30
        coach_rate_limiter.reset()

    assert response.status_code == 429


def test_separate_clients_have_separate_budgets() -> None:
    limiter = type(coach_rate_limiter)(limit=1, window_seconds=60.0)

    assert limiter.check("10.0.0.1") is None
    assert limiter.check("10.0.0.1") is not None
    # A different caller is unaffected.
    assert limiter.check("10.0.0.2") is None


def test_the_window_rolls_forward() -> None:
    limiter = type(coach_rate_limiter)(limit=1, window_seconds=60.0)

    assert limiter.check("a", now=0.0) is None
    assert limiter.check("a", now=30.0) is not None
    # Once the first hit ages out, the budget is available again.
    assert limiter.check("a", now=61.0) is None


def test_rate_limiting_can_be_disabled() -> None:
    limiter = type(coach_rate_limiter)(limit=0, window_seconds=60.0)

    assert all(limiter.check("a") is None for _ in range(100))
