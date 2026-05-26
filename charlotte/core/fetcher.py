"""
Page fetcher for Charlotte (spec §8, §8.1, §8.2).

Implements async HTTP fetching with the full timeout policy, redirect policy,
and a Playwright stub that raises immediately until CHAR-015 is complete.

Public classes: FetchResult, PageFetcher
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

from charlotte.core.normalizer import normalize_url
from charlotte.exceptions import (
    CharlotteConfigError,
    CharlotteNetworkError,
    CharlotteRedirectError,
    CharlotteTimeoutError,
)

_MAX_REDIRECTS: int = 5


@dataclass
class FetchResult:
    """Result of a single page fetch, including redirect history."""

    url: str
    html: str
    status_code: int
    fetch_ms: int
    redirect_chain: list[tuple[int, str]] = field(default_factory=list)


class PageFetcher:
    """Async HTTP fetcher with Charlotte's timeout and redirect policies.

    Args:
        allowed_domains: Hostnames Charlotte is permitted to fetch.
        connect_timeout: Seconds to establish a TCP connection (spec §8.1).
        read_timeout: Seconds to receive the complete response body (spec §8.1).
        render_js: If True, raises immediately — full Playwright support is CHAR-015.
        polite_delay: Seconds to sleep before each top-level fetch call.
    """

    def __init__(
        self,
        allowed_domains: set[str],
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        render_js: bool = False,
        polite_delay: float = 1.0,
    ) -> None:
        if render_js:
            raise CharlotteConfigError(
                "Playwright rendering (render_js=True) requires the playwright extra. "
                "Install it with: pip install 'charlotte-crawler[playwright]'. "
                "Full Playwright support arrives in a future release."
            )
        self._allowed_domains: frozenset[str] = frozenset(d.lower() for d in allowed_domains)
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._polite_delay = polite_delay

    def _hostname(self, url: str) -> str:
        return (urlsplit(url).hostname or "").lower()

    def _is_allowed(self, url: str) -> bool:
        return self._hostname(url) in self._allowed_domains

    async def fetch(self, url: str, *, visited_urls: set[str]) -> FetchResult:
        """Fetch a page, following redirects per spec §8.2.

        Args:
            url: Absolute URL to fetch. Must be within allowed_domains.
            visited_urls: Normalized URLs already visited this crawl — used for
                          redirect-loop detection.

        Returns:
            FetchResult with the final URL, HTML, status code, timing, and redirect chain.

        Raises:
            CharlotteTimeoutError: Connect or read timeout.
            CharlotteNetworkError: DNS failure, connection refused, or other network error.
            CharlotteRedirectError: Redirect chain exceeds 5 hops, crosses into a
                                    disallowed domain, or forms a loop.
            CharlotteConfigError: Malformed URL.
        """
        await asyncio.sleep(self._polite_delay)

        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=None,
            pool=None,
        )
        redirect_chain: list[tuple[int, str]] = []
        chain_seen: set[str] = {normalize_url(url)}
        current_url = url
        start = time.monotonic()

        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            while True:
                try:
                    response = await client.get(current_url)
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

                if not response.is_redirect:
                    return FetchResult(
                        url=current_url,
                        html=response.text,
                        status_code=response.status_code,
                        fetch_ms=int((time.monotonic() - start) * 1000),
                        redirect_chain=redirect_chain,
                    )

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

                try:
                    norm_dest = normalize_url(destination)
                except CharlotteConfigError as exc:
                    raise CharlotteRedirectError(
                        f"Redirect destination {destination!r} is not a valid URL: {exc}"
                    ) from exc

                if norm_dest in chain_seen or norm_dest in visited_urls:
                    raise CharlotteRedirectError(
                        f"Redirect loop detected: {destination!r} already visited"
                    )

                chain_seen.add(norm_dest)
                current_url = destination
