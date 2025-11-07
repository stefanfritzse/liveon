"""Publisher agents responsible for persisting polished articles."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess
from typing import Any, Protocol

import yaml

from app.models.content import Article
from app.models.editor import EditedArticle
from app.models.publisher import PublicationResult
from datetime import datetime, timezone

_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")

def _as_datetime(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            # accept '...Z' or '+00:00'
            txt = value.replace("Z", "+00:00")
            return datetime.fromisoformat(txt).astimezone(timezone.utc)
        except Exception:
            pass
    # last resort: now
    return datetime.now(timezone.utc)

def _slugify(value: str) -> str:
    """Return a filesystem and URL friendly slug for the given value."""

    normalised = value.lower()
    normalised = _SLUG_PATTERN.sub("-", normalised)
    normalised = normalised.strip("-")
    return normalised or "article"


class SupportsArticleRepository(Protocol):
    """Subset of the repository relied upon by publishers."""

    def get_article(self, article_id: str) -> Article | None:
        """Retrieve an article by identifier."""

    def save_article(self, article: Article) -> Article:
        """Persist an article and return the stored representation."""

    @property
    def article_collection(self) -> Any:
        """Return the backing collection reference (duck-typed in tests)."""


# ----------------------------------------------------------------------
# Git publisher (unchanged)
# ----------------------------------------------------------------------
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
        print("publish 1")
        repo_path = self.repo_path
        if not repo_path.exists():
            raise FileNotFoundError(f"Repository path '{repo_path}' does not exist")

        article_model = article.to_article()
        published = (published_at or article_model.published_date or datetime.now(timezone.utc)).astimezone(
            timezone.utc
        )
        print("publish 2")

        base_slug = _slugify(slug or article_model.title)
        final_slug, destination = self._resolve_destination(base_slug)

        front_matter = self._build_front_matter(article, article_model, published)
        body = article_model.content_body.strip()
        payload = f"---\n{front_matter}\n---\n\n{body}\n"
        print("publish 3")

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload, encoding="utf-8")

        relative_path = destination.relative_to(repo_path)
        self._run_git("add", str(relative_path))

        message = commit_message or f"Add article: {article_model.title}"
        self._run_git("commit", "-m", message)

        commit_hash = self._run_git("rev-parse", "HEAD").stdout.strip()
        print("publish 4")

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


# ----------------------------------------------------------------------
# New: Local DB publisher (SQLite or any repo with the same surface)
# ----------------------------------------------------------------------
@dataclass(slots=True)
class LocalDBPublisher:
    """Publish edited articles to a local database-backed repository (e.g., SQLite).

    Mirrors FirestorePublisher's slug resolution and duplicate detection so the
    pipeline behaves identically regardless of storage backend.
    """

    repository: SupportsArticleRepository

    def publish(
        self,
        article: EditedArticle,
        *,
        slug: str | None = None,
        commit_message: str | None = None,  # ignored – present for interface parity
        published_at: datetime | None = None,
    ) -> PublicationResult:
        """Persist the article to the local DB and return publication metadata."""
        print("db publisher 1")
        _ = commit_message  # No VCS metadata for DB publishes.

        published = (published_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        base_slug = _slugify(slug or article.title)
        print("db publisher 2")
        resolved_slug, existing = self._resolve_target(base_slug, article)
        if existing is not None and self._is_duplicate(existing, article):
            return PublicationResult(
                slug=existing.id or resolved_slug,
                path=self._build_storage_path(existing.id or resolved_slug),
                commit_hash=None,
                published_at=existing.published_date,
            )
        print("db publisher 3")
        article_model = article.to_article()
        article_model.id = resolved_slug
        article_model.published_date = published
        print("db publisher 4")
        stored = self.repository.save_article(article_model)
        print("db publisher 5")
        return PublicationResult(
            slug=stored.id or resolved_slug,
            path=self._build_storage_path(stored.id or resolved_slug),
            commit_hash=None,
            published_at=_as_datetime(stored.published_date),  
        )

    # ------------------------------------------------------------------
    # Helpers (copied to keep class self-contained)
    # ------------------------------------------------------------------
    def _resolve_target(self, base_slug: str, article: EditedArticle) -> tuple[str, Article | None]:
        slug = base_slug or "article"
        existing = self.repository.get_article(slug)
        if existing is not None and self._is_duplicate(existing, article):
            return slug, existing

        suffix = 2
        while existing is not None:
            slug = f"{base_slug}-{suffix}" if base_slug else f"article-{suffix}"
            candidate = self.repository.get_article(slug)
            if candidate is None or self._is_duplicate(candidate, article):
                return slug, candidate
            existing = candidate
            suffix += 1

        return slug, None

    @staticmethod
    def _is_duplicate(existing: Article, article: EditedArticle) -> bool:
        """Return ``True`` when the edited article matches the stored entry."""

        if existing.title.strip() != article.title.strip():
            return False

        rendered = article.to_article()
        rendered.summary = rendered.summary.strip()
        rendered.content_body = rendered.content_body.strip()

        return (
            existing.summary == rendered.summary
            and existing.content_body == rendered.content_body
            and set(existing.source_urls) == set(rendered.source_urls)
            and set(existing.tags) == set(rendered.tags)
        )

    def _build_storage_path(self, slug: str) -> Path:
        """Return a storage-agnostic, local path metadata value for logging/UX."""
        collection = getattr(self.repository.article_collection, "id", "articles")
        return Path("db") / str(collection) / slug
