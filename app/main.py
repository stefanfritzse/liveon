"""FastAPI web application for the Live On Longevity Coach platform"""

from __future__ import annotations
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Callable
from functools import lru_cache
from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import json
import time
import logging
import os
import secrets
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import parse_qsl, urlparse
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from app.models.coach import COACH_ROLE, USER_ROLE, CoachQuestion, CoachTurn
from app.models.content import Article, ContentPage, Tip
from app.services.coach import (
    CoachAgent,
    CoachError,
    CoachTimeoutError,
    CoachUnavailableError,
    create_coach_llm,
    resolve_llm_timeout,
    separate_disclaimer,
)
from app.services.pipeline_scheduler import (
    CADENCES,
    create_pipeline_scheduler,
    resolve_cadence_key,
)
from app.utils.text import markdown_to_plain_text, markdown_to_html
from app.services.sqlite_repo import LocalSQLiteContentRepository

def _normalize_root_path(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned or cleaned == "/":
        return ""
    return "/" + cleaned.strip("/")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop the app's background pieces.

    Replaces the deprecated ``@app.on_event`` handlers. The helpers referenced here
    are defined further down the module; they are only called at run time.
    """

    if admin_console_enabled():
        logger.info("Admin console enabled", extra={"event": "admin.enabled"})
    else:
        logger.warning(
            "Admin console disabled: set LIVEON_ADMIN_PASSWORD to enable content management",
            extra={"event": "admin.disabled"},
        )

    scheduler = create_pipeline_scheduler()
    if scheduler is None:
        logger.info("Pipeline scheduler disabled", extra={"event": "pipeline_scheduler.disabled"})
    else:
        app.state.pipeline_scheduler = scheduler
        await scheduler.start()

    try:
        yield
    finally:
        if scheduler is not None:
            await scheduler.stop()

        # The repository now lives for the process, so close it explicitly.
        repository = getattr(app.state, "content_repository", None)
        closer = getattr(repository, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.exception("Failed to close the content repository")
        app.state.content_repository = None


ROOT_PATH = _normalize_root_path(os.getenv("LIVEON_ROOT_PATH", ""))
app = FastAPI(title="Live On Longevity Coach", root_path=ROOT_PATH, lifespan=lifespan)

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


class _SlidingWindowRateLimiter:
    """Per-client request budget over a rolling window.

    Deliberately in-process and in-memory: it exists to stop one browser tab (or a
    stuck retry loop) from monopolising a single local model, not to defend a public
    API. Multiple workers each get their own budget.
    """

    def __init__(self, *, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, *, now: float | None = None) -> float | None:
        """Record a request; return seconds to wait when the budget is exhausted."""

        if self.limit <= 0:
            return None

        moment = time.monotonic() if now is None else now
        hits = self._hits[key]
        cutoff = moment - self.window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= self.limit:
            return max(0.0, hits[0] + self.window_seconds - moment)

        hits.append(moment)
        return None

    def reset(self) -> None:
        self._hits.clear()


def _resolve_rate_limit() -> int:
    raw = (os.getenv("LIVEON_ASK_RATE_LIMIT") or "").strip()
    if not raw:
        return 30
    try:
        return max(0, int(raw))
    except ValueError:
        return 30


coach_rate_limiter = _SlidingWindowRateLimiter(limit=_resolve_rate_limit(), window_seconds=60.0)


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def enforce_coach_rate_limit(request: Request) -> None:
    """Reject a client that is asking faster than the budget allows."""

    retry_after = coach_rate_limiter.check(_client_key(request))
    if retry_after is None:
        return

    logger.warning(
        "Coach rate limit exceeded",
        extra={"event": "coach.rate_limited", "client": _client_key(request)},
    )
    raise HTTPException(
        status_code=429,
        detail={
            "message": (
                "You're asking faster than the coach can answer. "
                "Give it a moment and try again."
            ),
            "retry_after": int(retry_after) + 1,
        },
        headers={"Retry-After": str(int(retry_after) + 1)},
    )


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


# ----------------------------------------------------------------------
# Error presentation
# ----------------------------------------------------------------------
# A visitor who mistypes an article URL used to get raw JSON with no header, no
# navigation, and no way back. Browsers get a styled page; API clients keep JSON.

_ERROR_HEADINGS: dict[int, tuple[str, str]] = {
    400: ("That request didn't look right", "Something about that request was malformed."),
    401: ("Sign in to continue", "This area needs credentials."),
    403: ("Not allowed", "You don't have access to that."),
    404: ("Page not found", "We couldn't find what you were looking for."),
    429: ("Slow down a moment", "You're sending requests faster than we can answer them."),
    500: ("Something went wrong", "An unexpected error occurred on our side."),
    503: ("Temporarily unavailable", "This part of the site isn't available right now."),
    504: ("That took too long", "The request timed out before it completed."),
}


def _wants_json(request: Request) -> bool:
    """Return ``True`` when the caller is an API client rather than a browser."""

    path = request.url.path
    if path.startswith("/api/") or path == "/healthz":
        return True

    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return False
    # An explicit JSON preference, or a non-browser client that stated nothing useful.
    return "application/json" in accept or not accept


def _describe_error(status_code: int, detail: object) -> tuple[str, str, str | None]:
    """Return the heading, message, and reference id to show for an error."""

    heading, fallback = _ERROR_HEADINGS.get(
        status_code, ("Something went wrong", "An unexpected error occurred.")
    )

    reference: str | None = None
    message = fallback
    if isinstance(detail, dict):
        message = str(detail.get("message") or fallback)
        raw_reference = detail.get("reference")
        reference = str(raw_reference) if raw_reference else None
    elif isinstance(detail, str) and detail.strip():
        message = detail.strip()

    return heading, message, reference


def _render_error_page(
    request: Request,
    status_code: int,
    detail: object,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    heading, message, reference = _describe_error(status_code, detail)
    return templates.TemplateResponse(
        request,
        "errors/error.html",
        {
            "title": heading,
            "heading": heading,
            "message": message,
            "status_code": status_code,
            "reference": reference,
        },
        status_code=status_code,
        headers=headers,
    )


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    headers = getattr(exc, "headers", None)
    if _wants_json(request):
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=headers
        )
    # Headers are preserved so, for example, a 401 still triggers the browser's
    # credential prompt rather than just showing a page about it.
    return _render_error_page(request, exc.status_code, exc.detail, headers)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    detail = _error_detail(
        "An unexpected error occurred.", exc, event="app.unhandled_error"
    )
    if _wants_json(request):
        return JSONResponse({"detail": detail}, status_code=500)
    return _render_error_page(request, 500, detail)


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


#: Ceiling on a single submitted history turn. The client sends the transcript, so the
#: request body is untrusted input and is bounded before it reaches the model.
_MAX_HISTORY_TURN_CHARS = 4000
_MAX_HISTORY_TURNS_ACCEPTED = 40

#: Ceiling on a single question. A pasted novel becomes a multi-minute generation that
#: occupies a worker and the model for everyone else.
MAX_QUESTION_CHARS = 2000


class CoachHistoryTurn(BaseModel):
    """One earlier message in the conversation, as supplied by the client."""

    role: str = Field(..., description="Either 'user' or 'coach'.")
    text: str = Field(..., max_length=_MAX_HISTORY_TURN_CHARS)

    @field_validator("role")
    @classmethod
    def _normalise_role(cls, value: str) -> str:
        cleaned = (value or "").strip().lower()
        if cleaned in {"user", "human"}:
            return USER_ROLE
        if cleaned in {"coach", "assistant", "ai"}:
            return COACH_ROLE
        raise ValueError("Role must be 'user' or 'coach'.")


class AskCoachRequest(BaseModel):
    """API payload submitted by clients requesting coach guidance."""

    question: str = Field(
        ...,
        max_length=MAX_QUESTION_CHARS,
        description="The longevity-related question to ask the coach.",
    )
    history: list[CoachHistoryTurn] = Field(
        default_factory=list,
        max_length=_MAX_HISTORY_TURNS_ACCEPTED,
        description="Earlier turns of this conversation, oldest first.",
    )

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

    def to_coach_question(self) -> CoachQuestion:
        """Build the domain object, letting the agent apply its own history budget."""

        return CoachQuestion(
            text=self.sanitized,
            history=[CoachTurn(role=turn.role, text=turn.text) for turn in self.history],
        )


class AskCoachResponse(BaseModel):
    """Structured response returned by the coach endpoint.

    Both the raw Markdown and the rendered HTML are returned. The browser displays
    the HTML, which keeps the server as the single authoritative renderer; the
    lightweight client-side renderer is only a preview while tokens stream in.
    """

    answer: str = Field(..., description="The coach's guidance for the submitted question.")
    disclaimer: str = Field(..., description="Safety disclaimer appended to every response.")
    answer_html: str = Field("", description="``answer`` rendered to sanitised HTML.")
    disclaimer_html: str = Field("", description="``disclaimer`` rendered to sanitised HTML.")

    @classmethod
    def from_coach_answer(cls, answer: "CoachAnswer") -> "AskCoachResponse":
        return cls(
            answer=answer.message,
            disclaimer=answer.disclaimer,
            answer_html=str(markdown_to_html(answer.message)),
            disclaimer_html=str(markdown_to_html(answer.disclaimer)),
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

    def browse_articles(
        self,
        *,
        query: str | None = None,
        tag: str | None = None,
        page: int = 1,
        per_page: int = 10,
    ) -> ContentPage:
        """Return a filtered, paginated page of articles."""

    def browse_tips(
        self,
        *,
        query: str | None = None,
        tag: str | None = None,
        page: int = 1,
        per_page: int = 10,
    ) -> ContentPage:
        """Return a filtered, paginated page of tips."""


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

    def browse_articles(self, **kwargs: object) -> ContentPage:
        return _paginate_in_memory(self.get_latest_articles(limit=1000), **kwargs)

    def browse_tips(self, **kwargs: object) -> ContentPage:
        return _paginate_in_memory(self.get_latest_tips(limit=1000), **kwargs)

def _paginate_in_memory(
    items: list,
    *,
    query: str | None = None,
    tag: str | None = None,
    page: int = 1,
    per_page: int = 10,
) -> ContentPage:
    """Filter and slice an in-memory list the same way the SQLite repository does."""

    page = max(1, int(page))
    per_page = max(1, int(per_page))

    cleaned_query = (query or "").strip().casefold()
    cleaned_tag = (tag or "").strip().casefold()

    matches = []
    tags: list[str] = []
    for item in items:
        for value in getattr(item, "tags", []) or []:
            if value not in tags:
                tags.append(value)
        if cleaned_tag and not any(
            value.casefold() == cleaned_tag for value in getattr(item, "tags", []) or []
        ):
            continue
        if cleaned_query:
            haystack = " ".join(
                str(part or "")
                for part in (
                    item.title,
                    getattr(item, "summary", ""),
                    item.content_body,
                    " ".join(getattr(item, "tags", []) or []),
                )
            ).casefold()
            if cleaned_query not in haystack:
                continue
        matches.append(item)

    start = (page - 1) * per_page
    return ContentPage(
        items=matches[start : start + per_page],
        total=len(matches),
        page=page,
        per_page=per_page,
        available_tags=tags,
    )


#: Items shown per page on the article and tip listings.
ITEMS_PER_PAGE = 10


def _browse_params(
    q: str | None, tag: str | None, page: int
) -> dict[str, object]:
    """Normalise the shared listing query parameters."""

    return {
        "query": (q or "").strip() or None,
        "tag": (tag or "").strip() or None,
        "page": max(1, page),
        "per_page": ITEMS_PER_PAGE,
    }


def build_repository() -> ContentRepository:
    """Construct the configured content repository.

    ``memory`` is a supported choice rather than an unrecognised value that happens to
    land on the fallback path.
    """

    storage = (os.getenv("LIVEON_STORAGE") or "sqlite").strip().lower()

    if storage in {"memory", "in-memory", "inmemory"}:
        logger.info("Using the in-memory content repository", extra={"event": "storage.memory"})
        return _InMemoryContentRepository()

    if storage != "sqlite":
        logger.warning(
            "Unsupported storage type %r; falling back to in-memory.",
            storage,
            extra={"event": "storage.unsupported"},
        )
        return _InMemoryContentRepository()

    try:
        return LocalSQLiteContentRepository(db_path=os.getenv("LIVEON_DB_PATH"))
    except Exception:  # noqa: BLE001 - the site still serves seed content
        logger.exception(
            "SQLite repository init failed; falling back to in-memory.",
            extra={"event": "storage.sqlite_failed"},
        )
        return _InMemoryContentRepository()


def get_repository() -> ContentRepository:
    """FastAPI dependency returning the process-wide content repository.

    This used to build a repository per request, which meant a ``mkdir``, a fresh
    connection, two PRAGMAs and six ``CREATE TABLE IF NOT EXISTS`` statements on every
    page view — around 100× the cost of reusing one. The instance is created lazily so
    importing the module never touches the filesystem.
    """

    repository = getattr(app.state, "content_repository", None)
    if repository is None:
        repository = build_repository()
        app.state.content_repository = repository
    return repository



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


def _article_payload(article: Article) -> dict[str, object]:
    return {
        "id": article.id,
        "title": article.title,
        "summary": article.summary,
        "content_body": article.content_body,
        "published_date": article.published_date.isoformat(),
        "source_urls": list(article.source_urls),
        "tags": list(article.tags),
    }


@app.get("/api/articles", response_class=JSONResponse)
async def fetch_articles(
    q: str | None = None,
    tag: str | None = None,
    page: int = 1,
    repository: ContentRepository = Depends(get_repository),
) -> JSONResponse:
    """Return a page of articles as JSON.

    Mirrors the browsing available on the HTML listing; the public API previously
    exposed tips but not articles.
    """

    results = repository.browse_articles(**_browse_params(q, tag, page))
    return JSONResponse(
        {
            "items": [_article_payload(article) for article in results.items],
            "total": results.total,
            "page": results.page,
            "per_page": results.per_page,
            "total_pages": results.total_pages,
        }
    )


@app.get("/api/articles/{article_id}", response_class=JSONResponse)
async def fetch_article(
    article_id: str,
    repository: ContentRepository = Depends(get_repository),
) -> JSONResponse:
    """Return a single article as JSON."""

    article = repository.get_article(article_id)
    if article is None:
        return JSONResponse({"detail": "Article not found"}, status_code=404)
    return JSONResponse(_article_payload(article))


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
    _rate_limit: None = Depends(enforce_coach_rate_limit),
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
            "history_turns": len(payload.history),
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
            agent.ask, payload.to_coach_question(), timeout=resolve_llm_timeout()
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


def _sse(event: str, data: dict[str, object]) -> str:
    """Format one server-sent event."""

    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _coach_error_event(exc: Exception) -> str:
    """Map a coach failure onto an SSE ``error`` event.

    The response status is already committed by the time generation fails, so the
    equivalent of the JSON endpoint's status code travels in the payload instead.
    """

    if isinstance(exc, (CoachTimeoutError, TimeoutError)):
        message = (
            "The coach took too long to respond. The local model may still be loading — "
            "try again in a moment."
        )
        event, status_code = "coach.timeout", 504
    elif isinstance(exc, CoachUnavailableError):
        message = "The coach is offline. Check that the local model server is running."
        event, status_code = "coach.unavailable", 503
    elif isinstance(exc, CoachError):
        message = "The coach language model could not complete the request."
        event, status_code = "coach.llm_error", 503
    else:
        message = "The coach could not answer that question."
        event, status_code = "coach.error", 500

    detail = _error_detail(message, exc, event=event)
    detail["status"] = status_code
    return _sse("error", detail)


@app.post("/api/ask/stream")
async def ask_coach_stream_endpoint(
    payload: AskCoachRequest,
    agent: CoachAgent = Depends(get_coach_agent),
    _rate_limit: None = Depends(enforce_coach_rate_limit),
) -> StreamingResponse:
    """Stream the coach's answer as server-sent events.

    A local model needs tens of seconds to finish; streaming turns that wait into
    visible progress. The blocking generator runs in a worker thread and hands
    fragments to the event loop through a queue, so — as with ``/api/ask`` — the rest
    of the site keeps serving while an answer is produced.
    """

    question = payload.to_coach_question()
    logger.info(
        "Coach stream requested",
        extra={
            "event": "coach.stream_request",
            "question_length": len(question.stripped()),
            "history_turns": len(payload.history),
        },
    )

    async def event_stream() -> "AsyncIterator[str]":
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()

        def produce() -> None:
            try:
                for fragment in agent.stream(question):
                    loop.call_soon_threadsafe(queue.put_nowait, ("chunk", fragment))
            except Exception as exc:  # noqa: BLE001 - reported as an SSE error event
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
            else:
                loop.call_soon_threadsafe(queue.put_nowait, ("end", None))

        worker: "asyncio.Task[object]" = asyncio.ensure_future(asyncio.to_thread(produce))
        deadline = loop.time() + resolve_llm_timeout()
        collected: list[str] = []

        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    yield _coach_error_event(TimeoutError("Coach stream exceeded its deadline."))
                    return
                try:
                    kind, value = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    yield _coach_error_event(TimeoutError("Coach stream exceeded its deadline."))
                    return

                if kind == "chunk":
                    text = str(value)
                    collected.append(text)
                    yield _sse("chunk", {"text": text})
                elif kind == "error":
                    yield _coach_error_event(value)  # type: ignore[arg-type]
                    return
                else:
                    break

            # The disclaimer can only be split off once the full text has arrived, so
            # the client swaps in the cleaned answer when this final event lands.
            message, disclaimer = separate_disclaimer(
                "".join(collected), default=agent.default_disclaimer
            )
            yield _sse(
                "done",
                {
                    "answer": message,
                    "disclaimer": disclaimer,
                    "answer_html": str(markdown_to_html(message)),
                    "disclaimer_html": str(markdown_to_html(disclaimer)),
                },
            )
        finally:
            # The worker cannot be interrupted; abandon it rather than block shutdown.
            if not worker.done():
                worker.cancel()
                worker.add_done_callback(_consume_abandoned_task)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/articles", response_class=HTMLResponse)
async def list_articles(
    request: Request,
    q: str | None = None,
    tag: str | None = None,
    page: int = 1,
    repository: ContentRepository = Depends(get_repository),
) -> HTMLResponse:
    """Render the article listing, filtered and paginated."""

    params = _browse_params(q, tag, page)
    results = repository.browse_articles(**params)
    return templates.TemplateResponse(
        request,
        "articles/list.html",
        {
            "title": "Longevity Articles",
            "articles": results.items,
            "results": results,
            "query": params["query"] or "",
            "active_tag": params["tag"],
            "base_path": f"{request.scope.get('root_path', '')}/articles",
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
    q: str | None = None,
    tag: str | None = None,
    page: int = 1,
    repository: ContentRepository = Depends(get_repository),
) -> HTMLResponse:
    """Render the tip listing, filtered and paginated."""

    params = _browse_params(q, tag, page)
    results = repository.browse_tips(**params)

    # The featured slot only makes sense on an unfiltered first page; once the reader
    # is searching, every match should be presented the same way.
    is_default_view = (
        params["page"] == 1 and not params["query"] and not params["tag"]
    )
    featured_tip = results.items[0] if (is_default_view and results.items) else None
    recent_tips = results.items[1:] if featured_tip else results.items

    return templates.TemplateResponse(
        request,
        "tips/list.html",
        {
            "title": "Longevity Tips",
            "featured_tip": featured_tip,
            "recent_tips": recent_tips,
            "results": results,
            "query": params["query"] or "",
            "active_tag": params["tag"],
            "base_path": f"{request.scope.get('root_path', '')}/tips",
        },
    )


@app.get("/coach", response_class=HTMLResponse)
async def ask_the_coach(request: Request) -> HTMLResponse:
    """Render the conversational coach page."""

    return templates.TemplateResponse(
        request,
        "coach.html",
        {
            "title": "Ask the Coach",
            "coach_prompts": list(_coach_prompt_suggestions()),
            # Give the server's own deadline a chance to answer first, so the user
            # sees the explanatory 504 rather than a bare client-side abort.
            "coach_timeout_ms": int(resolve_llm_timeout() * 1000) + 15_000,
            "max_question_chars": MAX_QUESTION_CHARS,
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
    scheduler = _get_scheduler()

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "title": "Admin Console",
            "articles": articles,
            "tips": tips,
            "pipeline_jobs": scheduler.describe_jobs() if scheduler else [],
            "scheduler_enabled": scheduler is not None,
            "cadences": CADENCES,
        },
    )


def _get_scheduler() -> object | None:
    """Return the running pipeline scheduler, if the app started one."""

    return getattr(app.state, "pipeline_scheduler", None)


@app.post("/admin/pipelines/{job_name}/run")
async def run_pipeline_admin(
    request: Request,
    job_name: str,
    username: str = Depends(require_admin_write),
) -> RedirectResponse:
    """Kick off a content pipeline immediately.

    Waiting for a run that takes minutes would hold the request open, so the job is
    started in the background and the console reports progress through its run times.
    """

    scheduler = _get_scheduler()
    if scheduler is None:
        raise HTTPException(
            status_code=503,
            detail="The pipeline scheduler is not running in this process.",
        )

    if not await scheduler.trigger(job_name):
        raise HTTPException(status_code=404, detail=f"Unknown pipeline: {job_name}")

    logger.info(
        "Admin triggered pipeline",
        extra={"event": "admin.pipeline_triggered", "job": job_name, "actor": username},
    )
    return RedirectResponse(
        request.url_for("admin_dashboard"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


#: Guard on the form body, which is a handful of bytes in normal use.
_MAX_FORM_BYTES = 4096


async def _read_form_field(request: Request, name: str) -> str:
    """Return one field from a URL-encoded form body.

    Parsed with the standard library rather than Starlette's ``request.form()``,
    which requires ``python-multipart`` even for URL-encoded bodies. One `<select>`
    does not justify an extra runtime dependency.
    """

    body = await request.body()
    if len(body) > _MAX_FORM_BYTES:
        raise HTTPException(status_code=413, detail="Form submission too large.")

    fields = dict(parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True))
    return (fields.get(name) or "").strip()


@app.post("/admin/pipelines/{job_name}/interval")
async def set_pipeline_interval_admin(
    request: Request,
    job_name: str,
    username: str = Depends(require_admin_write),
) -> RedirectResponse:
    """Change how often a content pipeline runs."""

    scheduler = _get_scheduler()
    if scheduler is None:
        raise HTTPException(
            status_code=503,
            detail="The pipeline scheduler is not running in this process.",
        )

    cadence_key = resolve_cadence_key(await _read_form_field(request, "cadence"))
    if cadence_key is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Choose one of: "
                + ", ".join(cadence.key for cadence in CADENCES)
            ),
        )

    if not scheduler.set_cadence(job_name, cadence_key):
        raise HTTPException(status_code=404, detail=f"Unknown pipeline: {job_name}")

    logger.info(
        "Admin changed pipeline cadence",
        extra={
            "event": "admin.cadence_changed",
            "job": job_name,
            "cadence": cadence_key,
            "actor": username,
        },
    )
    return RedirectResponse(
        request.url_for("admin_dashboard"),
        status_code=status.HTTP_303_SEE_OTHER,
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
