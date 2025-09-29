"""Orchestration layer that chains the aggregator, summariser, editor, and publisher agents."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, Sequence

from app.models.aggregator import AggregatedContent
from app.models.editor import EditedArticle
from app.models.publisher import PublicationResult
from app.models.summarizer import ArticleDraft
from app.services.aggregator import AggregationResult


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

        if not aggregation.items:
            warning_message = "No aggregated content available to summarise."
            warnings.append(warning_message)
            return PipelineResult(
                aggregation=aggregation,
                draft=None,
                edited=None,
                publication=None,
                errors=errors,
                warnings=warnings,
            )

        try:
            draft = self.summarizer.summarize(aggregation.items)
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

        try:
            publication = self.publisher.publish(
                edited,
                slug=slug,
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
