"""
Integration tests — robots.txt RFC 9309 status-code and redirect handling.

Per RFC 9309:
  - 3xx  → follow the redirect
  - 4xx (except 429) → no restrictions
  - 429  → uncrawlable (rate-limited)
  - 5xx  → uncrawlable (server error)

These tests document the behaviour introduced in the RFC 9309 compliance fix
and complement T-06 through T-08 in test_t05_t08_playwright_robots.py.
"""

from __future__ import annotations

import httpx
import respx

from charlotte.core.engine import crawl
from charlotte.models import CrawlComplete, PageSkipped

from tests.integration.conftest import collect, nav, page, seq

_BASE = "http://example.com"
_START = f"{_BASE}/"
_ROBOTS = f"{_BASE}/robots.txt"
_GOAL = "find the target"


# ---------------------------------------------------------------------------
# 403 on robots.txt → no restrictions (RFC 9309 §2.3.1)
# ---------------------------------------------------------------------------

@respx.mock
async def test_robots_403_treated_as_no_restrictions():
    """robots.txt 403 → no restrictions; crawl proceeds normally (RFC 9309 §2.3.1)."""
    respx.get(_ROBOTS).mock(return_value=httpx.Response(403))
    respx.get(_START).mock(return_value=httpx.Response(200, text=page()))

    result = await crawl(
        _START, _GOAL,
        model=seq(nav(found=True, confidence=0.95, result_url=_START, links=[])),
        stream=False, respect_robots=True, default_delay=0,
        verify_destination="off",
    )

    assert result.found
    assert result.pages_visited == 1


# ---------------------------------------------------------------------------
# 401 on robots.txt → no restrictions (RFC 9309 §2.3.1)
# ---------------------------------------------------------------------------

@respx.mock
async def test_robots_401_treated_as_no_restrictions():
    """robots.txt 401 → no restrictions; crawl proceeds normally (RFC 9309 §2.3.1)."""
    respx.get(_ROBOTS).mock(return_value=httpx.Response(401))
    respx.get(_START).mock(return_value=httpx.Response(200, text=page()))

    result = await crawl(
        _START, _GOAL,
        model=seq(nav(found=True, confidence=0.95, result_url=_START, links=[])),
        stream=False, respect_robots=True, default_delay=0,
        verify_destination="off",
    )

    assert result.found
    assert result.pages_visited == 1


# ---------------------------------------------------------------------------
# 500 on robots.txt → uncrawlable (server error, conservative block)
# ---------------------------------------------------------------------------

@respx.mock
async def test_robots_500_blocks_crawl():
    """robots.txt 500 → domain treated as uncrawlable; PageSkipped emitted."""
    respx.get(_ROBOTS).mock(return_value=httpx.Response(500))

    events = await collect(crawl(
        _START, _GOAL,
        model=seq(), stream=True, respect_robots=True, default_delay=0,
    ))

    skipped = [e for e in events if isinstance(e, PageSkipped)]
    complete = next(e for e in events if isinstance(e, CrawlComplete))

    assert len(skipped) == 1
    assert skipped[0].error_type == "RobotsError"
    assert "500" in skipped[0].reason
    assert not complete.found
    assert complete.pages_visited == 0


# ---------------------------------------------------------------------------
# 429 on robots.txt → uncrawlable (rate-limited, conservative block)
# ---------------------------------------------------------------------------

@respx.mock
async def test_robots_429_blocks_crawl():
    """robots.txt 429 → domain treated as uncrawlable; PageSkipped emitted."""
    respx.get(_ROBOTS).mock(return_value=httpx.Response(429))

    events = await collect(crawl(
        _START, _GOAL,
        model=seq(), stream=True, respect_robots=True, default_delay=0,
    ))

    skipped = [e for e in events if isinstance(e, PageSkipped)]
    complete = next(e for e in events if isinstance(e, CrawlComplete))

    assert len(skipped) == 1
    assert skipped[0].error_type == "RobotsError"
    assert "429" in skipped[0].reason
    assert not complete.found
    assert complete.pages_visited == 0


# ---------------------------------------------------------------------------
# 301 redirect on robots.txt → followed; crawl proceeds (RFC 9309 §2.3)
# ---------------------------------------------------------------------------

@respx.mock
async def test_robots_301_redirect_followed():
    """robots.txt 301 → redirect followed; crawl proceeds with the redirected content."""
    https_robots = "https://example.com/robots.txt"
    # http → https redirect (the common case for university sites like UCSD)
    respx.get(_ROBOTS).mock(return_value=httpx.Response(
        301, headers={"location": https_robots}
    ))
    respx.get(https_robots).mock(return_value=httpx.Response(200, text=""))
    respx.get(_START).mock(return_value=httpx.Response(200, text=page()))

    result = await crawl(
        _START, _GOAL,
        model=seq(nav(found=True, confidence=0.95, result_url=_START, links=[])),
        stream=False, respect_robots=True, default_delay=0,
        verify_destination="off",
    )

    assert result.found
    assert result.pages_visited == 1
