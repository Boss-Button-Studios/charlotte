"""
Crawl engine — BFS crawl loop, streaming events, budget controls (CHAR-013).

CHAR-013: crawl() public function; full BFS loop; streaming events; budget.

Adapter output validation (CHAR-006) lives in adapter_validation.py.

See spec §4, §5.1, §12, §17.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from time import monotonic
from typing import TYPE_CHECKING, Any, AsyncGenerator
from urllib.parse import urlsplit

from charlotte.config import CharlotteConfig
from charlotte.core.adapter_validation import call_with_validation
from charlotte.core.extractor import extract
from charlotte.core.fetcher import PageFetcher, _import_playwright
from charlotte.core.normalizer import normalize_url
from charlotte.core.plausibility import NavDecision, check_plausibility
from charlotte.core.provenance import check_provenance
from charlotte.core.robots import RobotsHandler
from charlotte.core.sanitizer import strip_hidden
from charlotte.exceptions import (
    AdapterOutputError,
    CharlotteConfigError,
    CharlotteNetworkError,
    CharlotteRedirectError,
    CharlotteTimeoutError,
    RobotsError,
)
from charlotte.models import (
    BudgetExhausted,
    CrawlComplete,
    CrawlResult,
    CrawlStarted,
    ModelDecision,
    PageFetched,
    PageSkipped,
    ResultFound,
    StreamEvent,
    VisitLogEntry,
)

if TYPE_CHECKING:
    from charlotte.adapters.base import AdapterProtocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default adapter resolution (spec §5.1, §6.3)
# ---------------------------------------------------------------------------

def _resolve_default_adapter() -> "AdapterProtocol":
    """Instantiate the default adapter from CharlotteConfig (spec §5.1).

    Consults CHARLOTTE_DEFAULT_ADAPTER ('groq' or 'local'). Falls back to
    GroqAdapter. Each constructor raises CharlotteConfigError with a clear
    message if its requirements (e.g. GROQ_API_KEY) are not met.
    """
    adapter_name = CharlotteConfig.default_adapter()
    if adapter_name == "local":
        from charlotte.adapters.local import LocalAdapter
        return LocalAdapter()
    from charlotte.adapters.groq import GroqAdapter
    return GroqAdapter()


# ---------------------------------------------------------------------------
# CHAR-013 — Public crawl() entry point
# ---------------------------------------------------------------------------

def crawl(
    start_url: str,
    goal: str,
    *,
    model: "AdapterProtocol | None" = None,
    max_pages: int = 20,
    max_depth: int = 5,
    max_results: "int | None" = 1,
    confidence_threshold: float = 0.70,
    render_js: bool = False,
    allowed_domains: "list[str] | None" = None,
    return_content: bool = False,
    navigation_hint: "str | None" = None,
    stream: "bool | None" = None,
    respect_robots: "bool | None" = None,
    connect_timeout: float = 10.0,
    read_timeout: float = 30.0,
    render_timeout: float = 15.0,
    default_delay: float = 1.0,
    chromium_executable: "str | None" = None,
) -> "AsyncGenerator[StreamEvent, None] | Any":
    """Navigate toward *goal* starting from *start_url*.

    Args:
        start_url:            Absolute URL at which to begin.
        goal:                 Natural language description of what to find.
        model:                Adapter callable (AdapterProtocol). Raises
                              CharlotteConfigError if None and no default is set.
        max_pages:            Hard ceiling on total pages fetched.
        max_depth:            Maximum link-hops from start_url.
        max_results:          Stop after this many confirmed results; None = collect all.
        confidence_threshold: Minimum model confidence to record a result (0–1).
        render_js:            Use Playwright (headless Chromium) to render pages.
                              Raises CharlotteConfigError if playwright is not installed.
        allowed_domains:      Hostnames Charlotte may visit; defaults to start_url domain.
        return_content:       Include sanitized page text in CrawlResult.content.
        navigation_hint:      Extra context passed to the model alongside the goal.
        stream:               True → return AsyncGenerator of events.
                              False → return coroutine resolving to CrawlResult.
        respect_robots:       Fetch and obey robots.txt before crawling.
        connect_timeout:      TCP connection timeout for HTTP requests (seconds).
        read_timeout:         Response body read timeout (seconds).
        render_timeout:       Seconds to wait for JS to settle after navigation (seconds).
        default_delay:        Floor for the polite inter-request delay (seconds).
        chromium_executable:  Path to a Chromium/Chrome binary. Use when Playwright's
                              bundled Chromium doesn't support the current OS (e.g.
                              Ubuntu 26.04). Ignored when render_js=False.

    Returns:
        AsyncGenerator[StreamEvent, None] when stream=True.
        Coroutine[CrawlResult] when stream=False — use `await crawl(...)`.

    Raises:
        CharlotteConfigError: Invalid configuration (playwright not installed,
                              invalid start_url, or no model provided).
    """
    # Resolve env-var defaults for sentinel parameters (spec §6.3).
    if stream is None:
        stream = CharlotteConfig.stream()
    if respect_robots is None:
        respect_robots = CharlotteConfig.respect_robots()

    if render_js:
        # Check availability before the generator starts — spec §8 requires the
        # error to surface immediately, not on the first iteration.
        _import_playwright()
    if not math.isfinite(render_timeout) or render_timeout <= 0:
        raise CharlotteConfigError(
            f"render_timeout must be a finite positive number, got: {render_timeout!r}"
        )
    if model is None:
        model = _resolve_default_adapter()
    try:
        normalized_start = normalize_url(start_url)
    except CharlotteConfigError as exc:
        raise CharlotteConfigError(f"Invalid start_url: {exc}") from exc

    start_hostname = (urlsplit(normalized_start).hostname or "").lower()
    _domains: frozenset[str]
    if allowed_domains is None:
        # Auto-include the www./non-www counterpart so that apex→www (or www→apex)
        # redirects on the start URL don't immediately raise CharlotteRedirectError.
        if start_hostname.startswith("www."):
            _domains = frozenset({start_hostname, start_hostname[4:]})
        else:
            _domains = frozenset({start_hostname, f"www.{start_hostname}"})
    else:
        _domains = frozenset(d.lower() for d in allowed_domains)

    result_holder: list[CrawlResult] = []
    gen = _crawl_core(
        result_holder=result_holder,
        model=model,
        start_url=normalized_start,
        goal=goal,
        max_pages=max_pages,
        max_depth=max_depth,
        max_results=max_results,
        confidence_threshold=confidence_threshold,
        allowed_domains=_domains,
        return_content=return_content,
        navigation_hint=navigation_hint,
        respect_robots=respect_robots,
        render_js=render_js,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        render_timeout=render_timeout,
        default_delay=default_delay,
        chromium_executable=chromium_executable,
    )

    if stream:
        return gen

    async def _silent() -> CrawlResult:
        async for _ in gen:
            pass
        return result_holder[0]

    return _silent()


# ---------------------------------------------------------------------------
# CHAR-013 — Core crawl loop
# ---------------------------------------------------------------------------

async def _crawl_core(
    *,
    result_holder: list[CrawlResult],
    model: Any,
    start_url: str,
    goal: str,
    max_pages: int,
    max_depth: int,
    max_results: "int | None",
    confidence_threshold: float,
    allowed_domains: frozenset,
    return_content: bool,
    navigation_hint: "str | None",
    respect_robots: bool,
    render_js: bool,
    connect_timeout: float,
    read_timeout: float,
    render_timeout: float,
    default_delay: float,
    chromium_executable: "str | None",
) -> "AsyncGenerator[StreamEvent, None]":
    start_time = monotonic()

    yield CrawlStarted(
        start_url=start_url,
        goal=goal,
        max_pages=max_pages,
        max_depth=max_depth,
        max_results=max_results,
    )

    robots: RobotsHandler | None = RobotsHandler(connect_timeout=connect_timeout) if respect_robots else None
    polite_delay = default_delay

    if robots is not None:
        try:
            polite_delay = await robots.check(start_url, default_delay)
        except RobotsError as exc:
            yield PageSkipped(url=start_url, reason=str(exc), error_type="RobotsError")
            result = _empty_result(budget_exhausted=False)
            result_holder.append(result)
            yield CrawlComplete(
                found=False, result_count=0, pages_visited=0, depth_reached=0,
                elapsed_ms=_elapsed_ms(start_time),
            )
            return

    fetcher = PageFetcher(
        allowed_domains=set(allowed_domains),
        render_js=render_js,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        render_timeout=render_timeout,
        polite_delay=polite_delay,
        chromium_executable=chromium_executable,
    )

    queue: deque[tuple[str, int]] = deque([(start_url, 0)])
    visited: set[str] = set()
    result_urls: list[str] = []
    answers_list: list[str | None] = []
    content_list: list[str] = []
    visit_log: list[VisitLogEntry] = []
    pages_visited = 0
    depth_reached = 0
    best_url: str | None = None
    best_conf: float = 0.0
    depth_budget_used = False

    while queue and pages_visited < max_pages:
        url, depth = queue.popleft()

        try:
            norm = normalize_url(url)
        except CharlotteConfigError:
            continue
        if norm in visited:
            continue
        visited.add(norm)
        depth_reached = max(depth_reached, depth)

        # Per-URL robots gate (handler caches per domain)
        if robots is not None:
            try:
                await robots.check(url, default_delay)
            except RobotsError as exc:
                yield PageSkipped(url=url, reason=str(exc), error_type="RobotsError")
                continue

        # Fetch
        try:
            # Exclude the current URL from visited so its own canonical redirect
            # (e.g. /path → /path/) is not mistaken for a cross-crawl revisit.
            page = await fetcher.fetch(url, visited_urls=visited - {norm})
        except CharlotteTimeoutError as exc:
            yield PageSkipped(url=url, reason=str(exc), error_type="CharlotteTimeoutError")
            continue
        except (CharlotteNetworkError, CharlotteRedirectError) as exc:
            yield PageSkipped(url=url, reason=str(exc), error_type=type(exc).__name__)
            continue
        except Exception as exc:
            yield PageSkipped(url=url, reason=f"Unexpected error: {type(exc).__name__}", error_type=None)
            continue

        pages_visited += 1
        yield PageFetched(url=page.url, depth=depth, http_status=page.status_code, fetch_ms=page.fetch_ms)

        # Sanitize → extract (no domain filter — model sees all observable links;
        # navigation is restricted at the enqueue step below)
        clean = strip_hidden(page.html)
        extracted = extract(clean, page_url=page.url)

        # Model call — include the current page in history so the model
        # doesn't recommend it as a next step when it's already standing on it.
        history = [e.url for e in visit_log[-10:]] + [page.url]
        try:
            output = await call_with_validation(
                model,
                goal=goal,
                navigation_hint=navigation_hint,
                page_title=extracted.title,
                page_url=page.url,
                page_summary=extracted.text,
                available_links=extracted.links,
                visit_history=history,
                results_so_far=len(result_urls),
            )
        except AdapterOutputError as exc:
            yield PageSkipped(url=page.url, reason=str(exc), error_type="AdapterOutputError")
            continue

        # Plausibility check — runs on raw model output, before provenance.
        # Spec §9.3 (plausibility) precedes §9.4 (provenance). Using raw output
        # keeps the navigation quality check independent of the security check so
        # a provenance rejection cannot cascade into a plausibility skip.
        raw_decision = NavDecision(
            found=output.found,
            confidence=output.confidence,
            result_url=output.result_url,
            links_to_follow=output.links_to_follow,
            reasoning=output.reasoning,
        )
        plaus = check_plausibility(
            decision=raw_decision,
            page_text=extracted.text,
            visited_urls=visited,
        )
        if not plaus.passed:
            reason = "; ".join(f.detail for f in plaus.flags)
            model_summary = f"model: found={output.found}, conf={output.confidence:.2f}"
            yield PageSkipped(url=page.url, reason=f"Plausibility ({model_summary}): {reason}", error_type=None)
            continue

        # Provenance check — current page URL is also "observed" by the model,
        # so it is valid as a result_url even if not present as a link on the page.
        # extracted_link_urls includes off-domain links; allowed_domains restricts
        # navigation (enqueueing) only — CrawlResult.result_urls may contain
        # off-domain hosts when the goal is to find an external URL.
        extracted_link_urls = [page.url] + [link["url"] for link in extracted.links]
        # For fact goals (answer != None) the result lives on the current page.
        # Override result_url to page.url BEFORE provenance so the check always
        # passes — models reliably hallucinate result_url on fact goals while
        # correctly extracting the answer value. page.url is always in
        # extracted_link_urls so provenance will accept it.
        provenance_result_url = (
            page.url
            if (output.found and output.answer is not None)
            else output.result_url
        )
        prov = check_provenance(
            found=output.found,
            result_url=provenance_result_url,
            links_to_follow=output.links_to_follow,
            extracted_urls=extracted_link_urls,
        )
        effective_found = output.found and prov.result_url_accepted
        effective_result_url = provenance_result_url if effective_found else None
        effective_links = prov.links_to_follow

        if not prov.result_url_accepted and prov.rejection_detail:
            logger.debug("Provenance rejection at %r: %s", page.url, prov.rejection_detail)

        visit_log.append(VisitLogEntry(
            url=page.url,
            depth=depth,
            found=effective_found,
            confidence=output.confidence,
            reasoning=output.reasoning,
        ))

        # Enqueue confirmed links
        enqueued = 0
        for link_url in effective_links:
            next_depth = depth + 1
            if next_depth > max_depth:
                depth_budget_used = True
                continue
            try:
                norm_link = normalize_url(link_url)
            except CharlotteConfigError:
                continue
            link_host = (urlsplit(norm_link).hostname or "").lower()
            if link_host not in allowed_domains:
                continue
            if norm_link in visited:
                continue
            queue.append((link_url, next_depth))
            enqueued += 1

        yield ModelDecision(
            url=page.url,
            found=effective_found,
            confidence=output.confidence,
            links_queued=enqueued,
            reasoning=output.reasoning,
            links_available=extracted.links,
            links_suggested=output.links_to_follow,
        )

        if effective_found and output.confidence >= confidence_threshold:
            result_urls.append(effective_result_url)  # type: ignore[arg-type]
            answers_list.append(output.answer)
            if return_content:
                content_list.append(extracted.text)
            yield ResultFound(
                url=effective_result_url,  # type: ignore[arg-type]
                confidence=output.confidence,
                result_index=len(result_urls),
                answer=output.answer,
            )
            if max_results is not None and len(result_urls) >= max_results:
                break
        elif output.confidence > best_conf:
            best_conf = output.confidence
            best_url = output.result_url or page.url

    # Budget exhaustion: page limit hit with items remaining, or depth cap triggered
    stopped_at_limit = pages_visited >= max_pages and bool(queue)
    budget_exhausted = stopped_at_limit or depth_budget_used

    found = bool(result_urls)
    if not found and budget_exhausted:
        yield BudgetExhausted(
            pages_visited=pages_visited,
            depth_reached=depth_reached,
            best_candidate=best_url,
        )

    result = CrawlResult(
        found=found,
        result_urls=result_urls,
        content=content_list if return_content else None,
        confidence=max((e.confidence for e in visit_log if e.found), default=best_conf),
        pages_visited=pages_visited,
        depth_reached=depth_reached,
        visit_log=visit_log,
        best_candidate_url=best_url if not found else None,
        budget_exhausted=budget_exhausted,
        answers=answers_list if found else None,
    )
    result_holder.append(result)

    yield CrawlComplete(
        found=found,
        result_count=len(result_urls),
        pages_visited=pages_visited,
        depth_reached=depth_reached,
        elapsed_ms=_elapsed_ms(start_time),
    )


def _empty_result(*, budget_exhausted: bool) -> CrawlResult:
    return CrawlResult(
        found=False,
        result_urls=[],
        content=None,
        confidence=0.0,
        pages_visited=0,
        depth_reached=0,
        visit_log=[],
        best_candidate_url=None,
        budget_exhausted=budget_exhausted,
    )


def _elapsed_ms(start: float) -> int:
    return int((monotonic() - start) * 1000)
