"""
Unit tests for LocalAdapter (CHAR-011, spec §6.3).

Covers constructor configuration via arguments and environment variables,
prompt construction, protocol conformance, and the full error boundary —
timeout, HTTP errors, connection failures, and malformed responses.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from charlotte.adapters.base import AdapterProtocol
from charlotte.adapters.local import (
    LocalAdapter,
    _DEFAULT_BASE_URL,
    _DEFAULT_MODEL,
    _build_user_prompt,
)
from charlotte.exceptions import AdapterOutputError, CharlotteConfigError, CharlotteTimeoutError

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_ENDPOINT = f"{_DEFAULT_BASE_URL}/v1/chat/completions"

_VALID_NAV_DICT: dict = {
    "found": False,
    "confidence": 0.2,
    "result_url": None,
    "links_to_follow": ["https://example.com/a"],
    "reasoning": "Goal not found on this page.",
}

_PAGE_CONTEXT: dict = dict(
    goal="Find the contact page",
    navigation_hint=None,
    page_title="Home",
    page_url="https://example.com/",
    page_summary="Welcome to our site.",
    available_links=[{"text": "Contact", "url": "https://example.com/contact"}],
    visit_history=[],
    results_so_far=0,
)


def _ok_response(nav_dict: dict | None = None) -> httpx.Response:
    """Build a 200 httpx.Response carrying the given nav dict as model content."""
    content = json.dumps(nav_dict or _VALID_NAV_DICT)
    body = {"choices": [{"message": {"content": content}}]}
    return httpx.Response(200, json=body)


# ---------------------------------------------------------------------------
# Constructor — defaults
# ---------------------------------------------------------------------------

def test_default_endpoint(monkeypatch):
    """Default endpoint is _DEFAULT_BASE_URL + /v1/chat/completions."""
    monkeypatch.delenv("CHARLOTTE_LOCAL_BASE_URL", raising=False)
    adapter = LocalAdapter()
    assert adapter._endpoint == f"{_DEFAULT_BASE_URL}/v1/chat/completions"


def test_default_model(monkeypatch):
    """Default model name is _DEFAULT_MODEL."""
    monkeypatch.delenv("CHARLOTTE_LOCAL_MODEL", raising=False)
    adapter = LocalAdapter()
    assert adapter._model == _DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Constructor — environment variables
# ---------------------------------------------------------------------------

def test_env_base_url(monkeypatch):
    """CHARLOTTE_LOCAL_BASE_URL sets the endpoint."""
    monkeypatch.setenv("CHARLOTTE_LOCAL_BASE_URL", "http://myserver:8080")
    adapter = LocalAdapter()
    assert adapter._endpoint == "http://myserver:8080/v1/chat/completions"


def test_env_model(monkeypatch):
    """CHARLOTTE_LOCAL_MODEL sets the model name."""
    monkeypatch.setenv("CHARLOTTE_LOCAL_MODEL", "phi3:mini")
    adapter = LocalAdapter()
    assert adapter._model == "phi3:mini"


# ---------------------------------------------------------------------------
# Constructor — explicit arguments override env vars
# ---------------------------------------------------------------------------

def test_explicit_base_url_overrides_env(monkeypatch):
    """Explicit base_url= takes precedence over CHARLOTTE_LOCAL_BASE_URL."""
    monkeypatch.setenv("CHARLOTTE_LOCAL_BASE_URL", "http://envserver:9999")
    adapter = LocalAdapter(base_url="http://argserver:8080")
    assert adapter._endpoint == "http://argserver:8080/v1/chat/completions"


def test_explicit_model_name_overrides_env(monkeypatch):
    """Explicit model_name= takes precedence over CHARLOTTE_LOCAL_MODEL."""
    monkeypatch.setenv("CHARLOTTE_LOCAL_MODEL", "llama3:70b")
    adapter = LocalAdapter(model_name="mistral:7b")
    assert adapter._model == "mistral:7b"


# ---------------------------------------------------------------------------
# Constructor — trailing slash stripped
# ---------------------------------------------------------------------------

def test_trailing_slash_stripped():
    """Trailing slash in base_url is stripped before appending the path."""
    adapter = LocalAdapter(base_url="http://localhost:11434/")
    assert adapter._endpoint == "http://localhost:11434/v1/chat/completions"


# ---------------------------------------------------------------------------
# Constructor — invalid base_url
# ---------------------------------------------------------------------------

def test_no_scheme_raises_config_error():
    """A base_url without a scheme raises CharlotteConfigError."""
    with pytest.raises(CharlotteConfigError, match="http"):
        LocalAdapter(base_url="localhost:11434")


def test_non_http_scheme_raises_config_error():
    """A non-HTTP scheme raises CharlotteConfigError."""
    with pytest.raises(CharlotteConfigError):
        LocalAdapter(base_url="ftp://localhost:11434")


def test_http_scheme_only_no_host_raises_config_error():
    """http:// with no hostname raises CharlotteConfigError."""
    with pytest.raises(CharlotteConfigError):
        LocalAdapter(base_url="http://")


def test_https_scheme_only_no_host_raises_config_error():
    """https:// with no hostname raises CharlotteConfigError."""
    with pytest.raises(CharlotteConfigError):
        LocalAdapter(base_url="https://")


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

def test_satisfies_adapter_protocol():
    """LocalAdapter satisfies the AdapterProtocol structural check."""
    assert isinstance(LocalAdapter(), AdapterProtocol)


# ---------------------------------------------------------------------------
# Happy path — call returns parsed dict
# ---------------------------------------------------------------------------

@respx.mock
async def test_successful_call_returns_dict():
    """A successful API call returns the parsed navigation dict."""
    respx.post(_ENDPOINT).mock(return_value=_ok_response())
    result = await LocalAdapter()(**_PAGE_CONTEXT)
    assert result == _VALID_NAV_DICT


@respx.mock
async def test_request_sent_to_configured_endpoint():
    """POST is sent to {base_url}/v1/chat/completions."""
    route = respx.post(_ENDPOINT).mock(return_value=_ok_response())
    await LocalAdapter()(**_PAGE_CONTEXT)
    assert route.called


@respx.mock
async def test_custom_base_url_uses_correct_endpoint():
    """A custom base_url targets the right endpoint."""
    custom = "http://lmstudio:1234"
    respx.post(f"{custom}/v1/chat/completions").mock(return_value=_ok_response())
    result = await LocalAdapter(base_url=custom)(**_PAGE_CONTEXT)
    assert result["found"] is False


# ---------------------------------------------------------------------------
# Request payload
# ---------------------------------------------------------------------------

@respx.mock
async def test_payload_includes_model_name():
    """Request body contains the configured model name."""
    route = respx.post(_ENDPOINT).mock(return_value=_ok_response())
    await LocalAdapter(model_name="mistral:7b")(**_PAGE_CONTEXT)
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "mistral:7b"


@respx.mock
async def test_payload_stream_is_false():
    """Request body always includes stream=false."""
    route = respx.post(_ENDPOINT).mock(return_value=_ok_response())
    await LocalAdapter()(**_PAGE_CONTEXT)
    sent = json.loads(route.calls[0].request.content)
    assert sent["stream"] is False


@respx.mock
async def test_payload_first_message_is_system():
    """First message in payload has role=system."""
    route = respx.post(_ENDPOINT).mock(return_value=_ok_response())
    await LocalAdapter()(**_PAGE_CONTEXT)
    sent = json.loads(route.calls[0].request.content)
    assert sent["messages"][0]["role"] == "system"
    assert len(sent["messages"][0]["content"]) > 0


# ---------------------------------------------------------------------------
# Prompt construction — goal and page context
# ---------------------------------------------------------------------------

@respx.mock
async def test_goal_in_user_prompt():
    """The navigation goal appears in the user prompt."""
    route = respx.post(_ENDPOINT).mock(return_value=_ok_response())
    await LocalAdapter()(**_PAGE_CONTEXT)
    user_content = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert "Find the contact page" in user_content


@respx.mock
async def test_page_url_in_user_prompt():
    """The current page URL appears in the user prompt."""
    route = respx.post(_ENDPOINT).mock(return_value=_ok_response())
    await LocalAdapter()(**_PAGE_CONTEXT)
    user_content = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert "https://example.com/" in user_content


@respx.mock
async def test_available_links_in_user_prompt():
    """Available link text and URL appear in the user prompt."""
    route = respx.post(_ENDPOINT).mock(return_value=_ok_response())
    await LocalAdapter()(**_PAGE_CONTEXT)
    user_content = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert "Contact" in user_content
    assert "https://example.com/contact" in user_content


@respx.mock
async def test_page_summary_wrapped_in_page_content_tags():
    """page_summary is enclosed in <page_content> delimiters."""
    route = respx.post(_ENDPOINT).mock(return_value=_ok_response())
    await LocalAdapter()(**_PAGE_CONTEXT)
    user_content = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert "<page_content>" in user_content
    assert "</page_content>" in user_content


@respx.mock
async def test_available_links_wrapped_in_tags():
    """available_links section is enclosed in <available_links> delimiters."""
    route = respx.post(_ENDPOINT).mock(return_value=_ok_response())
    await LocalAdapter()(**_PAGE_CONTEXT)
    user_content = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert "<available_links>" in user_content
    assert "</available_links>" in user_content


@respx.mock
async def test_visit_history_wrapped_in_tags():
    """visit_history section is enclosed in <visit_history> delimiters."""
    route = respx.post(_ENDPOINT).mock(return_value=_ok_response())
    await LocalAdapter()(**_PAGE_CONTEXT)
    user_content = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert "<visit_history>" in user_content
    assert "</visit_history>" in user_content


@respx.mock
async def test_empty_links_shows_none_marker():
    """When available_links is empty, the prompt shows '(none)'."""
    route = respx.post(_ENDPOINT).mock(return_value=_ok_response())
    await LocalAdapter()(**dict(_PAGE_CONTEXT, available_links=[]))
    user_content = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert "(none)" in user_content


@respx.mock
async def test_visit_history_in_user_prompt():
    """Previously visited URLs appear in the user prompt."""
    route = respx.post(_ENDPOINT).mock(return_value=_ok_response())
    ctx = dict(_PAGE_CONTEXT, visit_history=["https://example.com/prev"])
    await LocalAdapter()(**ctx)
    user_content = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert "https://example.com/prev" in user_content


@respx.mock
async def test_empty_visit_history_shows_none_marker():
    """When visit_history is empty, the prompt shows '(none)'."""
    route = respx.post(_ENDPOINT).mock(return_value=_ok_response())
    await LocalAdapter()(**_PAGE_CONTEXT)  # visit_history=[]
    user_content = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert "(none)" in user_content


# ---------------------------------------------------------------------------
# Prompt construction — optional fields
# ---------------------------------------------------------------------------

@respx.mock
async def test_navigation_hint_included_when_provided():
    """navigation_hint appears in the user prompt when provided."""
    route = respx.post(_ENDPOINT).mock(return_value=_ok_response())
    ctx = dict(_PAGE_CONTEXT, navigation_hint="Focus on the nav menu")
    await LocalAdapter()(**ctx)
    user_content = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert "Focus on the nav menu" in user_content


@respx.mock
async def test_navigation_hint_absent_when_none():
    """When navigation_hint=None, no hint line appears in the prompt."""
    route = respx.post(_ENDPOINT).mock(return_value=_ok_response())
    await LocalAdapter()(**_PAGE_CONTEXT)  # navigation_hint=None
    user_content = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert "Navigation hint:" not in user_content


@respx.mock
async def test_schema_hint_prepended_when_provided():
    """schema_hint appears at the top of the user prompt when provided."""
    route = respx.post(_ENDPOINT).mock(return_value=_ok_response())
    ctx = dict(_PAGE_CONTEXT, schema_hint="REMINDER: respond with JSON only.")
    await LocalAdapter()(**ctx)
    user_content = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert user_content.startswith("REMINDER: respond with JSON only.")


@respx.mock
async def test_schema_hint_absent_prompt_starts_with_goal():
    """When schema_hint=None, the user prompt starts with the goal line."""
    route = respx.post(_ENDPOINT).mock(return_value=_ok_response())
    await LocalAdapter()(**_PAGE_CONTEXT)  # no schema_hint
    user_content = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert user_content.startswith("Goal:")


# ---------------------------------------------------------------------------
# Error handling — HTTP request failures
# ---------------------------------------------------------------------------

@respx.mock
async def test_timeout_raises_charlotte_timeout_error():
    """httpx.TimeoutException maps to CharlotteTimeoutError."""
    respx.post(_ENDPOINT).mock(side_effect=httpx.TimeoutException("timed out"))
    with pytest.raises(CharlotteTimeoutError):
        await LocalAdapter()(**_PAGE_CONTEXT)


@respx.mock
async def test_http_500_raises_adapter_output_error_with_status():
    """HTTP 500 raises AdapterOutputError mentioning the status code."""
    respx.post(_ENDPOINT).mock(return_value=httpx.Response(500, text="err"))
    with pytest.raises(AdapterOutputError, match="500"):
        await LocalAdapter()(**_PAGE_CONTEXT)


@respx.mock
async def test_http_403_raises_adapter_output_error_with_status():
    """HTTP 403 raises AdapterOutputError mentioning the status code."""
    respx.post(_ENDPOINT).mock(return_value=httpx.Response(403, text="forbidden"))
    with pytest.raises(AdapterOutputError, match="403"):
        await LocalAdapter()(**_PAGE_CONTEXT)


@respx.mock
async def test_connection_error_raises_adapter_output_error():
    """A connection-refused error raises AdapterOutputError."""
    respx.post(_ENDPOINT).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(AdapterOutputError):
        await LocalAdapter()(**_PAGE_CONTEXT)


# ---------------------------------------------------------------------------
# Error handling — response parsing failures
# ---------------------------------------------------------------------------

@respx.mock
async def test_non_json_http_body_raises_adapter_output_error():
    """A non-JSON HTTP response body raises AdapterOutputError."""
    respx.post(_ENDPOINT).mock(return_value=httpx.Response(200, text="not json"))
    with pytest.raises(AdapterOutputError, match="not valid JSON"):
        await LocalAdapter()(**_PAGE_CONTEXT)


@respx.mock
async def test_non_json_model_content_raises_adapter_output_error():
    """Plain-text model content (not JSON) raises AdapterOutputError."""
    body = {"choices": [{"message": {"content": "I cannot help with that."}}]}
    respx.post(_ENDPOINT).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(AdapterOutputError, match="not valid JSON"):
        await LocalAdapter()(**_PAGE_CONTEXT)


@respx.mock
async def test_missing_choices_key_raises_adapter_output_error():
    """Missing 'choices' key in response raises AdapterOutputError."""
    respx.post(_ENDPOINT).mock(return_value=httpx.Response(200, json={"result": "x"}))
    with pytest.raises(AdapterOutputError, match="unexpected structure"):
        await LocalAdapter()(**_PAGE_CONTEXT)


@respx.mock
async def test_empty_choices_list_raises_adapter_output_error():
    """Empty 'choices' list raises AdapterOutputError."""
    respx.post(_ENDPOINT).mock(return_value=httpx.Response(200, json={"choices": []}))
    with pytest.raises(AdapterOutputError, match="unexpected structure"):
        await LocalAdapter()(**_PAGE_CONTEXT)


@respx.mock
async def test_missing_content_key_raises_adapter_output_error():
    """Missing 'content' in message raises AdapterOutputError."""
    body = {"choices": [{"message": {"role": "assistant"}}]}
    respx.post(_ENDPOINT).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(AdapterOutputError, match="unexpected structure"):
        await LocalAdapter()(**_PAGE_CONTEXT)


# ---------------------------------------------------------------------------
# Thinking-model tag stripping
# ---------------------------------------------------------------------------

@respx.mock
async def test_think_tag_stripped_before_json_parse():
    """<think>...</think> block before JSON is stripped; valid JSON is returned."""
    raw = "<think>Let me reason about this step by step.</think>\n" + json.dumps(_VALID_NAV_DICT)
    body = {"choices": [{"message": {"content": raw}}]}
    respx.post(_ENDPOINT).mock(return_value=httpx.Response(200, json=body))
    result = await LocalAdapter()(**_PAGE_CONTEXT)
    assert result["found"] is False


@respx.mock
async def test_thinking_tag_variant_stripped():
    """<thinking>...</thinking> variant (some models use this form) is also stripped."""
    raw = "<thinking>Internal chain-of-thought here.</thinking>\n" + json.dumps(_VALID_NAV_DICT)
    body = {"choices": [{"message": {"content": raw}}]}
    respx.post(_ENDPOINT).mock(return_value=httpx.Response(200, json=body))
    result = await LocalAdapter()(**_PAGE_CONTEXT)
    assert result["found"] is False


@respx.mock
async def test_think_tag_multiline_stripped():
    """A multi-line <think> block is fully stripped regardless of newlines."""
    think_block = "<think>\nLine one.\nLine two.\nLine three.\n</think>\n"
    raw = think_block + json.dumps(_VALID_NAV_DICT)
    body = {"choices": [{"message": {"content": raw}}]}
    respx.post(_ENDPOINT).mock(return_value=httpx.Response(200, json=body))
    result = await LocalAdapter()(**_PAGE_CONTEXT)
    assert result["found"] is False


@respx.mock
async def test_lone_close_think_tag_stripped():
    """A lone </think> separator (opening tag absent from content) is stripped."""
    raw = "Some reasoning text here.</think>\n" + json.dumps(_VALID_NAV_DICT)
    body = {"choices": [{"message": {"content": raw}}]}
    respx.post(_ENDPOINT).mock(return_value=httpx.Response(200, json=body))
    result = await LocalAdapter()(**_PAGE_CONTEXT)
    assert result["found"] is False


@respx.mock
async def test_no_think_tag_still_works():
    """A normal response with no think tags is parsed unchanged."""
    respx.post(_ENDPOINT).mock(return_value=_ok_response())
    result = await LocalAdapter()(**_PAGE_CONTEXT)
    assert result["found"] is False
