"""Orchestration layer that chains the aggregator, summariser, editor, and publisher agents."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, Sequence

from app.models.aggregator import AggregatedContent
from app.models.content import Article
from app.models.editor import EditedArticle
from app.models.publisher import PublicationResult
from app.models.summarizer import ArticleDraft
from app.services.aggregator import AggregationResult
from app.services.publisher import _slugify


class SupportsAggregation(Protocol):
    """Subset of :class:`LongevityNewsAggregator` relied on by the pipeline."""

    def gather(self, *, limit_per_feed: int = 5) -> AggregationResult:
        """Return aggregated longevity updates."""


class SupportsSummarisation(Protocol):
    """Protocol describing the summariser agent interface."""

    def summarize(self, items: Sequence[AggregatedContent]) -> ArticleDraft:
        """Return a draft article derived from aggregated content."""


class SupportsEditing(Protocol):
    """Protocol describing the editor agent interface."""

    def revise(self, draft: ArticleDraft) -> EditedArticle:
        """Return a polished article derived from the summariser draft."""


class SupportsPublishing(Protocol):
    """Protocol describing the publisher agent interface."""

    def publish(
        self,
        article: EditedArticle,
        *,
        slug: str | None = None,
        commit_message: str | None = None,
        published_at: datetime | None = None,
    ) -> PublicationResult:
        """Publish the edited article and return metadata for the operation."""


class SupportsSourceLookup(Protocol):
    """Repository helper able to look up stored articles by source URL."""

    def find_article_by_source_url(self, url: str) -> Article | None:
        """Return an existing article that references ``url`` if one exists."""


@dataclass(slots=True)
class PipelineResult:
    """Structured summary of a pipeline execution."""

    aggregation: AggregationResult
    draft: ArticleDraft | None = None
    edited: EditedArticle | None = None
    publication: PublicationResult | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        """Return ``True`` when the pipeline produced a publication without fatal errors."""

        return self.publication is not None and not self.errors


@dataclass(slots=True)
class ContentPipeline:
    """Coordinate the agent workflow from aggregation through publication."""

    aggregator: SupportsAggregation
    summarizer: SupportsSummarisation
    editor: SupportsEditing
    publisher: SupportsPublishing
    repository: SupportsSourceLookup | None = None

    def run(
        self,
        *,
        limit_per_feed: int = 5,
        slug: str | None = None,
        commit_message: str | None = None,
        published_at: datetime | None = None,
    ) -> PipelineResult:
        """Execute the end-to-end content pipeline, returning the aggregated results."""

        aggregation = self.aggregator.gather(limit_per_feed=limit_per_feed)
        warnings = list(aggregation.errors)
        errors: list[str] = []

        repository = self.repository or getattr(self.publisher, "repository", None)

        selected_item: AggregatedContent | None = None
        if aggregation.items and repository is not None:
            for item in aggregation.items:
                url = (item.url or "").strip()
                if not url:
                    continue
                if repository.find_article_by_source_url(url) is None:
                    selected_item = item
                    break
        elif aggregation.items:
            for item in aggregation.items:
                if (item.url or "").strip():
                    selected_item = item
                    break

        if selected_item is None:
            if aggregation.items:
                warnings.append("No new aggregated content available to publish.")
            else:
                warnings.append("No aggregated content available to summarise.")
            return PipelineResult(
                aggregation=aggregation,
                draft=None,
                edited=None,
                publication=None,
                errors=errors,
                warnings=warnings,
            )

        try_items: list[AggregatedContent] = [selected_item]

        try:
            draft = self.summarizer.summarize(try_items)
        except Exception as exc:  # pragma: no cover - defensive fallback
            errors.append(f"Summarizer failed: {exc}")
            return PipelineResult(
                aggregation=aggregation,
                draft=None,
                edited=None,
                publication=None,
                errors=errors,
                warnings=warnings,
            )

        try:
            edited = self.editor.revise(draft)
        except Exception as exc:  # pragma: no cover - defensive fallback
            errors.append(f"Editor failed: {exc}")
            return PipelineResult(
                aggregation=aggregation,
                draft=draft,
                edited=None,
                publication=None,
                errors=errors,
                warnings=warnings,
            )

        slug_to_use = slug or _slugify(selected_item.title or selected_item.url or "")

        try:
            publication = self.publisher.publish(
                edited,
                slug=slug_to_use,
                commit_message=commit_message,
                published_at=published_at,
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            errors.append(f"Publisher failed: {exc}")
            return PipelineResult(
                aggregation=aggregation,
                draft=draft,
                edited=edited,
                publication=None,
                errors=errors,
                warnings=warnings,
            )

        return PipelineResult(
            aggregation=aggregation,
            draft=draft,
            edited=edited,
            publication=publication,
            errors=errors,
            warnings=warnings,
        )

