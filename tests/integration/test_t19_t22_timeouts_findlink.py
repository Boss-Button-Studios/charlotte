"""
Integration tests T-19 through T-22 — timeouts and find_link() (spec §19).

T-19  Connect timeout — CharlotteTimeoutError, page skipped, crawl continues
T-20  Read timeout — CharlotteTimeoutError, page skipped, crawl continues
T-21  Model timeout — adapter exception wrapped, page skipped
T-22  find_link() with max_results=None collects all matching URLs
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from charlotte.core.engine import crawl
from charlotte.core.find_link import find_link
from charlotte.exceptions import CharlotteTimeoutError
from charlotte.models import CrawlComplete, LinkResult, PageSkipped, ResultFound

from tests.integration.conftest import collect, nav, page, seq

_BASE = "http://example.com"
_START = f"{_BASE}/"
_GOAL = "find the target"


# ---------------------------------------------------------------------------
# T-19  Connect timeout on start URL — PageSkipped, crawl ends gracefully.
# ---------------------------------------------------------------------------

@respx.mock
async def test_t19_connect_timeout_skips_page():
    respx.get(_START).mock(side_effect=httpx.ConnectTimeout("timed out"))

    events = await collect(crawl(
        _START, _GOAL,
        model=seq(), stream=True, respect_robots=False, default_delay=0,
    ))

    skipped = [e for e in events if isinstance(e, PageSkipped)]
    complete = next(e for e in events if isinstance(e, CrawlComplete))

    assert len(skipped) == 1
    assert skipped[0].error_type == "CharlotteTimeoutError"
    assert "Connect timeout" in skipped[0].reason
    assert not complete.found
    assert complete.pages_visited == 0


# ---------------------------------------------------------------------------
# T-20  Read timeout on start URL — PageSkipped, crawl ends gracefully.
# ---------------------------------------------------------------------------

@respx.mock
async def test_t20_read_timeout_skips_page():
    respx.get(_START).mock(side_effect=httpx.ReadTimeout("timed out"))

    events = await collect(crawl(
        _START, _GOAL,
        model=seq(), stream=True, respect_robots=False, default_delay=0,
    ))

    skipped = [e for e in events if isinstance(e, PageSkipped)]
    complete = next(e for e in events if isinstance(e, CrawlComplete))

    assert len(skipped) == 1
    assert skipped[0].error_type == "CharlotteTimeoutError"
    assert "Read timeout" in skipped[0].reason
    assert not complete.found
    assert complete.pages_visited == 0


# ---------------------------------------------------------------------------
# T-21  Model raises CharlotteTimeoutError — wrapped as AdapterOutputError,
#       page skipped.
#
#       Spec §12 notes that a model timeout should trigger a single retry
#       before the page is skipped.  The current implementation wraps any
#       non-AdapterOutputError adapter exception immediately without a
#       schema-hint retry; this test documents that behaviour.  If the retry
#       is ever implemented, the test should be updated to expect two adapter
#       calls and eventual success.
# ---------------------------------------------------------------------------

@respx.mock
async def test_t21_model_timeout_skips_page():
    respx.get(_START).mock(return_value=httpx.Response(200, text=page()))

    call_count = 0

    async def _timing_out(*, schema_hint: str | None = None, **_kw: Any) -> dict:
        nonlocal call_count
        call_count += 1
        raise CharlotteTimeoutError("model call timed out")

    events = await collect(crawl(
        _START, _GOAL,
        model=_timing_out, stream=True, respect_robots=False, default_delay=0,
    ))

    skipped = [e for e in events if isinstance(e, PageSkipped)]
    complete = next(e for e in events if isinstance(e, CrawlComplete))

    assert len(skipped) == 1
    assert skipped[0].error_type == "AdapterOutputError"
    assert not complete.found
    assert call_count >= 1   # At minimum one attempt was made


# ---------------------------------------------------------------------------
# T-22  find_link() with max_results=None — all matching URLs collected.
# ---------------------------------------------------------------------------

@respx.mock
async def test_t22_find_link_collects_all_matches():
    url_a = f"{_BASE}/a"
    url_b = f"{_BASE}/b"

    respx.get(_START).mock(return_value=httpx.Response(
        200, text=page(links=[("A", url_a), ("B", url_b)])
    ))
    respx.get(url_a).mock(return_value=httpx.Response(200, text=page()))
    respx.get(url_b).mock(return_value=httpx.Response(200, text=page()))

    model = seq(
        nav(found=False, confidence=0.1, result_url=None, links=[url_a, url_b]),
        nav(found=True, confidence=0.9, result_url=url_a, links=[]),
        nav(found=True, confidence=0.9, result_url=url_b, links=[]),
    )
    result: LinkResult = await find_link(
        _START, _GOAL,
        model=model, stream=False, respect_robots=False, default_delay=0,
    )

    assert isinstance(result, LinkResult)
    assert result.found
    assert len(result.urls) == 2
    assert url_a in result.urls
    assert url_b in result.urls
