from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Sequence

from app.models.content import Tip
from app.models.tip import TipDraft
from app.models.tip_context import TipGenerationContext
from app.models.tip_editor import TipReviewResult
from app.services.pipeline import TipPipeline
from app.services.tip_publisher import TipPublisher


@dataclass(slots=True)
class StubContextProvider:
    context: TipGenerationContext
    calls: int = 0

    def build(self) -> TipGenerationContext:
        self.calls += 1
        return self.context


@dataclass(slots=True)
class StubTipGenerator:
    draft: TipDraft
    fail_on_attempts: set[int] = field(default_factory=set)
    calls: int = 0
    seen_published: list = field(default_factory=list)

    def generate(
        self,
        *,
        context: TipGenerationContext,
        feedback: str | None = None,
        published_tips: Sequence[object] = (),
    ) -> TipDraft:
        self.calls += 1
        self.seen_published.append(list(published_tips))
        if self.calls in self.fail_on_attempts:
            raise RuntimeError("model offline")
        return self.draft


@dataclass(slots=True)
class StubTipEditor:
    responses: Sequence[TipReviewResult]
    calls: int = 0

    def review(self, draft: TipDraft, existing_tips: Sequence[Tip]) -> TipReviewResult:
        response = self.responses[self.calls] if self.calls < len(self.responses) else self.responses[-1]
        self.calls += 1
        return response


@dataclass(slots=True)
class StubTipRepository:
    stored: list[Tip] = field(default_factory=list)
    existing_by_title: dict[str, Tip] = field(default_factory=dict)
    existing_by_tags: dict[tuple[str, ...], Tip] = field(default_factory=dict)

    def save_tip(self, tip: Tip) -> Tip:
        saved = Tip(
            title=tip.title,
            content_body=tip.content_body,
            published_date=tip.published_date,
            tags=list(tip.tags),
            id=tip.id or f"tip-{len(self.stored) + 1}",
        )
        self.stored.append(saved)
        self.existing_by_title[saved.title] = saved
        self.existing_by_tags[tuple(sorted(saved.tags))] = saved
        return saved

    def find_tip_by_title(self, title: str) -> Tip | None:
        return self.existing_by_title.get(title)

    def find_tip_by_tags(self, tags: Iterable[str]) -> Tip | None:
        normalized = tuple(sorted(tag.strip() for tag in tags if isinstance(tag, str) and tag.strip()))
        return self.existing_by_tags.get(normalized)

    def get_latest_tips(self, *, limit: int = 5) -> list[Tip]:
        combined = list(self.stored) + list(self.existing_by_title.values())
        return combined[:limit]

    def find_article_by_source_url(self, url: str) -> Tip | None:  # pragma: no cover - compatibility
        return None


def sample_context() -> TipGenerationContext:
    return TipGenerationContext(
        notes=["Encourage a brisk 10-minute walk to spark circulation."],
        sources=["https://example.com/walk"],
        guidance="Prompt a short walk before lunch to stabilise glucose.",
    )


def sample_draft() -> TipDraft:
    return TipDraft(
        title="Power Walk Pulse",
        body="Schedule a 10-minute brisk walk before lunch to stabilise glucose and sharpen focus.",
        tags=["movement", "metabolism"],
        metadata={"sources": ["https://example.com/walk"]},
    )


def test_tip_pipeline_publishes_new_tip() -> None:
    repository = StubTipRepository()
    pipeline = TipPipeline(
        context_provider=StubContextProvider(context=sample_context()),
        generator=StubTipGenerator(draft=sample_draft()),
        editor=StubTipEditor(
            responses=[TipReviewResult(is_approved=True, feedback="Looks good", revised_draft=None)]
        ),
        publisher=TipPublisher(repository),
        repository=repository,
    )

    published_at = datetime(2024, 2, 2, tzinfo=timezone.utc)
    result = pipeline.run(published_at=published_at)

    assert result.succeeded
    assert result.created
    assert result.tip is not None
    assert result.tip.title == "Power Walk Pulse"
    assert repository.stored and repository.stored[0].published_date == published_at
    assert pipeline.context_provider.calls == 1
    assert pipeline.generator.calls == 1
    assert pipeline.editor.calls == 1


def test_tip_pipeline_retries_after_rejection() -> None:
    repository = StubTipRepository()
    review_responses = [
        TipReviewResult(is_approved=False, feedback="Too generic", revised_draft=None),
        TipReviewResult(is_approved=True, feedback="Better", revised_draft=None),
    ]
    pipeline = TipPipeline(
        context_provider=StubContextProvider(context=sample_context()),
        generator=StubTipGenerator(draft=sample_draft()),
        editor=StubTipEditor(responses=review_responses),
        publisher=TipPublisher(repository),
        repository=repository,
    )

    result = pipeline.run()

    assert result.succeeded
    assert result.generation_attempts == 2
    assert any("Tip draft rejected" in warning for warning in result.warnings)
    assert "Too generic" in "\n".join(result.editor_feedback)


def test_tip_pipeline_stops_when_generator_keeps_failing() -> None:
    repository = StubTipRepository()
    generator = StubTipGenerator(draft=sample_draft(), fail_on_attempts={1, 2, 3})
    pipeline = TipPipeline(
        context_provider=StubContextProvider(context=sample_context()),
        generator=generator,
        editor=StubTipEditor(
            responses=[TipReviewResult(is_approved=True, feedback=None, revised_draft=None)]
        ),
        publisher=TipPublisher(repository),
        repository=repository,
    )

    result = pipeline.run()

    assert not result.succeeded
    assert result.tip is None
    assert result.errors
    assert any("Tip generator failed" in error for error in result.errors)
    assert repository.stored == []
