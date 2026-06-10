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


def _url_path_tokens(url: str) -> list[str]:
    """Return normalized tokens from the URL path for BM25 corpus augmentation.

    Including path segments alongside anchor text lets the ranker reward links
    like ``library/functools.html`` for the query term ``functools`` even when
    the anchor text uses only generic phrasing (e.g. "Higher-order functions").
    """
    path = urlsplit(url).path
    tokens = []
    for part in _URL_SEP_RE.split(path):
        tok = normalize_text(part)
        if tok and tok not in _URL_EXT_SKIP and len(tok) > 1:
            tokens.append(tok)
    return tokens

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
# BM25LinkRanker
# ---------------------------------------------------------------------------

class BM25LinkRanker:
    """Rank links using Okapi BM25 over normalized anchor text.

    The BM25 corpus is built from the link anchor texts for the current page.
    The query is the union of GoalContext.anchor_terms and all synonym values
    (synonym keys already appear in anchor_terms via §4.5.2). Both sides are
    NFKC-normalized and casefolded before scoring, matching the normalization
    applied when GoalContext was built (§4.5.1 symmetry requirement).
    """

    def __call__(
        self,
        goal_context: "GoalContext",
        links: list[dict[str, str]],
    ) -> list[tuple[str, float]]:
        if not links:
            return []

        from rank_bm25 import BM25Okapi

        # Build corpus — anchor text tokens + URL path tokens.  URL path tokens
        # let the ranker reward links whose path matches goal terms even when the
        # anchor text is generic (e.g. "library/functools.html" for a functools
        # query).  Use a sentinel token for empty documents so BM25Okapi never
        # receives an empty token list (which raises ValueError).
        corpus = [
            (tokenize(lnk.get("text", "")) + _url_path_tokens(lnk.get("url") or ""))
            or ["__empty__"]
            for lnk in links
        ]

        # Query = anchor_terms + all synonym values (all pre-normalized in GoalContext).
        synonym_expansions: list[str] = []
        for values in goal_context.synonyms.values():
            synonym_expansions.extend(values)
        query = goal_context.anchor_terms + synonym_expansions

        if not query:
            # No signal — return links in original order with zero scores.
            return [(lnk["url"], 0.0) for lnk in links]

        scores = BM25Okapi(corpus).get_scores(query)

        # If every score is zero, preserve original DOM order (stable tiebreak).
        if not any(s > 0 for s in scores):
            return [(lnk["url"], 0.0) for lnk in links]

        indexed = sorted(
            enumerate(links),
            key=lambda pair: scores[pair[0]],
            reverse=True,
        )
        return [(lnk["url"], float(scores[i])) for i, lnk in indexed]
