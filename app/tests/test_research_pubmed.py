"""Tests for PubMed acquisition.

The parser is the boundary between an external format and everything the gates rely on:
the canonical key, the classification metadata, the retraction state, and the verbatim
document that spans index into. All of it is exercised against fixture XML — no network.
"""

from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path

import httpx
import pytest

from app.models.evidence import Span
from app.services.research.http import ResearchHttpClient, ResearchRequestError
from app.services.research.pubmed import (
    PubMedClient,
    build_pubmed_client,
    parse_pubmed_articles,
)

ARTICLE_XML = """<?xml version="1.0" ?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation Status="MEDLINE">
      <PMID Version="1">38412345</PMID>
      <Article PubModel="Print-Electronic">
        <Journal>
          <Title>JAMA Internal Medicine</Title>
          <JournalIssue>
            <PubDate><Year>2024</Year><Month>Mar</Month><Day>04</Day></PubDate>
          </JournalIssue>
        </Journal>
        <ArticleTitle>Time-restricted eating and <i>cardiometabolic</i> risk</ArticleTitle>
        <Abstract>
          <AbstractText Label="METHODS">We randomised 412 adults to an eight-hour window.</AbstractText>
          <AbstractText Label="RESULTS">Fasting glucose fell by 4.2 mg/dL over 12 weeks.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Nakamura</LastName><ForeName>Aiko</ForeName></Author>
          <Author><CollectiveName>The TREAT Study Group</CollectiveName></Author>
        </AuthorList>
        <PublicationTypeList>
          <PublicationType UI="D016428">Journal Article</PublicationType>
          <PublicationType UI="D016449">Randomized Controlled Trial</PublicationType>
        </PublicationTypeList>
        <ArticleDate DateType="Electronic">
          <Year>2024</Year><Month>02</Month><Day>19</Day>
        </ArticleDate>
      </Article>
      <MeshHeadingList>
        <MeshHeading><DescriptorName UI="D006801">Humans</DescriptorName></MeshHeading>
        <MeshHeading><DescriptorName UI="D005215">Fasting</DescriptorName></MeshHeading>
      </MeshHeadingList>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">38412345</ArticleId>
        <ArticleId IdType="doi">10.1001/JAMAINTERNMED.2024.1234</ArticleId>
        <ArticleId IdType="pmc">PMC10123456</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""

RETRACTED_XML = """<?xml version="1.0" ?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">31111111</PMID>
      <Article>
        <ArticleTitle>Resveratrol reverses ageing in mice</ArticleTitle>
        <Abstract><AbstractText>Mice lived 30% longer.</AbstractText></Abstract>
        <PublicationTypeList>
          <PublicationType>Journal Article</PublicationType>
        </PublicationTypeList>
      </Article>
      <MeshHeadingList>
        <MeshHeading><DescriptorName>Animals</DescriptorName></MeshHeading>
        <MeshHeading><DescriptorName>Mice</DescriptorName></MeshHeading>
      </MeshHeadingList>
      <CommentsCorrectionsList>
        <CommentsCorrections RefType="RetractionIn">
          <RefSource>Nature. 2021;590(1):1</RefSource>
        </CommentsCorrections>
      </CommentsCorrectionsList>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList><ArticleId IdType="pubmed">31111111</ArticleId></ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


def _record():
    records = parse_pubmed_articles(ARTICLE_XML)
    assert len(records) == 1
    return records[0]


# -- parsing -----------------------------------------------------------


def test_doi_becomes_the_canonical_key_with_every_identifier_as_an_alias() -> None:
    record = _record()

    assert record.source_key == "doi:10.1001/jamainternmed.2024.1234"
    assert set(record.aliases) == {
        "doi:10.1001/jamainternmed.2024.1234",
        "pmid:38412345",
        "pmcid:PMC10123456",
    }


def test_document_text_is_the_title_then_labelled_abstract_sections() -> None:
    """Its shape is fixed: every stored span indexes into exactly this string."""

    record = _record()

    assert record.document_text == (
        "Time-restricted eating and cardiometabolic risk\n\n"
        "METHODS: We randomised 412 adults to an eight-hour window.\n\n"
        "RESULTS: Fasting glucose fell by 4.2 mg/dL over 12 weeks."
    )
    assert record.document_kind == "abstract"


def test_spans_can_be_anchored_in_the_document_it_produces() -> None:
    record = _record()

    span = Span.locate(record.document_text, "412 adults")

    assert span is not None
    assert span.verify(record.document_text) is True


def test_inline_markup_in_the_title_is_flattened_not_dropped() -> None:
    assert "cardiometabolic" in _record().title


def test_metadata_the_gates_depend_on_is_carried_through() -> None:
    record = _record()

    assert record.publication_types == ["Journal Article", "Randomized Controlled Trial"]
    assert record.mesh_terms == ["Humans", "Fasting"]
    assert record.classification.design == "rct"
    assert record.classification.subject == "human"


def test_the_electronic_article_date_wins_over_the_issue_date() -> None:
    record = _record()

    assert record.source_published_at is not None
    assert record.source_published_at.year == 2024
    assert record.source_published_at.month == 2
    assert record.source_published_at.day == 19
    assert record.source_published_at.tzinfo == timezone.utc


def test_authors_include_collective_names() -> None:
    assert _record().authors == ["Aiko Nakamura", "The TREAT Study Group"]


def test_open_access_follows_the_pmc_identifier() -> None:
    assert _record().open_access is True


def test_a_record_arrives_acquired_not_approved() -> None:
    """Nothing is publishable straight out of acquisition."""

    assert _record().state == "acquired"


def test_retraction_notices_are_detected_and_explained() -> None:
    record = parse_pubmed_articles(RETRACTED_XML)[0]

    assert record.retraction_state == "retracted"
    assert record.retraction_notes == ["RetractionIn: Nature. 2021;590(1):1"]
    assert record.is_retracted is True


def test_a_mouse_study_is_classified_as_animal_evidence() -> None:
    record = parse_pubmed_articles(RETRACTED_XML)[0]

    assert record.classification.subject == "animal"
    assert record.source_key == "pmid:31111111"


@pytest.mark.parametrize(
    ("publication_type", "expected"),
    [("Meta-Analysis", "meta_analysis"), ("Systematic Review", "systematic_review"), ("Preprint", "preprint")],
)
def test_source_type_follows_the_publication_type(publication_type: str, expected: str) -> None:
    xml = ARTICLE_XML.replace(
        '<PublicationType UI="D016449">Randomized Controlled Trial</PublicationType>',
        f"<PublicationType>{publication_type}</PublicationType>",
    )

    assert parse_pubmed_articles(xml)[0].source_type == expected


def test_an_article_without_any_identifier_is_skipped_not_guessed() -> None:
    xml = """<?xml version="1.0" ?>
    <PubmedArticleSet><PubmedArticle><MedlineCitation>
      <Article><ArticleTitle>No identifiers here</ArticleTitle></Article>
    </MedlineCitation></PubmedArticle></PubmedArticleSet>"""

    assert parse_pubmed_articles(xml) == []


def test_empty_and_broken_payloads() -> None:
    assert parse_pubmed_articles("") == []
    assert parse_pubmed_articles("   ") == []
    with pytest.raises(ResearchRequestError):
        parse_pubmed_articles("<PubmedArticleSet><unclosed>")


def test_a_medline_date_without_a_month_still_yields_a_date() -> None:
    xml = ARTICLE_XML.replace(
        "<ArticleDate DateType=\"Electronic\">\n          <Year>2024</Year><Month>02</Month><Day>19</Day>\n        </ArticleDate>",
        "",
    ).replace(
        "<PubDate><Year>2024</Year><Month>Mar</Month><Day>04</Day></PubDate>",
        "<PubDate><MedlineDate>2023 Nov-Dec</MedlineDate></PubDate>",
    )

    record = parse_pubmed_articles(xml)[0]

    assert record.source_published_at is not None
    assert record.source_published_at.year == 2023


# -- client ------------------------------------------------------------


def _client(handler, tmp_path: Path) -> PubMedClient:
    http = ResearchHttpClient(
        source="pubmed",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        cache_dir=tmp_path / "cache",
        rate_per_second=0.0,
        sleep=lambda _seconds: None,
    )
    return PubMedClient(http=http, email="test@example.com")


def test_search_then_fetch_returns_records(tmp_path: Path) -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        if "esearch" in request.url.path:
            return httpx.Response(
                200, text=json.dumps({"esearchresult": {"idlist": ["38412345"]}})
            )
        return httpx.Response(200, text=ARTICLE_XML)

    records = _client(handler, tmp_path).search_records("longevity", max_results=5)

    assert [record.source_key for record in records] == ["doi:10.1001/jamainternmed.2024.1234"]
    assert "efetch" in seen[1].path


def test_the_client_identifies_itself_to_ncbi(tmp_path: Path) -> None:
    """Unidentified E-utilities traffic is the first to be throttled."""

    captured: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url)
        return httpx.Response(200, text=json.dumps({"esearchresult": {"idlist": []}}))

    _client(handler, tmp_path).search("longevity")

    query = dict(captured[0].params)
    assert query["tool"] == "liveon"
    assert query["email"] == "test@example.com"


def test_a_search_matching_nothing_returns_no_records_without_fetching(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text=json.dumps({"esearchresult": {"idlist": []}}))

    assert _client(handler, tmp_path).search_records("nothing at all") == []
    assert not any("efetch" in path for path in calls)


def test_a_date_window_is_passed_through(tmp_path: Path) -> None:
    captured: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url)
        return httpx.Response(200, text=json.dumps({"esearchresult": {"idlist": []}}))

    _client(handler, tmp_path).search("longevity", min_date="2024/01/01")

    query = dict(captured[0].params)
    assert query["datetype"] == "pdat"
    assert query["mindate"] == "2024/01/01"


def test_an_unexpected_payload_is_an_error_not_an_empty_result(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps(["unexpected"]))

    with pytest.raises(ResearchRequestError):
        _client(handler, tmp_path).search("longevity")


def test_an_api_key_raises_the_rate_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEON_NCBI_API_KEY", "secret-key")
    monkeypatch.setenv("LIVEON_NCBI_EMAIL", "ops@example.com")

    client = build_pubmed_client(cache_ttl_seconds=0.0)

    assert client.api_key == "secret-key"
    assert client.email == "ops@example.com"
    assert client._credentials()["api_key"] == "secret-key"
    client.http.close()
