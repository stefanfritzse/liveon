"""FastAPI web application for the Live On Longevity Coach platform"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import DefaultCredentialsError
from pydantic import BaseModel, Field, field_validator

from app.models.content import Article, Tip
from app.services.coach import CoachAgent, create_coach_llm
from app.services.firestore import FirestoreContentRepository
from app.services.monitoring import GCPMetricsService
from app.utils.text import markdown_to_plain_text

app = FastAPI(title="Live On Longevity Coach")

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals.update(now=lambda: datetime.now(timezone.utc))
templates.env.filters["markdown_to_text"] = markdown_to_plain_text

metrics_service = GCPMetricsService()
logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - for static type checking only
    from app.models.coach import CoachAnswer


@lru_cache()
def _cached_coach_agent() -> CoachAgent:
    """Create a singleton CoachAgent backed by the configured language model."""

    llm = create_coach_llm()
    return CoachAgent(llm=llm)


def get_coach_agent() -> CoachAgent:
    """FastAPI dependency returning the shared CoachAgent instance."""

    try:
        return _cached_coach_agent()
    except (DefaultCredentialsError, RuntimeError) as exc:
        logger.exception("Coach agent initialisation failed", extra={"event": "coach.agent_init"})
        raise HTTPException(status_code=503, detail="Coach service temporarily unavailable") from exc


class AskCoachRequest(BaseModel):
    """API payload submitted by clients requesting coach guidance."""

    question: str = Field(..., description="The longevity-related question to ask the coach.")

    @field_validator("question")
    @classmethod
    def _ensure_question_not_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Question must not be empty.")
        return cleaned

    @property
    def sanitized(self) -> str:
        """Return the trimmed question text ready for downstream use."""

        return self.question.strip()


class AskCoachResponse(BaseModel):
    """Structured response returned by the coach endpoint."""

    answer: str = Field(..., description="The coach's guidance for the submitted question.")
    disclaimer: str = Field(..., description="Safety disclaimer appended to every response.")

    @classmethod
    def from_coach_answer(cls, answer: "CoachAnswer") -> "AskCoachResponse":
        return cls(
            answer=answer.message,
            disclaimer=answer.disclaimer,
        )


class ContentRepository(Protocol):
    """Contract for retrieving longevity content."""

    def get_latest_articles(self, *, limit: int = 5) -> list[Article]:
        """Return the newest articles."""

    def get_article(self, article_id: str) -> Article | None:
        """Return a single article or ``None`` when not found."""

    def get_latest_tips(self, *, limit: int = 5) -> list[Tip]:
        """Return the newest longevity tips."""

    def get_latest_tip(self) -> Tip | None:
        """Return the most recent tip when available."""


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

    def get_latest_tip(self) -> Tip | None:
        return next(iter(self.get_latest_tips(limit=1)), None)


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


def _safe_fetch_tip(callback: Callable[[], Tip | None]) -> Tip | None:
    """Execute a repository call returning a single tip with graceful error handling."""

    try:
        return callback()
    except (DefaultCredentialsError, GoogleAPIError):
        return None


@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    repository: ContentRepository = Depends(get_repository),
) -> HTMLResponse:
    """Render the homepage with highlights from articles and tips."""

    articles = _safe_fetch(lambda: repository.get_latest_articles(limit=3))
    featured_tip = _safe_fetch_tip(repository.get_latest_tip)
    tips = _safe_fetch(lambda: repository.get_latest_tips(limit=4))
    recent_tips = [tip for tip in tips if not featured_tip or tip != featured_tip]
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "title": "Live On Longevity Coach",
            "articles": articles,
            "featured_tip": featured_tip,
            "recent_tips": recent_tips,
        },
    )


@app.get("/api/metrics/run-pipeline", response_class=JSONResponse)
async def fetch_run_pipeline_metrics() -> JSONResponse:
    """Return health metrics for the ``run_pipeline`` pipeline trigger."""

    payload = metrics_service.fetch_run_pipeline_health(
        tips_job_id=GCPMetricsService.DEFAULT_TIP_JOB_ID
    )
    status_code = 200
    if payload.get("status") == "error":
        status_code = 503
    return JSONResponse(content=payload, status_code=status_code)


@app.get("/api/tips/latest", response_class=JSONResponse)
async def fetch_latest_tip(
    repository: ContentRepository = Depends(get_repository),
) -> JSONResponse:
    """Return the most recent coaching tip for client-side integrations."""

    tip = _safe_fetch_tip(repository.get_latest_tip)
    if tip is None:
        return JSONResponse({"detail": "No tips available"}, status_code=404)

    return JSONResponse(
        {
            "id": tip.id,
            "title": tip.title,
            "content_body": tip.content_body,
            "published_date": tip.published_date.isoformat(),
            "tags": tip.tags,
        }
    )


@app.post("/api/ask", response_model=AskCoachResponse)
async def ask_coach_endpoint(
    payload: AskCoachRequest,
    agent: CoachAgent = Depends(get_coach_agent),
) -> AskCoachResponse:
    """Handle Ask the Coach API queries and return structured guidance."""

    question = payload.sanitized
    if not question:
        raise HTTPException(status_code=400, detail="Question must not be empty.")

    logger.info(
        "Coach request received",
        extra={
            "event": "coach.request",
            "question_length": len(question),
        },
    )

    try:
        answer = agent.ask(question)
    except (DefaultCredentialsError, GoogleAPIError, RuntimeError) as exc:
        logger.exception(
            "Coach language model unavailable",
            extra={"event": "coach.error", "reason": "llm"},
        )
        raise HTTPException(status_code=503, detail="Coach language model unavailable") from exc

    return AskCoachResponse.from_coach_answer(answer)


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
    featured_tip = tips[0] if tips else None
    recent_tips = tips[1:] if len(tips) > 1 else []
    return templates.TemplateResponse(
        "tips/list.html",
        {
            "request": request,
            "title": "Longevity Tips",
            "featured_tip": featured_tip,
            "recent_tips": recent_tips,
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
