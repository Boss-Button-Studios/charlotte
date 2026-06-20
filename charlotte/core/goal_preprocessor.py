"""
Goal preprocessor — spec §4.

GoalPreprocessorProtocol defines the callable interface. DeterministicPreprocessor
is the Phase A default: tokenizes the goal into anchor_terms with no model calls.
HybridPreprocessor (Phase B) calls a local model to expand synonyms and improve
goal-type classification, falling back to DeterministicPreprocessor on any failure.

Cache and AutoPreprocessor live in goal_context_cache to keep this file under
the 600-line cap.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Protocol, runtime_checkable

import httpx

from charlotte.core import model_metrics
from charlotte.core.text_normalization import normalize_text, tokenize
from charlotte.models import GoalContext, GoalType

_logger = logging.getLogger(__name__)

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
    # Navigation prose — these describe the type of thing we're looking for,
    # not the thing itself.  High IDF in anchor-text corpora causes noise links
    # like "Search page" to outscore genuinely relevant links.
    "page", "site", "web", "section",
})

# ---------------------------------------------------------------------------
# Goal-type detection (keyword rules, first match wins)
# ---------------------------------------------------------------------------

_GOAL_TYPE_RULES: list[tuple[str, GoalType]] = [
    # More specific multi-word patterns first
    ("phone number", "phone_extraction"),
    ("phone #", "phone_extraction"),
    ("how much", "price_extraction"),
    ("when was", "date_extraction"),
    ("when is", "date_extraction"),
    ("download the", "document_link"),
    ("download a", "document_link"),
    # Single-word triggers
    ("phone", "phone_extraction"),
    ("address", "address_extraction"),
    ("price", "price_extraction"),
    ("pricing", "price_extraction"),
    ("fee", "price_extraction"),
    ("cost", "price_extraction"),
    ("date", "date_extraction"),
    ("published", "date_extraction"),
    ("schedule", "date_extraction"),
    ("pdf", "document_link"),
    ("bulletin", "document_link"),
    ("handbook", "document_link"),
    (".doc", "document_link"),
    (".xlsx", "document_link"),
    (".csv", "document_link"),
    ("hours", "freeform_fact"),
    # WH-question fact patterns — placed after all specific-type triggers so that
    # "What is the price?" still matches "price" → price_extraction first.
    ("what is the name", "freeform_fact"),
    ("what is the", "freeform_fact"),
    ("what are the", "freeform_fact"),
    ("what are", "freeform_fact"),
    ("what does", "freeform_fact"),
    ("what function", "freeform_fact"),
    ("what version", "freeform_fact"),
    ("who is", "freeform_fact"),
    ("who are", "freeform_fact"),
    ("how many", "freeform_fact"),
]


def _detect_goal_type(goal_normalized: str) -> GoalType:
    for keyword, goal_type in _GOAL_TYPE_RULES:
        if " " in keyword or keyword.startswith("."):
            # Multi-word and extension patterns are specific enough for plain substring.
            if keyword in goal_normalized:
                return goal_type
        elif re.search(
            r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])",
            goal_normalized,
        ):
            # Single-token trigger: require token boundaries to avoid e.g.
            # "coffee" → fee, "unpublished" → published.
            return goal_type
    return "navigation"


# ---------------------------------------------------------------------------
# Temporal reference date detection
# ---------------------------------------------------------------------------

# Terms that signal the goal is time-relative; when matched, the preprocessor
# stamps GoalContext.reference_date with today's date so the model knows what
# "latest" or "current" means during page evaluation.
_TEMPORAL_RE = re.compile(
    r"\b(?:latest|newest|most\s+recent|recent|current|today"
    r"|this\s+week|this\s+month|this\s+year|upcoming)\b",
    re.IGNORECASE,
)


def _detect_reference_date(goal: str, navigation_hint: str | None) -> date | None:
    """Return today's date if the goal or hint contains temporal terms, else None."""
    combined = goal + " " + (navigation_hint or "")
    return date.today() if _TEMPORAL_RE.search(combined) else None


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
            reference_date=_detect_reference_date(goal, navigation_hint),
        )


# ---------------------------------------------------------------------------
# HybridPreprocessor — model-assisted synonym expansion (Phase B, spec §4.4)
# ---------------------------------------------------------------------------

# Matches reasoning-model think-blocks before the JSON answer.
_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_LONE_CLOSE_THINK_RE = re.compile(r"^.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
# Matches JSON wrapped in a markdown code fence (```json ... ``` or ``` ... ```).
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE)
# Python raw-string literal: r"..." — some models write regex patterns this way.
# \b ensures we only match a standalone r token (not the r at the end of a word
# like "manager"), since r"..." is only valid Python syntax after non-word chars.
_RAWSTR_RE = re.compile(r'\br"([^"]*)"')
# JavaScript-style // comment at end of a JSON line.
# Requires at least one space/tab before // so we don't match :// inside URLs.
_JSON_COMMENT_RE = re.compile(r'(?<!:)[ \t]+//[^\n]*')
# Python-style implicit string concatenation: "a"\n"b" → "a",\n"b".
# JSON strings can't contain unescaped quotes, so this only fires between two
# separate string literals that are missing a comma separator.
_ADJACENT_STRINGS_RE = re.compile(r'"(\s+)"')
# ANSI escape sequences and non-printable ASCII control chars (§4.5.4).
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|\x1b\[[0-9;]*[A-Za-z]")

_HYBRID_BASE_URL = "http://localhost:11434"
_HYBRID_MODEL = "deepseek-r1:14b"
_COMPLETIONS_PATH = "/v1/chat/completions"

_GOAL_TYPES: frozenset[str] = frozenset({
    "navigation", "phone_extraction", "date_extraction", "address_extraction",
    "price_extraction", "document_link", "freeform_fact",
})

_HYBRID_SYSTEM = """\
You are a web-crawl goal analyzer. For each goal, produce JSON that helps a \
web crawler find the right page faster.

Your primary job is to expand the goal with SYNONYMS and NEGATIVE TERMS. \
This is the main value you add beyond simple keyword matching — do not leave \
synonyms empty for any meaningful goal.

JSON fields:
  "goal_type"            — one of: navigation, phone_extraction, date_extraction,
                           address_extraction, price_extraction, document_link,
                           freeform_fact
  "goal_type_confidence" — float 0.0–1.0
  "synonyms"             — object mapping each key term (verbatim in goal) to a
                           list of alternative phrasings a website might use.
                           Keys MUST appear in the goal. Do NOT leave this empty.
  "anchor_terms"         — the most discriminating tokens from the goal (skip
                           generic words like "find", "the", "page")
  "negative_terms"       — terms that indicate the WRONG page. MUST NOT appear
                           in the goal and MUST NOT overlap synonyms or anchor_terms.
  "regex_hints"          — valid Python regex patterns (fact goals only); [] for
                           navigation goals
  "description"          — one plain-English sentence describing what to find

Example input:  "Find the contact page"
Example output:
{
  "goal_type": "navigation",
  "goal_type_confidence": 0.95,
  "synonyms": {
    "contact": ["contact us", "get in touch", "reach us", "contact information",
                "email us", "connect with us"]
  },
  "anchor_terms": ["contact"],
  "negative_terms": ["home", "about", "careers", "sitemap", "login", "news"],
  "regex_hints": [],
  "description": "Find the page where visitors can contact the organization."
}

Respond with JSON only — no explanation text, no code fences, no markdown."""


def _clean_model_json(content: str) -> str:
    """Strip model-specific syntax that makes otherwise-valid JSON unparseable.

    Handles three patterns observed in llama3.1 / codellama output:
      - Python raw-string literals: ``r"\\b..."`` → ``"\\\\b..."``
        (backslashes are double-escaped so they survive json.loads as literals)
      - JavaScript ``//`` end-of-line comments (e.g. after a closing ``]``)
        Requires at least one space/tab before ``//`` so ``://`` inside URLs
        is never touched.
      - Python-style implicit string concatenation: ``"a"\\n"b"`` → ``"a",\\n"b"``
        (missing comma between adjacent string literals in an array)
    """
    def _fix_raw(m: re.Match) -> str:
        return '"' + m.group(1).replace("\\", "\\\\") + '"'

    content = _RAWSTR_RE.sub(_fix_raw, content)
    content = _JSON_COMMENT_RE.sub("", content)
    content = _ADJACENT_STRINGS_RE.sub('",\n"', content)
    return content


def _extract_json(content: str) -> dict:
    """Extract a JSON object from model output using three fallback strategies.

    Applies _clean_model_json first, then tries strategies in order:
      1. Entire content is valid JSON.
      2. JSON inside a markdown code fence (``` or ```json).
      3. First ``{`` in the content — parse forward using raw_decode so trailing
         prose after the closing ``}`` is silently ignored.

    Raises ValueError if no parseable JSON object is found.
    """
    content = _clean_model_json(content)

    # Strategy 1: clean JSON
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # Strategy 2: inside a code fence
    fence = _FENCE_RE.search(content)
    if fence:
        try:
            result = json.loads(fence.group(1).strip())
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    # Strategy 3: first { … } object, ignoring surrounding prose
    start = content.find("{")
    if start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(content, start)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    raise ValueError("no parseable JSON object found in model output")


def _validate_hybrid_output(
    raw: dict,
    goal: str,
    navigation_hint: str | None,
    locale: str,
    model_used: str,
) -> GoalContext:
    """Validate model output per §4.5 and return GoalContext. Raises ValueError on rejection."""
    warnings: list[str] = []

    def _san(s: object, field: str) -> str:
        """§4.5.4: strip ANSI escape sequences and ASCII control characters."""
        text = s if isinstance(s, str) else str(s)
        clean = _CTRL_RE.sub("", text)
        if clean != text:
            warnings.append(f"sanitization: control chars stripped from {field}")
        return clean

    # §4.5.2 goal_type
    goal_type_raw = _san(raw.get("goal_type", ""), "goal_type")
    if goal_type_raw not in _GOAL_TYPES:
        raise ValueError(f"invalid goal_type: {goal_type_raw!r}")
    goal_type: GoalType = goal_type_raw  # type: ignore[assignment]

    # §4.5.2 goal_type_confidence
    conf = raw.get("goal_type_confidence")
    if not isinstance(conf, (int, float)) or not (0.0 <= float(conf) <= 1.0):
        raise ValueError(f"invalid goal_type_confidence: {conf!r}")

    # §4.5.1 normalize goal + hint for all boundary checks
    goal_norm = normalize_text(goal)
    hint_norm = normalize_text(navigation_hint or "")
    combined_norm = f"{goal_norm} {hint_norm}".strip()

    def _in_combined(term_norm: str) -> bool:
        return bool(re.search(
            r"(?<![a-z0-9])" + re.escape(term_norm) + r"(?![a-z0-9])",
            combined_norm,
        ))

    # §4.5.2 synonyms — keys must appear in goal/hint (token-boundary, case-insensitive)
    raw_synonyms = raw.get("synonyms") or {}
    synonyms: dict[str, list[str]] = {}
    if isinstance(raw_synonyms, dict):
        for k, v in raw_synonyms.items():
            k_clean = _san(k, "synonyms key")
            if not _in_combined(normalize_text(k_clean)):
                # §4.5.2: a synonym key that doesn't appear in the goal/hint is a hard
                # rejection, not a silent drop — consistent with negative_terms below and
                # the confusable-key defense (§11.6). The caller falls back to
                # deterministic preprocessing for the whole context.
                raise ValueError(f"synonym key {k_clean!r} not in goal/hint")
            synonyms[k_clean] = [_san(vv, "synonym value")
                                  for vv in (v if isinstance(v, list) else [])
                                  if isinstance(vv, str)]

    # §4.5.2 anchor_terms — must be tokens/sequences from goal/hint
    raw_anchors = raw.get("anchor_terms") or []
    anchor_terms: list[str] = []
    if isinstance(raw_anchors, list):
        for t in raw_anchors:
            if not isinstance(t, str):
                continue
            t_clean = _san(t, "anchor_term")
            if not _in_combined(normalize_text(t_clean)):
                warnings.append(f"near_miss: anchor_term {t_clean!r} not in goal/hint, dropped")
                continue
            anchor_terms.append(t_clean)
    if not anchor_terms:
        raw_tokens = tokenize(goal) + (tokenize(navigation_hint) if navigation_hint else [])
        anchor_terms = [tok for tok in raw_tokens if tok not in _STOP_WORDS and len(tok) > 1]

    # §4.5.2 regex_hints — compile each; drop invalid (record in warnings)
    raw_regex = raw.get("regex_hints") or []
    if isinstance(raw_regex, str):
        # Model returned a bare string instead of a list — coerce and warn.
        warnings.append("format_coerced: regex_hints was a string, wrapped in list")
        raw_regex = [raw_regex]
    regex_hints: list[str] = []
    if isinstance(raw_regex, list):
        for pattern in raw_regex:
            if not isinstance(pattern, str):
                continue
            p_clean = _san(pattern, "regex_hint")
            try:
                re.compile(p_clean)
                regex_hints.append(p_clean)
            except re.error as exc:
                warnings.append(f"regex_dropped: {exc}: {p_clean!r}")

    # §4.5.3 negative_terms — must not overlap positives or appear in goal/hint
    raw_negatives = raw.get("negative_terms") or []
    positive_norms = (
        {normalize_text(k) for k in synonyms}
        | {normalize_text(v) for vs in synonyms.values() for v in vs}
        | {normalize_text(t) for t in anchor_terms}
    )
    negative_terms: list[str] = []
    if isinstance(raw_negatives, list):
        for nt in raw_negatives:
            if not isinstance(nt, str):
                continue
            nt_clean = _san(nt, "negative_term")
            nt_norm = normalize_text(nt_clean)
            if _in_combined(nt_norm):
                raise ValueError(f"negative_term {nt_clean!r} appears in goal/hint")
            if nt_norm in positive_norms:
                raise ValueError(f"negative_term {nt_clean!r} overlaps positive terms")
            negative_terms.append(nt_clean)

    description = _san(str(raw.get("description", "")), "description")

    # §4.5.5: the 4 KB cap is on the post-normalization *serialized* form (the cached
    # value), measured in bytes — not a sum of field-length char counts. Serialize the
    # semantic fields deterministically and measure the UTF-8 byte length so the bound
    # accounts for structure and multi-byte characters.
    serialized = json.dumps(
        {
            "goal": goal,
            "navigation_hint": navigation_hint,
            "goal_type": goal_type,
            "goal_type_confidence": float(conf),
            "synonyms": synonyms,
            "anchor_terms": anchor_terms,
            "negative_terms": negative_terms,
            "regex_hints": regex_hints,
            "description": description,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    size_bytes = len(serialized.encode("utf-8"))
    if size_bytes > 4096:
        raise ValueError(f"GoalContext exceeds 4KB cap ({size_bytes} bytes)")

    return GoalContext(
        goal=goal,
        navigation_hint=navigation_hint,
        goal_type=goal_type,
        goal_type_confidence=float(conf),
        synonyms=synonyms,
        anchor_terms=anchor_terms,
        negative_terms=negative_terms,
        regex_hints=regex_hints,
        description=description,
        source="model",
        model_used=model_used,
        created_at=datetime.now(timezone.utc),
        locale=locale,
        validation_warnings=warnings,
        reference_date=_detect_reference_date(goal, navigation_hint),
    )


class HybridPreprocessor:
    """Phase B preprocessor — model-assisted synonym expansion (spec §4.4).

    Calls a local OpenAI-compatible inference server to classify the goal type,
    expand synonyms, and generate regex hints. Falls back silently to
    DeterministicPreprocessor on any failure (network, parse, or validation).

    **Threading note:** The model call uses a synchronous ``httpx.Client``.
    ``GoalPreprocessorProtocol.__call__`` is intentionally synchronous; the
    engine (C4) will invoke it in a thread via ``asyncio.to_thread`` before
    the async crawl loop starts, preventing event-loop blocking.

    For fast crawls, configure a small model — the default (deepseek-r1:14b)
    produces higher quality but adds latency.
    """

    model_id: str | None

    def __init__(
        self,
        *,
        base_url: str = _HYBRID_BASE_URL,
        model: str = _HYBRID_MODEL,
        timeout: float | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._fallback = DeterministicPreprocessor()
        self.model_id = model

    def __call__(
        self,
        goal: str,
        navigation_hint: str | None,
        locale: str,
    ) -> GoalContext:
        try:
            return self._call_model(goal, navigation_hint, locale)
        except Exception:
            _logger.debug("HybridPreprocessor fell back to DeterministicPreprocessor",
                          exc_info=True)
            return self._fallback(goal, navigation_hint, locale)

    def _call_model(self, goal: str, navigation_hint: str | None, locale: str) -> GoalContext:
        model_metrics.record(model_metrics.PREPROCESSOR)
        hint_line = f"\nNavigation hint: {navigation_hint}" if navigation_hint else ""
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _HYBRID_SYSTEM},
                {"role": "user", "content": f"Goal: {goal}{hint_line}"},
            ],
            "format": "json",
        }
        with httpx.Client(timeout=self._timeout or 30.0) as client:
            resp = client.post(f"{self._base_url}{_COMPLETIONS_PATH}", json=payload)
        resp.raise_for_status()

        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"malformed completion response: {exc}") from exc
        content = _THINK_RE.sub("", content).strip()
        content = _LONE_CLOSE_THINK_RE.sub("", content).strip()
        return _validate_hybrid_output(_extract_json(content), goal, navigation_hint, locale,
                                       self._model)

