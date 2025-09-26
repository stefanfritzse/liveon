"""FastAPI web application for the Live On Longevity Coach platform."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import DefaultCredentialsError

from app.models.content import Article, Tip
from app.services.firestore import FirestoreContentRepository

app = FastAPI(title="Live On Longevity Coach")

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals.update(now=lambda: datetime.now(timezone.utc))


class ContentRepository(Protocol):
    """Contract for retrieving longevity content."""

    def get_latest_articles(self, *, limit: int = 5) -> list[Article]:
        """Return the newest articles."""

    def get_article(self, article_id: str) -> Article | None:
        """Return a single article or ``None`` when not found."""

    def get_latest_tips(self, *, limit: int = 5) -> list[Tip]:
        """Return the newest longevity tips."""


@dataclass(slots=True)
class _InMemoryContentRepository:
    """Fallback repository used when Firestore is unavailable during local dev."""

    _articles: list[Article]
    _tips: list[Tip]

    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self._articles = [
            Article(
                id="welcome-to-live-on",
                title="Welcome to Live On",
                content_body=(
                    "Live On keeps you informed about actionable longevity science. "
                    "This in-memory article appears when Firestore is not configured so "
                    "that the web experience remains usable during development."
                ),
                summary="An introduction article displayed when Firestore access is unavailable.",
                source_urls=["https://cloud.google.com/firestore/docs"],
                tags=["introduction", "platform"],
                published_date=now,
            ),
        ]
        self._tips = [
            Tip(
                id="stay-hydrated",
                title="Hydration Reminder",
                content_body="Staying hydrated supports cellular health and overall longevity.",
                tags=["habit", "daily"],
                published_date=now,
            )
        ]

    def get_latest_articles(self, *, limit: int = 5) -> list[Article]:
        return sorted(self._articles, key=lambda article: article.published_date, reverse=True)[:limit]

    def get_article(self, article_id: str) -> Article | None:
        return next((article for article in self._articles if article.id == article_id), None)

    def get_latest_tips(self, *, limit: int = 5) -> list[Tip]:
        return sorted(self._tips, key=lambda tip: tip.published_date, reverse=True)[:limit]


def get_repository() -> ContentRepository:
    """Resolve the content repository with graceful fallback when Firestore is unavailable."""

    try:
        return FirestoreContentRepository()
    except (DefaultCredentialsError, GoogleAPIError):
        return _InMemoryContentRepository()


def _safe_fetch(callback: Callable[[], list[Article] | list[Tip]]) -> list[Article] | list[Tip]:
    """Execute a repository call, swallowing Firestore errors for a smooth UX."""

    try:
        return callback()
    except (DefaultCredentialsError, GoogleAPIError):
        return []


@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    repository: ContentRepository = Depends(get_repository),
) -> HTMLResponse:
    """Render the homepage with highlights from articles and tips."""

    articles = _safe_fetch(lambda: repository.get_latest_articles(limit=3))
    tips = _safe_fetch(lambda: repository.get_latest_tips(limit=3))
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "title": "Live On Longevity Coach",
            "articles": articles,
            "tips": tips,
        },
    )


@app.get("/articles", response_class=HTMLResponse)
async def list_articles(
    request: Request,
    repository: ContentRepository = Depends(get_repository),
) -> HTMLResponse:
    """Render a page containing the latest longevity articles."""

    articles = _safe_fetch(lambda: repository.get_latest_articles(limit=20))
    return templates.TemplateResponse(
        "articles/list.html",
        {
            "request": request,
            "title": "Longevity Articles",
            "articles": articles,
        },
    )


@app.get("/articles/{article_id}", response_class=HTMLResponse)
async def article_detail(
    request: Request,
    article_id: str,
    repository: ContentRepository = Depends(get_repository),
) -> HTMLResponse:
    """Render the article detail page."""

    try:
        article = repository.get_article(article_id)
    except (DefaultCredentialsError, GoogleAPIError) as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=503, detail="Content service unavailable") from exc

    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")

    return templates.TemplateResponse(
        "articles/detail.html",
        {
            "request": request,
            "title": article.title,
            "article": article,
        },
    )


@app.get("/tips", response_class=HTMLResponse)
async def list_tips(
    request: Request,
    repository: ContentRepository = Depends(get_repository),
) -> HTMLResponse:
    """Render a page containing the latest coaching tips."""

    tips = _safe_fetch(lambda: repository.get_latest_tips(limit=20))
    return templates.TemplateResponse(
        "tips/list.html",
        {
            "request": request,
            "title": "Longevity Tips",
            "tips": tips,
        },
    )


@app.get("/coach", response_class=HTMLResponse)
async def ask_the_coach(request: Request) -> HTMLResponse:
    """Render the placeholder page for the future interactive coach experience."""

    return templates.TemplateResponse(
        "coach.html",
        {
            "request": request,
            "title": "Ask the Coach",
        },
    )
