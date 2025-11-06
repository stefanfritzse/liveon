"""Orchestration layer that chains the aggregator, summariser, editor, and publisher agents."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, Sequence

from app.models.aggregator import AggregatedContent
from app.models.content import Article, Tip
from app.models.editor import EditedArticle
from app.models.publisher import PublicationResult
from app.models.summarizer import ArticleDraft
from app.models.tip import TipDraft
from app.services.aggregator import AggregationResult
from app.services.publisher import _slugify
from app.services.tip_publisher import TipPublicationResult


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
        print("1")
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
        print("2")
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
        print("3")
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
        print("4")
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
        print("5")
        slug_to_use = slug or _slugify(selected_item.title or selected_item.url or "")
        print("5.1")
        try:
            publication = self.publisher.publish(
                edited,
                slug=slug_to_use,
                commit_message=commit_message,
                published_at=published_at,
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            #errors.append(f"Publisher failed: {exc}")
            import logging
            logging.getLogger("liveon.pipeline").exception("Publisher failed")
            return PipelineResult(
                aggregation=aggregation,
                draft=draft,
                edited=edited,
                publication=None,
                errors=errors,
                warnings=warnings,
            )
        print("6")
        return PipelineResult(
            aggregation=aggregation,
            draft=draft,
            edited=edited,
            publication=publication,
            errors=errors,
            warnings=warnings,
        )


class SupportsTipGeneration(Protocol):
    """Protocol describing the tip generator agent interface."""

    def generate(self, items: Sequence[AggregatedContent]) -> TipDraft:
        """Return a tip draft derived from aggregated content."""


class SupportsTipPublishing(Protocol):
    """Protocol describing the tip publisher interface used by the pipeline."""

    def publish(
        self,
        draft: TipDraft,
        *,
        published_at: datetime | None = None,
    ) -> TipPublicationResult:
        """Persist the draft and return metadata about the stored tip."""


@dataclass(slots=True)
class TipPipelineResult:
    """Structured summary of a tip pipeline execution."""

    aggregation: AggregationResult
    draft: TipDraft | None = None
    tip: Tip | None = None
    publication: TipPublicationResult | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        """Return ``True`` when the pipeline generated a tip without fatal errors."""

        return self.tip is not None and not self.errors

    @property
    def created(self) -> bool:
        """Return ``True`` when the pipeline resulted in a newly stored tip."""

        return bool(self.publication and self.publication.created)


@dataclass(slots=True)
class TipPipeline:
    """Coordinate the workflow from aggregation through tip publication."""

    aggregator: SupportsAggregation
    generator: SupportsTipGeneration
    publisher: SupportsTipPublishing

    def run(
        self,
        *,
        limit_per_feed: int = 5,
        published_at: datetime | None = None,
    ) -> TipPipelineResult:
        """Execute the tip pipeline, returning a structured result."""

        aggregation = self.aggregator.gather(limit_per_feed=limit_per_feed)
        warnings = list(aggregation.errors)
        errors: list[str] = []

        if not aggregation.items:
            warnings.append("No aggregated content available to generate tips.")
            return TipPipelineResult(
                aggregation=aggregation,
                draft=None,
                tip=None,
                publication=None,
                errors=errors,
                warnings=warnings,
            )

        try:
            draft = self.generator.generate(aggregation.items)
        except Exception as exc:  # pragma: no cover - defensive fallback
            errors.append(f"Tip generator failed: {exc}")
            return TipPipelineResult(
                aggregation=aggregation,
                draft=None,
                tip=None,
                publication=None,
                errors=errors,
                warnings=warnings,
            )

        try:
            publication = self.publisher.publish(draft, published_at=published_at)
        except Exception as exc:  # pragma: no cover - defensive fallback
            errors.append(f"Tip publisher failed: {exc}")
            return TipPipelineResult(
                aggregation=aggregation,
                draft=draft,
                tip=None,
                publication=None,
                errors=errors,
                warnings=warnings,
            )

        tip = publication.tip
        if not publication.created:
            warnings.append("Tip already exists; skipped creating a duplicate.")

        return TipPipelineResult(
            aggregation=aggregation,
            draft=draft,
            tip=tip,
            publication=publication,
            errors=errors,
            warnings=warnings,
        )

