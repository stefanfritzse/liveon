"""FastAPI web application for the Live On Longevity Coach platform"""

from __future__ import annotations
from collections.abc import Callable
from functools import lru_cache
from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import json
import logging
import os
import secrets
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlparse
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from app.models.content import Article, Tip
from app.services.coach import (
    CoachAgent,
    CoachError,
    CoachTimeoutError,
    CoachUnavailableError,
    create_coach_llm,
    resolve_llm_timeout,
)
from app.services.pipeline_scheduler import create_pipeline_scheduler
from app.utils.text import markdown_to_plain_text, markdown_to_html
from app.services.sqlite_repo import LocalSQLiteContentRepository

def _normalize_root_path(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned or cleaned == "/":
        return ""
    return "/" + cleaned.strip("/")


ROOT_PATH = _normalize_root_path(os.getenv("LIVEON_ROOT_PATH", ""))
app = FastAPI(title="Live On Longevity Coach", root_path=ROOT_PATH)

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals.update(now=lambda: datetime.now(timezone.utc))
templates.env.filters["markdown_to_text"] = markdown_to_plain_text
templates.env.filters["markdown_to_html"] = markdown_to_html

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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


@lru_cache
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


def _consume_abandoned_task(task: "asyncio.Task[object]") -> None:
    """Retrieve an abandoned task's outcome so asyncio does not log it as unhandled."""

    if not task.cancelled():
        task.exception()


async def _run_with_deadline(func: Callable[..., object], *args: object, timeout: float) -> object:
    """Run a blocking callable in a worker thread under a wall-clock deadline.

    A Python thread cannot be interrupted, so when the deadline expires the worker is
    *abandoned* rather than awaited: the caller regains control immediately and the
    thread finishes on its own (or dies on the client's transport timeout) with its
    result discarded. ``asyncio.wait_for`` cannot be used here because it waits for the
    cancelled task to settle, which for an uninterruptible thread means waiting out the
    very call the deadline was meant to bound.
    """

    task: "asyncio.Task[object]" = asyncio.ensure_future(asyncio.to_thread(func, *args))
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.cancel()
        task.add_done_callback(_consume_abandoned_task)
        raise TimeoutError(f"Call exceeded the {timeout:g}s deadline.")
    return task.result()


def _debug_errors_enabled() -> bool:
    """Return ``True`` when exception details may be sent to the client."""

    raw = (os.getenv("LIVEON_DEBUG_ERRORS") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _error_detail(message: str, exc: Exception, *, event: str) -> dict[str, object]:
    """Log ``exc`` against a fresh reference id and build the client-facing detail.

    Internal exception text is not something a visitor asking about sleep should ever
    read, so the default payload carries only a reference the operator can grep for in
    the logs. Set ``LIVEON_DEBUG_ERRORS=1`` to inline the details while developing.
    """

    reference = uuid.uuid4().hex[:12]
    logger.exception(
        "%s (reference=%s)",
        message,
        reference,
        extra={"event": event, "error_reference": reference},
    )

    detail: dict[str, object] = {"message": message, "reference": reference}
    if _debug_errors_enabled():
        detail["debug"] = _build_debug_detail(exc)
    return detail


@app.on_event("startup")
async def _log_admin_console_state() -> None:
    if admin_console_enabled():
        logger.info("Admin console enabled", extra={"event": "admin.enabled"})
    else:
        logger.warning(
            "Admin console disabled: set LIVEON_ADMIN_PASSWORD to enable content management",
            extra={"event": "admin.disabled"},
        )


@app.on_event("startup")
async def _start_pipeline_scheduler() -> None:
    scheduler = create_pipeline_scheduler()
    if scheduler is None:
        logger.info("Pipeline scheduler disabled", extra={"event": "pipeline_scheduler.disabled"})
        return
    app.state.pipeline_scheduler = scheduler
    await scheduler.start()


@app.on_event("shutdown")
async def _stop_pipeline_scheduler() -> None:
    scheduler = getattr(app.state, "pipeline_scheduler", None)
    if scheduler is None:
        return
    await scheduler.stop()

@app.get("/healthz")
def healthz():
    return {"ok": True}

@lru_cache
def _cached_coach_agent() -> CoachAgent:
    """Create a singleton CoachAgent backed by the configured language model."""

    llm = create_coach_llm()
    return CoachAgent(llm=llm)


def get_coach_agent() -> CoachAgent:
    """FastAPI dependency returning the shared CoachAgent instance."""

    try:
        return _cached_coach_agent()
    except Exception as exc:  # noqa: BLE001 - surfaced to the client as 503
        raise HTTPException(
            status_code=503,
            detail=_error_detail(
                "Coach service temporarily unavailable",
                exc,
                event="coach.agent_init",
            ),
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

    def delete_article(self, article_id: str) -> bool:
        """Remove an article, returning ``True`` when a row was deleted."""

    def delete_tip(self, tip_id: str) -> bool:
        """Remove a tip, returning ``True`` when a row was deleted."""


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

    def delete_article(self, article_id: str) -> bool:
        before = len(self._articles)
        self._articles = [article for article in self._articles if article.id != article_id]
        return len(self._articles) != before

    def delete_tip(self, tip_id: str) -> bool:
        before = len(self._tips)
        self._tips = [tip for tip in self._tips if tip.id != tip_id]
        return len(self._tips) != before

def get_repository() -> ContentRepository:
    """Resolve the content repository (SQLite only)."""
    storage = (os.getenv("LIVEON_STORAGE") or "sqlite").strip().lower()

    if storage == "sqlite":
        try:
            db_path = os.getenv("LIVEON_DB_PATH")
            return LocalSQLiteContentRepository(db_path=db_path)
        except Exception as exc:
            logger.exception("SQLite repository init failed; falling back to in-memory.")
            return _InMemoryContentRepository()

    # Fallback for any other storage type
    logger.warning("Unsupported storage type '%s'; falling back to in-memory.", storage)
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

    # The model call is synchronous and can run for minutes on a local 14B model.
    # Running it on the event loop would freeze every other request for the duration
    # and starve the container's liveness probe into restarting the pod mid-answer.
    #
    # The client's own timeout only bounds the gap between streamed tokens, so a model
    # that keeps emitting slowly would never trip it. The wall-clock ceiling here is
    # what actually guarantees the request ends. The abandoned worker thread finishes
    # on its own (or dies on the transport timeout); its result is simply discarded.
    try:
        answer = await _run_with_deadline(
            agent.ask, question, timeout=resolve_llm_timeout()
        )
    except (CoachTimeoutError, TimeoutError) as exc:
        raise HTTPException(
            status_code=504,
            detail=_error_detail(
                "The coach took too long to respond. The local model may still be loading — "
                "try again in a moment.",
                exc,
                event="coach.timeout",
            ),
        ) from exc
    except CoachUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=_error_detail(
                "The coach is offline. Check that the local model server is running.",
                exc,
                event="coach.unavailable",
            ),
        ) from exc
    except CoachError as exc:
        # Reached the model but could not get an answer out of it (model not pulled,
        # bad response, protocol error). The dependency is at fault, not the request.
        raise HTTPException(
            status_code=503,
            detail=_error_detail(
                "The coach language model could not complete the request.",
                exc,
                event="coach.llm_error",
            ),
        ) from exc
    except Exception as exc:  # noqa: BLE001 - every failure gets a friendly answer
        raise HTTPException(
            status_code=500,
            detail=_error_detail(
                "The coach could not answer that question.",
                exc,
                event="coach.error",
            ),
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


# ----------------------------------------------------------------------
# Admin console access control
# ----------------------------------------------------------------------
# The console can permanently delete published content, so it is gated on credentials
# supplied through the environment. When none are configured it is switched off rather
# than left open: an unconfigured deployment should not ship a public delete button.

_admin_security = HTTPBasic(auto_error=False)

_ADMIN_AUTH_HEADERS = {"WWW-Authenticate": 'Basic realm="Live On admin console"'}


def admin_credentials() -> tuple[str, str] | None:
    """Return the configured admin username/password, or ``None`` when disabled."""

    password = os.getenv("LIVEON_ADMIN_PASSWORD") or ""
    if not password:
        return None
    username = (os.getenv("LIVEON_ADMIN_USER") or "admin").strip() or "admin"
    return username, password


def admin_console_enabled() -> bool:
    """Return ``True`` when admin credentials have been configured."""

    return admin_credentials() is not None


def require_admin(
    credentials: HTTPBasicCredentials | None = Depends(_admin_security),
) -> str:
    """Authenticate an admin request, returning the verified username."""

    expected = admin_credentials()
    if expected is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "The admin console is disabled. Set LIVEON_ADMIN_PASSWORD "
                "(and optionally LIVEON_ADMIN_USER) to enable it."
            ),
        )

    expected_user, expected_password = expected
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Admin credentials required.",
            headers=_ADMIN_AUTH_HEADERS,
        )

    # Compare both fields unconditionally so the response time does not reveal
    # which half of the credential was wrong.
    user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"), expected_user.encode("utf-8")
    )
    password_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"), expected_password.encode("utf-8")
    )
    if not (user_ok and password_ok):
        logger.warning(
            "Rejected admin credentials", extra={"event": "admin.auth_failed"}
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid admin credentials.",
            headers=_ADMIN_AUTH_HEADERS,
        )

    return credentials.username


def _request_is_same_origin(request: Request) -> bool:
    """Return ``True`` when the request originates from this site.

    Browsers attach ``Origin`` to cross-site form posts, so comparing it against the
    ``Host`` we were addressed by blocks a third-party page from driving the admin
    console with the browser's cached Basic-auth credentials.
    """

    host = request.headers.get("host")
    if not host:
        return False

    for header in ("origin", "referer"):
        raw = request.headers.get(header)
        if not raw:
            continue
        netloc = urlparse(raw).netloc
        return bool(netloc) and netloc == host

    # Neither header present: a browser always sends at least one on a cross-site
    # POST, so treat the absence as untrusted.
    return False


def require_admin_write(
    request: Request,
    username: str = Depends(require_admin),
) -> str:
    """Authenticate a state-changing admin request and reject cross-site submissions."""

    if not _request_is_same_origin(request):
        logger.warning(
            "Rejected cross-origin admin write",
            extra={
                "event": "admin.cross_origin_blocked",
                "origin": request.headers.get("origin"),
            },
        )
        raise HTTPException(
            status_code=403,
            detail="Cross-origin admin requests are not allowed.",
        )
    return username


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    repository: ContentRepository = Depends(get_repository),
    _: str = Depends(require_admin),
) -> HTMLResponse:
    """Render the administrative console."""

    articles = repository.get_latest_articles(limit=20)
    tips = repository.get_latest_tips(limit=20)

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "title": "Admin Console",
            "articles": articles,
            "tips": tips,
        },
    )


@app.post("/admin/articles/{article_id}/delete")
async def delete_article_admin(
    request: Request,
    article_id: str,
    repository: ContentRepository = Depends(get_repository),
    username: str = Depends(require_admin_write),
) -> RedirectResponse:
    """Handle deletion of an article from the admin console."""

    deleted = repository.delete_article(article_id)
    logger.info(
        "Admin deleted article",
        extra={
            "event": "admin.article_deleted",
            "article_id": article_id,
            "actor": username,
            "deleted": deleted,
        },
    )
    return RedirectResponse(
        request.url_for("admin_dashboard"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/admin/tips/{tip_id}/delete")
async def delete_tip_admin(
    request: Request,
    tip_id: str,
    repository: ContentRepository = Depends(get_repository),
    username: str = Depends(require_admin_write),
) -> RedirectResponse:
    """Handle deletion of a tip from the admin console."""

    deleted = repository.delete_tip(tip_id)
    logger.info(
        "Admin deleted tip",
        extra={
            "event": "admin.tip_deleted",
            "tip_id": tip_id,
            "actor": username,
            "deleted": deleted,
        },
    )
    return RedirectResponse(
        request.url_for("admin_dashboard"),
        status_code=status.HTTP_303_SEE_OTHER,
    )
