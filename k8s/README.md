# Kubernetes Manifests

Kubernetes deployment manifests for the Live On platform are stored here.

- `serviceaccount.yaml` – Configures the workload identity service account
  used by the web app and pipeline.
- `deployment.yaml` – Deploys the FastAPI web application serving the
  longevity coach frontend.
- `service.yaml` – Exposes the web deployment to the cluster.
- `ingress.yaml` – Routes HTTPS traffic to the service.
- `cronjob-pipeline.yaml` – Schedules the AI content pipeline to run daily at
  05:00 UTC, invoking `python -m app.scripts.run_pipeline` to publish the
  latest longevity article to Firestore.

Apply the manifests in the following order to ensure Workload Identity is
configured before the workloads start:

```sh
kubectl apply -f k8s/serviceaccount.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/cronjob-pipeline.yaml
```
