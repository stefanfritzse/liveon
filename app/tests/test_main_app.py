"""Tests covering FastAPI routes and template rendering for the public site."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import json
import sys
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

from app.main import ContentRepository, app, get_coach_agent, get_repository
from app.models.coach import CoachAnswer
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
def client() -> Callable[[ContentRepository, object | None], TestClient]:
    """Provide a helper that returns a configured ``TestClient`` for a repository."""

    clients: list[TestClient] = []

    def _factory(repository: ContentRepository, *, agent: object | None = None) -> TestClient:
        app.dependency_overrides[get_repository] = lambda: repository
        app.dependency_overrides.pop(get_coach_agent, None)
        if agent is not None:
            app.dependency_overrides[get_coach_agent] = lambda: agent
        test_client = TestClient(app)
        clients.append(test_client)
        return test_client

    yield _factory

    for created_client in clients:
        created_client.close()
    app.dependency_overrides.pop(get_repository, None)
    app.dependency_overrides.pop(get_coach_agent, None)


def _build_tip(identifier: str, published_offset_hours: int) -> Tip:
    published = datetime.now(timezone.utc) - timedelta(hours=published_offset_hours)
    return Tip(
        id=identifier,
        title=f"Tip {identifier}",
        content_body=f"Content for {identifier}",
        tags=["daily"],
        published_date=published,
    )


class _RecordingCoachAgent:
    def __init__(self, answer: CoachAnswer) -> None:
        self.answer = answer
        self.questions: list[str] = []

    def ask(self, question: str) -> CoachAnswer:
        self.questions.append(question)
        return self.answer


class _FailingCoachAgent:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def ask(self, question: str) -> CoachAnswer:  # type: ignore[override]
        raise self.error


class _SimplePromptTemplate:
    """Minimal prompt template used to build CoachAgent instances in tests."""

    def __init__(self, _: Iterable[tuple[str, ...]]) -> None:
        pass

    @classmethod
    def from_messages(cls, message_specs: Iterable[tuple[str, ...]]) -> "_SimplePromptTemplate":
        return cls(message_specs)

    def invoke(self, mapping: dict[str, Any]) -> str:
        return mapping["question"]


def test_homepage_context_includes_featured_tip(client: Callable[..., TestClient]) -> None:
    tips = [_build_tip("a", 1), _build_tip("b", 2)]
    repository = StubContentRepository(tips=tips)
    test_client = client(repository)

    response = test_client.get("/")

    assert response.status_code == 200
    assert response.template.name == "home.html"
    assert response.context["featured_tip"].id == "a"
    assert [tip.id for tip in response.context["recent_tips"]] == ["b"]


def test_homepage_handles_missing_tip(client: Callable[..., TestClient]) -> None:
    repository = StubContentRepository(tips=[])
    test_client = client(repository)

    response = test_client.get("/")

    assert response.status_code == 200
    assert response.context["featured_tip"] is None
    assert response.context["recent_tips"] == []


def test_latest_tip_endpoint_returns_tip_payload(
    client: Callable[..., TestClient]
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
    client: Callable[..., TestClient]
) -> None:
    repository = StubContentRepository(tips=[])
    test_client = client(repository)

    response = test_client.get("/api/tips/latest")

    assert response.status_code == 404
    assert response.json() == {"detail": "No tips available"}


def test_tips_page_splits_featured_and_recent(client: Callable[..., TestClient]) -> None:
    tips = [_build_tip("latest", 0), _build_tip("older", 4)]
    repository = StubContentRepository(tips=tips)
    test_client = client(repository)

    response = test_client.get("/tips")

    assert response.status_code == 200
    assert response.template.name == "tips/list.html"
    assert response.context["featured_tip"].id == "latest"
    assert [tip.id for tip in response.context["recent_tips"]] == ["older"]


def test_tips_page_empty_state(client: Callable[..., TestClient]) -> None:
    repository = StubContentRepository(tips=[])
    test_client = client(repository)

    response = test_client.get("/tips")

    assert response.status_code == 200
    assert response.context["featured_tip"] is None
    assert response.context["recent_tips"] == []


def test_coach_page_includes_prompt_suggestions(client: Callable[..., TestClient]) -> None:
    repository = StubContentRepository(tips=[])
    test_client = client(repository)

    from app import main as main_module

    main_module._coach_prompt_suggestions.cache_clear()
    try:
        response = test_client.get("/coach")
        assert response.status_code == 200
        suggestions = list(main_module._coach_prompt_suggestions())
        assert suggestions
        assert "Need inspiration?" in response.text
        assert suggestions[0]["label"] in response.text
    finally:
        main_module._coach_prompt_suggestions.cache_clear()


def test_coach_prompt_suggestions_respect_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main as main_module

    payload = [
        {
            "label": "Hydration plan",
            "question": "How much water should I aim for each day?",
            "description": "Balance electrolytes with daily fluid intake.",
        },
        {"question": "Do short walks after meals help glucose stability?"},
    ]
    monkeypatch.setenv("LIVEON_COACH_PROMPTS", json.dumps(payload))
    main_module._coach_prompt_suggestions.cache_clear()

    try:
        prompts = main_module._coach_prompt_suggestions()
        assert prompts[0]["label"] == "Hydration plan"
        assert prompts[0]["description"] == "Balance electrolytes with daily fluid intake."
        assert prompts[1]["question"].startswith("Do short walks")
    finally:
        main_module._coach_prompt_suggestions.cache_clear()


def test_coach_prompt_suggestions_invalid_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main as main_module

    monkeypatch.setenv("LIVEON_COACH_PROMPTS", "{invalid json")
    main_module._coach_prompt_suggestions.cache_clear()

    try:
        prompts = main_module._coach_prompt_suggestions()
        assert prompts == main_module._DEFAULT_COACH_PROMPTS
    finally:
        main_module._coach_prompt_suggestions.cache_clear()


def test_ask_coach_endpoint_returns_structured_response(client: Callable[..., TestClient]) -> None:
    repository = StubContentRepository(tips=[])
    answer = CoachAnswer(message="Here is guidance.", disclaimer="Stay safe.")
    agent = _RecordingCoachAgent(answer)
    test_client = client(repository, agent=agent)

    response = test_client.post("/api/ask", json={"question": "  How do I sleep better?  "})

    assert response.status_code == 200
    payload = response.json()
    assert payload["answer"] == "Here is guidance."
    assert payload["disclaimer"] == "Stay safe."
    assert agent.questions == ["How do I sleep better?"]


def test_ask_coach_endpoint_rejects_blank_questions(client: Callable[..., TestClient]) -> None:
    repository = StubContentRepository(tips=[])
    answer = CoachAnswer(message="", disclaimer="")
    agent = _RecordingCoachAgent(answer)
    test_client = client(repository, agent=agent)

    response = test_client.post("/api/ask", json={"question": "   "})

    assert response.status_code == 422
    detail = response.json()
    assert any("Question must not be empty" in item["msg"] for item in detail["detail"])


def test_ask_coach_endpoint_handles_llm_failures(client: Callable[..., TestClient]) -> None:
    repository = StubContentRepository(tips=[])
    agent = _FailingCoachAgent(RuntimeError("LLM offline"))
    test_client = client(repository, agent=agent)

    response = test_client.post("/api/ask", json={"question": "Share exercise tips"})

    assert response.status_code == 503
    payload = response.json()
    assert payload == {
        "detail": {
            "message": "Coach language model unavailable",
            "debug": {"type": "RuntimeError", "message": "LLM offline"},
        }
    }


def test_ask_coach_endpoint_exposes_agent_initialisation_debug(
    monkeypatch: pytest.MonkeyPatch, client: Callable[..., TestClient]
) -> None:
    from app import main as main_module

    repository = StubContentRepository(tips=[])

    main_module._cached_coach_agent.cache_clear()

    def _raise_runtime() -> None:
        raise RuntimeError("LangChain dependency missing")

    monkeypatch.setattr(main_module, "_cached_coach_agent", _raise_runtime)

    test_client = client(repository)

    response = test_client.post("/api/ask", json={"question": "Share sleep advice"})

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "message": "Coach service temporarily unavailable",
            "debug": {
                "type": "RuntimeError",
                "message": "LangChain dependency missing",
            },
        }
    }



