# GitHub Actions Workflows

GitHub Actions workflows will be added here to automate CI/CD for the Live On platform.

## GKE deployment runner requirements

The `Build and Deploy` workflow expects to run on a self-hosted runner that
resides inside the same GCP project/network as the private GKE control plane.
Register the runner (for example on an `e2-micro` VM) and ensure it has Docker,
`gcloud`, and `kubectl` installed so that `kubectl apply` can reach the cluster
API server.
