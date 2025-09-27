"""Utilities for querying Google Cloud Monitoring metrics used by the web UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Any, Iterable, Sequence

from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import monitoring_v3
from google.cloud.monitoring_v3 import ListTimeSeriesRequest, TimeInterval
from google.protobuf.timestamp_pb2 import Timestamp

from app.services.tfvars_loader import extract_project_id


@dataclass(slots=True, frozen=True)
class _MetricQuery:
    """Configuration describing a Cloud Monitoring metric to fetch."""

    metric_type: str
    title: str
    description: str


@dataclass(slots=True, frozen=True)
class _ProjectIdResolution:
    """Container for project ID resolution results and supporting diagnostics."""

    value: str | None
    diagnostics: list[str]


class GCPMetricsService:
    """Fetch health signals for the ``run_pipeline`` Cloud Scheduler job."""

    _DEFAULT_TFVARS_PATH = Path(__file__).resolve().parents[2] / "infra/environments/prod/terraform.tfvars"
    _FALLBACK_PROJECT_ID = "live-on-473112"

    _METRIC_QUERIES: Sequence[_MetricQuery] = (
        _MetricQuery(
            metric_type="cloudscheduler.googleapis.com/job/execution_count",
            title="Successful executions",
            description="Number of successful job runs in the lookback window.",
        ),
        _MetricQuery(
            metric_type="cloudscheduler.googleapis.com/job/attempt_count",
            title="Execution attempts",
            description="Total attempts, including retries triggered by failures.",
        ),
        _MetricQuery(
            metric_type="cloudscheduler.googleapis.com/job/response_count",
            title="HTTP response codes",
            description="Breakdown of responses returned by the target endpoint.",
        ),
    )

    def __init__(
        self,
        *,
        project_id: str | None = None,
        tfvars_path: Path | None = _DEFAULT_TFVARS_PATH,
    ) -> None:
        resolution = self._resolve_project_id(project_id=project_id, tfvars_path=tfvars_path)
        self._project_id = resolution.value
        self._project_resolution_logs = resolution.diagnostics

    def fetch_run_pipeline_health(self, *, job_id: str = "run_pipeline") -> dict[str, Any]:
        """Return human-readable log lines summarising the job health metrics."""

        retrieved_at = datetime.now(timezone.utc)
        log_lines: list[str] = [
            f"[{retrieved_at.isoformat()}] Cloud Scheduler health probe for job '{job_id}'",
        ]

        if self._project_resolution_logs and self._project_id:
            log_lines.extend(self._project_resolution_logs)

        if not self._project_id:
            log_lines.extend(self._project_resolution_logs)
            log_lines.append(
                "Project ID is not configured. Set the GCP_PROJECT or GOOGLE_CLOUD_PROJECT environment variable."
            )
            log_lines.append(
                "Using sample telemetry so the dashboard remains interactive while local credentials are missing."
            )
            log_lines.extend(self._local_debug_hints(job_id))
            return {
                "status": "warning",
                "project_id": None,
                "using_sample_data": True,
                "logs": log_lines,
                "retrieved_at": retrieved_at.isoformat(),
            }

        try:
            client = monitoring_v3.MetricServiceClient()
        except (DefaultCredentialsError, GoogleAPIError) as exc:
            log_lines.append(f"Unable to initialise Cloud Monitoring client: {exc}")
            log_lines.extend(self._local_debug_hints(job_id))
            return {
                "status": "error",
                "project_id": self._project_id,
                "logs": log_lines,
                "retrieved_at": retrieved_at.isoformat(),
            }

        start_time = retrieved_at - timedelta(hours=24)
        interval = TimeInterval(
            start_time=self._to_timestamp(start_time),
            end_time=self._to_timestamp(retrieved_at),
        )

        dataset_found = False
        for query in self._METRIC_QUERIES:
            filter_expression = (
                f'metric.type="{query.metric_type}" '
                f'AND resource.type="cloud_scheduler_job" '
                f'AND resource.labels.job_id="{job_id}"'
            )
            request = ListTimeSeriesRequest(
                name=f"projects/{self._project_id}",
                filter=filter_expression,
                interval=interval,
                view=ListTimeSeriesRequest.TimeSeriesView.FULL,
            )

            try:
                time_series = list(client.list_time_series(request=request))
            except (DefaultCredentialsError, GoogleAPIError) as exc:
                log_lines.append(
                    f"Failed to query metric '{query.metric_type}' from project {self._project_id}: {exc}"
                )
                log_lines.extend(self._local_debug_hints(job_id))
                return {
                    "status": "error",
                    "project_id": self._project_id,
                    "logs": log_lines,
                    "retrieved_at": retrieved_at.isoformat(),
                }

            if not time_series:
                log_lines.append(f"{query.title}: No datapoints found in the past 24 hours.")
                continue

            dataset_found = True
            log_lines.append(f"{query.title} ({query.description})")
            log_lines.extend(self._summarise_time_series(time_series))

        if not dataset_found:
            log_lines.append(
                "Cloud Monitoring did not return any datapoints. Confirm the job name and project ID, "
                "and ensure the job has executed within the last 24 hours."
            )

        return {
            "status": "success" if dataset_found else "warning",
            "project_id": self._project_id,
            "logs": log_lines,
            "retrieved_at": retrieved_at.isoformat(),
        }

    @classmethod
    def _resolve_project_id(
        cls, *, project_id: str | None, tfvars_path: Path | None
    ) -> _ProjectIdResolution:
        if project_id:
            return _ProjectIdResolution(
                project_id,
                ["Project ID provided explicitly when initialising GCPMetricsService."],
            )

        diagnostics: list[str] = []

        env_var_sources = ("GCP_PROJECT", "GOOGLE_CLOUD_PROJECT")
        for env_var in env_var_sources:
            env_value = os.getenv(env_var)
            if env_value:
                return _ProjectIdResolution(
                    env_value,
                    [f"Project ID resolved from environment variable '{env_var}'."],
                )

        diagnostics.append(
            "Environment variables 'GCP_PROJECT' and 'GOOGLE_CLOUD_PROJECT' are not set."
        )

        if tfvars_path is None:
            diagnostics.append("Terraform variable lookup was disabled via configuration.")
            return cls._fallback_resolution(diagnostics)

        resolved_tfvars_path = tfvars_path
        try:
            resolved_tfvars_path = tfvars_path.resolve()
        except OSError:
            # ``Path.resolve`` may fail on some platforms for relative paths. Fall back to
            # the configured value if resolution is unavailable.
            resolved_tfvars_path = tfvars_path

        terraform_context = [
            f"Terraform lookup path (configured): {tfvars_path}",
            f"Terraform lookup path (absolute): {resolved_tfvars_path}",
            (
                "Terraform lookup parent directory exists: "
                f"{resolved_tfvars_path.parent.exists()} ({resolved_tfvars_path.parent})"
            ),
            f"Current working directory: {Path.cwd()}",
            f"Monitoring service module path: {Path(__file__).resolve()}",
        ]

        try:
            if not tfvars_path.exists():
                diagnostics.extend(terraform_context)
                diagnostics.append(
                    f"Terraform variables file not found at {tfvars_path}."
                )
                return cls._fallback_resolution(diagnostics)

            project_id_from_tfvars = extract_project_id(tfvars_path)
        except OSError as exc:
            diagnostics.extend(terraform_context)
            diagnostics.append(
                f"Terraform variables file at {tfvars_path} could not be read: {exc}."
            )
            return cls._fallback_resolution(diagnostics)
        except ValueError as exc:
            diagnostics.extend(terraform_context)
            diagnostics.append(
                f"Failed to parse Terraform variables from {tfvars_path}: {exc}."
            )
            return cls._fallback_resolution(diagnostics)

        if project_id_from_tfvars:
            return _ProjectIdResolution(
                project_id_from_tfvars,
                [
                    "Project ID resolved from Terraform variables file.",
                    f"Terraform source: {tfvars_path}",
                ],
            )

        diagnostics.extend(terraform_context)
        diagnostics.append(
            f"Terraform variables file located at {tfvars_path} but no 'project_id' entry was found."
        )
        return cls._fallback_resolution(diagnostics)

    @classmethod
    def _fallback_resolution(cls, diagnostics: list[str]) -> _ProjectIdResolution:
        diagnostics.append(
            "Using built-in development project ID '"
            f"{cls._FALLBACK_PROJECT_ID}'; adjust configuration before production deployment."
        )
        diagnostics.append(
            "Hard-coded project configuration enables local monitoring without Terraform state."
        )
        return _ProjectIdResolution(cls._FALLBACK_PROJECT_ID, diagnostics)

    @staticmethod
    def _summarise_time_series(time_series: Iterable[monitoring_v3.TimeSeries]) -> list[str]:
        summaries: list[str] = []
        for series in time_series:
            metric_labels = ", ".join(
                f"{key}={value}" for key, value in sorted(series.metric.labels.items()) if value
            )
            resource_labels = ", ".join(
                f"{key}={value}" for key, value in sorted(series.resource.labels.items()) if value
            )
            header_parts = [part for part in (metric_labels, resource_labels) if part]
            if header_parts:
                summaries.append(f"  • Labels: {' | '.join(header_parts)}")

            for point in series.points:
                value = GCPMetricsService._extract_point_value(point)
                point_time = point.interval.end_time.ToDatetime().astimezone(timezone.utc).isoformat()
                summaries.append(f"    - {point_time}: {value}")

        if not summaries:
            summaries.append("  • Datapoints were returned but contained no readable values.")
        return summaries

    @staticmethod
    def _extract_point_value(point: monitoring_v3.Point) -> str:
        value = point.value
        if value is None:
            return "(no value)"
        value_kind = value.WhichOneof("value")
        if value_kind == "string_value":
            return value.string_value
        if value_kind == "double_value":
            return f"{value.double_value:.2f}"
        if value_kind == "int64_value":
            return str(value.int64_value)
        if value_kind == "bool_value":
            return str(value.bool_value)
        if value_kind == "distribution_value":
            return f"distribution(count={value.distribution_value.count})"
        return "(unsupported value type)"

    @staticmethod
    def _to_timestamp(moment: datetime) -> Timestamp:
        timestamp = Timestamp()
        timestamp.FromDatetime(moment)
        return timestamp

    @staticmethod
    def _local_debug_hints(job_id: str) -> list[str]:
        return [
            "Local hint: ensure Application Default Credentials are available via 'gcloud auth application-default login'.",
            f"Local hint: verify that the Cloud Scheduler job '{job_id}' exists in the target project.",
            "Sample log placeholder:",
            f"  - {datetime.now(timezone.utc).isoformat()}: run_pipeline executed (sample)",
            "  - No live data retrieved; using sample output for UI purposes.",
        ]

