"""Tests for the shared chat-model factory and the tip CLI's provider handling.

The article pipeline, the tip pipeline, and the coach each grew their own copy of
this logic and the copies drifted: only the article path applied JSON mode and a low
temperature, and the tip CLI could not select Ollama at all.
"""

from __future__ import annotations

import pytest

from app.scripts import run_tip_pipeline
from app.services import llm_factory
from app.services.llm_factory import (
    SUPPORTED_PROVIDERS,
    build_chat_ollama,
    create_chat_model,
    normalise_provider,
    resolve_model_temperature,
    resolve_ollama_base_url,
)


class _StubChat:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [
        "LIVEON_OLLAMA_URL",
        "OLLAMA_HOST",
        "LIVEON_MODEL_TEMPERATURE",
        "LIVEON_OLLAMA_FORMAT",
        "LIVEON_OLLAMA_MODEL",
        "LIVEON_LLM_PROVIDER",
        "LIVEON_TIP_MODEL",
        "LIVEON_SUMMARIZER_MODEL",
        "LIVEON_ALLOW_LOCAL_LLM",
    ]:
        monkeypatch.delenv(key, raising=False)


# ----------------------------------------------------------------------
# URL and temperature resolution
# ----------------------------------------------------------------------


def test_base_url_defaults_to_localhost() -> None:
    assert resolve_ollama_base_url() == "http://127.0.0.1:11434"


@pytest.mark.parametrize("bind_address", ["http://0.0.0.0:11434", "0.0.0.0:11434"])
def test_bind_address_is_rewritten_to_a_reachable_host(
    monkeypatch: pytest.MonkeyPatch, bind_address: str
) -> None:
    """A daemon *bound* to 0.0.0.0 is *reached* at 127.0.0.1."""

    monkeypatch.setenv("LIVEON_OLLAMA_URL", bind_address)
    assert resolve_ollama_base_url() == "http://127.0.0.1:11434"


def test_base_url_honours_a_remote_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEON_OLLAMA_URL", "http://host.minikube.internal:11434")
    assert resolve_ollama_base_url() == "http://host.minikube.internal:11434"


def test_temperature_defaults_low() -> None:
    """Ollama's own default of 0.8 invents facts and breaks JSON adherence."""

    assert resolve_model_temperature() == 0.2


def test_temperature_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEON_MODEL_TEMPERATURE", "0.7")
    assert resolve_model_temperature() == 0.7


def test_temperature_falls_back_on_junk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEON_MODEL_TEMPERATURE", "warm")
    assert resolve_model_temperature() == 0.2


# ----------------------------------------------------------------------
# Provider normalisation
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("ollama", "ollama"), ("OLLAMA", "ollama"), ("gpt", "openai"), ("openai", "openai"),
     ("local", "local"), ("stub", "local"), (" ollama ", "ollama")],
)
def test_known_providers_normalise(raw: str, expected: str) -> None:
    assert normalise_provider(raw) == expected


def test_unknown_provider_falls_back_to_the_default() -> None:
    assert normalise_provider("vertex", default="ollama") == "ollama"


def test_missing_provider_uses_the_default() -> None:
    assert normalise_provider(None, default="local") == "local"


# ----------------------------------------------------------------------
# ChatOllama construction
# ----------------------------------------------------------------------


def test_json_mode_and_temperature_are_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_factory, "resolve_chat_ollama_class", lambda: _StubChat)

    client = build_chat_ollama(model="qwen2.5:14b", json_mode=True)

    assert client.kwargs["model"] == "qwen2.5:14b"
    assert client.kwargs["format"] == "json"
    assert client.kwargs["temperature"] == 0.2


def test_json_mode_can_be_left_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_factory, "resolve_chat_ollama_class", lambda: _StubChat)

    client = build_chat_ollama(model="qwen2.5:14b", json_mode=False)

    assert "format" not in client.kwargs


def test_unsupported_options_are_dropped_rather_than_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Option support differs between the langchain-ollama and community builds."""

    attempts: list[dict[str, object]] = []

    class _Picky:
        def __init__(self, **kwargs: object) -> None:
            attempts.append(dict(kwargs))
            if "timeout" in kwargs:
                raise TypeError("unexpected keyword argument 'timeout'")

    monkeypatch.setattr(llm_factory, "resolve_chat_ollama_class", lambda: _Picky)

    client = build_chat_ollama(model="m", json_mode=True, timeout=120)

    assert isinstance(client, _Picky)
    assert len(attempts) == 2
    assert "timeout" in attempts[0]
    assert "timeout" not in attempts[1]
    # The options that *are* supported survive the retry.
    assert attempts[1]["format"] == "json"


def test_create_chat_model_builds_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_factory, "resolve_chat_ollama_class", lambda: _StubChat)
    monkeypatch.setenv("LIVEON_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LIVEON_OLLAMA_MODEL", "gemma2:2b")

    client = create_chat_model(agent_label="summarizer")

    assert client.kwargs["model"] == "gemma2:2b"


def test_agent_specific_model_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_factory, "resolve_chat_ollama_class", lambda: _StubChat)
    monkeypatch.setenv("LIVEON_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LIVEON_OLLAMA_MODEL", "shared-model")
    monkeypatch.setenv("LIVEON_TIP_OLLAMA_MODEL", "tip-model")

    assert create_chat_model(agent_label="tip").kwargs["model"] == "tip-model"


def test_local_provider_uses_the_supplied_stub() -> None:
    sentinel = object()

    result = create_chat_model(provider="local", local_factory=lambda: sentinel)

    assert result is sentinel


def test_local_provider_without_a_stub_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="No local stub"):
        create_chat_model(provider="local", agent_label="tip")


# ----------------------------------------------------------------------
# Tip CLI provider handling
# ----------------------------------------------------------------------


def test_tip_cli_offers_every_supported_provider() -> None:
    """Ollama used to be implemented but unreachable from the command line."""

    args = run_tip_pipeline._parse_args(["--model-provider", "ollama"])

    assert args.model_provider == "ollama"
    for provider in SUPPORTED_PROVIDERS:
        assert run_tip_pipeline._parse_args(["--model-provider", provider])


def test_summarizer_env_no_longer_breaks_the_tip_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configuring the article pipeline used to make the tip CLI abort on startup."""

    monkeypatch.setenv("LIVEON_SUMMARIZER_MODEL", "ollama")

    args = run_tip_pipeline._parse_args([])

    assert args.model_provider == "ollama"


def test_tip_provider_follows_the_app_wide_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment that configures Ollama should not silently publish stub tips."""

    monkeypatch.setenv("LIVEON_LLM_PROVIDER", "ollama")

    assert run_tip_pipeline._default_model_provider() == "ollama"


def test_tip_provider_falls_back_to_the_local_stub() -> None:
    assert run_tip_pipeline._default_model_provider() == "local"


def test_local_stub_requires_explicit_opt_in() -> None:
    """--allow-local-llm was accepted and then ignored; now it gates the stub.

    A plain exception rather than SystemExit, so the in-process scheduler can fail one
    job instead of taking the web server down with it.
    """

    with pytest.raises(run_tip_pipeline.PipelineConfigurationError, match="allow-local-llm"):
        run_tip_pipeline._create_tip_llm("local", model_name=None, allow_local_stub=False)


def test_a_misconfigured_tip_provider_is_a_cli_exit_code_not_a_crash() -> None:
    assert run_tip_pipeline.main(["--model-provider", "local"]) == 2


def test_local_stub_is_available_with_the_flag() -> None:
    llm = run_tip_pipeline._create_tip_llm("local", model_name=None, allow_local_stub=True)

    assert isinstance(llm, run_tip_pipeline.TipLocalJSONResponder)


def test_tip_cli_builds_ollama_with_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tip agents demand strict JSON, so the tip path must set JSON mode too."""

    monkeypatch.setattr(llm_factory, "resolve_chat_ollama_class", lambda: _StubChat)

    llm = run_tip_pipeline._create_tip_llm("ollama", model_name="qwen2.5:14b", allow_local_stub=False)

    assert llm.kwargs["format"] == "json"
    assert llm.kwargs["temperature"] == 0.2
    assert llm.kwargs["model"] == "qwen2.5:14b"
