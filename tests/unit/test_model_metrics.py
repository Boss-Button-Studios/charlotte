"""Tests for model-call instrumentation (charlotte.core.model_metrics).

The point of the instrument is to make the *hidden* model calls visible — the
schema-validation retry inside call_with_validation and the engine's plausibility
re-evaluations, neither of which appears in the event stream. The integration
test forces a schema retry and asserts the tally catches it.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from charlotte.core import model_metrics
from charlotte.core.engine import crawl
from charlotte.core.fetcher import FetchResult
from charlotte.models import CrawlComplete


# ---------------------------------------------------------------------------
# Unit — the counter primitives
# ---------------------------------------------------------------------------

def test_record_and_snapshot_within_context():
    model_metrics.reset()
    model_metrics.record(model_metrics.BASE)
    model_metrics.record(model_metrics.BASE)
    model_metrics.record(model_metrics.SCHEMA_RETRY)
    assert model_metrics.snapshot() == {"base": 2, "schema_retry": 1}
    assert model_metrics.total() == 3


def test_reset_clears_the_tally():
    model_metrics.reset()
    model_metrics.record(model_metrics.PREPROCESSOR)
    assert model_metrics.total() == 1
    model_metrics.reset()
    assert model_metrics.snapshot() == {}
    assert model_metrics.total() == 0


def test_record_is_noop_without_active_tally():
    # Fresh ContextVar with no value set in this context — must not raise.
    import contextvars
    ctx = contextvars.Context()

    def _call_without_reset():
        # record/snapshot/total are safe even though reset() was never called here.
        model_metrics.record(model_metrics.BASE)
        return model_metrics.snapshot(), model_metrics.total()

    snap, tot = ctx.run(_call_without_reset)
    assert snap == {} and tot == 0


# ---------------------------------------------------------------------------
# Integration — the instrument catches the hidden schema retry
# ---------------------------------------------------------------------------

_START = "http://example.com/"
_WORDS = " ".join(["word"] * 60)  # above the thin-content plausibility threshold


async def _collect(gen):
    return [e async for e in gen]


@pytest.mark.asyncio
async def test_crawl_counts_base_and_hidden_schema_retry():
    """A single-page crawl whose model returns invalid JSON first, valid on the
    reinforced retry, must tally base=1 AND schema_retry=1 — the retry that is
    otherwise invisible in the event stream."""
    page = FetchResult(
        url=_START,
        html=f"<html><body><p>{_WORDS}</p></body></html>",
        status_code=200, fetch_ms=0,
    )

    async def _mock_fetch(self, url, *, visited_urls, **kwargs):
        return page

    async def _bad_then_good(*, schema_hint=None, page_url, **kwargs):
        # First attempt (schema_hint is None) returns output that fails validation;
        # the reinforced retry (schema_hint set) returns a valid decision.
        if schema_hint is None:
            return {"found": "not-a-boolean"}  # missing fields / wrong type → invalid
        return {
            "found": True, "confidence": 0.95, "result_url": page_url,
            "links_to_follow": [], "reasoning": "found it",
        }

    with patch("charlotte.core.fetcher.PageFetcher.fetch", _mock_fetch):
        events = await _collect(crawl(
            _START, "Find the contact page",  # navigation goal → no preprocessor model call
            model=_bad_then_good,
            stream=True, respect_robots=False, default_delay=0.0,
        ))

    # CrawlComplete is the last event; the tally is complete by then.
    assert any(isinstance(e, CrawlComplete) for e in events)
    snap = model_metrics.snapshot()
    assert snap.get("base", 0) >= 1, f"base eval not counted: {snap}"
    assert snap.get("schema_retry", 0) >= 1, f"hidden schema retry not counted: {snap}"
    # A navigation goal must NOT incur a preprocessor model call.
    assert "preprocessor" not in snap, f"navigation goal should not call the model: {snap}"


@pytest.mark.asyncio
async def test_tally_resets_between_crawls():
    """Each crawl() starts a fresh tally — counts don't accumulate across crawls."""
    page = FetchResult(
        url=_START, html=f"<html><body><p>{_WORDS}</p></body></html>",
        status_code=200, fetch_ms=0,
    )

    async def _mock_fetch(self, url, *, visited_urls, **kwargs):
        return page

    async def _ok_adapter(*, page_url, **kwargs):
        return {
            "found": True, "confidence": 0.95, "result_url": page_url,
            "links_to_follow": [], "reasoning": "found it",
        }

    with patch("charlotte.core.fetcher.PageFetcher.fetch", _mock_fetch):
        await _collect(crawl(_START, "Find the contact page", model=_ok_adapter,
                             stream=True, respect_robots=False, default_delay=0.0))
        first_total = model_metrics.total()
        await _collect(crawl(_START, "Find the contact page", model=_ok_adapter,
                             stream=True, respect_robots=False, default_delay=0.0))
        second_total = model_metrics.total()

    # One clean eval each — the second crawl did not inherit the first's count.
    assert first_total == 1, f"expected 1 base call, got {first_total}"
    assert second_total == 1, f"tally leaked across crawls: {second_total}"
