"""
Unit tests for RobotsHandler (CHAR-012, spec §11, §11.1).

Covers T-06, T-07, and T-08 from the test matrix, plus full edge-case
coverage: user-agent matching, crawl-delay, caching, HTTP failures,
decoding failures, and boundary conditions.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from charlotte.core.robots import RobotsHandler
from charlotte.exceptions import CharlotteInternalError, RobotsError

_BASE = "http://example.com"
_ROBOTS = f"{_BASE}/robots.txt"
_PAGE = f"{_BASE}/page"
_DEFAULT_DELAY = 1.0


def _handler(**kwargs) -> RobotsHandler:
    return RobotsHandler(**kwargs)


def _robots_response(content: str) -> httpx.Response:
    return httpx.Response(200, text=content)


# ---------------------------------------------------------------------------
# Constructor defaults
# ---------------------------------------------------------------------------

def test_default_connect_timeout():
    h = _handler()
    assert h._connect_timeout == 10.0


def test_default_read_timeout():
    h = _handler()
    assert h._read_timeout == 10.0


def test_custom_timeouts():
    h = _handler(connect_timeout=5.0, read_timeout=3.0)
    assert h._connect_timeout == 5.0
    assert h._read_timeout == 3.0


# ---------------------------------------------------------------------------
# T-07 — 404 → no restrictions, crawl proceeds
# ---------------------------------------------------------------------------

@respx.mock
async def test_t07_404_treated_as_no_restrictions():
    """T-07: robots.txt 404 → treated as no restrictions, returns default delay."""
    respx.get(_ROBOTS).mock(return_value=httpx.Response(404))
    delay = await _handler().check(_PAGE, _DEFAULT_DELAY)
    assert delay == _DEFAULT_DELAY


@respx.mock
async def test_t07_404_allows_any_path():
    """T-07: 404 means all paths are allowed."""
    respx.get(_ROBOTS).mock(return_value=httpx.Response(404))
    delay = await _handler().check(f"{_BASE}/admin/secret", _DEFAULT_DELAY)
    assert delay == _DEFAULT_DELAY


# ---------------------------------------------------------------------------
# T-08 — Timeout → RobotsError (uncrawlable)
# ---------------------------------------------------------------------------

@respx.mock
async def test_t08_timeout_raises_robots_error():
    """T-08: robots.txt timeout → RobotsError."""
    respx.get(_ROBOTS).mock(side_effect=httpx.TimeoutException("timed out"))
    with pytest.raises(RobotsError, match="timeout"):
        await _handler().check(_PAGE, _DEFAULT_DELAY)


@respx.mock
async def test_t08_connect_timeout_raises_robots_error():
    """T-08: connect timeout specifically → RobotsError."""
    respx.get(_ROBOTS).mock(side_effect=httpx.ConnectTimeout("connect timed out"))
    with pytest.raises(RobotsError, match="timeout"):
        await _handler().check(_PAGE, _DEFAULT_DELAY)


# ---------------------------------------------------------------------------
# T-06 — robots.txt disallows crawl → RobotsError
# ---------------------------------------------------------------------------

@respx.mock
async def test_t06_disallow_all_raises_robots_error():
    """T-06: Disallow: / blocks the crawl with RobotsError."""
    content = "User-agent: *\nDisallow: /"
    respx.get(_ROBOTS).mock(return_value=_robots_response(content))
    with pytest.raises(RobotsError):
        await _handler().check(_PAGE, _DEFAULT_DELAY)


@respx.mock
async def test_t06_disallow_specific_path_raises_robots_error():
    """T-06: Disallow: /page blocks that specific path."""
    content = "User-agent: *\nDisallow: /page"
    respx.get(_ROBOTS).mock(return_value=_robots_response(content))
    with pytest.raises(RobotsError):
        await _handler().check(_PAGE, _DEFAULT_DELAY)


# ---------------------------------------------------------------------------
# HTTP error responses
# ---------------------------------------------------------------------------

@respx.mock
async def test_http_500_raises_robots_error():
    """Non-200, non-404 response → RobotsError mentioning the status code."""
    respx.get(_ROBOTS).mock(return_value=httpx.Response(500))
    with pytest.raises(RobotsError, match="500"):
        await _handler().check(_PAGE, _DEFAULT_DELAY)


@respx.mock
async def test_http_403_allows_crawl():
    """403 on robots.txt → no restrictions (RFC 9309 §2.3.1); crawl proceeds."""
    respx.get(_ROBOTS).mock(return_value=httpx.Response(403))
    delay = await _handler().check(_PAGE, _DEFAULT_DELAY)
    assert delay == _DEFAULT_DELAY


@respx.mock
async def test_http_429_raises_robots_error():
    """429 on robots.txt → RobotsError; 429 is the one 4xx that blocks (rate-limited)."""
    respx.get(_ROBOTS).mock(return_value=httpx.Response(429))
    with pytest.raises(RobotsError, match="429"):
        await _handler().check(_PAGE, _DEFAULT_DELAY)


@respx.mock
async def test_connection_error_raises_robots_error():
    """Connection error on robots.txt → RobotsError."""
    respx.get(_ROBOTS).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(RobotsError):
        await _handler().check(_PAGE, _DEFAULT_DELAY)


# ---------------------------------------------------------------------------
# User-agent matching — CareNavigator and * sections
# ---------------------------------------------------------------------------

@respx.mock
async def test_charlotte_crawler_section_disallows():
    """charlotte-crawler-specific Disallow blocks the crawl."""
    content = (
        "User-agent: charlotte-crawler\n"
        "Disallow: /\n"
        "\n"
        "User-agent: *\n"
        "Allow: /\n"
    )
    respx.get(_ROBOTS).mock(return_value=_robots_response(content))
    with pytest.raises(RobotsError):
        await _handler().check(_PAGE, _DEFAULT_DELAY)


@respx.mock
async def test_charlotte_crawler_section_allows_when_wildcard_disallows():
    """charlotte-crawler Allow takes precedence over a wildcard Disallow."""
    content = (
        "User-agent: charlotte-crawler\n"
        "Allow: /\n"
        "\n"
        "User-agent: *\n"
        "Disallow: /\n"
    )
    respx.get(_ROBOTS).mock(return_value=_robots_response(content))
    delay = await _handler().check(_PAGE, _DEFAULT_DELAY)
    assert delay == _DEFAULT_DELAY


@respx.mock
async def test_wildcard_disallows_when_no_care_navigator_section():
    """When there is no charlotte-crawler section, * rules apply."""
    content = "User-agent: *\nDisallow: /"
    respx.get(_ROBOTS).mock(return_value=_robots_response(content))
    with pytest.raises(RobotsError):
        await _handler().check(_PAGE, _DEFAULT_DELAY)


@respx.mock
async def test_wildcard_allows_when_no_care_navigator_section():
    """No charlotte-crawler section and * allows → crawl proceeds."""
    content = "User-agent: *\nDisallow: /other"
    respx.get(_ROBOTS).mock(return_value=_robots_response(content))
    delay = await _handler().check(_PAGE, _DEFAULT_DELAY)
    assert delay == _DEFAULT_DELAY


@respx.mock
async def test_no_matching_section_treats_as_allowed():
    """Neither charlotte-crawler nor * present → fully crawlable."""
    content = "User-agent: Googlebot\nDisallow: /"
    respx.get(_ROBOTS).mock(return_value=_robots_response(content))
    delay = await _handler().check(_PAGE, _DEFAULT_DELAY)
    assert delay == _DEFAULT_DELAY


@respx.mock
async def test_empty_robots_txt_is_fully_allowed():
    """An empty robots.txt imposes no restrictions."""
    respx.get(_ROBOTS).mock(return_value=_robots_response(""))
    delay = await _handler().check(_PAGE, _DEFAULT_DELAY)
    assert delay == _DEFAULT_DELAY


@respx.mock
async def test_empty_disallow_means_allow_all():
    """Disallow: (empty value) means allow all for that agent."""
    content = "User-agent: *\nDisallow: "
    respx.get(_ROBOTS).mock(return_value=_robots_response(content))
    delay = await _handler().check(_PAGE, _DEFAULT_DELAY)
    assert delay == _DEFAULT_DELAY


# ---------------------------------------------------------------------------
# Crawl-delay
# ---------------------------------------------------------------------------

@respx.mock
async def test_crawl_delay_larger_than_default_is_returned():
    """Crawl-delay from robots.txt overrides default when larger."""
    content = "User-agent: *\nCrawl-delay: 5"
    respx.get(_ROBOTS).mock(return_value=_robots_response(content))
    delay = await _handler().check(_PAGE, default_delay=1.0)
    assert delay == 5.0


@respx.mock
async def test_default_delay_larger_than_crawl_delay_is_returned():
    """Default delay is used when it is larger than the robots.txt directive."""
    content = "User-agent: *\nCrawl-delay: 0.5"
    respx.get(_ROBOTS).mock(return_value=_robots_response(content))
    delay = await _handler().check(_PAGE, default_delay=2.0)
    assert delay == 2.0


@respx.mock
async def test_charlotte_crawler_crawl_delay_preferred_over_wildcard():
    """charlotte-crawler crawl-delay takes priority over * crawl-delay."""
    content = (
        "User-agent: charlotte-crawler\n"
        "Crawl-delay: 3\n"
        "\n"
        "User-agent: *\n"
        "Crawl-delay: 10\n"
    )
    respx.get(_ROBOTS).mock(return_value=_robots_response(content))
    delay = await _handler().check(_PAGE, default_delay=1.0)
    assert delay == 3.0


@respx.mock
async def test_wildcard_crawl_delay_used_when_no_care_navigator_delay():
    """Falls back to * crawl-delay when CareNavigator has none."""
    content = (
        "User-agent: charlotte-crawler\n"
        "Allow: /\n"
        "\n"
        "User-agent: *\n"
        "Crawl-delay: 4\n"
    )
    respx.get(_ROBOTS).mock(return_value=_robots_response(content))
    delay = await _handler().check(_PAGE, default_delay=1.0)
    assert delay == 4.0


@respx.mock
async def test_no_crawl_delay_returns_default():
    """When robots.txt has no Crawl-delay, default_delay is returned."""
    content = "User-agent: *\nDisallow: /other"
    respx.get(_ROBOTS).mock(return_value=_robots_response(content))
    delay = await _handler().check(_PAGE, default_delay=1.5)
    assert delay == 1.5


# ---------------------------------------------------------------------------
# Per-domain caching
# ---------------------------------------------------------------------------

@respx.mock
async def test_robots_fetched_only_once_per_domain():
    """robots.txt is fetched exactly once per domain per handler instance."""
    route = respx.get(_ROBOTS).mock(return_value=httpx.Response(404))
    handler = _handler()
    await handler.check(_PAGE, _DEFAULT_DELAY)
    await handler.check(f"{_BASE}/other", _DEFAULT_DELAY)
    await handler.check(f"{_BASE}/another", _DEFAULT_DELAY)
    assert route.call_count == 1


@respx.mock
async def test_different_domains_each_fetched_once():
    """Each domain's robots.txt is fetched independently."""
    route_a = respx.get("http://example.com/robots.txt").mock(
        return_value=httpx.Response(404)
    )
    route_b = respx.get("http://other.com/robots.txt").mock(
        return_value=httpx.Response(404)
    )
    handler = _handler()
    await handler.check("http://example.com/page", _DEFAULT_DELAY)
    await handler.check("http://other.com/page", _DEFAULT_DELAY)
    assert route_a.call_count == 1
    assert route_b.call_count == 1


@respx.mock
async def test_cached_error_is_reused():
    """A blocked (error) domain stays blocked for subsequent calls."""
    route = respx.get(_ROBOTS).mock(side_effect=httpx.ConnectError("refused"))
    handler = _handler()
    with pytest.raises(RobotsError):
        await handler.check(_PAGE, _DEFAULT_DELAY)
    with pytest.raises(RobotsError):
        await handler.check(f"{_BASE}/other", _DEFAULT_DELAY)
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# Hostname normalisation
# ---------------------------------------------------------------------------

@respx.mock
async def test_hostname_treated_case_insensitively():
    """Upper-case and lower-case hostnames share one cache entry."""
    route = respx.get("http://example.com/robots.txt").mock(
        return_value=httpx.Response(404)
    )
    handler = _handler()
    await handler.check("http://EXAMPLE.COM/page", _DEFAULT_DELAY)
    await handler.check("http://example.com/page", _DEFAULT_DELAY)
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# Error message content
# ---------------------------------------------------------------------------

@respx.mock
async def test_timeout_error_message_names_hostname():
    """RobotsError from a timeout message mentions the hostname."""
    respx.get(_ROBOTS).mock(side_effect=httpx.TimeoutException("timed out"))
    with pytest.raises(RobotsError, match="example.com"):
        await _handler().check(_PAGE, _DEFAULT_DELAY)


@respx.mock
async def test_http_error_message_includes_status():
    """RobotsError from a non-200 response includes the status code."""
    respx.get(_ROBOTS).mock(return_value=httpx.Response(503))
    with pytest.raises(RobotsError, match="503"):
        await _handler().check(_PAGE, _DEFAULT_DELAY)


# ---------------------------------------------------------------------------
# Defensive branches — decoding error, parse error, internal error
# ---------------------------------------------------------------------------

@respx.mock
async def test_decoding_error_raises_robots_error(monkeypatch):
    """A DecodingError when reading the response body → RobotsError (malformed)."""
    respx.get(_ROBOTS).mock(return_value=httpx.Response(200, content=b"\xff\xfe"))
    monkeypatch.setattr(
        httpx.Response,
        "text",
        property(lambda self: (_ for _ in ()).throw(httpx.DecodingError("bad bytes"))),
    )
    with pytest.raises(RobotsError, match="decoded"):
        await _handler().check(_PAGE, _DEFAULT_DELAY)


@respx.mock
async def test_parse_exception_raises_robots_error(monkeypatch):
    """An unexpected exception from RobotFileParser → RobotsError (unparseable)."""
    from urllib.robotparser import RobotFileParser

    respx.get(_ROBOTS).mock(return_value=_robots_response("User-agent: *\nDisallow: /"))
    monkeypatch.setattr(RobotFileParser, "parse", lambda self, lines: (_ for _ in ()).throw(RuntimeError("bad")))
    with pytest.raises(RobotsError, match="parsed"):
        await _handler().check(_PAGE, _DEFAULT_DELAY)


async def test_unexpected_internal_exception_raises_internal_error(monkeypatch):
    """An unexpected exception inside _do_check is wrapped in CharlotteInternalError."""
    async def _boom(url, default_delay):
        raise ValueError("unexpected")

    handler = _handler()
    monkeypatch.setattr(handler, "_do_check", _boom)
    with pytest.raises(CharlotteInternalError):
        await handler.check(_PAGE, _DEFAULT_DELAY)
