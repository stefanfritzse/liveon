"""Utilities for querying Google Cloud Monitoring metrics used by the web UI."""

from __future__ import annotations

import copy
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


@dataclass(slots=True, frozen=True)
class _BackendResolution:
    """Container for pipeline trigger backend resolution results."""

    value: str
    diagnostics: list[str]


class GCPMetricsService:
    """Fetch health signals for the ``run_pipeline`` pipeline trigger."""

    _DEFAULT_TFVARS_PATH = Path(__file__).resolve().parents[2] / "infra/environments/prod/terraform.tfvars"
    _FALLBACK_PROJECT_ID = "live-on-473112"
    _DEFAULT_PIPELINE_BACKEND = "cloud_scheduler"
    _SUPPORTED_PIPELINE_BACKENDS = {"cloud_scheduler", "k8s_cronjob"}
    _PIPELINE_BACKEND_ENV = "PIPELINE_TRIGGER_BACKEND"
    _K8S_NAMESPACE_ENV = "K8S_NAMESPACE"
    _K8S_CRONJOB_NAME_ENV = "K8S_CRONJOB_NAME"
    _K8S_TIP_CRONJOB_NAME_ENV = "K8S_TIP_CRONJOB_NAME"
    DEFAULT_ARTICLE_JOB_ID = "run_pipeline"
    DEFAULT_TIP_JOB_ID = "run_tip_pipeline"
    _STATUS_SEVERITY = {"success": 0, "warning": 1, "error": 2}

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
        backend_resolution = self._resolve_backend()
        self._pipeline_backend = backend_resolution.value
        self._backend_resolution_logs = backend_resolution.diagnostics

    def fetch_run_pipeline_health(
        self,
        *,
        job_id: str = DEFAULT_ARTICLE_JOB_ID,
        tips_job_id: str | None = DEFAULT_TIP_JOB_ID,
    ) -> dict[str, Any]:
        """Return health telemetry for the configured pipeline trigger backend."""

        primary_payload = self._fetch_backend_health(
            job_id=job_id,
            job_label=job_id,
            cronjob_env_var=self._K8S_CRONJOB_NAME_ENV,
        )

        if not tips_job_id:
            return primary_payload

        tip_payload = self._fetch_backend_health(
            job_id=tips_job_id,
            job_label=tips_job_id,
            cronjob_env_var=self._K8S_TIP_CRONJOB_NAME_ENV,
        )

        return self._combine_pipeline_payloads(primary_payload, tip_payload)

    def _fetch_backend_health(
        self,
        *,
        job_id: str,
        job_label: str,
        cronjob_env_var: str,
    ) -> dict[str, Any]:
        if self._pipeline_backend == "k8s_cronjob":
            return self._fetch_k8s_cronjob_health(
                job_id=job_id,
                job_label=job_label,
                cronjob_env_var=cronjob_env_var,
            )
        return self._fetch_cloud_scheduler_health(job_id=job_id, job_label=job_label)

    def _combine_pipeline_payloads(
        self,
        articles_payload: dict[str, Any],
        tips_payload: dict[str, Any],
    ) -> dict[str, Any]:
        combined = copy.deepcopy(articles_payload)
        combined_status = self._worst_status(
            (articles_payload.get("status"), tips_payload.get("status"))
        )
        combined["status"] = combined_status
        combined["using_sample_data"] = bool(articles_payload.get("using_sample_data")) or bool(
            tips_payload.get("using_sample_data")
        )
        combined["logs"] = self._merge_logs(articles_payload, tips_payload)
        combined.setdefault("project_id", tips_payload.get("project_id"))
        combined.setdefault("backend", tips_payload.get("backend"))
        combined.setdefault("retrieved_at", articles_payload.get("retrieved_at"))
        combined["pipelines"] = {
            "articles": self._sanitise_pipeline_payload(articles_payload),
            "tips": self._sanitise_pipeline_payload(tips_payload),
        }
        return combined

    @classmethod
    def _worst_status(cls, statuses: Iterable[str | None]) -> str:
        worst_status = "success"
        worst_score = cls._STATUS_SEVERITY[worst_status]
        for status in statuses:
            candidate = status or "warning"
            score = cls._STATUS_SEVERITY.get(candidate, cls._STATUS_SEVERITY["warning"])
            if score > worst_score:
                worst_status = candidate
                worst_score = score
        return worst_status

    @staticmethod
    def _sanitise_pipeline_payload(payload: dict[str, Any]) -> dict[str, Any]:
        cleaned = copy.deepcopy(payload)
        cleaned.pop("pipelines", None)
        return cleaned

    @staticmethod
    def _merge_logs(
        articles_payload: dict[str, Any], tips_payload: dict[str, Any]
    ) -> list[str]:
        content_header = (
            f"=== Content pipeline ({articles_payload.get('job_id') or 'run_pipeline'}) ==="
        )
        tips_header = f"=== Tip pipeline ({tips_payload.get('job_id') or 'run_tip_pipeline'}) ==="
        logs: list[str] = [content_header]
        logs.extend(articles_payload.get("logs", []))
        logs.append("")
        logs.append(tips_header)
        logs.extend(tips_payload.get("logs", []))
        return [line for line in logs if line is not None]

    def _fetch_cloud_scheduler_health(self, *, job_id: str, job_label: str) -> dict[str, Any]:
        """Collect health signals from Cloud Scheduler metrics."""

        retrieved_at = datetime.now(timezone.utc)
        log_lines: list[str] = [
            (
                f"[{retrieved_at.isoformat()}] Pipeline schedule health probe "
                f"(backend: Cloud Scheduler, job: {job_label})"
            ),
        ]

        if self._backend_resolution_logs:
            log_lines.extend(self._backend_resolution_logs)

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
            return self._sample_payload(
                log_lines=log_lines,
                retrieved_at=retrieved_at,
                project_id=None,
                job_id=job_id,
                backend="cloud_scheduler",
            )

        try:
            client = monitoring_v3.MetricServiceClient()
        except (DefaultCredentialsError, GoogleAPIError) as exc:
            log_lines.append(f"Unable to initialise Cloud Monitoring client: {exc}")
            return self._sample_payload(
                log_lines=log_lines,
                retrieved_at=retrieved_at,
                project_id=self._project_id,
                job_id=job_id,
                backend="cloud_scheduler",
            )

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
                return self._sample_payload(
                    log_lines=log_lines,
                    retrieved_at=retrieved_at,
                    project_id=self._project_id,
                    job_id=job_id,
                    backend="cloud_scheduler",
                )

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
            "backend": "cloud_scheduler",
            "using_sample_data": False,
            "logs": log_lines,
            "retrieved_at": retrieved_at.isoformat(),
            "job_id": job_id,
        }

    def _sample_payload(
        self,
        *,
        log_lines: list[str],
        retrieved_at: datetime,
        project_id: str | None,
        job_id: str,
        backend: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not any("Using sample telemetry" in line for line in log_lines):
            log_lines.append(
                "Using sample telemetry so the dashboard remains interactive while live metrics are unavailable."
            )
        log_lines.extend(self._local_debug_hints(job_id, backend))
        payload: dict[str, Any] = {
            "status": "warning",
            "project_id": project_id,
            "backend": backend,
            "using_sample_data": True,
            "logs": log_lines,
            "retrieved_at": retrieved_at.isoformat(),
            "job_id": job_id,
        }
        if extra:
            payload.update(extra)
        return payload

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

    @classmethod
    def _resolve_backend(cls) -> _BackendResolution:
        raw_value = os.getenv(cls._PIPELINE_BACKEND_ENV)
        diagnostics: list[str] = []

        if not raw_value:
            diagnostics.append(
                "PIPELINE_TRIGGER_BACKEND not set; defaulting to 'cloud_scheduler'."
            )
            return _BackendResolution(cls._DEFAULT_PIPELINE_BACKEND, diagnostics)

        backend = raw_value.strip().lower()
        if backend in cls._SUPPORTED_PIPELINE_BACKENDS:
            diagnostics.append(
                f"Pipeline backend set to '{backend}' via environment variable {cls._PIPELINE_BACKEND_ENV}."
            )
            return _BackendResolution(backend, diagnostics)

        diagnostics.append(
            f"Unrecognised pipeline backend '{raw_value}'. Falling back to '{cls._DEFAULT_PIPELINE_BACKEND}'."
        )
        return _BackendResolution(cls._DEFAULT_PIPELINE_BACKEND, diagnostics)

    def _fetch_k8s_cronjob_health(
        self,
        *,
        job_id: str,
        job_label: str,
        cronjob_env_var: str,
    ) -> dict[str, Any]:
        """Collect health signals from a Kubernetes CronJob."""

        retrieved_at = datetime.now(timezone.utc)
        log_lines: list[str] = [
            (
                f"[{retrieved_at.isoformat()}] Pipeline schedule health probe "
                f"(backend: Kubernetes CronJob, job: {job_label})"
            ),
        ]

        if self._backend_resolution_logs:
            log_lines.extend(self._backend_resolution_logs)

        namespace = os.getenv(self._K8S_NAMESPACE_ENV)
        cronjob_env = cronjob_env_var or self._K8S_CRONJOB_NAME_ENV
        cronjob_name = os.getenv(cronjob_env) or job_id

        missing_env: list[str] = []
        if not namespace:
            missing_env.append(self._K8S_NAMESPACE_ENV)
        if not os.getenv(cronjob_env):
            missing_env.append(cronjob_env)

        if missing_env:
            log_lines.append(
                "Missing required Kubernetes configuration: "
                + ", ".join(sorted(missing_env))
                + "."
            )
            return self._sample_payload(
                log_lines=log_lines,
                retrieved_at=retrieved_at,
                project_id=None,
                job_id=cronjob_name,
                backend="k8s_cronjob",
                extra={
                    "cronjob": cronjob_name,
                    "namespace": namespace,
                    "last_schedule_time": None,
                    "last_successful_time": None,
                    "active_runs": 0,
                    "recent_jobs": [],
                    "job_id": job_id,
                },
            )

        try:
            client_module, config_module, api_exception, config_exception = self._load_kubernetes_client()
        except ImportError as exc:
            log_lines.append(f"Kubernetes client library not available: {exc}.")
            return self._sample_payload(
                log_lines=log_lines,
                retrieved_at=retrieved_at,
                project_id=None,
                job_id=cronjob_name,
                backend="k8s_cronjob",
                extra={
                    "cronjob": cronjob_name,
                    "namespace": namespace,
                    "last_schedule_time": None,
                    "last_successful_time": None,
                    "active_runs": 0,
                    "recent_jobs": [],
                    "job_id": job_id,
                },
            )

        try:
            try:
                config_module.load_incluster_config()
                log_lines.append("Loaded in-cluster Kubernetes configuration.")
            except config_exception:
                config_module.load_kube_config()
                log_lines.append("Loaded local Kubernetes configuration from kubeconfig.")
        except Exception as exc:  # pragma: no cover - defensive
            log_lines.append(f"Unable to load Kubernetes configuration: {exc}")
            return self._sample_payload(
                log_lines=log_lines,
                retrieved_at=retrieved_at,
                project_id=None,
                job_id=cronjob_name,
                backend="k8s_cronjob",
                extra={
                    "cronjob": cronjob_name,
                    "namespace": namespace,
                    "last_schedule_time": None,
                    "last_successful_time": None,
                    "active_runs": 0,
                    "recent_jobs": [],
                    "job_id": job_id,
                },
            )

        batch_api = client_module.BatchV1Api()

        try:
            cronjob = batch_api.read_namespaced_cron_job(name=cronjob_name, namespace=namespace)
        except api_exception as exc:
            log_lines.append(f"Failed to read CronJob '{cronjob_name}' in namespace '{namespace}': {exc}")
            return self._sample_payload(
                log_lines=log_lines,
                retrieved_at=retrieved_at,
                project_id=None,
                job_id=cronjob_name,
                backend="k8s_cronjob",
                extra={
                    "cronjob": cronjob_name,
                    "namespace": namespace,
                    "last_schedule_time": None,
                    "last_successful_time": None,
                    "active_runs": 0,
                    "recent_jobs": [],
                    "job_id": job_id,
                },
            )

        status = getattr(cronjob, "status", None)
        last_schedule_time = self._format_optional_datetime(
            getattr(status, "last_schedule_time", None)
        )
        last_successful_time = self._format_optional_datetime(
            getattr(status, "last_successful_time", None)
        )
        active_refs = getattr(status, "active", None) or []

        log_lines.append(
            f"CronJob '{cronjob_name}' in namespace '{namespace}' retrieved successfully."
        )
        log_lines.append(f"Last schedule time: {last_schedule_time or 'n/a'}")
        log_lines.append(f"Last successful completion: {last_successful_time or 'n/a'}")
        log_lines.append(f"Active job references: {len(active_refs)}")

        try:
            jobs_response = batch_api.list_namespaced_job(namespace=namespace)
            jobs = getattr(jobs_response, "items", []) or []
        except api_exception as exc:
            log_lines.append(f"Failed to list jobs in namespace '{namespace}': {exc}")
            jobs = []

        related_jobs = [
            job
            for job in jobs
            if any(
                getattr(owner, "kind", None) == "CronJob" and getattr(owner, "name", None) == cronjob_name
                for owner in getattr(getattr(job, "metadata", None), "owner_references", None) or []
            )
        ]

        related_jobs.sort(
            key=lambda job: (
                getattr(getattr(job, "status", None), "start_time", None)
                or getattr(getattr(job, "metadata", None), "creation_timestamp", None)
                or datetime.min.replace(tzinfo=timezone.utc)
            ),
            reverse=True,
        )

        recent_jobs: list[dict[str, Any]] = []
        for job in related_jobs[:5]:
            job_metadata = getattr(job, "metadata", None)
            job_status = getattr(job, "status", None)
            job_name = getattr(job_metadata, "name", "(unknown)")
            recent_jobs.append(
                {
                    "job": job_name,
                    "start": self._format_optional_datetime(getattr(job_status, "start_time", None)),
                    "completion": self._format_optional_datetime(
                        getattr(job_status, "completion_time", None)
                    ),
                    "succeeded": getattr(job_status, "succeeded", 0) or 0,
                    "failed": getattr(job_status, "failed", 0) or 0,
                    "active": getattr(job_status, "active", 0) or 0,
                }
            )
            log_lines.append(
                "Job {name}: start={start} completion={completion} succeeded={succeeded} "
                "failed={failed} active={active}".format(
                    name=job_name,
                    start=recent_jobs[-1]["start"] or "n/a",
                    completion=recent_jobs[-1]["completion"] or "n/a",
                    succeeded=recent_jobs[-1]["succeeded"],
                    failed=recent_jobs[-1]["failed"],
                    active=recent_jobs[-1]["active"],
                )
            )

        payload = {
            "status": "success",
            "backend": "k8s_cronjob",
            "using_sample_data": False,
            "cronjob": cronjob_name,
            "namespace": namespace,
            "last_schedule_time": last_schedule_time,
            "last_successful_time": last_successful_time,
            "active_runs": len(active_refs),
            "recent_jobs": recent_jobs,
            "logs": log_lines,
            "retrieved_at": retrieved_at.isoformat(),
            "job_id": job_id,
        }
        return payload

    @staticmethod
    def _load_kubernetes_client():
        from kubernetes import client, config
        from kubernetes.client.exceptions import ApiException
        from kubernetes.config.config_exception import ConfigException

        return client, config, ApiException, ConfigException

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
    def _local_debug_hints(job_id: str, backend: str) -> list[str]:
        if backend == "k8s_cronjob":
            return [
                "Local hint: install the 'kubernetes' Python package to query the cluster when running locally.",
                f"Local hint: export {GCPMetricsService._K8S_NAMESPACE_ENV} and {GCPMetricsService._K8S_CRONJOB_NAME_ENV} to match your cluster.",
                "Local hint: ensure your kubeconfig points to the correct cluster or run inside GKE for in-cluster auth.",
                "Sample log placeholder:",
                f"  - {datetime.now(timezone.utc).isoformat()}: Kubernetes CronJob '{job_id}' executed (sample)",
                "  - No live data retrieved; using sample output for UI purposes.",
            ]
        return [
            "Local hint: ensure Application Default Credentials are available via 'gcloud auth application-default login'.",
            f"Local hint: verify that the Cloud Scheduler job '{job_id}' exists in the target project.",
            "Sample log placeholder:",
            f"  - {datetime.now(timezone.utc).isoformat()}: run_pipeline executed (sample)",
            "  - No live data retrieved; using sample output for UI purposes.",
        ]

    @staticmethod
    def _format_optional_datetime(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).isoformat()
        # ``kubernetes`` library returns ``datetime`` objects; fallback kept for resilience.
        try:
            return datetime.fromisoformat(str(value)).astimezone(timezone.utc).isoformat()
        except Exception:  # pragma: no cover - defensive
            return str(value)

