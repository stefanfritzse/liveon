"""PubMed acquisition through the NCBI E-utilities.

This is the primary discovery path. Querying the literature directly gives structured
search over metadata indexed by people — publication types, MeSH descriptors, retraction
links — which is precisely what the gates need and precisely what a news article cannot
supply. News is a topicality signal elsewhere; it never becomes a record.

``document_text`` is assembled here, once, and every span in the pipeline indexes into it.
Its construction is deliberately boring and deterministic — the title, a blank line, then
each labelled abstract section — because changing it later would invalidate every stored
span pointing into it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
from typing import Any, Iterable, Sequence
from xml.etree import ElementTree

from app.models.evidence import EvidenceRecord, make_source_key
from app.services.evidence.classification import classify
from app.services.research.http import ResearchHttpClient, ResearchRequestError

LOGGER = logging.getLogger(__name__)

__all__ = ["PubMedClient", "build_pubmed_client", "parse_pubmed_articles"]

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

#: E-utilities asks callers to identify themselves; unidentified traffic gets throttled first.
_TOOL_NAME = "liveon"

#: How many PMIDs to request per efetch call. NCBI suggests keeping batches modest.
_FETCH_BATCH = 50

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

#: RefType values that change whether a paper may be cited at all (G6).
_RETRACTION_REFTYPES = {
    "retractionin": "retracted",
    "expressionofconcernin": "concern",
    "erratumin": "corrected",
}


@dataclass(slots=True)
class PubMedClient:
    """Search and fetch PubMed records as :class:`EvidenceRecord` instances."""

    http: ResearchHttpClient
    api_key: str | None = None
    email: str | None = None

    def search(
        self,
        query: str,
        *,
        max_results: int = 20,
        min_date: str | None = None,
        max_date: str | None = None,
    ) -> list[str]:
        """Return PMIDs matching ``query``, newest first."""

        params: dict[str, Any] = {
            "db": "pubmed",
            "term": query,
            "retmode": "json",
            "retmax": max(1, max_results),
            "sort": "date",
            **self._credentials(),
        }
        if min_date or max_date:
            params["datetype"] = "pdat"
            params["mindate"] = min_date or "1900/01/01"
            params["maxdate"] = max_date or datetime.now(timezone.utc).strftime("%Y/%m/%d")

        payload = self.http.get_json(f"{EUTILS_BASE}/esearch.fcgi", params)
        if not isinstance(payload, dict):
            raise ResearchRequestError("PubMed esearch returned an unexpected payload")

        result = payload.get("esearchresult")
        if not isinstance(result, dict):
            raise ResearchRequestError("PubMed esearch returned no result block")

        ids = result.get("idlist")
        return [str(item).strip() for item in ids or [] if str(item).strip()]

    def fetch(self, pmids: Sequence[str]) -> list[EvidenceRecord]:
        """Fetch full records for ``pmids``, in batches."""

        wanted = [str(pmid).strip() for pmid in pmids if str(pmid).strip()]
        records: list[EvidenceRecord] = []

        for start in range(0, len(wanted), _FETCH_BATCH):
            batch = wanted[start : start + _FETCH_BATCH]
            xml_text = self.http.get_text(
                f"{EUTILS_BASE}/efetch.fcgi",
                {
                    "db": "pubmed",
                    "id": ",".join(batch),
                    "retmode": "xml",
                    **self._credentials(),
                },
            )
            records.extend(parse_pubmed_articles(xml_text))

        return records

    def search_records(
        self,
        query: str,
        *,
        max_results: int = 20,
        min_date: str | None = None,
        max_date: str | None = None,
    ) -> list[EvidenceRecord]:
        """Search, then fetch. Returns an empty list when the search matches nothing."""

        pmids = self.search(
            query, max_results=max_results, min_date=min_date, max_date=max_date
        )
        if not pmids:
            LOGGER.info(
                "PubMed query matched nothing",
                extra={"event": "research.pubmed.empty", "query": query},
            )
            return []
        return self.fetch(pmids)

    def _credentials(self) -> dict[str, str]:
        params = {"tool": _TOOL_NAME}
        if self.email:
            params["email"] = self.email
        if self.api_key:
            params["api_key"] = self.api_key
        return params


def build_pubmed_client(**overrides: Any) -> PubMedClient:
    """Build a client configured from the environment.

    An API key raises the NCBI ceiling from three requests a second to ten, so the rate
    the shared limiter enforces depends on whether one is configured.
    """

    api_key = (os.getenv("LIVEON_NCBI_API_KEY") or "").strip() or None
    email = (os.getenv("LIVEON_NCBI_EMAIL") or "").strip() or None
    rate = 9.0 if api_key else 2.5

    http = overrides.pop("http", None) or ResearchHttpClient(
        source="pubmed", rate_per_second=rate, **overrides
    )
    return PubMedClient(http=http, api_key=api_key, email=email)


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------


def parse_pubmed_articles(xml_text: str) -> list[EvidenceRecord]:
    """Parse an efetch response into records. Malformed entries are skipped, not guessed."""

    if not (xml_text or "").strip():
        return []

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ResearchRequestError(f"PubMed returned unparseable XML: {exc}") from exc

    records: list[EvidenceRecord] = []
    for article in root.iter("PubmedArticle"):
        record = _parse_article(article)
        if record is not None:
            records.append(record)
    return records


def _parse_article(article: ElementTree.Element) -> EvidenceRecord | None:
    citation = article.find("MedlineCitation")
    if citation is None:
        return None

    pmid = _text(citation.find("PMID"))
    ids = _article_ids(article)
    doi = ids.get("doi", "")
    pmcid = ids.get("pmc", "")

    if not pmid and not doi:
        # Without an identifier there is nothing to key, dedupe, or cite by.
        return None

    title = _text(citation.find(".//Article/ArticleTitle"))
    abstract_sections = _abstract_sections(citation)
    document_text = _build_document_text(title, abstract_sections)

    publication_types = [
        _text(node)
        for node in citation.findall(".//Article/PublicationTypeList/PublicationType")
        if _text(node)
    ]
    mesh_terms = [
        _text(node)
        for node in citation.findall(".//MeshHeadingList/MeshHeading/DescriptorName")
        if _text(node)
    ]

    retraction_state, retraction_notes = _retraction(citation, publication_types)
    source_type = _source_type(publication_types)

    aliases: list[str] = []
    for scheme, value in (("pmid", pmid), ("doi", doi), ("pmcid", pmcid)):
        if value:
            try:
                aliases.append(make_source_key(scheme, value))
            except ValueError:
                continue

    # DOI is the identifier other sources also carry, so it makes the better canonical key.
    source_key = aliases[0] if aliases else ""
    for alias in aliases:
        if alias.startswith("doi:"):
            source_key = alias
            break
    if not source_key:
        return None

    record = EvidenceRecord(
        source_key=source_key,
        source_type=source_type,
        title=title,
        aliases=aliases,
        authors=_authors(citation),
        journal=_text(citation.find(".//Article/Journal/Title")),
        source_published_at=_published_at(citation),
        retrieved_at=datetime.now(timezone.utc),
        document_text=document_text,
        document_kind="abstract" if abstract_sections else "record",
        open_access=bool(pmcid),
        publication_types=publication_types,
        mesh_terms=mesh_terms,
        retraction_state=retraction_state,
        retraction_notes=retraction_notes,
        state="acquired",
    )
    record.classification = classify(
        publication_types=publication_types,
        mesh_terms=mesh_terms,
        source_type=source_type,
    )
    return record


def _build_document_text(title: str, sections: Sequence[tuple[str, str]]) -> str:
    """Assemble the verbatim document every span indexes into.

    Format, fixed: the title, a blank line, then each abstract section as
    ``LABEL: text`` (label omitted when PubMed supplies none), separated by blank lines.
    """

    blocks: list[str] = []
    if title:
        blocks.append(title)
    for label, text in sections:
        if not text:
            continue
        blocks.append(f"{label}: {text}" if label else text)
    return "\n\n".join(blocks)


def _abstract_sections(citation: ElementTree.Element) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    for node in citation.findall(".//Article/Abstract/AbstractText"):
        label = (node.get("Label") or node.get("NlmCategory") or "").strip()
        text = _text(node)
        if text:
            sections.append((label, text))
    return sections


def _article_ids(article: ElementTree.Element) -> dict[str, str]:
    ids: dict[str, str] = {}
    for node in article.findall(".//PubmedData/ArticleIdList/ArticleId"):
        id_type = (node.get("IdType") or "").strip().lower()
        value = _text(node)
        if id_type and value:
            ids.setdefault(id_type, value)
    return ids


def _authors(citation: ElementTree.Element) -> list[str]:
    authors: list[str] = []
    for node in citation.findall(".//Article/AuthorList/Author"):
        last = _text(node.find("LastName"))
        fore = _text(node.find("ForeName"))
        collective = _text(node.find("CollectiveName"))
        if collective:
            authors.append(collective)
        elif last:
            authors.append(f"{fore} {last}".strip())
    return authors


def _published_at(citation: ElementTree.Element) -> datetime | None:
    """Prefer the electronic article date; fall back to the journal issue date."""

    for path in (".//Article/ArticleDate", ".//Article/Journal/JournalIssue/PubDate"):
        node = citation.find(path)
        if node is None:
            continue
        moment = _date_from(node)
        if moment is not None:
            return moment
    return None


def _date_from(node: ElementTree.Element) -> datetime | None:
    year_text = _text(node.find("Year"))
    if not year_text:
        # PubDate sometimes carries a free-text MedlineDate such as "2023 Nov-Dec".
        medline = _text(node.find("MedlineDate"))
        year_text = medline.split(" ")[0] if medline else ""
    try:
        year = int(year_text[:4])
    except (TypeError, ValueError):
        return None

    month_text = _text(node.find("Month"))
    month = _MONTHS.get(month_text[:3].lower(), 0)
    if not month:
        try:
            month = int(month_text)
        except (TypeError, ValueError):
            month = 1
    month = min(max(month, 1), 12)

    try:
        day = int(_text(node.find("Day")) or 1)
    except ValueError:
        day = 1
    day = min(max(day, 1), 28) if month == 2 else min(max(day, 1), 31)

    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return datetime(year, month, 1, tzinfo=timezone.utc)


def _retraction(
    citation: ElementTree.Element, publication_types: Iterable[str]
) -> tuple[str, list[str]]:
    """Return the retraction state and the notes that decided it.

    Order matters: a paper both corrected and retracted is retracted.
    """

    notes: list[str] = []
    states: set[str] = set()

    if any(item.strip().lower() == "retracted publication" for item in publication_types):
        states.add("retracted")
        notes.append("Publication type: Retracted Publication")

    for node in citation.findall(".//CommentsCorrectionsList/CommentsCorrections"):
        ref_type = (node.get("RefType") or "").strip().lower()
        state = _RETRACTION_REFTYPES.get(ref_type)
        if not state:
            continue
        states.add(state)
        source = _text(node.find("RefSource"))
        notes.append(f"{node.get('RefType')}: {source}" if source else str(node.get("RefType")))

    for candidate in ("retracted", "concern", "corrected"):
        if candidate in states:
            return candidate, notes
    return "none", notes


def _source_type(publication_types: Iterable[str]) -> str:
    lowered = {item.strip().lower() for item in publication_types}
    if "preprint" in lowered:
        return "preprint"
    if "meta-analysis" in lowered:
        return "meta_analysis"
    if "systematic review" in lowered:
        return "systematic_review"
    return "journal_article"


def _text(node: ElementTree.Element | None) -> str:
    """Return an element's full text, including any inline markup children."""

    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())
