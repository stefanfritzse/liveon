from __future__ import annotations

import pytest
from langchain_community.chat_models import ChatOllama

from app.scripts.run_pipeline import LocalJSONResponder, _create_llm


def _clear_env(monkeypatch: pytest.MonkeyPatch, *keys: str) -> None:
    for key in keys:
        monkeypatch.delenv(key, raising=False)


def test_create_llm_defaults_to_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that the default LLM is ChatOllama when no env var is set."""
    _clear_env(monkeypatch, "LIVEON_SUMMARIZER_MODEL")
    llm = _create_llm("summarizer")
    assert isinstance(llm, ChatOllama)


def test_create_llm_uses_local_json_responder_as_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the local stub is used if the env var is set to a non-Ollama value."""
    monkeypatch.setenv("LIVEON_SUMMARIZER_MODEL", "stub")
    llm = _create_llm("summarizer")
    assert isinstance(llm, LocalJSONResponder)
