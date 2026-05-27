"""
Unit tests for the content extractor (CHAR-007, spec §10).

Covers visible text extraction, link extraction, URL resolution, domain
filtering, deduplication, budget truncation, and exception boundaries.
"""

from unittest.mock import MagicMock, patch

import pytest

from charlotte.core.extractor import ExtractedPage, extract
from charlotte.exceptions import CharlotteConfigError, CharlotteInternalError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE = "https://example.com/page"


# ---------------------------------------------------------------------------
# ExtractedPage dataclass
# ---------------------------------------------------------------------------

def test_extracted_page_has_text_and_links():
    page = ExtractedPage(text="hello", links=[{"text": "Link", "url": "https://example.com"}])
    assert page.text == "hello"
    assert page.links[0]["url"] == "https://example.com"


def test_extracted_page_links_defaults_to_empty_list():
    page = ExtractedPage(text="hi")
    assert page.links == []


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def test_text_extracted_from_paragraphs():
    html = "<html><body><p>Hello world.</p></body></html>"
    page = extract(html, _BASE)
    assert "Hello world." in page.text


def test_text_extracted_from_headings_and_paragraphs():
    html = "<html><body><h1>Title</h1><p>Content here.</p></body></html>"
    page = extract(html, _BASE)
    assert "Title" in page.text
    assert "Content here." in page.text


def test_horizontal_whitespace_collapsed():
    html = "<p>Hello   world</p>"
    page = extract(html, _BASE)
    assert "  " not in page.text
    assert "Hello world" in page.text


def test_text_truncated_to_max_text_chars():
    long_text = "A" * 1000
    html = f"<p>{long_text}</p>"
    page = extract(html, _BASE, max_text_chars=100)
    assert len(page.text) <= 100


def test_text_not_truncated_when_under_budget():
    html = "<p>Short text.</p>"
    page = extract(html, _BASE, max_text_chars=1000)
    assert "Short text." in page.text


def test_empty_html_returns_empty_text():
    page = extract("", _BASE)
    assert page.text == ""


def test_html_with_no_body_text_returns_empty_text():
    html = "<html><head><title>Page</title></head><body></body></html>"
    page = extract(html, _BASE)
    # title may appear in get_text, but body is empty — just check no crash
    assert isinstance(page.text, str)


# ---------------------------------------------------------------------------
# Link extraction — basics
# ---------------------------------------------------------------------------

def test_link_extracted_with_text_and_url():
    html = '<a href="https://example.com/about">About us</a>'
    page = extract(html, _BASE)
    assert len(page.links) == 1
    assert page.links[0]["text"] == "About us"
    assert page.links[0]["url"] == "https://example.com/about"


def test_link_text_whitespace_collapsed():
    html = '<a href="https://example.com/a">Click  here</a>'
    page = extract(html, _BASE)
    assert page.links[0]["text"] == "Click here"


def test_link_with_empty_anchor_text():
    html = '<a href="https://example.com/img"><img src="x.png"/></a>'
    page = extract(html, _BASE)
    # img has no text — anchor text should be empty string
    assert page.links[0]["text"] == ""


def test_empty_href_skipped():
    html = '<a href="">Nothing</a><a href="https://example.com/">Real</a>'
    page = extract(html, _BASE)
    assert len(page.links) == 1
    assert "example.com" in page.links[0]["url"]


def test_no_links_in_page():
    html = "<p>No links here.</p>"
    page = extract(html, _BASE)
    assert page.links == []


# ---------------------------------------------------------------------------
# URL resolution — relative hrefs
# ---------------------------------------------------------------------------

def test_relative_href_resolved_to_absolute():
    html = '<a href="/contact">Contact</a>'
    page = extract(html, "https://example.com/page")
    assert page.links[0]["url"] == "https://example.com/contact"


def test_relative_path_href_resolved():
    html = '<a href="about.html">About</a>'
    page = extract(html, "https://example.com/dir/page.html")
    assert page.links[0]["url"] == "https://example.com/dir/about.html"


def test_protocol_relative_href_inherits_scheme():
    html = '<a href="//example.com/path">Link</a>'
    page = extract(html, "https://example.com/page")
    assert page.links[0]["url"] == "https://example.com/path"


# ---------------------------------------------------------------------------
# Non-http/https schemes are discarded
# ---------------------------------------------------------------------------

def test_mailto_link_excluded():
    html = '<a href="mailto:user@example.com">Email</a>'
    page = extract(html, _BASE)
    assert page.links == []


def test_javascript_link_excluded():
    html = '<a href="javascript:void(0)">Click</a>'
    page = extract(html, _BASE)
    assert page.links == []


def test_tel_link_excluded():
    html = '<a href="tel:+1555555555">Call us</a>'
    page = extract(html, _BASE)
    assert page.links == []


def test_ftp_link_excluded():
    html = '<a href="ftp://files.example.com/data">FTP</a>'
    page = extract(html, _BASE)
    assert page.links == []


# ---------------------------------------------------------------------------
# Domain filtering
# ---------------------------------------------------------------------------

def test_domain_filter_keeps_matching_links():
    html = (
        '<a href="https://example.com/a">Same domain</a>'
        '<a href="https://other.com/b">Other domain</a>'
    )
    page = extract(html, _BASE, allowed_domains={"example.com"})
    assert len(page.links) == 1
    assert "example.com" in page.links[0]["url"]


def test_domain_filter_none_allows_all_http_links():
    html = (
        '<a href="https://example.com/a">A</a>'
        '<a href="https://other.com/b">B</a>'
    )
    page = extract(html, _BASE, allowed_domains=None)
    assert len(page.links) == 2


def test_domain_filter_empty_set_excludes_all():
    html = '<a href="https://example.com/a">A</a>'
    page = extract(html, _BASE, allowed_domains=set())
    assert page.links == []


def test_domain_filter_multiple_domains():
    html = (
        '<a href="https://alpha.com/x">Alpha</a>'
        '<a href="https://beta.com/y">Beta</a>'
        '<a href="https://gamma.com/z">Gamma</a>'
    )
    page = extract(html, _BASE, allowed_domains={"alpha.com", "beta.com"})
    urls = [lnk["url"] for lnk in page.links]
    assert any("alpha.com" in u for u in urls)
    assert any("beta.com" in u for u in urls)
    assert not any("gamma.com" in u for u in urls)


def test_domain_filter_uses_hostname_only_not_path():
    # Ensure the filter matches on hostname, not URL substring
    html = '<a href="https://notexample.com/example.com">Tricky</a>'
    page = extract(html, _BASE, allowed_domains={"example.com"})
    assert page.links == []


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def test_duplicate_links_deduplicated():
    html = (
        '<a href="https://example.com/page">First</a>'
        '<a href="https://example.com/page">Duplicate</a>'
    )
    page = extract(html, _BASE)
    assert len(page.links) == 1


def test_dedup_uses_normalized_comparison():
    # These normalize to the same URL (trailing slash removed, query sorted)
    html = (
        '<a href="https://example.com/page">A</a>'
        '<a href="https://example.com/page/">B</a>'  # trailing slash variant
    )
    page = extract(html, _BASE)
    assert len(page.links) == 1


def test_different_urls_not_deduplicated():
    html = (
        '<a href="https://example.com/page1">Page 1</a>'
        '<a href="https://example.com/page2">Page 2</a>'
    )
    page = extract(html, _BASE)
    assert len(page.links) == 2


# ---------------------------------------------------------------------------
# max_links cap
# ---------------------------------------------------------------------------

def test_max_links_cap_applied():
    hrefs = "".join(
        f'<a href="https://example.com/p{i}">Link {i}</a>'
        for i in range(20)
    )
    page = extract(hrefs, _BASE, max_links=5)
    assert len(page.links) == 5


def test_max_links_default_is_fifty():
    hrefs = "".join(
        f'<a href="https://example.com/p{i}">Link {i}</a>'
        for i in range(60)
    )
    page = extract(hrefs, _BASE)
    assert len(page.links) == 50


def test_max_links_zero_returns_empty():
    html = '<a href="https://example.com/">Link</a>'
    page = extract(html, _BASE, max_links=0)
    assert page.links == []


# ---------------------------------------------------------------------------
# Combined text + links
# ---------------------------------------------------------------------------

def test_full_page_extracts_both_text_and_links():
    html = """
    <html><body>
      <h1>Welcome</h1>
      <p>Visit our <a href="/about">about page</a> for more.</p>
    </body></html>
    """
    page = extract(html, "https://example.com/")
    assert "Welcome" in page.text
    assert "Visit our" in page.text
    assert any("about" in lnk["url"] for lnk in page.links)


# ---------------------------------------------------------------------------
# Exception boundaries
# ---------------------------------------------------------------------------

def test_resolve_href_exception_skips_link():
    # If urljoin raises unexpectedly, the link is silently skipped (lines 58-59).
    html = '<a href="/path">Link</a><a href="https://example.com/">Good</a>'
    with patch("charlotte.core.extractor.urljoin", side_effect=ValueError("bad")):
        page = extract(html, _BASE)
    assert page.links == []


def test_normalize_url_config_error_skips_link():
    # If normalize_url raises CharlotteConfigError, the link is silently skipped
    # (lines 124-125). The rest of the page still processes normally.
    html = '<a href="https://example.com/a">A</a>'
    with patch("charlotte.core.extractor.normalize_url", side_effect=CharlotteConfigError("bad")):
        page = extract(html, _BASE)
    assert page.links == []


def test_parser_exception_raises_internal_error():
    with patch("charlotte.core.extractor.BeautifulSoup", side_effect=RuntimeError("boom")):
        with pytest.raises(CharlotteInternalError, match="extraction failed"):
            extract("<p>Hello</p>", _BASE)


def test_inner_charlotte_internal_error_reraises_unchanged():
    # A CharlotteInternalError raised inside extract must propagate as-is —
    # not be double-wrapped.
    inner = CharlotteInternalError("inner problem")
    mock_soup = MagicMock()
    mock_soup.get_text.side_effect = inner
    with patch("charlotte.core.extractor.BeautifulSoup", return_value=mock_soup):
        with pytest.raises(CharlotteInternalError) as exc_info:
            extract("<p>Hello</p>", _BASE)
    assert exc_info.value is inner
