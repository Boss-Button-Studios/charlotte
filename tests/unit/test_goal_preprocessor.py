"""Unit tests for DeterministicPreprocessor, HybridPreprocessor, and InMemoryGoalContextCache."""

import json

import httpx
import pytest
import respx

from charlotte.core.goal_preprocessor import (
    DeterministicPreprocessor,
    HybridPreprocessor,
    InMemoryGoalContextCache,
    _clean_model_json,
    _extract_json,
)
from charlotte.models import GoalContext


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


def test_goal_type_price_extraction_fee():
    ctx = _PREPROCESSOR("What is the membership fee?", None, "en_US")
    assert ctx.goal_type == "price_extraction"


def test_goal_type_price_extraction_pricing():
    ctx = _PREPROCESSOR("Find the subscription pricing", None, "en_US")
    assert ctx.goal_type == "price_extraction"


def test_goal_type_date_extraction_when_was():
    ctx = _PREPROCESSOR("When was this article published?", None, "en_US")
    assert ctx.goal_type == "date_extraction"


def test_goal_type_date_extraction_when_is():
    ctx = _PREPROCESSOR("When is the next board meeting?", None, "en_US")
    assert ctx.goal_type == "date_extraction"


def test_goal_type_date_extraction_published():
    ctx = _PREPROCESSOR("Find the date this document was published", None, "en_US")
    assert ctx.goal_type == "date_extraction"


def test_goal_type_document_link_bulletin():
    ctx = _PREPROCESSOR("Find the latest bulletin", None, "en_US")
    assert ctx.goal_type == "document_link"


def test_goal_type_document_link_handbook():
    ctx = _PREPROCESSOR("Get the employee handbook", None, "en_US")
    assert ctx.goal_type == "document_link"


def test_goal_type_freeform_fact_hours():
    ctx = _PREPROCESSOR("What are the business hours?", None, "en_US")
    assert ctx.goal_type == "freeform_fact"


# Edge cases — substring collisions that word-boundary matching must not trigger.

def test_goal_type_no_fee_inside_coffee():
    # "coffee" contains "fee" but must not trigger price_extraction.
    ctx = _PREPROCESSOR("Find the coffee menu", None, "en_US")
    assert ctx.goal_type == "navigation"


def test_goal_type_no_published_inside_unpublished():
    # "unpublished" contains "published" but must not trigger date_extraction.
    ctx = _PREPROCESSOR("Find the unpublished draft", None, "en_US")
    assert ctx.goal_type == "navigation"


def test_goal_type_no_hours_inside_behaviours():
    # "behaviours" contains "hours" but must not trigger freeform_fact.
    ctx = _PREPROCESSOR("Find the safety behaviours page", None, "en_US")
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
        cache.get_or_create("Find contact", None, "en_US", p)
        assert len(cache._store) == 2  # old + new
    finally:
        m.CACHE_FORMAT_VERSION = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# _clean_model_json — pre-parse cleanup
# ---------------------------------------------------------------------------

def test_clean_strips_raw_string_prefix():
    # r"\b..." → "\\b..." so json.loads yields the literal backslash.
    result = _clean_model_json(r'{"regex_hints": [r"\b\d+\b"]}')
    parsed = json.loads(result)
    assert parsed["regex_hints"][0] == r"\b\d+\b"


def test_clean_strips_line_comment_after_bracket():
    raw = '{"a": [1, 2]} // a comment\n{"b": 3}'
    result = _clean_model_json(raw)
    assert "//" not in result


def test_clean_strips_line_comment_after_string_value():
    raw = '{"x": "hello"} // explanation\n'
    result = _clean_model_json(raw)
    assert "//" not in result
    assert '"hello"' in result


def test_clean_does_not_strip_url_slashes():
    # // inside a URL string must not be touched.
    raw = '{"url": "https://example.com/path"}'
    result = _clean_model_json(raw)
    assert "https://example.com/path" in result


def test_clean_inserts_comma_between_adjacent_strings():
    # Python implicit string concatenation: "a"\n"b" → "a",\n"b"
    raw = '{"synonyms": {"x": ["foo"\n                     "bar"]}}'
    parsed = json.loads(_clean_model_json(raw))
    assert parsed["synonyms"]["x"] == ["foo", "bar"]


def test_clean_llama31_bulletin_output():
    # Exact shape from the failing llama3.1:8b run.
    raw = (
        '{\n'
        '  "goal_type": "document_link",\n'
        '  "goal_type_confidence": 0.98,\n'
        '  "synonyms": {"bulletin": ["update", "notice"]},\n'
        '  "anchor_terms": ["bulletin"],\n'
        '  "negative_terms": ["archive"],\n'
        r'  "regex_hints": [r"\b[0-9]{1,2}[/-]\d{1,2}\b"],'
        ' // match dates\n'
        '  "description": "Find the latest bulletin."\n'
        '}'
    )
    parsed = json.loads(_clean_model_json(raw))
    assert parsed["synonyms"]["bulletin"] == ["update", "notice"]
    # Regex hint has backslashes properly escaped — compiles as a valid pattern.
    import re
    assert re.compile(parsed["regex_hints"][0])


def test_clean_llama31_phone_manager_output():
    # Exact shape from the failing llama3.1:8b run: raw strings + missing comma.
    raw = (
        '{\n'
        '  "goal_type": "phone_extraction",\n'
        '  "goal_type_confidence": 0.95,\n'
        '  "synonyms": {\n'
        '    "assistant manager": ["shop supervisor", "store manager"],\n'
        '    "phone number": ["phonenumber", "phone contact"\n'
        '                     "contact info"]\n'
        '  },\n'
        '  "anchor_terms": ["assistant manager", "phone number"],\n'
        '  "negative_terms": ["customer service"],\n'
        r'  "regex_hints": [r"\d{3}-\d{3}-\d{4}"],'
        '\n'
        '  "description": "Find the phone number of an assistant manager."\n'
        '}'
    )
    parsed = json.loads(_clean_model_json(raw))
    assert "contact info" in parsed["synonyms"]["phone number"]
    import re
    assert re.compile(parsed["regex_hints"][0])


# ---------------------------------------------------------------------------
# _extract_json — JSON extraction strategies
# ---------------------------------------------------------------------------

def test_extract_json_clean():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fence_json_tag():
    content = '```json\n{"a": 1}\n```'
    assert _extract_json(content) == {"a": 1}


def test_extract_json_fence_no_tag():
    content = '```\n{"a": 1}\n```'
    assert _extract_json(content) == {"a": 1}


def test_extract_json_prose_then_fence():
    # Mirrors llama3.1 output: explanation text followed by fenced JSON.
    content = (
        "Here is the JSON context:\n\n"
        "```json\n{\"goal_type\": \"navigation\"}\n```\n\n"
        "Let me explain…"
    )
    result = _extract_json(content)
    assert result["goal_type"] == "navigation"


def test_extract_json_prose_surrounding_object():
    # JSON embedded in prose without a code fence.
    content = 'Sure! Here you go: {"goal_type": "navigation"} Hope that helps.'
    assert _extract_json(content)["goal_type"] == "navigation"


def test_extract_json_trailing_prose_after_object():
    # raw_decode must stop at } and not fail on trailing text.
    content = '{"a": 1} some trailing explanation'
    assert _extract_json(content) == {"a": 1}


def test_extract_json_raises_when_no_json():
    with pytest.raises(ValueError, match="no parseable JSON"):
        _extract_json("No JSON here at all.")


def test_extract_json_invalid_fence_content_falls_to_strategy3():
    # Fence contains non-JSON; strategy 3 should still find the object.
    content = "```\nnot json\n```\n{\"a\": 2}"
    assert _extract_json(content) == {"a": 2}


# ---------------------------------------------------------------------------
# HybridPreprocessor
# ---------------------------------------------------------------------------

_HYBRID_ENDPOINT = "http://localhost:11434/v1/chat/completions"

_VALID_HYBRID_OUTPUT = {
    "goal_type": "navigation",
    "goal_type_confidence": 0.9,
    "synonyms": {"tutorial": ["guide", "walkthrough", "introduction"]},
    "anchor_terms": ["tutorial", "python"],
    "negative_terms": [],
    "regex_hints": [],
    "description": "User wants to find a beginner tutorial page",
}


def _mock_response(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@respx.mock
def test_hybrid_happy_path():
    respx.post(_HYBRID_ENDPOINT).mock(return_value=_mock_response(
        json.dumps(_VALID_HYBRID_OUTPUT)
    ))
    ctx = HybridPreprocessor()("Find the Python tutorial page", None, "en_US")
    assert ctx.goal_type == "navigation"
    assert ctx.source == "model"
    assert ctx.model_used == "deepseek-r1:14b"
    assert "tutorial" in ctx.synonyms
    assert ctx.synonyms["tutorial"] == ["guide", "walkthrough", "introduction"]
    assert "tutorial" in ctx.anchor_terms


@respx.mock
def test_hybrid_accepts_raw_string_and_comment():
    # Mirrors the failing llama3.1:8b bulletin output: r"..." and // comment.
    raw_output = (
        '{\n'
        '  "goal_type": "navigation",\n'
        '  "goal_type_confidence": 0.95,\n'
        '  "synonyms": {"bulletin": ["update", "notice", "announcement"]},\n'
        '  "anchor_terms": ["bulletin"],\n'
        '  "negative_terms": ["archive", "old"],\n'
        r'  "regex_hints": [r"\b\d{4}\b"],'
        ' // match years\n'
        '  "description": "Find the latest bulletin."\n'
        '}'
    )
    respx.post(_HYBRID_ENDPOINT).mock(return_value=_mock_response(raw_output))
    ctx = HybridPreprocessor()("Find the latest bulletin", None, "en_US")
    assert ctx.source == "model"
    assert "bulletin" in ctx.synonyms
    assert "update" in ctx.synonyms["bulletin"]


@respx.mock
def test_hybrid_accepts_fenced_json():
    # Mirrors llama3.1:8b output — JSON wrapped in a code fence with surrounding prose.
    fenced = (
        "Here is the JSON context:\n\n"
        f"```json\n{json.dumps(_VALID_HYBRID_OUTPUT)}\n```\n\n"
        "Let me explain how I generated this JSON: …"
    )
    respx.post(_HYBRID_ENDPOINT).mock(return_value=_mock_response(fenced))
    ctx = HybridPreprocessor()("Find the Python tutorial page", None, "en_US")
    assert ctx.source == "model"
    assert ctx.goal_type == "navigation"


@respx.mock
def test_hybrid_accepts_prose_wrapped_json():
    # JSON embedded in prose without a fence — strategy 3.
    wrapped = f"Sure, here you go: {json.dumps(_VALID_HYBRID_OUTPUT)} Hope that helps!"
    respx.post(_HYBRID_ENDPOINT).mock(return_value=_mock_response(wrapped))
    ctx = HybridPreprocessor()("Find the Python tutorial page", None, "en_US")
    assert ctx.source == "model"


@respx.mock
def test_hybrid_regex_hints_as_string_is_coerced():
    # Mirrors llama3.1:8b second-run output: regex_hints is a bare string, not a list.
    output = {
        **_VALID_HYBRID_OUTPUT,
        "regex_hints": r"(.*?bulletin|newsletter)[._\- ]*(?=[0-9])",
    }
    respx.post(_HYBRID_ENDPOINT).mock(return_value=_mock_response(json.dumps(output)))
    ctx = HybridPreprocessor()("Find the Python tutorial page", None, "en_US")
    assert ctx.source == "model"
    assert len(ctx.regex_hints) == 1
    assert any("format_coerced" in w for w in ctx.validation_warnings)


@respx.mock
def test_hybrid_with_navigation_hint():
    respx.post(_HYBRID_ENDPOINT).mock(return_value=_mock_response(
        json.dumps(_VALID_HYBRID_OUTPUT)
    ))
    ctx = HybridPreprocessor()("Find the Python tutorial page", "top nav", "en_US")
    assert ctx.navigation_hint == "top nav"
    assert ctx.source == "model"


@respx.mock
def test_hybrid_strips_think_blocks():
    content = f"<think>Let me analyze this.</think>\n{json.dumps(_VALID_HYBRID_OUTPUT)}"
    respx.post(_HYBRID_ENDPOINT).mock(return_value=_mock_response(content))
    ctx = HybridPreprocessor()("Find the Python tutorial page", None, "en_US")
    assert ctx.source == "model"


@respx.mock
def test_hybrid_falls_back_on_connection_error():
    respx.post(_HYBRID_ENDPOINT).mock(side_effect=httpx.ConnectError("refused"))
    ctx = HybridPreprocessor()("Find the contact page", None, "en_US")
    assert ctx.source == "deterministic"


@respx.mock
def test_hybrid_falls_back_on_bad_json():
    respx.post(_HYBRID_ENDPOINT).mock(return_value=_mock_response("not { valid json"))
    ctx = HybridPreprocessor()("Find the contact page", None, "en_US")
    assert ctx.source == "deterministic"


@respx.mock
def test_hybrid_falls_back_on_invalid_goal_type():
    bad = {**_VALID_HYBRID_OUTPUT, "goal_type": "made_up_type"}
    respx.post(_HYBRID_ENDPOINT).mock(return_value=_mock_response(json.dumps(bad)))
    ctx = HybridPreprocessor()("Find the Python tutorial page", None, "en_US")
    assert ctx.source == "deterministic"


@respx.mock
def test_hybrid_synonym_key_not_in_goal_is_dropped():
    bad = {**_VALID_HYBRID_OUTPUT, "synonyms": {"unrelated_term": ["something"]}}
    respx.post(_HYBRID_ENDPOINT).mock(return_value=_mock_response(json.dumps(bad)))
    ctx = HybridPreprocessor()("Find the Python tutorial page", None, "en_US")
    assert ctx.source == "model"
    assert "unrelated_term" not in ctx.synonyms


@respx.mock
def test_hybrid_warning_recorded_for_dropped_synonym():
    bad = {**_VALID_HYBRID_OUTPUT, "synonyms": {"unrelated_term": ["x"]}}
    respx.post(_HYBRID_ENDPOINT).mock(return_value=_mock_response(json.dumps(bad)))
    ctx = HybridPreprocessor()("Find the Python tutorial page", None, "en_US")
    assert any("near_miss" in w and "unrelated_term" in w for w in ctx.validation_warnings)


@respx.mock
def test_hybrid_invalid_regex_dropped_with_warning():
    with_bad_regex = {**_VALID_HYBRID_OUTPUT, "regex_hints": ["[invalid", r"\d+"]}
    respx.post(_HYBRID_ENDPOINT).mock(return_value=_mock_response(json.dumps(with_bad_regex)))
    ctx = HybridPreprocessor()("Find the Python tutorial page", None, "en_US")
    assert ctx.source == "model"
    assert r"\d+" in ctx.regex_hints
    assert "[invalid" not in ctx.regex_hints
    assert any("regex_dropped" in w for w in ctx.validation_warnings)


@respx.mock
def test_hybrid_negative_term_in_goal_causes_fallback():
    # "python" appears in the goal, so it's an invalid negative term → fallback
    bad = {**_VALID_HYBRID_OUTPUT, "synonyms": {}, "anchor_terms": [],
           "negative_terms": ["python"]}
    respx.post(_HYBRID_ENDPOINT).mock(return_value=_mock_response(json.dumps(bad)))
    ctx = HybridPreprocessor()("Find the Python tutorial page", None, "en_US")
    assert ctx.source == "deterministic"


@respx.mock
def test_hybrid_negative_term_overlapping_positive_causes_fallback():
    # "tutorial" is an anchor_term, so it can't also be a negative_term
    bad = {**_VALID_HYBRID_OUTPUT, "synonyms": {}, "negative_terms": ["tutorial"]}
    respx.post(_HYBRID_ENDPOINT).mock(return_value=_mock_response(json.dumps(bad)))
    ctx = HybridPreprocessor()("Find the Python tutorial page", None, "en_US")
    assert ctx.source == "deterministic"


def test_hybrid_model_id_attribute():
    p = HybridPreprocessor(model="llama3:8b")
    assert p.model_id == "llama3:8b"


def test_hybrid_custom_base_url():
    p = HybridPreprocessor(base_url="http://myserver:8080")
    assert "myserver:8080" in p._base_url


def test_hybrid_satisfies_protocol():
    from charlotte.core.goal_preprocessor import GoalPreprocessorProtocol
    assert isinstance(HybridPreprocessor(), GoalPreprocessorProtocol)


@respx.mock
def test_hybrid_falls_back_on_invalid_confidence():
    bad = {**_VALID_HYBRID_OUTPUT, "goal_type_confidence": 1.5}
    respx.post(_HYBRID_ENDPOINT).mock(return_value=_mock_response(json.dumps(bad)))
    ctx = HybridPreprocessor()("Find the Python tutorial page", None, "en_US")
    assert ctx.source == "deterministic"


@respx.mock
def test_hybrid_falls_back_on_oversized_context():
    bad = {**_VALID_HYBRID_OUTPUT, "description": "x" * 5000}
    respx.post(_HYBRID_ENDPOINT).mock(return_value=_mock_response(json.dumps(bad)))
    ctx = HybridPreprocessor()("Find the Python tutorial page", None, "en_US")
    assert ctx.source == "deterministic"


@respx.mock
def test_hybrid_falls_back_on_malformed_completion_structure():
    # Empty choices array → IndexError inside content extraction → fallback.
    respx.post(_HYBRID_ENDPOINT).mock(return_value=httpx.Response(200, json={"choices": []}))
    ctx = HybridPreprocessor()("Find the contact page", None, "en_US")
    assert ctx.source == "deterministic"


@respx.mock
def test_hybrid_falls_back_on_missing_choices_key():
    # No choices key at all → KeyError inside content extraction → fallback.
    respx.post(_HYBRID_ENDPOINT).mock(return_value=httpx.Response(200, json={"result": "unexpected"}))
    ctx = HybridPreprocessor()("Find the contact page", None, "en_US")
    assert ctx.source == "deterministic"


@respx.mock
def test_hybrid_anchor_terms_fallback_to_tokenized_goal():
    # All model anchor_terms invalid → code falls back to deterministic tokens;
    # source stays "model" because validation did not hard-reject.
    bad = {**_VALID_HYBRID_OUTPUT, "synonyms": {}, "anchor_terms": ["not_in_goal"]}
    respx.post(_HYBRID_ENDPOINT).mock(return_value=_mock_response(json.dumps(bad)))
    ctx = HybridPreprocessor()("Find the Python tutorial page", None, "en_US")
    assert ctx.source == "model"
    assert "python" in ctx.anchor_terms
