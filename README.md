# Live On – Longevity Coach Platform

Live On is an experimental platform that combines agentic content pipelines with a FastAPI web front-end. It aggregates longevity news, summarizes drafts, polishes the copy, and publishes both long-form articles and coaching tips. A conversational coach interface lets users ask questions that are answered with a locally hosted Ollama model, making it easy to iterate offline while keeping sensitive data on the developer machine.

## Key Components

- **FastAPI application (`app/main.py`)** – Serves the public site (home, articles, tips) plus the `/coach` interface and JSON APIs (`/api/ask`, `/api/tips/latest`, `/healthz`).
- **SQLite content repository (`app/services/sqlite_repo.py`)** – Stores articles and tips locally, mirroring the Firestore surface used in production. Falls back to in-memory seed data if a database is unavailable.
- **Agent pipeline (`app/services/pipeline.py`, `app/scripts/run_pipeline.py`)** – Orchestrates aggregation, summarisation, editing, and publishing. Supports Git- or DB-backed publication flows.
- **Coach agent (`app/services/coach.py`)** – Wraps LangChain + Ollama (or a direct HTTP client) to generate answers with the configured local model.
- **Deployment scripts (`deploy.ps1`, `deployment.yaml`, `service.yaml`)** – Automate build + apply steps for a Minikube cluster, including port-forwarding and health checks.

## Requirements

- Python 3.11
- pip / virtual environment of your choice
- SQLite (bundled with Python) for local storage
- [Ollama](https://ollama.com/) running locally (default: `http://127.0.0.1:11434`) for the coaching model
- Optional: Docker + Minikube if you want to run `deploy.ps1`

Install dependencies:

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r app/requirements.txt
```

The coach agent can use LangChain’s wrappers when available:

```powershell
python -m pip install --upgrade langchain-core langchain-community langchain-ollama
```

## Running the Web App

1. Ensure Ollama is running (`ollama serve`) and has the desired model pulled (default: `phi3:14b-medium-4k-instruct-q4_K_M`).
2. Start the API:

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

3. Visit `http://localhost:8080/` for the site or `http://localhost:8080/coach` for the conversational UI.

### Environment Variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `LIVEON_STORAGE` | Storage backend (`sqlite`, `memory`) | `sqlite` |
| `LIVEON_DB_PATH` | Custom SQLite file path | `~/liveon/data/content.db` |
| `LIVEON_LLM_PROVIDER` | `ollama` or a future provider | `ollama` |
| `LIVEON_OLLAMA_MODEL` | Ollama model name | `phi3:14b-medium-4k-instruct-q4_K_M` |
| `LIVEON_OLLAMA_URL` | Ollama base URL | `http://127.0.0.1:11434` |

When the Ollama daemon is bound to `0.0.0.0`, still point `LIVEON_OLLAMA_URL` (or the pipeline command's environment) at a reachable host such as `http://127.0.0.1:11434` so local clients can connect successfully.

## Running the Content Pipeline

The pipeline CLI (`app/scripts/run_pipeline.py`) can be executed to aggregate feeds and publish new content:

```powershell
python -m app.scripts.run_pipeline --feed-limit 5
```

This command respects the same storage environment variables, so ensure `LIVEON_DB_PATH` points to the SQLite file you want to populate. The project also ships with a Git publisher for writing Markdown into a repository, making it easy to sync finished articles elsewhere.

## Deployment (Minikube)

`deploy.ps1` automates the local Kubernetes workflow:

1. Builds the Docker image using Minikube’s Docker daemon.
2. Applies `deployment.yaml` and `service.yaml`.
3. Waits for rollout, cleans old port-forward jobs, and establishes a new `kubectl port-forward` to `http://127.0.0.1:8080`.

Run it from PowerShell:

```powershell
pwsh ./deploy.ps1
```

Ensure Minikube (Docker driver) and kubectl are available. The script forwards proxy environment variables automatically and sets custom DNS entries to avoid registry resolution issues.

## Testing

Unit tests live under `app/tests`. Use pytest from the repo root:

```powershell
pytest
```

The test suite focuses on models, pipeline orchestration, tip publishing, and FastAPI routes. Extend these tests when adding new agents, storage backends, or API endpoints.

## Repository Structure

```
├── app/
│   ├── main.py                 # FastAPI entrypoint
│   ├── models/                 # Domain models (content, coach, editor, tips, etc.)
│   ├── services/               # Pipeline, publishers, repositories, coach agent
│   ├── scripts/                # CLI utilities (run_pipeline, run_tip_pipeline)
│   ├── templates/              # Jinja2 templates for the web UI
│   └── tests/                  # Pytest suites
├── deploy.ps1 / deployment.yaml / service.yaml
├── Dockerfile
├── deploy_patch.json (optional PVC patch)
└── README.md
```

## Support & Next Steps

- Adjust `app/services/coach.py` if you want different prompts, or point to remote LLM providers.
- Extend `app/services/sqlite_repo.py` for alternate storage (e.g., Postgres) while keeping the repository interface consistent.
- Integrate CI/CD pipelines (GitHub Actions templates live under `.github/`) to automate content runs or deployments.

Feel free to fork and tailor the agents, feeds, or storage layer to match your longevity coaching workflows.
