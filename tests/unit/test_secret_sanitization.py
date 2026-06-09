"""
Unit tests for secret sanitization in exception handling (CHAR-016, spec §18).

Covers T-25 from the test matrix: API keys and sensitive provider payloads
must not appear in exception messages or traceback chains raised to the caller.

All groq SDK calls are mocked — the groq package is an optional dependency.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from charlotte.adapters.local import LocalAdapter
from charlotte.exceptions import AdapterOutputError, CharlotteTimeoutError

_FAKE_KEY = "gsk_FAKEKEYDONOTUSE1234567890abcdef"

_PAGE_CONTEXT: dict = dict(
    goal="Find the contact page",
    navigation_hint=None,
    page_title="Home",
    page_url="http://example.com/",
    page_summary="Welcome",
    available_links=[],
    visit_history=[],
    results_so_far=0,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_groq_adapter() -> "object":
    """Return a GroqAdapter with a fake API key, mocking the groq import."""
    from charlotte.adapters.groq import GroqAdapter

    mock_groq_module = MagicMock()
    mock_client = MagicMock()
    mock_groq_module.AsyncGroq.return_value = mock_client
    with patch.dict("sys.modules", {"groq": mock_groq_module}):
        adapter = GroqAdapter(api_key=_FAKE_KEY)
    adapter._client = mock_client
    return adapter


def _assert_sanitized(exc: Exception, secret: str) -> None:
    """Assert the exception message doesn't contain the secret and chain is cut."""
    assert secret not in str(exc), f"Secret leaked into exception message: {exc}"
    assert exc.__cause__ is None, "Exception chain not suppressed (from None expected)"
    assert exc.__suppress_context__ is True, "__suppress_context__ should be True"


# ---------------------------------------------------------------------------
# T-25 — GroqAdapter: API key must not leak through exception chain
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_t25_groq_api_error_suppresses_key():
    """T-25: GroqError containing the API key is sanitized before reaching caller."""
    from charlotte.adapters.groq import GroqAdapter

    mock_groq_module = MagicMock()
    mock_client = MagicMock()
    mock_groq_module.AsyncGroq.return_value = mock_client
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()

    # Simulate a groq SDK exception that embeds the API key in its message.
    api_error = RuntimeError(f"Authentication failed for key {_FAKE_KEY}")
    mock_client.chat.completions.create = AsyncMock(side_effect=api_error)

    with patch.dict("sys.modules", {"groq": mock_groq_module}):
        adapter = GroqAdapter(api_key=_FAKE_KEY)
    adapter._client = mock_client

    with pytest.raises(AdapterOutputError) as exc_info:
        await adapter(**_PAGE_CONTEXT)

    _assert_sanitized(exc_info.value, _FAKE_KEY)


@pytest.mark.asyncio
async def test_groq_json_decode_error_suppresses_model_output():
    """Model output content (in JSONDecodeError.doc) must not reach the caller."""
    from charlotte.adapters.groq import GroqAdapter

    sensitive_content = "SECRET PAGE CONTENT that appeared in model output"
    mock_groq_module = MagicMock()
    mock_client = MagicMock()
    mock_groq_module.AsyncGroq.return_value = mock_client

    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = f"not valid json: {sensitive_content}"
    mock_client.chat.completions.create = AsyncMock(return_value=response)

    with patch.dict("sys.modules", {"groq": mock_groq_module}):
        adapter = GroqAdapter(api_key=_FAKE_KEY)
    adapter._client = mock_client

    with pytest.raises(AdapterOutputError) as exc_info:
        await adapter(**_PAGE_CONTEXT)

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True
    assert sensitive_content not in str(exc_info.value)


@pytest.mark.asyncio
async def test_groq_api_error_message_is_generic():
    """The sanitized AdapterOutputError message is generic — no provider detail."""
    from charlotte.adapters.groq import GroqAdapter

    mock_groq_module = MagicMock()
    mock_client = MagicMock()
    mock_groq_module.AsyncGroq.return_value = mock_client
    mock_client.chat.completions.create = AsyncMock(
        side_effect=RuntimeError("internal groq server detail with tokens")
    )

    with patch.dict("sys.modules", {"groq": mock_groq_module}):
        adapter = GroqAdapter(api_key=_FAKE_KEY)
    adapter._client = mock_client

    with pytest.raises(AdapterOutputError) as exc_info:
        await adapter(**_PAGE_CONTEXT)

    assert "groq server detail" not in str(exc_info.value)
    assert "tokens" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# T-25 — GroqAdapter: debug logging fires, root logger stays silent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_groq_api_error_logged_at_debug(caplog):
    """Raw exception is logged at DEBUG — not visible at default WARNING level."""
    from charlotte.adapters.groq import GroqAdapter

    mock_groq_module = MagicMock()
    mock_client = MagicMock()
    mock_groq_module.AsyncGroq.return_value = mock_client
    mock_client.chat.completions.create = AsyncMock(
        side_effect=RuntimeError("groq internal error")
    )

    with patch.dict("sys.modules", {"groq": mock_groq_module}):
        adapter = GroqAdapter(api_key=_FAKE_KEY)
    adapter._client = mock_client

    with caplog.at_level(logging.DEBUG, logger="charlotte.adapters.groq"):
        with pytest.raises(AdapterOutputError):
            await adapter(**_PAGE_CONTEXT)

    assert any("Groq API call failed" in r.message for r in caplog.records)
    assert all(r.levelno == logging.DEBUG for r in caplog.records
               if "Groq API call failed" in r.message)


@pytest.mark.asyncio
async def test_groq_error_not_logged_at_warning_by_default(caplog):
    """At the default WARNING level, no charlotte logger output is produced."""
    from charlotte.adapters.groq import GroqAdapter

    mock_groq_module = MagicMock()
    mock_client = MagicMock()
    mock_groq_module.AsyncGroq.return_value = mock_client
    mock_client.chat.completions.create = AsyncMock(
        side_effect=RuntimeError("groq internal error")
    )

    with patch.dict("sys.modules", {"groq": mock_groq_module}):
        adapter = GroqAdapter(api_key=_FAKE_KEY)
    adapter._client = mock_client

    with caplog.at_level(logging.WARNING, logger="charlotte.adapters.groq"):
        with pytest.raises(AdapterOutputError):
            await adapter(**_PAGE_CONTEXT)

    charlotte_records = [r for r in caplog.records if r.name.startswith("charlotte")]
    assert charlotte_records == []


# ---------------------------------------------------------------------------
# LocalAdapter — secret sanitization
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_local_http_status_error_suppresses_response_body():
    """HTTP error response body (may contain server secrets) is not chained."""
    respx.post("http://localhost:11434/api/chat").mock(
        return_value=httpx.Response(401, text="Unauthorized: bad credentials xyz")
    )
    adapter = LocalAdapter()

    with pytest.raises(AdapterOutputError) as exc_info:
        await adapter(**_PAGE_CONTEXT)

    _assert_sanitized(exc_info.value, "bad credentials xyz")
    assert "401" in str(exc_info.value)


@respx.mock
@pytest.mark.asyncio
async def test_local_json_decode_error_suppresses_model_output():
    """JSONDecodeError.doc (model output) is not chained to the raised exception."""
    sensitive = "SENSITIVE MODEL OUTPUT CONTENT"
    respx.post("http://localhost:11434/api/chat").mock(
        return_value=httpx.Response(200, json={
            "message": {"content": f"not json: {sensitive}"}, "done": True
        })
    )
    adapter = LocalAdapter()

    with pytest.raises(AdapterOutputError) as exc_info:
        await adapter(**_PAGE_CONTEXT)

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True
    assert sensitive not in str(exc_info.value)


@respx.mock
@pytest.mark.asyncio
async def test_local_unexpected_structure_suppresses_response():
    """KeyError/IndexError from malformed response structure is not chained."""
    # Return a response missing the expected 'choices' structure.
    respx.post("http://localhost:11434/api/chat").mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )
    adapter = LocalAdapter()

    with pytest.raises(AdapterOutputError) as exc_info:
        await adapter(**_PAGE_CONTEXT)

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


@respx.mock
@pytest.mark.asyncio
async def test_local_api_error_logged_at_debug(caplog):
    """Network failure is logged at DEBUG on the charlotte.adapters.local logger."""
    respx.post("http://localhost:11434/api/chat").mock(
        side_effect=httpx.ConnectError("refused")
    )
    adapter = LocalAdapter()

    with caplog.at_level(logging.DEBUG, logger="charlotte.adapters.local"):
        with pytest.raises(AdapterOutputError):
            await adapter(**_PAGE_CONTEXT)

    assert any("Local model API call failed" in r.message for r in caplog.records)


@respx.mock
@pytest.mark.asyncio
async def test_local_timeout_preserved_as_charlotte_error():
    """Timeout exception is converted to CharlotteTimeoutError, not suppressed."""
    respx.post("http://localhost:11434/api/chat").mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    adapter = LocalAdapter()

    with pytest.raises(CharlotteTimeoutError, match="timed out"):
        await adapter(**_PAGE_CONTEXT)
