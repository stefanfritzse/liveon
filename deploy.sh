#!/bin/bash

# Exit on error
set -e

# Start Minikube
echo "Starting Minikube..."
sudo minikube start --driver=docker --force

# Set Docker environment to Minikube's Docker daemon
eval $(sudo minikube docker-env)

# Build the Docker image
echo "Building Docker image..."
sudo docker build -t longevity-coach:latest .

# Apply Kubernetes manifests
echo "Applying Kubernetes manifests..."
sudo kubectl apply -f deployment.yaml
sudo kubectl apply -f service.yaml

# Get the URL of the service
echo "Getting service URL..."
sudo minikube service longevity-coach-service --url

echo "Deployment complete!"
