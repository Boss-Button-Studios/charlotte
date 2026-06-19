"""Fetch-layer utilities — document/challenge detection, Playwright import,
and the FetchResult type. Kept separate so the httpx fetcher and the Playwright
browser-fetch mixin can both import them without a circular dependency."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from charlotte.exceptions import CharlotteConfigError


# URL path extensions that belong to downloadable documents rather than web pages.
# These can't be loaded with Playwright's page.goto() (Chromium renders them inline
# or starts a download); when render_js=True they are fetched via Playwright's
# APIRequestContext instead, and via httpx otherwise.
_DOCUMENT_EXTENSIONS: frozenset[str] = frozenset({
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp",
})

_MAX_REDIRECTS: int = 5

# High-precision markers of an active anti-bot challenge interstitial (Cloudflare
# "Just a moment", Turnstile, hCaptcha, generic JS challenges). These are checked
# only against the *start* of a small response body, and only on the statuses
# challenges use (403/429/503) plus the Cloudflare 200-with-challenge case, so the
# odds of a false positive on real page content are negligible. When matched,
# Charlotte treats the site as declining identified automated access and stops —
# it does not try to solve or evade the challenge. See CharlotteChallengeError.
_CHALLENGE_MARKERS: tuple[str, ...] = (
    "challenges.cloudflare.com",
    "/cdn-cgi/challenge-platform/",
    "just a moment...",
    "cf-browser-verification",
    "_cf_chl_opt",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "window._cf_chl_opt",
    "/hcaptcha.com/",
    "h-captcha",
)
# Only sniff the body when the status is one challenges actually use. Cloudflare's
# interstitial is most often served with 403/503/429, occasionally 200.
_CHALLENGE_STATUSES: frozenset[int] = frozenset({200, 403, 429, 503})


def _is_bot_challenge(status_code: int, body_text: str) -> bool:
    """True if a response looks like an active anti-bot challenge interstitial.

    Conservative by design: requires both a challenge-typical status code and a
    high-precision body marker, matched case-insensitively against the first few
    kilobytes only. Used to honour a site's refusal of automated access rather
    than circumvent it (CharlotteChallengeError).
    """
    if status_code not in _CHALLENGE_STATUSES:
        return False
    head = body_text[:4096].lower()
    return any(marker in head for marker in _CHALLENGE_MARKERS)


def _import_playwright() -> tuple:
    """Import playwright.async_api, raising CharlotteConfigError if not installed.

    Returns (async_playwright factory, PlaywrightTimeoutError class).
    Called at PageFetcher init time when render_js=True, and by crawl() for
    an early availability check before the generator starts.
    """
    try:
        from playwright.async_api import TimeoutError as _PlaywrightTimeout
        from playwright.async_api import async_playwright
        return async_playwright, _PlaywrightTimeout
    except ImportError as exc:
        raise CharlotteConfigError(
            "Playwright rendering (render_js=True) requires the playwright package. "
            "Install it with: python3 -m pip install playwright && "
            "python3 -m playwright install chromium"
        ) from exc


def _is_document_url(url: str) -> bool:
    """True when the URL path ends with a document extension (e.g. .pdf).

    Document URLs must be fetched with httpx even when render_js=True.
    Playwright raises 'Download is starting' when navigating to binary content
    and cannot render the response as HTML.
    """
    path = urlsplit(url).path
    ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    return ext in _DOCUMENT_EXTENSIONS




@dataclass
class FetchResult:
    """Result of a single page fetch, including redirect history."""

    url: str
    html: str
    status_code: int
    fetch_ms: int
    redirect_chain: list[tuple[int, str]] = field(default_factory=list)
    # Set when a binary document was fetched via Playwright APIRequestContext
    # (render_js=True). Empty / absent on the httpx path and for HTML pages.
    raw_bytes: bytes | None = None

