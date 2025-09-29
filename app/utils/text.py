"""Utilities for working with content text."""
from __future__ import annotations

import re
from typing import Any


_MARKDOWN_HEADING_RE = re.compile(r"(^|\n)#{1,6}\s*")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_MARKDOWN_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_MARKDOWN_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MARKDOWN_EMPHASIS_RE = re.compile(r"([*_]{1,3})([^*_]+)\1")
_BLOCKQUOTE_RE = re.compile(r"(^|\n)>\s*")


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


__all__ = ["markdown_to_plain_text"]
