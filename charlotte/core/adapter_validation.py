"""
Adapter output validation for Charlotte (CHAR-006, spec §6.5).

Validates raw adapter output against the strict schema in §6.5, retries once
with a reinforced schema hint on failure, and raises AdapterOutputError if
both attempts fail. This is Charlotte's responsibility, not the adapter's.

Public types:    AdapterOutput
Public function: validate_adapter_output, call_with_validation
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from charlotte.exceptions import AdapterOutputError

if TYPE_CHECKING:
    from charlotte.adapters.base import AdapterProtocol

_MAX_REASONING_CHARS: int = 4096
_MAX_ANSWER_CHARS: int = 1024
_MAX_LINKS_TO_FOLLOW: int = 50
_TRUNCATION_SUFFIX: str = " [truncated]"

# Matches ANSI CSI escape sequences and non-printable control characters,
# including tab (\x09). CR (\x0d) and LF (\x0a) are handled separately
# (normalised to a single space rather than stripped outright).
_CONTROL_CHAR_RE = re.compile(
    r"[\x00-\x09\x0b\x0c\x0e-\x1f\x7f]"   # control chars including tab, except CR/LF
    r"|\x1b\[[0-9;]*[a-zA-Z]"              # ANSI CSI escape sequences
)


def _sanitize_text(text: str, max_chars: int) -> str:
    """Strip control chars, normalize newlines to spaces, truncate at max_chars.

    The returned string is guaranteed to be at most max_chars characters long,
    including the ' [truncated]' suffix when truncation occurs.
    """
    text = _CONTROL_CHAR_RE.sub("", text)
    text = re.sub(r"\r\n|\r|\n", " ", text)
    if len(text) > max_chars:
        text = text[:max_chars - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX
    return text


# Injected into the adapter prompt on retry when the first response fails
# schema validation. Restates all field requirements explicitly. See §6.5.
_SCHEMA_HINT = (
    "Your previous response did not match the required output schema. "
    "You MUST return a JSON object with exactly these fields: "
    '"found" (boolean), '
    '"confidence" (float between 0.0 and 1.0 inclusive), '
    '"result_url" (non-null URL string when found=true, null when found=false), '
    '"links_to_follow" (array of URL strings, may be empty), '
    '"reasoning" (non-empty string), '
    '"answer" (string with the extracted fact when found=true and the goal is factual, '
    'null otherwise — optional field). '
    "Respond with JSON only — no prose outside the object."
)


@dataclass
class AdapterOutput:
    """Validated adapter output. All fields are guaranteed clean and correct.

    Produced by call_with_validation(). The engine acts only on AdapterOutput,
    never on the raw dict returned by the adapter. See spec §6.5.
    """

    found: bool
    confidence: float
    result_url: str | None   # Non-null iff found=True
    links_to_follow: list[str]
    reasoning: str
    answer: str | None = None  # Verbatim extracted value for factual goals; null otherwise


def _is_valid_url(value: object) -> bool:
    """Return True if value is a non-empty http/https URL string."""
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def validate_adapter_output(raw: object) -> AdapterOutput:
    """Validate raw adapter output against the schema defined in spec §6.5.

    Checks all five required fields for presence, correct types, and
    constraint satisfaction. Invalid links_to_follow items are silently
    dropped; all other violations raise ValueError.

    Args:
        raw: The value returned by the adapter (expected to be a dict).

    Returns:
        AdapterOutput with all fields validated and cleaned.

    Raises:
        ValueError: Describes the first constraint violation found.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"adapter output must be a dict, got {type(raw).__name__}")

    _ALLOWED_KEYS = {"found", "confidence", "result_url", "links_to_follow", "reasoning", "answer"}
    extra_keys = set(raw) - _ALLOWED_KEYS
    if extra_keys:
        raise ValueError(f"unexpected field(s): {', '.join(sorted(extra_keys))}")

    # --- found ---
    if "found" not in raw:
        raise ValueError("missing required field: 'found'")
    found = raw["found"]
    if not isinstance(found, bool):
        raise ValueError(f"'found' must be a boolean, got {type(found).__name__}")

    # --- confidence ---
    if "confidence" not in raw:
        raise ValueError("missing required field: 'confidence'")
    confidence = raw["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError(f"'confidence' must be a float, got {type(confidence).__name__}")
    confidence = float(confidence)
    if not (0.0 <= confidence <= 1.0):
        raise ValueError(f"'confidence' must be in [0.0, 1.0], got {confidence}")

    # --- result_url ---
    if "result_url" not in raw:
        raise ValueError("missing required field: 'result_url'")
    result_url = raw["result_url"]
    if found:
        if result_url is None:
            raise ValueError("'result_url' must not be null when 'found' is true")
        if not _is_valid_url(result_url):
            raise ValueError(f"'result_url' is not a valid URL: {result_url!r}")
    else:
        if result_url is not None:
            raise ValueError("'result_url' must be null when 'found' is false")

    # --- links_to_follow ---
    if "links_to_follow" not in raw:
        raise ValueError("missing required field: 'links_to_follow'")
    raw_links = raw["links_to_follow"]
    if not isinstance(raw_links, list):
        raise ValueError(
            f"'links_to_follow' must be a list, got {type(raw_links).__name__}"
        )
    # Invalid URL items are silently dropped; the response is not rejected.
    # Cap at _MAX_LINKS_TO_FOLLOW after filtering — a model returning thousands of
    # links is either confused or being manipulated; log volume would also be abusive.
    links_to_follow = [item for item in raw_links if _is_valid_url(item)]
    links_to_follow = links_to_follow[:_MAX_LINKS_TO_FOLLOW]

    # --- reasoning ---
    if "reasoning" not in raw:
        raise ValueError("missing required field: 'reasoning'")
    reasoning = raw["reasoning"]
    if not isinstance(reasoning, str):
        raise ValueError(f"'reasoning' must be a string, got {type(reasoning).__name__}")
    # Sanitize before the non-empty check — control chars could produce whitespace-only
    # strings that appear non-empty before sanitization.
    reasoning = _sanitize_text(reasoning, _MAX_REASONING_CHARS)
    if not reasoning.strip():
        raise ValueError("'reasoning' must not be empty or whitespace-only")

    # --- answer (optional) ---
    answer = raw.get("answer", None)
    if answer is not None:
        if not found:
            raise ValueError("'answer' must be null when 'found' is false")
        if not isinstance(answer, str):
            raise ValueError(f"'answer' must be a string, got {type(answer).__name__}")
        answer = _sanitize_text(answer, _MAX_ANSWER_CHARS)
        if not answer.strip():
            raise ValueError("'answer' must not be empty or whitespace-only")

    return AdapterOutput(
        found=found,
        confidence=confidence,
        result_url=result_url if found else None,
        links_to_follow=links_to_follow,
        reasoning=reasoning,
        answer=answer,
    )


async def call_with_validation(
    adapter: "AdapterProtocol",
    *,
    goal: str,
    navigation_hint: str | None,
    page_title: str,
    page_url: str,
    page_summary: str,
    available_links: list[dict[str, str]],
    visit_history: list[str],
    results_so_far: int,
    schema_hint: str | None = None,
    reference_date: date | None = None,
) -> AdapterOutput:
    """Call an adapter, validate its output, and retry once with a schema hint.

    On the first schema validation failure, the adapter is called a second time
    with a schema reminder injected into the prompt (T-09). If the second
    response also fails validation, AdapterOutputError is raised and the caller
    should treat the page as unevaluable (T-10). See spec §6.5.

    If the adapter itself raises AdapterOutputError (e.g., API failure), that
    exception is re-raised immediately without a schema retry.

    Args:
        adapter: Any object satisfying AdapterProtocol.
        goal, navigation_hint, page_title, page_url, page_summary,
        available_links, visit_history, results_so_far: Page context passed
            directly to the adapter unchanged.
        schema_hint: Optional hint passed as ``schema_hint`` to the adapter
            on the first call. Used by the engine's H3 plausibility retry
            to inject a reinforced navigation reminder. If None (default),
            the adapter receives ``schema_hint=None`` on the first attempt
            and ``schema_hint=_SCHEMA_HINT`` on a schema-validation retry.

    Returns:
        Validated AdapterOutput ready for the engine to act on.

    Raises:
        AdapterOutputError: Adapter raised an exception, or both validation
            attempts failed.
    """
    common: dict = dict(
        goal=goal,
        navigation_hint=navigation_hint,
        page_title=page_title,
        page_url=page_url,
        page_summary=page_summary,
        available_links=available_links,
        visit_history=visit_history,
        results_so_far=results_so_far,
        reference_date=reference_date,
    )

    # First attempt — optional hint (None for normal calls; reinforced text for H3 retry)
    try:
        raw = await adapter(schema_hint=schema_hint, **common)
    except AdapterOutputError:
        raise
    except Exception as exc:
        raise AdapterOutputError("Adapter call failed before validation") from exc
    try:
        return validate_adapter_output(raw)
    except ValueError:
        pass  # Fall through to retry with reinforced schema hint

    # Second attempt — reinforced schema hint (T-09 path succeeds here)
    try:
        raw = await adapter(schema_hint=_SCHEMA_HINT, **common)
    except AdapterOutputError:
        raise
    except Exception as exc:
        raise AdapterOutputError("Adapter retry failed before validation") from exc
    try:
        return validate_adapter_output(raw)
    except ValueError as exc:
        # Both attempts failed (T-10) — treat page as unevaluable
        raise AdapterOutputError(
            f"Adapter output failed schema validation after two attempts: {exc}"
        ) from exc
