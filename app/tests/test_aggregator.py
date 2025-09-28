from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.models.aggregator import FeedSource
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

SAMPLE_FEED_WITH_TRACKING = """<?xml version='1.0' encoding='UTF-8'?>
<rss version="2.0">
  <channel>
    <title>Longevity Research Updates</title>
    <link>https://example.com</link>
    <description>Recent news on healthy aging</description>
    <item>
      <title>Metabolic panel biomarkers improve</title>
      <link>https://example.com/articles/longevity-biomarkers/?utm_source=newsletter&amp;utm_medium=email</link>
      <description>Tracking parameters should not create duplicate entries.</description>
      <pubDate>Thu, 04 Jan 2024 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Metabolic panel biomarkers improve (duplicate)</title>
      <link>https://EXAMPLE.com/articles/longevity-biomarkers/?utm_source=social&amp;fbclid=tracking</link>
      <description>Same story syndicated with altered parameters.</description>
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
    feeds = [FeedSource(name="Longevity Research Digest", url="https://example.com/feed", topic="research")]
    aggregator = LongevityNewsAggregator(feeds, fetcher=lambda url: SAMPLE_FEED_WITH_TRACKING)

    result = aggregator.gather(limit_per_feed=10)

    assert len(result.items) == 1
    assert result.items[0].url == "https://example.com/articles/longevity-biomarkers"
    assert result.items[0].title == "Metabolic panel biomarkers improve"
