"""Evidence handles: how writers cite without being able to invent.

The editor agent used to be handed URLs, and it routinely returned plausible-looking ones
that did not exist — invented publisher pages, mangled copies of a real link. The existing
defence allowlists model-supplied URLs against the feed
(:func:`app.models.editor.allowlisted_sources`). This module generalises that from URLs to
evidence keys and adds the part that makes invention structurally impossible: writers see
opaque handles (``[E1]``, ``[E2]``) whose mapping to real sources lives in application
code. A handle nobody issued resolves to nothing, and citations are rendered from stored
records rather than from anything a model wrote.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Iterable, Mapping, Sequence

__all__ = [
    "EvidenceHandles",
    "allowlisted_evidence",
    "citation_url",
    "handle_pattern",
    "rejected_evidence",
    "strip_handles",
]

_HANDLE_RE = re.compile(r"\[E(\d+)\]")


def handle_pattern() -> re.Pattern[str]:
    """The handle syntax, exposed so prompts and tests cannot drift apart."""

    return _HANDLE_RE


@dataclass(slots=True)
class EvidenceHandles:
    """A run-scoped mapping between opaque handles and canonical source keys."""

    by_handle: dict[str, str]

    @classmethod
    def for_keys(cls, source_keys: Iterable[str]) -> "EvidenceHandles":
        """Issue ``[E1]…[En]`` for ``source_keys``, preserving order and dropping blanks."""

        cleaned = (key.strip() for key in source_keys if isinstance(key, str))
        ordered = [key for key in dict.fromkeys(cleaned) if key]
        return cls(by_handle={f"E{index}": key for index, key in enumerate(ordered, start=1)})

    @property
    def source_keys(self) -> list[str]:
        return list(self.by_handle.values())

    def handle_for(self, source_key: str) -> str | None:
        for handle, key in self.by_handle.items():
            if key == source_key:
                return handle
        return None

    def resolve(self, handle: str) -> str | None:
        """Return the source key for ``E3`` or ``[E3]``, or ``None`` if never issued."""

        cleaned = (handle or "").strip().strip("[]")
        return self.by_handle.get(cleaned)

    def found_in(self, text: str) -> list[str]:
        """Every handle cited in ``text``, in order of first appearance."""

        seen: list[str] = []
        for match in _HANDLE_RE.finditer(text or ""):
            handle = f"E{match.group(1)}"
            if handle not in seen:
                seen.append(handle)
        return seen

    def resolve_all(self, text: str) -> tuple[list[str], list[str]]:
        """Split the handles cited in ``text`` into ``(source_keys, unknown_handles)``."""

        resolved: list[str] = []
        unknown: list[str] = []
        for handle in self.found_in(text):
            key = self.by_handle.get(handle)
            if key is None:
                unknown.append(handle)
            elif key not in resolved:
                resolved.append(key)
        return resolved, unknown

    def prompt_block(self, titles: Mapping[str, str] | None = None) -> str:
        """Render the handle list for a prompt, with titles but never URLs.

        Withholding the URL is the point: a model that never sees one cannot echo a
        mangled version of it into the body.
        """

        lines = []
        for handle, key in self.by_handle.items():
            title = (titles or {}).get(key, "")
            lines.append(f"[{handle}] {title}".rstrip())
        return "\n".join(lines) if lines else "No evidence available."


def _identity(value: str) -> str:
    return (value or "").strip()


def allowlisted_evidence(
    allowed_keys: Sequence[str],
    model_keys: Sequence[str],
    *,
    key: Callable[[str], str] = _identity,
) -> list[str]:
    """Return only the identifiers that were actually issued for this run.

    Provenance is identity, not prose, so the model gets no say in it: anything it
    supplies is kept only when it matches an issued identifier, and the issued spelling
    always wins. ``key`` normalises before comparison — the URL caller in
    :mod:`app.models.editor` uses it to ignore a trailing slash.
    """

    allowed = {key(value) for value in allowed_keys if (value or "").strip()}
    seen: set[str] = set()
    kept: list[str] = []
    for value in list(allowed_keys) + list(model_keys):
        cleaned = (value or "").strip()
        normalised = key(cleaned)
        if not cleaned or normalised not in allowed or normalised in seen:
            continue
        seen.add(normalised)
        kept.append(cleaned)
    return kept


def rejected_evidence(
    allowed_keys: Sequence[str],
    model_keys: Sequence[str],
    *,
    key: Callable[[str], str] = _identity,
) -> list[str]:
    """Return the model-supplied identifiers dropped by :func:`allowlisted_evidence`."""

    allowed = {key(value) for value in allowed_keys if (value or "").strip()}
    seen: set[str] = set()
    dropped: list[str] = []
    for value in model_keys:
        cleaned = (value or "").strip()
        normalised = key(cleaned)
        if not cleaned or normalised in allowed or normalised in seen:
            continue
        seen.add(normalised)
        dropped.append(cleaned)
    return dropped


def citation_url(source_key: str) -> str:
    """Build the public URL for a source key, in code.

    Item 4 of improvements.md: the model never writes a URL, a DOI, or a journal name into
    a body. Links are derived from the canonical identifier at render time, so a citation
    is only ever as wrong as the identifier the store holds — and that identifier came
    from the API, not from a sentence.
    """

    from app.models.evidence import parse_source_key

    try:
        scheme, value = parse_source_key(source_key)
    except ValueError:
        return ""

    if scheme == "doi":
        return f"https://doi.org/{value}"
    if scheme == "pmid":
        return f"https://pubmed.ncbi.nlm.nih.gov/{value}/"
    if scheme == "pmcid":
        return f"https://www.ncbi.nlm.nih.gov/pmc/articles/{value}/"
    if scheme == "nct":
        return f"https://clinicaltrials.gov/study/{value}"
    if scheme == "url":
        return value
    return ""


#: Runs of spaces or tabs, but never a newline: removing a handle must not reflow a body.
_RUN_OF_SPACES = re.compile(r"[^\S\n]{2,}")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"[^\S\n]+([.,;:!?])")


def strip_handles(text: str) -> str:
    """Remove ``[E1]`` markers from prose destined for a reader.

    Handles are a machine device for tying a sentence to a source. The publisher
    resolves them into a citation list; the marker itself does not belong in the body.
    """

    cleaned = _HANDLE_RE.sub("", text or "")
    # Removal leaves double spaces, and spaces stranded before punctuation. Paragraph
    # breaks survive: this tidies a line, it does not reflow a body.
    cleaned = _RUN_OF_SPACES.sub(" ", cleaned)
    cleaned = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", cleaned)
    return cleaned.strip()
