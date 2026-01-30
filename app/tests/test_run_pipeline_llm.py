from __future__ import annotations

import pytest

from app.scripts.run_pipeline import LocalJSONResponder, _create_llm


def _clear_env(monkeypatch: pytest.MonkeyPatch, *keys: str) -> None:
    for key in keys:
        monkeypatch.delenv(key, raising=False)


def test_summarizer_local_stub_rejected_in_managed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(
        monkeypatch,
        "LIVEON_SUMMARIZER_MODEL",
        "LIVEON_ALLOW_LOCAL_LLM",
        "LIVEON_ENV",
        "GCP_PROJECT",
        "KUBERNETES_SERVICE_HOST",
    )
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project-id")

    with pytest.raises(RuntimeError) as excinfo:
        _create_llm("summarizer")

    message = str(excinfo.value)
    assert "local summarizer stubs" in message.lower()
    assert "liveon_summarizer_model" in message.lower()


def test_summarizer_local_stub_allowed_with_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(
        monkeypatch,
        "LIVEON_SUMMARIZER_MODEL",
        "LIVEON_ENV",
        "GCP_PROJECT",
        "KUBERNETES_SERVICE_HOST",
    )
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project-id")
    monkeypatch.setenv("LIVEON_ALLOW_LOCAL_LLM", "true")

    llm = _create_llm("summarizer")

    assert isinstance(llm, LocalJSONResponder)
