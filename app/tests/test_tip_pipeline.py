from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Sequence

from app.models.aggregator import AggregatedContent, FeedSource
from app.models.content import Tip
from app.models.tip import TipDraft
from app.services.aggregator import AggregationResult
from app.services.pipeline import TipPipeline
from app.services.tip_publisher import TipPublisher


@dataclass(slots=True)
class StubAggregator:
    items: list[AggregatedContent]
    errors: list[str] | None = None
    called_with: dict[str, int] = field(default_factory=dict, init=False)

    def gather(self, *, limit_per_feed: int = 5) -> AggregationResult:
        self.called_with["limit_per_feed"] = limit_per_feed
        return AggregationResult(items=list(self.items), errors=list(self.errors or []))


@dataclass(slots=True)
class StubTipGenerator:
    draft: TipDraft
    calls: list[list[AggregatedContent]] = field(default_factory=list, init=False)

    def generate(self, items: Sequence[AggregatedContent]) -> TipDraft:
        self.calls.append(list(items))
        return self.draft


@dataclass(slots=True)
class StubTipRepository:
    stored: list[Tip] = field(default_factory=list)
    existing_title: dict[str, Tip] = field(default_factory=dict)
    existing_tags: dict[tuple[str, ...], Tip] = field(default_factory=dict)
    find_title_calls: list[str] = field(default_factory=list, init=False)
    find_tags_calls: list[tuple[str, ...]] = field(default_factory=list, init=False)

    def save_tip(self, tip: Tip) -> Tip:
        stored_tip = Tip(
            title=tip.title,
            content_body=tip.content_body,
            published_date=tip.published_date,
            tags=list(tip.tags),
            id=tip.id or f"tip-{len(self.stored) + 1}",
        )
        self.stored.append(stored_tip)
        self.existing_title[stored_tip.title] = stored_tip
        self.existing_tags[tuple(sorted(stored_tip.tags))] = stored_tip
        return stored_tip

    def find_tip_by_title(self, title: str) -> Tip | None:
        self.find_title_calls.append(title)
        return self.existing_title.get(title)

    def find_tip_by_tags(self, tags: Iterable[str]) -> Tip | None:
        tag_list = sorted(tag.strip() for tag in tags if isinstance(tag, str) and tag.strip())
        self.find_tags_calls.append(tuple(tag_list))
        return self.existing_tags.get(tuple(tag_list))


def sample_aggregated() -> list[AggregatedContent]:
    feed = FeedSource(name="Daily Longevity", url="https://example.com/rss", topic="tips")
    return [
        AggregatedContent(
            title="Move a little more", 
            url="https://example.com/articles/move-more",
            summary="Light movement supports cardiovascular health.",
            published_at=datetime(2024, 2, 1, 8, tzinfo=timezone.utc),
            source="Example News",
            topic=feed.topic,
            raw={},
        )
    ]


def test_tip_pipeline_publishes_new_tip() -> None:
    aggregator = StubAggregator(items=sample_aggregated(), errors=["Feed timeout"])
    draft = TipDraft(title="Daily Movement", body="Take a brisk 10-minute walk today.", tags=["movement", "cardio"])
    generator = StubTipGenerator(draft=draft)
    repository = StubTipRepository()
    publisher = TipPublisher(repository)

    pipeline = TipPipeline(aggregator=aggregator, generator=generator, publisher=publisher)

    published_at = datetime(2024, 2, 2, tzinfo=timezone.utc)
    result = pipeline.run(limit_per_feed=4, published_at=published_at)

    assert result.succeeded
    assert result.created
    assert result.tip is not None
    assert result.tip.title == "Daily Movement"
    assert result.tip.content_body.startswith("Take a brisk")
    assert result.tip.tags == ["movement", "cardio"]
    assert result.publication is not None
    assert result.publication.created
    assert result.warnings == ["Feed timeout"]
    assert aggregator.called_with["limit_per_feed"] == 4
    assert generator.calls and generator.calls[0][0].title == "Move a little more"
    assert len(repository.stored) == 1


def test_tip_pipeline_suppresses_duplicates() -> None:
    existing_tip = Tip(
        title="Daily Movement",
        content_body="Take a brisk 10-minute walk today.",
        tags=["movement", "cardio"],
        published_date=datetime(2024, 1, 30, tzinfo=timezone.utc),
        id="existing-tip",
    )

    repository = StubTipRepository(
        existing_title={existing_tip.title: existing_tip},
        existing_tags={tuple(sorted(existing_tip.tags)): existing_tip},
    )

    aggregator = StubAggregator(items=sample_aggregated())
    draft = TipDraft(title="Daily Movement", body="Take a brisk 10-minute walk today.", tags=["movement", "cardio"])
    generator = StubTipGenerator(draft=draft)
    publisher = TipPublisher(repository)
    pipeline = TipPipeline(aggregator=aggregator, generator=generator, publisher=publisher)

    result = pipeline.run()

    assert result.succeeded
    assert not result.created
    assert result.tip == existing_tip
    assert result.publication is not None and not result.publication.created
    assert "Tip already exists" in result.warnings[0]
    assert len(repository.stored) == 0
    assert repository.find_title_calls == ["Daily Movement"]
    if repository.find_tags_calls:
        assert repository.find_tags_calls == [tuple(sorted(existing_tip.tags))]


def test_tip_pipeline_handles_generator_failure() -> None:
    class FailingGenerator(StubTipGenerator):
        def generate(self, items: Sequence[AggregatedContent]) -> TipDraft:  # type: ignore[override]
            raise RuntimeError("model offline")

    aggregator = StubAggregator(items=sample_aggregated())
    generator = FailingGenerator(draft=TipDraft(title="", body="", tags=[]))
    repository = StubTipRepository()
    publisher = TipPublisher(repository)
    pipeline = TipPipeline(aggregator=aggregator, generator=generator, publisher=publisher)

    result = pipeline.run()

    assert not result.succeeded
    assert result.errors and "Tip generator failed: model offline" in result.errors[0]
    assert result.draft is None
    assert result.tip is None
    assert result.publication is None
    assert repository.stored == []
