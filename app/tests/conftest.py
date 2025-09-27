"""Shared fixtures and helpers for the test suite."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.services.tfvars_loader import load_tfvars

_TFVARS_PATH = Path(__file__).resolve().parents[2] / "infra/environments/prod/terraform.tfvars"


@pytest.fixture(scope="session")
def prod_tfvars() -> dict[str, Any]:
    """Return Terraform variables defined for the production environment."""

    if not _TFVARS_PATH.exists():
        pytest.skip("Production terraform.tfvars file is not available")
    return load_tfvars(_TFVARS_PATH)


@pytest.fixture(scope="session")
def gcp_project_id(prod_tfvars: dict[str, Any]) -> str:
    """Provide the configured GCP project identifier for tests that need it."""

    project_id = prod_tfvars.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise RuntimeError("Terraform variables do not define a valid 'project_id'.")
    return project_id
