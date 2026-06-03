"""
Unit tests for find_link() and _to_link_result() — CHAR-014 (spec §5.2).

Test areas:
  - _to_link_result: found, not-found, budget-exhausted note text
  - Eager config validation (no model, render_js)
  - stream=True returns AsyncGenerator; stream=False returns coroutine → LinkResult
  - Single result found: LinkResult.found=True, urls populated
  - Multiple results collected (max_results always None)
  - Not-found crawl: LinkResult.found=False, note set
  - Budget exhaustion: budget_exhausted=True, note mentions max_pages
"""

from __future__ import annotations

import httpx
import pytest
import respx
from unittest.mock import patch

from charlotte.core.find_link import _to_link_result, find_link
from charlotte.exceptions import CharlotteConfigError
from charlotte.models import CrawlResult, LinkResult, VisitLogEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE = "http://example.com"
_ROBOTS = f"{_BASE}/robots.txt"
_START = f"{_BASE}/"
_GOAL = "Find the contact page"
_CONTACT = f"{_BASE}/contact"
_OTHER = f"{_BASE}/other"

_WORDS = " ".join(["word"] * 60)

_HTML_WITH_TWO_LINKS = (
    f'<html><body><p>{_WORDS}</p>'
    '<a href="/contact">Contact</a>'
    '<a href="/other">Other</a>'
    '</body></html>'
)
_HTML_CONTACT = (
    f'<html><body><h1>Contact</h1><p>{_WORDS}</p>'
    '<a href="/">Home</a>'
    '</body></html>'
)
_HTML_OTHER = (
    f'<html><body><h1>Other</h1><p>{_WORDS}</p>'
    '<a href="/">Home</a>'
    '</body></html>'
)
_HTML_EMPTY = f'<html><body><p>{_WORDS}</p></body></html>'


def _mock_404_robots():
    respx.get(_ROBOTS).mock(return_value=httpx.Response(404))


def _adapter_found_at(url: str):
    """Reports found=True only when page_url matches *url*."""
    async def _adapter(*, schema_hint=None, page_url, available_links, **kwargs):
        if page_url == url:
            return {
                "found": True,
                "confidence": 0.95,
                "result_url": url,
                "links_to_follow": [],
                "reasoning": "Found it.",
            }
        links = [link["url"] for link in available_links]
        return {
            "found": False,
            "confidence": 0.1,
            "result_url": None,
            "links_to_follow": links[:3],
            "reasoning": "Not found yet.",
        }
    return _adapter


def _adapter_found_on_subpages():
    """Not found on start; found=True (no further links) on any other page."""
    async def _adapter(*, schema_hint=None, page_url, available_links, **kwargs):
        if page_url == _START:
            links = [link["url"] for link in available_links]
            return {
                "found": False,
                "confidence": 0.1,
                "result_url": None,
                "links_to_follow": links,
                "reasoning": "Not on start page, following links.",
            }
        return {
            "found": True,
            "confidence": 0.95,
            "result_url": page_url,
            "links_to_follow": [],  # found — no need to follow back-links
            "reasoning": "Found it.",
        }
    return _adapter


def _adapter_never_found():
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


def _make_crawl_result(
    *,
    found: bool = False,
    result_urls: list[str] | None = None,
    confidence: float = 0.0,
    pages_visited: int = 1,
    best_candidate_url: str | None = None,
    budget_exhausted: bool = False,
) -> CrawlResult:
    return CrawlResult(
        found=found,
        result_urls=result_urls or [],
        content=None,
        confidence=confidence,
        pages_visited=pages_visited,
        depth_reached=0,
        visit_log=[],
        best_candidate_url=best_candidate_url,
        budget_exhausted=budget_exhausted,
    )


# ---------------------------------------------------------------------------
# _to_link_result — pure conversion, no I/O
# ---------------------------------------------------------------------------

def test_to_link_result_found():
    cr = _make_crawl_result(found=True, result_urls=["http://x.com/a"], confidence=0.9)
    lr = _to_link_result(cr)
    assert isinstance(lr, LinkResult)
    assert lr.found is True
    assert lr.urls == ["http://x.com/a"]
    assert lr.confidence == 0.9
    assert lr.note is None


def test_to_link_result_not_found_not_exhausted():
    cr = _make_crawl_result(found=False, pages_visited=3, budget_exhausted=False)
    lr = _to_link_result(cr)
    assert lr.found is False
    assert lr.budget_exhausted is False
    assert lr.note is not None
    assert "3" in lr.note
    assert "max_pages" not in lr.note


def test_to_link_result_not_found_budget_exhausted():
    cr = _make_crawl_result(found=False, pages_visited=20, budget_exhausted=True)
    lr = _to_link_result(cr)
    assert lr.found is False
    assert lr.budget_exhausted is True
    assert lr.note is not None
    assert "max_pages" in lr.note


def test_to_link_result_copies_all_fields():
    cr = _make_crawl_result(
        found=True,
        result_urls=["http://x.com/a", "http://x.com/b"],
        confidence=0.88,
        pages_visited=5,
        best_candidate_url=None,
        budget_exhausted=False,
    )
    lr = _to_link_result(cr)
    assert lr.urls == ["http://x.com/a", "http://x.com/b"]
    assert lr.confidence == 0.88
    assert lr.pages_visited == 5
    assert lr.best_candidate_url is None
    assert lr.budget_exhausted is False


# ---------------------------------------------------------------------------
# Config validation — eager, no I/O
# ---------------------------------------------------------------------------

def test_no_model_uses_default_adapter(monkeypatch):
    # Force the Groq branch via env var, then remove the key to trigger the error.
    monkeypatch.setenv("CHARLOTTE_DEFAULT_ADAPTER", "groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(CharlotteConfigError, match="Groq API key"):
        find_link(_START, _GOAL, model=None)


def test_render_js_raises_config_error_when_playwright_not_installed():
    async def _m(**_): return {}
    with patch(
        "charlotte.core.engine._import_playwright",
        side_effect=CharlotteConfigError("playwright"),
    ):
        with pytest.raises(CharlotteConfigError, match="playwright"):
            find_link(_START, _GOAL, model=_m, render_js=True)


# ---------------------------------------------------------------------------
# stream=True / stream=False dispatch
# ---------------------------------------------------------------------------

def test_stream_true_returns_async_generator():
    import types
    async def _m(**_): return {}
    gen = find_link(_START, _GOAL, model=_m, stream=True)
    assert isinstance(gen, types.AsyncGeneratorType)


def test_stream_false_returns_coroutine():
    import inspect
    async def _m(**_): return {}
    coro = find_link(_START, _GOAL, model=_m, stream=False)
    assert inspect.iscoroutine(coro)
    coro.close()  # prevent ResourceWarning


# ---------------------------------------------------------------------------
# Integration: stream=False happy path
# ---------------------------------------------------------------------------

@pytest.mark.anyio
@respx.mock
async def test_find_link_result_found():
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, html=_HTML_WITH_TWO_LINKS))
    respx.get(_CONTACT).mock(return_value=httpx.Response(200, html=_HTML_CONTACT))

    result = await find_link(
        _START, _GOAL,
        model=_adapter_found_at(_CONTACT),
        stream=False,
        respect_robots=True,
        default_delay=0.0,
    )

    assert isinstance(result, LinkResult)
    assert result.found is True
    assert _CONTACT in result.urls
    assert result.note is None


@pytest.mark.anyio
@respx.mock
async def test_find_link_not_found_note_set():
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, html=_HTML_EMPTY))

    result = await find_link(
        _START, _GOAL,
        model=_adapter_never_found(),
        stream=False,
        respect_robots=True,
        default_delay=0.0,
    )

    assert result.found is False
    assert result.note is not None
    assert result.pages_visited >= 1


# ---------------------------------------------------------------------------
# Integration: max_results=None — collects all matches
# ---------------------------------------------------------------------------

@pytest.mark.anyio
@respx.mock
async def test_find_link_collects_multiple_results():
    """find_link() must keep going after the first match (max_results=None)."""
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, html=_HTML_WITH_TWO_LINKS))
    respx.get(_CONTACT).mock(return_value=httpx.Response(200, html=_HTML_CONTACT))
    respx.get(_OTHER).mock(return_value=httpx.Response(200, html=_HTML_OTHER))

    result = await find_link(
        _START, _GOAL,
        model=_adapter_found_on_subpages(),
        stream=False,
        respect_robots=True,
        default_delay=0.0,
    )

    assert result.found is True
    assert len(result.urls) >= 2


# ---------------------------------------------------------------------------
# Integration: budget exhaustion
# ---------------------------------------------------------------------------

@pytest.mark.anyio
@respx.mock
async def test_find_link_budget_exhausted_note():
    _mock_404_robots()
    respx.get(_START).mock(return_value=httpx.Response(200, html=_HTML_WITH_TWO_LINKS))
    respx.get(_CONTACT).mock(return_value=httpx.Response(200, html=_HTML_CONTACT))
    respx.get(_OTHER).mock(return_value=httpx.Response(200, html=_HTML_OTHER))

    result = await find_link(
        _START, _GOAL,
        model=_adapter_never_found(),
        max_pages=1,
        stream=False,
        respect_robots=True,
        default_delay=0.0,
    )

    assert result.found is False
    assert result.budget_exhausted is True
    assert "max_pages" in result.note
