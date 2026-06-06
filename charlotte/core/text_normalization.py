"""
Text normalization shared across all v2 components — spec §4.5.1.

Applied symmetrically by the goal preprocessor, link ranker, candidate
extractor, and destination verifier so that inputs normalized during
GoalContext validation are comparable to inputs normalized at scoring time.

Three-step pipeline (applied in order):
  1. Unicode NFKC — collapses compatibility forms (fullwidth, halfwidth,
     ligatures) into canonical equivalents.
  2. Whitespace folding — runs of whitespace become a single space; leading
     and trailing whitespace stripped.
  3. Casefolding — lower-cases using Unicode casefold rules (handles
     non-ASCII cases that str.lower() misses).

Comparisons use the normalized form; stored values retain original case
(the caller applies normalization only when comparing, not when storing).
"""

from __future__ import annotations

import re
import unicodedata

_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """NFKC → whitespace fold → casefold.  Returns a normalized string."""
    text = unicodedata.normalize("NFKC", text)
    text = _WS_RE.sub(" ", text).strip()
    return text.casefold()


def tokenize(text: str) -> list[str]:
    """Normalize then split on whitespace.  Returns a list of tokens."""
    return normalize_text(text).split()
