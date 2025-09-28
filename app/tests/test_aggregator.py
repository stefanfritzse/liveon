from __future__ import annotations

from datetime import datetime, timezone

import feedparser

import httpx

from app.models.aggregator import AggregatedContent, FeedSource
from app.services.aggregator import LongevityNewsAggregator


SAMPLE_FEED = """<?xml version='1.0' encoding='UTF-8'?>
<rss version="2.0">
  <channel>
    <title>Longevity Research Updates</title>
    <link>https://example.com</link>
    <description>Recent news on healthy aging</description>
    <item>
      <title>Intermittent fasting study shows promising biomarkers</title>
      <link>https://example.com/articles/intermittent-fasting</link>
      <description>A new longitudinal study tracks biomarker improvements in fasting cohorts.</description>
      <pubDate>Tue, 02 Jan 2024 09:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Strength training linked to increased healthspan</title>
      <link>https://example.com/articles/strength-training</link>
      <description>Meta-analysis finds resistance training supports metabolic resilience.</description>
      <pubDate>Wed, 03 Jan 2024 08:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


URL_VARIANTS_FEED = """<?xml version='1.0' encoding='UTF-8'?>
<rss version="2.0">
  <channel>
    <title>Longevity Research Updates</title>
    <link>https://example.com</link>
    <description>Recent news on healthy aging</description>
    <item>
      <title>Peptide therapy gains traction</title>
      <link>HTTPS://EXAMPLE.COM/articles/peptide-therapy</link>
      <description>Researchers explore standard protocols.</description>
      <pubDate>Fri, 05 Jan 2024 10:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Peptide therapy gains traction</title>
      <link>https://example.com/articles/peptide-therapy/</link>
      <description>Researchers explore standard protocols.</description>
      <pubDate>Fri, 05 Jan 2024 10:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


MISSING_URL_FEED = """<?xml version='1.0' encoding='UTF-8'?>
<rss version="2.0">
  <channel>
    <title>Longevity Research Updates</title>
    <link>https://example.com</link>
    <description>Recent news on healthy aging</description>
    <item>
      <title>Cellular senescence markers fall after therapy</title>
      <description>Trial indicates sustained biomarker improvements.</description>
      <pubDate>Thu, 04 Jan 2024 11:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Cellular senescence markers fall after therapy</title>
      <description>Trial indicates sustained biomarker improvements.</description>
      <pubDate>Thu, 04 Jan 2024 11:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


def test_gather_returns_sorted_articles() -> None:
    feeds = [FeedSource(name="Longevity Research Digest", url="https://example.com/feed", topic="research")]
    aggregator = LongevityNewsAggregator(feeds, fetcher=lambda url: SAMPLE_FEED)

    result = aggregator.gather(limit_per_feed=5)

    assert not result.errors
    assert [item.title for item in result.items] == [
        "Strength training linked to increased healthspan",
        "Intermittent fasting study shows promising biomarkers",
    ]
    assert all(item.topic == "research" for item in result.items)
    assert all(isinstance(item.published_at, datetime) for item in result.items)
    assert all(item.published_at.tzinfo == timezone.utc for item in result.items)


def test_gather_handles_fetch_errors_gracefully() -> None:
    feeds = [FeedSource(name="Longevity Research Digest", url="https://example.com/feed")]

    def failing_fetcher(url: str) -> str:
        raise httpx.RequestError("Network failure", request=httpx.Request("GET", url))

    aggregator = LongevityNewsAggregator(feeds, fetcher=failing_fetcher)

    result = aggregator.gather()

    assert not result.items
    assert result.errors
    assert "Network failure" in result.errors[0]

def test_gather_deduplicates_tracking_parameters() -> None:
    feeds = [FeedSource(name="Digest", url="https://example.com/feed")]
    feed_with_tracking = """<?xml version='1.0' encoding='UTF-8'?>
    <rss version="2.0">
      <channel>
        <title>Longevity Research Updates</title>
        <item>
          <title>Strength training linked to increased healthspan</title>
          <link>https://example.com/articles/strength-training?utm_source=rss&amp;utm_medium=feed</link>
          <description>Meta-analysis finds resistance training supports metabolic resilience.</description>
          <pubDate>Wed, 03 Jan 2024 08:00:00 GMT</pubDate>
        </item>
        <item>
          <title>Strength training linked to increased healthspan</title>
          <link>https://example.com/articles/strength-training</link>
          <description>Duplicate entry that should be ignored.</description>
          <pubDate>Wed, 03 Jan 2024 08:00:00 GMT</pubDate>
        </item>
      </channel>
    </rss>
    """

    aggregator = LongevityNewsAggregator(feeds, fetcher=lambda url: feed_with_tracking)

    result = aggregator.gather(limit_per_feed=5)

    assert [item.url for item in result.items] == ["https://example.com/articles/strength-training"]


def test_gather_deduplicates_when_links_are_missing() -> None:
    feeds = [FeedSource(name="Digest", url="https://example.com/feed")]
    feed_without_links = """<?xml version='1.0' encoding='UTF-8'?>
    <rss version="2.0">
      <channel>
        <title>Longevity Research Updates</title>
        <item>
          <title>Intermittent fasting study shows promising biomarkers</title>
          <link>https://example.com/articles/intermittent-fasting</link>
          <description>Longitudinal study tracks biomarker improvements in fasting cohorts.</description>
          <pubDate>Tue, 02 Jan 2024 09:00:00 GMT</pubDate>
        </item>
        <item>
          <title>Intermittent fasting study shows promising biomarkers</title>
          <description>Duplicate entry lacking a canonical URL.</description>
          <pubDate>Tue, 02 Jan 2024 09:00:00 GMT</pubDate>
        </item>
      </channel>
    </rss>
    """

    aggregator = LongevityNewsAggregator(feeds, fetcher=lambda url: feed_without_links)

    result = aggregator.gather(limit_per_feed=5)

    assert len(result.items) == 1
    assert result.items[0].url == "https://example.com/articles/intermittent-fasting"


def test_gather_prefers_entries_with_urls_for_same_guid() -> None:
    feeds = [FeedSource(name="Digest", url="https://example.com/feed")]
    feed_with_guid = """<?xml version='1.0' encoding='UTF-8'?>
    <rss version="2.0">
      <channel>
        <title>Longevity Research Updates</title>
        <item>
          <title>Cellular rejuvenation trial expands cohort</title>
          <guid isPermaLink="false">article-123</guid>
          <description>Initial report without the canonical link.</description>
          <pubDate>Thu, 04 Jan 2024 10:00:00 GMT</pubDate>
        </item>
        <item>
          <title>Cellular rejuvenation trial expands cohort</title>
          <link>https://example.com/articles/rejuvenation-trial</link>
          <guid>article-123</guid>
          <description>Updated entry containing the canonical link.</description>
          <pubDate>Thu, 04 Jan 2024 10:00:00 GMT</pubDate>
        </item>
      </channel>
    </rss>
    """

    aggregator = LongevityNewsAggregator(feeds, fetcher=lambda url: feed_with_guid)

    result = aggregator.gather(limit_per_feed=5)

    assert len(result.items) == 1
    assert result.items[0].url == "https://example.com/articles/rejuvenation-trial"

