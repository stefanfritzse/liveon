"""Lightweight helpers for parsing Terraform ``.tfvars`` files."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator


def _iter_clean_lines(path: Path) -> Iterator[str]:
    """Yield lines from ``path`` with comments removed and whitespace stripped."""

    for raw_line in path.read_text().splitlines():
        line, *_ = raw_line.split("#", maxsplit=1)
        stripped = line.strip()
        if stripped:
            yield stripped


def _normalise_scalar(value: str) -> Any:
    """Convert a Terraform scalar representation into a Python value."""

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


def load_tfvars(path: Path) -> dict[str, Any]:
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


def extract_project_id(path: Path) -> str | None:
    """Return the ``project_id`` entry from ``path`` if available and valid."""

    variables = load_tfvars(path)
    project_id = variables.get("project_id")
    if isinstance(project_id, str) and project_id.strip():
        return project_id.strip()
    return None

