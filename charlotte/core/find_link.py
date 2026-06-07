"""
find_link() — thin wrapper around crawl() for link discovery (CHAR-014).

find_link() differs from crawl() in two ways: it always collects all matching
links (max_results=None) and never returns page content (return_content=False).
The underlying crawl and event stream are identical to crawl(). See spec §5.2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncGenerator

from charlotte.config import CharlotteConfig
from charlotte.core.engine import crawl
from charlotte.models import CrawlResult, LinkResult, StreamEvent

if TYPE_CHECKING:
    from charlotte.adapters.base import AdapterProtocol


def _to_link_result(result: CrawlResult) -> LinkResult:
    """Convert a CrawlResult to the compact LinkResult format."""
    note: str | None = None
    if not result.found:
        if result.budget_exhausted:
            note = (
                f"Search stopped after {result.pages_visited} page(s) without "
                "finding a match. Try increasing max_pages or max_depth."
            )
        else:
            note = (
                f"No matching link found after visiting {result.pages_visited} page(s)."
            )
    return LinkResult(
        found=result.found,
        urls=result.result_urls,
        confidence=result.confidence,
        pages_visited=result.pages_visited,
        best_candidate_url=result.best_candidate_url,
        budget_exhausted=result.budget_exhausted,
        note=note,
    )


def find_link(
    start_url: str,
    goal: str,
    *,
    model: "AdapterProtocol | None" = None,
    max_pages: int = 20,
    max_depth: int = 5,
    confidence_threshold: float = 0.70,
    render_js: bool = False,
    allowed_domains: "list[str] | None" = None,
    navigation_hint: "str | None" = None,
    stream: "bool | None" = None,
    respect_robots: "bool | None" = None,
    connect_timeout: float = 10.0,
    read_timeout: float = 30.0,
    render_timeout: float = 15.0,
    default_delay: float = 1.0,
    chromium_executable: "str | None" = None,
    max_response_bytes: int = 10 * 1024 * 1024,
    user_agent: "str | None" = None,
    preprocessor: "Any | None" = None,
    ranker: "Any | None" = None,
    locale: str = "en_US",
) -> "AsyncGenerator[StreamEvent, None] | Any":
    """Find all links matching *goal* starting from *start_url*.

    A thin wrapper around crawl() with find_link()-specific defaults:
    max_results=None (collect every matching link) and return_content=False
    (always — use crawl() directly if you need page text). The event stream
    is identical to crawl(). See spec §5.2.

    Args:
        start_url:            Absolute URL at which to begin.
        goal:                 Natural language description of what to find.
        model:                Adapter callable. None resolves via
                              CHARLOTTE_DEFAULT_ADAPTER (default: GroqAdapter).
                              Raises CharlotteConfigError if the resolved adapter
                              cannot be configured (e.g. missing GROQ_API_KEY).
        max_pages:            Hard ceiling on total pages fetched.
        max_depth:            Maximum link-hops from start_url.
        confidence_threshold: Minimum model confidence to record a result (0–1).
        render_js:            Use Playwright (headless Chromium) to render pages.
                              Raises CharlotteConfigError if playwright not installed.
        allowed_domains:      Hostnames Charlotte may visit; defaults to start_url domain.
        navigation_hint:      Extra context passed to the model alongside the goal.
        stream:               True → return AsyncGenerator of events.
                              False → return coroutine resolving to LinkResult.
                              None → read CHARLOTTE_STREAM (default: True).
        respect_robots:       True/False overrides CHARLOTTE_RESPECT_ROBOTS.
                              None → read CHARLOTTE_RESPECT_ROBOTS (default: True).
        connect_timeout:      TCP connection timeout for HTTP requests (seconds).
        read_timeout:         Response body read timeout (seconds).
        render_timeout:       Seconds to wait for JS to settle after navigation (seconds).
        default_delay:        Floor for the polite inter-request delay (seconds).
        chromium_executable:  Path to a Chromium/Chrome binary, used only when
                              render_js=True. Overrides Playwright's bundled
                              Chromium — needed on OS versions Playwright
                              doesn't yet package a bundled binary for.
        max_response_bytes:   Maximum response body size in bytes (default: 10 MB).
        user_agent:           HTTP User-Agent header. None → CHARLOTTE_USER_AGENT env
                              var, or the built-in default (charlotte-crawler/1.0).
        preprocessor:         GoalPreprocessorProtocol instance used to extract
                              anchor terms and classify the goal. None →
                              DeterministicPreprocessor (no model calls).
        ranker:               LinkRankerProtocol instance used to score and sort
                              candidate links before the model call. None →
                              BM25LinkRanker.
        locale:               BCP 47 locale tag passed to the preprocessor (default:
                              "en_US"). Affects stop-word filtering and goal
                              classification in Phase B and later.

    Returns:
        AsyncGenerator[StreamEvent, None] when stream=True.
        Coroutine[LinkResult] when stream=False — use ``await find_link(...)``.

    Raises:
        CharlotteConfigError: Invalid configuration (render_js=True, bad URL,
                              or no model provided).
    """
    _kwargs: dict[str, Any] = dict(
        model=model,
        max_pages=max_pages,
        max_depth=max_depth,
        max_results=None,
        confidence_threshold=confidence_threshold,
        render_js=render_js,
        allowed_domains=allowed_domains,
        return_content=False,
        navigation_hint=navigation_hint,
        respect_robots=respect_robots,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        render_timeout=render_timeout,
        default_delay=default_delay,
        chromium_executable=chromium_executable,
        max_response_bytes=max_response_bytes,
        user_agent=user_agent,
        preprocessor=preprocessor,
        ranker=ranker,
        locale=locale,
    )

    resolved_stream = CharlotteConfig.stream() if stream is None else stream

    if resolved_stream:
        return crawl(start_url, goal, stream=True, **_kwargs)

    async def _silent() -> LinkResult:
        crawl_result: CrawlResult = await crawl(start_url, goal, stream=False, **_kwargs)
        return _to_link_result(crawl_result)

    return _silent()
