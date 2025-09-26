# Kubernetes Manifests

Kubernetes deployment manifests for the Live On platform are stored here.

- `deployment.yaml` – Deploys the FastAPI web application serving the
  longevity coach frontend.
- `service.yaml` – Exposes the web deployment to the cluster.
- `ingress.yaml` – Routes HTTPS traffic to the service.
- `cronjob-pipeline.yaml` – Schedules the AI content pipeline to run daily at
  05:00 UTC, invoking `python -m app.scripts.run_pipeline` to publish the
  latest longevity article to Firestore.
