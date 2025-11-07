from __future__ import annotations

import pytest

from app.scripts.run_pipeline import LocalJSONResponder, _create_llm


def _clear_env(monkeypatch: pytest.MonkeyPatch, *keys: str) -> None:
    for key in keys:
        monkeypatch.delenv(key, raising=False)


def test_summarizer_local_stub_allowed_with_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEON_SUMMARIZER_MODEL", "local")

    llm = _create_llm("summarizer")

    assert isinstance(llm, LocalJSONResponder)
