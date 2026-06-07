"""
Candidate extractor — spec §6.

For fact-extraction goals, finds candidate values on the page before the model
sees anything. Each extractor is specialized for one GoalType and returns
Candidate instances sorted by score descending.

DefaultCandidateExtractor dispatches to the right specialist based on
GoalContext.goal_type. FreeformFactExtractor returns an empty list, signalling
the engine to fall back to whole-page model reading (v1.4 freeform mode).

Zone attribution: ExtractedPage.text is zone-ordered (content before neutral
before chrome) but zones are not delimited by markers. Zone is estimated from
character-position fraction (see _estimate_zone). Proper zone attribution would
require re-parsing the HTML — this approximation is accurate enough for scoring
and will improve if ExtractedPage gains explicit zone boundaries.

Scoring weights (_W_*) are hand-tuned starting points per spec §6.4; they are
the part of the design most likely to need iteration against real-world pages.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date as _date
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

from charlotte.core.text_normalization import normalize_text
from charlotte.models import Candidate

if TYPE_CHECKING:
    from charlotte.core.extractor import ExtractedPage
    from charlotte.models import GoalContext

# ---------------------------------------------------------------------------
# Scoring constants — spec §6.4
# ---------------------------------------------------------------------------

_ZONE_WEIGHTS: dict[str, float] = {"content": 1.0, "neutral": 0.5, "chrome": 0.3}
_W_ZONE: float = 0.35
_W_ANCHOR: float = 0.30
_W_NEGATIVE: float = 0.15   # subtracted when a negative term is nearer than anchor
_W_FORMAT: float = 0.10
_W_UNIQUENESS: float = 0.10

_ANCHOR_DECAY: float = 500.0   # chars; anchor_proximity reaches 0 at this distance
_NEARBY_WINDOW: int = 50        # chars of preceding text captured as nearby_text

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _estimate_zone(position: int, text_len: int) -> Literal["content", "neutral", "chrome"]:
    if text_len == 0:
        return "neutral"
    f = position / text_len
    if f < 0.40:
        return "content"
    if f < 0.75:
        return "neutral"
    return "chrome"


def _nearest_distance(position: int, text_norm: str, terms: list[str]) -> float | None:
    best: float | None = None
    for term in terms:
        t = normalize_text(term)
        if not t:
            continue
        idx = 0
        while True:
            found = text_norm.find(t, idx)
            if found == -1:
                break
            d = abs(position - found)
            if best is None or d < best:
                best = d
            idx = found + 1
    return best


def _format_quality(value: str, regex_hints: list[str]) -> float:
    """1.0 on full match, 0.5 on partial match, 0.0 on no match or no hints."""
    if not regex_hints:
        return 0.0
    for pat in regex_hints:
        try:
            if re.fullmatch(pat, value, re.IGNORECASE):
                return 1.0
        except re.error:
            continue
    for pat in regex_hints:
        try:
            if re.search(pat, value, re.IGNORECASE):
                return 0.5
        except re.error:
            continue
    return 0.0


def _compute_score(
    value: str,
    position: int,
    zone: Literal["content", "neutral", "chrome"],
    text_norm: str,
    goal_context: "GoalContext",
    value_counts: dict[str, int],
) -> tuple[float, dict[str, float]]:
    zone_weight = _ZONE_WEIGHTS[zone]

    anchor_dist = _nearest_distance(position, text_norm, goal_context.anchor_terms)
    anchor_proximity = (
        max(0.0, 1.0 - anchor_dist / _ANCHOR_DECAY) if anchor_dist is not None else 0.0
    )

    neg_dist = _nearest_distance(position, text_norm, goal_context.negative_terms)
    negative_proximity = (
        max(0.0, 1.0 - neg_dist / _ANCHOR_DECAY)
        if neg_dist is not None and (anchor_dist is None or neg_dist < anchor_dist)
        else 0.0
    )

    format_quality = _format_quality(value, goal_context.regex_hints)
    uniqueness = 1.0 / max(value_counts.get(value, 1), 1)

    features: dict[str, float] = {
        "zone_weight": zone_weight,
        "anchor_proximity": anchor_proximity,
        "negative_proximity": negative_proximity,
        "format_quality": format_quality,
        "uniqueness": uniqueness,
    }
    raw = (
        _W_ZONE * zone_weight
        + _W_ANCHOR * anchor_proximity
        - _W_NEGATIVE * negative_proximity
        + _W_FORMAT * format_quality
        + _W_UNIQUENESS * uniqueness
    )
    return max(0.0, min(1.0, raw)), features


def _build_candidate(
    raw_value: str,
    value: str,
    position: int,
    page_text: str,
    goal_context: "GoalContext",
    value_counts: dict[str, int],
) -> Candidate:
    zone = _estimate_zone(position, len(page_text))
    text_norm = normalize_text(page_text)
    nearby_text = page_text[max(0, position - _NEARBY_WINDOW):position].strip()
    score, features = _compute_score(value, position, zone, text_norm, goal_context, value_counts)
    return Candidate(
        value=value,
        raw_value=raw_value,
        zone=zone,
        nearby_text=nearby_text,
        position=position,
        score=score,
        features=features,
    )


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class CandidateExtractorProtocol(Protocol):
    """Extract scored candidates from a page for a fact-extraction goal.

    Returns a list sorted by score descending. An empty list signals the engine
    to fall back to whole-page model reading (freeform mode). See spec §6.2.
    """

    async def __call__(
        self,
        *,
        goal_context: "GoalContext",
        page: "ExtractedPage",
        locale: str = "en_US",
    ) -> list[Candidate]: ...


# ---------------------------------------------------------------------------
# PhoneNumberExtractor
# ---------------------------------------------------------------------------

# Matches NANP and simple international numbers; requires at least an area code.
_PHONE_RE = re.compile(
    r"""
    (?:\+?1[\s\-.]?)?          # optional +1 country code
    \(?(\d{3})\)?[\s\-.]?      # area code
    (\d{3})[\s\-.]?            # exchange
    (\d{4})                    # subscriber
    (?!\d)                     # not followed by more digits
    """,
    re.VERBOSE,
)


def _normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+1-{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits[0] == "1":
        return f"+1-{digits[1:4]}-{digits[4:7]}-{digits[7:]}"
    return raw.strip()


class PhoneNumberExtractor:
    """Extracts and normalizes phone numbers from page text."""

    async def __call__(
        self,
        *,
        goal_context: "GoalContext",
        page: "ExtractedPage",
        locale: str = "en_US",
    ) -> list[Candidate]:
        value_counts: dict[str, int] = {}
        matches: list[tuple[str, str, int]] = []  # (raw, normalized, position)
        for m in _PHONE_RE.finditer(page.text):
            raw = m.group(0)
            norm = _normalize_phone(raw)
            value_counts[norm] = value_counts.get(norm, 0) + 1
            matches.append((raw, norm, m.start()))

        return sorted(
            (
                _build_candidate(raw, norm, pos, page.text, goal_context, value_counts)
                for raw, norm, pos in matches
            ),
            key=lambda c: c.score,
            reverse=True,
        )


# ---------------------------------------------------------------------------
# DateExtractor
# ---------------------------------------------------------------------------

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_PAT = (
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December|"
    "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
)

_DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_MDY_RE = re.compile(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b")
_DATE_LONG_RE = re.compile(
    rf"\b({_MONTH_PAT})\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.IGNORECASE
)
_DATE_MY_RE = re.compile(
    rf"\b({_MONTH_PAT})\s+(\d{{4}})\b", re.IGNORECASE
)


def _parse_date(m: re.Match[str], pattern_id: str) -> str | None:
    try:
        if pattern_id == "iso":
            return _date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        if pattern_id == "mdy":
            return _date(int(m[3]), int(m[1]), int(m[2])).isoformat()
        if pattern_id == "long":
            month = _MONTHS.get(m[1].lower())
            return _date(int(m[3]), month, int(m[2])).isoformat() if month else None
        if pattern_id == "my":
            month = _MONTHS.get(m[1].lower())
            return _date(int(m[2]), month, 1).isoformat() if month else None
    except ValueError:
        return None
    return None


class DateExtractor:
    """Extracts and ISO-normalizes dates from page text."""

    async def __call__(
        self,
        *,
        goal_context: "GoalContext",
        page: "ExtractedPage",
        locale: str = "en_US",
    ) -> list[Candidate]:
        matches: list[tuple[str, str, int]] = []
        for pat, pid in (
            (_DATE_ISO_RE, "iso"),
            (_DATE_LONG_RE, "long"),
            (_DATE_MY_RE, "my"),
            (_DATE_MDY_RE, "mdy"),
        ):
            for m in pat.finditer(page.text):
                norm = _parse_date(m, pid)  # type: ignore[arg-type]
                if norm:
                    matches.append((m.group(0), norm, m.start()))

        value_counts: dict[str, int] = {}
        for _, norm, _ in matches:
            value_counts[norm] = value_counts.get(norm, 0) + 1

        return sorted(
            (
                _build_candidate(raw, norm, pos, page.text, goal_context, value_counts)
                for raw, norm, pos in matches
            ),
            key=lambda c: c.score,
            reverse=True,
        )


# ---------------------------------------------------------------------------
# AddressExtractor
# ---------------------------------------------------------------------------

_STREET_TYPES = (
    r"Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Road|Rd|"
    r"Way|Lane|Ln|Court|Ct|Place|Pl|Parkway|Pkwy|Circle|Cir|Highway|Hwy"
)
_ADDRESS_RE = re.compile(
    rf"(?<!\w)\d{{1,5}}\s+[A-Za-z][A-Za-z0-9\s\-.]{{1,40}}"
    rf"(?:{_STREET_TYPES})\.?"
    r"(?:\s+(?:Ste|Suite|Apt|Unit|#)\s*[\w\-]+)?"
    r"(?:,\s*[A-Za-z][\w\s]{2,25})?",
    re.IGNORECASE,
)


class AddressExtractor:
    """Extracts US postal addresses from page text using structural heuristics."""

    async def __call__(
        self,
        *,
        goal_context: "GoalContext",
        page: "ExtractedPage",
        locale: str = "en_US",
    ) -> list[Candidate]:
        value_counts: dict[str, int] = {}
        matches: list[tuple[str, str, int]] = []
        for m in _ADDRESS_RE.finditer(page.text):
            raw = m.group(0).strip()
            norm = " ".join(raw.split())
            value_counts[norm] = value_counts.get(norm, 0) + 1
            matches.append((raw, norm, m.start()))

        return sorted(
            (
                _build_candidate(raw, norm, pos, page.text, goal_context, value_counts)
                for raw, norm, pos in matches
            ),
            key=lambda c: c.score,
            reverse=True,
        )


# ---------------------------------------------------------------------------
# PriceExtractor
# ---------------------------------------------------------------------------

_PRICE_RE = re.compile(
    r"(?:[$€£¥₹])\s*[\d,]+(?:\.\d{1,2})?"
    r"|[\d,]+(?:\.\d{1,2})?\s*(?:USD|EUR|GBP|JPY|CAD|AUD)\b",
    re.IGNORECASE,
)


class PriceExtractor:
    """Extracts price / currency values from page text."""

    async def __call__(
        self,
        *,
        goal_context: "GoalContext",
        page: "ExtractedPage",
        locale: str = "en_US",
    ) -> list[Candidate]:
        value_counts: dict[str, int] = {}
        matches: list[tuple[str, str, int]] = []
        for m in _PRICE_RE.finditer(page.text):
            raw = m.group(0).strip()
            norm = " ".join(raw.split())
            value_counts[norm] = value_counts.get(norm, 0) + 1
            matches.append((raw, norm, m.start()))

        return sorted(
            (
                _build_candidate(raw, norm, pos, page.text, goal_context, value_counts)
                for raw, norm, pos in matches
            ),
            key=lambda c: c.score,
            reverse=True,
        )


# ---------------------------------------------------------------------------
# DocumentLinkExtractor
# ---------------------------------------------------------------------------

_DOC_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".csv", ".txt", ".rtf", ".odt", ".ods", ".odp", ".zip", ".epub",
})


class DocumentLinkExtractor:
    """Extracts document-link candidates from page.links.

    Candidate value is the URL; raw_value is the anchor text. Matches links
    that have a known document extension OR whose URL / anchor text satisfies
    a regex_hint. When no regex_hints are set, only extension-matching links
    are returned.
    """

    async def __call__(
        self,
        *,
        goal_context: "GoalContext",
        page: "ExtractedPage",
        locale: str = "en_US",
    ) -> list[Candidate]:
        url_counts: dict[str, int] = {}
        for link in page.links:
            url = link.get("url", "")
            if url:
                url_counts[url] = url_counts.get(url, 0) + 1

        candidates: list[Candidate] = []
        for idx, link in enumerate(page.links):
            url = link.get("url", "")
            text = link.get("text", "")
            if not url:
                continue

            path = urlsplit(url).path
            filename = path.rsplit("/", 1)[-1]
            ext = ("." + filename.rsplit(".", 1)[-1]).lower() if "." in filename else ""

            is_doc = ext in _DOC_EXTENSIONS
            hint_q = max(
                _format_quality(url, goal_context.regex_hints),
                _format_quality(text, goal_context.regex_hints),
                _format_quality(filename, goal_context.regex_hints),
            )
            has_hint = hint_q > 0

            if not is_doc and not has_hint:
                continue

            # Estimate position from link text location in page text.
            position = page.text.find(text) if text else -1
            if position == -1:
                position = idx * 80  # fallback: approximate from link list index

            candidates.append(
                _build_candidate(text or filename, url, position, page.text, goal_context, url_counts)
            )

        return sorted(candidates, key=lambda c: c.score, reverse=True)


# ---------------------------------------------------------------------------
# FreeformFactExtractor
# ---------------------------------------------------------------------------

class FreeformFactExtractor:
    """No-op extractor for freeform_fact goals.

    Returns an empty list unconditionally. An empty candidate list signals the
    engine to fall back to whole-page model reading (v1.4 freeform mode). See
    spec §6.5, §6.3.
    """

    async def __call__(
        self,
        *,
        goal_context: "GoalContext",
        page: "ExtractedPage",
        locale: str = "en_US",
    ) -> list[Candidate]:
        return []


# ---------------------------------------------------------------------------
# DefaultCandidateExtractor — dispatcher
# ---------------------------------------------------------------------------

class DefaultCandidateExtractor:
    """Dispatch to the goal-type-appropriate extractor. See spec §6.3.

    For navigation goals, returns an empty list (extraction is not applicable;
    the link ranker handles navigation goals). For freeform_fact, delegates to
    FreeformFactExtractor which also returns an empty list (signals freeform
    mode). All other goal types have a specialized extractor.
    """

    def __init__(self) -> None:
        self._extractors: dict[str, CandidateExtractorProtocol] = {
            "phone_extraction": PhoneNumberExtractor(),
            "date_extraction": DateExtractor(),
            "address_extraction": AddressExtractor(),
            "price_extraction": PriceExtractor(),
            "document_link": DocumentLinkExtractor(),
            "freeform_fact": FreeformFactExtractor(),
        }

    async def __call__(
        self,
        *,
        goal_context: "GoalContext",
        page: "ExtractedPage",
        locale: str = "en_US",
    ) -> list[Candidate]:
        extractor = self._extractors.get(goal_context.goal_type)
        if extractor is None:
            return []  # navigation goals — handled by link ranker, not extractor
        return await extractor(goal_context=goal_context, page=page, locale=locale)
