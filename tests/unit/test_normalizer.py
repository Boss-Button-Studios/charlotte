"""
Unit tests for the URL normalizer (CHAR-003).

Covers T-13 (fragment deduplication) and T-14 (query parameter ordering) from
the test matrix, plus edge cases for each of the eight normalization rules.
"""

from unittest.mock import patch

import pytest

from charlotte.core.normalizer import _normalize_path, normalize_url
from charlotte.exceptions import CharlotteConfigError


# ---------------------------------------------------------------------------
# Rule 1: Lowercase scheme and host
# ---------------------------------------------------------------------------

def test_uppercase_scheme_lowercased():
    assert normalize_url("HTTP://example.com/") == "http://example.com/"


def test_uppercase_host_lowercased():
    assert normalize_url("http://EXAMPLE.COM/") == "http://example.com/"


def test_mixed_case_scheme_and_host():
    assert normalize_url("HTTPS://Example.COM/page") == "https://example.com/page"


def test_path_case_preserved():
    # Only scheme and host are lowercased — path case is untouched
    assert normalize_url("http://example.com/CaseSensitivePath") == "http://example.com/CaseSensitivePath"


# ---------------------------------------------------------------------------
# Rule 2: Remove default ports
# ---------------------------------------------------------------------------

def test_http_port_80_removed():
    assert normalize_url("http://example.com:80/") == "http://example.com/"


def test_https_port_443_removed():
    assert normalize_url("https://example.com:443/") == "https://example.com/"


def test_ftp_port_21_removed():
    assert normalize_url("ftp://example.com:21/file") == "ftp://example.com/file"


def test_non_default_http_port_preserved():
    assert normalize_url("http://example.com:8080/") == "http://example.com:8080/"


def test_non_default_https_port_preserved():
    assert normalize_url("https://example.com:8443/") == "https://example.com:8443/"


def test_https_port_80_preserved():
    # Port 80 is only default for http, not https
    assert normalize_url("https://example.com:80/") == "https://example.com:80/"


# ---------------------------------------------------------------------------
# Rule 3: Resolve relative URLs
# ---------------------------------------------------------------------------

def test_relative_path_resolved():
    assert normalize_url("page.html", "http://example.com/dir/") == "http://example.com/dir/page.html"


def test_relative_path_traversal_resolved():
    assert normalize_url("../other.html", "http://example.com/dir/sub/") == "http://example.com/dir/other.html"


def test_absolute_path_resolved_against_base_host():
    assert normalize_url("/absolute", "http://example.com/dir/page.html") == "http://example.com/absolute"


def test_absolute_url_unaffected_by_base():
    assert normalize_url("http://other.com/page", "http://example.com/") == "http://other.com/page"


def test_relative_url_without_base_raises():
    with pytest.raises(CharlotteConfigError, match="no scheme"):
        normalize_url("relative/path")


def test_empty_url_raises():
    with pytest.raises(CharlotteConfigError):
        normalize_url("")


def test_query_only_relative_resolved():
    assert normalize_url("?q=1", "http://example.com/page") == "http://example.com/page?q=1"


# ---------------------------------------------------------------------------
# Rule 4: Decode percent-encoded unreserved characters
# ---------------------------------------------------------------------------

def test_uppercase_letter_decoded():
    # %41 == 'A' — unreserved, must be decoded
    assert normalize_url("http://example.com/%41page") == "http://example.com/Apage"


def test_lowercase_letter_decoded():
    # %61 == 'a'
    assert normalize_url("http://example.com/%61page") == "http://example.com/apage"


def test_digit_decoded():
    # %31 == '1'
    assert normalize_url("http://example.com/%31page") == "http://example.com/1page"


def test_unreserved_hyphen_decoded():
    # %2D == '-'
    assert normalize_url("http://example.com/%2Dpath") == "http://example.com/-path"


def test_unreserved_tilde_decoded():
    # %7E == '~'
    assert normalize_url("http://example.com/%7Epath") == "http://example.com/~path"


def test_space_not_decoded():
    # %20 == ' ' — not an unreserved character, must stay encoded
    result = normalize_url("http://example.com/my%20page")
    assert "%" in result  # still percent-encoded


def test_slash_not_decoded():
    # %2F == '/' — reserved; decoding would create a false path separator
    result = normalize_url("http://example.com/path%2Fembedded")
    assert "%2F" in result or "%2f" in result


def test_non_ascii_not_decoded():
    # %C3%A9 == 'é' (UTF-8) — non-ASCII, must stay encoded
    result = normalize_url("http://example.com/caf%C3%A9")
    assert "%C3%A9" in result or "%c3%a9" in result


# ---------------------------------------------------------------------------
# Rule 5: Remove URL fragments — T-13
# ---------------------------------------------------------------------------

def test_fragment_stripped():
    assert normalize_url("http://example.com/page#section") == "http://example.com/page"


def test_empty_fragment_stripped():
    assert normalize_url("http://example.com/page#") == "http://example.com/page"


def test_fragment_url_equals_no_fragment_url():
    # T-13: both must normalize to the same string
    with_frag = normalize_url("http://example.com/page#section-1")
    without_frag = normalize_url("http://example.com/page")
    assert with_frag == without_frag


def test_different_fragments_same_page():
    # Two different fragments on the same page → same normalized URL
    assert (
        normalize_url("http://example.com/page#intro")
        == normalize_url("http://example.com/page#conclusion")
    )


# ---------------------------------------------------------------------------
# Rule 6: Normalize path separators
# ---------------------------------------------------------------------------

def test_double_slash_collapsed():
    assert normalize_url("http://example.com//a//b") == "http://example.com/a/b"


def test_dot_segment_resolved():
    assert normalize_url("http://example.com/a/./b") == "http://example.com/a/b"


def test_dot_dot_segment_resolved():
    assert normalize_url("http://example.com/a/b/../c") == "http://example.com/a/c"


def test_multiple_dot_dot_segments():
    assert normalize_url("http://example.com/a/b/c/../../d") == "http://example.com/a/d"


def test_leading_double_slash_collapsed():
    # POSIX preserves // at the start; Charlotte collapses it
    assert normalize_url("http://example.com//leading") == "http://example.com/leading"


def test_root_path_preserved():
    assert normalize_url("http://example.com/") == "http://example.com/"


def test_no_path_gets_root_slash():
    assert normalize_url("http://example.com") == "http://example.com/"


# ---------------------------------------------------------------------------
# Rule 7: Sort query parameters — T-14
# ---------------------------------------------------------------------------

def test_query_params_sorted_alphabetically():
    assert normalize_url("http://example.com/?b=2&a=1") == "http://example.com/?a=1&b=2"


def test_already_sorted_query_unchanged():
    assert normalize_url("http://example.com/?a=1&b=2") == "http://example.com/?a=1&b=2"


def test_reversed_param_order_equals_sorted():
    # T-14: both orderings must normalize to the same string
    order_1 = normalize_url("http://example.com/?b=2&a=1")
    order_2 = normalize_url("http://example.com/?a=1&b=2")
    assert order_1 == order_2


def test_multiple_params_sorted():
    assert normalize_url("http://example.com/?z=3&a=1&m=2") == "http://example.com/?a=1&m=2&z=3"


def test_no_query_string_unchanged():
    assert normalize_url("http://example.com/page") == "http://example.com/page"


def test_empty_query_string_dropped():
    # A bare ? with no params should not appear in the output
    result = normalize_url("http://example.com/page?")
    assert result == "http://example.com/page"


def test_duplicate_keys_stable_sort():
    # Duplicate keys are sorted by key; relative order within each key is preserved
    result = normalize_url("http://example.com/?b=2&a=1&b=3&a=4")
    assert result == "http://example.com/?a=1&a=4&b=2&b=3"


# ---------------------------------------------------------------------------
# Rule 8: Remove trailing slash from non-root paths
# ---------------------------------------------------------------------------

def test_trailing_slash_removed():
    assert normalize_url("http://example.com/page/") == "http://example.com/page"


def test_deep_path_trailing_slash_removed():
    assert normalize_url("http://example.com/a/b/c/") == "http://example.com/a/b/c"


def test_root_trailing_slash_preserved():
    assert normalize_url("http://example.com/") == "http://example.com/"


# ---------------------------------------------------------------------------
# Combined / interaction tests
# ---------------------------------------------------------------------------

def test_all_rules_combined():
    result = normalize_url("HTTP://Example.COM:80/a/../b//c/?z=3&a=1#frag")
    assert result == "http://example.com/b/c?a=1&z=3"


def test_normalization_is_idempotent():
    url = "http://example.com/path?b=2&a=1#frag"
    once = normalize_url(url)
    twice = normalize_url(once)
    assert once == twice


def test_visited_set_deduplication():
    """All these variants describe the same page and must collapse to one URL."""
    variants = [
        "HTTP://Example.COM/page?b=2&a=1#section",
        "http://example.com/page?a=1&b=2#other-section",
        "http://example.com:80/page?a=1&b=2",
        "http://example.com/page?a=1&b=2",
    ]
    normalized = {normalize_url(v) for v in variants}
    assert len(normalized) == 1, f"Expected 1 canonical URL, got {normalized}"


# ---------------------------------------------------------------------------
# Coverage for defensive branches
# ---------------------------------------------------------------------------

def test_url_with_username_and_password():
    # Exercises the username+password userinfo branch (L149 in normalizer.py)
    result = normalize_url("http://user:pass@example.com/path")
    assert result == "http://user:pass@example.com/path"


def test_urlsplit_value_error_raises_config_error():
    # Malformed IPv6 address causes urlsplit to raise ValueError
    with pytest.raises(CharlotteConfigError):
        normalize_url("http://[invalid/path")


def test_normalize_path_dot_returns_root():
    # _normalize_path(".")  — can't arise from HTTP URLs (which have a leading /)
    # but the branch must be covered as defensive code.
    assert _normalize_path(".") == "/"


def test_trailing_slash_safety_net():
    # Verifies the safety-net branch (path != "/" and path.endswith("/"))
    # by patching _normalize_path to return a path that posixpath.normpath
    # would normally have already cleaned.
    with patch("charlotte.core.normalizer._normalize_path", return_value="/page/"):
        result = normalize_url("http://example.com/page/")
    assert result == "http://example.com/page"


def test_urljoin_value_error_raises_config_error():
    with patch("charlotte.core.normalizer.urljoin", side_effect=ValueError("bad")):
        with pytest.raises(CharlotteConfigError, match="Could not resolve"):
            normalize_url("page", base_url="http://example.com/")


def test_urlunsplit_value_error_raises_config_error():
    with patch("charlotte.core.normalizer.urlunsplit", side_effect=ValueError("bad")):
        with pytest.raises(CharlotteConfigError, match="Could not reassemble"):
            normalize_url("http://example.com/")
