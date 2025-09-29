from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.models.aggregator import AggregatedContent, FeedSource
from app.models.content import Article
from app.models.editor import EditedArticle
from app.models.publisher import PublicationResult
from app.models.summarizer import ArticleDraft
from app.services.aggregator import AggregationResult
from app.services.pipeline import ContentPipeline


@dataclass(slots=True)
class StubAggregator:
    items: list[AggregatedContent]
    errors: list[str] | None = None
    called_with: dict[str, int] = field(default_factory=dict, init=False)

    def gather(self, *, limit_per_feed: int = 5) -> AggregationResult:
        self.called_with["limit_per_feed"] = limit_per_feed
        return AggregationResult(items=list(self.items), errors=list(self.errors or []))


@dataclass(slots=True)
class StubSummarizer:
    draft: ArticleDraft
    calls: list[list[AggregatedContent]] = field(default_factory=list, init=False)

    def summarize(self, items: list[AggregatedContent]) -> ArticleDraft:
        self.calls.append(list(items))
        return self.draft


@dataclass(slots=True)
class StubEditor:
    result: EditedArticle
    calls: list[ArticleDraft] = field(default_factory=list, init=False)

    def revise(self, draft: ArticleDraft) -> EditedArticle:
        self.calls.append(draft)
        return self.result


class StubPublisher:
    def __init__(self, base_path: Path) -> None:
        self.base_path = base_path
        self.calls: list[EditedArticle] = []
        self.kwargs: list[dict[str, object]] = []

    def publish(
        self,
        article: EditedArticle,
        *,
        slug: str | None = None,
        commit_message: str | None = None,
        published_at: datetime | None = None,
    ) -> PublicationResult:
        self.calls.append(article)
        self.kwargs.append(
            {
                "slug": slug,
                "commit_message": commit_message,
                "published_at": published_at,
            }
        )
        final_slug = slug or "generated-slug"
        path = self.base_path / f"{final_slug}.md"
        return PublicationResult(
            slug=final_slug,
            path=path,
            commit_hash="abc123",
            published_at=(published_at or datetime.now(timezone.utc)),
        )


@dataclass(slots=True)
class StubRepository:
    existing_urls: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list, init=False)

    def find_article_by_source_url(self, url: str) -> Article | None:
        self.calls.append(url)
        if url in self.existing_urls:
            return Article(
                title="Existing",
                content_body="Stored",
                summary="",
                source_urls=[url],
                id="existing-article",
            )
        return None


def sample_aggregated() -> list[AggregatedContent]:
    feed = FeedSource(name="Longevity Digest", url="https://example.com/rss", topic="research")
    return [
        AggregatedContent(
            title="Intermittent fasting study shows promising biomarkers",
            url="https://example.com/articles/intermittent-fasting",
            summary="Longitudinal study tracks biomarker improvements in fasting cohorts.",
            published_at=datetime(2024, 1, 2, 9, tzinfo=timezone.utc),
            source="Example News",
            topic=feed.topic,
            raw={},
        )
    ]


def test_pipeline_runs_all_agents(tmp_path: Path) -> None:
    aggregator = StubAggregator(items=sample_aggregated(), errors=["Feed temporarily unavailable"])
    draft = ArticleDraft(
        title="Longevity Highlights",
        summary="Updates from recent research",
        body="## Body",
        takeaways=["Stay active"],
        sources=["https://example.com/articles/intermittent-fasting"],
        tags=["exercise"],
    )
    summarizer = StubSummarizer(draft=draft)
    edited = EditedArticle.from_draft(draft)
    editor = StubEditor(result=edited)
    publisher = StubPublisher(tmp_path)
    repository = StubRepository()

    pipeline = ContentPipeline(
        aggregator=aggregator,
        summarizer=summarizer,
        editor=editor,
        publisher=publisher,
        repository=repository,
    )

    published_at = datetime(2024, 1, 5, tzinfo=timezone.utc)
    result = pipeline.run(limit_per_feed=3, slug="custom-slug", commit_message="Test article", published_at=published_at)

    assert result.succeeded
    assert result.warnings == ["Feed temporarily unavailable"]
    assert result.aggregation.items
    assert result.draft == draft
    assert result.edited == edited
    assert result.publication is not None
    assert result.publication.slug == "custom-slug"
    assert result.publication.path == tmp_path / "custom-slug.md"
    assert result.publication.published_at == published_at
    assert aggregator.called_with["limit_per_feed"] == 3
    assert summarizer.calls and summarizer.calls[0][0].title.startswith("Intermittent")
    assert editor.calls == [draft]
    assert publisher.calls == [edited]
    assert publisher.kwargs[0]["commit_message"] == "Test article"


def test_pipeline_handles_missing_content(tmp_path: Path) -> None:
    aggregator = StubAggregator(items=[], errors=["Feed unavailable"])
    summarizer = StubSummarizer(draft=ArticleDraft(title="", summary="", body=""))
    editor = StubEditor(result=EditedArticle.from_draft(summarizer.draft))
    publisher = StubPublisher(tmp_path)
    repository = StubRepository()

    pipeline = ContentPipeline(
        aggregator=aggregator,
        summarizer=summarizer,
        editor=editor,
        publisher=publisher,
        repository=repository,
    )

    result = pipeline.run()

    assert not result.succeeded
    assert result.errors == []
    assert result.warnings == ["Feed unavailable", "No aggregated content available to summarise."]
    assert result.draft is None
    assert result.edited is None
    assert result.publication is None
    assert not summarizer.calls
    assert not editor.calls
    assert not publisher.calls


def test_pipeline_surfaces_agent_failures(tmp_path: Path) -> None:
    class FailingSummarizer(StubSummarizer):
        def summarize(self, items: list[AggregatedContent]) -> ArticleDraft:  # type: ignore[override]
            raise RuntimeError("model offline")

    aggregator = StubAggregator(items=sample_aggregated())
    summarizer = FailingSummarizer(draft=ArticleDraft(title="", summary="", body=""))
    editor = StubEditor(result=EditedArticle.from_draft(summarizer.draft))
    publisher = StubPublisher(tmp_path)
    repository = StubRepository()

    pipeline = ContentPipeline(
        aggregator=aggregator,
        summarizer=summarizer,
        editor=editor,
        publisher=publisher,
        repository=repository,
    )

    result = pipeline.run()

    assert not result.succeeded
    assert result.errors and "Summarizer failed: model offline" in result.errors[0]
    assert result.draft is None
    assert result.edited is None
    assert result.publication is None


def test_pipeline_skips_when_all_sources_exist(tmp_path: Path) -> None:
    items = sample_aggregated()
    aggregator = StubAggregator(items=items)
    draft = ArticleDraft(title="", summary="", body="")
    summarizer = StubSummarizer(draft=draft)
    editor = StubEditor(result=EditedArticle.from_draft(draft))
    publisher = StubPublisher(tmp_path)
    repository = StubRepository(existing_urls={items[0].url})

    pipeline = ContentPipeline(
        aggregator=aggregator,
        summarizer=summarizer,
        editor=editor,
        publisher=publisher,
        repository=repository,
    )

    result = pipeline.run()

    assert not result.succeeded
    assert result.errors == []
    assert "No new aggregated content available to publish." in result.warnings
    assert not summarizer.calls
    assert not editor.calls
    assert not publisher.calls
    assert repository.calls == [items[0].url]


def test_pipeline_publishes_only_first_fresh_item(tmp_path: Path) -> None:
    first, second = sample_aggregated()[0], sample_aggregated()[0]
    second = AggregatedContent(
        title="Cellular rejuvenation trial advances",
        url="https://example.com/articles/cellular-rejuvenation",
        summary="New trial results show rejuvenation markers.",
        published_at=datetime(2024, 1, 3, 10, tzinfo=timezone.utc),
        source="Longevity Times",
        topic="research",
        raw={},
    )
    aggregator = StubAggregator(items=[first, second])
    draft = ArticleDraft(title="Fresh Longevity", summary="Summary", body="Body")
    summarizer = StubSummarizer(draft=draft)
    edited = EditedArticle.from_draft(draft)
    editor = StubEditor(result=edited)
    publisher = StubPublisher(tmp_path)
    repository = StubRepository(existing_urls={first.url})

    pipeline = ContentPipeline(
        aggregator=aggregator,
        summarizer=summarizer,
        editor=editor,
        publisher=publisher,
        repository=repository,
    )

    result = pipeline.run()

    assert result.succeeded
    assert summarizer.calls == [[second]]
    assert editor.calls == [draft]
    assert publisher.calls == [edited]
    assert publisher.kwargs[0]["slug"] == "cellular-rejuvenation-trial-advances"
    assert repository.calls == [first.url, second.url]

