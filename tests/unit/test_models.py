"""Unit tests for Charlotte data models and streaming events (CHAR-002)."""

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import pytest

from charlotte.models import (
    BudgetExhausted,
    Candidate,
    CandidatesExtracted,
    CrawlComplete,
    CrawlResult,
    CrawlStarted,
    DestinationVerificationFailed,
    FailureMode,
    GoalPreprocessed,
    LinkResult,
    LinksRanked,
    ModelDecision,
    ModelSkipped,
    PageFetched,
    PageSkipped,
    RankedLink,
    ResultContent,
    ResultContentMetadata,
    ResultFound,
    TrustLevel,
    VerificationResult,
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
        "answers",              # v1.1 — factual extraction
        "failure_mode",         # v2 Phase C
        "goal_context",         # v2 Phase C
        "verified_candidates",  # v2 Phase C
        "result_contents",      # v2 Phase C
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


def test_crawl_result_v2_defaults():
    r = _minimal_crawl_result()
    assert r.failure_mode is None
    assert r.goal_context is None
    assert r.verified_candidates == []
    assert r.result_contents == []


def test_crawl_result_failure_mode():
    r = _minimal_crawl_result(failure_mode=FailureMode.NO_CANDIDATES_FOUND)
    assert r.failure_mode is FailureMode.NO_CANDIDATES_FOUND


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
        "result_content",  # v2 Phase C
    }
    actual = {f.name for f in dataclasses.fields(LinkResult)}
    assert actual == expected


def test_link_result_urls_is_list():
    r = _minimal_link_result(urls=["https://example.com"])
    assert isinstance(r.urls, list)


def test_link_result_note_when_not_found():
    r = _minimal_link_result(found=False, note="Could not locate the target document.")
    assert r.note is not None


def test_link_result_result_content_default():
    r = _minimal_link_result()
    assert r.result_content is None


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
# v2 Phase C data types
# ---------------------------------------------------------------------------

def test_ranked_link_frozen():
    rl = RankedLink(text="Download PDF", url="https://example.com/doc.pdf", score=0.87)
    assert rl.text == "Download PDF"
    assert rl.score == pytest.approx(0.87)
    with pytest.raises(dataclasses.FrozenInstanceError):
        rl.score = 0.5  # type: ignore[misc]


def test_candidate_fields_and_frozen():
    c = Candidate(
        value="+1-800-555-0100",
        raw_value="(800) 555-0100",
        zone="content",
        nearby_text="Call us at (800) 555-0100 today",
        position=42,
        score=0.91,
        features={"zone_weight": 1.0, "anchor_proximity": 0.8},
    )
    assert c.value == "+1-800-555-0100"
    assert c.zone == "content"
    assert c.features["zone_weight"] == pytest.approx(1.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.score = 0.0  # type: ignore[misc]


def test_verification_result_fields():
    vr = VerificationResult(
        url="https://example.com/contact",
        passed=True,
        mode="relevance",
        score=0.78,
        reason="BM25 score exceeds threshold",
    )
    assert vr.passed is True
    assert vr.mode == "relevance"
    assert vr.score == pytest.approx(0.78)


def test_verification_result_score_none_when_off():
    vr = VerificationResult(
        url="https://example.com/page",
        passed=True,
        mode="off",
        score=None,
        reason="Verification disabled",
    )
    assert vr.score is None


def test_failure_mode_is_str_enum():
    assert isinstance(FailureMode.NO_CANDIDATES_FOUND, str)
    assert FailureMode.NO_CANDIDATES_FOUND == "no_candidates_found"
    assert FailureMode.ALL_CANDIDATES_REJECTED == "all_candidates_rejected"
    assert FailureMode.BUDGET_EXHAUSTED == "budget_exhausted"
    assert FailureMode.PLAUSIBILITY_FAILURES == "plausibility_failures"
    assert FailureMode.FETCH_FAILURES == "fetch_failures"


def test_failure_mode_members_count():
    assert len(FailureMode) == 5


def test_result_content_metadata_frozen():
    m = ResultContentMetadata(
        content_type="application/pdf",
        content_length=102400,
        suggested_filename="report.pdf",
        etag='"abc123"',
    )
    assert m.content_type == "application/pdf"
    assert m.content_length == 102400
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.content_length = 0  # type: ignore[misc]


def test_result_content_frozen():
    now = datetime.now(timezone.utc)
    rc = ResultContent(
        content=b"<html>...</html>",
        content_type="text/html",
        content_length=16,
        suggested_filename=None,
        etag=None,
        fetched_at=now,
        file_path=None,
    )
    assert rc.content == b"<html>...</html>"
    assert rc.fetched_at == now
    with pytest.raises(dataclasses.FrozenInstanceError):
        rc.content = b""  # type: ignore[misc]


def test_result_content_with_file_path():
    rc = ResultContent(
        content=None,
        content_type="application/pdf",
        content_length=8192,
        suggested_filename="spec.pdf",
        etag='"xyz"',
        fetched_at=datetime.now(timezone.utc),
        file_path=Path("/tmp/spec.pdf"),
    )
    assert rc.file_path == Path("/tmp/spec.pdf")


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
    assert e.content_metadata is None  # v2 default


def test_result_found_answer_field():
    e = ResultFound(url="https://example.com/contact", confidence=0.97, result_index=1, answer="555-1234")
    assert e.answer == "555-1234"


def test_result_found_content_metadata():
    m = ResultContentMetadata(
        content_type="text/html",
        content_length=4096,
        suggested_filename=None,
        etag='"etag1"',
    )
    e = ResultFound(url="https://example.com/r", confidence=0.9, result_index=1, content_metadata=m)
    assert e.content_metadata is m


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


def test_crawl_complete_v2_defaults():
    e = CrawlComplete(
        found=False, result_count=0, pages_visited=3, depth_reached=1, elapsed_ms=1500,
    )
    assert e.failure_mode is None
    assert e.failure_reason is None
    assert e.goal_context is None


def test_crawl_complete_failure_mode():
    e = CrawlComplete(
        found=False,
        result_count=0,
        pages_visited=5,
        depth_reached=2,
        elapsed_ms=4000,
        failure_mode=FailureMode.BUDGET_EXHAUSTED,
        failure_reason="Reached max_pages=5 without a result.",
    )
    assert e.failure_mode is FailureMode.BUDGET_EXHAUSTED
    assert "max_pages" in e.failure_reason


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
# v2 Phase C streaming events
# ---------------------------------------------------------------------------

def test_goal_preprocessed_type():
    e = GoalPreprocessed(goal_context=None, duration_ms=12, source="fresh")
    assert e.type == "goal_preprocessed"
    assert e.goal_context is None
    assert e.source == "fresh"


def test_goal_preprocessed_timestamp_is_string():
    e = GoalPreprocessed(goal_context=None, duration_ms=5, source="cached")
    assert isinstance(e.timestamp, str)
    assert e.timestamp


def test_links_ranked_type():
    links = [RankedLink(text="Next", url="https://example.com/next", score=0.9)]
    e = LinksRanked(page_url="https://example.com", total_links=12, top_links=links, duration_ms=3)
    assert e.type == "links_ranked"
    assert e.total_links == 12
    assert len(e.top_links) == 1


def test_candidates_extracted_type():
    c = Candidate(
        value="555-1234",
        raw_value="555-1234",
        zone="content",
        nearby_text="call 555-1234",
        position=10,
        score=0.8,
        features={},
    )
    e = CandidatesExtracted(page_url="https://example.com", candidates=[c], duration_ms=7)
    assert e.type == "candidates_extracted"
    assert len(e.candidates) == 1


def test_model_skipped_type():
    e = ModelSkipped(
        page_url="https://example.com",
        reason="single_candidate_confident",
        decision="https://example.com/contact",
        confidence=0.95,
    )
    assert e.type == "model_skipped"
    assert e.reason == "single_candidate_confident"
    assert e.confidence == pytest.approx(0.95)


def test_destination_verification_failed_type():
    vr = VerificationResult(
        url="https://example.com/wrong",
        passed=False,
        mode="relevance",
        score=0.1,
        reason="BM25 score below threshold",
    )
    e = DestinationVerificationFailed(url="https://example.com/wrong", result=vr)
    assert e.type == "destination_verification_failed"
    assert e.result.passed is False


def test_phase_c_events_type_not_in_init():
    """type field must be auto-set for all Phase C events — not accepted via __init__."""
    vr = VerificationResult(url="http://x.com", passed=False, mode="off", score=None, reason="r")
    c = Candidate(value="v", raw_value="v", zone="neutral", nearby_text="t", position=0, score=0.5, features={})
    rl = RankedLink(text="t", url="http://x.com", score=0.5)
    with pytest.raises(TypeError):
        GoalPreprocessed(type="injected", goal_context=None, duration_ms=1, source="fresh")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        LinksRanked(type="injected", page_url="http://x.com", total_links=1, top_links=[rl], duration_ms=1)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        CandidatesExtracted(type="injected", page_url="http://x.com", candidates=[c], duration_ms=1)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        ModelSkipped(type="injected", page_url="http://x.com", reason="ranker_confident", decision="d", confidence=0.9)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        DestinationVerificationFailed(type="injected", url="http://x.com", result=vr)  # type: ignore[call-arg]


def test_phase_c_events_timestamps_are_strings():
    vr = VerificationResult(url="http://x.com", passed=False, mode="off", score=None, reason="r")
    c = Candidate(value="v", raw_value="v", zone="neutral", nearby_text="t", position=0, score=0.5, features={})
    rl = RankedLink(text="t", url="http://x.com", score=0.5)
    events = [
        GoalPreprocessed(goal_context=None, duration_ms=1, source="fresh"),
        LinksRanked(page_url="http://x.com", total_links=1, top_links=[rl], duration_ms=1),
        CandidatesExtracted(page_url="http://x.com", candidates=[c], duration_ms=1),
        ModelSkipped(page_url="http://x.com", reason="ranker_confident", decision="http://x.com/r", confidence=0.9),
        DestinationVerificationFailed(url="http://x.com", result=vr),
    ]
    for e in events:
        assert isinstance(e.timestamp, str), f"{type(e).__name__}.timestamp must be str"
        assert e.timestamp


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
