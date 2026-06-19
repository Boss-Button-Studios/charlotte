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

from charlotte.core.browser_fetch import _PlaywrightFetchMixin
from charlotte.core.fetch_util import (
    FetchResult,
    _MAX_REDIRECTS,
    _import_playwright,
    _is_bot_challenge,
    _is_document_url,
)


class PageFetcher(_PlaywrightFetchMixin):
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
                        self._fetch_document_with_playwright(
                            url, visited_urls=visited_urls,
                            robots_handler=robots_handler, default_delay=default_delay,
                        ),
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

