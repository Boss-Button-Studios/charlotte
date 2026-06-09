"""
Integration tests T-31 through T-33 — answer field (spec §6.2, §6.5, §7, §17).

T-31  Factual goal — model populates answer → returned in CrawlResult and ResultFound
T-32  Navigation goal — model returns answer=null → CrawlResult.answers[0] is None
T-33  answer present with found=False → validation rejects it; page skipped
"""

from __future__ import annotations

import httpx
import respx

from charlotte.core.engine import crawl
from charlotte.models import CrawlResult, PageSkipped, ResultFound

from tests.integration.conftest import collect, nav, page, seq

_BASE = "http://example.com"
_START = f"{_BASE}/"
_GOAL = "find the target"


# ---------------------------------------------------------------------------
# T-31  Factual goal — answer populated verbatim by the model.
# ---------------------------------------------------------------------------

_PHONE = "(858) 966-1700"


@respx.mock
async def test_t31_factual_answer_returned_in_result_and_event():
    """T-31: Model populates answer for a factual goal; returned in CrawlResult.answers and ResultFound."""
    respx.get(_START).mock(return_value=httpx.Response(200, text=page(extra=_PHONE)))

    result = await crawl(
        _START, _GOAL,
        model=seq(nav(
            found=True, confidence=0.95, result_url=_START, links=[],
            answer=_PHONE,
        )),
        stream=False, respect_robots=False, default_delay=0,
        verify_destination="off",
    )

    assert isinstance(result, CrawlResult)
    assert result.found
    assert result.answers == [_PHONE]


@respx.mock
async def test_t31_answer_in_result_found_event():
    """T-31: ResultFound event carries the answer field when the model extracts one."""
    respx.get(_START).mock(return_value=httpx.Response(200, text=page(extra=_PHONE)))

    events = await collect(crawl(
        _START, _GOAL,
        model=seq(nav(
            found=True, confidence=0.95, result_url=_START, links=[],
            answer=_PHONE,
        )),
        stream=True, respect_robots=False, default_delay=0,
        verify_destination="off",
    ))

    result_events = [e for e in events if isinstance(e, ResultFound)]
    assert len(result_events) == 1
    assert result_events[0].answer == _PHONE


# ---------------------------------------------------------------------------
# T-32  Navigation goal — model omits answer (null); answers list element is None.
# ---------------------------------------------------------------------------

@respx.mock
async def test_t32_navigation_goal_answer_is_none():
    """T-32: Model returns answer=null for a navigation goal; CrawlResult.answers[0] is None."""
    respx.get(_START).mock(return_value=httpx.Response(200, text=page()))

    result = await crawl(
        _START, _GOAL,
        model=seq(nav(
            found=True, confidence=0.95, result_url=_START, links=[],
            answer=None,
        )),
        stream=False, respect_robots=False, default_delay=0,
        verify_destination="off",
    )

    assert result.found
    assert result.answers == [None]


@respx.mock
async def test_t32_absent_answer_field_treated_as_none():
    """T-32: Model omits answer entirely; CrawlResult.answers[0] is None."""
    respx.get(_START).mock(return_value=httpx.Response(200, text=page()))

    # nav() without answer= omits the key from the response dict
    result = await crawl(
        _START, _GOAL,
        model=seq(nav(found=True, confidence=0.95, result_url=_START, links=[])),
        stream=False, respect_robots=False, default_delay=0,
        verify_destination="off",
    )

    assert result.found
    assert result.answers == [None]


@respx.mock
async def test_t32_found_false_answers_is_null():
    """T-32: When found=False, CrawlResult.answers is None (not an empty list)."""
    respx.get(_START).mock(return_value=httpx.Response(200, text=page()))

    result = await crawl(
        _START, _GOAL,
        model=seq(nav(found=False, confidence=0.1, result_url=None, links=[])),
        stream=False, respect_robots=False, default_delay=0,
    )

    assert not result.found
    assert result.answers is None


# ---------------------------------------------------------------------------
# T-33  answer present with found=False — validation rejects it; page skipped.
# ---------------------------------------------------------------------------

@respx.mock
async def test_t33_answer_with_found_false_rejected():
    """T-33: answer non-null when found=False fails validation; page skipped with AdapterOutputError."""
    respx.get(_START).mock(return_value=httpx.Response(200, text=page()))

    events = await collect(crawl(
        _START, _GOAL,
        model=seq(nav(
            found=False, confidence=0.1, result_url=None, links=[],
            answer="should not be here",
        )),
        stream=True, respect_robots=False, default_delay=0,
    ))

    skipped = [e for e in events if isinstance(e, PageSkipped)]
    assert len(skipped) == 1
    assert skipped[0].error_type == "AdapterOutputError"
