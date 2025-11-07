"""FastAPI web application for the Live On Longevity Coach platform"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
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
from app.utils.text import markdown_to_plain_text
from app.services.sqlite_repo import LocalSQLiteContentRepository

app = FastAPI(title="Live On Longevity Coach")

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals.update(now=lambda: datetime.now(timezone.utc))
templates.env.filters["markdown_to_text"] = markdown_to_plain_text

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - for static type checking only
    from app.models.coach import CoachAnswer


_DEFAULT_COACH_PROMPTS: tuple[dict[str, str], ...] = (
    {
        "label": "Restore deeper sleep",
        "question": "How can I improve my sleep quality and recovery this month?",
        "description": "Wind-down habits and environment tweaks for restorative rest.",
    },
    {
        "label": "Plan longevity workouts",
        "question": "What mix of strength, cardio, and mobility should I follow each week?",
        "description": "Balance resistance, aerobic, and mobility training across 7 days.",
    },
    {
        "label": "Support brain health",
        "question": "Which nutrition habits best protect long-term cognitive health?",
        "description": "Everyday food choices that reinforce brain resilience.",
    },
)


def _coach_prompt_suggestions() -> tuple[dict[str, str], ...]:
    """Return curated coach prompt presets, optionally overridden by environment."""

    raw_value = os.getenv("LIVEON_COACH_PROMPTS")
    if raw_value:
        try:
            payload = json.loads(raw_value)
        except (TypeError, ValueError):  # pragma: no cover - defensive branch
            logger.warning("Invalid LIVEON_COACH_PROMPTS payload; using defaults", extra={"event": "coach.prompts_invalid"})
        else:
            prompts: list[dict[str, str]] = []
            for item in payload if isinstance(payload, list) else [payload]:
                if isinstance(item, str):
                    text = item.strip()
                    if text:
                        prompts.append({"label": text, "question": text})
                    continue
                if not isinstance(item, dict):
                    continue
                question = str(item.get("question", "")).strip()
                if not question:
                    continue
                label = str(item.get("label") or item.get("title") or question).strip() or question
                description = str(item.get("description") or item.get("summary") or "").strip()
                entry = {"label": label, "question": question}
                if description:
                    entry["description"] = description
                prompts.append(entry)
            if prompts:
                return tuple(prompts)
    return _DEFAULT_COACH_PROMPTS

def _build_debug_detail(exc: Exception) -> dict[str, str]:
    """Return a serialisable mapping describing ``exc`` for debugging."""

    message = str(exc).strip()
    return {
        "type": type(exc).__name__,
        "message": message or "No exception message provided.",
    }

@app.get("/healthz")
def healthz():
    return {"ok": True}

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
        debug_detail = _build_debug_detail(exc)
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Coach service temporarily unavailable",
                "debug": debug_detail,
            },
        ) from exc


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
    """Fallback repository used when the database is unavailable during local dev."""

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
                    "This in-memory article appears when the database is not configured so "
                    "that the web experience remains usable during development."
                ),
                summary="An introduction article displayed when database access is unavailable.",
                source_urls=[],
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
    """Resolve the content repository (SQLite locally, Firestore in cloud)."""
    #storage = (os.getenv("LIVEON_STORAGE") or "sqlite").strip().lower()
    storage = "sqlite"

    if storage == "sqlite":
        try:
            db_path = os.getenv("LIVEON_DB_PATH")
            return LocalSQLiteContentRepository(db_path=db_path)
        except Exception as exc:
            logger.exception("SQLite repository init failed; falling back to in-memory.")
            return _InMemoryContentRepository()

    # default: Firestore (with graceful fallback)
    try:
        db_path = os.getenv("LIVEON_DB_PATH")
        return LocalSQLiteContentRepository(db_path=db_path)
    except Exception as exc:
        logger.exception("SQLite repository init failed; falling back to in-memory.")
        return _InMemoryContentRepository()



@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    repository: ContentRepository = Depends(get_repository),
) -> HTMLResponse:
    """Render the homepage with highlights from articles and tips."""

    articles = repository.get_latest_articles(limit=3)
    featured_tip = repository.get_latest_tip()
    tips = repository.get_latest_tips(limit=4)
    recent_tips = [tip for tip in tips if not featured_tip or tip != featured_tip]
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "title": "Live On Longevity Coach",
            "articles": articles,
            "featured_tip": featured_tip,
            "recent_tips": recent_tips,
        },
    )


@app.get("/api/tips/latest", response_class=JSONResponse)
async def fetch_latest_tip(
    repository: ContentRepository = Depends(get_repository),
) -> JSONResponse:
    """Return the most recent coaching tip for client-side integrations."""

    tip = repository.get_latest_tip()
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

        debug_detail = _build_debug_detail(exc)


        raise HTTPException(
            status_code=503,
            detail={
                "message": "Coach language model unavailable",
                "debug": debug_detail,
            },
        ) from exc

    return AskCoachResponse.from_coach_answer(answer)


@app.get("/articles", response_class=HTMLResponse)
async def list_articles(
    request: Request,
    repository: ContentRepository = Depends(get_repository),
) -> HTMLResponse:
    """Render a page containing the latest longevity articles."""

    articles = repository.get_latest_articles(limit=20)
    return templates.TemplateResponse(
        request,
        "articles/list.html",
        {
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

    article = repository.get_article(article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")

    return templates.TemplateResponse(
        request,
        "articles/detail.html",
        {
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

    tips = repository.get_latest_tips(limit=20)
    featured_tip = tips[0] if tips else None
    recent_tips = tips[1:] if len(tips) > 1 else []
    return templates.TemplateResponse(
        request,
        "tips/list.html",
        {
            "title": "Longevity Tips",
            "featured_tip": featured_tip,
            "recent_tips": recent_tips,
        },
    )


@app.get("/coach", response_class=HTMLResponse)
async def ask_the_coach(request: Request) -> HTMLResponse:
    """Render the placeholder page for the future interactive coach experience."""

    return templates.TemplateResponse(
        request,
        "coach.html",
        {
            "title": "Ask the Coach",
            "coach_prompts": list(_coach_prompt_suggestions()),
        },
    )
