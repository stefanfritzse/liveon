# Kubernetes Manifests

Kubernetes deployment manifests for the Live On platform are stored here.

- `serviceaccount.yaml` – Configures the workload identity service account
  used by the web app and pipeline workloads.
- `deployment.yaml` – Deploys the FastAPI web application serving the
  longevity coach frontend.
- `service.yaml` – Exposes the web deployment to the cluster.
- `ingress.yaml` – Routes HTTPS traffic to the service.
- `cronjob-pipeline.yaml` – Schedules the AI content pipeline to run daily at
  05:00 UTC, invoking `python -m app.scripts.run_pipeline` to publish the
  latest longevity article to Firestore.
- `cronjob-tips.yaml` – Triggers the daily tip pipeline at 06:30 UTC via
  `python -m app.scripts.run_tip_pipeline`, storing concise longevity advice.

## Applying manifests

Apply the manifests in the following order to ensure Workload Identity is
configured before the workloads start:

```sh
kubectl apply -f k8s/serviceaccount.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/cronjob-pipeline.yaml
kubectl apply -f k8s/cronjob-tips.yaml
```

## Tip pipeline configuration

The tip CronJob relies on the following environment variables and CLI flags:

- `LIVEON_LOG_LEVEL` – Controls logging verbosity (default `INFO`).
- `LIVEON_TIP_FEED_LIMIT` – Optional override for `--limit-per-feed`.
- `LIVEON_TIP_MODEL`, or the `--model-provider` flag – Selects the LLM backend
  (`local`, `vertex`, or `openai`).
- `LIVEON_TIP_MODEL_NAME`, `LIVEON_TIP_VERTEX_MODEL`, or `LIVEON_TIP_OPENAI_MODEL` –
  Override the model identifier used by the selected provider. Alternatively
  pass `--model` to the script.
- `LIVEON_ALLOW_LOCAL_LLM` or the `--allow-local-llm` flag – Permit the local
  stub responder when running in managed environments (disabled by default).
- `GOOGLE_CLOUD_PROJECT`/`GOOGLE_CLOUD_REGION` – Required for the Firestore
  repository and Vertex AI integration.

The CronJob manifest sets `--limit-per-feed 4`, `--model-provider vertex`, and
`--model gemini-2.0-flash-lite-001` so the task runs after the article pipeline
with a lightweight Vertex AI model.

## Coach API configuration

The FastAPI deployment exposes `/api/ask`, which depends on an LLM backend for
answers. Configure the following environment variables (defaults shown below)
to point the service at Vertex AI or OpenAI models:

| Variable | Default | Description |
| --- | --- | --- |
| `LIVEON_COACH_MODEL` | `local` | Selects the LLM provider (`vertex`, `openai`, or `local`). |
| `LIVEON_COACH_VERTEX_MODEL` | `chat-bison` | Vertex AI chat model to invoke when the provider is `vertex`. |
| `LIVEON_VERTEX_LOCATION` | _unset_ | Optional region for Vertex AI API calls. |
| `LIVEON_COACH_OPENAI_MODEL` | `gpt-4o-mini` | OpenAI chat model to invoke when the provider is `openai`. |
| `LIVEON_MODEL_TEMPERATURE` | `0.2` | Sampling temperature forwarded to the configured LLM. |
| `LIVEON_MODEL_MAX_OUTPUT_TOKENS` | `1024` | Maximum tokens returned per response. |

Override these values in production by patching the deployment or sourcing them
from a ConfigMap/Secret to align with the preferred LLM provider.

The `/api/ask` response always includes the safety disclaimer so downstream
clients must surface the message verbatim.
