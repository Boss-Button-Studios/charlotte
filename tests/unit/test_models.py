"""Unit tests for Charlotte data models and streaming events (CHAR-002)."""

import dataclasses

import pytest

from charlotte.models import (
    BudgetExhausted,
    CrawlComplete,
    CrawlResult,
    CrawlStarted,
    LinkResult,
    ModelDecision,
    PageFetched,
    PageSkipped,
    ResultFound,
    TrustLevel,
    VisitLogEntry,
)


# ---------------------------------------------------------------------------
# CrawlResult
# ---------------------------------------------------------------------------

def _minimal_crawl_result(**overrides) -> CrawlResult:
    defaults = dict(
        found=False,
        result_urls=[],
        content=None,
        confidence=0.0,
        pages_visited=0,
        depth_reached=0,
        visit_log=[],
        best_candidate_url=None,
        budget_exhausted=False,
    )
    return CrawlResult(**{**defaults, **overrides})


def test_crawl_result_is_dataclass():
    assert dataclasses.is_dataclass(CrawlResult)


def test_crawl_result_fields():
    expected = {
        "found", "result_urls", "content", "confidence",
        "pages_visited", "depth_reached", "visit_log",
        "best_candidate_url", "budget_exhausted",
        "answers",  # v1.1 — factual extraction
    }
    actual = {f.name for f in dataclasses.fields(CrawlResult)}
    assert actual == expected


def test_crawl_result_urls_is_always_list():
    r = _minimal_crawl_result()
    assert isinstance(r.result_urls, list)


def test_crawl_result_found_true():
    r = _minimal_crawl_result(
        found=True,
        result_urls=["https://example.com/result"],
        confidence=0.92,
    )
    assert r.found is True
    assert len(r.result_urls) == 1


def test_crawl_result_content_none_by_default():
    r = _minimal_crawl_result()
    assert r.content is None


def test_crawl_result_budget_exhausted():
    r = _minimal_crawl_result(budget_exhausted=True)
    assert r.budget_exhausted is True


# ---------------------------------------------------------------------------
# LinkResult
# ---------------------------------------------------------------------------

def _minimal_link_result(**overrides) -> LinkResult:
    defaults = dict(
        found=False,
        urls=[],
        confidence=0.0,
        pages_visited=0,
        best_candidate_url=None,
        budget_exhausted=False,
        note=None,
    )
    return LinkResult(**{**defaults, **overrides})


def test_link_result_is_dataclass():
    assert dataclasses.is_dataclass(LinkResult)


def test_link_result_fields():
    expected = {
        "found", "urls", "confidence", "pages_visited",
        "best_candidate_url", "budget_exhausted", "note",
    }
    actual = {f.name for f in dataclasses.fields(LinkResult)}
    assert actual == expected


def test_link_result_urls_is_list():
    r = _minimal_link_result(urls=["https://example.com"])
    assert isinstance(r.urls, list)


def test_link_result_note_when_not_found():
    r = _minimal_link_result(found=False, note="Could not locate the target document.")
    assert r.note is not None


# ---------------------------------------------------------------------------
# VisitLogEntry
# ---------------------------------------------------------------------------

def test_visit_log_entry_is_dataclass():
    assert dataclasses.is_dataclass(VisitLogEntry)


def test_visit_log_entry_fields():
    entry = VisitLogEntry(
        url="https://example.com",
        depth=1,
        found=False,
        confidence=0.4,
        reasoning="Navigated to home page; did not find target.",
    )
    assert entry.url == "https://example.com"
    assert entry.depth == 1
    assert entry.found is False
    assert entry.confidence == pytest.approx(0.4)
    assert "home page" in entry.reasoning


# ---------------------------------------------------------------------------
# Streaming events — type constant and timestamp auto-population
# ---------------------------------------------------------------------------

def test_crawl_started_type_and_timestamp():
    e = CrawlStarted(
        start_url="https://example.com",
        goal="Find the calendar",
        max_pages=20,
        max_depth=5,
        max_results=1,
    )
    assert e.type == "crawl_started"
    assert e.timestamp  # non-empty string


def test_page_fetched_type():
    e = PageFetched(url="https://example.com", depth=1, http_status=200, fetch_ms=120)
    assert e.type == "page_fetched"


def test_model_decision_type():
    e = ModelDecision(
        url="https://example.com",
        found=False,
        confidence=0.3,
        links_queued=3,
        reasoning="Navigating deeper.",
    )
    assert e.type == "model_decision"


def test_result_found_type():
    e = ResultFound(url="https://example.com/calendar", confidence=0.95, result_index=1)
    assert e.type == "result_found"
    assert e.result_index == 1
    assert e.answer is None  # default for navigation goals


def test_result_found_answer_field():
    e = ResultFound(url="https://example.com/contact", confidence=0.97, result_index=1, answer="555-1234")
    assert e.answer == "555-1234"


def test_page_skipped_type():
    e = PageSkipped(
        url="https://example.com/pdf",
        reason="Connect timeout after 10s",
        error_type="CharlotteTimeoutError",
    )
    assert e.type == "page_skipped"
    assert e.error_type == "CharlotteTimeoutError"


def test_page_skipped_error_type_nullable():
    e = PageSkipped(url="https://example.com", reason="Off-domain link dropped", error_type=None)
    assert e.error_type is None


def test_budget_exhausted_type():
    e = BudgetExhausted(pages_visited=20, depth_reached=4, best_candidate="https://example.com/close")
    assert e.type == "budget_exhausted"
    assert e.pages_visited == 20


def test_crawl_complete_type():
    e = CrawlComplete(
        found=True,
        result_count=1,
        pages_visited=5,
        depth_reached=2,
        elapsed_ms=3200,
    )
    assert e.type == "crawl_complete"
    assert e.elapsed_ms == 3200


def test_event_timestamps_are_strings():
    """Timestamps must be ISO 8601 strings, not datetime objects."""
    events = [
        CrawlStarted(start_url="http://x.com", goal="g", max_pages=1, max_depth=1, max_results=1),
        PageFetched(url="http://x.com", depth=0, http_status=200, fetch_ms=50),
        ModelDecision(url="http://x.com", found=False, confidence=0.1, links_queued=0, reasoning="r"),
        ResultFound(url="http://x.com/r", confidence=0.9, result_index=1),
        PageSkipped(url="http://x.com", reason="err", error_type=None),
        BudgetExhausted(pages_visited=1, depth_reached=0, best_candidate=None),
        CrawlComplete(found=False, result_count=0, pages_visited=1, depth_reached=0, elapsed_ms=100),
    ]
    for e in events:
        assert isinstance(e.timestamp, str), f"{type(e).__name__}.timestamp must be str"
        assert e.timestamp  # non-empty


def test_type_field_not_in_init():
    """The `type` field must be set automatically — not accepted as a constructor argument."""
    with pytest.raises(TypeError):
        CrawlStarted(
            type="injected",  # type: ignore[call-arg]
            start_url="http://x.com",
            goal="g",
            max_pages=1,
            max_depth=1,
            max_results=1,
        )


# ---------------------------------------------------------------------------
# TrustLevel
# ---------------------------------------------------------------------------

def test_trust_level_values():
    assert TrustLevel.TRUSTED.value == "trusted"
    assert TrustLevel.UNTRUSTED.value == "untrusted"
    assert TrustLevel.SEMI_TRUSTED.value == "semi_trusted"
    assert TrustLevel.PROMOTED.value == "promoted"


def test_trust_level_is_enum():
    from enum import Enum
    assert issubclass(TrustLevel, Enum)


def test_trust_levels_are_distinct():
    levels = [TrustLevel.TRUSTED, TrustLevel.UNTRUSTED, TrustLevel.SEMI_TRUSTED, TrustLevel.PROMOTED]
    assert len(set(levels)) == 4
