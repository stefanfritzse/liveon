"""Minimal FastAPI application for the Longevity Coach platform."""

from fastapi import FastAPI

app = FastAPI(title="Longevity Coach API")


@app.get("/")
def read_root() -> dict[str, str]:
    """Return a simple health message for Phase 1 validation."""
    return {"message": "Hello World"}
