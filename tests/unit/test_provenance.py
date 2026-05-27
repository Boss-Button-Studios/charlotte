"""
Unit tests for the URL provenance check (CHAR-010, spec §9.4).

Covers T-11 (hallucinated result_url rejected) and T-12 (off-list
links_to_follow silently dropped), plus normalization, edge cases,
and the exception boundary.
"""

import pytest

from charlotte.core.provenance import ProvenanceResult, check_provenance
from charlotte.exceptions import CharlotteInternalError

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_EXTRACTED = [
    "https://example.com/about",
    "https://example.com/contact",
    "https://example.com/products",
]


# ---------------------------------------------------------------------------
# ProvenanceResult dataclass
# ---------------------------------------------------------------------------

def test_provenance_result_defaults():
    """ProvenanceResult defaults: empty links and no rejection detail."""
    r = ProvenanceResult(result_url_accepted=True)
    assert r.links_to_follow == []
    assert r.rejection_detail is None


def test_provenance_result_stores_all_fields():
    """ProvenanceResult stores all three fields correctly."""
    r = ProvenanceResult(
        result_url_accepted=False,
        links_to_follow=["https://example.com/a"],
        rejection_detail="not found",
    )
    assert r.result_url_accepted is False
    assert r.links_to_follow == ["https://example.com/a"]
    assert r.rejection_detail == "not found"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_result_url_in_extracted_accepted():
    """result_url present in extracted list passes the provenance check."""
    result = check_provenance(
        found=True,
        result_url="https://example.com/about",
        links_to_follow=[],
        extracted_urls=_EXTRACTED,
    )
    assert result.result_url_accepted is True
    assert result.rejection_detail is None


def test_links_to_follow_in_extracted_kept():
    """links_to_follow URLs present in extracted list are returned unchanged."""
    result = check_provenance(
        found=False,
        result_url=None,
        links_to_follow=["https://example.com/about", "https://example.com/contact"],
        extracted_urls=_EXTRACTED,
    )
    assert "https://example.com/about" in result.links_to_follow
    assert "https://example.com/contact" in result.links_to_follow


def test_returns_provenance_result_instance():
    """check_provenance always returns a ProvenanceResult."""
    result = check_provenance(False, None, [], _EXTRACTED)
    assert isinstance(result, ProvenanceResult)


# ---------------------------------------------------------------------------
# T-11 — hallucinated result_url rejected
# ---------------------------------------------------------------------------

def test_hallucinated_result_url_rejected(t11=True):
    """T-11: result_url not in extracted list is hard-rejected."""
    result = check_provenance(
        found=True,
        result_url="https://example.com/hallucinated-page",
        links_to_follow=[],
        extracted_urls=_EXTRACTED,
    )
    assert result.result_url_accepted is False


def test_rejected_result_url_has_rejection_detail():
    """A rejected result_url produces a non-empty rejection_detail for logging."""
    result = check_provenance(
        found=True,
        result_url="https://example.com/hallucinated-page",
        links_to_follow=[],
        extracted_urls=_EXTRACTED,
    )
    assert result.rejection_detail is not None
    assert len(result.rejection_detail) > 0


def test_rejection_detail_contains_hostname_not_full_url():
    """Rejection detail logs the hostname only — not the full URL with query string."""
    result = check_provenance(
        found=True,
        result_url="https://example.com/page?token=secret123",
        links_to_follow=[],
        extracted_urls=[],
    )
    assert result.result_url_accepted is False
    assert "secret123" not in result.rejection_detail
    assert "example.com" in result.rejection_detail


def test_found_false_result_url_not_checked():
    """When found=False, result_url is not checked — any value is accepted."""
    result = check_provenance(
        found=False,
        result_url="https://not-in-extracted.com/page",
        links_to_follow=[],
        extracted_urls=_EXTRACTED,
    )
    assert result.result_url_accepted is True
    assert result.rejection_detail is None


def test_found_true_result_url_none_not_checked():
    """When found=True but result_url=None, the check is skipped."""
    result = check_provenance(
        found=True,
        result_url=None,
        links_to_follow=[],
        extracted_urls=_EXTRACTED,
    )
    assert result.result_url_accepted is True


def test_malformed_result_url_rejected():
    """A result_url that cannot be normalized is hard-rejected."""
    result = check_provenance(
        found=True,
        result_url="not-a-valid-url-at-all",
        links_to_follow=[],
        extracted_urls=_EXTRACTED,
    )
    assert result.result_url_accepted is False
    assert result.rejection_detail is not None


# ---------------------------------------------------------------------------
# T-12 — off-list links_to_follow silently dropped
# ---------------------------------------------------------------------------

def test_off_list_link_silently_dropped(t12=True):
    """T-12: links_to_follow URL not in extracted list is silently dropped."""
    result = check_provenance(
        found=False,
        result_url=None,
        links_to_follow=["https://example.com/not-on-page"],
        extracted_urls=_EXTRACTED,
    )
    assert result.links_to_follow == []


def test_mixed_links_only_extracted_kept():
    """A mix of extracted and non-extracted links — only extracted ones returned."""
    result = check_provenance(
        found=False,
        result_url=None,
        links_to_follow=[
            "https://example.com/about",        # in extracted
            "https://example.com/hallucinated",  # not in extracted
            "https://example.com/contact",       # in extracted
        ],
        extracted_urls=_EXTRACTED,
    )
    assert "https://example.com/about" in result.links_to_follow
    assert "https://example.com/contact" in result.links_to_follow
    assert "https://example.com/hallucinated" not in result.links_to_follow


def test_malformed_link_in_links_to_follow_silently_dropped():
    """A malformed URL in links_to_follow is silently dropped."""
    result = check_provenance(
        found=False,
        result_url=None,
        links_to_follow=["not-a-url", "https://example.com/about"],
        extracted_urls=_EXTRACTED,
    )
    assert "not-a-url" not in result.links_to_follow
    assert "https://example.com/about" in result.links_to_follow


def test_empty_links_to_follow_returns_empty():
    """An empty links_to_follow list returns an empty filtered list."""
    result = check_provenance(False, None, [], _EXTRACTED)
    assert result.links_to_follow == []


def test_all_links_in_extracted_all_kept():
    """When all links_to_follow are in the extracted list, all are returned."""
    result = check_provenance(
        found=False,
        result_url=None,
        links_to_follow=_EXTRACTED[:2],
        extracted_urls=_EXTRACTED,
    )
    assert len(result.links_to_follow) == 2


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def test_normalization_trailing_slash_matches():
    """Trailing-slash variant of an extracted URL passes the provenance check."""
    result = check_provenance(
        found=True,
        result_url="https://example.com/about/",   # trailing slash
        links_to_follow=[],
        extracted_urls=["https://example.com/about"],
    )
    assert result.result_url_accepted is True


def test_normalization_fragment_stripped_matches():
    """Fragment in result_url is stripped before comparison."""
    result = check_provenance(
        found=True,
        result_url="https://example.com/about#section",
        links_to_follow=[],
        extracted_urls=["https://example.com/about"],
    )
    assert result.result_url_accepted is True


def test_normalization_query_order_matches():
    """Differently ordered query params are treated as the same URL."""
    result = check_provenance(
        found=True,
        result_url="https://example.com/search?z=1&a=2",
        links_to_follow=[],
        extracted_urls=["https://example.com/search?a=2&z=1"],
    )
    assert result.result_url_accepted is True


def test_normalization_applied_to_links_to_follow():
    """Trailing-slash variant in links_to_follow matches the extracted URL."""
    result = check_provenance(
        found=False,
        result_url=None,
        links_to_follow=["https://example.com/about/"],  # trailing slash
        extracted_urls=["https://example.com/about"],
    )
    assert len(result.links_to_follow) == 1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_extracted_list_rejects_result_url():
    """With no extracted URLs, any result_url is rejected."""
    result = check_provenance(
        found=True,
        result_url="https://example.com/about",
        links_to_follow=[],
        extracted_urls=[],
    )
    assert result.result_url_accepted is False


def test_empty_extracted_list_drops_all_links():
    """With no extracted URLs, all links_to_follow are dropped."""
    result = check_provenance(
        found=False,
        result_url=None,
        links_to_follow=["https://example.com/about"],
        extracted_urls=[],
    )
    assert result.links_to_follow == []


def test_malformed_extracted_url_skipped():
    """A malformed URL in extracted_urls is silently skipped."""
    result = check_provenance(
        found=True,
        result_url="https://example.com/about",
        links_to_follow=[],
        extracted_urls=["not-a-url", "https://example.com/about"],
    )
    assert result.result_url_accepted is True


def test_order_of_filtered_links_preserved():
    """The order of links_to_follow is preserved after filtering."""
    links = [
        "https://example.com/products",
        "https://example.com/about",
        "https://example.com/contact",
    ]
    result = check_provenance(False, None, links, _EXTRACTED)
    assert result.links_to_follow == links


# ---------------------------------------------------------------------------
# Exception boundary
# ---------------------------------------------------------------------------

def test_unexpected_exception_wrapped_as_internal_error():
    """An unexpected exception inside the check is wrapped as CharlotteInternalError."""
    from unittest.mock import patch

    with patch(
        "charlotte.core.provenance._build_normalized_set",
        side_effect=RuntimeError("unexpected boom"),
    ):
        with pytest.raises(CharlotteInternalError, match="unexpectedly"):
            check_provenance(False, None, [], _EXTRACTED)


def test_inner_charlotte_internal_error_reraises_unchanged():
    """A CharlotteInternalError raised inside the check propagates as-is."""
    from unittest.mock import patch

    inner = CharlotteInternalError("inner problem")
    with patch(
        "charlotte.core.provenance._build_normalized_set", side_effect=inner
    ):
        with pytest.raises(CharlotteInternalError) as exc_info:
            check_provenance(False, None, [], _EXTRACTED)
    assert exc_info.value is inner
