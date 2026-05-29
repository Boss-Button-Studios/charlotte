"""
Integration tests T-09 through T-12 — adapter validation and provenance (spec §19).

T-09  Malformed model output on first attempt — retry with schema hint succeeds
T-10  Malformed model output on both attempts — page skipped, crawl continues
T-11  Model returns hallucinated result_url — provenance rejects it
T-12  Model returns off-domain URL in links_to_follow — silently dropped
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from charlotte.core.engine import crawl
from charlotte.models import CrawlComplete, PageSkipped, ResultFound

from tests.integration.conftest import BODY, collect, nav, page, seq

_BASE = "http://example.com"
_START = f"{_BASE}/"
_GOAL = "find the target"


# ---------------------------------------------------------------------------
# T-09  First attempt fails schema validation; second attempt (with schema
#       hint) returns a valid response → goal found, no page skipped.
# ---------------------------------------------------------------------------

@respx.mock
async def test_t09_schema_retry_succeeds_on_second_attempt():
    """T-09: Invalid schema on first call; schema-hint retry on second call succeeds."""
    respx.get(_START).mock(return_value=httpx.Response(200, text=page()))

    schema_hints_received: list[str | None] = []

    async def _adapter(*, schema_hint: str | None = None, **_kw: Any) -> dict:
        schema_hints_received.append(schema_hint)
        if schema_hint is None:
            return {"this": "is invalid"}          # First attempt: bad schema
        return nav(found=True, confidence=0.95, result_url=_START, links=[])

    result = await crawl(
        _START, _GOAL,
        model=_adapter, stream=False, respect_robots=False, default_delay=0,
    )

    assert result.found
    assert len(schema_hints_received) == 2          # Two attempts made
    assert schema_hints_received[0] is None         # No hint on first try
    assert schema_hints_received[1] is not None     # Hint injected on retry


# ---------------------------------------------------------------------------
# T-10  Both attempts return invalid output → page skipped, crawl ends unfound.
# ---------------------------------------------------------------------------

@respx.mock
async def test_t10_both_attempts_fail_page_skipped():
    """T-10: Both schema-validation attempts return invalid output; page is skipped."""
    respx.get(_START).mock(return_value=httpx.Response(200, text=page()))

    async def _always_bad(*, schema_hint: str | None = None, **_kw: Any) -> dict:
        return {"garbage": True}

    events = await collect(crawl(
        _START, _GOAL,
        model=_always_bad, stream=True, respect_robots=False, default_delay=0,
    ))

    skipped = [e for e in events if isinstance(e, PageSkipped)]
    complete = next(e for e in events if isinstance(e, CrawlComplete))

    assert len(skipped) == 1
    assert skipped[0].error_type == "AdapterOutputError"
    assert not complete.found


# ---------------------------------------------------------------------------
# T-11  Model claims found=True with a result_url not present on the page.
#       Provenance check rejects the hallucinated URL; goal not recorded.
#
#       The page has a real link (/next) that passes provenance, so
#       zero_links_no_path does not trigger and the visit is logged normally.
# ---------------------------------------------------------------------------

@respx.mock
async def test_t11_hallucinated_result_url_rejected_by_provenance():
    """T-11: Provenance check rejects result_url not present on page; goal not recorded."""
    next_url = f"{_BASE}/next"
    hallucinated = f"{_BASE}/not-on-this-page"

    respx.get(_START).mock(return_value=httpx.Response(
        200, text=page(links=[("Next", next_url)])
    ))
    respx.get(next_url).mock(return_value=httpx.Response(200, text=page()))

    model = seq(
        # On start: found=True but result_url is not in extracted links
        nav(found=True, confidence=0.9, result_url=hallucinated, links=[next_url]),
        # On /next: not found (so crawl ends cleanly)
        nav(found=False, confidence=0.1, result_url=None, links=[]),
    )

    events = await collect(crawl(
        _START, _GOAL,
        model=model, stream=True, respect_robots=False, default_delay=0,
    ))

    result_events = [e for e in events if isinstance(e, ResultFound)]
    complete = next(e for e in events if isinstance(e, CrawlComplete))

    # The hallucinated result_url was rejected — nothing recorded
    assert len(result_events) == 0
    assert not complete.found


# ---------------------------------------------------------------------------
# T-12  Model returns an off-domain URL in links_to_follow.
#       The extractor sees it (it's a real link on the page), provenance
#       accepts it, but the engine's domain filter drops it at enqueue time.
#       other-domain.com is never fetched.
# ---------------------------------------------------------------------------

@respx.mock
async def test_t12_off_domain_link_silently_dropped():
    """T-12: Off-domain URL in links_to_follow is dropped by domain filter; never fetched."""
    external = "http://other-domain.com/page"

    respx.get(_START).mock(return_value=httpx.Response(
        200, text=page(links=[("External", external)])
    ))
    # other-domain.com must NOT be registered — if it is fetched, respx raises

    model = seq(
        nav(found=False, confidence=0.2, result_url=None, links=[external]),
    )
    result = await crawl(
        _START, _GOAL,
        model=model, stream=False, respect_robots=False, default_delay=0,
    )

    assert not result.found
    assert result.pages_visited == 1   # Only the start page was visited
