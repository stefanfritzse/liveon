# deploy.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Write-Host "Starting Minikube..."
minikube start --driver=docker --force

Write-Host "Pointing Docker to Minikube's daemon..."
(minikube docker-env --shell=powershell) | Invoke-Expression

Write-Host "Building Docker image..."
docker build -t longevity-coach:latest .

Write-Host "Applying Kubernetes manifests..."
kubectl apply -f .\deployment.yaml
kubectl apply -f .\service.yaml

Write-Host "Getting service URL..."
$urls = minikube service longevity-coach-service --url
$urls | ForEach-Object { Write-Host "Service available at $_" }

Write-Host "Deployment complete!"
