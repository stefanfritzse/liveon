"""Utilities for working with content text."""
from __future__ import annotations

import re
from html import escape as _escape
from html.parser import HTMLParser as _HTMLParser
from typing import Any

import markdown as _markdown
from markupsafe import Markup


_MARKDOWN_HEADING_RE = re.compile(r"(^|\n)#{1,6}\s*")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_MARKDOWN_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_MARKDOWN_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MARKDOWN_EMPHASIS_RE = re.compile(r"([*_]{1,3})([^*_]+)\1")
_BLOCKQUOTE_RE = re.compile(r"(^|\n)>\s*")
_MARKDOWN_EXTENSIONS: tuple[str, ...] = ("extra", "sane_lists", "smarty")


def markdown_to_plain_text(value: Any) -> str:
    """Convert basic Markdown content into a plain text snippet.

    The function intentionally handles the subset of Markdown most likely to appear
    in aggregated article summaries. The implementation removes headings, links,
    images, emphasis markers, and inline code fences while normalising whitespace.
    Non-string inputs return an empty string to keep template rendering predictable.
    """

    if not isinstance(value, str):
        return ""

    text = value
    text = _MARKDOWN_CODE_BLOCK_RE.sub(" ", text)
    text = _MARKDOWN_IMAGE_RE.sub(" ", text)
    text = _MARKDOWN_LINK_RE.sub(r"\1", text)
    text = _MARKDOWN_INLINE_CODE_RE.sub(r"\1", text)
    text = _MARKDOWN_HEADING_RE.sub(r"\1", text)
    text = _MARKDOWN_EMPHASIS_RE.sub(r"\2", text)
    text = _BLOCKQUOTE_RE.sub(r"\1", text)

    # Replace remaining Markdown list markers with whitespace so that they don't
    # appear at the start of the summary.
    text = re.sub(r"(^|\n)[\-*+]\s+", r"\1", text)
    text = re.sub(r"(^|\n)\d+\.\s+", r"\1", text)

    # Normalise whitespace to keep excerpts concise.
    text = text.replace("\r", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()

# ----------------------------------------------------------------------
# Sanitisation
# ----------------------------------------------------------------------
# Rendered content comes from two untrusted upstreams — RSS feeds and a language
# model — and python-markdown passes raw HTML straight through. Everything rendered
# to a page therefore goes through an allowlist first.

_ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "a", "abbr", "b", "blockquote", "br", "caption", "code", "dd", "del", "div",
        "dl", "dt", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "li",
        "ol", "p", "pre", "s", "small", "span", "strong", "sub", "sup", "table",
        "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
    }
)

#: Tags whose *contents* are dropped too, so script bodies do not become visible text.
_DISCARD_CONTENT_TAGS: frozenset[str] = frozenset({"script", "style", "iframe", "object", "embed"})

_VOID_TAGS: frozenset[str] = frozenset({"br", "hr", "img"})

_GLOBAL_ATTRS: frozenset[str] = frozenset({"title"})
_ALLOWED_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "rel", "target"}),
    "img": frozenset({"src", "alt", "width", "height"}),
    "td": frozenset({"colspan", "rowspan", "align"}),
    "th": frozenset({"colspan", "rowspan", "align", "scope"}),
    "ol": frozenset({"start"}),
}

_ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto", "tel"})
_URL_ATTRS: frozenset[str] = frozenset({"href", "src"})


def _is_safe_url(value: str) -> bool:
    """Reject ``javascript:`` and friends while allowing relative links."""

    candidate = (value or "").strip()
    if not candidate:
        return False
    # Strip characters browsers ignore when resolving a scheme.
    probe = "".join(candidate.split()).lower()
    if ":" not in probe.split("/")[0].split("?")[0].split("#")[0]:
        return True  # relative URL
    scheme = probe.split(":", 1)[0]
    return scheme in _ALLOWED_URL_SCHEMES


class _Sanitizer(_HTMLParser):
    """Rebuild a document from allowlisted tags and attributes only."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._suppress_depth = 0
        self._open_tags: list[str] = []

    # -- output ---------------------------------------------------------
    def result(self) -> str:
        # Close anything the source left open so the page cannot be broken by it.
        for tag in reversed(self._open_tags):
            self._parts.append(f"</{tag}>")
        self._open_tags.clear()
        return "".join(self._parts)

    def _render_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        allowed = _ALLOWED_ATTRS.get(tag, frozenset()) | _GLOBAL_ATTRS
        rendered: list[str] = []
        for name, value in attrs:
            key = (name or "").lower()
            if key.startswith("on") or key not in allowed:
                continue
            text = value or ""
            if key in _URL_ATTRS and not _is_safe_url(text):
                continue
            rendered.append(f' {key}="{_escape(text, quote=True)}"')

        if tag == "a" and any(name.lower() == "href" for name, _ in attrs):
            # Anything linked here is third-party; never hand it window.opener.
            rendered.append(' rel="nofollow noopener"')
        return "".join(rendered)

    # -- parser hooks ---------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DISCARD_CONTENT_TAGS:
            self._suppress_depth += 1
            return
        if self._suppress_depth or tag not in _ALLOWED_TAGS:
            return
        self._parts.append(f"<{tag}{self._render_attrs(tag, attrs)}>")
        if tag not in _VOID_TAGS:
            self._open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._suppress_depth or tag not in _ALLOWED_TAGS:
            return
        self._parts.append(f"<{tag}{self._render_attrs(tag, attrs)} />")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DISCARD_CONTENT_TAGS:
            self._suppress_depth = max(0, self._suppress_depth - 1)
            return
        if self._suppress_depth or tag not in _ALLOWED_TAGS or tag in _VOID_TAGS:
            return
        if tag in self._open_tags:
            # Close any tags the source left dangling inside this one.
            while self._open_tags:
                open_tag = self._open_tags.pop()
                self._parts.append(f"</{open_tag}>")
                if open_tag == tag:
                    break

    def handle_data(self, data: str) -> None:
        if not self._suppress_depth:
            self._parts.append(_escape(data, quote=False))

    def handle_comment(self, data: str) -> None:  # pragma: no cover - comments dropped
        return


def sanitize_html(value: str) -> str:
    """Return ``value`` with only allowlisted tags, attributes, and URL schemes."""

    if not value:
        return ""
    parser = _Sanitizer()
    parser.feed(value)
    parser.close()
    return parser.result()


def markdown_to_html(value: Any) -> Markup:
    """Render Markdown into sanitised HTML suitable for templates."""

    if not isinstance(value, str):
        return Markup("")

    html = _markdown.markdown(value, extensions=_MARKDOWN_EXTENSIONS, output_format="html5")
    return Markup(sanitize_html(html))


__all__ = ["markdown_to_plain_text", "markdown_to_html", "sanitize_html"]
