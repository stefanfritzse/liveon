"""Tests for the GCP Cloud Monitoring integration."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import DefaultCredentialsError

from app.services.monitoring import GCPMetricsService


def _articles_payload(result: dict[str, Any]) -> dict[str, Any]:
    return result["pipelines"]["articles"]


def _tips_payload(result: dict[str, Any]) -> dict[str, Any]:
    return result["pipelines"]["tips"]


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
    articles = _articles_payload(result)
    tips = _tips_payload(result)

    assert result["status"] == "warning"
    assert result["project_id"] == GCPMetricsService._FALLBACK_PROJECT_ID
    assert result["backend"] == "cloud_scheduler"
    assert not result.get("using_sample_data", False)
    assert articles["job_id"] == GCPMetricsService.DEFAULT_ARTICLE_JOB_ID
    assert tips["job_id"] == GCPMetricsService.DEFAULT_TIP_JOB_ID
    assert any("Environment variables" in line for line in articles["logs"])
    assert any("Using built-in development project ID" in line for line in articles["logs"])
    assert result["logs"][0].startswith("=== Content pipeline")
    assert any("=== Tip pipeline" in line for line in result["logs"])


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
    articles = _articles_payload(result)
    tips = _tips_payload(result)

    assert result["status"] == "warning"
    assert result["using_sample_data"] is True
    assert articles["using_sample_data"] is True
    assert tips["using_sample_data"] is True
    assert any("Unable to initialise" in line for line in articles["logs"])
    assert any("Using sample telemetry" in line for line in articles["logs"])
    assert any("run_tip_pipeline" in line for line in tips["logs"])


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
    articles = _articles_payload(result)
    tips = _tips_payload(result)

    assert result["status"] == "warning"
    assert result["using_sample_data"] is True
    assert articles["using_sample_data"] is True
    assert tips["using_sample_data"] is True
    assert any("Permission denied" in line for line in articles["logs"])
    assert any("Using sample telemetry" in line for line in articles["logs"])


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
    articles = _articles_payload(result)
    tips = _tips_payload(result)

    assert result["status"] == "warning"
    assert any("No datapoints" in line for line in articles["logs"])
    assert any("No datapoints" in line for line in tips["logs"])


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
    articles = _articles_payload(result)
    tips = _tips_payload(result)

    assert result["project_id"] == "tfvars-project"
    assert result["status"] == "warning"
    assert not result.get("using_sample_data", False)
    assert articles["project_id"] == "tfvars-project"
    assert tips["project_id"] == "tfvars-project"


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

    articles = _articles_payload(result)
    logs = articles["logs"]

    assert any("Terraform lookup path (configured):" in line for line in logs)
    assert any(
        f"Terraform lookup path (absolute): {missing_tfvars.resolve()}" in line
        for line in logs
    )
    assert any("Terraform lookup parent directory exists:" in line for line in logs)
    assert any("Current working directory:" in line for line in logs)
    assert any("Monitoring service module path:" in line for line in logs)
    assert any("Using built-in development project ID" in line for line in logs)
    assert "pipelines" in result


def test_k8s_backend_requires_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Kubernetes backend returns sample data when required env vars are missing."""

    monkeypatch.setenv("PIPELINE_TRIGGER_BACKEND", "k8s_cronjob")
    monkeypatch.delenv("K8S_NAMESPACE", raising=False)
    monkeypatch.delenv("K8S_CRONJOB_NAME", raising=False)

    service = GCPMetricsService(project_id="any")
    result = service.fetch_run_pipeline_health()
    articles = _articles_payload(result)
    tips = _tips_payload(result)

    assert result["backend"] == "k8s_cronjob"
    assert result["status"] == "warning"
    assert result["using_sample_data"] is True
    assert articles["using_sample_data"] is True
    assert tips["using_sample_data"] is True
    assert any(
        "Missing required Kubernetes configuration" in line for line in articles["logs"]
    )
    assert any(
        "Missing required Kubernetes configuration" in line for line in tips["logs"]
    )
    assert tips["cronjob"] == GCPMetricsService.DEFAULT_TIP_JOB_ID


def test_k8s_backend_collects_cronjob_details(monkeypatch: pytest.MonkeyPatch) -> None:
    """CronJob health is summarised when the Kubernetes API responds successfully."""

    monkeypatch.setenv("PIPELINE_TRIGGER_BACKEND", "k8s_cronjob")
    monkeypatch.setenv("K8S_NAMESPACE", "default")
    monkeypatch.setenv("K8S_CRONJOB_NAME", "longevity")
    monkeypatch.setenv("K8S_TIP_CRONJOB_NAME", "longevity-tips")

    class _ApiException(Exception):
        pass

    class _ConfigException(Exception):
        pass

    class _ConfigModule:
        def load_incluster_config(self) -> None:
            raise _ConfigException("not in cluster")

        def load_kube_config(self) -> None:
            return None

    class _JobMetadata:
        def __init__(self, name: str, owner: str) -> None:
            owner_ref = type("Owner", (), {"kind": "CronJob", "name": owner})
            self.name = name
            self.owner_references = [owner_ref]
            self.creation_timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc)

    class _JobStatus:
        def __init__(self, *, succeeded: int = 1) -> None:
            self.start_time = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
            self.completion_time = datetime(2024, 1, 1, 0, 30, tzinfo=timezone.utc)
            self.succeeded = succeeded
            self.failed = 0
            self.active = 0

    class _CronJob:
        def __init__(self, name: str, *, active: int) -> None:
            self.metadata = type("CronMeta", (), {"name": name})
            self.status = type(
                "CronStatus",
                (),
                {
                    "last_schedule_time": datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
                    "last_successful_time": datetime(2024, 1, 1, 0, 30, tzinfo=timezone.utc),
                    "active": [object() for _ in range(active)],
                },
            )()

    class _JobsResponse:
        def __init__(self, owner: str) -> None:
            if owner == "longevity-tips":
                self.items: list[Any] = []
            else:
                job = type(
                    "Job",
                    (),
                    {
                        "metadata": _JobMetadata(f"{owner}-123", owner),
                        "status": _JobStatus(),
                    },
                )
                self.items = [job]

    class _BatchClient:
        def __init__(self) -> None:
            self._current_owner = "longevity"

        def read_namespaced_cron_job(self, name: str, namespace: str) -> _CronJob:
            assert namespace == "default"
            self._current_owner = name
            active = 1 if name == "longevity" else 0
            return _CronJob(name, active=active)

        def list_namespaced_job(self, namespace: str) -> _JobsResponse:
            assert namespace == "default"
            return _JobsResponse(owner=self._current_owner)

    class _ClientModule:
        BatchV1Api = _BatchClient

    def _load_stub_client():  # noqa: D401 - test helper
        return _ClientModule, _ConfigModule(), _ApiException, _ConfigException

    monkeypatch.setattr(
        GCPMetricsService,
        "_load_kubernetes_client",
        staticmethod(lambda: _load_stub_client()),
    )

    service = GCPMetricsService(project_id="any")
    result = service.fetch_run_pipeline_health()
    articles = _articles_payload(result)
    tips = _tips_payload(result)

    assert result["backend"] == "k8s_cronjob"
    assert result["status"] == "success"
    assert result["using_sample_data"] is False
    assert result["active_runs"] == 1
    assert articles["recent_jobs"]
    assert tips["recent_jobs"] == []
    assert articles["cronjob"] == "longevity"
    assert tips["cronjob"] == "longevity-tips"
    assert any("CronJob 'longevity'" in line for line in articles["logs"])
    assert any("CronJob 'longevity-tips'" in line for line in tips["logs"])
