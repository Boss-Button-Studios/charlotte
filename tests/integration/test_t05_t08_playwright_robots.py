"""
Integration tests T-05 through T-08 — Playwright and robots.txt (spec §19).

T-05  JS-rendered page with render_js=True
T-06  robots.txt disallows the crawl
T-07  robots.txt returns 404 — treated as no restrictions
T-08  robots.txt unreachable (timeout) — treated as uncrawlable
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from charlotte.core.engine import crawl
from charlotte.core.fetcher import FetchResult, PageFetcher
from charlotte.models import CrawlComplete, CrawlResult, PageSkipped

from tests.integration.conftest import collect, nav, page, seq

_BASE = "http://example.com"
_START = f"{_BASE}/"
_ROBOTS = f"{_BASE}/robots.txt"
_GOAL = "find the content"


# ---------------------------------------------------------------------------
# T-05  render_js=True — Playwright renders the page
#
# _import_playwright is called eagerly by crawl() before the generator starts.
# PageFetcher._fetch_with_playwright is patched to avoid launching a real browser.
# ---------------------------------------------------------------------------

async def test_t05_playwright_renders_page():
    fake = FetchResult(url=_START, html=page(), status_code=200, fetch_ms=50)

    with (
        patch("charlotte.core.engine._import_playwright"),                              # eager check in crawl()
        patch("charlotte.core.fetcher._import_playwright", return_value=(MagicMock(), Exception)),  # PageFetcher.__init__
        patch.object(PageFetcher, "_fetch_with_playwright", new=AsyncMock(return_value=fake)),
    ):
        result = await crawl(
            _START, _GOAL,
            model=seq(nav(found=True, confidence=0.95, result_url=_START, links=[])),
            stream=False, respect_robots=False, default_delay=0,
            render_js=True,
        )

    assert result.found
    assert result.result_urls == [_START]
    assert result.pages_visited == 1


# ---------------------------------------------------------------------------
# T-06  robots.txt disallows the crawl — no pages fetched, RobotsError result
# ---------------------------------------------------------------------------

@respx.mock
async def test_t06_robots_disallows_crawl():
    respx.get(_ROBOTS).mock(return_value=httpx.Response(
        200, text="User-agent: *\nDisallow: /\n"
    ))

    events = await collect(crawl(
        _START, _GOAL,
        model=seq(),  # no model calls expected
        stream=True, respect_robots=True, default_delay=0,
    ))

    skipped = [e for e in events if isinstance(e, PageSkipped)]
    complete = next(e for e in events if isinstance(e, CrawlComplete))

    assert len(skipped) == 1
    assert skipped[0].error_type == "RobotsError"
    assert not complete.found
    assert complete.pages_visited == 0


# ---------------------------------------------------------------------------
# T-07  robots.txt returns 404 — treated as no restrictions, crawl proceeds
# ---------------------------------------------------------------------------

@respx.mock
async def test_t07_robots_404_means_no_restrictions():
    respx.get(_ROBOTS).mock(return_value=httpx.Response(404))
    respx.get(_START).mock(return_value=httpx.Response(200, text=page()))

    result = await crawl(
        _START, _GOAL,
        model=seq(nav(found=True, confidence=0.95, result_url=_START, links=[])),
        stream=False, respect_robots=True, default_delay=0,
    )

    assert result.found
    assert result.pages_visited == 1


# ---------------------------------------------------------------------------
# T-08  robots.txt times out — treated as uncrawlable, no pages fetched
# ---------------------------------------------------------------------------

@respx.mock
async def test_t08_robots_timeout_blocks_crawl():
    respx.get(_ROBOTS).mock(side_effect=httpx.ConnectTimeout("timed out"))

    events = await collect(crawl(
        _START, _GOAL,
        model=seq(), stream=True, respect_robots=True, default_delay=0,
    ))

    skipped = [e for e in events if isinstance(e, PageSkipped)]
    complete = next(e for e in events if isinstance(e, CrawlComplete))

    assert len(skipped) == 1
    assert skipped[0].error_type == "RobotsError"
    assert "uncrawlable" in skipped[0].reason
    assert not complete.found
    assert complete.pages_visited == 0
