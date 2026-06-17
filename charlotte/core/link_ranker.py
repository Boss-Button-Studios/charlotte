"""
BM25 link ranker — spec §5.

Scores extracted page links against GoalContext.anchor_terms (and synonym
expansions) using BM25Okapi, then returns them sorted best-first for:

  1. The model — model receives ranked links so the most relevant candidates
     appear first in its context window.
  2. The crawl queue — links are enqueued with their BM25 score so the
     priority queue visits higher-relevance pages before lower-relevance ones.

Normalization is applied symmetrically (§4.5.1): anchor_terms stored in
GoalContext are already normalized; link anchor text is normalized here before
scoring so both sides use the same canonical form (T-69).

When GoalContext.reference_date is set (triggered by "latest", "recent", etc.),
a recency bonus is added to each link's BM25 score based on the age of any
date detected in the link's anchor text or URL path.  This implements the
"latest = top of stack" discipline: the most recently dated link always
surfaces before older links with equivalent BM25 scores.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from urllib.parse import urlsplit

from charlotte.core.text_normalization import normalize_text, tokenize

if TYPE_CHECKING:
    from charlotte.models import GoalContext

# File extensions and URL structural noise that carry no semantic signal.
_URL_SEP_RE = re.compile(r"[/\-_.]")
_URL_EXT_SKIP = frozenset({
    "", "html", "htm", "xhtml", "php", "asp", "aspx", "jsp",
    "xml", "json", "pdf", "rss", "atom",
})

# ---------------------------------------------------------------------------
# Temporal scoring — "latest = top of stack" discipline
# ---------------------------------------------------------------------------

_MONTH_NAMES: dict[str, int] = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

# YYYY-MM-DD or YYYY/MM/DD
_ISO_DATE_RE = re.compile(
    r"\b(20\d\d)[/\-](0?[1-9]|1[0-2])[/\-](0?[1-9]|[12]\d|3[01])\b"
)
# MM-DD-YYYY or MM/DD/YYYY
_MDY_DATE_RE = re.compile(
    r"\b(0?[1-9]|1[0-2])[/\-](0?[1-9]|[12]\d|3[01])(?!\d)[/\-](20\d\d)\b"
)
# "June 14" / "June-14th" / "January 4th, 2026" — named month + day, optional year.
# (?!\d) after the day prevents "2026" from matching as day=20 + noise.
_NAMED_DATE_RE = re.compile(
    r"\b(january|jan|february|feb|march|mar|april|apr|may|june|jun"
    r"|july|jul|august|aug|september|sept?|october|oct|november|nov|december|dec)"
    r"[\s\-_]+(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?(?!\d)"
    r"(?:[\s,\-_]+(20\d\d))?\b",
    re.IGNORECASE,
)

# A bare 4-digit year (20xx) standing alone as a URL path segment, e.g. the
# "2025" in /wp-content/uploads/2025/06/June-15th-Bulletin.pdf.  Used to
# anchor named-month dates that carry no inline year.
_PATH_YEAR_RE = re.compile(r"(?<![0-9])(20\d\d)(?![0-9])")

# Compact YYYYMMDD in a URL filename, e.g. 20260614B.pdf (parishesonline.com style).
# Searched only in the URL path — not anchor text — to avoid colliding with
# invoice numbers, phone numbers, or other 8-digit sequences in page copy.
_YYYYMMDD_RE = re.compile(
    r"(?<![0-9])(20\d\d)(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?![0-9])"
)

# Recency bonus caps at this value for a zero-day-old link — chosen to exceed
# the ~1.43 BM25 score that "Parish History" (anchor + URL both match "parish")
# produces when "parish" is an anchor term, so a dated bulletin always wins.
_TEMPORAL_BONUS_MAX: float = 2.0
# Bonus halves every 30 days; at 161 days (January in June) it drops to ~0.05.
_TEMPORAL_HALF_LIFE: float = 30.0


def _infer_year(month: int, day: int, reference: date) -> int:
    """Return the year placing (month, day) most recently without exceeding 14 days future."""
    for year in (reference.year, reference.year - 1):
        try:
            candidate = date(year, month, day)
            if candidate <= reference + timedelta(days=14):
                return year
        except ValueError:
            pass
    return reference.year


def _extract_date(anchor: str, url: str, reference: date) -> date | None:
    """Extract the publication date most likely to represent this link.

    Searches anchor text and URL path for ISO (YYYY-MM-DD), MDY (MM-DD-YYYY),
    and named-month patterns.  Returns the plausible date closest to
    reference_date, excluding dates more than 14 days in the future.

    When a named-month pattern has no inline year (e.g. "June-15th-Bulletin.pdf"),
    the year is taken from a lone 20xx segment in the URL path if exactly one is
    present (e.g. /2025/06/ → 2025), falling back to _infer_year otherwise.
    """
    path = urlsplit(url).path
    combined = anchor + " " + path
    cutoff = reference + timedelta(days=14)
    candidates: list[date] = []

    # Collect any explicit 20xx years present as URL path segments.
    path_years = {int(m.group(1)) for m in _PATH_YEAR_RE.finditer(path)}

    for m in _YYYYMMDD_RE.finditer(path):
        try:
            candidates.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass

    for m in _ISO_DATE_RE.finditer(combined):
        try:
            candidates.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass

    for m in _MDY_DATE_RE.finditer(combined):
        try:
            candidates.append(date(int(m.group(3)), int(m.group(1)), int(m.group(2))))
        except ValueError:
            pass

    for m in _NAMED_DATE_RE.finditer(combined):
        month = _MONTH_NAMES.get(m.group(1).lower())
        if month is None:
            continue
        try:
            day = int(m.group(2))
            year_str = m.group(3)
            if year_str:
                year = int(year_str)
            elif len(path_years) == 1:
                year = next(iter(path_years))
            else:
                year = _infer_year(month, day, reference)
            candidates.append(date(year, month, day))
        except ValueError:
            pass

    plausible = [d for d in candidates if d <= cutoff]
    if not plausible:
        return None
    return min(plausible, key=lambda d: abs((reference - d).days))


def _temporal_bonus(link_date: date | None, reference: date) -> float:
    """Recency bonus for a link whose publication date is link_date.

    Peaks at _TEMPORAL_BONUS_MAX for a zero-day-old (or slightly future) date
    and halves every _TEMPORAL_HALF_LIFE days.  Returns 0.0 when no date
    was detected.
    """
    if link_date is None:
        return 0.0
    days_old = max(0, (reference - link_date).days)
    return _TEMPORAL_BONUS_MAX * (0.5 ** (days_old / _TEMPORAL_HALF_LIFE))


def _anchor_tokens(text: str) -> list[str]:
    """Tokenize anchor text and emit de-pluralised forms alongside plural tokens.

    Mirrors the de-pluralisation applied by ``_url_path_tokens`` so that an
    anchor like "Service Names and Port Numbers" contributes both "names" and
    "name" (and "numbers"/"number") to the BM25 document.  Without this, a
    verbose anchor such as "Service Names and Transport Protocol Port Numbers"
    is penalised by BM25 length normalisation relative to short anchors like
    "Port Type Names" even though it is a better match for singular query
    terms "name" and "number".
    """
    seen: dict[str, None] = {}
    for tok in tokenize(text):
        seen[tok] = None
        if tok.endswith("s") and len(tok) > 3:
            seen[tok[:-1]] = None
    return list(seen)


def _url_path_tokens(url: str) -> list[str]:
    """Return normalized tokens from the URL path for BM25 corpus augmentation.

    Including path segments alongside anchor text lets the ranker reward links
    like ``library/functools.html`` for the query term ``functools`` even when
    the anchor text uses only generic phrasing (e.g. "Higher-order functions").

    A de-pluralised form is emitted alongside each plural token so that URL
    path segments like ``service-names-port-numbers`` match singular goal terms
    ``name`` and ``number``.  BM25 has no stemming; both forms must be present
    for either to match.  The original plural is kept so it still matches a
    plural query term.

    Tokens are deduplicated (order-preserving) so that URLs where the directory
    and filename share the same name — e.g.
    ``/assignments/gssapi-service-names/gssapi-service-names.xhtml`` — do not
    double-count their tokens and inflate BM25 TF scores artificially.
    """
    path = urlsplit(url).path
    seen: dict[str, None] = {}
    for part in _URL_SEP_RE.split(path):
        tok = normalize_text(part)
        if tok and tok not in _URL_EXT_SKIP and len(tok) > 1:
            seen[tok] = None
            if tok.endswith("s") and len(tok) > 3:
                seen[tok[:-1]] = None
    return list(seen)

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class LinkRankerProtocol(Protocol):
    """Score and rank links against a GoalContext.

    Args:
        goal_context: Preprocessed goal produced by GoalPreprocessorProtocol.
        links:        List of ``{"text": ..., "url": ...}`` dicts from the
                      extractor — raw, un-normalized anchor text.

    Returns:
        List of ``(url, score)`` tuples sorted by score descending.
        When all scores are zero (no term overlap), links are returned in
        their original order with score 0.0.
    """

    def __call__(
        self,
        goal_context: "GoalContext",
        links: list[dict[str, str]],
    ) -> list[tuple[str, float]]: ...


# ---------------------------------------------------------------------------
# Scoring helper
# ---------------------------------------------------------------------------

def _coverage_score(doc_tokens: list[str], query_set: set[str], k1: float) -> float:
    """Score a document by BM25 TF saturation over query term coverage.

    For each query term that appears in doc_tokens, contributes
    ``tf * (k1 + 1) / (tf + k1)`` to the score (BM25 TF saturation, no IDF,
    no length normalization).  Terms not in query_set are ignored.

    Returns 0.0 when no query term appears in the document.
    """
    tf: dict[str, int] = {}
    for tok in doc_tokens:
        if tok in query_set:
            tf[tok] = tf.get(tok, 0) + 1
    return sum(v * (k1 + 1) / (v + k1) for v in tf.values())


# ---------------------------------------------------------------------------
# BM25LinkRanker
# ---------------------------------------------------------------------------

class BM25LinkRanker:
    """Rank links by query-term coverage using BM25 TF saturation (no IDF).

    Each link's score is the sum of k1-saturated TF contributions for each
    query term found in its tokens (anchor text + URL path).  IDF is
    intentionally omitted: on dense IANA-style corpora where terms like
    "service", "name", "port", and "number" appear in the majority of links,
    BM25Okapi's epsilon-adjusted IDF goes negative and penalises documents
    that match the most query terms — the opposite of what we want.  Without
    IDF, a link matching 4 distinct query terms always outscores one matching
    2, regardless of corpus statistics.

    Query = GoalContext.anchor_terms + all synonym values (both pre-normalised).
    Both sides use NFKC normalization + casefolding (§4.5.1 symmetry).
    """

    _K1: float = 1.5  # BM25 TF saturation parameter

    def __call__(
        self,
        goal_context: "GoalContext",
        links: list[dict[str, str]],
    ) -> list[tuple[str, float]]:
        if not links:
            return []

        # Query = anchor_terms + all synonym values (all pre-normalized in GoalContext).
        synonym_expansions: list[str] = []
        for values in goal_context.synonyms.values():
            synonym_expansions.extend(values)
        query = goal_context.anchor_terms + synonym_expansions

        if not query:
            # No signal — return links in original order with zero scores.
            return [(lnk["url"], 0.0) for lnk in links]

        query_set = set(query)
        scores = [
            _coverage_score(
                _anchor_tokens(lnk.get("text") or "") + _url_path_tokens(lnk.get("url") or ""),
                query_set,
                self._K1,
            )
            for lnk in links
        ]

        # When the goal contains temporal terms ("latest", "recent", etc.),
        # add a recency bonus so the most recently dated link surfaces first.
        ref = goal_context.reference_date
        if ref is not None:
            scores = [
                s + _temporal_bonus(
                    _extract_date(lnk.get("text") or "", lnk.get("url") or "", ref),
                    ref,
                )
                for s, lnk in zip(scores, links)
            ]

        # If every score is zero, preserve original DOM order (stable tiebreak).
        if not any(s > 0 for s in scores):
            return [(lnk["url"], 0.0) for lnk in links]

        indexed = sorted(
            enumerate(links),
            key=lambda pair: scores[pair[0]],
            reverse=True,
        )
        return [(lnk["url"], float(scores[i])) for i, lnk in indexed]
