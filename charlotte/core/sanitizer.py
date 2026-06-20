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
    # Off-screen: absolute/fixed positioning combined with a large negative offset.
    if re.search(r"position:(?:absolute|fixed)", style) and re.search(r"(?:left|top):-\d{4,}", style):
        return True
    # Text indented far off-screen (classic "text-indent:-9999px" image-replacement).
    if re.search(r"text-indent:-\d{3,}", style):
        return True

    return False


# Matches a flat CSS rule "selector(s) { declarations }".
_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)

# Matches a whole at-rule block (e.g. @media …{ .x{display:none} }), including one level
# of nested rules. These are stripped before flat-rule extraction so a rule that only
# hides under a media/feature query is NOT treated as an unconditional selector — that
# would over-remove content that is actually visible. At-rule-nested hiding is a
# documented out-of-scope case (see strip_hidden), so dropping it here is intentional.
_AT_RULE_BLOCK_RE = re.compile(r"@[a-zA-Z-]+[^{]*\{(?:[^{}]|\{[^{}]*\})*\}", re.DOTALL)


def _hidden_selectors_from_styles(soup) -> list[str]:
    """Selectors from <style> blocks whose declarations hide the matched element.

    Catches the common "class hiding" pattern (e.g. ``.hidden{display:none}`` with
    ``<div class="hidden">…</div>``) that inline-style inspection alone misses: the
    stylesheet is decomposed before its rules can be applied, so the hidden text would
    otherwise survive into extraction. Only flat ``display:none`` / ``visibility:hidden``
    rules are extracted; rules nested in at-rules (``@media`` …) and JS/computed hiding
    are out of scope.
    """
    selectors: list[str] = []
    for style_tag in soup.find_all("style"):
        # Drop at-rule blocks first so their conditionally-applied inner rules are not
        # mistaken for unconditional flat rules.
        css = _AT_RULE_BLOCK_RE.sub("", style_tag.get_text())
        for raw_selector, body in _CSS_RULE_RE.findall(css):
            declarations = re.sub(r"\s+", "", body.lower())
            if "display:none" in declarations or "visibility:hidden" in declarations:
                for sel in raw_selector.split(","):
                    sel = sel.strip()
                    if sel and not sel.startswith("@"):  # skip at-rule preludes
                        selectors.append(sel)
    return selectors


def _strip_invisible(text: str) -> str:
    """Remove invisible Unicode and non-printable control characters from a string."""
    return _CONTROL.sub("", _INVISIBLE.sub("", text))


def strip_hidden(html: str) -> str:
    """Strip hidden and invisible content from HTML per spec section 9.1.

    Removes:
    - <script>, <style>, and <noscript> tag content
    - HTML comments
    - <meta> content attributes (invisible instruction vectors)
    - Elements hidden via an inline style (display:none, visibility:hidden, opacity:0,
      font-size:0, text-indent off-screen) or the HTML hidden attribute
    - Off-screen positioned elements (position:absolute/fixed with left/top <= -1000px)
    - Elements hidden by a flat stylesheet rule — display:none / visibility:hidden in a
      <style> block, matched by selector before the block is decomposed
    - Zero-width and invisible Unicode characters from all text nodes
    - Non-printable control characters (preserving tab and newline)

    The same pass covers link anchor text, making crafted anchor text ineffective
    as a link-ranking manipulation vector.

    Out of scope (a determined author can still hide text these ways; the goal is to
    raise the bar, not to be a CSS engine): rules nested in at-rules (@media, @supports),
    hiding applied by JavaScript at runtime, and exotic clip/transform/zero-size tricks.
    These are documented limits, not silent gaps — see SECURITY.md.

    Args:
        html: Raw HTML string from the page fetcher.

    Returns:
        Sanitized HTML string with all hidden content removed.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")

        # Remove elements hidden by a stylesheet rule (class/id hiding) *before* the
        # <style> blocks are decomposed below — otherwise the rule is gone before it
        # can be applied and the hidden text survives into extraction.
        for selector in _hidden_selectors_from_styles(soup):
            try:
                for el in soup.select(selector):
                    el.decompose()
            except Exception:
                continue  # unsupported/invalid selector — never break sanitization

        for tag in soup.find_all(["script", "style", "noscript"]):
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
