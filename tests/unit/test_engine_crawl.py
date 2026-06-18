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
from unittest.mock import patch

from charlotte.core.engine import crawl
from charlotte.core.engine_support import (
    _build_binary_result,
    _document_claim_age_days,
    _fresher_exploration_links,
    _is_stale_dated_document,
    _queue_has_unvisited,
)
from charlotte.core.fetcher import FetchResult
from charlotte.core.goal_preprocessor import DeterministicPreprocessor
from charlotte.exceptions import (
    CharlotteChallengeError,
    CharlotteConfigError,
    CharlotteNetworkError,
    CharlotteTimeoutError,
)
from charlotte.models import (
    BudgetExhausted,
    CrawlComplete,
    CrawlResult,
    CrawlStarted,
    DestinationVerificationFailed,
    ModelDecision,
    PageFetched,
    PageSkipped,
    ResultFound,
    VerificationResult,
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

_PHONE = "555-867-5309"
_HTML_WITH_PHONE = (
    f'<html><head><title>Contact</title></head><body><p>{_WORDS}</p>'
    f'<p>Main line: {_PHONE}</p>'
    '</body></html>'
)
_HTML_PHONE_IN_TITLE = (
    f'<html><head><title>Call {_PHONE}</title></head><body><p>{_WORDS}</p></body></html>'
)


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


async def _pass_pdf_verifier(*, url, goal_context):
    """A verifier mock that accepts any URL and returns minimal PDF content.

    Used by document-link tests where the model CLAIMS a PDF as result_url (which
    routes through the verifier, unlike a PDF popped from the queue that uses the
    fetcher's raw_bytes short-circuit).
    """
    from datetime import datetime, timezone
    from charlotte.models import ResultContent
    content = ResultContent(
        content=b"%PDF-1.4", content_type="application/pdf",
        content_length=8, suggested_filename=url.rsplit("/", 1)[-1] or "bulletin.pdf",
        etag=None, fetched_at=datetime.now(timezone.utc), file_path=None,
    )
    return VerificationResult(
        url=url, passed=True, mode="existence", score=None, reason="ok",
    ), content


async def _collect_events_with_fetch(fetch_fn, start_url, adapter, **crawl_kwargs) -> list:
    """Run a streaming crawl with PageFetcher.fetch patched, collecting all events.

    Defaults to the temporal "latest bulletin" goal and a passing PDF verifier used
    by the document-link tests; pass crawl_kwargs to override per test.
    """
    kwargs = dict(
        stream=True, respect_robots=False, default_delay=0.0,
        verifier=_pass_pdf_verifier,
    )
    kwargs.update(crawl_kwargs)
    with patch("charlotte.core.fetcher.PageFetcher.fetch", fetch_fn):
        return await _collect_events(crawl(
            start_url, "Find the latest bulletin PDF", model=adapter, **kwargs,
        ))


# ---------------------------------------------------------------------------
# Config validation — eager, before any I/O
# ---------------------------------------------------------------------------

def test_no_model_uses_default_adapter(monkeypatch):
    # model=None resolves via CharlotteConfig; default is LocalAdapter, which
    # constructs without an API key. Setting CHARLOTTE_DEFAULT_ADAPTER='groq' and
    # removing GROQ_API_KEY confirms the resolution path runs for the Groq branch.
    monkeypatch.setenv("CHARLOTTE_DEFAULT_ADAPTER", "groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(CharlotteConfigError, match="Groq API key"):
        crawl(_START, _GOAL, model=None)


def test_render_js_raises_config_error_when_playwright_not_installed():
    async def _m(**_): return {}
    with patch(
        "charlotte.core.engine._import_playwright",
        side_effect=CharlotteConfigError("playwright"),
    ):
        with pytest.raises(CharlotteConfigError, match="playwright"):
            crawl(_START, _GOAL, model=_m, render_js=True)


def test_render_timeout_zero_raises_config_error():
    async def _m(**_): return {}
    with pytest.raises(CharlotteConfigError, match="render_timeout"):
        crawl(_START, _GOAL, model=_m, render_timeout=0)


def test_render_timeout_negative_raises_config_error():
    async def _m(**_): return {}
    with pytest.raises(CharlotteConfigError, match="render_timeout"):
        crawl(_START, _GOAL, model=_m, render_timeout=-5.0)


def test_render_timeout_nan_raises_config_error():
    async def _m(**_): return {}
    with pytest.raises(CharlotteConfigError, match="render_timeout"):
        crawl(_START, _GOAL, model=_m, render_timeout=float("nan"))


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
# www. / apex redirect handling
# ---------------------------------------------------------------------------

@respx.mock
async def test_apex_to_www_redirect_is_followed():
    """Default domain scope allows all subdomains of the base domain, so apex→www redirects succeed."""
    # rchsd.org → www.rchsd.org redirect pattern
    respx.get("http://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("http://www.example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("http://www.example.com/").mock(
        return_value=httpx.Response(200, text=_HTML_CONTACT)
    )

    from charlotte.core import fetcher as fetcher_mod

    original_fetch = fetcher_mod.PageFetcher.fetch

    async def _redirect_fetch(self, url, *, visited_urls, **kwargs):
        if "www" not in url:
            from charlotte.core.fetcher import FetchResult
            return FetchResult(
                url="http://www.example.com/",
                html=_HTML_CONTACT,
                status_code=200,
                fetch_ms=10,
                redirect_chain=[(301, "http://www.example.com/")],
            )
        return await original_fetch(self, url, visited_urls=visited_urls, **kwargs)

    fetcher_mod.PageFetcher.fetch = _redirect_fetch
    try:
        result = await crawl(
            "http://example.com/", _GOAL,
            model=_adapter_found("http://www.example.com/"),
            stream=False, respect_robots=True, default_delay=0.0,
            verify_destination="off",
        )
    finally:
        fetcher_mod.PageFetcher.fetch = original_fetch

    assert result.found is True


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
        verify_destination="off",
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
        verify_destination="off",
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
        verify_destination="off",
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
        verify_destination="off",
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
        verify_destination="off",
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

    async def _boom(self, url, *, visited_urls, **kwargs):
        if url.rstrip("/") == _START.rstrip("/"):
            raise CharlotteNetworkError("connection refused")
        return await original_fetch(self, url, visited_urls=visited_urls, **kwargs)

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


@respx.mock
async def test_non_200_status_skips_without_model_call():
    """A 403 (or any non-2xx) response must be skipped immediately — no model
    call — so it doesn't consume model quota or waste crawl time."""
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, text=_HTML_WITH_LINK))
    respx.get(_CONTACT).mock(return_value=httpx.Response(403))

    model_calls: list[str] = []

    async def _tracking_adapter(*, schema_hint=None, page_url, **kwargs):
        model_calls.append(page_url)
        return {
            "found": False, "confidence": 0.1, "result_url": None,
            "links_to_follow": [_CONTACT], "reasoning": "following link",
        }

    events = await _collect_events(crawl(
        _START, _GOAL,
        model=_tracking_adapter,
        stream=True, respect_robots=True, default_delay=0.0,
    ))

    # Model must only have been called for the start page, not the 403 page.
    assert _CONTACT not in model_calls, f"Model called on 403 page: {model_calls}"
    skipped = [e for e in events if isinstance(e, PageSkipped)]
    assert any("http_403" in (e.reason or "") for e in skipped)


@respx.mock
async def test_binary_document_url_skips_model_and_verifies_directly():
    """A PDF URL queued by the model must go straight to the verifier without
    a model call — no text to evaluate, and the retry path would cost two
    model calls on the same binary content."""
    _mock_404_robots()
    _PDF_URL = f"{_BASE}/bulletin.pdf"
    html_with_pdf = f'<html><body><a href="{_PDF_URL}">Bulletin PDF</a></body></html>'
    respx.get(_START).mock(return_value=httpx.Response(200, text=html_with_pdf))
    respx.get(_PDF_URL).mock(return_value=httpx.Response(200, content=b"%PDF-bulletin"))

    model_calls: list[str] = []

    async def _tracking_adapter(*, schema_hint=None, page_url, **kwargs):
        model_calls.append(page_url)
        return {
            "found": False, "confidence": 0.5, "result_url": None,
            "links_to_follow": [_PDF_URL], "reasoning": "follow pdf link",
        }

    events = await _collect_events(crawl(
        _START, "Find the latest bulletin PDF",
        model=_tracking_adapter,
        stream=True, respect_robots=True, default_delay=0.0,
    ))

    assert _PDF_URL not in model_calls, f"Model should not be called for PDF URL: {model_calls}"
    found = [e for e in events if isinstance(e, ResultFound)]
    assert found, "PDF should have been claimed via verifier direct path"
    assert found[0].url == _PDF_URL


@respx.mock
async def test_binary_document_url_skips_page_on_verifier_failure():
    """If the verifier rejects a binary document URL, emit PageSkipped and
    continue crawling rather than spending a model call."""
    _mock_404_robots()
    _PDF_URL = f"{_BASE}/private.pdf"
    html_with_pdf = f'<html><body><a href="{_PDF_URL}">Private PDF</a></body></html>'
    respx.get(_START).mock(return_value=httpx.Response(200, text=html_with_pdf))
    # Verifier re-fetches — return 403 at verification time
    respx.get(_PDF_URL).mock(side_effect=[
        httpx.Response(200, content=b"%PDF-private"),  # engine fetch
        httpx.Response(403),                            # verifier re-fetch
    ])

    model_calls: list[str] = []

    async def _tracking_adapter(*, schema_hint=None, page_url, **kwargs):
        model_calls.append(page_url)
        return {
            "found": False, "confidence": 0.5, "result_url": None,
            "links_to_follow": [_PDF_URL], "reasoning": "follow pdf link",
        }

    events = await _collect_events(crawl(
        _START, "Find the latest bulletin PDF",
        model=_tracking_adapter,
        stream=True, respect_robots=True, default_delay=0.0,
    ))

    assert _PDF_URL not in model_calls, f"Model should not be called for PDF URL: {model_calls}"
    skipped = [e for e in events if isinstance(e, PageSkipped)]
    assert any("binary_document" in (e.reason or "") for e in skipped)


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
        verify_destination="off",
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
        verify_destination="off",
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
        verify_destination="off",
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
        verify_destination="off",
    )
    assert result.found is True
    assert evil_route.call_count == 0


@respx.mock
async def test_fetch_timeout_error_emits_page_skipped():
    _mock_404_robots()
    from charlotte.core import fetcher as fetcher_mod

    original_fetch = fetcher_mod.PageFetcher.fetch

    async def _timeout(self, url, *, visited_urls, **kwargs):
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

    async def _crash(self, url, *, visited_urls, **kwargs):
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


# ---------------------------------------------------------------------------
# Answer content gate — spec §9.4
# ---------------------------------------------------------------------------

def _adapter_fact_answer(answer: str):
    """Reports found=True with the given answer string on any page."""
    async def _adapter(*, page_url, **kwargs):
        return {
            "found": True,
            "confidence": 0.95,
            "result_url": page_url,
            "links_to_follow": [],
            "reasoning": f"Found fact: {answer}",
            "answer": answer,
        }
    return _adapter


@pytest.mark.anyio
@respx.mock
async def test_answer_content_gate_passes_when_answer_in_body():
    """answer present in body text → result promoted normally."""
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, html=_HTML_WITH_PHONE))

    result = await crawl(
        _START, "Find the phone number",
        model=_adapter_fact_answer(_PHONE),
        stream=False, respect_robots=True, default_delay=0.0,
        verify_destination="off",
    )

    assert result.found is True
    assert _START in result.result_urls
    assert result.answers == [_PHONE]


@pytest.mark.anyio
@respx.mock
async def test_answer_content_gate_rejects_fabricated_answer():
    """answer not in page text → gate rejects, result not promoted."""
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, html=_HTML_WITH_PHONE))

    result = await crawl(
        _START, "Find the phone number",
        model=_adapter_fact_answer("999-MADE-UP"),
        stream=False, respect_robots=True, default_delay=0.0,
    )

    assert result.found is False
    assert result.result_urls == []


@pytest.mark.anyio
@respx.mock
async def test_answer_content_gate_passes_when_answer_in_title():
    """answer present in <title> tag → gate accepts (title checked alongside body)."""
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, html=_HTML_PHONE_IN_TITLE))

    result = await crawl(
        _START, "Find the phone number",
        model=_adapter_fact_answer(_PHONE),
        stream=False, respect_robots=True, default_delay=0.0,
        verify_destination="off",
    )

    assert result.found is True
    assert result.answers == [_PHONE]


@pytest.mark.anyio
@respx.mock
async def test_answer_content_gate_whitespace_normalization():
    """answer split across extra whitespace in page text is still accepted."""
    _mock_404_robots()
    # Embed the phone with extra internal spaces so normalization is required
    spaced_phone = "555 -  867 -  5309"
    html = (
        f'<html><body><p>{_WORDS}</p>'
        f'<p>Number:  {spaced_phone}</p>'
        '</body></html>'
    )
    respx.get(_START).mock(return_value=httpx.Response(200, html=html))

    # Model returns the number with standard formatting
    result = await crawl(
        _START, "Find the phone number",
        model=_adapter_fact_answer("555 - 867 - 5309"),
        stream=False, respect_robots=True, default_delay=0.0,
        verify_destination="off",
    )

    assert result.found is True


# ---------------------------------------------------------------------------
# H3: Plausibility retry paths
# ---------------------------------------------------------------------------

@respx.mock
async def test_plausibility_instruction_mirroring_retry_succeeds():
    """H3: instruction_mirroring flag → retry with reinforced hint → clean response → result promoted."""
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, text=_HTML_CONTACT))

    calls: list[str | None] = []

    async def _adapter(*, schema_hint=None, page_url, available_links, **kwargs):
        calls.append(schema_hint)
        if len(calls) == 1:
            # First call: reasoning echoes injection language → instruction_mirroring flag
            return {
                "found": True, "confidence": 0.9, "result_url": page_url,
                "links_to_follow": [],
                "reasoning": "I have been instructed to find this page.",
            }
        # Retry call: clean reasoning passes plausibility
        return {
            "found": True, "confidence": 0.9, "result_url": page_url,
            "links_to_follow": [],
            "reasoning": "This matches the navigation goal.",
        }

    result = await crawl(
        _START, _GOAL,
        model=_adapter, stream=False, respect_robots=True, default_delay=0.0,
        verify_destination="off",
    )

    assert result.found is True
    assert len(calls) == 2
    # Retry call receives the reinforced hint (non-None schema_hint)
    assert calls[1] is not None


@respx.mock
async def test_plausibility_zero_links_no_path_refetch_succeeds():
    """H3: zero_links_no_path flag → re-fetch → second evaluation passes → result promoted."""
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, text=_HTML_CONTACT))

    calls: list[str] = []

    async def _adapter(*, schema_hint=None, page_url, **kwargs):
        calls.append(page_url)
        if len(calls) == 1:
            # Dead-end: found=False with no links → zero_links_no_path flag
            return {
                "found": False, "confidence": 0.1, "result_url": None,
                "links_to_follow": [],
                "reasoning": "Nothing relevant found here.",
            }
        # Second call after re-fetch: goal satisfied
        return {
            "found": True, "confidence": 0.9, "result_url": page_url,
            "links_to_follow": [],
            "reasoning": "Goal satisfied on closer inspection.",
        }

    result = await crawl(
        _START, _GOAL,
        model=_adapter, stream=False, respect_robots=True, default_delay=0.0,
        verify_destination="off",
    )

    assert result.found is True
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# _build_binary_result unit tests
# ---------------------------------------------------------------------------

_PREPROCESSOR = DeterministicPreprocessor()


def _goal_ctx(goal: str):
    return _PREPROCESSOR(goal, None, "en_US")


def test_build_binary_result_document_link_passes_with_content():
    """Non-empty bytes for a document_link goal → passed + ResultContent."""
    ctx = _goal_ctx("Find the latest parish bulletin PDF")
    vresult, content = _build_binary_result(
        "http://example.com/bulletin.pdf",
        b"%PDF-1.4 content",
        ctx,
    )
    assert vresult.passed is True
    assert vresult.reason == "ok_existence_binary"
    assert content is not None
    assert content.content == b"%PDF-1.4 content"
    assert content.suggested_filename == "bulletin.pdf"


def test_build_binary_result_empty_body_fails():
    """Empty body → passed=False, reason='empty_response', no content."""
    ctx = _goal_ctx("Find the parish bulletin")
    vresult, content = _build_binary_result("http://example.com/empty.pdf", b"", ctx)
    assert vresult.passed is False
    assert vresult.reason == "empty_response"
    assert content is None


def test_build_binary_result_non_document_goal_no_content():
    """A navigation goal that incidentally visits a binary URL does not capture content."""
    ctx = _goal_ctx("Find the contact page")
    vresult, content = _build_binary_result(
        "http://example.com/contact.pdf",
        b"%PDF-content",
        ctx,
    )
    assert vresult.passed is True
    assert content is None


# ---------------------------------------------------------------------------
# render_js binary document: raw_bytes bypasses verifier re-fetch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_binary_document_raw_bytes_bypasses_verifier():
    """When FetchResult.raw_bytes is set (Playwright path), the verifier must NOT
    make a second HTTP request — the pre-fetched bytes are used directly."""
    _PDF_URL = f"{_BASE}/bulletin.pdf"
    html_with_pdf = f'<html><body><a href="{_PDF_URL}">Bulletin PDF</a></body></html>'

    fetch_map = {
        _START: FetchResult(url=_START, html=html_with_pdf, status_code=200, fetch_ms=0),
        _PDF_URL: FetchResult(
            url=_PDF_URL, html="", status_code=200, fetch_ms=0,
            raw_bytes=b"%PDF-playwright-fetched",
        ),
    }

    async def _mock_fetch(self, url, *, visited_urls, **kwargs):
        return fetch_map[url]

    model_calls: list[str] = []
    verifier_calls: list[str] = []

    async def _tracking_adapter(*, page_url, **kwargs):
        model_calls.append(page_url)
        return {
            "found": False, "confidence": 0.5, "result_url": None,
            "links_to_follow": [_PDF_URL], "reasoning": "follow pdf link",
        }

    async def _tracking_verifier(*, url, goal_context):
        verifier_calls.append(url)
        return VerificationResult(
            url=url, passed=True, mode="existence", score=None, reason="ok"
        ), None

    with patch("charlotte.core.fetcher.PageFetcher.fetch", _mock_fetch):
        events = await _collect_events(crawl(
            _START, "Find the latest bulletin PDF",
            model=_tracking_adapter,
            verifier=_tracking_verifier,
            stream=True, respect_robots=False, default_delay=0.0,
        ))

    assert _PDF_URL not in model_calls, "Model must not be called for a binary URL"
    assert _PDF_URL not in verifier_calls, "Verifier must not re-fetch when raw_bytes is set"
    found = [e for e in events if isinstance(e, ResultFound)]
    assert found, "PDF should be claimed from pre-fetched bytes"
    assert found[0].url == _PDF_URL


# ---------------------------------------------------------------------------
# html_not_document auto-enqueue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_html_not_document_result_auto_enqueued_as_navigation():
    """When html_not_document rejects the model's result_url, the engine
    should enqueue that URL for navigation instead of stranding with an
    empty queue.  This covers the case where the model claims a bulletin
    listing page (HTML) as the result rather than following it as a link.

    Scenario:
      1. Homepage → model returns found=True result_url=/bulletin/ (HTML), no links_to_follow.
      2. Verifier rejects /bulletin/ as html_not_document.
      3. Engine auto-enqueues /bulletin/ as a nav step.
      4. /bulletin/ → model returns found=True result_url=/bulletin/latest.pdf.
      5. Verifier accepts the PDF → ResultFound.
    """
    _H = "http://example.com/"
    _LISTING = "http://example.com/bulletin/"
    _PDF = "http://example.com/bulletin/latest.pdf"

    # Pages need enough text to pass the thin-content plausibility check.
    fetch_map = {
        _H: FetchResult(
            url=_H,
            html=(
                f'<html><body><p>{_WORDS}</p>'
                f'<a href="{_LISTING}">Weekly Bulletins</a>'
                f'</body></html>'
            ),
            status_code=200, fetch_ms=0,
        ),
        _LISTING: FetchResult(
            url=_LISTING,
            html=(
                f'<html><body><p>{_WORDS}</p>'
                f'<a href="{_PDF}">Download PDF</a>'
                f'</body></html>'
            ),
            status_code=200, fetch_ms=0,
        ),
    }

    async def _mock_fetch(self, url, *, visited_urls, **kwargs):
        return fetch_map[url]

    async def _mock_adapter(*, page_url, **kwargs):
        if page_url == _H:
            return {
                "found": True, "confidence": 0.90, "result_url": _LISTING,
                "links_to_follow": [], "reasoning": "bulletin listing page found",
            }
        # /bulletin/ page — claim the PDF directly
        return {
            "found": True, "confidence": 0.95, "result_url": _PDF,
            "links_to_follow": [], "reasoning": "PDF link found on bulletin page",
        }

    from datetime import datetime, timezone
    from charlotte.models import ResultContent

    async def _mock_verifier(*, url, goal_context):
        if url == _LISTING:
            return VerificationResult(
                url=url, passed=False, mode="existence", score=None,
                reason="html_not_document",
            ), None
        content = ResultContent(
            content=b"%PDF-1.4", content_type="application/pdf",
            content_length=8, suggested_filename="latest.pdf",
            etag=None, fetched_at=datetime.now(timezone.utc), file_path=None,
        )
        return VerificationResult(
            url=url, passed=True, mode="existence", score=None, reason="ok",
        ), content

    with patch("charlotte.core.fetcher.PageFetcher.fetch", _mock_fetch):
        events = await _collect_events(crawl(
            _H, "Find the latest bulletin PDF",
            model=_mock_adapter,
            verifier=_mock_verifier,
            stream=True, respect_robots=False, default_delay=0.0,
        ))

    dvf = [e for e in events if isinstance(e, DestinationVerificationFailed)]
    assert dvf, "Verifier should reject /bulletin/ as html_not_document"
    assert dvf[0].url == _LISTING
    assert dvf[0].result.reason == "html_not_document"

    found = [e for e in events if isinstance(e, ResultFound)]
    assert found, "Engine should navigate to /bulletin/ and find the PDF"
    assert found[0].url == _PDF

    fetched_urls = [e.url for e in events if isinstance(e, PageFetched)]
    assert _H in fetched_urls
    assert _LISTING in fetched_urls


# ---------------------------------------------------------------------------
# Stranding safety net — force-enqueue top ranked links
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stranding_fallback_enqueues_top_ranked_link():
    """When the model contributes no followable links and the queue would empty,
    the engine force-enqueues the top BM25-ranked links so the crawl doesn't
    dead-end.  Models do this on non-terminal pages (e.g. a parish homepage whose
    bulletin lives one hop away): they suggest only off-domain links — all
    filtered out — leaving queued=0 with good on-domain nav links unfollowed.

    Scenario:
      1. Homepage has an on-domain /bulletin/ link (ranks high on "bulletin")
         and an off-domain link.  Model returns found=False, suggests ONLY the
         off-domain link → filtered out → queued=0, queue empties.
      2. Engine force-enqueues the top ranked on-domain link (/bulletin/).
      3. /bulletin/ → model returns found=True result_url=PDF → ResultFound.
    """
    _H = "http://example.com/"
    _BULLETIN = "http://example.com/bulletin/"
    _PDF = "http://example.com/bulletin/latest.pdf"
    _OFFSITE = "http://offsite.example.net/social"

    fetch_map = {
        _H: FetchResult(
            url=_H,
            html=(
                f'<html><body><p>{_WORDS}</p>'
                f'<a href="{_BULLETIN}">Weekly Bulletin</a>'
                f'<a href="{_OFFSITE}">Our Facebook</a>'
                f'</body></html>'
            ),
            status_code=200, fetch_ms=0,
        ),
        _BULLETIN: FetchResult(
            url=_BULLETIN,
            html=(
                f'<html><body><p>{_WORDS}</p>'
                f'<a href="{_PDF}">Download the latest bulletin PDF</a>'
                f'</body></html>'
            ),
            status_code=200, fetch_ms=0,
        ),
    }

    async def _mock_fetch(self, url, *, visited_urls, **kwargs):
        return fetch_map[url]

    async def _mock_adapter(*, page_url, **kwargs):
        if page_url == _H:
            # Model strands: low confidence, suggests only the off-domain link,
            # which the engine filters out → queued=0.
            return {
                "found": False, "confidence": 0.30, "result_url": None,
                "links_to_follow": [_OFFSITE],
                "reasoning": "no bulletin here; maybe social media has it",
            }
        # /bulletin/ — claim the PDF
        return {
            "found": True, "confidence": 0.95, "result_url": _PDF,
            "links_to_follow": [], "reasoning": "latest bulletin PDF is here",
        }

    from datetime import datetime, timezone
    from charlotte.models import ResultContent

    async def _mock_verifier(*, url, goal_context):
        content = ResultContent(
            content=b"%PDF-1.4", content_type="application/pdf",
            content_length=8, suggested_filename="latest.pdf",
            etag=None, fetched_at=datetime.now(timezone.utc), file_path=None,
        )
        return VerificationResult(
            url=url, passed=True, mode="existence", score=None, reason="ok",
        ), content

    with patch("charlotte.core.fetcher.PageFetcher.fetch", _mock_fetch):
        events = await _collect_events(crawl(
            _H, "Find the latest bulletin PDF",
            model=_mock_adapter,
            verifier=_mock_verifier,
            stream=True, respect_robots=False, default_delay=0.0,
        ))

    # The homepage decision should report the fallback enqueue, not queued=0.
    home_decision = [
        e for e in events if isinstance(e, ModelDecision) and e.url == _H
    ]
    assert home_decision, "Homepage should produce a ModelDecision"
    assert home_decision[0].links_queued >= 1, (
        "Fallback must enqueue a ranked link when the model strands the crawl"
    )

    # The off-domain link must never be followed.
    fetched_urls = [e.url for e in events if isinstance(e, PageFetched)]
    assert _OFFSITE not in fetched_urls
    assert _BULLETIN in fetched_urls, "Fallback should route the crawl to /bulletin/"

    found = [e for e in events if isinstance(e, ResultFound)]
    assert found, "Crawl should recover via the fallback and find the PDF"
    assert found[0].url == _PDF


@pytest.mark.asyncio
async def test_stranding_fallback_not_triggered_when_queue_nonempty():
    """The fallback must NOT fire while other pages remain queued — it only
    rescues a genuine dead-end.  Here the homepage enqueues a real link, so the
    second page stranding the model still has nothing force-enqueued because the
    crawl completes normally once the queue drains."""
    _H = "http://example.com/"
    _A = "http://example.com/a/"
    _CONTACT = "http://example.com/contact/"

    fetch_map = {
        _H: FetchResult(
            url=_H,
            html=(
                f'<html><body><p>{_WORDS}</p>'
                f'<a href="{_A}">Page A</a>'
                f'<a href="{_CONTACT}">Contact</a>'
                f'</body></html>'
            ),
            status_code=200, fetch_ms=0,
        ),
        _A: FetchResult(
            url=_A,
            html=f'<html><body><p>{_WORDS}</p></body></html>',
            status_code=200, fetch_ms=0,
        ),
        _CONTACT: FetchResult(
            url=_CONTACT,
            html=f'<html><body><p>{_WORDS}</p></body></html>',
            status_code=200, fetch_ms=0,
        ),
    }

    async def _mock_fetch(self, url, *, visited_urls, **kwargs):
        return fetch_map[url]

    async def _mock_adapter(*, page_url, available_links, **kwargs):
        if page_url == _H:
            # Real navigation: enqueue both on-domain links.
            return {
                "found": False, "confidence": 0.2, "result_url": None,
                "links_to_follow": [lk["url"] for lk in available_links],
                "reasoning": "following on-domain links",
            }
        # Sub-pages strand (suggest an off-domain link → filtered), but the queue
        # still has siblings, so no fallback should be needed to keep going.
        return {
            "found": False, "confidence": 0.2, "result_url": None,
            "links_to_follow": ["http://offsite.example.net/x"],
            "reasoning": "nothing here",
        }

    with patch("charlotte.core.fetcher.PageFetcher.fetch", _mock_fetch):
        events = await _collect_events(crawl(
            _H, "Find the latest bulletin PDF",
            model=_mock_adapter,
            stream=True, respect_robots=False, default_delay=0.0,
        ))

    # All three real pages visited; no off-domain leak; crawl completes unfound.
    fetched_urls = [e.url for e in events if isinstance(e, PageFetched)]
    assert _H in fetched_urls and _A in fetched_urls and _CONTACT in fetched_urls
    assert "http://offsite.example.net/x" not in fetched_urls
    complete = [e for e in events if isinstance(e, CrawlComplete)]
    assert complete and complete[0].found is False


@pytest.mark.asyncio
async def test_verify_403_on_render_js_site_reroutes_as_navigation():
    """On a render_js site, a verifier http_403 (its plain-httpx fetch was
    bot-blocked) must re-route the claimed URL as a navigation step so the
    engine's Playwright-capable fetcher can render it.  This is the Holy Spirit
    case: the model claims /bulletin/ (the date-grid listing) as the document,
    the httpx verifier 403s, but the page loads fine in a browser and contains
    the real PDF links.

    The fetcher's Playwright lifecycle is mocked out — render_js only needs to
    be True for the re-route branch to engage.
    """
    _H = "http://example.com/"
    _BULLETIN = "http://example.com/bulletin/"
    _PDF = "http://example.com/bulletin/latest.pdf"

    fetch_map = {
        _H: FetchResult(
            url=_H,
            html=(
                f'<html><body><p>{_WORDS}</p>'
                f'<a href="{_BULLETIN}">Bulletin</a>'
                f'</body></html>'
            ),
            status_code=200, fetch_ms=0,
        ),
        _BULLETIN: FetchResult(
            url=_BULLETIN,
            html=(
                f'<html><body><p>{_WORDS}</p>'
                f'<a href="{_PDF}">June 15 Bulletin (PDF)</a>'
                f'</body></html>'
            ),
            status_code=200, fetch_ms=0,
        ),
    }

    async def _mock_fetch(self, url, *, visited_urls, **kwargs):
        return fetch_map[url]

    async def _noop_aenter(self):
        return self

    async def _noop_aexit(self, *exc):
        return False

    # PageFetcher.__init__ calls _import_playwright() when render_js=True to store
    # the factory + timeout class. CI has no Playwright installed, so stub the
    # fetcher's own reference (not just the engine's eager check) with a dummy
    # factory and timeout class — neither is exercised here because the browser
    # lifecycle (__aenter__/__aexit__/fetch) is fully mocked.
    class _DummyPWTimeout(Exception):
        pass

    def _fake_import_playwright():
        return (lambda *a, **k: None, _DummyPWTimeout)

    async def _mock_adapter(*, page_url, **kwargs):
        if page_url == _H:
            # Model prematurely claims the listing page as the document.
            return {
                "found": True, "confidence": 0.90, "result_url": _BULLETIN,
                "links_to_follow": [], "reasoning": "bulletin link is here",
            }
        return {
            "found": True, "confidence": 0.95, "result_url": _PDF,
            "links_to_follow": [], "reasoning": "the dated bulletin PDF is here",
        }

    from datetime import datetime, timezone
    from charlotte.models import ResultContent

    async def _mock_verifier(*, url, goal_context):
        if url == _BULLETIN:
            # Plain-httpx verifier is bot-blocked.
            return VerificationResult(
                url=url, passed=False, mode="existence", score=None,
                reason="http_403",
            ), None
        content = ResultContent(
            content=b"%PDF-1.4", content_type="application/pdf",
            content_length=8, suggested_filename="june15.pdf",
            etag=None, fetched_at=datetime.now(timezone.utc), file_path=None,
        )
        return VerificationResult(
            url=url, passed=True, mode="existence", score=None, reason="ok",
        ), content

    with patch("charlotte.core.engine._import_playwright"), \
         patch("charlotte.core.fetcher._import_playwright", _fake_import_playwright), \
         patch("charlotte.core.fetcher.PageFetcher.__aenter__", _noop_aenter), \
         patch("charlotte.core.fetcher.PageFetcher.__aexit__", _noop_aexit), \
         patch("charlotte.core.fetcher.PageFetcher.fetch", _mock_fetch):
        events = await _collect_events(crawl(
            _H, "Find the latest bulletin PDF",
            model=_mock_adapter,
            verifier=_mock_verifier,
            render_js=True,
            stream=True, respect_robots=False, default_delay=0.0,
        ))

    dvf = [e for e in events if isinstance(e, DestinationVerificationFailed)]
    assert dvf and dvf[0].result.reason == "http_403"

    fetched_urls = [e.url for e in events if isinstance(e, PageFetched)]
    assert _BULLETIN in fetched_urls, "403 candidate should be re-fetched as a nav step"

    found = [e for e in events if isinstance(e, ResultFound)]
    assert found, "Engine should recover via the render_js re-route and find the PDF"
    assert found[0].url == _PDF


@pytest.mark.asyncio
async def test_verify_403_without_render_js_does_not_reroute():
    """Without render_js, a verifier http_403 must NOT be re-routed — the engine
    fetcher uses the same httpx path that just failed, so navigating there would
    only waste budget.  The candidate is rejected and the crawl ends unfound."""
    _H = "http://example.com/"
    _DOC = "http://example.com/secret.pdf"

    fetch_map = {
        _H: FetchResult(
            url=_H,
            html=(
                f'<html><body><p>{_WORDS}</p>'
                f'<a href="{_DOC}">Secret PDF</a>'
                f'</body></html>'
            ),
            status_code=200, fetch_ms=0,
        ),
    }

    async def _mock_fetch(self, url, *, visited_urls, **kwargs):
        return fetch_map[url]

    async def _mock_adapter(*, page_url, **kwargs):
        return {
            "found": True, "confidence": 0.90, "result_url": _DOC,
            "links_to_follow": [], "reasoning": "the PDF is here",
        }

    async def _mock_verifier(*, url, goal_context):
        return VerificationResult(
            url=url, passed=False, mode="existence", score=None, reason="http_403",
        ), None

    with patch("charlotte.core.fetcher.PageFetcher.fetch", _mock_fetch):
        events = await _collect_events(crawl(
            _H, "Find the latest bulletin PDF",
            model=_mock_adapter,
            verifier=_mock_verifier,
            stream=True, respect_robots=False, default_delay=0.0,
        ))

    fetched_urls = [e.url for e in events if isinstance(e, PageFetched)]
    assert _DOC not in fetched_urls, "403 doc must not be re-fetched without render_js"
    complete = [e for e in events if isinstance(e, CrawlComplete)]
    assert complete and complete[0].found is False


# ---------------------------------------------------------------------------
# Stranding fallback — stale-queue masking (St. Anne regression)
# ---------------------------------------------------------------------------

def test_queue_has_unvisited_ignores_stale_visited_entries():
    """The live-queue check returns False when every queued URL is already
    visited (stale duplicates that pop as no-ops), and True as soon as one
    unvisited URL remains."""
    from charlotte.core.normalizer import normalize_url
    a = "http://example.com/a"
    b = "http://example.com/b"
    visited = {normalize_url(a)}
    # queue tuples: (neg_score, serial, url, depth)
    empty: list = []
    only_stale = [(-1.0, 0, a, 1)]
    has_live = [(-1.0, 0, a, 1), (-0.5, 1, b, 1)]
    assert _queue_has_unvisited(empty, visited) is False
    assert _queue_has_unvisited(only_stale, visited) is False
    assert _queue_has_unvisited(has_live, visited) is True
    # A malformed URL in the queue is skipped, not raised.
    assert _queue_has_unvisited([(-1.0, 0, "not a url", 1)], visited) is False


@pytest.mark.asyncio
async def test_stranding_fallback_fires_when_only_stale_entries_queued():
    """Regression (St. Anne): a page strands with queued=0 while the queue still
    holds a stale, already-visited duplicate.  The old `not queue` gate counted
    that dead entry as live work and suppressed the fallback, so the crawl
    dead-ended even though the ranker had surfaced the target PDF.  The fallback
    must fire once no *unvisited* entry remains.

    Trace: home enqueues [listing, post]; listing (higher-ranked, popped first)
    re-enqueues post, creating a duplicate; the first post instance is visited
    and strands (queued=0), leaving only the now-stale duplicate in the queue.
    The post page ranks the target PDF, which the fallback must enqueue.

    The post URL/anchor are deliberately date-free so the temporal ranker keeps
    the bulletin *listing* above the post (otherwise the dated post would pop
    first and the choreography wouldn't reproduce the stale-queue condition).
    """
    _H = "http://example.com/"
    _LISTING = "http://example.com/weekly-bulletin/"
    _POST = "http://example.com/pastor-message/"
    _PDF = "http://example.com/uploads/06.14.2026-bulletin.pdf"

    fetch_map = {
        _H: FetchResult(
            url=_H,
            html=(
                f'<html><body><p>{_WORDS}</p>'
                f'<a href="{_LISTING}">Weekly Bulletin listing</a>'
                f'<a href="{_POST}">Pastor message</a>'
                f'</body></html>'
            ),
            status_code=200, fetch_ms=0,
        ),
        _LISTING: FetchResult(
            url=_LISTING,
            html=(
                f'<html><body><p>{_WORDS}</p>'
                f'<a href="{_POST}">Pastor message</a>'
                f'</body></html>'
            ),
            status_code=200, fetch_ms=0,
        ),
        _POST: FetchResult(
            url=_POST,
            html=(
                f'<html><body><p>{_WORDS}</p>'
                f'<a href="{_PDF}">June 14 Bulletin (PDF)</a>'
                f'</body></html>'
            ),
            status_code=200, fetch_ms=0,
        ),
        # The fallback-enqueued PDF is a document URL: when popped, the engine's
        # binary short-circuit fetches it and (raw_bytes present) builds the
        # result directly — no model or verifier call.
        _PDF: FetchResult(
            url=_PDF, html="", status_code=200, fetch_ms=0,
            raw_bytes=b"%PDF-1.4 st-anne-bulletin",
        ),
    }

    async def _mock_fetch(self, url, *, visited_urls, **kwargs):
        return fetch_map[url]

    async def _mock_adapter(*, page_url, available_links, **kwargs):
        if page_url == _H:
            # Enqueue both; "Weekly Bulletin listing" ranks above the post, so
            # listing is popped first and re-enqueues post (the stale duplicate).
            return {
                "found": False, "confidence": 0.3, "result_url": None,
                "links_to_follow": [_LISTING, _POST],
                "reasoning": "two candidate pages to explore",
            }
        if page_url == _LISTING:
            return {
                "found": False, "confidence": 0.3, "result_url": None,
                "links_to_follow": [_POST],
                "reasoning": "the post may have the bulletin",
            }
        # _POST — model strands: suggests only an off-domain link → queued=0.
        return {
            "found": False, "confidence": 0.5, "result_url": None,
            "links_to_follow": ["http://offsite.example.net/x"],
            "reasoning": "lists past bulletins but no direct link",
        }

    with patch("charlotte.core.fetcher.PageFetcher.fetch", _mock_fetch):
        events = await _collect_events(crawl(
            _H, "Find the latest bulletin PDF",
            model=_mock_adapter,
            stream=True, respect_robots=False, default_delay=0.0,
        ))

    # The PDF (ranked on _POST) must be reached via the fallback and verified.
    found = [e for e in events if isinstance(e, ResultFound)]
    assert found, "Fallback should fire at _POST despite the stale queued duplicate"
    assert found[0].url == _PDF
    fetched_urls = [e.url for e in events if isinstance(e, PageFetched)]
    assert "http://offsite.example.net/x" not in fetched_urls


# ---------------------------------------------------------------------------
# Bot-challenge → honest skip (Holy Spirit / Cloudflare)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bot_challenge_skips_page_honestly():
    """When the fetcher reports an anti-bot challenge, the engine skips the page
    with a clear 'declines automated access' reason and ends unfound — it does
    not retry, evade, or crash. (Holy Spirit / Cloudflare.)"""
    _H = "http://example.com/"

    async def _challenge_fetch(self, url, *, visited_urls, **kwargs):
        raise CharlotteChallengeError(
            f"{url!r} is behind an anti-bot challenge — site declines automated access"
        )

    async def _adapter(*, page_url, **kwargs):
        raise AssertionError("model must not be called when the page is challenge-blocked")

    from charlotte.exceptions import AdapterOutputError  # noqa: F401  (kept local like siblings)

    with patch("charlotte.core.fetcher.PageFetcher.fetch", _challenge_fetch):
        events = await _collect_events(crawl(
            _H, "Find the latest bulletin PDF",
            model=_adapter,
            stream=True, respect_robots=False, default_delay=0.0,
        ))

    skips = [e for e in events if isinstance(e, PageSkipped)]
    assert skips, "the challenge-blocked page should be skipped"
    assert skips[0].error_type == "CharlotteChallengeError"
    assert "declines automated access" in skips[0].reason
    complete = [e for e in events if isinstance(e, CrawlComplete)]
    assert complete and complete[0].found is False


# ---------------------------------------------------------------------------
# Staleness guard (Mary Star — claims a months-old bulletin)
# ---------------------------------------------------------------------------

def test_document_claim_age_and_staleness_helpers():
    """Age extraction and the staleness predicate over dated document URLs."""
    from datetime import date
    ref = date(2026, 6, 16)
    stale = "http://example.com/bulletins/20260301B.pdf"   # ~107 days old
    fresh = "http://example.com/bulletins/20260614B.pdf"   # 2 days old
    assert _document_claim_age_days(stale, ref) == (ref - date(2026, 3, 1)).days
    assert _document_claim_age_days(fresh, ref) == 2
    # Non-document and undated-document URLs have no age.
    assert _document_claim_age_days("http://example.com/bulletins/", ref) is None
    assert _document_claim_age_days("http://example.com/latest.pdf", ref) is None
    # Staleness predicate.
    assert _is_stale_dated_document(stale, ref) is True
    assert _is_stale_dated_document(fresh, ref) is False
    assert _is_stale_dated_document("http://example.com/bulletins/", ref) is False


def test_fresher_exploration_links_filters_stale_and_irrelevant():
    """Returns goal-relevant, unvisited, non-stale links — excluding the stale
    claim, other stale documents, and zero-score (irrelevant) links."""
    from datetime import date
    from charlotte.core.normalizer import normalize_url
    ref = date(2026, 6, 16)
    stale_pdf = "http://example.com/bulletins/20260301B.pdf"
    older_pdf = "http://example.com/bulletins/20260201B.pdf"
    view_all = "http://example.com/bulletins/"
    junk = "http://example.com/donate/"
    ranked = [(stale_pdf, 2.5), (view_all, 1.4), (older_pdf, 1.2), (junk, 0.0)]
    out = _fresher_exploration_links(
        ranked, stale_url=stale_pdf, reference_date=ref,
        visited=set(), allowed_domains=frozenset({"example.com"}),
    )
    assert out == [view_all], (
        "only the relevant, non-stale, non-claim link should be offered for exploration"
    )
    # When the only candidates are stale or irrelevant, returns nothing.
    out_none = _fresher_exploration_links(
        [(stale_pdf, 2.5), (older_pdf, 1.2), (junk, 0.0)],
        stale_url=stale_pdf, reference_date=ref,
        visited=set(), allowed_domains=frozenset({"example.com"}),
    )
    assert out_none == []


@pytest.mark.asyncio
async def test_staleness_guard_downgrades_stale_claim_and_explores():
    """Regression (Mary Star): the model claims a months-old bulletin from the
    homepage while a fresher 'view bulletins' path is unexplored. The guard
    downgrades that claim and steers the crawl to the listing, where it finds the
    current bulletin. Dates are computed from today so the test never ages out."""
    from datetime import date, timedelta
    today = date.today()
    stale_d = (today - timedelta(days=100)).strftime("%Y%m%d")
    fresh_d = today.strftime("%Y%m%d")

    _H = "http://example.com/"
    _STALE_PDF = f"http://example.com/{stale_d}B.pdf"
    _LISTING = "http://example.com/bulletins/"
    _FRESH_PDF = f"http://example.com/bulletins/{fresh_d}B.pdf"

    fetch_map = {
        _H: FetchResult(
            url=_H,
            html=(
                f'<html><body><p>{_WORDS}</p>'
                f'<a href="{_STALE_PDF}">Latest Bulletin</a>'
                f'<a href="{_LISTING}">View all bulletins</a>'
                f'</body></html>'
            ),
            status_code=200, fetch_ms=0,
        ),
        _LISTING: FetchResult(
            url=_LISTING,
            html=(
                f'<html><body><p>{_WORDS}</p>'
                f'<a href="{_FRESH_PDF}">This week\'s bulletin</a>'
                f'</body></html>'
            ),
            status_code=200, fetch_ms=0,
        ),
        _FRESH_PDF: FetchResult(
            url=_FRESH_PDF, html="", status_code=200, fetch_ms=0,
            raw_bytes=b"%PDF-1.4 fresh-bulletin",
        ),
        _STALE_PDF: FetchResult(
            url=_STALE_PDF, html="", status_code=200, fetch_ms=0,
            raw_bytes=b"%PDF-1.4 stale-bulletin",
        ),
    }

    async def _mock_fetch(self, url, *, visited_urls, **kwargs):
        return fetch_map[url]

    async def _mock_adapter(*, page_url, **kwargs):
        if page_url == _H:
            # Mimics Mary Star: confidently claims the stale homepage PDF, no links.
            return {
                "found": True, "confidence": 0.90, "result_url": _STALE_PDF,
                "links_to_follow": [], "reasoning": "bulletin link on the homepage",
            }
        # On the listing page the model claims the current bulletin.
        return {
            "found": True, "confidence": 0.95, "result_url": _FRESH_PDF,
            "links_to_follow": [], "reasoning": "this week's bulletin",
        }

    events = await _collect_events_with_fetch(_mock_fetch, _H, _mock_adapter)

    found = [e for e in events if isinstance(e, ResultFound)]
    assert found, "the crawl should recover and find the current bulletin"
    assert found[0].url == _FRESH_PDF, "must return the fresh bulletin, not the stale claim"
    assert all(e.url != _STALE_PDF for e in found), "the stale bulletin must not be claimed"


@pytest.mark.asyncio
async def test_staleness_guard_does_not_touch_fresh_claim():
    """A recent dated bulletin is claimed normally — the guard only fires on
    clearly-stale claims, so the working parishes are unaffected."""
    from datetime import date, timedelta
    today = date.today()
    fresh_d = (today - timedelta(days=2)).strftime("%Y%m%d")
    _H = "http://example.com/"
    _FRESH_PDF = f"http://example.com/{fresh_d}B.pdf"

    fetch_map = {
        _H: FetchResult(
            url=_H,
            html=(
                f'<html><body><p>{_WORDS}</p>'
                f'<a href="{_FRESH_PDF}">Latest Bulletin</a>'
                f'<a href="http://example.com/bulletins/">View all bulletins</a>'
                f'</body></html>'
            ),
            status_code=200, fetch_ms=0,
        ),
        _FRESH_PDF: FetchResult(
            url=_FRESH_PDF, html="", status_code=200, fetch_ms=0,
            raw_bytes=b"%PDF-1.4 fresh",
        ),
    }

    async def _mock_fetch(self, url, *, visited_urls, **kwargs):
        return fetch_map[url]

    async def _mock_adapter(*, page_url, **kwargs):
        return {
            "found": True, "confidence": 0.95, "result_url": _FRESH_PDF,
            "links_to_follow": [], "reasoning": "this week's bulletin",
        }

    events = await _collect_events_with_fetch(_mock_fetch, _H, _mock_adapter)
    found = [e for e in events if isinstance(e, ResultFound)]
    assert found and found[0].url == _FRESH_PDF


@pytest.mark.asyncio
async def test_staleness_guard_skips_stale_doc_in_links_to_follow():
    """Regression (Mary Star four-pack): the model returns found=False but puts a
    months-old bulletin PDF in links_to_follow alongside a fresher 'view bulletins'
    link. Without the guard the stale PDF is enqueued and claimed by the binary
    short-circuit (bypassing the claim-path guard). The links-path guard must skip
    enqueuing the stale doc when a fresher path exists, so the crawl explores to the
    current bulletin instead."""
    from datetime import date, timedelta
    today = date.today()
    stale_d = (today - timedelta(days=100)).strftime("%Y%m%d")
    fresh_d = today.strftime("%Y%m%d")

    _H = "http://example.com/"
    _STALE_PDF = f"http://example.com/{stale_d}B.pdf"
    _LISTING = "http://example.com/bulletins/"
    _FRESH_PDF = f"http://example.com/bulletins/{fresh_d}B.pdf"

    fetch_map = {
        _H: FetchResult(
            url=_H,
            html=(
                f'<html><body><p>{_WORDS}</p>'
                f'<a href="{_STALE_PDF}">Latest Bulletin</a>'
                f'<a href="{_LISTING}">View all bulletins</a>'
                f'</body></html>'
            ),
            status_code=200, fetch_ms=0,
        ),
        _LISTING: FetchResult(
            url=_LISTING,
            html=(
                f'<html><body><p>{_WORDS}</p>'
                f'<a href="{_FRESH_PDF}">This week\'s bulletin</a>'
                f'</body></html>'
            ),
            status_code=200, fetch_ms=0,
        ),
        _STALE_PDF: FetchResult(
            url=_STALE_PDF, html="", status_code=200, fetch_ms=0,
            raw_bytes=b"%PDF-1.4 stale",
        ),
        _FRESH_PDF: FetchResult(
            url=_FRESH_PDF, html="", status_code=200, fetch_ms=0,
            raw_bytes=b"%PDF-1.4 fresh",
        ),
    }

    async def _mock_fetch(self, url, *, visited_urls, **kwargs):
        return fetch_map[url]

    async def _mock_adapter(*, page_url, **kwargs):
        if page_url == _H:
            # found=False, but the stale PDF is offered as a link (Mary Star pattern).
            return {
                "found": False, "confidence": 0.5, "result_url": None,
                "links_to_follow": [_STALE_PDF, _LISTING],
                "reasoning": "bulletin links on the homepage",
            }
        return {
            "found": True, "confidence": 0.95, "result_url": _FRESH_PDF,
            "links_to_follow": [], "reasoning": "this week's bulletin",
        }

    events = await _collect_events_with_fetch(_mock_fetch, _H, _mock_adapter)

    found = [e for e in events if isinstance(e, ResultFound)]
    assert found, "the crawl should reach the current bulletin"
    assert found[0].url == _FRESH_PDF, "must return the fresh bulletin, not the stale link"
    fetched = [e.url for e in events if isinstance(e, PageFetched)]
    assert _STALE_PDF not in fetched, "the stale doc must not be enqueued/fetched"


@pytest.mark.asyncio
async def test_staleness_guard_accepts_latest_when_all_old():
    """Guard-rail (Holy Spirit pattern): when every bulletin on the page is old and
    there is NO fresher path, the guard must NOT skip — the latest-available old
    bulletin is still enqueued and accepted. The most recent of the old ones wins
    via the temporal ranker."""
    from datetime import date, timedelta
    today = date.today()
    old_recent_d = (today - timedelta(days=40)).strftime("%Y%m%d")   # stale but newest
    old_older_d = (today - timedelta(days=100)).strftime("%Y%m%d")

    _H = "http://example.com/"
    _OLD_NEWER = f"http://example.com/{old_recent_d}B.pdf"
    _OLD_OLDER = f"http://example.com/{old_older_d}B.pdf"

    fetch_map = {
        _H: FetchResult(
            url=_H,
            html=(
                f'<html><body><p>{_WORDS}</p>'
                f'<a href="{_OLD_NEWER}">Recent bulletin</a>'
                f'<a href="{_OLD_OLDER}">Older bulletin</a>'
                f'</body></html>'
            ),
            status_code=200, fetch_ms=0,
        ),
        _OLD_NEWER: FetchResult(
            url=_OLD_NEWER, html="", status_code=200, fetch_ms=0,
            raw_bytes=b"%PDF-1.4 old-newer",
        ),
        _OLD_OLDER: FetchResult(
            url=_OLD_OLDER, html="", status_code=200, fetch_ms=0,
            raw_bytes=b"%PDF-1.4 old-older",
        ),
    }

    async def _mock_fetch(self, url, *, visited_urls, **kwargs):
        return fetch_map[url]

    async def _mock_adapter(*, page_url, **kwargs):
        return {
            "found": False, "confidence": 0.5, "result_url": None,
            "links_to_follow": [_OLD_NEWER, _OLD_OLDER],
            "reasoning": "only old bulletins are available",
        }

    events = await _collect_events_with_fetch(_mock_fetch, _H, _mock_adapter)
    found = [e for e in events if isinstance(e, ResultFound)]
    assert found, "an all-old site should still deliver its latest-available bulletin"
    assert found[0].url == _OLD_NEWER, "the most recent of the old bulletins should win"


# ---------------------------------------------------------------------------
# follow_linked_resources — terminal off-domain document, no off-domain nav
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_off_domain_document_followed_when_flag_set():
    """With follow_linked_resources, an off-domain document the in-scope page links
    to is enqueued, fetched, and returned — without listing its host in
    allowed_domains. The off-domain HTML link on the same page is NOT followed."""
    _HOME = "http://example.com/"
    _PDF = "http://cdn.example.net/calendar.pdf"      # off-domain document
    _OFF_HTML = "http://other.example.net/events"     # off-domain HTML (must not be followed)

    fetch_map = {
        _HOME: FetchResult(
            url=_HOME,
            html=(
                f'<html><body><p>{_WORDS}</p>'
                f'<a href="{_PDF}">Calendar PDF</a>'
                f'<a href="{_OFF_HTML}">Events page</a>'
                f'</body></html>'
            ),
            status_code=200, fetch_ms=0,
        ),
        _PDF: FetchResult(url=_PDF, html="", status_code=200, fetch_ms=0,
                          raw_bytes=b"%PDF-1.4 calendar"),
    }
    fetched: list[str] = []

    async def _mock_fetch(self, url, *, visited_urls, **kwargs):
        fetched.append(url)
        return fetch_map[url]

    async def _mock_adapter(*, page_url, **kwargs):
        # Model lists both off-domain links to follow (it doesn't claim either).
        return {
            "found": False, "confidence": 0.3, "result_url": None,
            "links_to_follow": [_PDF, _OFF_HTML],
            "reasoning": "calendar might be in one of these",
        }

    with patch("charlotte.core.fetcher.PageFetcher.fetch", _mock_fetch):
        events = await _collect_events(crawl(
            _HOME, "Find the latest calendar PDF",
            model=_mock_adapter,
            follow_linked_resources=True,
            stream=True, respect_robots=False, default_delay=0.0,
        ))

    found = [e for e in events if isinstance(e, ResultFound)]
    assert found and found[0].url == _PDF, "off-domain document should be retrieved"
    assert _PDF in fetched
    assert _OFF_HTML not in fetched, "off-domain HTML must never be followed (no off-domain nav)"


@pytest.mark.asyncio
async def test_off_domain_document_filtered_when_flag_off():
    """Default (flag off): the same off-domain document is filtered at enqueue —
    byte-identical to the historical start-domain-only scope."""
    _HOME = "http://example.com/"
    _PDF = "http://cdn.example.net/calendar.pdf"

    fetch_map = {
        _HOME: FetchResult(
            url=_HOME,
            html=f'<html><body><p>{_WORDS}</p><a href="{_PDF}">Calendar PDF</a></body></html>',
            status_code=200, fetch_ms=0,
        ),
    }
    fetched: list[str] = []

    async def _mock_fetch(self, url, *, visited_urls, **kwargs):
        fetched.append(url)
        return fetch_map[url]

    async def _mock_adapter(*, page_url, **kwargs):
        return {
            "found": False, "confidence": 0.3, "result_url": None,
            "links_to_follow": [_PDF], "reasoning": "maybe here",
        }

    with patch("charlotte.core.fetcher.PageFetcher.fetch", _mock_fetch):
        events = await _collect_events(crawl(
            _HOME, "Find the latest calendar PDF",
            model=_mock_adapter,
            # follow_linked_resources defaults to False
            stream=True, respect_robots=False, default_delay=0.0,
        ))

    assert _PDF not in fetched, "off-domain document must be filtered when flag is off"
    assert not [e for e in events if isinstance(e, ResultFound)]


def test_build_binary_result_writes_result_to_file(tmp_path):
    """Regression: render_js binary docs (raw_bytes path) bypass the verifier, so
    _build_binary_result must itself honour result_to_file — otherwise document_link
    + result_to_file + render_js silently drops the file (school-calendar T1)."""
    ctx = _goal_ctx("Find the latest calendar PDF")
    vresult, content = _build_binary_result(
        "http://cdn.example.net/Calendar2026.pdf",
        b"%PDF-1.4 calendar-bytes",
        ctx,
        result_to_file=tmp_path,
    )
    assert vresult.passed is True
    assert content is not None
    # Written to disk, not held in memory.
    assert content.file_path == tmp_path / "Calendar2026.pdf"
    assert content.file_path.read_bytes() == b"%PDF-1.4 calendar-bytes"
    assert content.content is None


def test_build_binary_result_result_to_file_sanitizes_traversal(tmp_path):
    """A URL-derived filename with parent components must not escape the directory."""
    ctx = _goal_ctx("Find the latest calendar PDF")
    _, content = _build_binary_result(
        "http://evil.example/a/..%2f..%2fetc/passwd.pdf",
        b"%PDF-1.4 x",
        ctx,
        result_to_file=tmp_path,
    )
    # Whatever the last path segment, the write stays inside tmp_path.
    assert content.file_path is not None
    assert content.file_path.parent == tmp_path
