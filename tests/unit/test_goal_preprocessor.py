"""Unit tests for DeterministicPreprocessor and InMemoryGoalContextCache."""

from charlotte.core.goal_preprocessor import DeterministicPreprocessor, InMemoryGoalContextCache
from charlotte.models import CACHE_FORMAT_VERSION, GoalContext


_PREPROCESSOR = DeterministicPreprocessor()


# ---------------------------------------------------------------------------
# GoalContext shape
# ---------------------------------------------------------------------------

def test_returns_goal_context():
    ctx = _PREPROCESSOR("Find the contact page", None, "en_US")
    assert isinstance(ctx, GoalContext)


def test_goal_and_hint_preserved():
    ctx = _PREPROCESSOR("Find the contact page", "top nav", "en_US")
    assert ctx.goal == "Find the contact page"
    assert ctx.navigation_hint == "top nav"
    assert ctx.locale == "en_US"


def test_source_is_deterministic():
    ctx = _PREPROCESSOR("Find the contact page", None, "en_US")
    assert ctx.source == "deterministic"
    assert ctx.model_used is None


def test_no_synonyms_or_negatives():
    ctx = _PREPROCESSOR("Find the contact page", None, "en_US")
    assert ctx.synonyms == {}
    assert ctx.negative_terms == []
    assert ctx.regex_hints == []
    assert ctx.validation_warnings == []


# ---------------------------------------------------------------------------
# Goal type detection
# ---------------------------------------------------------------------------

def test_goal_type_navigation_default():
    ctx = _PREPROCESSOR("Find the about us page", None, "en_US")
    assert ctx.goal_type == "navigation"


def test_goal_type_phone_extraction():
    ctx = _PREPROCESSOR("Find the phone number for the clinic", None, "en_US")
    assert ctx.goal_type == "phone_extraction"


def test_goal_type_price_extraction():
    ctx = _PREPROCESSOR("What is the price of the membership?", None, "en_US")
    assert ctx.goal_type == "price_extraction"


def test_goal_type_date_extraction():
    ctx = _PREPROCESSOR("Find the date of the next event", None, "en_US")
    assert ctx.goal_type == "date_extraction"


def test_goal_type_document_link_pdf():
    ctx = _PREPROCESSOR("Find the annual report PDF", None, "en_US")
    assert ctx.goal_type == "document_link"


def test_goal_type_document_link_download():
    ctx = _PREPROCESSOR("Download the application form", None, "en_US")
    assert ctx.goal_type == "document_link"


def test_goal_type_navigation_not_confused_by_downloads_page():
    # "downloads page" is a navigation goal, not document_link.
    ctx = _PREPROCESSOR("Find the Python downloads page", None, "en_US")
    assert ctx.goal_type == "navigation"


# ---------------------------------------------------------------------------
# Anchor terms
# ---------------------------------------------------------------------------

def test_anchor_terms_are_normalized_tokens():
    ctx = _PREPROCESSOR("Find the Python downloads page", None, "en_US")
    # Stop words ("find", "the") should be removed; remaining tokens normalized.
    assert "python" in ctx.anchor_terms
    assert "downloads" in ctx.anchor_terms
    assert "page" in ctx.anchor_terms


def test_anchor_terms_stop_words_removed():
    ctx = _PREPROCESSOR("Find the contact page", None, "en_US")
    assert "find" not in ctx.anchor_terms
    assert "the" not in ctx.anchor_terms


def test_anchor_terms_include_hint_tokens():
    ctx = _PREPROCESSOR("Find the contact page", "navigation header", "en_US")
    assert "navigation" in ctx.anchor_terms
    assert "header" in ctx.anchor_terms


def test_anchor_terms_nonempty_for_normal_goal():
    ctx = _PREPROCESSOR("Find the Python downloads page", None, "en_US")
    assert len(ctx.anchor_terms) > 0


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def test_cache_hit_returns_same_object():
    cache = InMemoryGoalContextCache()
    p = DeterministicPreprocessor()
    ctx1 = cache.get_or_create("Find the contact page", None, "en_US", p)
    ctx2 = cache.get_or_create("Find the contact page", None, "en_US", p)
    assert ctx1 is ctx2


def test_cache_miss_on_different_locale():
    cache = InMemoryGoalContextCache()
    p = DeterministicPreprocessor()
    ctx_en = cache.get_or_create("Find the contact page", None, "en_US", p)
    ctx_fr = cache.get_or_create("Find the contact page", None, "fr_FR", p)
    assert ctx_en is not ctx_fr
    assert ctx_en.locale == "en_US"
    assert ctx_fr.locale == "fr_FR"


def test_cache_miss_on_different_goal():
    cache = InMemoryGoalContextCache()
    p = DeterministicPreprocessor()
    ctx1 = cache.get_or_create("Find the contact page", None, "en_US", p)
    ctx2 = cache.get_or_create("Find the about page", None, "en_US", p)
    assert ctx1 is not ctx2


def test_cache_format_version_in_key():
    """Changing CACHE_FORMAT_VERSION must bust the cache."""
    import charlotte.models as m
    cache = InMemoryGoalContextCache()
    p = DeterministicPreprocessor()
    cache.get_or_create("Find contact", None, "en_US", p)
    original = m.CACHE_FORMAT_VERSION
    try:
        m.CACHE_FORMAT_VERSION = original + 1  # type: ignore[assignment]
        # New version should be a cache miss.
        assert len(cache._store) == 1  # only the old entry
        ctx_new = cache.get_or_create("Find contact", None, "en_US", p)
        assert len(cache._store) == 2  # old + new
    finally:
        m.CACHE_FORMAT_VERSION = original  # type: ignore[assignment]
