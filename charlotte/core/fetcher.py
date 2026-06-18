"""
Page fetcher for Charlotte (spec §8, §8.1, §8.2).

Implements async HTTP fetching with the full timeout policy and redirect policy.
When render_js=True, uses Playwright (headless Chromium) instead of httpx.
Playwright is an optional dependency — CharlotteConfigError is raised at
PageFetcher instantiation time if it is not installed.

PageFetcher supports the async context manager protocol. When render_js=True,
use it as ``async with PageFetcher(...) as fetcher:`` to share one browser
across all fetches in the crawl. This avoids the ~2 s per-launch overhead.
Calling fetch() outside a context manager still works (per-call browser launch).

Public classes: FetchResult, PageFetcher
Public helpers: _import_playwright (used by engine for early availability check)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

import httpx

from charlotte.config import HTTP_USER_AGENT
from charlotte.core.normalizer import normalize_url, validate_url_safety
from charlotte.exceptions import (
    CharlotteChallengeError,
    CharlotteConfigError,
    CharlotteNetworkError,
    CharlotteRedirectError,
    CharlotteResponseTooLargeError,
    CharlotteTimeoutError,
    RobotsError,
)

if TYPE_CHECKING:
    from charlotte.core.robots import RobotsHandler

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


class PageFetcher:
    """Async HTTP fetcher with Charlotte's timeout and redirect policies.

    When render_js=False (default), uses httpx for all fetching. When
    render_js=True, uses headless Chromium via Playwright — the playwright
    package must be installed or CharlotteConfigError is raised at init.

    Args:
        allowed_domains:    Hostnames Charlotte is permitted to fetch.
        connect_timeout:    Seconds to establish a TCP connection (spec §8.1).
        read_timeout:       Seconds to receive the complete response body (spec §8.1).
        render_js:          If True, use Playwright instead of httpx.
        render_timeout:     Seconds to wait for JS to settle after navigation (spec §8.1).
        polite_delay:       Seconds to sleep before each top-level fetch call.
        max_response_bytes: Maximum response body size in bytes. Responses exceeding
                            this limit raise CharlotteResponseTooLargeError.
        user_agent:         HTTP User-Agent header value.
    """

    def __init__(
        self,
        allowed_domains: set[str],
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        render_js: bool = False,
        render_timeout: float = 15.0,
        polite_delay: float = 1.0,
        chromium_executable: str | None = None,
        max_response_bytes: int = 10 * 1024 * 1024,
        user_agent: str = HTTP_USER_AGENT,
        follow_linked_resources: bool = False,
    ) -> None:
        self._render_js = render_js
        # When True, an off-domain URL is fetchable only if it is a document
        # (PDF/DOCX/etc.) — a terminal resource the in-scope site linked to.
        # Off-domain HTML is still refused, so this never enables off-domain
        # navigation/crawling. SSRF validation (validate_url_safety) is unaffected.
        self._follow_linked_resources = follow_linked_resources
        self._render_timeout = render_timeout
        # None → use Playwright's bundled Chromium; non-None → use this path instead.
        # Useful on OS versions Playwright doesn't yet support (e.g. Ubuntu 26.04).
        self._chromium_executable = chromium_executable
        if render_js:
            factory, timeout_err = _import_playwright()
            self._playwright_factory = factory
            self._playwright_timeout_error = timeout_err
        # Shared Playwright state — set by __aenter__, cleared by __aexit__.
        # None when not using the context manager (per-call browser launch path).
        self._browser = None
        self._pw_cm = None
        self._allowed_domains: frozenset[str] = frozenset(d.lower() for d in allowed_domains)
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._polite_delay = polite_delay
        self._max_response_bytes = max_response_bytes
        self._user_agent = user_agent

    def _hostname(self, url: str) -> str:
        return (urlsplit(url).hostname or "").lower()

    def _is_allowed(self, url: str) -> bool:
        host = self._hostname(url)
        if host in self._allowed_domains:
            return True
        if any(host.endswith("." + d) for d in self._allowed_domains):
            return True
        # Terminal-resource relaxation: an off-domain *document* (the file the
        # in-scope site pointed at) is fetchable; off-domain HTML is not, so no
        # off-domain navigation is ever enabled. SSRF checks still apply on fetch.
        return self._follow_linked_resources and _is_document_url(url)

    async def fetch(
        self,
        url: str,
        *,
        visited_urls: set[str],
        robots_handler: "RobotsHandler | None" = None,
        default_delay: float = 0.0,
    ) -> FetchResult:
        """Fetch a page, following redirects per spec §8.2.

        Args:
            url: Absolute URL to fetch. Must be within allowed_domains.
            visited_urls: Normalized URLs already visited this crawl — used for
                          redirect-loop detection.
            robots_handler: When provided, checked against the destination domain
                            whenever a redirect crosses a host boundary (spec §11.1).
            default_delay: Passed to robots_handler.check() for crawl-delay resolution.

        Returns:
            FetchResult with the final URL, HTML, status code, timing, and redirect chain.

        Raises:
            CharlotteTimeoutError: Connect, read, or render timeout.
            CharlotteNetworkError: DNS failure, connection refused, Playwright error,
                                   or other network error.
            CharlotteRedirectError: Redirect chain exceeds 5 hops, crosses into a
                                    disallowed domain, or forms a loop (httpx path only).
            RobotsError: Cross-domain redirect target disallowed by robots.txt.
            CharlotteConfigError: Malformed URL.
        """
        await asyncio.sleep(self._polite_delay)

        if self._render_js:
            # Hard ceiling that covers browser launch + network + render time.
            # When using the context manager (shared browser), launch cost is excluded.
            _launch_cost = 0 if self._browser is not None else 30
            _total_pw_timeout = self._connect_timeout + self._render_timeout + _launch_cost
            if _is_document_url(url):
                # Binary documents can't be navigated to via page.goto() — Chromium
                # renders PDFs inline or triggers a download. Use APIRequestContext
                # instead: a browser-authenticated HTTP request that bypasses
                # bot-detection headers checks without page rendering.
                try:
                    return await asyncio.wait_for(
                        self._fetch_document_with_playwright(url, visited_urls=visited_urls),
                        timeout=_total_pw_timeout,
                    )
                except asyncio.TimeoutError:
                    raise CharlotteTimeoutError(
                        f"Playwright document fetch of {url!r} timed out after "
                        f"{_total_pw_timeout:.0f}s"
                    )
            _suffix = "" if _launch_cost == 0 else " (including browser launch)"
            try:
                return await asyncio.wait_for(
                    self._fetch_with_playwright(url, visited_urls=visited_urls),
                    timeout=_total_pw_timeout,
                )
            except asyncio.TimeoutError:
                raise CharlotteTimeoutError(
                    f"Playwright fetch of {url!r} timed out after "
                    f"{_total_pw_timeout:.0f}s{_suffix}"
                )

        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=None,
            pool=None,
        )
        redirect_chain: list[tuple[int, str]] = []
        # Raw (un-normalized) URLs — keeps /path and /path/ distinct so a
        # server-side trailing-slash canonicalization isn't mistaken for a loop.
        chain_seen: set[str] = {url}
        current_url = url
        start = time.monotonic()

        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            headers={"User-Agent": self._user_agent},
        ) as client:
            while True:
                # SSRF check before each request (also catches redirected URLs).
                validate_url_safety(current_url)
                try:
                    async with client.stream("GET", current_url) as response:
                        if response.is_redirect:
                            location = response.headers.get("location", "")
                            destination = urljoin(current_url, location)
                            redirect_chain.append((response.status_code, destination))

                            if len(redirect_chain) > _MAX_REDIRECTS:
                                raise CharlotteRedirectError(
                                    f"Redirect chain for {url!r} exceeded {_MAX_REDIRECTS} hops"
                                )
                            if not self._is_allowed(destination):
                                raise CharlotteRedirectError(
                                    f"Redirect from {current_url!r} to {destination!r} "
                                    f"crosses into disallowed domain {self._hostname(destination)!r}"
                                )
                            # Cross-domain robots check — spec §11.1: permissions do not
                            # inherit across domain boundaries.
                            if (
                                robots_handler is not None
                                and self._hostname(destination) != self._hostname(current_url)
                            ):
                                await robots_handler.check(destination, default_delay)

                            try:
                                norm_dest = normalize_url(destination)
                            except CharlotteConfigError as exc:
                                raise CharlotteRedirectError(
                                    f"Redirect destination {destination!r} is not a valid URL: {exc}"
                                ) from exc

                            if destination in chain_seen or norm_dest in visited_urls:
                                raise CharlotteRedirectError(
                                    f"Redirect loop detected: {destination!r} already visited"
                                )

                            chain_seen.add(destination)
                            current_url = destination
                            continue  # back to while True — SSRF re-checked on redirect destination

                        # Non-redirect: stream body with size cap.
                        total = 0
                        chunks: list[bytes] = []
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > self._max_response_bytes:
                                raise CharlotteResponseTooLargeError(
                                    f"Response from {current_url!r} exceeded "
                                    f"{self._max_response_bytes // (1024 * 1024)} MB limit"
                                )
                            chunks.append(chunk)
                        html = b"".join(chunks).decode(
                            response.encoding or "utf-8", errors="replace"
                        )
                        if _is_bot_challenge(response.status_code, html):
                            raise CharlotteChallengeError(
                                f"{current_url!r} is behind an anti-bot challenge — "
                                "site declines automated access"
                            )
                        return FetchResult(
                            url=current_url,
                            html=html,
                            status_code=response.status_code,
                            fetch_ms=int((time.monotonic() - start) * 1000),
                            redirect_chain=redirect_chain,
                        )
                except (
                    CharlotteResponseTooLargeError,
                    CharlotteRedirectError,
                    CharlotteChallengeError,
                    RobotsError,
                ):
                    raise
                except httpx.ConnectTimeout as exc:
                    raise CharlotteTimeoutError(
                        f"Connect timeout fetching {current_url!r}"
                    ) from exc
                except httpx.ReadTimeout as exc:
                    raise CharlotteTimeoutError(
                        f"Read timeout fetching {current_url!r}"
                    ) from exc
                except httpx.InvalidURL as exc:
                    raise CharlotteConfigError(
                        f"Invalid URL {current_url!r}: {exc}"
                    ) from exc
                except httpx.NetworkError as exc:
                    raise CharlotteNetworkError(
                        f"Network error fetching {current_url!r}: {exc}"
                    ) from exc
                except httpx.RequestError as exc:
                    raise CharlotteNetworkError(
                        f"Request failed for {current_url!r}: {exc}"
                    ) from exc

    async def _fetch_document_with_playwright(
        self, url: str, *, visited_urls: set[str]
    ) -> FetchResult:
        """Fetch a binary document using Playwright's APIRequestContext.

        When render_js=True, document URLs (PDFs, etc.) cannot be navigated to
        via page.goto() — Chromium either renders them inline or triggers a
        download. APIRequestContext issues the HTTP request through Chromium's
        network stack with the configured (identified) User-Agent. The caller
        receives status_code and raw_bytes; html is always empty for binary content.

        Note: this is a *fresh* request context — it does not carry cookies or a
        cleared challenge from any prior page render, so it does not defeat an
        active anti-bot challenge (e.g. Cloudflare). That is deliberate: Charlotte
        honours such refusals (CharlotteChallengeError) rather than circumventing
        them. Sites that merely require a real JS runtime (Wix SPAs, etc.) still
        work because the document itself is a plain file request.
        """
        validate_url_safety(url)
        start = time.monotonic()

        async def _do_request(browser) -> tuple[int, str, bytes]:
            context = await browser.new_context(
                extra_http_headers={"User-Agent": self._user_agent}
            )
            try:
                resp = await context.request.get(
                    url,
                    timeout=(self._connect_timeout + self._render_timeout) * 1000,
                )
                body = await resp.body()
                return resp.status, resp.url, body
            finally:
                await context.close()

        try:
            if self._browser is not None:
                status, final_url, body = await _do_request(self._browser)
            else:
                async with self._playwright_factory() as pw:
                    launch_kwargs: dict = {"headless": True}
                    if self._chromium_executable:
                        launch_kwargs["executable_path"] = self._chromium_executable
                    browser = await pw.chromium.launch(**launch_kwargs)
                    try:
                        status, final_url, body = await _do_request(browser)
                    finally:
                        await browser.close()
        except self._playwright_timeout_error as exc:
            raise CharlotteTimeoutError(
                f"Playwright document fetch of {url!r} timed out"
            ) from exc
        except (
            CharlotteTimeoutError,
            CharlotteNetworkError,
            CharlotteRedirectError,
            CharlotteResponseTooLargeError,
            CharlotteChallengeError,
        ):
            raise
        except Exception as exc:
            raise CharlotteNetworkError(
                f"Playwright document fetch error for {url!r}: {type(exc).__name__}: {exc}"
            ) from exc

        # A challenge interstitial comes back as a small HTML body in place of the
        # document. Detect it before the size/domain checks so the refusal is
        # reported honestly rather than as a generic failure.
        if _is_bot_challenge(status, body[:4096].decode("utf-8", errors="replace")):
            raise CharlotteChallengeError(
                f"{url!r} is behind an anti-bot challenge — site declines automated access"
            )

        if len(body) > self._max_response_bytes:
            raise CharlotteResponseTooLargeError(
                f"Downloaded document from {url!r} exceeded "
                f"{self._max_response_bytes // (1024 * 1024)} MB limit"
            )
        if not self._is_allowed(final_url):
            raise CharlotteRedirectError(
                f"Redirect from {url!r} to {final_url!r} crosses into "
                f"disallowed domain {self._hostname(final_url)!r}"
            )
        try:
            norm_final = normalize_url(final_url)
        except CharlotteConfigError as exc:
            raise CharlotteRedirectError(
                f"Redirect destination {final_url!r} is not a valid URL: {exc}"
            ) from exc
        if norm_final in visited_urls:
            raise CharlotteRedirectError(
                f"Redirect loop detected: {final_url!r} already visited"
            )

        return FetchResult(
            url=final_url,
            html="",
            status_code=status,
            fetch_ms=int((time.monotonic() - start) * 1000),
            raw_bytes=body if (200 <= status < 300) else None,
        )

    async def __aenter__(self) -> "PageFetcher":
        """Launch a shared browser when render_js=True; no-op otherwise."""
        if self._render_js:
            self._pw_cm = self._playwright_factory()
            try:
                pw = await self._pw_cm.__aenter__()
            except Exception as exc:
                self._pw_cm = None
                raise CharlotteConfigError(
                    "Playwright failed to initialise — the installed version may be "
                    "incompatible with the browser binary. "
                    "Try running with the project virtual environment: "
                    f".venv/bin/python <script>. Underlying error: {exc}"
                ) from exc
            try:
                launch_kwargs: dict = {"headless": True}
                if self._chromium_executable:
                    launch_kwargs["executable_path"] = self._chromium_executable
                self._browser = await pw.chromium.launch(**launch_kwargs)
            except Exception as exc:
                await self._pw_cm.__aexit__(type(exc), exc, exc.__traceback__)
                self._pw_cm = None
                raise CharlotteConfigError(
                    f"Playwright browser launch failed: {exc}. "
                    "If using chromium_executable, verify the path points to the "
                    "real binary, not a snap stub (e.g. use "
                    "/snap/chromium/current/usr/lib/chromium-browser/chrome)."
                ) from exc
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Close the shared browser and Playwright context."""
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._pw_cm is not None:
            await self._pw_cm.__aexit__(exc_type, exc_val, exc_tb)
            self._pw_cm = None

    async def _render_page(self, browser, url: str) -> tuple:
        """Open a fresh page in browser, navigate to url, and return (html, final_url, status).

        Sets Charlotte's User-Agent so server-side logs see the same identifier
        as the httpx path. Prefers networkidle so SPA content is fully rendered;
        falls back to whatever has rendered when networkidle never settles (e.g.
        sites with persistent analytics or social-media background requests).
        """
        page = await browser.new_page()
        try:
            await page.set_extra_http_headers({"User-Agent": self._user_agent})
            response = None
            try:
                response = await page.goto(
                    url,
                    wait_until="networkidle",
                    timeout=self._render_timeout * 1000,
                )
            except self._playwright_timeout_error:
                # networkidle never settled — capture whatever has rendered.
                pass
            html = await page.content()
            if len(html.encode()) > self._max_response_bytes:
                raise CharlotteResponseTooLargeError(
                    f"Rendered page from {url!r} exceeded "
                    f"{self._max_response_bytes // (1024 * 1024)} MB limit"
                )
            status_code = response.status if response is not None else 200
            # If the headless browser was served a challenge instead of the page
            # (it could not transparently clear it), honour the refusal.
            if _is_bot_challenge(status_code, html):
                raise CharlotteChallengeError(
                    f"{url!r} is behind an anti-bot challenge — site declines automated access"
                )
            return html, page.url, status_code
        finally:
            await page.close()

    async def _fetch_with_playwright(
        self, url: str, *, visited_urls: set[str]
    ) -> FetchResult:
        """Fetch a JS-rendered page using headless Chromium via Playwright.

        Uses the shared browser from __aenter__ when available (fast path).
        Falls back to launching a per-call browser otherwise (backwards-compatible
        but ~2 s slower per page due to Chromium startup cost).
        Waits for networkidle so SPA content is fully rendered before capture.
        """
        start = time.monotonic()
        try:
            if self._browser is not None:
                html, final_url, status_code = await self._render_page(self._browser, url)
            else:
                async with self._playwright_factory() as pw:
                    launch_kwargs: dict = {"headless": True}
                    if self._chromium_executable:
                        launch_kwargs["executable_path"] = self._chromium_executable
                    browser = await pw.chromium.launch(**launch_kwargs)
                    try:
                        html, final_url, status_code = await self._render_page(browser, url)
                    finally:
                        await browser.close()
        except self._playwright_timeout_error as exc:
            raise CharlotteTimeoutError(f"Render timeout fetching {url!r}") from exc
        except (
            CharlotteTimeoutError,
            CharlotteNetworkError,
            CharlotteRedirectError,
            CharlotteResponseTooLargeError,
            CharlotteChallengeError,
        ):
            raise
        except Exception as exc:
            raise CharlotteNetworkError(
                f"Playwright error fetching {url!r}: {type(exc).__name__}: {exc}"
            ) from exc

        # Post-navigation domain and loop checks — mirrors the httpx redirect policy.
        if not self._is_allowed(final_url):
            raise CharlotteRedirectError(
                f"Redirect from {url!r} to {final_url!r} crosses into "
                f"disallowed domain {self._hostname(final_url)!r}"
            )
        try:
            norm_final = normalize_url(final_url)
        except CharlotteConfigError as exc:
            raise CharlotteRedirectError(
                f"Redirect destination {final_url!r} is not a valid URL: {exc}"
            ) from exc
        if norm_final in visited_urls:
            raise CharlotteRedirectError(
                f"Redirect loop detected: {final_url!r} already visited"
            )

        return FetchResult(
            url=final_url,
            html=html,
            status_code=status_code,
            fetch_ms=int((time.monotonic() - start) * 1000),
            redirect_chain=[],  # Playwright follows redirects internally
        )
