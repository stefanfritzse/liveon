"""Orchestration layer that chains the aggregator, summariser, editor, and publisher agents."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, Sequence

from app.models.aggregator import AggregatedContent
from app.models.content import Article, Tip
from app.models.editor import EditedArticle
from app.models.publisher import PublicationResult
from app.models.summarizer import ArticleDraft
from app.models.tip import TipDraft
from app.models.tip_editor import TipReviewResult
from app.services.aggregator import AggregationResult
from app.services.publisher import _slugify
from app.services.tip_publisher import TipPublicationResult


logger = logging.getLogger(__name__)
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

    def generate(self, items: Sequence[AggregatedContent], feedback: str | None = None) -> TipDraft:
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


class SupportsTipEditing(Protocol):
    """Protocol describing the tip editor agent interface."""

    def review(self, draft: TipDraft, existing_tips: Sequence[Tip]) -> TipReviewResult:
        """Return a review result for the provided draft."""


class SupportsTipLookup(Protocol):
    """Repository helper able to fetch recent tips for comparison."""

    def get_latest_tips(self, *, limit: int = 5) -> list[Tip]:
        """Return the latest stored tips, ordered by recency."""

    def find_article_by_source_url(self, url: str) -> Article | None:
        """Optional compatibility hook shared with the article repository."""


@dataclass(slots=True)
class TipPipelineResult:
    """Structured summary of a tip pipeline execution."""

    aggregation: AggregationResult
    draft: TipDraft | None = None
    tip: Tip | None = None
    publication: TipPublicationResult | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    generation_attempts: int = 1
    editor_feedback: list[str] = field(default_factory=list)

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
    editor: SupportsTipEditing
    publisher: SupportsTipPublishing
    repository: SupportsTipLookup
    MAX_GENERATION_ATTEMPTS: int = 3

    def run(
        self,
        *,
        limit_per_feed: int = 5,
        published_at: datetime | None = None,
    ) -> TipPipelineResult:
        """Execute the tip pipeline with an editor-in-the-loop review cycle."""

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
                generation_attempts=0,
                editor_feedback=[],
            )

        try:
            existing_tips = self.repository.get_latest_tips(limit=20)
        except Exception as exc:
            warnings.append(f"Could not fetch existing tips for comparison: {exc}")
            existing_tips = []

        feedback: str | None = None
        draft: TipDraft | None = None
        review_result: TipReviewResult | None = None
        attempt = 0
        feedback_log: list[str] = []

        while attempt < self.MAX_GENERATION_ATTEMPTS:
            attempt += 1
            try:
                draft = self.generator.generate(aggregation.items, feedback=feedback)
            except Exception as exc:
                error_msg = f"Tip generator failed on attempt {attempt}: {exc}"
                errors.append(error_msg)
                feedback = (
                    f"The previous attempt failed with error '{exc}'. "
                    "Generate a new, high-quality tip that avoids that issue."
                )
                feedback_log.append(error_msg)
                continue

            if not draft or not draft.body.strip():
                warning_msg = f"Generator produced empty draft on attempt {attempt}."
                warnings.append(warning_msg)
                feedback = (
                    "The generated tip was empty or invalid. Produce a concise title and body "
                    "with actionable guidance."
                )
                feedback_log.append(warning_msg)
                continue

            logger.info(
                "TIP_PIPELINE_DRAFT attempt=%s title=%s body=%s",
                attempt,
                draft.title.strip(),
                draft.body.strip(),
            )

            try:
                review_result = self.editor.review(draft, existing_tips)
            except Exception as exc:
                error_msg = f"Tip editor failed on attempt {attempt}: {exc}"
                errors.append(error_msg)
                feedback = (
                    "The previous tip could not be reviewed due to an internal error. "
                    "Propose a fresh tip that strictly follows the rubric."
                )
                feedback_log.append(error_msg)
                review_result = None
                continue

            review_feedback = review_result.feedback or ""
            if review_feedback:
                feedback_log.append(f"Attempt {attempt} feedback: {review_feedback}")
            else:
                feedback_log.append(f"Attempt {attempt} feedback: (no feedback provided)")

            if review_result.is_approved:
                break

            feedback = (
                review_feedback
                or "The tip was rejected for unspecified reasons. Provide a more novel, concise insight."
            )
            warnings.append(
                f"Tip draft rejected (Attempt {attempt}/{self.MAX_GENERATION_ATTEMPTS}): {feedback}"
            )
            review_result = None

        if review_result is None or not review_result.is_approved:
            errors.append(
                f"Failed to generate an approved tip after {self.MAX_GENERATION_ATTEMPTS} attempts."
            )
            return TipPipelineResult(
                aggregation=aggregation,
                draft=draft,
                tip=None,
                publication=None,
                errors=errors,
                warnings=warnings,
                generation_attempts=attempt,
                editor_feedback=feedback_log,
            )

        final_draft = review_result.revised_draft or draft
        if final_draft is None:
            errors.append("Editor approved review result but no draft was available.")
            return TipPipelineResult(
                aggregation=aggregation,
                draft=draft,
                tip=None,
                publication=None,
                errors=errors,
                warnings=warnings,
                generation_attempts=attempt,
                editor_feedback=feedback_log,
            )

        try:
            publication = self.publisher.publish(final_draft, published_at=published_at)
        except Exception as exc:
            errors.append(f"Tip publisher failed: {exc}")
            return TipPipelineResult(
                aggregation=aggregation,
                draft=final_draft,
                tip=None,
                publication=None,
                errors=errors,
                warnings=warnings,
                generation_attempts=attempt,
                editor_feedback=feedback_log,
            )

        tip = publication.tip
        if not publication.created:
            warnings.append("Tip already exists; skipped creating a duplicate.")

        return TipPipelineResult(
            aggregation=aggregation,
            draft=final_draft,
            tip=tip,
            publication=publication,
            errors=errors,
            warnings=warnings,
            generation_attempts=attempt,
            editor_feedback=feedback_log,
        )

