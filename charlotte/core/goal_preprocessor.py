"""
Goal preprocessor — spec §4.

GoalPreprocessorProtocol defines the callable interface. DeterministicPreprocessor
is the Phase A default: tokenizes the goal into anchor_terms with no model calls.
InMemoryGoalContextCache caches GoalContext objects within a single crawl.

Phase B will add HybridPreprocessor (model-assisted synonym expansion).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

import charlotte.models as _models
from charlotte.core.text_normalization import normalize_text, tokenize
from charlotte.models import GoalContext, GoalType

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class GoalPreprocessorProtocol(Protocol):
    """Callable that converts (goal, hint, locale) → GoalContext."""

    #: Identifier used as part of the cache key; None for deterministic processors.
    model_id: str | None

    def __call__(
        self,
        goal: str,
        navigation_hint: str | None,
        locale: str,
    ) -> GoalContext: ...


# ---------------------------------------------------------------------------
# Stop words filtered from anchor_terms
# ---------------------------------------------------------------------------

_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "to", "of", "in", "on",
    "at", "by", "for", "with", "about", "from", "into", "through",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "it", "they",
    "this", "that", "these", "those", "and", "or", "but", "if", "not",
    "no", "nor", "so", "yet", "just", "how", "what", "where", "when",
    "who", "which", "find", "get", "go", "look", "search",
})

# ---------------------------------------------------------------------------
# Goal-type detection (keyword rules, first match wins)
# ---------------------------------------------------------------------------

_GOAL_TYPE_RULES: list[tuple[str, GoalType]] = [
    # More specific multi-word patterns first
    ("phone number", "phone_extraction"),
    ("phone #", "phone_extraction"),
    ("how much", "price_extraction"),
    ("download the", "document_link"),
    ("download a", "document_link"),
    # Single-word triggers
    ("phone", "phone_extraction"),
    ("address", "address_extraction"),
    ("price", "price_extraction"),
    ("cost", "price_extraction"),
    ("date", "date_extraction"),
    ("schedule", "date_extraction"),
    ("pdf", "document_link"),
    (".doc", "document_link"),
    (".xlsx", "document_link"),
    (".csv", "document_link"),
]


def _detect_goal_type(goal_normalized: str) -> GoalType:
    for keyword, goal_type in _GOAL_TYPE_RULES:
        if keyword in goal_normalized:
            return goal_type
    return "navigation"


# ---------------------------------------------------------------------------
# DeterministicPreprocessor
# ---------------------------------------------------------------------------

class DeterministicPreprocessor:
    """Phase A default preprocessor — no model calls.

    Produces a GoalContext by tokenizing the goal into anchor_terms and
    applying a keyword heuristic for goal_type. synonyms and regex_hints
    are left empty; Phase B's HybridPreprocessor fills them via a model call.
    """

    model_id: str | None = None

    def __call__(
        self,
        goal: str,
        navigation_hint: str | None,
        locale: str,
    ) -> GoalContext:
        goal_norm = normalize_text(goal)
        hint_norm = normalize_text(navigation_hint or "")

        # Anchor terms: tokens from goal and hint, stop-words removed.
        raw_tokens = tokenize(goal) + (tokenize(navigation_hint) if navigation_hint else [])
        anchor_terms = [t for t in raw_tokens if t not in _STOP_WORDS and len(t) > 1]

        goal_type = _detect_goal_type(goal_norm)
        description = f"Deterministic: {goal_type}, {len(anchor_terms)} anchor term(s)"

        return GoalContext(
            goal=goal,
            navigation_hint=navigation_hint,
            goal_type=goal_type,
            goal_type_confidence=0.7,
            synonyms={},
            anchor_terms=anchor_terms,
            negative_terms=[],
            regex_hints=[],
            description=description,
            source="deterministic",
            model_used=None,
            created_at=datetime.now(timezone.utc),
            locale=locale,
            validation_warnings=[],
        )


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
