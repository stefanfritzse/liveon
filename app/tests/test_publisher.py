from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import subprocess
from pathlib import Path
from types import SimpleNamespace

import yaml

from app.models.content import Article
from app.models.editor import EditedArticle
from app.services.publisher import GitPublisher


def sample_article() -> EditedArticle:
    return EditedArticle(
        title="Prioritise Deep Sleep for Longevity Gains",
        summary="Consistent deep sleep supports metabolic and cognitive resilience over time.",
        body=(
            "## Restore nightly\n"
            "Aim for seven to nine hours of high-quality sleep to regulate hormones and support cellular repair."
        ),
        takeaways=[
            "Keep a consistent bedtime routine",
            "Limit blue light exposure one hour before sleep",
        ],
        sources=["https://example.com/research/sleep-quality"],
        tags=["sleep", "recovery"],
        disclaimer="Consult your healthcare professional when adjusting sleep aids or medications.",
    )


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    subprocess.run(["git", "config", "user.name", "LiveOn Bot"], cwd=path, check=True, stdout=subprocess.PIPE, text=True)
    subprocess.run([
        "git",
        "config",
        "user.email",
        "bot@example.com",
    ], cwd=path, check=True, stdout=subprocess.PIPE, text=True)


def test_publish_creates_markdown_and_git_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)

    publisher = GitPublisher(repo_path=repo)

    result = publisher.publish(sample_article())

    output_path = repo / "content" / "articles" / f"{result.slug}.md"
    assert output_path.exists()

    content = output_path.read_text(encoding="utf-8")
    front_matter_raw, body = content.split("---\n", 2)[1:3]
    front_matter = yaml.safe_load(front_matter_raw)

    assert front_matter["title"] == "Prioritise Deep Sleep for Longevity Gains"
    assert "sleep" in front_matter["tags"]
    assert front_matter["sources"] == ["https://example.com/research/sleep-quality"]
    assert front_matter["disclaimer"].startswith("Consult your healthcare professional")
    assert "## Restore nightly" in body

    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    assert log.stdout.strip() == "Add article: Prioritise Deep Sleep for Longevity Gains"

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    assert result.commit_hash == head.stdout.strip()
    assert isinstance(result.published_at, datetime)
    assert result.published_at.tzinfo == timezone.utc


def test_publish_generates_unique_slug(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)

    publisher = GitPublisher(repo_path=repo)

    first = publisher.publish(sample_article())
    second = publisher.publish(sample_article())

    assert first.slug != second.slug

    first_path = repo / "content" / "articles" / f"{first.slug}.md"
    second_path = repo / "content" / "articles" / f"{second.slug}.md"
    assert first_path.exists()
    assert second_path.exists()

