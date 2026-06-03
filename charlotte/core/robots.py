"""
robots.txt handler for Charlotte (spec §11, §11.1).

Fetches, parses, and caches robots.txt per domain for the duration of a
crawl session. Provides a single ``check()`` coroutine that the engine calls
before fetching any URL when ``respect_robots=True``.

Behaviour summary (§11.1):
  - 4xx except 429    → no restrictions, crawl proceeds (RFC 9309 §2.3.1)
  - 429 / 5xx / other → RobotsError (uncrawlable)
  - Timeout           → RobotsError (uncrawlable)
  - Connection error  → RobotsError (uncrawlable)
  - Malformed body    → RobotsError (uncrawlable); no partial parsing
  - Disallowed        → RobotsError (disallowed)
  - Allowed           → returns effective crawl delay

User-agent matching: ``charlotte-crawler`` checked first, then ``*``. If neither
section is present the domain is treated as fully crawlable.

Crawl-delay: ``charlotte-crawler`` section checked first, then ``*``. The
effective delay is whichever is larger: the robots.txt directive or the
caller-supplied default.

Public class: RobotsHandler
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from charlotte.config import HTTP_USER_AGENT
from charlotte.exceptions import CharlotteInternalError, RobotsError

_CHARLOTTE_UA: str = "charlotte-crawler"
_WILDCARD_UA: str = "*"
_DEFAULT_CONNECT_TIMEOUT: float = 10.0
_DEFAULT_READ_TIMEOUT: float = 10.0


@dataclass
class _CachedEntry:
    """Parsed result for one domain's robots.txt, held in RobotsHandler._cache."""

    blocked: bool
    reason: str = ""
    parser: RobotFileParser | None = None
    crawl_delay: float = 0.0


class RobotsHandler:
    """Fetches, parses, and caches robots.txt per domain for one crawl session.

    One instance should be shared across all fetches in a single crawl so that
    each domain's robots.txt is fetched at most once.

    Args:
        connect_timeout: TCP connection timeout (seconds) for the robots.txt fetch.
        read_timeout:    Response body read timeout (seconds) for the robots.txt fetch.
    """

    def __init__(
        self,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = _DEFAULT_READ_TIMEOUT,
    ) -> None:
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._cache: dict[str, _CachedEntry] = {}
        self._cache_locks: dict[str, asyncio.Lock] = {}

    async def check(self, url: str, default_delay: float) -> float:
        """Check whether *url* may be fetched per this domain's robots.txt.

        Args:
            url:           Absolute URL about to be fetched.
            default_delay: Charlotte's polite request delay (seconds); used as
                           the floor for the effective crawl delay.

        Returns:
            Effective crawl delay in seconds — the larger of the robots.txt
            ``Crawl-delay`` directive and *default_delay*.

        Raises:
            RobotsError: The domain's robots.txt disallows the URL, could not
                         be fetched (timeout, connection error, non-200), or
                         could not be decoded.
            CharlotteInternalError: Unexpected failure during the check.
        """
        try:
            return await self._do_check(url, default_delay)
        except RobotsError:
            raise
        except Exception as exc:
            raise CharlotteInternalError(
                "Robots check failed unexpectedly — please report this at "
                "https://github.com/Boss-Button-Studios/charlotte/issues"
            ) from exc

    async def _do_check(self, url: str, default_delay: float) -> float:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower()
        scheme = parsed.scheme

        if hostname not in self._cache:
            lock = self._cache_locks.setdefault(hostname, asyncio.Lock())
            async with lock:
                if hostname not in self._cache:
                    self._cache[hostname] = await self._fetch_and_parse(scheme, hostname)

        entry = self._cache[hostname]

        if entry.blocked:
            raise RobotsError(entry.reason)

        if entry.parser is not None and not entry.parser.can_fetch(_CHARLOTTE_UA, url):
            raise RobotsError(
                f"robots.txt on {hostname!r} disallows crawling this URL"
            )

        if entry.crawl_delay > 0.0:
            return max(entry.crawl_delay, default_delay)
        return default_delay

    async def _fetch_and_parse(self, scheme: str, hostname: str) -> _CachedEntry:
        robots_url = f"{scheme}://{hostname}/robots.txt"
        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=None,
            pool=None,
        )

        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                headers={"User-Agent": HTTP_USER_AGENT},
                follow_redirects=True,
            ) as client:
                response = await client.get(robots_url)
        except httpx.TimeoutException:
            return _CachedEntry(
                blocked=True,
                reason=(
                    f"robots.txt for {hostname!r} could not be fetched (timeout)"
                    " — treating domain as uncrawlable"
                ),
            )
        except Exception:
            return _CachedEntry(
                blocked=True,
                reason=(
                    f"robots.txt for {hostname!r} could not be reached"
                    " — treating domain as uncrawlable"
                ),
            )

        status = response.status_code

        # RFC 9309 §2.3.1: any 4xx except 429 means the robots.txt file is
        # inaccessible — treat as no restrictions.  429 (Too Many Requests)
        # signals rate-limiting, so we conservatively block the domain.
        if 400 <= status < 500 and status != 429:
            return _CachedEntry(blocked=False)

        if status != 200:
            return _CachedEntry(
                blocked=True,
                reason=(
                    f"robots.txt for {hostname!r} returned HTTP {status}"
                    " — treating domain as uncrawlable"
                ),
            )

        try:
            content = response.text
        except httpx.DecodingError:
            return _CachedEntry(
                blocked=True,
                reason=(
                    f"robots.txt for {hostname!r} could not be decoded"
                    " — treating domain as uncrawlable"
                ),
            )

        return _parse_robots_content(content, hostname)


def _parse_robots_content(content: str, hostname: str) -> _CachedEntry:
    """Parse robots.txt text into a _CachedEntry.

    Uses stdlib RobotFileParser. No partial parsing: any exception during
    parsing treats the entire file as unparseable and blocks the domain.
    """
    try:
        parser = RobotFileParser()
        parser.parse(content.splitlines())

        crawl_delay = parser.crawl_delay(_CHARLOTTE_UA)
        if crawl_delay is None:
            crawl_delay = parser.crawl_delay(_WILDCARD_UA)
        effective_delay = float(crawl_delay) if crawl_delay is not None else 0.0

        return _CachedEntry(blocked=False, parser=parser, crawl_delay=effective_delay)
    except Exception:
        return _CachedEntry(
            blocked=True,
            reason=(
                f"robots.txt for {hostname!r} could not be parsed"
                " — treating domain as uncrawlable"
            ),
        )
