"""
GoalContext cache and AutoPreprocessor — spec §4.4, §4.6.

Separated from goal_preprocessor to keep each file under the 600-line cap.
InMemoryGoalContextCache caches GoalContext objects within a single crawl.
AutoPreprocessor selects the preprocessing strategy automatically.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import charlotte.models as _models
from charlotte.core.goal_preprocessor import (
    DeterministicPreprocessor,
    GoalPreprocessorProtocol,
    HybridPreprocessor,
    _HYBRID_BASE_URL,
    _HYBRID_MODEL,
)
from charlotte.core.text_normalization import normalize_text
from charlotte.models import GoalContext


# ---------------------------------------------------------------------------
# Cache protocol and in-memory implementation
# ---------------------------------------------------------------------------

@runtime_checkable
class GoalContextCacheProtocol(Protocol):
    def get_or_create(
        self,
        goal: str,
        navigation_hint: str | None,
        locale: str,
        preprocessor: GoalPreprocessorProtocol,
    ) -> GoalContext: ...


class InMemoryGoalContextCache:
    """Dict-backed GoalContext cache scoped to a single crawl.

    Cache key includes locale and CACHE_FORMAT_VERSION so that locale changes
    and library upgrades always produce fresh contexts (spec §4.6).
    """

    def __init__(self) -> None:
        self._store: dict[tuple, GoalContext] = {}

    def get_or_create(
        self,
        goal: str,
        navigation_hint: str | None,
        locale: str,
        preprocessor: GoalPreprocessorProtocol,
    ) -> GoalContext:
        key = (
            normalize_text(goal),
            normalize_text(navigation_hint or ""),
            type(preprocessor).__name__,
            preprocessor.model_id,
            locale,
            _models.CACHE_FORMAT_VERSION,  # read at call time so bumps bust cached entries
        )
        if key not in self._store:
            self._store[key] = preprocessor(goal, navigation_hint, locale)
        return self._store[key]


# ---------------------------------------------------------------------------
# AutoPreprocessor — selects strategy automatically from goal type (spec §4.4)
# ---------------------------------------------------------------------------

class AutoPreprocessor:
    """Zero-config preprocessor: fast for navigation, smart for fact goals.

    Runs DeterministicPreprocessor first for a free goal-type signal.
    Navigation goals return immediately with no model call. Fact-type goals
    (freeform_fact, phone_extraction, etc.) escalate to HybridPreprocessor
    for synonym expansion and better classification. Falls back silently to
    the deterministic result if the model call fails.
    """

    model_id: str | None

    def __init__(
        self, *, base_url: str = _HYBRID_BASE_URL, model: str = _HYBRID_MODEL,
        timeout: float | None = None,
    ) -> None:
        self._deterministic = DeterministicPreprocessor()
        self._hybrid = HybridPreprocessor(base_url=base_url, model=model, timeout=timeout)
        self.model_id = model

    def __call__(self, goal: str, navigation_hint: str | None, locale: str) -> GoalContext:
        ctx = self._deterministic(goal, navigation_hint, locale)
        if ctx.goal_type == "navigation":
            return ctx
        return self._hybrid(goal, navigation_hint, locale)
