"""Tests for the GCP Cloud Monitoring integration."""

from __future__ import annotations

from typing import Any

import pytest
from google.auth.exceptions import DefaultCredentialsError

from app.services.monitoring import GCPMetricsService


def test_fetch_run_pipeline_health_without_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service surfaces a helpful error when the project ID is missing."""

    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    service = GCPMetricsService(project_id=None)
    result = service.fetch_run_pipeline_health()

    assert result["status"] == "error"
    assert any("Project ID" in line for line in result["logs"])


def test_fetch_run_pipeline_health_handles_client_initialisation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credential errors from the monitoring client are converted into log output."""

    class _BrokenClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401 - test helper
            raise DefaultCredentialsError("ADC missing")

    monkeypatch.setenv("GCP_PROJECT", "demo-project")
    monkeypatch.setattr(
        "app.services.monitoring.monitoring_v3.MetricServiceClient",
        _BrokenClient,
    )

    service = GCPMetricsService(project_id="demo-project")
    result = service.fetch_run_pipeline_health()

    assert result["status"] == "error"
    assert any("Unable to initialise" in line for line in result["logs"])


def test_fetch_run_pipeline_health_returns_warning_when_no_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the API returns no time series the response is marked as a warning."""

    class _EmptyClient:
        def list_time_series(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

    monkeypatch.setenv("GCP_PROJECT", "demo-project")
    monkeypatch.setattr(
        "app.services.monitoring.monitoring_v3.MetricServiceClient",
        lambda: _EmptyClient(),
    )

    service = GCPMetricsService(project_id="demo-project")
    result = service.fetch_run_pipeline_health()

    assert result["status"] == "warning"
    assert any("No datapoints" in line for line in result["logs"])
