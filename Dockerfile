# syntax=docker/dockerfile:1

############################
# Builder: install deps into /opt/venv
############################
FROM python:3.11-slim AS builder
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Build tools only in builder (grpc/cryptography wheels usually prebuilt,
# but this keeps you safe if any wheel falls back to source)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Keep layer cache stable: copy only requirements first
COPY app/requirements.txt ./requirements.txt

RUN python -m venv "$VIRTUAL_ENV" \
 && pip install --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

############################
# Runtime: minimal, non-root
############################
FROM python:3.11-slim AS runtime

# Fast logs + fewer temp files
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Stockholm

WORKDIR /app

# Add an unprivileged user
RUN useradd -u 10001 -m appuser

# Bring in the prebuilt venv and your code
COPY --from=builder /opt/venv /opt/venv
COPY app /app/app
# Ensure app user can read the code (and write if your app needs it)
RUN chown -R appuser:appuser /app

USER appuser
EXPOSE 8080

# Default for the web app; the CronJob overrides with: python -m app.scripts.run_pipeline
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
