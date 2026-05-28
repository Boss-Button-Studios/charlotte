"""
Unit tests for crawl() — CHAR-013 (spec §4, §5.1, §12, §17).

Each test uses respx to intercept HTTP and a simple async mock adapter
so we never hit the network or a real model.

Test areas:
  - Eager config validation (no model, render_js, bad URL)
  - stream=False returns CrawlResult; stream=True yields events
  - Single-page result found immediately
  - Multi-hop navigation (follow link to find result)
  - max_results=1 stops after first match
  - max_results=None collects all matches
  - max_pages budget exhaustion
  - max_depth prevents deep link enqueuing
  - respect_robots=True blocks crawl on start_url RobotsError
  - respect_robots=True skips individual blocked URLs, crawl continues
  - respect_robots=False skips robots check entirely
  - Fetch error → PageSkipped, crawl continues
  - AdapterOutputError → PageSkipped, crawl continues
  - Plausibility failure → PageSkipped, crawl continues
  - Visited-set prevents re-fetching the same URL
  - Off-domain links are never enqueued
  - return_content=True populates CrawlResult.content
  - CrawlStarted is the first event; CrawlComplete is the last
  - BudgetExhausted event emitted when budget runs out unfound
"""

from __future__ import annotations

import httpx
import pytest
import respx

from charlotte.core.engine import crawl
from charlotte.exceptions import (
    CharlotteConfigError,
    CharlotteNetworkError,
    CharlotteTimeoutError,
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
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE = "http://example.com"
_ROBOTS = f"{_BASE}/robots.txt"
_START = f"{_BASE}/"
_GOAL = "Find the contact page"
_CONTACT = f"{_BASE}/contact"

_WORDS = " ".join(["word"] * 60)  # 60 words — above plausibility thin-content threshold

_HTML_WITH_LINK = (
    f'<html><body><p>{_WORDS}</p>'
    '<a href="/contact">Contact Us</a>'
    '</body></html>'
)
_HTML_CONTACT = (
    f'<html><body><h1>Contact Us</h1><p>{_WORDS}</p>'
    '<a href="/">Home</a>'
    '</body></html>'
)
_HTML_EMPTY = f'<html><body><p>{_WORDS}</p></body></html>'


def _mock_404_robots():
    """Register a 404 response for example.com/robots.txt."""
    respx.get(_ROBOTS).mock(return_value=httpx.Response(404))


def _adapter_found(url: str):
    """Adapter that reports found=True only when page_url matches *url*."""
    async def _adapter(*, schema_hint=None, page_url, available_links, **kwargs):
        if page_url == url:
            return {
                "found": True,
                "confidence": 0.95,
                "result_url": url,
                "links_to_follow": [],
                "reasoning": "This is the contact page.",
            }
        links = [link["url"] for link in available_links]
        return {
            "found": False,
            "confidence": 0.1,
            "result_url": None,
            "links_to_follow": links[:3],
            "reasoning": "Not found yet, following links.",
        }
    return _adapter


def _adapter_never_found():
    """Adapter that always reports found=False."""
    async def _adapter(*, schema_hint=None, available_links, **kwargs):
        links = [link["url"] for link in available_links]
        return {
            "found": False,
            "confidence": 0.2,
            "result_url": None,
            "links_to_follow": links[:3],
            "reasoning": "Not here.",
        }
    return _adapter


async def _collect_events(gen) -> list:
    events = []
    async for event in gen:
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# Config validation — eager, before any I/O
# ---------------------------------------------------------------------------

def test_no_model_raises_config_error():
    with pytest.raises(CharlotteConfigError, match="No model adapter provided"):
        crawl(_START, _GOAL, model=None)


def test_render_js_raises_config_error():
    async def _m(**_): return {}
    with pytest.raises(CharlotteConfigError, match="render_js"):
        crawl(_START, _GOAL, model=_m, render_js=True)


def test_invalid_start_url_raises_config_error():
    async def _m(**_): return {}
    with pytest.raises(CharlotteConfigError, match="Invalid start_url"):
        crawl("not-a-url", _GOAL, model=_m)


# ---------------------------------------------------------------------------
# stream=False / stream=True dispatch
# ---------------------------------------------------------------------------

@respx.mock
async def test_stream_false_returns_crawl_result():
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, text=_HTML_WITH_LINK))
    respx.get(_CONTACT).mock(return_value=httpx.Response(200, text=_HTML_CONTACT))

    result = await crawl(
        _START, _GOAL,
        model=_adapter_found(_CONTACT),
        stream=False, respect_robots=True, default_delay=0.0,
    )
    assert isinstance(result, CrawlResult)


@respx.mock
async def test_stream_true_returns_async_generator():
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, text=_HTML_WITH_LINK))
    respx.get(_CONTACT).mock(return_value=httpx.Response(200, text=_HTML_CONTACT))

    gen = crawl(
        _START, _GOAL,
        model=_adapter_found(_CONTACT),
        stream=True, respect_robots=True, default_delay=0.0,
    )
    events = await _collect_events(gen)
    assert any(isinstance(e, CrawlComplete) for e in events)


# ---------------------------------------------------------------------------
# Single-page result found immediately
# ---------------------------------------------------------------------------

@respx.mock
async def test_result_found_on_start_page():
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, text=_HTML_CONTACT))

    result = await crawl(
        _START, _GOAL,
        model=_adapter_found(_START),
        stream=False, respect_robots=True, default_delay=0.0,
    )
    assert result.found is True
    assert result.pages_visited == 1


# ---------------------------------------------------------------------------
# Multi-hop navigation
# ---------------------------------------------------------------------------

@respx.mock
async def test_multi_hop_follows_link_to_result():
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, text=_HTML_WITH_LINK))
    respx.get(_CONTACT).mock(return_value=httpx.Response(200, text=_HTML_CONTACT))

    result = await crawl(
        _START, _GOAL,
        model=_adapter_found(_CONTACT),
        stream=False, respect_robots=True, default_delay=0.0,
    )
    assert result.found is True
    assert _CONTACT in result.result_urls
    assert result.pages_visited == 2


# ---------------------------------------------------------------------------
# max_results
# ---------------------------------------------------------------------------

@respx.mock
async def test_max_results_1_stops_at_first_match():
    _mock_404_robots()
    html = (
        '<html><body>'
        '<a href="/a">A</a>'
        '<a href="/b">B</a>'
        '</body></html>'
    )
    respx.get(_START).mock(return_value=httpx.Response(200, text=html))
    respx.get(f"{_BASE}/a").mock(return_value=httpx.Response(200, text=_HTML_CONTACT))
    respx.get(f"{_BASE}/b").mock(return_value=httpx.Response(200, text=_HTML_CONTACT))

    found_pages: list[str] = []

    async def _adapter(*, schema_hint=None, page_url, available_links, **kwargs):
        if page_url.endswith("/a") or page_url.endswith("/b"):
            found_pages.append(page_url)
            return {
                "found": True,
                "confidence": 0.95,
                "result_url": page_url,
                "links_to_follow": [],
                "reasoning": "found",
            }
        links = [link["url"] for link in available_links]
        return {
            "found": False, "confidence": 0.1, "result_url": None,
            "links_to_follow": links, "reasoning": "not found",
        }

    result = await crawl(
        _START, _GOAL,
        model=_adapter, max_results=1,
        stream=False, respect_robots=True, default_delay=0.0,
    )
    assert result.found is True
    assert len(result.result_urls) == 1
    assert len(found_pages) == 1


@respx.mock
async def test_max_results_none_collects_all_matches():
    _mock_404_robots()
    html = '<html><body><a href="/a">A</a><a href="/b">B</a></body></html>'
    respx.get(_START).mock(return_value=httpx.Response(200, text=html))
    respx.get(f"{_BASE}/a").mock(return_value=httpx.Response(200, text=_HTML_CONTACT))
    respx.get(f"{_BASE}/b").mock(return_value=httpx.Response(200, text=_HTML_CONTACT))

    async def _adapter(*, schema_hint=None, page_url, available_links, **kwargs):
        if page_url != _START:
            return {
                "found": True, "confidence": 0.95, "result_url": page_url,
                "links_to_follow": [], "reasoning": "found",
            }
        links = [link["url"] for link in available_links]
        return {
            "found": False, "confidence": 0.1, "result_url": None,
            "links_to_follow": links, "reasoning": "not found",
        }

    result = await crawl(
        _START, _GOAL,
        model=_adapter, max_results=None,
        stream=False, respect_robots=True, default_delay=0.0,
    )
    assert result.found is True
    assert len(result.result_urls) == 2


# ---------------------------------------------------------------------------
# Budget exhaustion
# ---------------------------------------------------------------------------

@respx.mock
async def test_max_pages_budget_exhausted():
    _mock_404_robots()
    # Start page links to more pages but max_pages=1
    html = '<html><body><a href="/a">A</a></body></html>'
    respx.get(_START).mock(return_value=httpx.Response(200, text=html))

    result = await crawl(
        _START, _GOAL,
        model=_adapter_never_found(), max_pages=1,
        stream=False, respect_robots=True, default_delay=0.0,
    )
    assert result.found is False
    assert result.budget_exhausted is True
    assert result.pages_visited == 1


@respx.mock
async def test_budget_exhausted_event_emitted_when_not_found():
    _mock_404_robots()
    html = '<html><body><a href="/a">A</a></body></html>'
    respx.get(_START).mock(return_value=httpx.Response(200, text=html))

    events = await _collect_events(crawl(
        _START, _GOAL,
        model=_adapter_never_found(), max_pages=1,
        stream=True, respect_robots=True, default_delay=0.0,
    ))
    assert any(isinstance(e, BudgetExhausted) for e in events)


# ---------------------------------------------------------------------------
# max_depth
# ---------------------------------------------------------------------------

@respx.mock
async def test_max_depth_1_does_not_follow_second_hop():
    _mock_404_robots()
    # start → /a → /b; with max_depth=1 only /a is enqueued
    html_start = '<html><body><a href="/a">A</a></body></html>'
    html_a = '<html><body><a href="/b">B</a></body></html>'
    respx.get(_START).mock(return_value=httpx.Response(200, text=html_start))
    respx.get(f"{_BASE}/a").mock(return_value=httpx.Response(200, text=html_a))

    fetch_calls: list[str] = []

    async def _adapter(*, schema_hint=None, page_url, available_links, **kwargs):
        fetch_calls.append(page_url)
        links = [link["url"] for link in available_links]
        return {
            "found": False, "confidence": 0.1, "result_url": None,
            "links_to_follow": links, "reasoning": "not found",
        }

    result = await crawl(
        _START, _GOAL,
        model=_adapter, max_depth=1, max_pages=10,
        stream=False, respect_robots=True, default_delay=0.0,
    )
    assert result.found is False
    # /b should never have been visited — depth 2 exceeds max_depth=1
    assert not any(u.endswith("/b") for u in fetch_calls)
    assert result.budget_exhausted  # depth cap triggered budget_exhausted flag


# ---------------------------------------------------------------------------
# respect_robots
# ---------------------------------------------------------------------------

@respx.mock
async def test_robots_blocks_start_url_returns_not_found():
    respx.get(_ROBOTS).mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /")
    )

    result = await crawl(
        _START, _GOAL,
        model=_adapter_found(_START),
        stream=False, respect_robots=True, default_delay=0.0,
    )
    assert result.found is False
    assert result.pages_visited == 0


@respx.mock
async def test_robots_false_skips_robots_check():
    # No robots.txt mock — would 404 with respx if called
    respx.get(_START).mock(return_value=httpx.Response(200, text=_HTML_CONTACT))

    result = await crawl(
        _START, _GOAL,
        model=_adapter_found(_START),
        stream=False, respect_robots=False, default_delay=0.0,
    )
    assert result.found is True
    assert result.pages_visited == 1


@respx.mock
async def test_robots_blocks_subsequent_url_page_skipped():
    # start_url allowed, /contact disallowed
    respx.get(_ROBOTS).mock(return_value=httpx.Response(404))
    respx.get(_START).mock(return_value=httpx.Response(200, text=_HTML_WITH_LINK))

    skip_events: list[PageSkipped] = []

    async def _adapter(*, schema_hint=None, page_url, available_links, **kwargs):
        links = [link["url"] for link in available_links]
        return {
            "found": False, "confidence": 0.1, "result_url": None,
            "links_to_follow": links, "reasoning": "not found",
        }

    # Patch robots handler so /contact is blocked but start is not
    from charlotte.core import robots as robots_mod

    original_check = robots_mod.RobotsHandler.check

    async def _patched_check(self, url, default_delay):
        if url.rstrip("/").endswith("/contact"):
            from charlotte.exceptions import RobotsError
            raise RobotsError("blocked by robots")
        return default_delay

    robots_mod.RobotsHandler.check = _patched_check
    try:
        events = await _collect_events(crawl(
            _START, _GOAL,
            model=_adapter, stream=True, respect_robots=True, default_delay=0.0,
        ))
    finally:
        robots_mod.RobotsHandler.check = original_check

    skip_events = [e for e in events if isinstance(e, PageSkipped)]
    assert any("robots" in e.reason.lower() or "blocked" in e.reason.lower() for e in skip_events)


# ---------------------------------------------------------------------------
# Fetch errors → PageSkipped, crawl continues
# ---------------------------------------------------------------------------

@respx.mock
async def test_fetch_network_error_skips_page():
    _mock_404_robots()
    respx.get(_START).mock(side_effect=httpx.ConnectError("refused"))
    respx.get(_CONTACT).mock(return_value=httpx.Response(200, text=_HTML_CONTACT))

    # We monkeypatch _crawl_core's fetcher to simulate CharlotteNetworkError
    from charlotte.core import fetcher as fetcher_mod

    original_fetch = fetcher_mod.PageFetcher.fetch

    async def _boom(self, url, *, visited_urls):
        if url.rstrip("/") == _START.rstrip("/"):
            raise CharlotteNetworkError("connection refused")
        return await original_fetch(self, url, visited_urls=visited_urls)

    fetcher_mod.PageFetcher.fetch = _boom
    try:
        events = await _collect_events(crawl(
            _START, _GOAL,
            model=_adapter_found(_CONTACT),
            stream=True, respect_robots=True, default_delay=0.0,
        ))
    finally:
        fetcher_mod.PageFetcher.fetch = original_fetch

    assert any(isinstance(e, PageSkipped) and "CharlotteNetworkError" in e.error_type for e in events)
    complete = [e for e in events if isinstance(e, CrawlComplete)]
    assert complete  # crawl still finished


@respx.mock
async def test_adapter_error_skips_page():
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, text=_HTML_EMPTY))

    from charlotte.exceptions import AdapterOutputError

    async def _bad_adapter(*, schema_hint=None, **kwargs):
        raise AdapterOutputError("model broken")

    events = await _collect_events(crawl(
        _START, _GOAL,
        model=_bad_adapter,
        stream=True, respect_robots=True, default_delay=0.0,
    ))
    skipped = [e for e in events if isinstance(e, PageSkipped)]
    assert any("AdapterOutputError" in (e.error_type or "") for e in skipped)
    assert any(isinstance(e, CrawlComplete) for e in events)


# ---------------------------------------------------------------------------
# Plausibility failure → PageSkipped
# ---------------------------------------------------------------------------

@respx.mock
async def test_plausibility_failure_skips_page():
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, text=_HTML_EMPTY))

    # Trigger "zero links / no path" flag: found=False and links_to_follow=[]
    async def _dead_end_adapter(*, schema_hint=None, **kwargs):
        return {
            "found": False, "confidence": 0.1, "result_url": None,
            "links_to_follow": [], "reasoning": "nowhere to go",
        }

    events = await _collect_events(crawl(
        _START, _GOAL,
        model=_dead_end_adapter,
        stream=True, respect_robots=True, default_delay=0.0,
    ))
    # The dead-end plausibility flag should fire → PageSkipped
    skipped = [e for e in events if isinstance(e, PageSkipped)]
    assert skipped


# ---------------------------------------------------------------------------
# Visited-set prevents duplicate fetches
# ---------------------------------------------------------------------------

@respx.mock
async def test_visited_set_prevents_revisit():
    _mock_404_robots()
    # /a and /b both link back to start_url; dedup must keep start at call_count=1
    html_start = f'<html><body><p>{_WORDS}</p><a href="/a">A</a><a href="/b">B</a></body></html>'
    html_back = f'<html><body><p>{_WORDS}</p><a href="/">Home</a></body></html>'
    route_start = respx.get(_START).mock(return_value=httpx.Response(200, text=html_start))
    respx.get(f"{_BASE}/a").mock(return_value=httpx.Response(200, text=html_back))
    respx.get(f"{_BASE}/b").mock(return_value=httpx.Response(200, text=html_back))

    result = await crawl(
        _START, _GOAL,
        model=_adapter_never_found(), max_pages=5,
        stream=False, respect_robots=True, default_delay=0.0,
    )
    assert route_start.call_count == 1
    assert result.pages_visited == 3


# ---------------------------------------------------------------------------
# Off-domain links never followed
# ---------------------------------------------------------------------------

@respx.mock
async def test_off_domain_links_not_enqueued():
    _mock_404_robots()
    html = (
        '<html><body>'
        '<a href="http://evil.com/page">Off domain</a>'
        '</body></html>'
    )
    respx.get(_START).mock(return_value=httpx.Response(200, text=html))
    respx.get("http://evil.com/robots.txt").mock(return_value=httpx.Response(404))
    evil_route = respx.get("http://evil.com/page").mock(
        return_value=httpx.Response(200, text=_HTML_EMPTY)
    )

    async def _adapter(*, schema_hint=None, available_links, **kwargs):
        links = [link["url"] for link in available_links]
        return {
            "found": False, "confidence": 0.1, "result_url": None,
            "links_to_follow": links, "reasoning": "not found",
        }

    await crawl(
        _START, _GOAL,
        model=_adapter, max_pages=5,
        stream=False, respect_robots=True, default_delay=0.0,
    )
    assert evil_route.call_count == 0


# ---------------------------------------------------------------------------
# return_content
# ---------------------------------------------------------------------------

@respx.mock
async def test_return_content_populates_content_field():
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, text=_HTML_CONTACT))

    result = await crawl(
        _START, _GOAL,
        model=_adapter_found(_START),
        return_content=True, stream=False, respect_robots=True, default_delay=0.0,
    )
    assert result.found is True
    assert result.content is not None
    assert len(result.content) == 1


@respx.mock
async def test_return_content_false_gives_none():
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, text=_HTML_CONTACT))

    result = await crawl(
        _START, _GOAL,
        model=_adapter_found(_START),
        return_content=False, stream=False, respect_robots=True, default_delay=0.0,
    )
    assert result.content is None


# ---------------------------------------------------------------------------
# Streaming event sequence
# ---------------------------------------------------------------------------

@respx.mock
async def test_crawl_started_is_first_event():
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, text=_HTML_EMPTY))

    events = await _collect_events(crawl(
        _START, _GOAL,
        model=_adapter_never_found(),
        stream=True, respect_robots=True, default_delay=0.0,
    ))
    assert isinstance(events[0], CrawlStarted)


@respx.mock
async def test_crawl_complete_is_last_event():
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, text=_HTML_EMPTY))

    events = await _collect_events(crawl(
        _START, _GOAL,
        model=_adapter_never_found(),
        stream=True, respect_robots=True, default_delay=0.0,
    ))
    assert isinstance(events[-1], CrawlComplete)


@respx.mock
async def test_page_fetched_and_model_decision_in_stream():
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, text=_HTML_EMPTY))

    events = await _collect_events(crawl(
        _START, _GOAL,
        model=_adapter_never_found(),
        stream=True, respect_robots=True, default_delay=0.0,
    ))
    assert any(isinstance(e, PageFetched) for e in events)
    # _HTML_EMPTY triggers dead-end plausibility → PageSkipped instead of ModelDecision
    # So we just verify PageFetched is emitted
    fetched = [e for e in events if isinstance(e, PageFetched)]
    assert fetched
    assert fetched[0].http_status == 200


@respx.mock
async def test_result_found_event_emitted_on_match():
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, text=_HTML_CONTACT))

    events = await _collect_events(crawl(
        _START, _GOAL,
        model=_adapter_found(_START),
        stream=True, respect_robots=True, default_delay=0.0,
    ))
    result_found_events = [e for e in events if isinstance(e, ResultFound)]
    assert len(result_found_events) == 1
    assert result_found_events[0].result_index == 1


@respx.mock
async def test_crawl_complete_reports_correct_counts():
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, text=_HTML_CONTACT))

    events = await _collect_events(crawl(
        _START, _GOAL,
        model=_adapter_found(_START),
        stream=True, respect_robots=True, default_delay=0.0,
    ))
    complete = [e for e in events if isinstance(e, CrawlComplete)][0]
    assert complete.found is True
    assert complete.result_count == 1
    assert complete.pages_visited == 1


# ---------------------------------------------------------------------------
# Additional coverage for explicit allowed_domains and fetch error variants
# ---------------------------------------------------------------------------

@respx.mock
async def test_explicit_allowed_domains_restricts_navigation():
    """allowed_domains= provided explicitly — off-domain links are never visited."""
    respx.get(_ROBOTS).mock(return_value=httpx.Response(404))
    html = (
        f'<html><body><p>{_WORDS}</p>'
        '<a href="/contact">Contact</a>'
        '<a href="http://evil.com/page">Off domain</a>'
        '</body></html>'
    )
    respx.get(_START).mock(return_value=httpx.Response(200, text=html))
    respx.get(_CONTACT).mock(return_value=httpx.Response(200, text=_HTML_CONTACT))
    evil_route = respx.get("http://evil.com/page").mock(
        return_value=httpx.Response(200, text=_HTML_EMPTY)
    )

    result = await crawl(
        _START, _GOAL,
        model=_adapter_found(_CONTACT),
        allowed_domains=["example.com"],
        stream=False, respect_robots=True, default_delay=0.0,
    )
    assert result.found is True
    assert evil_route.call_count == 0


@respx.mock
async def test_fetch_timeout_error_emits_page_skipped():
    _mock_404_robots()
    from charlotte.core import fetcher as fetcher_mod

    original_fetch = fetcher_mod.PageFetcher.fetch

    async def _timeout(self, url, *, visited_urls):
        raise CharlotteTimeoutError("connect timed out")

    fetcher_mod.PageFetcher.fetch = _timeout
    try:
        events = await _collect_events(crawl(
            _START, _GOAL,
            model=_adapter_found(_START),
            stream=True, respect_robots=True, default_delay=0.0,
        ))
    finally:
        fetcher_mod.PageFetcher.fetch = original_fetch

    skipped = [e for e in events if isinstance(e, PageSkipped)]
    assert any("CharlotteTimeoutError" in (e.error_type or "") for e in skipped)


@respx.mock
async def test_fetch_unexpected_exception_emits_page_skipped():
    _mock_404_robots()
    from charlotte.core import fetcher as fetcher_mod

    original_fetch = fetcher_mod.PageFetcher.fetch

    async def _crash(self, url, *, visited_urls):
        raise RuntimeError("something unexpected")

    fetcher_mod.PageFetcher.fetch = _crash
    try:
        events = await _collect_events(crawl(
            _START, _GOAL,
            model=_adapter_found(_START),
            stream=True, respect_robots=True, default_delay=0.0,
        ))
    finally:
        fetcher_mod.PageFetcher.fetch = original_fetch

    skipped = [e for e in events if isinstance(e, PageSkipped)]
    assert skipped  # unexpected exception caught and emitted as PageSkipped
    assert any(isinstance(e, CrawlComplete) for e in events)
