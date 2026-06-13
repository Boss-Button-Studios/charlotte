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
"""

from __future__ import annotations

import re
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
                _anchor_tokens(lnk.get("text", "")) + _url_path_tokens(lnk.get("url") or ""),
                query_set,
                self._K1,
            )
            for lnk in links
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
