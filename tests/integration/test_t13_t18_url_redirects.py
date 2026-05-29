"""
Integration tests T-13 through T-18 — URL normalization and redirects (spec §19).

T-13  URL with fragment treated as same URL without fragment
T-14  URL with equivalent query param order treated as same URL
T-15  Redirect within allowed_domains followed correctly
T-16  Redirect to disallowed domain — CharlotteRedirectError, page skipped
T-17  Redirect loop A → B → A — detected, page skipped
T-18  Redirect chain exceeding 5 hops — CharlotteRedirectError, page skipped
"""

from __future__ import annotations

import httpx
import pytest
import respx

from charlotte.core.engine import crawl
from charlotte.models import CrawlComplete, PageSkipped

from tests.integration.conftest import collect, nav, page, seq

_BASE = "http://example.com"
_START = f"{_BASE}/"
_GOAL = "find the target"


# ---------------------------------------------------------------------------
# T-13  Fragment variants of the same URL are deduplicated.
#
#       /page already visited; /page#section normalizes to /page → skip.
# ---------------------------------------------------------------------------

@respx.mock
async def test_t13_fragment_url_treated_as_already_visited():
    """T-13: Fragment variants of the same URL are deduplicated via normalization."""
    target = f"{_BASE}/page"
    fragment = f"{_BASE}/page#section"

    respx.get(_START).mock(return_value=httpx.Response(
        200, text=page(links=[("Page", target)])
    ))
    # /page has a self-referencing link with a fragment
    respx.get(target).mock(return_value=httpx.Response(
        200, text=page(links=[("Section", fragment)])
    ))

    model = seq(
        nav(found=False, confidence=0.1, result_url=None, links=[target]),
        # /page visited; tries to enqueue /page#section → normalises to /page → skip
        nav(found=False, confidence=0.1, result_url=None, links=[fragment]),
    )
    result = await crawl(
        _START, _GOAL,
        model=model, stream=False, respect_robots=False, default_delay=0,
    )

    # start + /page; /page#section never causes a third visit
    assert result.pages_visited == 2
    assert not result.found


# ---------------------------------------------------------------------------
# T-14  Query params in different order normalise to the same URL.
#
#       /search?a=1&b=2 visited; /search?b=2&a=1 normalises to same → skip.
# ---------------------------------------------------------------------------

@respx.mock
async def test_t14_equivalent_query_param_order_deduplicated():
    """T-14: Query params in different order normalize to the same URL; second visit skipped."""
    canonical = f"{_BASE}/search?a=1&b=2"
    alternate = f"{_BASE}/search?b=2&a=1"

    respx.get(_START).mock(return_value=httpx.Response(
        200, text=page(links=[("Search", canonical)])
    ))
    # The search page links back to itself with reversed param order
    respx.get(canonical).mock(return_value=httpx.Response(
        200, text=page(links=[("Alt", alternate)])
    ))

    model = seq(
        nav(found=False, confidence=0.1, result_url=None, links=[canonical]),
        nav(found=False, confidence=0.1, result_url=None, links=[alternate]),
    )
    result = await crawl(
        _START, _GOAL,
        model=model, stream=False, respect_robots=False, default_delay=0,
    )

    # start + /search?a=1&b=2; the alternate form never causes a third visit
    assert result.pages_visited == 2


# ---------------------------------------------------------------------------
# T-15  Redirect within allowed_domains followed correctly.
# ---------------------------------------------------------------------------

@respx.mock
async def test_t15_redirect_within_allowed_domain_followed():
    """T-15: 301 redirect within allowed_domains is followed; goal found at final URL."""
    old = f"{_BASE}/old"
    new = f"{_BASE}/new"

    respx.get(_START).mock(return_value=httpx.Response(
        200, text=page(links=[("Old", old)])
    ))
    respx.get(old).mock(return_value=httpx.Response(
        301, headers={"location": new}
    ))
    respx.get(new).mock(return_value=httpx.Response(200, text=page()))

    model = seq(
        nav(found=False, confidence=0.1, result_url=None, links=[old]),
        # Fetcher follows the redirect; page_url is now /new
        nav(found=True, confidence=0.9, result_url=new, links=[]),
    )
    result = await crawl(
        _START, _GOAL,
        model=model, stream=False, respect_robots=False, default_delay=0,
    )

    assert result.found
    assert result.result_urls == [new]


# ---------------------------------------------------------------------------
# T-16  Redirect to a domain outside allowed_domains — page skipped.
# ---------------------------------------------------------------------------

@respx.mock
async def test_t16_redirect_to_disallowed_domain_skips_page():
    """T-16: Redirect to a domain outside allowed_domains raises CharlotteRedirectError; page skipped."""
    offsite = f"{_BASE}/offsite"
    external = "http://other-domain.com/page"

    respx.get(_START).mock(return_value=httpx.Response(
        200, text=page(links=[("Offsite", offsite)])
    ))
    respx.get(offsite).mock(return_value=httpx.Response(
        301, headers={"location": external}
    ))
    # external must NOT be registered — fetcher raises before following there

    model = seq(
        nav(found=False, confidence=0.1, result_url=None, links=[offsite]),
    )
    events = await collect(crawl(
        _START, _GOAL,
        model=model, stream=True, respect_robots=False, default_delay=0,
    ))

    skipped = [e for e in events if isinstance(e, PageSkipped)]
    complete = next(e for e in events if isinstance(e, CrawlComplete))

    # start was fetched and evaluated (model returned links=[offsite], logged in visit_log)
    # /offsite fetch → redirect to disallowed domain → PageSkipped
    assert any(e.error_type == "CharlotteRedirectError" for e in skipped)
    assert not complete.found


# ---------------------------------------------------------------------------
# T-17  Redirect loop A → B → A — detected, page skipped, crawl continues.
# ---------------------------------------------------------------------------

@respx.mock
async def test_t17_redirect_loop_detected():
    """T-17: A→B→A redirect cycle is detected and raises CharlotteRedirectError; page skipped."""
    loop_a = f"{_BASE}/loop-a"
    loop_b = f"{_BASE}/loop-b"

    respx.get(_START).mock(return_value=httpx.Response(
        200, text=page(links=[("Loop A", loop_a)])
    ))
    respx.get(loop_a).mock(return_value=httpx.Response(
        301, headers={"location": loop_b}
    ))
    respx.get(loop_b).mock(return_value=httpx.Response(
        301, headers={"location": loop_a}  # loops back → detected
    ))

    model = seq(
        nav(found=False, confidence=0.1, result_url=None, links=[loop_a]),
    )
    events = await collect(crawl(
        _START, _GOAL,
        model=model, stream=True, respect_robots=False, default_delay=0,
    ))

    skipped = [e for e in events if isinstance(e, PageSkipped)]
    assert any(e.error_type == "CharlotteRedirectError" for e in skipped)
    assert any("loop" in e.reason.lower() for e in skipped)


# ---------------------------------------------------------------------------
# T-18  Redirect chain exceeding 5 hops — CharlotteRedirectError, page skipped.
#
#       _MAX_REDIRECTS = 5; the check fires after the 6th redirect response,
#       so we mock 6 redirect hops (hop0 → hop1 → … → hop5 → hop6).
#       hop6 is never actually fetched — the error is raised before that.
# ---------------------------------------------------------------------------

@respx.mock
async def test_t18_redirect_chain_too_long():
    """T-18: Redirect chain exceeding 5 hops raises CharlotteRedirectError; page skipped."""
    hop0 = f"{_BASE}/hop0"
    hops = [f"{_BASE}/hop{i}" for i in range(7)]

    respx.get(_START).mock(return_value=httpx.Response(
        200, text=page(links=[("Hop 0", hop0)])
    ))
    # Mock each of the 6 redirect hops (hop0 through hop5)
    for i in range(6):
        respx.get(hops[i]).mock(return_value=httpx.Response(
            301, headers={"location": hops[i + 1]}
        ))

    model = seq(
        nav(found=False, confidence=0.1, result_url=None, links=[hop0]),
    )
    events = await collect(crawl(
        _START, _GOAL,
        model=model, stream=True, respect_robots=False, default_delay=0,
    ))

    skipped = [e for e in events if isinstance(e, PageSkipped)]
    assert any(e.error_type == "CharlotteRedirectError" for e in skipped)
    assert any("exceeded" in e.reason for e in skipped)
