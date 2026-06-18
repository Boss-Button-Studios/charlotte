"""Public crawl() entry point — config validation, component wiring, domain scoping,
and stream/non-stream dispatch. The priority crawl loop itself lives in
``engine_loop._crawl_core``; small shared helpers live in ``engine_support``.
See spec §4, §5.1."""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncGenerator
from urllib.parse import urlsplit

from charlotte.config import CharlotteConfig
from charlotte.core.candidate_extractor import DefaultCandidateExtractor
from charlotte.core.destination_verifier import DefaultDestinationVerifier
from charlotte.core.engine_support import (
    _resolve_default_adapter,
)
from charlotte.core.engine_loop import _crawl_core
from charlotte.core.fetcher import _import_playwright
from charlotte.core.goal_context_cache import AutoPreprocessor
from charlotte.core.link_ranker import BM25LinkRanker
from charlotte.core.normalizer import normalize_url, validate_url_safety
from charlotte.exceptions import (
    CharlotteConfigError,
    CharlotteSSRFError,
)
from charlotte.models import (
    CrawlResult,
    StreamEvent,
)

if TYPE_CHECKING:
    from charlotte.adapters.base import AdapterProtocol
    from charlotte.core.candidate_extractor import CandidateExtractorProtocol
    from charlotte.core.destination_verifier import DestinationVerifierProtocol
    from charlotte.core.goal_preprocessor import GoalPreprocessorProtocol
    from charlotte.core.link_ranker import LinkRankerProtocol

logger = logging.getLogger(__name__)


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
    max_response_bytes: int = 10 * 1024 * 1024,
    user_agent: "str | None" = None,
    preprocessor: "GoalPreprocessorProtocol | None" = None,
    ranker: "LinkRankerProtocol | None" = None,
    locale: str = "en_US",
    candidate_extractor: "CandidateExtractorProtocol | None" = None,
    verifier: "DestinationVerifierProtocol | None" = None,
    verify_destination: str = "relevance",
    verify_threshold: float = 0.3,
    fetch_result_content: "bool | None" = None,
    max_result_bytes: int = 10_485_760,
    result_to_file: "Path | None" = None,
) -> "AsyncGenerator[StreamEvent, None] | Any":
    """Navigate toward *goal* starting from *start_url*. See spec §4, §5.1.

    Key args:
        model:               Adapter callable. None → CHARLOTTE_DEFAULT_ADAPTER.
        max_pages:           Page budget ceiling.
        max_results:         Stop after N results; None = collect all.
        verify_destination:  "off" / "existence" / "relevance" (default) / "full".
        verify_threshold:    BM25/embedding threshold (default 0.3). See spec §7.3.
        fetch_result_content: Capture bytes per result. None = on for document_link.
        result_to_file:      Directory for file-based content delivery. See spec §7.7.
        stream:              True → AsyncGenerator; False → Coroutine[CrawlResult].

    Raises:
        CharlotteConfigError: Bad config (no model, invalid URL, playwright absent).
    """
    if stream is None:
        stream = CharlotteConfig.stream()
    if respect_robots is None:
        respect_robots = CharlotteConfig.respect_robots()

    if render_js:
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

    try:
        validate_url_safety(normalized_start)
    except CharlotteSSRFError:
        raise

    resolved_user_agent = user_agent if user_agent is not None else CharlotteConfig.user_agent()

    _preprocessor = preprocessor or AutoPreprocessor()
    _ranker = ranker or BM25LinkRanker()
    _extractor = candidate_extractor or DefaultCandidateExtractor()
    _verifier = verifier or DefaultDestinationVerifier(
        mode=verify_destination,
        verify_threshold=verify_threshold,
        fetch_result_content=fetch_result_content,
        max_result_bytes=max_result_bytes,
        result_to_file=result_to_file,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        user_agent=resolved_user_agent,
    )

    start_hostname = (urlsplit(normalized_start).hostname or "").lower()
    if allowed_domains is None:
        # Strip a leading "www." to get the registrant-level base domain, then
        # allow any subdomain of it.  This lets a crawl starting at www.python.org
        # follow links to docs.python.org, peps.python.org, etc. without requiring
        # an explicit allowed_domains list.  Stripping only "www." (not deeper
        # labels) keeps multi-tenant hosting domains safe: user.github.io stays
        # scoped to user.github.io subdomains, not all *.github.io.
        base = start_hostname[4:] if start_hostname.startswith("www.") else start_hostname
        _domains: frozenset[str] = frozenset({base})
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
        max_response_bytes=max_response_bytes,
        user_agent=resolved_user_agent,
        preprocessor=_preprocessor,
        ranker=_ranker,
        candidate_extractor=_extractor,
        verifier=_verifier,
        locale=locale,
    )

    if stream:
        return gen

    async def _silent() -> CrawlResult:
        async for _ in gen:
            pass
        return result_holder[0]

    return _silent()

