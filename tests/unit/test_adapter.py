"""
Unit tests for the adapter interface and GroqAdapter (CHAR-006).

Covers T-09 (malformed output — retry triggered) and T-10 (both attempts
fail — page skipped) from the test matrix, plus unit tests for each
validation rule in spec §6.5, GroqAdapter prompt construction, and
GroqAdapter error handling.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from charlotte.adapters.base import AdapterProtocol
from charlotte.adapters.groq import GroqAdapter, _build_user_prompt
from charlotte.core.engine import (
    AdapterOutput,
    _SCHEMA_HINT,
    call_with_validation,
    validate_adapter_output,
)
from charlotte.exceptions import AdapterOutputError, CharlotteConfigError

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_VALID_NOT_FOUND: dict = {
    "found": False,
    "confidence": 0.2,
    "result_url": None,
    "links_to_follow": ["http://example.com/a", "http://example.com/b"],
    "reasoning": "Goal not found on this page.",
}

_VALID_FOUND: dict = {
    "found": True,
    "confidence": 0.95,
    "result_url": "http://example.com/result",
    "links_to_follow": [],
    "reasoning": "The goal is satisfied here.",
}

_PAGE_CONTEXT: dict = dict(
    goal="Find the contact page",
    navigation_hint=None,
    page_title="Home",
    page_url="http://example.com/",
    page_summary="Welcome to example.com",
    available_links=[{"text": "Contact", "url": "http://example.com/contact"}],
    visit_history=[],
    results_so_far=0,
)


def _make_groq_response(content: str) -> MagicMock:
    """Build a mock groq ChatCompletion response with the given content string."""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


# ---------------------------------------------------------------------------
# AdapterProtocol — runtime_checkable structural check
# ---------------------------------------------------------------------------

def test_groq_adapter_satisfies_protocol(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    adapter = GroqAdapter()
    assert isinstance(adapter, AdapterProtocol)


# ---------------------------------------------------------------------------
# validate_adapter_output — valid inputs
# ---------------------------------------------------------------------------

def test_validate_found_false_returns_adapter_output():
    result = validate_adapter_output(_VALID_NOT_FOUND)
    assert isinstance(result, AdapterOutput)
    assert result.found is False
    assert result.confidence == 0.2
    assert result.result_url is None
    assert result.links_to_follow == ["http://example.com/a", "http://example.com/b"]
    assert result.reasoning == "Goal not found on this page."


def test_validate_found_true_returns_adapter_output():
    result = validate_adapter_output(_VALID_FOUND)
    assert result.found is True
    assert result.confidence == 0.95
    assert result.result_url == "http://example.com/result"
    assert result.links_to_follow == []


def test_validate_confidence_as_integer_accepted():
    raw = {**_VALID_NOT_FOUND, "confidence": 0}
    result = validate_adapter_output(raw)
    assert result.confidence == 0.0
    assert isinstance(result.confidence, float)


def test_validate_confidence_boundary_zero():
    result = validate_adapter_output({**_VALID_NOT_FOUND, "confidence": 0.0})
    assert result.confidence == 0.0


def test_validate_confidence_boundary_one():
    raw = {**_VALID_NOT_FOUND, "confidence": 1.0, "result_url": None}
    result = validate_adapter_output(raw)
    assert result.confidence == 1.0


def test_validate_links_invalid_urls_silently_dropped():
    raw = {
        **_VALID_NOT_FOUND,
        "links_to_follow": [
            "http://example.com/good",
            "not-a-url",
            "ftp://example.com/ftp",
            "http://example.com/also-good",
        ],
    }
    result = validate_adapter_output(raw)
    assert result.links_to_follow == [
        "http://example.com/good",
        "http://example.com/also-good",
    ]


def test_validate_links_all_invalid_returns_empty_list():
    raw = {**_VALID_NOT_FOUND, "links_to_follow": ["bad", "also-bad", ""]}
    result = validate_adapter_output(raw)
    assert result.links_to_follow == []


def test_validate_https_result_url_accepted():
    raw = {**_VALID_FOUND, "result_url": "https://example.com/result"}
    result = validate_adapter_output(raw)
    assert result.result_url == "https://example.com/result"


# ---------------------------------------------------------------------------
# validate_adapter_output — not a dict
# ---------------------------------------------------------------------------

def test_validate_not_dict_raises():
    with pytest.raises(ValueError, match="dict"):
        validate_adapter_output("not a dict")


def test_validate_list_raises():
    with pytest.raises(ValueError, match="dict"):
        validate_adapter_output([_VALID_NOT_FOUND])


# ---------------------------------------------------------------------------
# validate_adapter_output — missing fields
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["found", "confidence", "result_url",
                                    "links_to_follow", "reasoning"])
def test_validate_missing_field_raises(field):
    raw = {k: v for k, v in _VALID_NOT_FOUND.items() if k != field}
    with pytest.raises(ValueError, match=field):
        validate_adapter_output(raw)


# ---------------------------------------------------------------------------
# validate_adapter_output — type errors
# ---------------------------------------------------------------------------

def test_validate_found_not_bool_raises():
    with pytest.raises(ValueError, match="found.*boolean"):
        validate_adapter_output({**_VALID_NOT_FOUND, "found": "false"})


def test_validate_confidence_not_numeric_raises():
    with pytest.raises(ValueError, match="confidence.*float"):
        validate_adapter_output({**_VALID_NOT_FOUND, "confidence": "0.5"})


def test_validate_links_not_list_raises():
    with pytest.raises(ValueError, match="links_to_follow.*list"):
        validate_adapter_output({**_VALID_NOT_FOUND, "links_to_follow": "http://x.com"})


def test_validate_reasoning_not_string_raises():
    with pytest.raises(ValueError, match="reasoning.*string"):
        validate_adapter_output({**_VALID_NOT_FOUND, "reasoning": 42})


# ---------------------------------------------------------------------------
# validate_adapter_output — constraint violations
# ---------------------------------------------------------------------------

def test_validate_confidence_below_zero_raises():
    with pytest.raises(ValueError, match="confidence"):
        validate_adapter_output({**_VALID_NOT_FOUND, "confidence": -0.01})


def test_validate_confidence_above_one_raises():
    with pytest.raises(ValueError, match="confidence"):
        validate_adapter_output({**_VALID_NOT_FOUND, "confidence": 1.01})


def test_validate_found_true_null_result_url_raises():
    with pytest.raises(ValueError, match="result_url.*null.*found.*true"):
        validate_adapter_output({**_VALID_FOUND, "result_url": None})


def test_validate_found_true_invalid_result_url_raises():
    with pytest.raises(ValueError, match="result_url.*not a valid URL"):
        validate_adapter_output({**_VALID_FOUND, "result_url": "not-a-url"})


def test_validate_found_false_nonnull_result_url_raises():
    with pytest.raises(ValueError, match="result_url.*null.*found.*false"):
        validate_adapter_output({**_VALID_NOT_FOUND, "result_url": "http://x.com"})


def test_validate_reasoning_empty_string_raises():
    with pytest.raises(ValueError, match="reasoning.*empty"):
        validate_adapter_output({**_VALID_NOT_FOUND, "reasoning": ""})


def test_validate_reasoning_whitespace_only_raises():
    with pytest.raises(ValueError, match="reasoning.*empty"):
        validate_adapter_output({**_VALID_NOT_FOUND, "reasoning": "   "})


# ---------------------------------------------------------------------------
# call_with_validation — happy path (first call succeeds)
# ---------------------------------------------------------------------------

async def test_call_with_validation_first_call_succeeds():
    adapter = AsyncMock(return_value=_VALID_NOT_FOUND)
    result = await call_with_validation(adapter, **_PAGE_CONTEXT)
    assert result.found is False
    adapter.assert_called_once()
    # No schema hint on the first call
    _, kwargs = adapter.call_args
    assert kwargs["schema_hint"] is None


# ---------------------------------------------------------------------------
# T-09: Malformed output on first attempt — retry triggered
# ---------------------------------------------------------------------------

async def test_t09_first_attempt_invalid_second_valid():
    """T-09: First adapter response fails validation; retry with schema hint succeeds."""
    invalid = {"found": "yes"}  # fails validation
    adapter = AsyncMock(side_effect=[invalid, _VALID_NOT_FOUND])

    result = await call_with_validation(adapter, **_PAGE_CONTEXT)

    assert result.found is False
    assert adapter.call_count == 2

    first_call_kwargs = adapter.call_args_list[0][1]
    second_call_kwargs = adapter.call_args_list[1][1]
    assert first_call_kwargs["schema_hint"] is None
    assert second_call_kwargs["schema_hint"] == _SCHEMA_HINT


async def test_t09_schema_hint_is_the_global_constant():
    """The schema hint injected on retry is the module-level _SCHEMA_HINT."""
    adapter = AsyncMock(side_effect=[{"found": "bad"}, _VALID_FOUND])
    await call_with_validation(adapter, **_PAGE_CONTEXT)
    _, kwargs = adapter.call_args_list[1]
    assert kwargs["schema_hint"] is _SCHEMA_HINT


# ---------------------------------------------------------------------------
# T-10: Both attempts fail — AdapterOutputError raised
# ---------------------------------------------------------------------------

async def test_t10_both_attempts_invalid_raises_adapter_output_error():
    """T-10: Both adapter responses fail validation — AdapterOutputError raised."""
    invalid = {"found": "yes"}
    adapter = AsyncMock(return_value=invalid)

    with pytest.raises(AdapterOutputError, match="two attempts"):
        await call_with_validation(adapter, **_PAGE_CONTEXT)

    assert adapter.call_count == 2


async def test_t10_error_wraps_validation_message():
    """The AdapterOutputError from T-10 includes the validation failure reason."""
    raw = {}  # missing all fields
    adapter = AsyncMock(return_value=raw)
    with pytest.raises(AdapterOutputError) as exc_info:
        await call_with_validation(adapter, **_PAGE_CONTEXT)
    assert exc_info.value.__cause__ is not None


# ---------------------------------------------------------------------------
# call_with_validation — adapter raises AdapterOutputError (API failure)
# ---------------------------------------------------------------------------

async def test_adapter_raises_propagated_immediately():
    """If the adapter raises AdapterOutputError, it propagates without a schema retry."""
    adapter = AsyncMock(side_effect=AdapterOutputError("API down"))
    with pytest.raises(AdapterOutputError, match="API down"):
        await call_with_validation(adapter, **_PAGE_CONTEXT)
    # The adapter is called, fails, and the error propagates — no second call
    # because the adapter raised, not returned invalid data.
    adapter.assert_called_once()


async def test_adapter_unexpected_exception_wrapped_as_adapter_output_error():
    """A non-AdapterOutputError from the adapter is wrapped as AdapterOutputError."""
    adapter = AsyncMock(side_effect=RuntimeError("unexpected crash"))
    with pytest.raises(AdapterOutputError):
        await call_with_validation(adapter, **_PAGE_CONTEXT)
    adapter.assert_called_once()


async def test_adapter_unexpected_exception_on_retry_also_wrapped():
    """A non-AdapterOutputError on the retry attempt is also wrapped."""
    invalid = {"found": "bad"}  # fails validation → triggers retry
    adapter = AsyncMock(side_effect=[invalid, RuntimeError("retry crash")])
    with pytest.raises(AdapterOutputError):
        await call_with_validation(adapter, **_PAGE_CONTEXT)
    assert adapter.call_count == 2


async def test_adapter_output_error_on_retry_propagated_unchanged():
    """AdapterOutputError raised on the retry call propagates unchanged."""
    invalid = {"found": "bad"}  # fails validation → triggers retry
    error = AdapterOutputError("retry API failure")
    adapter = AsyncMock(side_effect=[invalid, error])
    with pytest.raises(AdapterOutputError) as exc_info:
        await call_with_validation(adapter, **_PAGE_CONTEXT)
    assert exc_info.value is error
    assert adapter.call_count == 2


# ---------------------------------------------------------------------------
# _is_valid_url — edge cases (coverage)
# ---------------------------------------------------------------------------

def test_is_valid_url_malformed_ipv6_returns_false():
    """urlparse raises ValueError on malformed IPv6 — _is_valid_url returns False."""
    from charlotte.core.engine import _is_valid_url
    assert _is_valid_url("http://[invalid/path") is False


# ---------------------------------------------------------------------------
# GroqAdapter — instantiation
# ---------------------------------------------------------------------------

def test_groq_not_installed_raises_config_error(monkeypatch):
    """GroqAdapter raises CharlotteConfigError when the groq package is missing."""
    with patch.dict("sys.modules", {"groq": None}):
        with pytest.raises(CharlotteConfigError, match="groq"):
            GroqAdapter(api_key="test-key")


def test_missing_api_key_raises_config_error(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(CharlotteConfigError, match="API key"):
        GroqAdapter()


def test_api_key_from_env_var(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_12345")
    adapter = GroqAdapter()
    assert adapter._client is not None


def test_explicit_api_key_used(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    adapter = GroqAdapter(api_key="explicit-key")
    assert adapter._client is not None


def test_custom_model_stored(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    adapter = GroqAdapter(model="llama-3.3-70b-versatile")
    assert adapter._model == "llama-3.3-70b-versatile"


def test_default_model(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    adapter = GroqAdapter()
    assert adapter._model == "llama-3.1-8b-instant"


# ---------------------------------------------------------------------------
# GroqAdapter — successful API call
# ---------------------------------------------------------------------------

@pytest.fixture
def groq_adapter(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    adapter = GroqAdapter()
    adapter._client = MagicMock()
    adapter._client.chat.completions.create = AsyncMock(
        return_value=_make_groq_response(json.dumps(_VALID_NOT_FOUND))
    )
    return adapter


async def test_groq_adapter_returns_parsed_dict(groq_adapter):
    result = await groq_adapter(**_PAGE_CONTEXT)
    assert result == _VALID_NOT_FOUND


async def test_groq_adapter_passes_model_to_api(groq_adapter):
    await groq_adapter(**_PAGE_CONTEXT)
    call_kwargs = groq_adapter._client.chat.completions.create.call_args[1]
    assert call_kwargs["model"] == "llama-3.1-8b-instant"


async def test_groq_adapter_uses_json_object_mode(groq_adapter):
    await groq_adapter(**_PAGE_CONTEXT)
    call_kwargs = groq_adapter._client.chat.completions.create.call_args[1]
    assert call_kwargs["response_format"] == {"type": "json_object"}


async def test_groq_adapter_sends_system_and_user_messages(groq_adapter):
    await groq_adapter(**_PAGE_CONTEXT)
    messages = groq_adapter._client.chat.completions.create.call_args[1]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"


# ---------------------------------------------------------------------------
# GroqAdapter — error handling
# ---------------------------------------------------------------------------

async def test_groq_api_error_raises_adapter_output_error(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    adapter = GroqAdapter()
    adapter._client = MagicMock()
    adapter._client.chat.completions.create = AsyncMock(
        side_effect=RuntimeError("connection refused")
    )
    with pytest.raises(AdapterOutputError):
        await adapter(**_PAGE_CONTEXT)


async def test_groq_json_decode_error_raises_adapter_output_error(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    adapter = GroqAdapter()
    adapter._client = MagicMock()
    adapter._client.chat.completions.create = AsyncMock(
        return_value=_make_groq_response("not valid json {{{")
    )
    with pytest.raises(AdapterOutputError, match="JSON"):
        await adapter(**_PAGE_CONTEXT)


async def test_groq_api_error_message_does_not_contain_key(monkeypatch):
    """API key must not appear in AdapterOutputError message — see spec §6.5, §18."""
    secret_key = "gsk_super_secret_12345"
    monkeypatch.setenv("GROQ_API_KEY", secret_key)
    adapter = GroqAdapter()
    adapter._client = MagicMock()
    adapter._client.chat.completions.create = AsyncMock(
        side_effect=RuntimeError(f"auth failed for key {secret_key}")
    )
    with pytest.raises(AdapterOutputError) as exc_info:
        await adapter(**_PAGE_CONTEXT)
    assert secret_key not in str(exc_info.value)


async def test_groq_api_error_chain_suppressed(monkeypatch):
    """Raw SDK exception is not chained on the AdapterOutputError (from None)."""
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    adapter = GroqAdapter()
    adapter._client = MagicMock()
    adapter._client.chat.completions.create = AsyncMock(
        side_effect=RuntimeError("sdk detail")
    )
    with pytest.raises(AdapterOutputError) as exc_info:
        await adapter(**_PAGE_CONTEXT)
    assert exc_info.value.__cause__ is None


async def test_groq_adapter_output_error_from_client_reraises(monkeypatch):
    """AdapterOutputError raised inside the try block is re-raised unchanged."""
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    adapter = GroqAdapter()
    adapter._client = MagicMock()
    original = AdapterOutputError("inner error")
    adapter._client.chat.completions.create = AsyncMock(side_effect=original)
    with pytest.raises(AdapterOutputError) as exc_info:
        await adapter(**_PAGE_CONTEXT)
    assert exc_info.value is original


# ---------------------------------------------------------------------------
# GroqAdapter — prompt construction
# ---------------------------------------------------------------------------

def test_prompt_contains_goal():
    prompt = _build_user_prompt(
        goal="Find pricing",
        navigation_hint=None,
        page_title="Home",
        page_url="http://example.com/",
        page_summary="Welcome",
        available_links=[],
        visit_history=[],
        results_so_far=0,
        schema_hint=None,
    )
    assert "Find pricing" in prompt


def test_prompt_contains_navigation_hint_when_provided():
    prompt = _build_user_prompt(
        goal="Find pricing",
        navigation_hint="Check the plans page",
        page_title="Home",
        page_url="http://example.com/",
        page_summary="Welcome",
        available_links=[],
        visit_history=[],
        results_so_far=0,
        schema_hint=None,
    )
    assert "Check the plans page" in prompt


def test_prompt_omits_navigation_hint_line_when_none():
    prompt = _build_user_prompt(
        goal="Find pricing",
        navigation_hint=None,
        page_title="Home",
        page_url="http://example.com/",
        page_summary="Welcome",
        available_links=[],
        visit_history=[],
        results_so_far=0,
        schema_hint=None,
    )
    assert "Navigation hint" not in prompt


def test_prompt_contains_page_title_and_url():
    prompt = _build_user_prompt(
        goal="x",
        navigation_hint=None,
        page_title="About Us",
        page_url="http://example.com/about",
        page_summary="info",
        available_links=[],
        visit_history=[],
        results_so_far=0,
        schema_hint=None,
    )
    assert "About Us" in prompt
    assert "http://example.com/about" in prompt


def test_prompt_contains_available_links():
    links = [
        {"text": "Contact", "url": "http://example.com/contact"},
        {"text": "Pricing", "url": "http://example.com/pricing"},
    ]
    prompt = _build_user_prompt(
        goal="x",
        navigation_hint=None,
        page_title="Home",
        page_url="http://example.com/",
        page_summary="summary",
        available_links=links,
        visit_history=[],
        results_so_far=0,
        schema_hint=None,
    )
    assert "Contact" in prompt
    assert "http://example.com/contact" in prompt
    assert "Pricing" in prompt


def test_prompt_shows_none_when_no_links():
    prompt = _build_user_prompt(
        goal="x",
        navigation_hint=None,
        page_title="Home",
        page_url="http://example.com/",
        page_summary="summary",
        available_links=[],
        visit_history=[],
        results_so_far=0,
        schema_hint=None,
    )
    assert "(none)" in prompt


def test_prompt_contains_visit_history():
    prompt = _build_user_prompt(
        goal="x",
        navigation_hint=None,
        page_title="Home",
        page_url="http://example.com/",
        page_summary="summary",
        available_links=[],
        visit_history=["http://example.com/a", "http://example.com/b"],
        results_so_far=0,
        schema_hint=None,
    )
    assert "http://example.com/a" in prompt
    assert "http://example.com/b" in prompt


def test_prompt_contains_results_so_far():
    prompt = _build_user_prompt(
        goal="x",
        navigation_hint=None,
        page_title="Home",
        page_url="http://example.com/",
        page_summary="summary",
        available_links=[],
        visit_history=[],
        results_so_far=3,
        schema_hint=None,
    )
    assert "3" in prompt


def test_schema_hint_appears_at_top_of_prompt():
    hint = "REMINDER: return valid JSON"
    prompt = _build_user_prompt(
        goal="x",
        navigation_hint=None,
        page_title="Home",
        page_url="http://example.com/",
        page_summary="summary",
        available_links=[],
        visit_history=[],
        results_so_far=0,
        schema_hint=hint,
    )
    assert prompt.startswith(hint)


def test_no_schema_hint_prompt_starts_with_goal():
    prompt = _build_user_prompt(
        goal="Find the contact page",
        navigation_hint=None,
        page_title="Home",
        page_url="http://example.com/",
        page_summary="summary",
        available_links=[],
        visit_history=[],
        results_so_far=0,
        schema_hint=None,
    )
    assert prompt.startswith("Goal:")
