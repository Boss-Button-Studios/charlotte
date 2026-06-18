"""Model-call instrumentation (telemetry).

Counts real model invocations per crawl — the goal-preprocessor call, the base
page evaluation, and the schema-validation retry inside ``call_with_validation`` —
so the true model-call cost is *observable* rather than inferred from
``ModelDecision`` events (which count pages, not calls). The schema retry and the
engine's plausibility re-evaluations are otherwise invisible in the event stream.

A ``ContextVar`` holds the per-crawl tally. ``reset()`` starts a fresh tally in the
current context; ``record()`` is a no-op when no tally is active, so the counted
call sites stay safe to invoke outside a crawl (unit tests, the preprocessor used
standalone). Crawls awaited sequentially in one task each ``reset()`` the tally;
crawls launched as separate asyncio tasks each copy the context and tally
independently.
"""

from __future__ import annotations

import contextvars
from collections import Counter

# Reason tags used as keys in the snapshot dict.
BASE = "base"                  # first model attempt of an evaluation (incl. plausibility re-evals)
SCHEMA_RETRY = "schema_retry"  # second attempt after the first produced invalid JSON/schema
PREPROCESSOR = "preprocessor"  # goal preprocessing (synonym/anchor expansion)

_calls: contextvars.ContextVar["Counter[str]"] = contextvars.ContextVar("charlotte_model_calls")


def reset() -> None:
    """Begin a fresh per-crawl tally in the current context."""
    _calls.set(Counter())


def record(reason: str) -> None:
    """Count one model invocation under *reason*. No-op outside an active tally."""
    try:
        _calls.get()[reason] += 1
    except LookupError:
        pass


def snapshot() -> dict[str, int]:
    """Return the current tally as a plain dict (empty when no tally is active)."""
    try:
        return dict(_calls.get())
    except LookupError:
        return {}


def total() -> int:
    """Total model invocations recorded in the current context."""
    try:
        return sum(_calls.get().values())
    except LookupError:
        return 0
