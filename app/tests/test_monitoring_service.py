"""Tests for the GCP Cloud Monitoring integration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import DefaultCredentialsError

from app.services.monitoring import GCPMetricsService


def test_fetch_run_pipeline_health_without_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service falls back to a built-in project ID during local development."""

    class _EmptyClient:
        def list_time_series(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr(
        "app.services.monitoring.monitoring_v3.MetricServiceClient",
        lambda: _EmptyClient(),
    )

    service = GCPMetricsService(project_id=None, tfvars_path=None)
    result = service.fetch_run_pipeline_health()

    assert result["status"] == "warning"
    assert result["project_id"] == GCPMetricsService._FALLBACK_PROJECT_ID
    assert not result.get("using_sample_data", False)
    assert any("Environment variables" in line for line in result["logs"])
    assert any("Using built-in development project ID" in line for line in result["logs"])


def test_fetch_run_pipeline_health_handles_client_initialisation_error(
    monkeypatch: pytest.MonkeyPatch, gcp_project_id: str
) -> None:
    """Credential errors from the monitoring client are converted into log output."""

    class _BrokenClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401 - test helper
            raise DefaultCredentialsError("ADC missing")

    monkeypatch.setenv("GCP_PROJECT", gcp_project_id)
    monkeypatch.setattr(
        "app.services.monitoring.monitoring_v3.MetricServiceClient",
        _BrokenClient,
    )

    service = GCPMetricsService(project_id=gcp_project_id)
    result = service.fetch_run_pipeline_health()

    assert result["status"] == "warning"
    assert result["using_sample_data"] is True
    assert any("Unable to initialise" in line for line in result["logs"])
    assert any("Using sample telemetry" in line for line in result["logs"])


def test_fetch_run_pipeline_health_falls_back_to_sample_on_api_error(
    monkeypatch: pytest.MonkeyPatch, gcp_project_id: str
) -> None:
    """Permission errors should not surface as HTTP 503 responses."""

    class _FlakyClient:
        def list_time_series(self, *args: Any, **kwargs: Any) -> list[Any]:  # noqa: D401 - test helper
            raise GoogleAPIError("403 Permission denied")

    monkeypatch.setenv("GCP_PROJECT", gcp_project_id)
    monkeypatch.setattr(
        "app.services.monitoring.monitoring_v3.MetricServiceClient",
        lambda: _FlakyClient(),
    )

    service = GCPMetricsService(project_id=gcp_project_id)
    result = service.fetch_run_pipeline_health()

    assert result["status"] == "warning"
    assert result["using_sample_data"] is True
    assert any("Permission denied" in line for line in result["logs"])
    assert any("Using sample telemetry" in line for line in result["logs"])


def test_fetch_run_pipeline_health_returns_warning_when_no_data(
    monkeypatch: pytest.MonkeyPatch, gcp_project_id: str
) -> None:
    """When the API returns no time series the response is marked as a warning."""

    class _EmptyClient:
        def list_time_series(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

    monkeypatch.setenv("GCP_PROJECT", gcp_project_id)
    monkeypatch.setattr(
        "app.services.monitoring.monitoring_v3.MetricServiceClient",
        lambda: _EmptyClient(),
    )

    service = GCPMetricsService(project_id=gcp_project_id)
    result = service.fetch_run_pipeline_health()

    assert result["status"] == "warning"
    assert any("No datapoints" in line for line in result["logs"])


def test_fetch_run_pipeline_health_resolves_project_id_from_tfvars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The service falls back to Terraform variables when env vars are missing."""

    tfvars_path = tmp_path / "terraform.tfvars"
    tfvars_path.write_text('project_id = "tfvars-project"\n')

    class _EmptyClient:
        def list_time_series(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr(
        "app.services.monitoring.monitoring_v3.MetricServiceClient",
        lambda: _EmptyClient(),
    )

    service = GCPMetricsService(project_id=None, tfvars_path=tfvars_path)
    result = service.fetch_run_pipeline_health()

    assert result["project_id"] == "tfvars-project"
    assert result["status"] == "warning"
    assert not result.get("using_sample_data", False)


def test_missing_tfvars_includes_contextual_diagnostics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Diagnostics should include context when the Terraform file is absent."""

    class _EmptyClient:
        def list_time_series(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr(
        "app.services.monitoring.monitoring_v3.MetricServiceClient",
        lambda: _EmptyClient(),
    )

    missing_tfvars = tmp_path / "env" / "terraform.tfvars"

    service = GCPMetricsService(project_id=None, tfvars_path=missing_tfvars)
    result = service.fetch_run_pipeline_health()

    logs = result["logs"]

    assert any("Terraform lookup path (configured):" in line for line in logs)
    assert any(
        f"Terraform lookup path (absolute): {missing_tfvars.resolve()}" in line
        for line in logs
    )
    assert any("Terraform lookup parent directory exists:" in line for line in logs)
    assert any("Current working directory:" in line for line in logs)
    assert any("Monitoring service module path:" in line for line in logs)
    assert any("Using built-in development project ID" in line for line in logs)
