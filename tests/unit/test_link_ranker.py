"""Unit tests for BM25LinkRanker (spec §5, T-69)."""

from datetime import date, datetime, timezone

from charlotte.core.goal_preprocessor import DeterministicPreprocessor
from charlotte.core.link_ranker import (
    BM25LinkRanker,
    _extract_date,
    _temporal_bonus,
)
from charlotte.models import GoalContext

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


# ---------------------------------------------------------------------------
# Temporal scoring
# ---------------------------------------------------------------------------

# Fixed reference date for deterministic temporal tests.
_REF = date(2026, 6, 14)


def _ctx_dated(anchor_terms: list[str], ref: date) -> GoalContext:
    """Minimal GoalContext with a specific reference_date for temporal tests."""
    return GoalContext(
        goal="test goal",
        navigation_hint=None,
        goal_type="document_link",
        goal_type_confidence=1.0,
        synonyms={},
        anchor_terms=anchor_terms,
        negative_terms=[],
        regex_hints=[],
        description="test",
        source="deterministic",
        model_used=None,
        created_at=datetime.now(timezone.utc),
        locale="en_US",
        validation_warnings=[],
        reference_date=ref,
    )


def test_extract_date_iso_in_url_path():
    d = _extract_date("", "https://example.com/bulletins/2026-06-14-third-sunday/", _REF)
    assert d == date(2026, 6, 14)


def test_extract_date_mdy_in_url_path():
    d = _extract_date("", "https://example.com/bulletins/06-14-2026-third-sunday/", _REF)
    assert d == date(2026, 6, 14)


def test_extract_date_named_month_in_anchor():
    d = _extract_date("June 14th Bulletin", "https://example.com/bulletin/", _REF)
    assert d == date(2026, 6, 14)


def test_extract_date_named_month_year_from_url_path_segment():
    """Year in a URL path segment (e.g. /2025/06/) should be used for a
    named-month date with no inline year, overriding _infer_year."""
    d = _extract_date(
        "Jun 15",
        "https://holyspiritsd.com/wp-content/uploads/2025/06/June-15th-Bulletin.pdf",
        _REF,
    )
    assert d == date(2025, 6, 15), f"Expected 2025-06-15 from path year, got {d}"


def test_extract_date_named_month_infers_year_when_no_path_year():
    """'January 4th' with no year anywhere should fall back to _infer_year.

    The URL deliberately has no year path segment, so _extract_date cannot read
    the year from the path and must infer it via _infer_year (the branch under test).
    """
    d = _extract_date(
        "January 4th Bulletin",
        "https://holyspiritsd.com/bulletins/January-4th-Bulletin.pdf",
        _REF,
    )
    assert d == date(2026, 1, 4)


def test_extract_date_no_date_returns_none():
    d = _extract_date("Parish History", "https://example.com/parish-history/", _REF)
    assert d is None


def test_extract_date_year_2026_not_misread_as_day():
    """'June 2026' must not be parsed as June 20 (year digit bleeds into day)."""
    d = _extract_date("June 2026 Newsletter", "https://example.com/", _REF)
    # No valid date should be extracted (no day present after "June")
    assert d is None


def test_extract_date_compact_yyyymmdd_in_url():
    """Compact YYYYMMDD in a parishesonline.com-style filename is parsed correctly."""
    d = _extract_date(
        "Bulletin",
        "https://container.parishesonline.com/bulletins/05/1315/20260614B.pdf",
        _REF,
    )
    assert d == date(2026, 6, 14)


def test_extract_date_compact_yyyymmdd_not_in_anchor():
    """Compact YYYYMMDD is only extracted from the URL path, not anchor text,
    to avoid false matches on phone numbers or invoice IDs in link labels."""
    d = _extract_date("20260614 Weekly Update", "https://example.com/bulletin", _REF)
    # No date in URL path — anchor text compact form should be ignored
    assert d is None


def test_temporal_bonus_zero_days():
    bonus = _temporal_bonus(date(2026, 6, 14), date(2026, 6, 14))
    assert bonus == 2.0


def test_temporal_bonus_thirty_days():
    bonus = _temporal_bonus(date(2026, 5, 15), date(2026, 6, 14))
    # 30-day-old link: bonus should be approximately half of max
    assert 0.9 < bonus < 1.1, f"Expected ~1.0 at 30 days, got {bonus}"


def test_temporal_bonus_none_date_returns_zero():
    assert _temporal_bonus(None, date(2026, 6, 14)) == 0.0


def test_temporal_bonus_old_link_near_zero():
    """A 180-day-old link should have a very small recency bonus."""
    bonus = _temporal_bonus(date(2025, 12, 16), date(2026, 6, 14))
    assert bonus < 0.1, f"Expected near-zero for 180-day-old link, got {bonus}"


def test_temporal_recent_dated_url_beats_high_bm25_nav():
    """St. Anne scenario: June 14 post (no BM25 signal) outranks 'Parish History'
    (~1.43 BM25) because the recency bonus exceeds the BM25 gap."""
    ctx = _ctx_dated(["bulletin", "parish", "pdf"], _REF)
    links = [
        # Recent post: date in URL, no BM25 keywords in URL or anchor.
        {"text": "Third Sunday of Ordinary Time",
         "url": "https://stannesd.com/2026/06/14/third-sunday-of-ordinary-time/"},
        # Nav link: "parish" in both anchor and URL → BM25 ~1.43, no date.
        {"text": "Parish History", "url": "https://stannesd.com/parish-history/"},
    ]
    ranked = _RANKER(ctx, links)
    urls = [url for url, _ in ranked]
    assert urls[0] == "https://stannesd.com/2026/06/14/third-sunday-of-ordinary-time/", (
        f"Recent dated URL should rank first, got: {ranked}"
    )


def test_temporal_newest_pdf_ranked_first():
    """Holy Spirit scenario: May 17 PDF outranks January 4 PDF despite identical
    BM25 scores because it is more recent."""
    ctx = _ctx_dated(["bulletin"], _REF)
    links = [
        {"text": "January 4th Bulletin",
         "url": "https://holyspiritsd.com/wp-content/uploads/2026/01/January-4th-Bulletin.pdf"},
        {"text": "May 17th Bulletin",
         "url": "https://holyspiritsd.com/wp-content/uploads/2026/05/May-17th-Bulletin.pdf"},
    ]
    ranked = _RANKER(ctx, links)
    assert ranked[0][0].endswith("May-17th-Bulletin.pdf"), (
        f"Most recent PDF should rank first, got: {ranked}"
    )


def test_temporal_compact_date_newest_first():
    """parishesonline.com scenario: compact YYYYMMDD URLs are recency-ranked so
    June 14 beats March 1 despite identical BM25 scores."""
    ctx = _ctx_dated(["bulletin"], _REF)
    links = [
        {"text": "Bulletin",
         "url": "https://container.parishesonline.com/bulletins/05/4241/20260301B.pdf"},
        {"text": "Bulletin",
         "url": "https://container.parishesonline.com/bulletins/05/4241/20260614B.pdf"},
    ]
    ranked = _RANKER(ctx, links)
    assert ranked[0][0].endswith("20260614B.pdf"), (
        f"June 14 should rank first, got: {ranked}"
    )


def test_temporal_no_reference_date_preserves_dom_order():
    """When reference_date is None, temporal bonus is not applied and original
    DOM order is preserved for equal-scoring links."""
    ctx = _ctx_dated(["bulletin"], None)  # no reference date
    ctx_no_ref = GoalContext(
        goal="test goal",
        navigation_hint=None,
        goal_type="document_link",
        goal_type_confidence=1.0,
        synonyms={},
        anchor_terms=["bulletin"],
        negative_terms=[],
        regex_hints=[],
        description="test",
        source="deterministic",
        model_used=None,
        created_at=datetime.now(timezone.utc),
        locale="en_US",
        validation_warnings=[],
        reference_date=None,
    )
    links = [
        {"text": "January 4th Bulletin",
         "url": "https://example.com/January-4th-Bulletin.pdf"},
        {"text": "May 17th Bulletin",
         "url": "https://example.com/May-17th-Bulletin.pdf"},
    ]
    ranked = _RANKER(ctx_no_ref, links)
    # Both score equally on BM25; DOM order preserved (January first).
    assert ranked[0][0].endswith("January-4th-Bulletin.pdf"), (
        f"DOM order should be preserved without reference_date, got: {ranked}"
    )
