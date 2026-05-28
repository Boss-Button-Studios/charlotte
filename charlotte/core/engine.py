"""
Crawl engine — adapter output validation (CHAR-006) and crawl loop (CHAR-013).

CHAR-006: validate adapter output against spec §6.5; call_with_validation.
CHAR-013: crawl() public function; full BFS loop; streaming events; budget.

See spec §4, §5.1, §6.5, §12, §17.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any, AsyncGenerator
from urllib.parse import urlparse, urlsplit

from charlotte.core.extractor import extract
from charlotte.core.fetcher import PageFetcher
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

# Injected into the adapter prompt on retry when the first response fails
# schema validation. Restates all field requirements explicitly. See §6.5.
_SCHEMA_HINT = (
    "Your previous response did not match the required output schema. "
    "You MUST return a JSON object with exactly these fields: "
    '"found" (boolean), '
    '"confidence" (float between 0.0 and 1.0 inclusive), '
    '"result_url" (non-null URL string when found=true, null when found=false), '
    '"links_to_follow" (array of URL strings, may be empty), '
    '"reasoning" (non-empty string). '
    "No extra fields. Respond with JSON only — no prose outside the object."
)


@dataclass
class AdapterOutput:
    """Validated adapter output. All fields are guaranteed clean and correct.

    Produced by call_with_validation(). The engine acts only on AdapterOutput,
    never on the raw dict returned by the adapter. See spec §6.5.
    """

    found: bool
    confidence: float
    result_url: str | None   # Non-null iff found=True
    links_to_follow: list[str]
    reasoning: str


def _is_valid_url(value: object) -> bool:
    """Return True if value is a non-empty http/https URL string."""
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def validate_adapter_output(raw: object) -> AdapterOutput:
    """Validate raw adapter output against the schema defined in spec §6.5.

    Checks all five required fields for presence, correct types, and
    constraint satisfaction. Invalid links_to_follow items are silently
    dropped; all other violations raise ValueError.

    Args:
        raw: The value returned by the adapter (expected to be a dict).

    Returns:
        AdapterOutput with all fields validated and cleaned.

    Raises:
        ValueError: Describes the first constraint violation found.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"adapter output must be a dict, got {type(raw).__name__}")

    # --- found ---
    if "found" not in raw:
        raise ValueError("missing required field: 'found'")
    found = raw["found"]
    if not isinstance(found, bool):
        raise ValueError(f"'found' must be a boolean, got {type(found).__name__}")

    # --- confidence ---
    if "confidence" not in raw:
        raise ValueError("missing required field: 'confidence'")
    confidence = raw["confidence"]
    if not isinstance(confidence, (int, float)):
        raise ValueError(f"'confidence' must be a float, got {type(confidence).__name__}")
    confidence = float(confidence)
    if not (0.0 <= confidence <= 1.0):
        raise ValueError(f"'confidence' must be in [0.0, 1.0], got {confidence}")

    # --- result_url ---
    if "result_url" not in raw:
        raise ValueError("missing required field: 'result_url'")
    result_url = raw["result_url"]
    if found:
        if result_url is None:
            raise ValueError("'result_url' must not be null when 'found' is true")
        if not _is_valid_url(result_url):
            raise ValueError(f"'result_url' is not a valid URL: {result_url!r}")
    else:
        if result_url is not None:
            raise ValueError("'result_url' must be null when 'found' is false")

    # --- links_to_follow ---
    if "links_to_follow" not in raw:
        raise ValueError("missing required field: 'links_to_follow'")
    raw_links = raw["links_to_follow"]
    if not isinstance(raw_links, list):
        raise ValueError(
            f"'links_to_follow' must be a list, got {type(raw_links).__name__}"
        )
    # Invalid URL items are silently dropped; the response is not rejected.
    links_to_follow = [item for item in raw_links if _is_valid_url(item)]

    # --- reasoning ---
    if "reasoning" not in raw:
        raise ValueError("missing required field: 'reasoning'")
    reasoning = raw["reasoning"]
    if not isinstance(reasoning, str):
        raise ValueError(f"'reasoning' must be a string, got {type(reasoning).__name__}")
    if not reasoning.strip():
        raise ValueError("'reasoning' must not be empty or whitespace-only")

    return AdapterOutput(
        found=found,
        confidence=confidence,
        result_url=result_url if found else None,
        links_to_follow=links_to_follow,
        reasoning=reasoning,
    )


async def call_with_validation(
    adapter: "AdapterProtocol",
    *,
    goal: str,
    navigation_hint: str | None,
    page_title: str,
    page_url: str,
    page_summary: str,
    available_links: list[dict[str, str]],
    visit_history: list[str],
    results_so_far: int,
) -> AdapterOutput:
    """Call an adapter, validate its output, and retry once with a schema hint.

    On the first schema validation failure, the adapter is called a second time
    with a schema reminder injected into the prompt (T-09). If the second
    response also fails validation, AdapterOutputError is raised and the caller
    should treat the page as unevaluable (T-10). See spec §6.5.

    If the adapter itself raises AdapterOutputError (e.g., API failure), that
    exception is re-raised immediately without a schema retry.

    Args:
        adapter: Any object satisfying AdapterProtocol.
        goal, navigation_hint, page_title, page_url, page_summary,
        available_links, visit_history, results_so_far: Page context passed
            directly to the adapter unchanged.

    Returns:
        Validated AdapterOutput ready for the engine to act on.

    Raises:
        AdapterOutputError: Adapter raised an exception, or both validation
            attempts failed.
    """
    common: dict = dict(
        goal=goal,
        navigation_hint=navigation_hint,
        page_title=page_title,
        page_url=page_url,
        page_summary=page_summary,
        available_links=available_links,
        visit_history=visit_history,
        results_so_far=results_so_far,
    )

    # First attempt — no schema hint
    try:
        raw = await adapter(schema_hint=None, **common)
    except AdapterOutputError:
        raise
    except Exception as exc:
        raise AdapterOutputError("Adapter call failed before validation") from exc
    try:
        return validate_adapter_output(raw)
    except ValueError:
        pass  # Fall through to retry with reinforced schema hint

    # Second attempt — reinforced schema hint (T-09 path succeeds here)
    try:
        raw = await adapter(schema_hint=_SCHEMA_HINT, **common)
    except AdapterOutputError:
        raise
    except Exception as exc:
        raise AdapterOutputError("Adapter retry failed before validation") from exc
    try:
        return validate_adapter_output(raw)
    except ValueError as exc:
        # Both attempts failed (T-10) — treat page as unevaluable
        raise AdapterOutputError(
            f"Adapter output failed schema validation after two attempts: {exc}"
        ) from exc


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
    confidence_threshold: float = 0.85,
    render_js: bool = False,
    allowed_domains: "list[str] | None" = None,
    return_content: bool = False,
    navigation_hint: "str | None" = None,
    stream: bool = True,
    respect_robots: bool = True,
    connect_timeout: float = 10.0,
    read_timeout: float = 30.0,
    default_delay: float = 1.0,
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
        render_js:            Not supported yet — raises CharlotteConfigError.
        allowed_domains:      Hostnames Charlotte may visit; defaults to start_url domain.
        return_content:       Include sanitized page text in CrawlResult.content.
        navigation_hint:      Extra context passed to the model alongside the goal.
        stream:               True → return AsyncGenerator of events.
                              False → return coroutine resolving to CrawlResult.
        respect_robots:       Fetch and obey robots.txt before crawling.
        connect_timeout:      TCP connection timeout for HTTP requests (seconds).
        read_timeout:         Response body read timeout (seconds).
        default_delay:        Floor for the polite inter-request delay (seconds).

    Returns:
        AsyncGenerator[StreamEvent, None] when stream=True.
        Coroutine[CrawlResult] when stream=False — use `await crawl(...)`.

    Raises:
        CharlotteConfigError: Invalid configuration (unsupported render_js,
                              invalid start_url, or no model provided).
    """
    if render_js:
        raise CharlotteConfigError(
            "render_js=True requires Playwright (CHAR-015). "
            "Install it with: pip install 'charlotte-crawler[playwright]'."
        )
    if model is None:
        raise CharlotteConfigError(
            "No model adapter provided. Pass model=LocalAdapter() or model=GroqAdapter()."
        )
    try:
        normalized_start = normalize_url(start_url)
    except CharlotteConfigError as exc:
        raise CharlotteConfigError(f"Invalid start_url: {exc}") from exc

    start_hostname = (urlsplit(normalized_start).hostname or "").lower()
    _domains: frozenset[str]
    if allowed_domains is None:
        _domains = frozenset({start_hostname})
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
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        default_delay=default_delay,
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
    connect_timeout: float,
    read_timeout: float,
    default_delay: float,
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
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        polite_delay=polite_delay,
    )

    queue: deque[tuple[str, int]] = deque([(start_url, 0)])
    visited: set[str] = set()
    result_urls: list[str] = []
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
            page = await fetcher.fetch(url, visited_urls=visited)
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

        # Sanitize → extract
        clean = strip_hidden(page.html)
        extracted = extract(clean, page_url=page.url, allowed_domains=set(allowed_domains))

        # Model call
        history = [e.url for e in visit_log[-10:]]
        try:
            output = await call_with_validation(
                model,
                goal=goal,
                navigation_hint=navigation_hint,
                page_title="",
                page_url=page.url,
                page_summary=extracted.text,
                available_links=extracted.links,
                visit_history=history,
                results_so_far=len(result_urls),
            )
        except AdapterOutputError as exc:
            yield PageSkipped(url=page.url, reason=str(exc), error_type="AdapterOutputError")
            continue

        # Provenance check — current page URL is also "observed" by the model,
        # so it is valid as a result_url even if not present as a link on the page.
        extracted_link_urls = [page.url] + [link["url"] for link in extracted.links]
        prov = check_provenance(
            found=output.found,
            result_url=output.result_url,
            links_to_follow=output.links_to_follow,
            extracted_urls=extracted_link_urls,
        )
        effective_found = output.found and prov.result_url_accepted
        effective_result_url = output.result_url if effective_found else None
        effective_links = prov.links_to_follow

        # Plausibility check
        decision = NavDecision(
            found=effective_found,
            confidence=output.confidence,
            result_url=effective_result_url,
            links_to_follow=effective_links,
            reasoning=output.reasoning,
        )
        plaus = check_plausibility(
            decision=decision,
            page_text=extracted.text,
            allowed_domains=allowed_domains,
            visited_urls=visited,
        )
        if not plaus.passed:
            reason = "; ".join(f.detail for f in plaus.flags)
            yield PageSkipped(url=page.url, reason=f"Plausibility: {reason}", error_type=None)
            continue

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
        )

        if effective_found and output.confidence >= confidence_threshold:
            result_urls.append(effective_result_url)  # type: ignore[arg-type]
            if return_content:
                content_list.append(extracted.text)
            yield ResultFound(
                url=effective_result_url,  # type: ignore[arg-type]
                confidence=output.confidence,
                result_index=len(result_urls),
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
