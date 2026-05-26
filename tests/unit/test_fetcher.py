"""
Unit tests for the page fetcher (CHAR-004).

Covers T-15 through T-20 from the test matrix, plus unit tests for each
component of the timeout policy, redirect policy, and Playwright stub.
"""

from unittest.mock import PropertyMock, patch

import httpx
import pytest
import respx

from charlotte.core.fetcher import FetchResult, PageFetcher
from charlotte.exceptions import (
    CharlotteConfigError,
    CharlotteNetworkError,
    CharlotteRedirectError,
    CharlotteTimeoutError,
)

_BASE = "http://example.com"
_OTHER = "http://other.com"


def _fetcher(**kwargs) -> PageFetcher:
    kwargs.setdefault("polite_delay", 0.0)
    kwargs.setdefault("allowed_domains", {"example.com"})
    return PageFetcher(**kwargs)


# ---------------------------------------------------------------------------
# Playwright stub
# ---------------------------------------------------------------------------

def test_render_js_true_raises_at_instantiation():
    with pytest.raises(CharlotteConfigError, match="playwright"):
        PageFetcher(allowed_domains={"example.com"}, render_js=True)


def test_render_js_false_does_not_raise():
    PageFetcher(allowed_domains={"example.com"}, render_js=False)


# ---------------------------------------------------------------------------
# Happy path — basic fetch
# ---------------------------------------------------------------------------

@respx.mock
async def test_basic_fetch_returns_result():
    respx.get(f"{_BASE}/").mock(return_value=httpx.Response(200, text="<html/>"))
    result = await _fetcher().fetch(f"{_BASE}/", visited_urls=set())
    assert isinstance(result, FetchResult)
    assert result.status_code == 200
    assert result.html == "<html/>"
    assert result.url == f"{_BASE}/"
    assert result.redirect_chain == []


@respx.mock
async def test_fetch_ms_is_non_negative():
    respx.get(f"{_BASE}/").mock(return_value=httpx.Response(200, text=""))
    result = await _fetcher().fetch(f"{_BASE}/", visited_urls=set())
    assert result.fetch_ms >= 0


@respx.mock
async def test_404_returned_as_result():
    respx.get(f"{_BASE}/missing").mock(return_value=httpx.Response(404, text="Not found"))
    result = await _fetcher().fetch(f"{_BASE}/missing", visited_urls=set())
    assert result.status_code == 404


# ---------------------------------------------------------------------------
# T-15: Redirect within allowed_domains — followed correctly
# ---------------------------------------------------------------------------

@respx.mock
async def test_t15_redirect_within_allowed_domains_followed():
    respx.get(f"{_BASE}/old").mock(
        return_value=httpx.Response(301, headers={"location": f"{_BASE}/new"})
    )
    respx.get(f"{_BASE}/new").mock(return_value=httpx.Response(200, text="<html/>"))

    result = await _fetcher().fetch(f"{_BASE}/old", visited_urls=set())

    assert result.status_code == 200
    assert result.url == f"{_BASE}/new"
    assert result.redirect_chain == [(301, f"{_BASE}/new")]


@respx.mock
async def test_redirect_chain_logged_per_hop():
    respx.get(f"{_BASE}/a").mock(
        return_value=httpx.Response(301, headers={"location": f"{_BASE}/b"})
    )
    respx.get(f"{_BASE}/b").mock(
        return_value=httpx.Response(302, headers={"location": f"{_BASE}/c"})
    )
    respx.get(f"{_BASE}/c").mock(return_value=httpx.Response(200, text=""))

    result = await _fetcher().fetch(f"{_BASE}/a", visited_urls=set())

    assert result.url == f"{_BASE}/c"
    assert result.redirect_chain == [(301, f"{_BASE}/b"), (302, f"{_BASE}/c")]


# ---------------------------------------------------------------------------
# T-16: Redirect to domain outside allowed_domains — not followed
# ---------------------------------------------------------------------------

@respx.mock
async def test_t16_cross_domain_redirect_raises():
    respx.get(f"{_BASE}/page").mock(
        return_value=httpx.Response(302, headers={"location": f"{_OTHER}/page"})
    )

    with pytest.raises(CharlotteRedirectError, match="disallowed domain"):
        await _fetcher().fetch(f"{_BASE}/page", visited_urls=set())


@respx.mock
async def test_cross_domain_redirect_blocked_mid_chain():
    """Cross-domain block applies regardless of how deep in the chain it occurs."""
    respx.get(f"{_BASE}/step1").mock(
        return_value=httpx.Response(301, headers={"location": f"{_BASE}/step2"})
    )
    respx.get(f"{_BASE}/step2").mock(
        return_value=httpx.Response(302, headers={"location": f"{_OTHER}/final"})
    )

    with pytest.raises(CharlotteRedirectError, match="disallowed domain"):
        await _fetcher().fetch(f"{_BASE}/step1", visited_urls=set())


# ---------------------------------------------------------------------------
# T-17: Redirect loop (A → B → A) — detected
# ---------------------------------------------------------------------------

@respx.mock
async def test_t17_redirect_loop_detected():
    respx.get(f"{_BASE}/a").mock(
        return_value=httpx.Response(302, headers={"location": f"{_BASE}/b"})
    )
    respx.get(f"{_BASE}/b").mock(
        return_value=httpx.Response(302, headers={"location": f"{_BASE}/a"})
    )

    fetcher = _fetcher(allowed_domains={"example.com"})
    with pytest.raises(CharlotteRedirectError, match="loop"):
        await fetcher.fetch(f"{_BASE}/a", visited_urls=set())


@respx.mock
async def test_redirect_to_previously_visited_url_raises():
    """Redirect into a URL already in visited_urls counts as a loop."""
    respx.get(f"{_BASE}/new").mock(
        return_value=httpx.Response(302, headers={"location": f"{_BASE}/old"})
    )

    visited = {"http://example.com/old"}
    with pytest.raises(CharlotteRedirectError, match="loop"):
        await _fetcher().fetch(f"{_BASE}/new", visited_urls=visited)


# ---------------------------------------------------------------------------
# T-18: Redirect chain exceeding 5 hops — CharlotteRedirectError
# ---------------------------------------------------------------------------

@respx.mock
async def test_t18_redirect_chain_exceeds_max_raises():
    for i in range(6):
        respx.get(f"{_BASE}/r{i}").mock(
            return_value=httpx.Response(302, headers={"location": f"{_BASE}/r{i + 1}"})
        )
    respx.get(f"{_BASE}/r6").mock(return_value=httpx.Response(200, text=""))

    with pytest.raises(CharlotteRedirectError, match="exceeded"):
        await _fetcher().fetch(f"{_BASE}/r0", visited_urls=set())


@respx.mock
async def test_exactly_five_hops_allowed():
    """A chain of exactly 5 redirects must succeed."""
    for i in range(5):
        respx.get(f"{_BASE}/r{i}").mock(
            return_value=httpx.Response(302, headers={"location": f"{_BASE}/r{i + 1}"})
        )
    respx.get(f"{_BASE}/r5").mock(return_value=httpx.Response(200, text="final"))

    result = await _fetcher().fetch(f"{_BASE}/r0", visited_urls=set())
    assert result.status_code == 200
    assert len(result.redirect_chain) == 5


# ---------------------------------------------------------------------------
# T-19: Connect timeout — CharlotteTimeoutError
# ---------------------------------------------------------------------------

@respx.mock
async def test_t19_connect_timeout_raises():
    respx.get(f"{_BASE}/").mock(side_effect=httpx.ConnectTimeout("timed out"))

    with pytest.raises(CharlotteTimeoutError, match="Connect timeout"):
        await _fetcher().fetch(f"{_BASE}/", visited_urls=set())


# ---------------------------------------------------------------------------
# T-20: Read timeout — CharlotteTimeoutError
# ---------------------------------------------------------------------------

@respx.mock
async def test_t20_read_timeout_raises():
    respx.get(f"{_BASE}/").mock(side_effect=httpx.ReadTimeout("timed out"))

    with pytest.raises(CharlotteTimeoutError, match="Read timeout"):
        await _fetcher().fetch(f"{_BASE}/", visited_urls=set())


# ---------------------------------------------------------------------------
# Network error → CharlotteNetworkError
# ---------------------------------------------------------------------------

@respx.mock
async def test_connect_error_raises_network_error():
    respx.get(f"{_BASE}/").mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(CharlotteNetworkError, match="Network error"):
        await _fetcher().fetch(f"{_BASE}/", visited_urls=set())


# ---------------------------------------------------------------------------
# All Charlotte exceptions are CharlotteError subclasses
# ---------------------------------------------------------------------------

@respx.mock
async def test_timeout_is_charlotte_error():
    from charlotte.exceptions import CharlotteError
    respx.get(f"{_BASE}/").mock(side_effect=httpx.ConnectTimeout("x"))
    with pytest.raises(CharlotteError):
        await _fetcher().fetch(f"{_BASE}/", visited_urls=set())


@respx.mock
async def test_redirect_error_is_charlotte_error():
    from charlotte.exceptions import CharlotteError
    respx.get(f"{_BASE}/page").mock(
        return_value=httpx.Response(302, headers={"location": f"{_OTHER}/page"})
    )
    with pytest.raises(CharlotteError):
        await _fetcher().fetch(f"{_BASE}/page", visited_urls=set())


# ---------------------------------------------------------------------------
# Exception coverage — branches added for spec §18 (no raw httpx to caller)
# ---------------------------------------------------------------------------

@respx.mock
async def test_invalid_url_raises_config_error():
    respx.get(f"{_BASE}/").mock(side_effect=httpx.InvalidURL("not a url"))
    with pytest.raises(CharlotteConfigError, match="Invalid URL"):
        await _fetcher().fetch(f"{_BASE}/", visited_urls=set())


@respx.mock
async def test_protocol_error_raises_network_error():
    # httpx.ProtocolError is a RequestError but not a NetworkError or TimeoutError —
    # it must fall through to the RequestError fallback handler.
    respx.get(f"{_BASE}/").mock(side_effect=httpx.ProtocolError("bad protocol"))
    with pytest.raises(CharlotteNetworkError, match="Request failed"):
        await _fetcher().fetch(f"{_BASE}/", visited_urls=set())


@respx.mock
async def test_decoding_error_on_response_text_raises_network_error():
    # DecodingError raised when reading response.text (e.g. broken content-encoding)
    # is wrapped as CharlotteNetworkError, not propagated raw.
    respx.get(f"{_BASE}/").mock(return_value=httpx.Response(200, content=b"ok"))
    with patch.object(
        httpx.Response, "text", new_callable=PropertyMock,
        side_effect=httpx.DecodingError("corrupt encoding"),
    ):
        with pytest.raises(CharlotteNetworkError, match="decode"):
            await _fetcher().fetch(f"{_BASE}/", visited_urls=set())


@respx.mock
async def test_invalid_redirect_destination_raises_redirect_error():
    # If the redirect Location resolves to a URL normalize_url rejects, we get
    # CharlotteRedirectError rather than a raw CharlotteConfigError.
    # The first normalize_url call builds chain_seen; the second (for the
    # redirect destination) is the one that raises.
    respx.get(f"{_BASE}/page").mock(
        return_value=httpx.Response(302, headers={"location": f"{_BASE}/dest"})
    )
    with patch("charlotte.core.fetcher.normalize_url", side_effect=[
        "http://example.com/page",   # initial chain_seen seed — succeeds
        CharlotteConfigError("bad"), # redirect destination normalize — raises
    ]):
        with pytest.raises(CharlotteRedirectError, match="not a valid URL"):
            await _fetcher().fetch(f"{_BASE}/page", visited_urls=set())
