"""
Navigation Sanitizer - Layer 1: Hidden content stripping (spec section 9.1).

Removes injection vectors from HTML before it reaches the content extractor
or navigator model. Strips hidden elements, invisible Unicode, non-printable
control characters, scripts, styles, comments, and meta content fields.

Public function: strip_hidden(html: str) -> str
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment

from charlotte.exceptions import CharlotteInternalError

# Zero-width and invisible Unicode characters used to embed hidden injection text.
# Adjacent string literals let each range carry an inline comment.
#   U+00AD           soft hyphen
#   U+200B-U+200F    zero-width space, ZWNJ, ZWJ, LTR mark, RTL mark
#   U+2028-U+2029    line separator, paragraph separator
#   U+202A-U+202E    directional embedding/override marks
#   U+2060-U+2064    word joiner, invisible operators
#   U+2066-U+206F    directional isolate and deprecated format marks
#   U+FEFF           BOM / zero-width no-break space
_INVISIBLE: re.Pattern[str] = re.compile(
    "[­"
    "​-‏"
    "  "
    "‪-‮"
    "⁠-⁤"
    "⁦-⁯"
    "﻿]"
)

# Non-printable ASCII control characters -- tab (0x09) and newline (0x0A) are preserved.
_CONTROL: re.Pattern[str] = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]")


def _is_hidden(tag) -> bool:
    """Return True if the element should be removed per spec section 9.1 hiding rules."""
    if tag.get("hidden") is not None:
        return True

    raw_style = tag.get("style", "")
    if not raw_style:
        return False

    # Normalize: lowercase and collapse whitespace for reliable substring matching.
    style = re.sub(r"\s+", "", raw_style.lower())

    if "display:none" in style:
        return True
    if "visibility:hidden" in style:
        return True
    # opacity:0 or 0.000... — not opacity:0.5, opacity:0.1, etc.
    if re.search(r"opacity:0(?:\.0+)?(?:;|$|!important)", style):
        return True
    # font-size:0 or 0.000... with any unit or bare — not 0.5em etc.
    if re.search(r"font-size:0(?:\.0+)?(?:[a-z%]|;|$)", style):
        return True
    # Off-screen: position:absolute combined with left or top offset <= -1000.
    if "position:absolute" in style and re.search(r"(?:left|top):-\d{4,}", style):
        return True

    return False


def _strip_invisible(text: str) -> str:
    """Remove invisible Unicode and non-printable control characters from a string."""
    return _CONTROL.sub("", _INVISIBLE.sub("", text))


def strip_hidden(html: str) -> str:
    """Strip hidden and invisible content from HTML per spec section 9.1.

    Removes:
    - <script> and <style> tag content
    - HTML comments
    - <meta> content attributes (invisible instruction vectors)
    - Elements hidden via CSS (display:none, visibility:hidden, opacity:0,
      font-size:0) or the HTML hidden attribute
    - Off-screen positioned elements (position:absolute with left/top <= -1000px)
    - Zero-width and invisible Unicode characters from all text nodes
    - Non-printable control characters (preserving tab and newline)

    The same pass covers link anchor text, making crafted anchor text ineffective
    as a link-ranking manipulation vector.

    Args:
        html: Raw HTML string from the page fetcher.

    Returns:
        Sanitized HTML string with all hidden content removed.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")

        for tag in soup.find_all(["script", "style"]):
            tag.decompose()

        for node in soup.find_all(string=lambda t: isinstance(t, Comment)):
            node.extract()

        # Strip meta content fields -- they can carry instruction-like text that is
        # invisible to the human reader but present in the DOM.
        for meta in soup.find_all("meta"):
            meta.attrs.pop("content", None)

        # Collect hidden elements before decomposing to avoid mutating the tree
        # mid-iteration. Tags already removed via a parent decomposition are skipped
        # via the parent-is-None guard.
        for tag in soup.find_all(True):
            if tag.parent is not None and _is_hidden(tag):
                tag.decompose()

        for text_node in soup.find_all(string=True):
            cleaned = _strip_invisible(str(text_node))
            if cleaned != str(text_node):
                text_node.replace_with(cleaned)

        return str(soup)
    except CharlotteInternalError:
        raise
    except Exception as exc:
        raise CharlotteInternalError(
            f"HTML sanitization failed — please report this at "
            f"https://github.com/Boss-Button-Studios/charlotte/issues: {exc}"
        ) from exc
