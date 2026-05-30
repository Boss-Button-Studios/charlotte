"""
Stable public data types for Charlotte.

CrawlResult, LinkResult, all streaming event dataclasses, VisitLogEntry,
TrustLevel, and the StreamEvent union type are all defined here. Field names
and types are stable public API — callers depend on attribute access and IDE
completion. Do not rename fields without a major version bump. See spec §7, §17.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Literal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    """ISO 8601 timestamp in UTC, used as the default for event timestamps."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Trust level
# ---------------------------------------------------------------------------

class TrustLevel(Enum):
    """Trust levels for data flowing through Charlotte's pipeline.

    Data does not move from a lower level to a higher one without explicit
    validation. The provenance check (§9.4) is the mechanism for promotion.
    See spec §13.3.
    """
    TRUSTED = "trusted"          # Caller-supplied parameters
    UNTRUSTED = "untrusted"      # All web content: HTML, link text, headers
    SEMI_TRUSTED = "semi_trusted"  # Model output — produced by a trusted component
                                   # operating on untrusted input; must be validated
    PROMOTED = "promoted"        # Model output that passed provenance + plausibility


# ---------------------------------------------------------------------------
# Visit log
# ---------------------------------------------------------------------------

@dataclass
class VisitLogEntry:
    """One entry in the CrawlResult visit log — a single page evaluation."""
    url: str
    depth: int
    found: bool
    confidence: float
    reasoning: str


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class CrawlResult:
    """Complete result returned by crawl().

    Always returned regardless of whether the goal was found. Callers check
    `found` first, then `result_urls`. When found=False, `visit_log` and
    `best_candidate_url` are the primary tools for regrouping. See spec §7.
    """
    found: bool
    # Always a list. When max_results=1 (default), contains at most one item.
    result_urls: list[str]
    # Populated only when return_content=True; same order as result_urls.
    content: list[str] | None
    # Highest model confidence among found results, or best confidence at abandonment.
    confidence: float
    pages_visited: int
    depth_reached: int
    visit_log: list[VisitLogEntry]
    # Highest-confidence URL seen below confidence_threshold, when found=False.
    best_candidate_url: str | None
    budget_exhausted: bool
    # Extracted answer text per result, parallel to result_urls. None per element when
    # the model did not extract an answer (navigation goals). Null when found=False.
    answers: list[str | None] | None = None


@dataclass
class LinkResult:
    """Lightweight result returned by find_link().

    Omits visit_log, depth_reached, and per-page content. Callers who need
    that detail should use crawl(). See spec §5.2.
    """
    found: bool
    urls: list[str]          # All discovered URLs, ordered by confidence.
    confidence: float
    pages_visited: int
    best_candidate_url: str | None
    budget_exhausted: bool
    note: str | None         # Plain-language explanation when found=False.


# ---------------------------------------------------------------------------
# Streaming event dataclasses
# ---------------------------------------------------------------------------
# Each event has a `type` class constant (set automatically, not via __init__)
# and a `timestamp` auto-populated to the current UTC time. All other fields
# are required at construction time. See spec §17.

@dataclass
class CrawlStarted:
    """Emitted once at the very start of a crawl."""
    start_url: str
    goal: str
    max_pages: int
    max_depth: int
    max_results: int | None
    type: Literal["crawl_started"] = field(default="crawl_started", init=False)
    timestamp: str = field(default_factory=_now)


@dataclass
class PageFetched:
    """Emitted after each successful page fetch, before model evaluation."""
    url: str
    depth: int
    http_status: int
    fetch_ms: int            # Fetch duration in milliseconds.
    type: Literal["page_fetched"] = field(default="page_fetched", init=False)
    timestamp: str = field(default_factory=_now)


@dataclass
class ModelDecision:
    """Emitted after the navigator model evaluates a page."""
    url: str
    found: bool
    confidence: float
    links_queued: int        # How many links were enqueued after this decision.
    reasoning: str
    type: Literal["model_decision"] = field(default="model_decision", init=False)
    timestamp: str = field(default_factory=_now)


@dataclass
class ResultFound:
    """Emitted each time Charlotte records a result above confidence_threshold."""
    url: str                 # As found on the page — not normalized.
    confidence: float
    result_index: int        # 1-based index within this crawl.
    # Verbatim extracted value for factual goals; null for navigation goals.
    answer: str | None = None
    type: Literal["result_found"] = field(default="result_found", init=False)
    timestamp: str = field(default_factory=_now)


@dataclass
class PageSkipped:
    """Emitted when a page cannot be fetched, evaluated, or passes plausibility."""
    url: str
    reason: str              # Human-readable skip reason.
    error_type: str | None   # Charlotte error class name, if applicable.
    type: Literal["page_skipped"] = field(default="page_skipped", init=False)
    timestamp: str = field(default_factory=_now)


@dataclass
class BudgetExhausted:
    """Emitted when max_pages or max_depth is reached before the goal is found."""
    pages_visited: int
    depth_reached: int
    best_candidate: str | None
    type: Literal["budget_exhausted"] = field(default="budget_exhausted", init=False)
    timestamp: str = field(default_factory=_now)


@dataclass
class CrawlComplete:
    """Always the last event in the stream, regardless of outcome."""
    found: bool
    result_count: int
    pages_visited: int
    depth_reached: int
    elapsed_ms: int
    type: Literal["crawl_complete"] = field(default="crawl_complete", init=False)
    timestamp: str = field(default_factory=_now)


# ---------------------------------------------------------------------------
# Union type for the event stream
# ---------------------------------------------------------------------------

StreamEvent = (
    CrawlStarted
    | PageFetched
    | ModelDecision
    | ResultFound
    | PageSkipped
    | BudgetExhausted
    | CrawlComplete
)
