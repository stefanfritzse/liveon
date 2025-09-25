# syntax=docker/dockerfile:1

# Builder stage installs dependencies into a dedicated virtual environment.
FROM python:3.11-slim AS base

# Using a separate builder stage keeps development tools out of the final image.
FROM python:3.11 AS builder
WORKDIR /app

# Install build dependencies and application requirements.
COPY app/requirements.txt ./
RUN python -m venv /opt/venv \
    && . /opt/venv/bin/activate \
    && pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Final runtime image with only the essentials.
FROM base AS runtime
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:$PATH"
WORKDIR /app

# Copy the preinstalled virtual environment and application source.
COPY --from=builder /opt/venv /opt/venv
COPY app/ ./

EXPOSE 8080

# Run the FastAPI application with uvicorn in exec form for proper signal handling.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
