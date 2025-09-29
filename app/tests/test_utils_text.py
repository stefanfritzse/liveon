"""Tests for markdown text utility helpers."""
from __future__ import annotations

from app.utils.text import markdown_to_plain_text


def test_markdown_to_plain_text_strips_headings_and_links() -> None:
    source = "### Heading\nSummary with a [link](https://example.com) and **bold** text."
    result = markdown_to_plain_text(source)
    assert result == "Heading Summary with a link and bold text."


def test_markdown_to_plain_text_handles_non_string_values() -> None:
    assert markdown_to_plain_text(None) == ""
    assert markdown_to_plain_text(42) == ""


def test_markdown_to_plain_text_normalises_whitespace() -> None:
    source = "Line one.\n\n* Bullet item\n* Second item"
    assert markdown_to_plain_text(source) == "Line one. Bullet item Second item"
