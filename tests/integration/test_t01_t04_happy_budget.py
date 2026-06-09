"""
Integration tests T-01 through T-04 — happy path and budget limits (spec §19).

T-01  Plain HTTP fetch — goal found on first page
T-02  Goal found after following one link
T-03  Goal not found within max_pages (budget exhaustion)
T-04  Goal not found within max_depth (depth limit)
"""

from __future__ import annotations

import httpx
import respx

from charlotte.core.engine import crawl
from charlotte.models import BudgetExhausted, CrawlResult

from tests.integration.conftest import collect, nav, page, seq

_BASE = "http://example.com"
_START = f"{_BASE}/"
_GOAL = "find the target"


# ---------------------------------------------------------------------------
# T-01  Goal found on the very first page
# ---------------------------------------------------------------------------

@respx.mock
async def test_t01_goal_found_on_first_page():
    """T-01: Goal found immediately on the start page — happy path, single hop."""
    respx.get(_START).mock(return_value=httpx.Response(200, text=page()))

    result = await crawl(
        _START, _GOAL,
        model=seq(nav(found=True, confidence=0.95, result_url=_START, links=[])),
        stream=False, respect_robots=False, default_delay=0,
        verify_destination="off",
    )

    assert isinstance(result, CrawlResult)
    assert result.found
    assert result.result_urls == [_START]
    assert result.pages_visited == 1
    assert not result.budget_exhausted


# ---------------------------------------------------------------------------
# T-02  Goal found after following one link
# ---------------------------------------------------------------------------

@respx.mock
async def test_t02_goal_found_after_one_hop():
    """T-02: Goal found on a linked page — happy path, two hops."""
    target = f"{_BASE}/target"
    respx.get(_START).mock(return_value=httpx.Response(
        200, text=page(links=[("Target", target)])
    ))
    respx.get(target).mock(return_value=httpx.Response(200, text=page()))

    model = seq(
        nav(found=False, confidence=0.1, result_url=None, links=[target]),
        nav(found=True, confidence=0.9, result_url=target, links=[]),
    )
    result = await crawl(
        _START, _GOAL,
        model=model, stream=False, respect_robots=False, default_delay=0,
        verify_destination="off",
    )

    assert result.found
    assert result.result_urls == [target]
    assert result.pages_visited == 2
    assert not result.budget_exhausted


# ---------------------------------------------------------------------------
# T-03  Goal not found within max_pages — budget_exhausted=True
# ---------------------------------------------------------------------------

@respx.mock
async def test_t03_budget_exhausted_max_pages():
    """T-03: Crawl stops at max_pages; BudgetExhausted event emitted."""
    page_a = f"{_BASE}/a"
    page_b = f"{_BASE}/b"
    respx.get(_START).mock(return_value=httpx.Response(
        200, text=page(links=[("A", page_a), ("B", page_b)])
    ))
    respx.get(page_a).mock(return_value=httpx.Response(200, text=page()))
    # /b should never be fetched with max_pages=2

    model = seq(
        nav(found=False, confidence=0.2, result_url=None, links=[page_a, page_b]),
        nav(found=False, confidence=0.2, result_url=None, links=[]),
    )
    gen = crawl(
        _START, _GOAL,
        model=model, stream=True, respect_robots=False, default_delay=0, max_pages=2,
    )
    events = await collect(gen)

    result_events = [e for e in events if isinstance(e, BudgetExhausted)]
    assert len(result_events) == 1, "BudgetExhausted event should be emitted"
    assert result_events[0].pages_visited == 2

    # Retrieve the CrawlResult via stream=False to check the field
    respx.get(_START).mock(return_value=httpx.Response(
        200, text=page(links=[("A", page_a), ("B", page_b)])
    ))
    respx.get(page_a).mock(return_value=httpx.Response(200, text=page()))
    model2 = seq(
        nav(found=False, confidence=0.2, result_url=None, links=[page_a, page_b]),
        nav(found=False, confidence=0.2, result_url=None, links=[]),
    )
    result = await crawl(
        _START, _GOAL,
        model=model2, stream=False, respect_robots=False, default_delay=0, max_pages=2,
    )
    assert not result.found
    assert result.budget_exhausted
    assert result.pages_visited == 2


# ---------------------------------------------------------------------------
# T-04  Goal not found within max_depth — depth limit enforced
# ---------------------------------------------------------------------------

@respx.mock
async def test_t04_budget_exhausted_max_depth():
    """T-04: Links beyond max_depth are never enqueued; budget_exhausted=True."""
    deep = f"{_BASE}/deep"
    deeper = f"{_BASE}/deeper"
    respx.get(_START).mock(return_value=httpx.Response(
        200, text=page(links=[("Deep", deep)])
    ))
    respx.get(deep).mock(return_value=httpx.Response(
        200, text=page(links=[("Deeper", deeper)])
    ))
    # /deeper must never be fetched — it would be at depth 2 with max_depth=1

    model = seq(
        # start (depth=0): follow /deep
        nav(found=False, confidence=0.1, result_url=None, links=[deep]),
        # /deep (depth=1): tries to enqueue /deeper but depth+1=2 > max_depth=1
        nav(found=False, confidence=0.1, result_url=None, links=[deeper]),
    )
    result = await crawl(
        _START, _GOAL,
        model=model, stream=False, respect_robots=False, default_delay=0, max_depth=1,
    )

    assert not result.found
    assert result.budget_exhausted
    assert result.pages_visited == 2   # start + /deep; /deeper never fetched
    assert result.depth_reached == 1
