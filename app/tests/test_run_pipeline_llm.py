from __future__ import annotations

import pytest

from app.scripts.run_pipeline import LocalJSONResponder, _create_llm


def _clear_env(monkeypatch: pytest.MonkeyPatch, *keys: str) -> None:
    for key in keys:
        monkeypatch.delenv(key, raising=False)


def test_summarizer_local_stub_allowed_with_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(
        monkeypatch,
        "LIVEON_SUMMARIZER_MODEL",
        "LIVEON_ENV",
        "GCP_PROJECT",
        "KUBERNETES_SERVICE_HOST",
    )
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "live-on-473112")
    monkeypatch.setenv("LIVEON_ALLOW_LOCAL_LLM", "true")

    llm = _create_llm("summarizer")

    assert isinstance(llm, LocalJSONResponder)
