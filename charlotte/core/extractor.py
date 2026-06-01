"""
Content extractor for Charlotte (spec §10).

Converts sanitized HTML into the two structures the navigator model needs:
  - visible text (collapsed whitespace, truncated to max_text_chars)
  - link list ({text, url} dicts — resolved absolute URLs, all http/https links
    included, deduplicated via normalized comparison, sorted by structural zone,
    capped at max_links)

The extractor operates on already-sanitized HTML. It is not responsible for
security — that is the sanitizer's job (spec §9.1). Its only concern is
producing a clean, compact, useful representation for the model.

Both text and links are ordered by structural zone before truncation/capping,
so page-specific content is never crowded out by the site's global navigation.
See _node_zone() for the three zones.

Public function: extract(html, page_url, ...) -> ExtractedPage
Public type:     ExtractedPage
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from charlotte.core.normalizer import normalize_url
from charlotte.exceptions import CharlotteConfigError, CharlotteInternalError

_DEFAULT_MAX_TEXT_CHARS: int = 16_384
_DEFAULT_MAX_LINKS: int = 200

# HTML elements whose presence as an ancestor determines node priority.
# Walking from the node up the tree, the first matching ancestor wins.
#   Zone 0 — content elements: text/links here are page-specific; shown first.
#   Zone 2 — chrome elements: text/links here are global chrome; shown last.
#   Zone 1 (default) — everything else: shown between the two.
_CONTENT_ZONE_TAGS: frozenset[str] = frozenset({"main", "article", "section"})
_CHROME_ZONE_TAGS: frozenset[str] = frozenset({"nav", "header", "footer"})


@dataclass
class ExtractedPage:
    """Result of content extraction from one sanitized page.

    Attributes:
        text:  Visible page text, whitespace-collapsed, zone-ordered, truncated.
        links: Extracted links as {text, url} dicts. URLs are absolute and
               http/https only. Filtered, deduplicated, zone-sorted, and capped.
        title: Text content of the page's <title> element, whitespace-collapsed.
               Empty string when no <title> is present.
    """

    text: str
    links: list[dict[str, str]] = field(default_factory=list)
    title: str = ""


def _node_zone(node) -> int:
    """Return the structural priority zone for any BeautifulSoup node.

    Walks the ancestor chain upward. The first recognized structural tag
    determines the zone:
      0 — content (main, article, section): page-specific content
      1 — neutral: no recognized structural ancestor
      2 — chrome (nav, header, footer): global navigation / site chrome
    """
    for parent in node.parents:
        if parent.name in _CONTENT_ZONE_TAGS:
            return 0
        if parent.name in _CHROME_ZONE_TAGS:
            return 2
    return 1


def _resolve_href(href: str, page_url: str) -> str | None:
    """Resolve an href to an absolute http/https URL, or return None.

    Non-http/https schemes (mailto:, javascript:, tel:, etc.) are discarded --
    they are not navigable and can carry injection payloads.
    """
    try:
        resolved = urljoin(page_url, href)
        scheme = urlsplit(resolved).scheme
        if scheme not in {"http", "https"}:
            return None
        return resolved
    except Exception:
        return None


def extract(
    html: str,
    page_url: str,
    max_text_chars: int = _DEFAULT_MAX_TEXT_CHARS,
    max_links: int = _DEFAULT_MAX_LINKS,
) -> ExtractedPage:
    """Extract visible text and links from sanitized HTML.

    Text extraction: text nodes are collected by structural zone (content
    before neutral before chrome), joined with spaces, horizontal whitespace
    collapsed, and truncated to max_text_chars characters. This ensures
    page-specific content (e.g. a department phone number) appears before
    global chrome (e.g. a general hospital number in the site header).

    Link extraction: every <a href="..."> is resolved to an absolute URL,
    filtered to http/https only, deduplicated using normalized URL comparison,
    sorted by structural zone (content before chrome), and capped at
    max_links entries. Domain filtering is the engine's job -- the extractor
    returns all observable links so the model can evaluate them.

    Args:
        html:            Sanitized HTML string from the Layer 1 sanitizer.
        page_url:        Absolute URL of the page -- used to resolve relative hrefs.
        max_text_chars:  Character budget for the text summary.
        max_links:       Maximum number of links to return.

    Returns:
        ExtractedPage with text and links fields populated.

    Raises:
        CharlotteInternalError: Unexpected parser failure.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")

        # --- Title ---
        title_tag = soup.find("title")
        title = re.sub(r"[ \t]+", " ", title_tag.get_text(strip=True)).strip() if title_tag else ""

        # --- Text ---
        # Collect text nodes by structural zone so content text is always read
        # by the model before global chrome (header/nav/footer).
        text_parts: dict[int, list[str]] = {0: [], 1: [], 2: []}
        for text_node in soup.find_all(string=True):
            fragment = str(text_node).strip()
            if fragment:
                text_parts[_node_zone(text_node)].append(fragment)

        raw_text = " ".join(text_parts[0] + text_parts[1] + text_parts[2])
        text = re.sub(r"[ \t]+", " ", raw_text).strip()
        text = text[:max_text_chars]

        # --- Links ---
        # Collect all candidates with their structural zone so content links
        # are never crowded out by global navigation at cap time.
        candidates: list[tuple[int, str, str, str]] = []  # (zone, text, url, norm)

        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href", "").strip()
            if not href:
                continue

            resolved = _resolve_href(href, page_url)
            if resolved is None:
                continue

            try:
                norm = normalize_url(resolved)
            except CharlotteConfigError:
                continue

            link_text = re.sub(
                r"[ \t]+", " ", anchor.get_text(separator=" ", strip=True)
            ).strip()
            candidates.append((_node_zone(anchor), link_text, resolved, norm))

        # Stable sort preserves DOM order within each zone.
        candidates.sort(key=lambda c: c[0])

        # Deduplicate keeping the highest-priority (lowest zone) occurrence,
        # then cap. The same URL in both <main> and <nav> keeps the <main> entry.
        seen_normalized: set[str] = set()
        links: list[dict[str, str]] = []

        for _zone, link_text, resolved, norm in candidates:
            if len(links) >= max_links:
                break
            if norm in seen_normalized:
                continue
            seen_normalized.add(norm)
            links.append({"text": link_text, "url": resolved})

        return ExtractedPage(text=text, links=links, title=title)

    except CharlotteInternalError:
        raise
    except Exception as exc:
        raise CharlotteInternalError(
            "Content extraction failed -- please report this at "
            "https://github.com/Boss-Button-Studios/charlotte/issues: "
            f"{exc}"
        ) from exc
