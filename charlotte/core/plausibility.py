"""
Navigation plausibility check for Charlotte (spec §9.3).

Layer 3 of the sanitization pipeline. Applied to validated model output before
Charlotte acts on it. Detects five categories of suspicious model behaviour
that indicate the navigator model may have been influenced by page content:

  1. Off-domain links            — links_to_follow contains out-of-scope hostnames
  2. Instruction mirroring       — reasoning echoes injection language ("ignore your
                                   goal", "I have been instructed to...", etc.)
  3. Confidence spike            — high confidence on a page with very thin content
  4. Zero links / no path        — found=False with an empty links_to_follow
  5. High-confidence negative    — found=False with confidence >= 0.9 (contradictory)

Each triggered condition is recorded as a PlausibilityFlag. The engine uses
PlausibilityResult.passed to decide whether to act on the decision or discard
it and retry.

Note: back-links (links_to_follow pointing to already-visited pages) are NOT
flagged here. They are normal model behaviour — a page often links back to its
parent. The engine already skips visited URLs when building the crawl queue.

Public function: check_plausibility(...) -> PlausibilityResult
Public types:    NavDecision, PlausibilityFlag, PlausibilityResult
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from charlotte.exceptions import CharlotteInternalError

# ---------------------------------------------------------------------------
# Thresholds (module-level constants so tests can reference them)
# ---------------------------------------------------------------------------

# Pages with fewer visible words than this are considered "thin content".
THIN_CONTENT_WORD_THRESHOLD: int = 50

# Confidence above this on thin content is flagged as a suspicious spike.
CONFIDENCE_SPIKE_THRESHOLD: float = 0.8

# found=False with confidence at or above this value is semantically contradictory.
HIGH_CONFIDENCE_NEGATIVE_THRESHOLD: float = 0.9

# Patterns in reasoning that mirror instruction-following language rather than
# navigation reasoning. All are matched case-insensitively against the full
# reasoning string. Derived from spec §9.3 and injection research.
_INSTRUCTION_MIRROR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"i have been instructed",
        r"my new goal is",
        r"ignore (?:your |the )?(?:previous |prior )?(?:goal|instructions?|task)",
        r"new (?:goal|instructions?|task) is",
        r"disregard (?:your |the )?(?:previous |prior )?(?:goal|instructions?)",
        r"instead[,.]?\s+(?:i will|i should|let me|my goal)",
        r"you are now\b",
        r"forget (?:your |the )?(?:previous |prior )?(?:goal|instructions?|task)",
    ]
]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class NavDecision:
    """Validated navigator model output for one page evaluation.

    All fields have passed §6.5 schema validation before reaching the
    plausibility check. The engine constructs this from the adapter's raw dict.

    Attributes:
        found:           True if the model believes the current page satisfies
                         the goal.
        confidence:      Model's confidence in the found assessment (0.0–1.0).
        result_url:      URL of the found result; non-None only when found=True.
        links_to_follow: Ordered list of URLs the model recommends visiting
                         next, best first. May be empty.
        reasoning:       Model's brief explanation of its decision. Non-empty
                         (§6.5 validation rejects empty reasoning).
    """

    found: bool
    confidence: float
    result_url: str | None
    links_to_follow: list[str]
    reasoning: str


@dataclass
class PlausibilityFlag:
    """A single triggered plausibility condition.

    Attributes:
        name:   Short machine-readable identifier (e.g. "off_domain_link").
        detail: Human-readable explanation suitable for the visit log.
    """

    name: str
    detail: str


@dataclass
class PlausibilityResult:
    """Outcome of a plausibility check on one NavDecision.

    Attributes:
        passed: True if no flags were triggered — the engine may act on the
                decision. False means the decision should be discarded.
        flags:  All triggered conditions. Empty when passed=True.
    """

    passed: bool
    flags: list[PlausibilityFlag] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def check_plausibility(
    decision: NavDecision,
    page_text: str,
    allowed_domains: set[str] | frozenset[str] | None,
    visited_urls: set[str],
) -> PlausibilityResult:
    """Check a validated model decision for signs of prompt injection or drift.

    Evaluates five flag conditions defined in spec §9.3. Any triggered flag
    causes the result to be marked as failed. The engine discards failed
    decisions and retries or skips the page.

    Args:
        decision:        Validated model output for the current page.
        page_text:       Visible text extracted from the page (pre-truncation).
                         Used for the thin-content confidence-spike check.
        allowed_domains: Hostnames the crawl is scoped to. None means no
                         domain restriction — the off-domain check is skipped.
        visited_urls:    Accepted but unused — retained for call-site compatibility.

    Returns:
        PlausibilityResult with passed=True and empty flags if all checks pass;
        passed=False with one or more flags if any check fails.

    Raises:
        CharlotteInternalError: Unexpected internal failure during evaluation.
    """
    try:
        flags: list[PlausibilityFlag] = []

        flag = _check_off_domain(decision, allowed_domains)
        if flag:
            flags.append(flag)

        flag = _check_instruction_mirroring(decision)
        if flag:
            flags.append(flag)

        flag = _check_confidence_spike(decision, page_text)
        if flag:
            flags.append(flag)

        flag = _check_zero_links_no_path(decision)
        if flag:
            flags.append(flag)

        flag = _check_high_confidence_negative(decision)
        if flag:
            flags.append(flag)

        return PlausibilityResult(passed=not flags, flags=flags)

    except CharlotteInternalError:
        raise
    except Exception as exc:
        raise CharlotteInternalError(
            "Plausibility check failed unexpectedly — please report this at "
            "https://github.com/Boss-Button-Studios/charlotte/issues: "
            f"{exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Private flag checks
# ---------------------------------------------------------------------------

def _check_off_domain(
    decision: NavDecision,
    allowed_domains: set[str] | frozenset[str] | None,
) -> PlausibilityFlag | None:
    """Flag if any recommended link is outside the allowed domain set."""
    if allowed_domains is None:
        return None
    offenders: list[str] = []
    for url in decision.links_to_follow:
        hostname = urlsplit(url).hostname or ""
        if hostname not in allowed_domains:
            offenders.append(url)
    if not offenders:
        return None
    # Log hostnames only — full URLs may carry query-string tokens.
    sample_hosts = [urlsplit(u).hostname or "" for u in offenders[:3]]
    return PlausibilityFlag(
        name="off_domain_link",
        detail=(
            f"{len(offenders)} link(s) outside allowed_domains in "
            f"links_to_follow (host sample): {sample_hosts}"
        ),
    )


def _check_instruction_mirroring(
    decision: NavDecision,
) -> PlausibilityFlag | None:
    """Flag if reasoning echoes instruction-following language."""
    for pattern in _INSTRUCTION_MIRROR_PATTERNS:
        match = pattern.search(decision.reasoning)
        if match:
            return PlausibilityFlag(
                name="instruction_mirroring",
                detail=(
                    f"Reasoning contains injection-like language "
                    f"(matched pattern: {match.group()!r})"
                ),
            )
    return None


def _check_confidence_spike(
    decision: NavDecision,
    page_text: str,
) -> PlausibilityFlag | None:
    """Flag if confidence spikes on a page with thin visible content."""
    word_count = len(page_text.split())
    if (
        word_count < THIN_CONTENT_WORD_THRESHOLD
        and decision.confidence > CONFIDENCE_SPIKE_THRESHOLD
    ):
        return PlausibilityFlag(
            name="confidence_spike",
            detail=(
                f"Confidence {decision.confidence:.2f} on a page with only "
                f"{word_count} visible words (threshold: "
                f"{THIN_CONTENT_WORD_THRESHOLD} words / "
                f"{CONFIDENCE_SPIKE_THRESHOLD} confidence)"
            ),
        )
    return None


def _check_zero_links_no_path(
    decision: NavDecision,
) -> PlausibilityFlag | None:
    """Flag if the model reports found=False with no links to follow."""
    if not decision.found and not decision.links_to_follow:
        return PlausibilityFlag(
            name="zero_links_no_path",
            detail=(
                "Model returned found=False with an empty links_to_follow — "
                "no navigation path available."
            ),
        )
    return None


def _check_high_confidence_negative(
    decision: NavDecision,
) -> PlausibilityFlag | None:
    """Flag if the model says found=False but reports very high confidence.

    found=False with confidence >= HIGH_CONFIDENCE_NEGATIVE_THRESHOLD is
    semantically contradictory: high confidence means the page strongly
    satisfies the goal, which implies found should be True. This pattern
    typically indicates model confusion or a misunderstood prompt.
    """
    if not decision.found and decision.confidence >= HIGH_CONFIDENCE_NEGATIVE_THRESHOLD:
        return PlausibilityFlag(
            name="high_confidence_negative",
            detail=(
                f"Model reported found=False with confidence={decision.confidence:.2f} "
                f"(threshold: {HIGH_CONFIDENCE_NEGATIVE_THRESHOLD}) — "
                "high confidence contradicts a negative finding."
            ),
        )
    return None
