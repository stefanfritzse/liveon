"""Domain models for content stored in the database."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Sequence


def _default_datetime() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    """Coerce a string/date/datetime value into a timezone-aware UTC datetime."""

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _listify_strings(value: Any) -> list[str]:
    """Normalise a value into a list of non-empty strings."""

    if isinstance(value, str):
        trimmed = value.strip()
        return [trimmed] if trimmed else []

    if isinstance(value, Sequence):
        result: list[str] = []
        for item in value:
            if isinstance(item, str):
                trimmed = item.strip()
                if trimmed:
                    result.append(trimmed)
        return result

    return []


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _text_value(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _snapshot_data(snapshot: Any) -> dict[str, Any]:
    if snapshot is None:
        return {}

    to_dict = getattr(snapshot, "to_dict", None)
    if callable(to_dict):
        return to_dict() or {}

    if isinstance(snapshot, dict):
        return dict(snapshot)

    return {}


def _snapshot_id(snapshot: Any) -> str | None:
    if snapshot is None:
        return None

    identifier = getattr(snapshot, "id", None)
    if identifier is not None:
        return str(identifier)

    if isinstance(snapshot, dict):
        candidate = snapshot.get("id")
        if candidate is not None:
            return str(candidate)

    return None


@dataclass(slots=True)
class Article:
    """Representation of an article stored in the Firestore ``articles`` collection."""

    title: str
    content_body: str
    summary: str | None = None
    published_date: datetime = field(default_factory=_default_datetime)
    source_urls: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    # Provenance carried through from the evidence layer. ``evidence_keys`` are canonical
    # source keys owned by the store, not URLs a model produced, so a published claim can
    # be traced back to the document span it came from.
    evidence_bundle_id: str | None = None
    evidence_keys: list[str] = field(default_factory=list)
    evidence_grade: str | None = None
    evidence_summary: str | None = None
    #: The limitations the reviewed claims carried. Shown to readers, because a grade
    #: without its caveats is a score rather than an explanation.
    evidence_limitations: list[str] = field(default_factory=list)
    #: Set when maintenance finds something wrong with the evidence after publication —
    #: a retraction, an expression of concern. Shown to readers above the body.
    correction_notice: str | None = None
    #: Withdrawn content stays in the database but leaves the site. Deleting it would
    #: destroy the record of what was published, which is the opposite of accountable.
    withdrawn: bool = False
    id: str | None = None

    @property
    def evidence_assessed(self) -> bool:
        """Whether this article went through evidence review.

        Content published before the evidence layer existed has no bundle and is badged
        as unassessed rather than being retro-graded on no information.
        """

        return bool(self.evidence_bundle_id or self.evidence_grade)

    def to_document(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": self.title,
            "content_body": self.content_body,
            "summary": self.summary,
            "published_date": self.published_date,
            "source_urls": list(self.source_urls),
            "tags": list(self.tags),
            "evidence_bundle_id": self.evidence_bundle_id,
            "evidence_keys": list(self.evidence_keys),
            "evidence_grade": self.evidence_grade,
            "evidence_summary": self.evidence_summary,
            "evidence_limitations": list(self.evidence_limitations),
            "correction_notice": self.correction_notice,
            "withdrawn": self.withdrawn,
        }
        if self.id:
            payload["id"] = self.id
        return payload

    @classmethod
    def from_document(cls, snapshot: Any) -> "Article":
        data = _snapshot_data(snapshot)
        doc_id = _snapshot_id(snapshot) or _optional_str(data.get("id"))

        title = _text_value(data.get("title"))
        summary = _optional_str(data.get("summary"))
        content_body = _text_value(data.get("content_body") or data.get("body") or data.get("content"))
        published = _parse_datetime(data.get("published_date") or data.get("published_at")) or _default_datetime()
        source_urls = _listify_strings(data.get("source_urls") or data.get("sources"))
        tags = _listify_strings(data.get("tags"))

        return cls(
            title=title or "",
            summary=summary,
            content_body=content_body,
            published_date=published,
            source_urls=source_urls,
            tags=tags,
            evidence_bundle_id=_optional_str(data.get("evidence_bundle_id")),
            evidence_keys=_listify_strings(data.get("evidence_keys")),
            evidence_grade=_optional_str(data.get("evidence_grade")),
            evidence_summary=_optional_str(data.get("evidence_summary")),
            evidence_limitations=_listify_strings(data.get("evidence_limitations")),
            correction_notice=_optional_str(data.get("correction_notice")),
            withdrawn=bool(data.get("withdrawn")),
            id=doc_id,
        )


@dataclass(slots=True)
class Tip:
    """Representation of a short coaching tip stored in the Firestore ``tips`` collection."""

    title: str
    content_body: str
    published_date: datetime = field(default_factory=_default_datetime)
    tags: list[str] = field(default_factory=list)
    # Tips previously lost their sources entirely at this boundary: the generator context
    # carried them, the publisher did not persist them, and nothing downstream could say
    # where a tip came from.
    source_urls: list[str] = field(default_factory=list)
    evidence_bundle_id: str | None = None
    evidence_keys: list[str] = field(default_factory=list)
    evidence_grade: str | None = None
    evidence_summary: str | None = None
    evidence_limitations: list[str] = field(default_factory=list)
    #: Set when maintenance finds something wrong with the evidence after publication —
    #: a retraction, an expression of concern. Shown to readers above the body.
    correction_notice: str | None = None
    #: Withdrawn content stays in the database but leaves the site. Deleting it would
    #: destroy the record of what was published, which is the opposite of accountable.
    withdrawn: bool = False
    id: str | None = None

    @property
    def evidence_assessed(self) -> bool:
        return bool(self.evidence_bundle_id or self.evidence_grade)

    def to_document(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": self.title,
            "content_body": self.content_body,
            "published_date": self.published_date,
            "tags": list(self.tags),
            "source_urls": list(self.source_urls),
            "evidence_bundle_id": self.evidence_bundle_id,
            "evidence_keys": list(self.evidence_keys),
            "evidence_grade": self.evidence_grade,
            "evidence_summary": self.evidence_summary,
            "evidence_limitations": list(self.evidence_limitations),
            "correction_notice": self.correction_notice,
            "withdrawn": self.withdrawn,
        }
        if self.id:
            payload["id"] = self.id
        return payload

    @classmethod
    def from_document(cls, snapshot: Any) -> "Tip":
        data = _snapshot_data(snapshot)
        doc_id = _snapshot_id(snapshot) or _optional_str(data.get("id"))

        title = _text_value(data.get("title"))
        content_body = _text_value(data.get("content_body") or data.get("body") or data.get("content"))
        published = _parse_datetime(data.get("published_date") or data.get("published_at")) or _default_datetime()
        tags = _listify_strings(data.get("tags"))

        return cls(
            title=title or "",
            content_body=content_body,
            published_date=published,
            tags=tags,
            source_urls=_listify_strings(data.get("source_urls") or data.get("sources")),
            evidence_bundle_id=_optional_str(data.get("evidence_bundle_id")),
            evidence_keys=_listify_strings(data.get("evidence_keys")),
            evidence_grade=_optional_str(data.get("evidence_grade")),
            evidence_summary=_optional_str(data.get("evidence_summary")),
            evidence_limitations=_listify_strings(data.get("evidence_limitations")),
            correction_notice=_optional_str(data.get("correction_notice")),
            withdrawn=bool(data.get("withdrawn")),
            id=doc_id,
        )


ContentItem = Article | Tip


@dataclass(slots=True)
class ContentPage:
    """One page of browsable content, plus what the filter UI needs to render."""

    items: list[Any]
    total: int
    page: int
    per_page: int
    available_tags: list[str] = field(default_factory=list)

    @property
    def total_pages(self) -> int:
        if self.per_page <= 0:
            return 1
        return max(1, -(-self.total // self.per_page))

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages

    @property
    def first_index(self) -> int:
        """1-based index of the first item on this page (0 when empty)."""

        return 0 if not self.items else (self.page - 1) * self.per_page + 1

    @property
    def last_index(self) -> int:
        return 0 if not self.items else self.first_index + len(self.items) - 1
