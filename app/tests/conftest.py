"""Shared fixtures and helpers for the test suite."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest


_TFVARS_PATH = Path(__file__).resolve().parents[2] / "infra/environments/prod/terraform.tfvars"


def _iter_clean_lines(path: Path) -> Iterator[str]:
    """Yield lines from ``path`` with comments removed and whitespace stripped."""

    for raw_line in path.read_text().splitlines():
        line, *_ = raw_line.split("#", maxsplit=1)
        stripped = line.strip()
        if stripped:
            yield stripped


def _normalise_scalar(value: str) -> Any:
    """Convert a Terraform scalar value to an equivalent Python value."""

    value = value.rstrip(",")
    if value.startswith("\"") and value.endswith("\""):
        return value[1:-1]
    lower = value.lower()
    if lower in {"true", "false"}:
        return lower == "true"
    if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
        return int(value)
    try:
        return float(value)
    except ValueError:
        return value


def _load_tfvars(path: Path) -> dict[str, Any]:
    """Parse a subset of Terraform ``.tfvars`` syntax into a dictionary."""

    assignments: dict[str, Any] = {}
    lines = list(_iter_clean_lines(path))
    i = 0
    while i < len(lines):
        line = lines[i]
        if "=" not in line:
            i += 1
            continue

        key, raw_value = (segment.strip() for segment in line.split("=", maxsplit=1))
        if not key:
            i += 1
            continue

        if raw_value.startswith(("[", "{")) and not raw_value.endswith(("]", "}")):
            block_lines = [raw_value]
            depth = raw_value.count("[") + raw_value.count("{")
            depth -= raw_value.count("]") + raw_value.count("}")
            i += 1
            while i < len(lines) and depth > 0:
                segment = lines[i]
                depth += segment.count("[") + segment.count("{")
                depth -= segment.count("]") + segment.count("}")
                block_lines.append(segment)
                i += 1
            assignments[key] = " ".join(block_lines).rstrip(",")
            continue

        assignments[key] = _normalise_scalar(raw_value)
        i += 1

    return assignments


@pytest.fixture(scope="session")
def prod_tfvars() -> dict[str, Any]:
    """Return Terraform variables defined for the production environment."""

    if not _TFVARS_PATH.exists():
        pytest.skip("Production terraform.tfvars file is not available")
    return _load_tfvars(_TFVARS_PATH)


@pytest.fixture(scope="session")
def gcp_project_id(prod_tfvars: dict[str, Any]) -> str:
    """Provide the configured GCP project identifier for tests that need it."""

    project_id = prod_tfvars.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise RuntimeError("Terraform variables do not define a valid 'project_id'.")
    return project_id
