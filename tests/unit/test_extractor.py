"""
Unit tests for the content extractor (CHAR-007, spec §10).

Covers visible text extraction, link extraction, URL resolution,
deduplication, budget truncation, and exception boundaries.
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
    """ExtractedPage stores text and links with the expected types."""
    page = ExtractedPage(text="hello", links=[{"text": "Link", "url": "https://example.com"}])
    assert page.text == "hello"
    assert page.links[0]["url"] == "https://example.com"


def test_extracted_page_links_defaults_to_empty_list():
    """links field defaults to [] so callers need not supply it."""
    page = ExtractedPage(text="hi")
    assert page.links == []


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def test_text_extracted_from_paragraphs():
    """Visible paragraph text is present in the extracted text."""
    html = "<html><body><p>Hello world.</p></body></html>"
    page = extract(html, _BASE)
    assert "Hello world." in page.text


def test_text_extracted_from_headings_and_paragraphs():
    """Both heading and paragraph text appear in the summary."""
    html = "<html><body><h1>Title</h1><p>Content here.</p></body></html>"
    page = extract(html, _BASE)
    assert "Title" in page.text
    assert "Content here." in page.text


def test_horizontal_whitespace_collapsed():
    """Consecutive spaces and tabs in source HTML are collapsed to one space."""
    html = "<p>Hello   world</p>"
    page = extract(html, _BASE)
    assert "  " not in page.text
    assert "Hello world" in page.text


def test_text_truncated_to_max_text_chars():
    """Text longer than max_text_chars is hard-truncated at the character boundary."""
    long_text = "A" * 1000
    html = f"<p>{long_text}</p>"
    page = extract(html, _BASE, max_text_chars=100)
    assert len(page.text) <= 100


def test_text_not_truncated_when_under_budget():
    """Text shorter than max_text_chars is returned in full."""
    html = "<p>Short text.</p>"
    page = extract(html, _BASE, max_text_chars=1000)
    assert "Short text." in page.text


def test_empty_html_returns_empty_text():
    """Empty HTML input produces an empty string, not an error."""
    page = extract("", _BASE)
    assert page.text == ""


def test_html_with_no_body_text_returns_empty_text():
    """HTML with no meaningful body content does not raise."""
    html = "<html><head><title>Page</title></head><body></body></html>"
    page = extract(html, _BASE)
    # title may appear in get_text, but body is empty — just check no crash
    assert isinstance(page.text, str)


# ---------------------------------------------------------------------------
# Link extraction — basics
# ---------------------------------------------------------------------------

def test_link_extracted_with_text_and_url():
    """A single anchor is returned as a {text, url} dict."""
    html = '<a href="https://example.com/about">About us</a>'
    page = extract(html, _BASE)
    assert len(page.links) == 1
    assert page.links[0]["text"] == "About us"
    assert page.links[0]["url"] == "https://example.com/about"


def test_link_text_whitespace_collapsed():
    """Anchor text with extra spaces is collapsed like body text."""
    html = '<a href="https://example.com/a">Click  here</a>'
    page = extract(html, _BASE)
    assert page.links[0]["text"] == "Click here"


def test_link_with_empty_anchor_text():
    """Anchors containing only non-text content (img) produce an empty text string."""
    html = '<a href="https://example.com/img"><img src="x.png"/></a>'
    page = extract(html, _BASE)
    # img has no text — anchor text should be empty string
    assert page.links[0]["text"] == ""


def test_empty_href_skipped():
    """Anchors with an empty href are skipped; non-empty hrefs are kept."""
    html = '<a href="">Nothing</a><a href="https://example.com/">Real</a>'
    page = extract(html, _BASE)
    assert len(page.links) == 1
    assert "example.com" in page.links[0]["url"]


def test_no_links_in_page():
    """A page with no anchors produces an empty links list."""
    html = "<p>No links here.</p>"
    page = extract(html, _BASE)
    assert page.links == []


# ---------------------------------------------------------------------------
# URL resolution — relative hrefs
# ---------------------------------------------------------------------------

def test_relative_href_resolved_to_absolute():
    """A root-relative href is resolved against the page's origin."""
    html = '<a href="/contact">Contact</a>'
    page = extract(html, "https://example.com/page")
    assert page.links[0]["url"] == "https://example.com/contact"


def test_relative_path_href_resolved():
    """A path-relative href is resolved against the page's directory."""
    html = '<a href="about.html">About</a>'
    page = extract(html, "https://example.com/dir/page.html")
    assert page.links[0]["url"] == "https://example.com/dir/about.html"


def test_protocol_relative_href_inherits_scheme():
    """A protocol-relative href (//) inherits the scheme from page_url."""
    html = '<a href="//example.com/path">Link</a>'
    page = extract(html, "https://example.com/page")
    assert page.links[0]["url"] == "https://example.com/path"


# ---------------------------------------------------------------------------
# Non-http/https schemes are discarded
# ---------------------------------------------------------------------------

def test_mailto_link_excluded():
    """mailto: links are discarded — not navigable by the crawler."""
    html = '<a href="mailto:user@example.com">Email</a>'
    page = extract(html, _BASE)
    assert page.links == []


def test_javascript_link_excluded():
    """javascript: hrefs are discarded as potential injection vectors."""
    html = '<a href="javascript:void(0)">Click</a>'
    page = extract(html, _BASE)
    assert page.links == []


def test_tel_link_excluded():
    """tel: links are discarded — not web-navigable."""
    html = '<a href="tel:+1555555555">Call us</a>'
    page = extract(html, _BASE)
    assert page.links == []


def test_ftp_link_excluded():
    """ftp: links are discarded — only http and https are crawlable."""
    html = '<a href="ftp://files.example.com/data">FTP</a>'
    page = extract(html, _BASE)
    assert page.links == []


# ---------------------------------------------------------------------------
# All http/https links included — no domain filtering in extractor
# ---------------------------------------------------------------------------

def test_external_links_included():
    """External links are returned — domain filtering is the engine's job, not ours."""
    html = (
        '<a href="https://example.com/a">Same domain</a>'
        '<a href="https://other.com/b">Other domain</a>'
    )
    page = extract(html, _BASE)
    urls = [lnk["url"] for lnk in page.links]
    assert any("example.com" in u for u in urls)
    assert any("other.com" in u for u in urls)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def test_duplicate_links_deduplicated():
    """Two anchors pointing to the same URL produce only one link entry."""
    html = (
        '<a href="https://example.com/page">First</a>'
        '<a href="https://example.com/page">Duplicate</a>'
    )
    page = extract(html, _BASE)
    assert len(page.links) == 1


def test_dedup_uses_normalized_comparison():
    """URLs that differ only by trailing slash are treated as the same after normalization."""
    html = (
        '<a href="https://example.com/page">A</a>'
        '<a href="https://example.com/page/">B</a>'  # trailing slash variant
    )
    page = extract(html, _BASE)
    assert len(page.links) == 1


def test_different_urls_not_deduplicated():
    """Distinct URLs each produce a separate link entry."""
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
    """The link list is capped at max_links regardless of how many anchors exist."""
    hrefs = "".join(
        f'<a href="https://example.com/p{i}">Link {i}</a>'
        for i in range(20)
    )
    page = extract(hrefs, _BASE, max_links=5)
    assert len(page.links) == 5


def test_max_links_default_is_two_hundred():
    """Default max_links is 200 -- pages with more anchors are capped at 200."""
    hrefs = "".join(
        f'<a href="https://example.com/p{i}">Link {i}</a>'
        for i in range(210)
    )
    page = extract(hrefs, _BASE)
    assert len(page.links) == 200


def test_max_links_zero_returns_empty():
    """max_links=0 returns an empty link list even when anchors are present."""
    html = '<a href="https://example.com/">Link</a>'
    page = extract(html, _BASE, max_links=0)
    assert page.links == []


# ---------------------------------------------------------------------------
# Combined text + links
# ---------------------------------------------------------------------------

def test_full_page_extracts_both_text_and_links():
    """A realistic page produces both non-empty text and a populated link list."""
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
    """An unexpected exception inside _resolve_href silently drops the link (lines 58-59)."""
    html = '<a href="/path">Link</a><a href="https://example.com/">Good</a>'
    with patch("charlotte.core.extractor.urljoin", side_effect=ValueError("bad")):
        page = extract(html, _BASE)
    assert page.links == []


def test_normalize_url_config_error_skips_link():
    """A CharlotteConfigError from normalize_url silently drops the offending link (lines 124-125)."""
    html = '<a href="https://example.com/a">A</a>'
    with patch("charlotte.core.extractor.normalize_url", side_effect=CharlotteConfigError("bad")):
        page = extract(html, _BASE)
    assert page.links == []


def test_parser_exception_raises_internal_error():
    """An unexpected BeautifulSoup crash is wrapped as CharlotteInternalError."""
    with patch("charlotte.core.extractor.BeautifulSoup", side_effect=RuntimeError("boom")):
        with pytest.raises(CharlotteInternalError, match="extraction failed"):
            extract("<p>Hello</p>", _BASE)


def test_inner_charlotte_internal_error_reraises_unchanged():
    """A CharlotteInternalError raised inside extract propagates as-is — not double-wrapped."""
    inner = CharlotteInternalError("inner problem")
    mock_soup = MagicMock()
    mock_soup.find.return_value = None  # title lookup must not interfere
    mock_soup.find_all.side_effect = inner
    with patch("charlotte.core.extractor.BeautifulSoup", return_value=mock_soup):
        with pytest.raises(CharlotteInternalError) as exc_info:
            extract("<p>Hello</p>", _BASE)
    assert exc_info.value is inner


# ---------------------------------------------------------------------------
# Structural zone priority
# ---------------------------------------------------------------------------

def test_main_content_link_before_nav_link():
    """A link inside <main> appears before a link inside <nav>, regardless of DOM order."""
    html = """
    <html><body>
      <nav><a href="https://example.com/nav">Nav link</a></nav>
      <main><a href="https://example.com/content">Content link</a></main>
    </body></html>
    """
    page = extract(html, _BASE)
    urls = [lnk["url"] for lnk in page.links]
    assert urls.index("https://example.com/content") < urls.index("https://example.com/nav")


def test_nav_links_cut_first_when_cap_reached():
    """When max_links is tight, chrome-zone links are dropped before content-zone links."""
    nav_links = "".join(
        f'<a href="https://example.com/nav{i}">Nav {i}</a>'
        for i in range(5)
    )
    content_links = "".join(
        f'<a href="https://example.com/content{i}">Content {i}</a>'
        for i in range(5)
    )
    # Nav appears first in DOM; content appears second. Cap set to 6 (less than 10 total).
    html = f"<nav>{nav_links}</nav><main>{content_links}</main>"
    page = extract(html, _BASE, max_links=6)
    urls = [lnk["url"] for lnk in page.links]
    # All 5 content links must be present.
    for i in range(5):
        assert f"https://example.com/content{i}" in urls
    # Only 1 nav link fits; the rest were cut.
    nav_count = sum(1 for u in urls if "/nav" in u)
    assert nav_count == 1


def test_section_and_article_treated_as_content_zone():
    """Links inside <section> and <article> are zone 0, same as <main>."""
    html = """
    <nav><a href="https://example.com/nav">Nav</a></nav>
    <article><a href="https://example.com/article">Article</a></article>
    <section><a href="https://example.com/section">Section</a></section>
    """
    page = extract(html, _BASE)
    urls = [lnk["url"] for lnk in page.links]
    nav_idx = urls.index("https://example.com/nav")
    assert urls.index("https://example.com/article") < nav_idx
    assert urls.index("https://example.com/section") < nav_idx


def test_header_and_footer_treated_as_chrome_zone():
    """Links inside <header> and <footer> are deprioritized like <nav>."""
    html = """
    <header><a href="https://example.com/header">Header</a></header>
    <footer><a href="https://example.com/footer">Footer</a></footer>
    <main><a href="https://example.com/content">Content</a></main>
    """
    page = extract(html, _BASE)
    urls = [lnk["url"] for lnk in page.links]
    content_idx = urls.index("https://example.com/content")
    assert urls.index("https://example.com/header") > content_idx
    assert urls.index("https://example.com/footer") > content_idx


def test_same_url_in_main_and_nav_kept_as_content_zone():
    """When a URL appears in both <main> and <nav>, the <main> (content) entry is kept."""
    html = """
    <nav><a href="https://example.com/page">Nav copy</a></nav>
    <main><a href="https://example.com/page">Content copy</a></main>
    """
    page = extract(html, _BASE)
    assert len(page.links) == 1
    assert page.links[0]["text"] == "Content copy"


def test_unstructured_link_between_content_and_chrome():
    """A link with no structural ancestor (neutral zone) sorts after content but before chrome."""
    html = """
    <nav><a href="https://example.com/nav">Nav</a></nav>
    <div><a href="https://example.com/div">Div</a></div>
    <main><a href="https://example.com/main">Main</a></main>
    """
    page = extract(html, _BASE)
    urls = [lnk["url"] for lnk in page.links]
    assert urls.index("https://example.com/main") < urls.index("https://example.com/div")
    assert urls.index("https://example.com/div") < urls.index("https://example.com/nav")


# ---------------------------------------------------------------------------
# Structural zone priority — text ordering
# ---------------------------------------------------------------------------

def test_text_content_zone_before_chrome_zone():
    """Text inside <main> appears before text inside <nav> regardless of DOM order."""
    html = """
    <nav>Site navigation</nav>
    <main>Page content</main>
    """
    page = extract(html, _BASE)
    assert page.text.index("Page content") < page.text.index("Site navigation")


def test_text_chrome_last_when_dom_first():
    """Header/footer text is pushed to the end even when it appears first in the DOM."""
    html = """
    <header>555-0000</header>
    <main>555-1234</main>
    <footer>Copyright</footer>
    """
    page = extract(html, _BASE)
    # Department number (main) must appear before the general number (header)
    assert page.text.index("555-1234") < page.text.index("555-0000")
    assert page.text.index("555-1234") < page.text.index("Copyright")


def test_text_all_zones_present():
    """Text from all three zones is present in the output."""
    html = "<nav>Nav</nav><div>Neutral</div><main>Content</main>"
    page = extract(html, _BASE)
    assert "Content" in page.text
    assert "Neutral" in page.text
    assert "Nav" in page.text


def test_text_zone_order_is_content_neutral_chrome():
    """Zone ordering is strictly: content (0) then neutral (1) then chrome (2)."""
    html = "<footer>Footer</footer><div>Middle</div><article>Article</article>"
    page = extract(html, _BASE)
    article_pos = page.text.index("Article")
    middle_pos = page.text.index("Middle")
    footer_pos = page.text.index("Footer")
    assert article_pos < middle_pos < footer_pos
