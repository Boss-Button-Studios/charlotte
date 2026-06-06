"""
Small helpers extracted from engine.py to keep that file under the 600-line cap.

These are private to the engine layer — not public API.
"""

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING

from charlotte.exceptions import CharlotteInternalError
from charlotte.models import CrawlResult

if TYPE_CHECKING:
    from charlotte.models import GoalContext


def _resolve_default_adapter():
    """Instantiate the default adapter from CharlotteConfig (spec §5.1).

    Consults CHARLOTTE_DEFAULT_ADAPTER ('local' or 'groq'). Falls back to
    LocalAdapter. Each constructor raises CharlotteConfigError with a clear
    message if its requirements are not met.
    """
    from charlotte.config import CharlotteConfig
    adapter_name = CharlotteConfig.default_adapter()
    if adapter_name == "groq":
        from charlotte.adapters.groq import GroqAdapter
        return GroqAdapter()
    from charlotte.adapters.local import LocalAdapter
    return LocalAdapter()


def _empty_result(*, budget_exhausted: bool) -> CrawlResult:
    return CrawlResult(
        found=False,
        result_urls=[],
        content=None,
        confidence=0.0,
        pages_visited=0,
        depth_reached=0,
        visit_log=[],
        best_candidate_url=None,
        budget_exhausted=budget_exhausted,
    )


def _elapsed_ms(start: float) -> int:
    return int((monotonic() - start) * 1000)


def _rank_links(ranker, goal_context: "GoalContext", links: list) -> list:
    """Call ranker, re-raising any exception as CharlotteInternalError."""
    try:
        return ranker(goal_context, links)
    except Exception as exc:
        raise CharlotteInternalError(
            f"Link ranker raised an unexpected error: {exc}. "
            "Please report this at https://github.com/Boss-Button-Studios/charlotte/issues"
        ) from exc
