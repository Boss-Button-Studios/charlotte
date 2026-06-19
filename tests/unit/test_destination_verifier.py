"""Unit tests for DefaultDestinationVerifier (spec §7)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from charlotte.core.destination_verifier import (
    DefaultDestinationVerifier,
    DestinationVerifierProtocol,
    _bm25_score,
    _extract_filename,
    _has_password_form,
    _is_login_wall_redirect,
)
from charlotte.models import GoalContext, ResultContent

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

_URL = "http://example.com/page"
_DOC_URL = "http://example.com/handbook.pdf"

# HTML with many occurrences of "contact" — should score above BM25 threshold.
_RELEVANT_HTML = b"""
<html><body>
<h1>Contact Us</h1>
<p>Contact our team for support. Our contact details are below.</p>
<p>Email contact: support@example.com. Phone contact: 555-1234.</p>
</body></html>
"""

# HTML with no query terms — BM25 score will be 0.
_IRRELEVANT_HTML = b"""
<html><body>
<p>This page is about cooking recipes, pasta, and vegetable soups.</p>
<p>Completely unrelated content about kitchen equipment.</p>
</body></html>
"""

_LOGIN_HTML = b"""
<html><body>
<form action="/authenticate"><input type="password" name="pass"></form>
</body></html>
"""


def _ctx(
    goal: str = "Find the contact page",
    goal_type: str = "navigation",
    anchor_terms: list[str] | None = None,
) -> GoalContext:
    return GoalContext(
        goal=goal,
        navigation_hint=None,
        goal_type=goal_type,
        goal_type_confidence=1.0,
        synonyms={},
        anchor_terms=anchor_terms or ["contact"],
        negative_terms=[],
        regex_hints=[],
        description="",
        source="deterministic",
        model_used=None,
        created_at=datetime.now(timezone.utc),
        locale="en_US",
        validation_warnings=[],
    )


def _verifier(**kwargs) -> DefaultDestinationVerifier:
    kwargs.setdefault("connect_timeout", 5.0)
    kwargs.setdefault("read_timeout", 5.0)
    return DefaultDestinationVerifier(**kwargs)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

def test_default_verifier_satisfies_protocol():
    assert isinstance(_verifier(), DestinationVerifierProtocol)


# ---------------------------------------------------------------------------
# Login wall helpers (pure functions)
# ---------------------------------------------------------------------------

def _mock_history_url(url: str) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.url = url
    return r


def test_login_wall_redirect_detects_login_path():
    history = [_mock_history_url("http://example.com/login")]
    assert _is_login_wall_redirect(history) is True


def test_login_wall_redirect_detects_signin():
    history = [_mock_history_url("http://example.com/signin")]
    assert _is_login_wall_redirect(history) is True


def test_login_wall_redirect_detects_auth_sub_path():
    history = [_mock_history_url("http://example.com/auth")]
    assert _is_login_wall_redirect(history) is True


def test_login_wall_redirect_ignores_benign_redirect():
    history = [_mock_history_url("http://example.com/contact")]
    assert _is_login_wall_redirect(history) is False


def test_login_wall_redirect_empty_history():
    assert _is_login_wall_redirect([]) is False


def test_has_password_form_positive():
    assert _has_password_form('<input type="password" name="p">') is True


def test_has_password_form_single_quotes():
    assert _has_password_form("<input type='password'>") is True


def test_has_password_form_mixed_attributes():
    assert _has_password_form('<input name="p" type="password" id="x">') is True


def test_has_password_form_negative():
    assert _has_password_form('<input type="text" name="user">') is False


def test_has_password_form_empty():
    assert _has_password_form("") is False


# ---------------------------------------------------------------------------
# Filename extraction helper
# ---------------------------------------------------------------------------

def test_extract_filename_from_content_disposition():
    assert _extract_filename('attachment; filename="report.pdf"', _URL) == "report.pdf"


def test_extract_filename_from_url_path():
    assert _extract_filename("", "http://example.com/docs/handbook.pdf") == "handbook.pdf"


def test_extract_filename_no_extension_in_url():
    result = _extract_filename("", "http://example.com/docs/")
    assert result is None


def test_extract_filename_content_disposition_takes_precedence():
    result = _extract_filename('attachment; filename="override.pdf"', "http://example.com/other.csv")
    assert result == "override.pdf"


# ---------------------------------------------------------------------------
# BM25 scoring helper
# ---------------------------------------------------------------------------

def test_bm25_score_relevant_text():
    text = "Contact us at our contact page for contact information."
    score = _bm25_score(text, ["contact"], [])
    assert score > 0.0


def test_bm25_score_irrelevant_text():
    text = "Pasta recipes include lasagna and spaghetti carbonara."
    score = _bm25_score(text, ["contact"], [])
    assert score == 0.0


def test_bm25_score_empty_query():
    score = _bm25_score("Any text here", [], [])
    assert score == 0.0


def test_bm25_score_empty_text():
    score = _bm25_score("", ["contact"], [])
    assert score == 0.0


def test_bm25_score_synonyms_included():
    text = "Reach out to our support team for help."
    score = _bm25_score(text, [], ["support", "reach"])
    assert score > 0.0


# ---------------------------------------------------------------------------
# Mode: off
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_mode_off_always_passes():
    v = _verifier(mode="off")
    result, content = await v(url=_URL, goal_context=_ctx())
    assert result.passed is True
    assert result.mode == "off"
    assert result.score is None
    assert content is None


@respx.mock
@pytest.mark.asyncio
async def test_mode_off_makes_no_http_request():
    # respx will error if any unexpected request is made
    v = _verifier(mode="off")
    result, _ = await v(url=_URL, goal_context=_ctx())
    assert result.passed is True


# ---------------------------------------------------------------------------
# Mode: existence — HTTP status checks
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_existence_200_passes():
    respx.get(_URL).mock(return_value=httpx.Response(200, content=_RELEVANT_HTML))
    v = _verifier(mode="existence")
    result, _ = await v(url=_URL, goal_context=_ctx())
    assert result.passed is True
    assert result.mode == "existence"


@respx.mock
@pytest.mark.asyncio
async def test_existence_404_fails():
    respx.get(_URL).mock(return_value=httpx.Response(404, content=b"not found"))
    v = _verifier(mode="existence")
    result, _ = await v(url=_URL, goal_context=_ctx())
    assert result.passed is False
    assert "http_404" in result.reason


@respx.mock
@pytest.mark.asyncio
async def test_existence_500_fails():
    respx.get(_URL).mock(return_value=httpx.Response(500, content=b"error"))
    v = _verifier(mode="existence")
    result, _ = await v(url=_URL, goal_context=_ctx())
    assert result.passed is False
    assert "http_500" in result.reason


@respx.mock
@pytest.mark.asyncio
async def test_existence_empty_body_fails():
    respx.get(_URL).mock(return_value=httpx.Response(200, content=b""))
    v = _verifier(mode="existence")
    result, _ = await v(url=_URL, goal_context=_ctx())
    assert result.passed is False
    assert result.reason == "empty_response"


@respx.mock
@pytest.mark.asyncio
async def test_existence_login_wall_form_fails():
    respx.get(_URL).mock(
        return_value=httpx.Response(200, content=_LOGIN_HTML,
                                    headers={"content-type": "text/html"})
    )
    v = _verifier(mode="existence")
    result, _ = await v(url=_URL, goal_context=_ctx())
    assert result.passed is False
    assert result.reason == "login_wall_form"


# ---------------------------------------------------------------------------
# Mode: relevance — BM25 scoring
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_relevance_passes_with_matching_content():
    respx.get(_URL).mock(
        return_value=httpx.Response(200, content=_RELEVANT_HTML,
                                    headers={"content-type": "text/html; charset=utf-8"})
    )
    v = _verifier(mode="relevance", verify_threshold=0.1)
    result, _ = await v(url=_URL, goal_context=_ctx(anchor_terms=["contact"]))
    assert result.passed is True
    assert result.score is not None and result.score > 0.0


@respx.mock
@pytest.mark.asyncio
async def test_relevance_fails_with_irrelevant_content():
    respx.get(_URL).mock(
        return_value=httpx.Response(200, content=_IRRELEVANT_HTML,
                                    headers={"content-type": "text/html; charset=utf-8"})
    )
    v = _verifier(mode="relevance", verify_threshold=0.3)
    result, _ = await v(url=_URL, goal_context=_ctx(anchor_terms=["contact"]))
    assert result.passed is False
    assert result.score == 0.0


@respx.mock
@pytest.mark.asyncio
async def test_relevance_score_recorded_in_result():
    respx.get(_URL).mock(
        return_value=httpx.Response(200, content=_RELEVANT_HTML,
                                    headers={"content-type": "text/html; charset=utf-8"})
    )
    v = _verifier(mode="relevance", verify_threshold=0.0)
    result, _ = await v(url=_URL, goal_context=_ctx(anchor_terms=["contact"]))
    assert isinstance(result.score, float)


@respx.mock
@pytest.mark.asyncio
async def test_relevance_binary_content_skips_text_scoring():
    """Binary responses cannot be BM25-scored; verifier falls back to existence pass."""
    respx.get(_DOC_URL).mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4 binary content",
                                    headers={"content-type": "application/pdf"})
    )
    v = _verifier(mode="relevance", verify_threshold=0.9)
    result, _ = await v(url=_DOC_URL, goal_context=_ctx(goal_type="document_link"))
    assert result.passed is True
    assert result.score is None
    assert result.reason == "ok_existence_binary"


# ---------------------------------------------------------------------------
# Mode: full — falls back to BM25 with raised threshold when extras absent
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_full_mode_without_embeddings_raises_threshold(monkeypatch):
    """Without sentence-transformers, full mode uses BM25 with threshold + 0.15.

    BM25 is patched to return 0.1 so the effective threshold of 0.15 (0.0 + 0.15)
    predictably causes a failure regardless of page content.
    """
    import charlotte.core.destination_verifier as _dv_mod

    respx.get(_URL).mock(
        return_value=httpx.Response(200, content=_RELEVANT_HTML,
                                    headers={"content-type": "text/html; charset=utf-8"})
    )
    # Make sentence_transformers unavailable so the BM25 fallback path is taken.
    import builtins
    real_import = builtins.__import__

    def _no_st(name, *args, **kwargs):
        if name == "sentence_transformers":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_st)
    # Fix BM25 score to 0.1 — below the raised threshold of 0.15 (0.0 + 0.15).
    monkeypatch.setattr(_dv_mod, "_bm25_score", lambda *_a, **_kw: 0.1)

    v = _verifier(mode="full", verify_threshold=0.0)
    result, _ = await v(url=_URL, goal_context=_ctx(anchor_terms=["contact"]))
    assert result.passed is False
    assert "below_threshold" in result.reason


# ---------------------------------------------------------------------------
# Content delivery (§7.7)
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_document_link_captures_bytes_by_default():
    content_bytes = b"%PDF-1.4 binary content here"
    respx.get(_DOC_URL).mock(
        return_value=httpx.Response(200, content=content_bytes,
                                    headers={"content-type": "application/pdf"})
    )
    v = _verifier(mode="existence")
    result, content = await v(url=_DOC_URL, goal_context=_ctx(goal_type="document_link"))
    assert result.passed is True
    assert content is not None
    assert isinstance(content, ResultContent)
    assert content.content == content_bytes


@respx.mock
@pytest.mark.asyncio
async def test_navigation_goal_does_not_capture_bytes_by_default():
    respx.get(_URL).mock(return_value=httpx.Response(200, content=_RELEVANT_HTML))
    v = _verifier(mode="existence")
    result, content = await v(url=_URL, goal_context=_ctx(goal_type="navigation"))
    assert result.passed is True
    assert content is None


@respx.mock
@pytest.mark.asyncio
async def test_fetch_result_content_true_captures_for_navigation():
    body = b"<html><body>nav content</body></html>"
    respx.get(_URL).mock(return_value=httpx.Response(200, content=body))
    v = _verifier(mode="existence", fetch_result_content=True)
    result, content = await v(url=_URL, goal_context=_ctx(goal_type="navigation"))
    assert result.passed is True
    assert content is not None
    assert content.content == body


@respx.mock
@pytest.mark.asyncio
async def test_fetch_result_content_false_suppresses_for_document_link():
    respx.get(_DOC_URL).mock(return_value=httpx.Response(200, content=b"%PDF"))
    v = _verifier(mode="existence", fetch_result_content=False)
    _, content = await v(url=_DOC_URL, goal_context=_ctx(goal_type="document_link"))
    assert content is None


@respx.mock
@pytest.mark.asyncio
async def test_content_not_captured_when_verification_fails():
    """Failed relevance check → no ResultContent even for document_link goals."""
    # Use HTML with no query-term overlap so BM25 scores 0 and fails.
    irrelevant_html = b"<html><body><p>Completely unrelated page about gardening tips.</p></body></html>"
    respx.get(_DOC_URL).mock(
        return_value=httpx.Response(200, content=irrelevant_html,
                                    headers={"content-type": "text/html; charset=utf-8"})
    )
    v = _verifier(mode="relevance", verify_threshold=0.5)
    result, content = await v(url=_DOC_URL, goal_context=_ctx(goal_type="document_link"))
    assert result.passed is False
    assert content is None


@respx.mock
@pytest.mark.asyncio
async def test_result_content_metadata_populated():
    body = b"file bytes"
    respx.get(_DOC_URL).mock(
        return_value=httpx.Response(
            200, content=body,
            headers={
                "content-type": "application/pdf",
                "etag": '"abc123"',
                "content-disposition": 'attachment; filename="report.pdf"',
            },
        )
    )
    v = _verifier(mode="existence")
    _, content = await v(url=_DOC_URL, goal_context=_ctx(goal_type="document_link"))
    assert content is not None
    assert content.content_type == "application/pdf"
    assert content.etag == '"abc123"'
    assert content.suggested_filename == "report.pdf"
    assert content.content_length == len(body)
    assert isinstance(content.fetched_at, datetime)


# ---------------------------------------------------------------------------
# result_to_file
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_result_to_file_writes_bytes(tmp_path):
    body = b"%PDF-1.4 the document content"
    respx.get(_DOC_URL).mock(
        return_value=httpx.Response(200, content=body,
                                    headers={"content-type": "application/pdf"})
    )
    v = _verifier(mode="existence", result_to_file=tmp_path)
    _, content = await v(url=_DOC_URL, goal_context=_ctx(goal_type="document_link"))
    assert content is not None
    assert content.content is None          # not in memory
    assert content.file_path is not None
    assert content.file_path.read_bytes() == body


@respx.mock
@pytest.mark.asyncio
async def test_result_to_file_uses_suggested_filename(tmp_path):
    respx.get(_DOC_URL).mock(
        return_value=httpx.Response(
            200, content=b"%PDF",
            headers={"content-disposition": 'attachment; filename="myfile.pdf"'},
        )
    )
    v = _verifier(mode="existence", result_to_file=tmp_path)
    _, content = await v(url=_DOC_URL, goal_context=_ctx(goal_type="document_link"))
    assert content is not None and content.file_path is not None
    assert content.file_path.name == "myfile.pdf"


# ---------------------------------------------------------------------------
# max_result_bytes enforcement (§7.7.3)
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_max_result_bytes_fails_oversized_response():
    large_body = b"x" * 200
    respx.get(_URL).mock(return_value=httpx.Response(200, content=large_body))
    v = _verifier(mode="existence", max_result_bytes=100)
    result, content = await v(url=_URL, goal_context=_ctx())
    assert result.passed is False
    assert result.reason == "response_too_large"
    assert content is None


@respx.mock
@pytest.mark.asyncio
async def test_max_result_bytes_passes_within_limit():
    body = b"x" * 50
    respx.get(_URL).mock(return_value=httpx.Response(200, content=body))
    v = _verifier(mode="existence", max_result_bytes=100)
    result, _ = await v(url=_URL, goal_context=_ctx())
    assert result.passed is True


# ---------------------------------------------------------------------------
# Network error handling
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_network_error_fails_gracefully():
    respx.get(_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    v = _verifier(mode="existence")
    result, content = await v(url=_URL, goal_context=_ctx())
    assert result.passed is False
    assert "fetch_failed" in result.reason
    assert content is None


@respx.mock
@pytest.mark.asyncio
async def test_timeout_error_fails_gracefully():
    respx.get(_URL).mock(side_effect=httpx.ReadTimeout("timed out"))
    v = _verifier(mode="existence")
    result, content = await v(url=_URL, goal_context=_ctx())
    assert result.passed is False
    assert "fetch_failed" in result.reason


# ---------------------------------------------------------------------------
# VerificationResult fields
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_verification_result_url_matches_input():
    respx.get(_URL).mock(return_value=httpx.Response(200, content=_RELEVANT_HTML))
    v = _verifier(mode="existence")
    result, _ = await v(url=_URL, goal_context=_ctx())
    assert result.url == _URL


@respx.mock
@pytest.mark.asyncio
async def test_verification_result_mode_matches_verifier():
    respx.get(_URL).mock(return_value=httpx.Response(200, content=_RELEVANT_HTML))
    v = _verifier(mode="existence")
    result, _ = await v(url=_URL, goal_context=_ctx())
    assert result.mode == "existence"


@respx.mock
@pytest.mark.asyncio
async def test_existence_mode_score_is_none():
    respx.get(_URL).mock(return_value=httpx.Response(200, content=_RELEVANT_HTML))
    v = _verifier(mode="existence")
    result, _ = await v(url=_URL, goal_context=_ctx())
    assert result.score is None


@respx.mock
@pytest.mark.asyncio
async def test_document_link_html_response_fails():
    """For document_link goals the result must be a binary file.  An HTML response
    means the model stopped at a listing page rather than the document itself."""
    respx.get(_URL).mock(return_value=httpx.Response(
        200, content=_RELEVANT_HTML, headers={"Content-Type": "text/html; charset=UTF-8"},
    ))
    v = _verifier()
    result, content = await v(url=_URL, goal_context=_ctx(goal_type="document_link"))
    assert result.passed is False
    assert result.reason == "html_not_document"
    assert content is None


@respx.mock
@pytest.mark.asyncio
async def test_non_document_link_html_response_passes_relevance():
    """The html_not_document guard must not fire for non-document_link goals."""
    respx.get(_URL).mock(return_value=httpx.Response(
        200, content=_RELEVANT_HTML, headers={"Content-Type": "text/html; charset=UTF-8"},
    ))
    v = _verifier()
    result, _ = await v(url=_URL, goal_context=_ctx(goal_type="navigation"))
    assert result.passed is True


@respx.mock
@pytest.mark.asyncio
async def test_result_to_file_write_failure_raises_config_error(tmp_path):
    """A write failure (result_to_file points at a file, not a dir) surfaces as a
    named CharlotteConfigError, never a raw OSError — consistent with
    _build_binary_result and the 'named exceptions only' rule."""
    from charlotte.exceptions import CharlotteConfigError
    blocked = tmp_path / "not_a_dir"
    blocked.write_text("i am a file")
    respx.get(_DOC_URL).mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4 x",
                                    headers={"content-type": "application/pdf"})
    )
    v = _verifier(mode="existence", result_to_file=blocked)
    with pytest.raises(CharlotteConfigError):
        await v(url=_DOC_URL, goal_context=_ctx(goal_type="document_link"))
