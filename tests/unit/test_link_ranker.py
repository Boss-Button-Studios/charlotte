"""Unit tests for BM25LinkRanker (spec §5, T-69)."""

from charlotte.core.goal_preprocessor import DeterministicPreprocessor
from charlotte.core.link_ranker import BM25LinkRanker

_PREPROCESSOR = DeterministicPreprocessor()
_RANKER = BM25LinkRanker()


def _ctx(goal: str, navigation_hint: str | None = None):
    return _PREPROCESSOR(goal, navigation_hint, "en_US")


# ---------------------------------------------------------------------------
# Basic ranking
# ---------------------------------------------------------------------------

def test_matching_link_scores_higher():
    ctx = _ctx("Find the Python downloads page")
    links = [
        {"text": "Downloads", "url": "https://python.org/downloads/"},
        {"text": "Jobs", "url": "https://python.org/jobs/"},
        {"text": "Community", "url": "https://python.org/community/"},
    ]
    ranked = _RANKER(ctx, links)
    urls = [url for url, _ in ranked]
    assert urls[0] == "https://python.org/downloads/", f"Expected downloads first, got: {urls}"


def test_returns_all_links():
    ctx = _ctx("Find the contact page")
    links = [
        {"text": "Contact", "url": "https://example.com/contact"},
        {"text": "About", "url": "https://example.com/about"},
    ]
    ranked = _RANKER(ctx, links)
    assert len(ranked) == len(links)


def test_scores_are_non_negative():
    ctx = _ctx("Find the contact page")
    links = [
        {"text": "Contact", "url": "https://example.com/contact"},
        {"text": "Something completely unrelated xyz", "url": "https://example.com/xyz"},
    ]
    ranked = _RANKER(ctx, links)
    assert all(score >= 0.0 for _, score in ranked)


def test_sorted_descending_by_score():
    ctx = _ctx("Find the contact page")
    links = [
        {"text": "Contact", "url": "https://example.com/contact"},
        {"text": "About", "url": "https://example.com/about"},
        {"text": "Home", "url": "https://example.com/"},
    ]
    ranked = _RANKER(ctx, links)
    scores = [score for _, score in ranked]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_links_returns_empty():
    ctx = _ctx("Find the contact page")
    assert _RANKER(ctx, []) == []


def test_single_link_returned():
    ctx = _ctx("Find the contact page")
    links = [{"text": "Contact", "url": "https://example.com/contact"}]
    ranked = _RANKER(ctx, links)
    assert len(ranked) == 1
    assert ranked[0][0] == "https://example.com/contact"


def test_empty_anchor_text_gets_zero_score():
    ctx = _ctx("Find the contact page")
    links = [
        {"text": "", "url": "https://example.com/empty"},
        {"text": "Contact", "url": "https://example.com/contact"},
    ]
    ranked = _RANKER(ctx, links)
    score_map = {url: score for url, score in ranked}
    assert score_map["https://example.com/empty"] == 0.0


def test_all_zero_scores_preserves_original_order():
    """When no query term matches any link, original DOM order is preserved."""
    ctx = _ctx("xyzzy quux frobnicate")  # nonsense terms unlikely to match
    links = [
        {"text": "Alpha", "url": "https://example.com/a"},
        {"text": "Beta", "url": "https://example.com/b"},
        {"text": "Gamma", "url": "https://example.com/c"},
    ]
    ranked = _RANKER(ctx, links)
    urls = [url for url, _ in ranked]
    assert urls == ["https://example.com/a", "https://example.com/b", "https://example.com/c"]


# ---------------------------------------------------------------------------
# Normalization symmetry (T-69)
# ---------------------------------------------------------------------------

def test_normalization_symmetry_fullwidth():
    """Fullwidth anchor text and ASCII anchor text rank the same link equivalently."""
    ctx = _ctx("Find the CEO page")
    links_ascii = [
        {"text": "CEO", "url": "https://example.com/ceo"},
        {"text": "Jobs", "url": "https://example.com/jobs"},
    ]
    links_fullwidth = [
        {"text": "ＣＥＯ", "url": "https://example.com/ceo"},  # fullwidth
        {"text": "Jobs", "url": "https://example.com/jobs"},
    ]
    ranked_ascii = _RANKER(ctx, links_ascii)
    ranked_fullwidth = _RANKER(ctx, links_fullwidth)

    top_ascii = ranked_ascii[0][0]
    top_fullwidth = ranked_fullwidth[0][0]
    assert top_ascii == top_fullwidth == "https://example.com/ceo"


def test_normalization_symmetry_case():
    """Link text casing does not affect which link ranks highest."""
    ctx = _ctx("Find the downloads page")
    links_lower = [
        {"text": "downloads", "url": "https://example.com/dl"},
        {"text": "about", "url": "https://example.com/about"},
    ]
    links_upper = [
        {"text": "DOWNLOADS", "url": "https://example.com/dl"},
        {"text": "ABOUT", "url": "https://example.com/about"},
    ]
    ranked_lower = _RANKER(ctx, links_lower)
    ranked_upper = _RANKER(ctx, links_upper)
    assert ranked_lower[0][0] == ranked_upper[0][0]


# ---------------------------------------------------------------------------
# Smoke test matching the plan's verification snippet
# ---------------------------------------------------------------------------

def test_plan_smoke():
    ctx = _ctx("Find the Python downloads page")
    ranked = _RANKER(ctx, [
        {"text": "Downloads", "url": "https://python.org/downloads/"},
        {"text": "Jobs", "url": "https://python.org/jobs/"},
        {"text": "Community", "url": "https://python.org/community/"},
    ])
    assert ranked[0][0] == "https://python.org/downloads/"


# ---------------------------------------------------------------------------
# URL path token scoring
# ---------------------------------------------------------------------------

def test_url_path_tokens_boost_relevant_link():
    """A link whose URL path matches the goal scores higher than a generic one
    with matching anchor text when the goal term only appears in the path."""
    ctx = _ctx("Find the functools module documentation")
    links = [
        # Anchor text is generic; goal term "functools" appears in the URL path.
        {"text": "Higher-order functions and operations on callables",
         "url": "https://docs.python.org/3/library/functools.html"},
        # Anchor text matches "values" which isn't in the goal, URL is unrelated.
        {"text": "Efficient arrays of numeric values",
         "url": "https://docs.python.org/3/library/array.html"},
    ]
    ranked = _RANKER(ctx, links)
    assert ranked[0][0] == "https://docs.python.org/3/library/functools.html", (
        f"Expected functools first, got: {ranked}"
    )


def test_url_path_tokens_hyphenated_segments():
    """Hyphenated URL path segments are split and de-pluralised so they match
    singular goal terms.  Four links are used so BM25 IDF is non-zero.

    Without de-pluralisation, ``auth-namespaces`` wins because its anchor text
    contains ``name`` (a literal query term), while ``service-names-port-numbers``
    has generic anchor text and URL tokens ``names``/``numbers`` that do not
    exactly match the singular query terms ``name``/``number``.
    """
    ctx = _ctx("Find service name and port number page")
    links = [
        # Generic anchor text; goal terms appear only as plurals in the path.
        {"text": "Protocol Registries",
         "url": "https://www.iana.org/assignments/service-names-port-numbers/"},
        # Anchor text contains "name" — the realistic false winner without
        # de-pluralisation because "names" in the path never matched "name".
        {"text": "Algorithm Name Space Values",
         "url": "https://www.iana.org/assignments/auth-namespaces/"},
        {"text": "Time Zones", "url": "https://www.iana.org/time-zones"},
        {"text": "Root Zone Database", "url": "https://www.iana.org/domains/root/db"},
    ]
    ranked = _RANKER(ctx, links)
    assert ranked[0][0] == "https://www.iana.org/assignments/service-names-port-numbers/", (
        f"Expected port-numbers first, got: {ranked}"
    )


def test_url_path_tokens_emits_singular_alongside_plural():
    """_url_path_tokens emits both the original plural and its de-pluralised form."""
    from charlotte.core.link_ranker import _url_path_tokens

    tokens = _url_path_tokens(
        "https://www.iana.org/assignments/service-names-port-numbers/"
    )
    assert "names" in tokens, "original plural should be preserved"
    assert "name" in tokens, "de-pluralised singular should also be emitted"
    assert "numbers" in tokens, "original plural should be preserved"
    assert "number" in tokens, "de-pluralised singular should also be emitted"


def test_page_stop_word_does_not_boost_noise_link():
    """'page' in the goal is a stop word and must not cause 'Search page' to
    outscore a link whose anchor text matches the substantive goal terms.

    BM25 IDF requires N >= 4 links to produce non-zero scores for rare terms,
    so this test uses a realistic-sized corpus matching the homepage scenario.
    """
    ctx = _ctx("Find the itertools module reference page")
    links = [
        {"text": "Search page", "url": "https://docs.python.org/3/search.html"},
        {"text": "Library reference", "url": "https://docs.python.org/3/library/index.html"},
        {"text": "Tutorial", "url": "https://docs.python.org/3/tutorial/index.html"},
        {"text": "What's new", "url": "https://docs.python.org/3/whatsnew/index.html"},
        {"text": "FAQ", "url": "https://docs.python.org/3/faq/index.html"},
    ]
    ranked = _RANKER(ctx, links)
    assert ranked[0][0] == "https://docs.python.org/3/library/index.html", (
        f"Expected library/index first, got: {ranked}"
    )
