"""
Unit tests for the page fetcher (CHAR-004).

Covers T-15 through T-20 from the test matrix, plus unit tests for each
component of the timeout policy, redirect policy, and Playwright stub.
"""

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import httpx
import pytest
import respx

from charlotte.core.fetcher import FetchResult, PageFetcher
from charlotte.exceptions import (
    CharlotteConfigError,
    CharlotteNetworkError,
    CharlotteRedirectError,
    CharlotteTimeoutError,
    RobotsError,
)

_BASE = "http://example.com"
_OTHER = "http://other.com"


def _fetcher(**kwargs) -> PageFetcher:
    kwargs.setdefault("polite_delay", 0.0)
    kwargs.setdefault("allowed_domains", {"example.com"})
    return PageFetcher(**kwargs)


# ---------------------------------------------------------------------------
# T-26 — Playwright not installed → CharlotteConfigError immediately
# ---------------------------------------------------------------------------

def test_t26_playwright_not_installed_raises_config_error():
    """T-26: CharlotteConfigError is raised at instantiation when playwright is absent."""
    with patch(
        "charlotte.core.fetcher._import_playwright",
        side_effect=CharlotteConfigError("playwright"),
    ):
        with pytest.raises(CharlotteConfigError, match="playwright"):
            PageFetcher(allowed_domains={"example.com"}, render_js=True)


def test_render_js_false_does_not_require_playwright():
    """render_js=False never imports playwright — no error even if absent."""
    PageFetcher(allowed_domains={"example.com"}, render_js=False)


# ---------------------------------------------------------------------------
# T-05 — Playwright render_js=True path (mocked browser)
# ---------------------------------------------------------------------------

def _make_playwright_fetcher(
    *,
    url: str = "http://example.com/",
    html: str = "<html><body>JS content</body></html>",
    status: int = 200,
    final_url: str | None = None,
    side_effect: Exception | None = None,
) -> tuple["PageFetcher", MagicMock]:
    """Build a PageFetcher with a mocked playwright factory and return (fetcher, mock_pw)."""
    mock_response = MagicMock()
    mock_response.status = status

    mock_page = MagicMock()
    mock_page.url = final_url or url
    mock_page.content = AsyncMock(return_value=html)
    if side_effect:
        mock_page.goto = AsyncMock(side_effect=side_effect)
    else:
        mock_page.goto = AsyncMock(return_value=mock_response)

    mock_browser = MagicMock()
    mock_browser.new_page = AsyncMock(return_value=mock_page)
    mock_browser.close = AsyncMock()

    mock_pw = MagicMock()
    mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_pw)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_cm)

    with patch(
        "charlotte.core.fetcher._import_playwright",
        return_value=(mock_factory, TimeoutError),
    ):
        fetcher = PageFetcher(
            allowed_domains={"example.com"},
            render_js=True,
            polite_delay=0.0,
            render_timeout=5.0,
        )

    return fetcher, mock_pw


@pytest.mark.asyncio
async def test_t05_render_js_returns_fetch_result():
    """T-05: render_js=True fetches via Playwright and returns a valid FetchResult."""
    fetcher, _ = _make_playwright_fetcher(html="<html><body>JS page</body></html>")
    result = await fetcher.fetch("http://example.com/", visited_urls=set())
    assert isinstance(result, FetchResult)
    assert result.html == "<html><body>JS page</body></html>"
    assert result.status_code == 200
    assert result.url == "http://example.com/"
    assert result.redirect_chain == []


@pytest.mark.asyncio
async def test_render_js_captures_final_url_after_redirect():
    """Playwright follows redirects internally; final_url is captured from page.url."""
    fetcher, _ = _make_playwright_fetcher(
        url="http://example.com/old",
        final_url="http://example.com/new",
    )
    result = await fetcher.fetch("http://example.com/old", visited_urls=set())
    assert result.url == "http://example.com/new"
    assert result.redirect_chain == []  # not tracked on Playwright path


@pytest.mark.asyncio
async def test_render_js_fetch_ms_is_non_negative():
    fetcher, _ = _make_playwright_fetcher()
    result = await fetcher.fetch("http://example.com/", visited_urls=set())
    assert result.fetch_ms >= 0


@pytest.mark.asyncio
async def test_render_js_timeout_raises_charlotte_timeout_error():
    """Playwright TimeoutError is converted to CharlotteTimeoutError."""
    fetcher, _ = _make_playwright_fetcher(side_effect=TimeoutError("timed out"))
    with pytest.raises(CharlotteTimeoutError, match="Render timeout"):
        await fetcher.fetch("http://example.com/", visited_urls=set())


@pytest.mark.asyncio
async def test_render_js_unexpected_error_raises_network_error():
    """Unexpected Playwright errors are wrapped as CharlotteNetworkError."""
    fetcher, _ = _make_playwright_fetcher(side_effect=RuntimeError("browser crashed"))
    with pytest.raises(CharlotteNetworkError, match="Playwright error"):
        await fetcher.fetch("http://example.com/", visited_urls=set())


@pytest.mark.asyncio
async def test_render_js_off_domain_final_url_raises_redirect_error():
    """If Playwright lands on a disallowed domain, CharlotteRedirectError is raised."""
    fetcher, _ = _make_playwright_fetcher(
        url="http://example.com/page",
        final_url="http://evil.com/landed",
    )
    with pytest.raises(CharlotteRedirectError, match="disallowed domain"):
        await fetcher.fetch("http://example.com/page", visited_urls=set())


@pytest.mark.asyncio
async def test_render_js_already_visited_final_url_raises_redirect_error():
    """If Playwright lands on an already-visited URL, CharlotteRedirectError is raised."""
    fetcher, _ = _make_playwright_fetcher(
        url="http://example.com/new",
        final_url="http://example.com/old",
    )
    visited = {"http://example.com/old"}
    with pytest.raises(CharlotteRedirectError, match="loop"):
        await fetcher.fetch("http://example.com/new", visited_urls=visited)


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


@respx.mock
async def test_trailing_slash_canonical_redirect_not_loop():
    """Server-side /path → /path/ redirect must not be detected as a loop."""
    respx.get(f"{_BASE}/docs").mock(
        return_value=httpx.Response(301, headers={"location": f"{_BASE}/docs/"})
    )
    respx.get(f"{_BASE}/docs/").mock(
        return_value=httpx.Response(200, text="<html><body>docs</body></html>")
    )

    fetcher = _fetcher(allowed_domains={"example.com"})
    result = await fetcher.fetch(f"{_BASE}/docs", visited_urls=set())
    assert result.url == f"{_BASE}/docs/"
    assert result.status_code == 200


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
    # chain_seen is seeded from the raw input URL, so the only normalize_url
    # call is for the redirect destination — that call is the one that raises.
    respx.get(f"{_BASE}/page").mock(
        return_value=httpx.Response(302, headers={"location": f"{_BASE}/dest"})
    )
    with patch("charlotte.core.fetcher.normalize_url", side_effect=[
        CharlotteConfigError("bad"), # redirect destination normalize — raises
    ]):
        with pytest.raises(CharlotteRedirectError, match="not a valid URL"):
            await _fetcher().fetch(f"{_BASE}/page", visited_urls=set())


# ---------------------------------------------------------------------------
# H2: Cross-domain redirect triggers robots_handler.check()
# ---------------------------------------------------------------------------

@respx.mock
async def test_cross_domain_redirect_calls_robots_handler():
    """H2: A redirect that crosses host boundaries calls robots_handler.check() on the destination."""
    respx.get(f"{_BASE}/page").mock(
        return_value=httpx.Response(301, headers={"location": "http://www.example.com/page"})
    )
    respx.get("http://www.example.com/page").mock(
        return_value=httpx.Response(200, text="<html><body>ok</body></html>")
    )

    check_calls: list[tuple[str, float]] = []

    class _MockRobots:
        async def check(self, url: str, default_delay: float) -> float:
            check_calls.append((url, default_delay))
            return default_delay

    fetcher = PageFetcher(allowed_domains={"example.com", "www.example.com"}, polite_delay=0.0)
    await fetcher.fetch(
        f"{_BASE}/page",
        visited_urls=set(),
        robots_handler=_MockRobots(),
        default_delay=0.5,
    )

    # Exactly one cross-domain check, for the www destination, with the correct delay
    assert len(check_calls) == 1
    assert check_calls[0] == ("http://www.example.com/page", 0.5)


@respx.mock
async def test_same_domain_redirect_skips_robots_handler():
    """H2: A redirect staying on the same host does NOT call robots_handler.check()."""
    respx.get(f"{_BASE}/a").mock(
        return_value=httpx.Response(301, headers={"location": f"{_BASE}/b"})
    )
    respx.get(f"{_BASE}/b").mock(return_value=httpx.Response(200, text="<html><body>ok</body></html>"))

    check_calls: list[str] = []

    class _MockRobots:
        async def check(self, url: str, default_delay: float) -> float:
            check_calls.append(url)
            return default_delay

    await _fetcher().fetch(f"{_BASE}/a", visited_urls=set(), robots_handler=_MockRobots())

    assert check_calls == []


@respx.mock
async def test_cross_domain_redirect_robots_blocked_raises():
    """H2: robots_handler.check() rejecting the redirect destination raises RobotsError."""
    respx.get(f"{_BASE}/page").mock(
        return_value=httpx.Response(301, headers={"location": "http://www.example.com/page"})
    )

    class _BlockingRobots:
        async def check(self, url: str, default_delay: float) -> float:
            raise RobotsError(f"robots.txt disallows {url}")

    fetcher = PageFetcher(allowed_domains={"example.com", "www.example.com"}, polite_delay=0.0)
    with pytest.raises(RobotsError, match="robots.txt disallows"):
        await fetcher.fetch(
            f"{_BASE}/page",
            visited_urls=set(),
            robots_handler=_BlockingRobots(),
            default_delay=0.0,
        )
