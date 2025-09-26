"""Publisher agent that commits polished articles to the repository."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from app.models.editor import EditedArticle
from app.models.publisher import PublicationResult


_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def _slugify(value: str) -> str:
    """Return a filesystem and URL friendly slug for the given value."""

    normalised = value.lower()
    normalised = _SLUG_PATTERN.sub("-", normalised)
    normalised = normalised.strip("-")
    return normalised or "article"


@dataclass(slots=True)
class GitPublisher:
    """Publish edited articles as Markdown committed to the local Git repository."""

    repo_path: Path
    content_directory: Path = field(default_factory=lambda: Path("content/articles"))
    git_executable: str = "git"

    def publish(
        self,
        article: EditedArticle,
        *,
        slug: str | None = None,
        commit_message: str | None = None,
        published_at: datetime | None = None,
    ) -> PublicationResult:
        """Write the article to disk and create a Git commit for the change."""

        repo_path = self.repo_path
        if not repo_path.exists():
            raise FileNotFoundError(f"Repository path '{repo_path}' does not exist")

        article_model = article.to_article()
        published = (published_at or article_model.published_date or datetime.now(timezone.utc)).astimezone(
            timezone.utc
        )

        base_slug = _slugify(slug or article_model.title)
        final_slug, destination = self._resolve_destination(base_slug)

        front_matter = self._build_front_matter(article, article_model, published)
        body = article_model.content_body.strip()
        payload = f"---\n{front_matter}\n---\n\n{body}\n"

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload, encoding="utf-8")

        relative_path = destination.relative_to(repo_path)
        self._run_git("add", str(relative_path))

        message = commit_message or f"Add article: {article_model.title}"
        self._run_git("commit", "-m", message)

        commit_hash = self._run_git("rev-parse", "HEAD").stdout.strip()

        return PublicationResult(slug=final_slug, path=destination, commit_hash=commit_hash, published_at=published)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _resolve_destination(self, base_slug: str) -> tuple[str, Path]:
        """Return a unique slug and corresponding path under the content directory."""

        content_root = self.repo_path / self.content_directory
        slug = base_slug or "article"
        candidate = content_root / f"{slug}.md"
        suffix = 2

        while candidate.exists():
            slug = f"{base_slug}-{suffix}" if base_slug else f"article-{suffix}"
            candidate = content_root / f"{slug}.md"
            suffix += 1

        return slug, candidate

    def _build_front_matter(self, article: EditedArticle, article_model: Any, published: datetime) -> str:
        """Construct YAML front matter for the markdown payload."""

        metadata: dict[str, Any] = {
            "title": article_model.title,
            "summary": article_model.summary,
            "published_at": published.isoformat(),
        }

        if article_model.tags:
            metadata["tags"] = list(article_model.tags)
        if article_model.source_urls:
            metadata["sources"] = list(article_model.source_urls)
        if article.takeaways:
            metadata["takeaways"] = list(article.takeaways)
        if article.disclaimer:
            metadata["disclaimer"] = article.disclaimer

        return yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()

    def _run_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Execute a Git command within the repository and raise on error."""

        result = subprocess.run(
            [self.git_executable, *args],
            cwd=self.repo_path,
            text=True,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            command = " ".join(args)
            raise RuntimeError(f"git {command} failed: {result.stderr.strip()}")
        return result

