"""
Content extractor for Charlotte (spec §10).

Converts sanitized HTML into the two structures the navigator model needs:
  - visible text (collapsed whitespace, truncated to max_text_chars)
  - link list ({text, url} dicts — resolved absolute URLs, all http/https links
    included, deduplicated via normalized comparison, capped at max_links)

The extractor operates on already-sanitized HTML. It is not responsible for
security — that is the sanitizer's job (spec §9.1). Its only concern is
producing a clean, compact, useful representation for the model.

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
_DEFAULT_MAX_LINKS: int = 50


@dataclass
class ExtractedPage:
    """Result of content extraction from one sanitized page.

    Attributes:
        text:  Visible page text, whitespace-collapsed and truncated.
        links: Extracted links as {text, url} dicts. URLs are absolute and
               http/https only. Filtered, deduplicated, and capped.
    """

    text: str
    links: list[dict[str, str]] = field(default_factory=list)


def _resolve_href(href: str, page_url: str) -> str | None:
    """Resolve an href to an absolute http/https URL, or return None.

    Non-http/https schemes (mailto:, javascript:, tel:, etc.) are discarded —
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

    Text extraction: all visible text nodes joined with spaces, horizontal
    whitespace collapsed, truncated to max_text_chars characters.

    Link extraction: every <a href="..."> is resolved to an absolute URL,
    filtered to http/https only, deduplicated using normalized URL comparison,
    and capped at max_links entries. Domain filtering is the engine's job —
    the extractor returns all observable links so the model can evaluate them.

    Args:
        html:            Sanitized HTML string from the Layer 1 sanitizer.
        page_url:        Absolute URL of the page — used to resolve relative hrefs.
        max_text_chars:  Character budget for the text summary.
        max_links:       Maximum number of links to return.

    Returns:
        ExtractedPage with text and links fields populated.

    Raises:
        CharlotteInternalError: Unexpected parser failure.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")

        # --- Text ---
        raw_text = soup.get_text(separator=" ", strip=True)
        # Collapse consecutive horizontal whitespace to a single space.
        # Newlines from get_text are already normalized by strip=True.
        text = re.sub(r"[ \t]+", " ", raw_text).strip()
        text = text[:max_text_chars]

        # --- Links ---
        seen_normalized: set[str] = set()
        links: list[dict[str, str]] = []

        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href", "").strip()
            if not href:
                continue

            resolved = _resolve_href(href, page_url)
            if resolved is None:
                continue

            # Deduplicate via normalized comparison.
            try:
                norm = normalize_url(resolved)
            except CharlotteConfigError:
                continue

            if norm in seen_normalized:
                continue
            seen_normalized.add(norm)

            link_text = anchor.get_text(separator=" ", strip=True)
            link_text = re.sub(r"[ \t]+", " ", link_text).strip()

            if len(links) >= max_links:
                break

            links.append({"text": link_text, "url": resolved})

        return ExtractedPage(text=text, links=links)

    except CharlotteInternalError:
        raise
    except Exception as exc:
        raise CharlotteInternalError(
            "Content extraction failed — please report this at "
            "https://github.com/Boss-Button-Studios/charlotte/issues: "
            f"{exc}"
        ) from exc
