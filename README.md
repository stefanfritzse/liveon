# Live On Platform

Live On is an AI-driven longevity coaching and content platform. It aggregates research, generates health tips, and provides an interactive coaching experience.

> **Note:** This project was originally designed for a cloud-native deployment on Google Cloud Platform (GKE, Cloud SQL, etc.). It has since been refactored to run as a standalone local application.

## Quick Start

### Prerequisites
- Python 3.12+
- `pip`

### Installation

1.  **Clone the repository:**
    ```bash
    git clone <repository-url>
    cd liveon
    ```

2.  **Create and activate a virtual environment:**
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```

3.  **Install dependencies:**
    ```bash
    pip install -r app/requirements.txt
    ```

### Running the Application

1.  **Start the Web Server:**
    ```bash
    uvicorn app.main:app --reload
    ```
    The application will be available at [http://localhost:8000](http://localhost:8000).

2.  **Explore the UI:**
    *   **Home:** [http://localhost:8000/](http://localhost:8000/)
    *   **Coach:** [http://localhost:8000/coach](http://localhost:8000/coach)
    *   **Articles:** [http://localhost:8000/articles](http://localhost:8000/articles)

### Running the Content Pipeline

The content pipeline aggregates news, summarizes articles, and publishes them locally.

1.  **Run the pipeline manually:**
    ```bash
    python -m app.scripts.run_pipeline
    ```
    By default, this uses local/deterministic responders.

## Configuration

The application uses environment variables for configuration.

| Variable | Description | Default |
| --- | --- | --- |
| `LIVEON_COACH_VERTEX_MODEL` | Vertex AI model name | `chat-bison` |
| `LIVEON_SUMMARIZER_MODEL` | Pipeline summarizer backend (`local`, `vertex`, `openai`) | `local` |
| `LIVEON_EDITOR_MODEL` | Pipeline editor backend (`local`, `vertex`, `openai`) | `local` |
| `GOOGLE_CLOUD_PROJECT` | GCP Project ID (for Vertex AI/Firestore) | (Optional for local mode) |

## Features

### Web Interface
Built with **FastAPI** and **Jinja2**, offering a responsive UI for:
*   **Home:** Highlights and tips.
*   **Articles:** Aggregated longevity research.
*   **Coach:** Interactive chat interface.

### AI Content Agents
*   **Aggregator:** Pulls content from RSS feeds (e.g., Google News).
*   **Summarizer:** Condenses articles into digestible summaries.
*   **Editor:** Refines content for clarity and tone.
*   **Publisher:** Saves content to the local repository or database.

## Legacy Architecture (GCP)

*Historical Context:* This project was initially architected for a production-grade Kubernetes deployment on Google Cloud. The `infra/` directory contains the original Terraform configurations for:
*   **GKE Cluster**: Autopilot cluster for hosting the app.
*   **Artifact Registry**: For container images.
*   **Cloud Build / GitHub Actions**: For CI/CD.

While these components are no longer required for the local version, they are preserved in the repository for reference or future cloud scaling.

## Local Kubernetes (Minikube)

You can run the application in a local Kubernetes cluster using Minikube.

1.  **Start Minikube:**
    ```bash
    minikube start
    minikube addons enable ingress
    ```

2.  **Build the Docker image:**
    Point your shell to Minikube's docker-daemon so the image is available to the cluster:
    ```bash
    eval $(minikube -p minikube docker-env)
    docker build -t liveon:latest .
    ```

3.  **Deploy manifests:**
    Apply the Kubernetes configurations found in the `k8s/` directory. You may need to adjust image names in `deployment.yaml` to match `liveon:latest` and set `imagePullPolicy: Never` or `IfNotPresent` for local dev.
    ```bash
    kubectl apply -f k8s/
    ```

4.  **Access the App:**
    Get the Ingress IP and add it to your `/etc/hosts` if necessary, or access via the service directly:
    ```bash
    minikube service liveon-service
    ```

## Testing

Run the test suite to ensure stability:

```bash
pytest
```