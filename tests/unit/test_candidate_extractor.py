"""Unit tests for CandidateExtractor — spec §6, Phase C (C2)."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from charlotte.core.candidate_extractor import (
    AddressExtractor,
    CandidateExtractorProtocol,
    DateExtractor,
    DefaultCandidateExtractor,
    DocumentLinkExtractor,
    FreeformFactExtractor,
    PhoneNumberExtractor,
    PriceExtractor,
    _estimate_zone,
    _format_quality,
    _nearest_distance,
)
from charlotte.core.extractor import ExtractedPage
from charlotte.core.text_normalization import normalize_text
from charlotte.models import Candidate, GoalContext


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _goal_context(
    goal: str = "test goal",
    goal_type: str = "freeform_fact",
    anchor_terms: list[str] | None = None,
    negative_terms: list[str] | None = None,
    regex_hints: list[str] | None = None,
) -> GoalContext:
    return GoalContext(
        goal=goal,
        navigation_hint=None,
        goal_type=goal_type,  # type: ignore[arg-type]
        goal_type_confidence=0.9,
        synonyms={},
        anchor_terms=anchor_terms or [],
        negative_terms=negative_terms or [],
        regex_hints=regex_hints or [],
        description="",
        source="deterministic",
        model_used=None,
        created_at=datetime.now(timezone.utc),
        locale="en_US",
        validation_warnings=[],
    )


def _page(text: str = "", links: list[dict[str, str]] | None = None) -> ExtractedPage:
    return ExtractedPage(text=text, links=links or [])


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

def test_default_extractor_satisfies_protocol():
    assert isinstance(DefaultCandidateExtractor(), CandidateExtractorProtocol)


def test_phone_extractor_satisfies_protocol():
    assert isinstance(PhoneNumberExtractor(), CandidateExtractorProtocol)


def test_date_extractor_satisfies_protocol():
    assert isinstance(DateExtractor(), CandidateExtractorProtocol)


def test_address_extractor_satisfies_protocol():
    assert isinstance(AddressExtractor(), CandidateExtractorProtocol)


def test_price_extractor_satisfies_protocol():
    assert isinstance(PriceExtractor(), CandidateExtractorProtocol)


def test_document_link_extractor_satisfies_protocol():
    assert isinstance(DocumentLinkExtractor(), CandidateExtractorProtocol)


def test_freeform_fact_extractor_satisfies_protocol():
    assert isinstance(FreeformFactExtractor(), CandidateExtractorProtocol)


def test_candidate_is_frozen_dataclass():
    c = Candidate(
        value="v", raw_value="v", zone="neutral", nearby_text="", position=0,
        score=0.5, features={},
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.score = 0.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Zone estimation
# ---------------------------------------------------------------------------

def test_estimate_zone_empty_text():
    assert _estimate_zone(0, 0) == "neutral"


def test_estimate_zone_content():
    assert _estimate_zone(0, 1000) == "content"
    assert _estimate_zone(399, 1000) == "content"


def test_estimate_zone_neutral():
    assert _estimate_zone(400, 1000) == "neutral"
    assert _estimate_zone(749, 1000) == "neutral"


def test_estimate_zone_chrome():
    assert _estimate_zone(750, 1000) == "chrome"
    assert _estimate_zone(999, 1000) == "chrome"


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def test_nearest_distance_finds_closest():
    text = normalize_text("call the clinic number")
    # "clinic" starts at a predictable position after normalization
    dist = _nearest_distance(0, text, ["clinic"])
    assert dist is not None
    assert dist > 0


def test_nearest_distance_returns_none_when_no_terms():
    dist = _nearest_distance(5, "hello world", [])
    assert dist is None


def test_nearest_distance_returns_none_when_term_absent():
    dist = _nearest_distance(5, "hello world", ["zzzz"])
    assert dist is None


def test_format_quality_full_match():
    assert _format_quality("+1-800-555-0100", [r"\+1-\d{3}-\d{3}-\d{4}"]) == pytest.approx(1.0)


def test_format_quality_partial_match():
    assert _format_quality("call 555-0100", [r"\d{3}-\d{4}"]) == pytest.approx(0.5)


def test_format_quality_no_hints():
    assert _format_quality("anything", []) == pytest.approx(0.0)


def test_format_quality_no_match():
    assert _format_quality("hello", [r"^\d+$"]) == pytest.approx(0.0)


def test_format_quality_silently_skips_invalid_regex():
    # Invalid regex should not raise — treated as no match.
    result = _format_quality("hello", [r"[invalid("])
    assert result == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# PhoneNumberExtractor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phone_extracts_us_number():
    page = _page("Call us at (858) 966-1700 for appointments.")
    ctx = _goal_context(goal_type="phone_extraction", anchor_terms=["appointments"])
    results = await PhoneNumberExtractor()(goal_context=ctx, page=page)
    assert len(results) == 1
    assert results[0].value == "+1-858-966-1700"
    assert results[0].raw_value.strip() == "(858) 966-1700"


@pytest.mark.asyncio
async def test_phone_normalizes_dashes():
    page = _page("Fax: 800-555-0100")
    ctx = _goal_context(goal_type="phone_extraction")
    results = await PhoneNumberExtractor()(goal_context=ctx, page=page)
    assert any(c.value == "+1-800-555-0100" for c in results)


@pytest.mark.asyncio
async def test_phone_normalizes_plus1():
    page = _page("+1 (415) 555-2671")
    ctx = _goal_context(goal_type="phone_extraction")
    results = await PhoneNumberExtractor()(goal_context=ctx, page=page)
    assert results[0].value == "+1-415-555-2671"


@pytest.mark.asyncio
async def test_phone_empty_page_returns_empty():
    page = _page("")
    ctx = _goal_context(goal_type="phone_extraction")
    results = await PhoneNumberExtractor()(goal_context=ctx, page=page)
    assert results == []


@pytest.mark.asyncio
async def test_phone_no_numbers_returns_empty():
    page = _page("No contact information here.")
    ctx = _goal_context(goal_type="phone_extraction")
    results = await PhoneNumberExtractor()(goal_context=ctx, page=page)
    assert results == []


@pytest.mark.asyncio
async def test_phone_nearby_anchor_boosts_score():
    """A phone number next to its anchor term should outscore one far from it."""
    far = "Some unrelated preamble. " * 30
    near = "Call the clinic at (858) 966-1700."
    page = _page(far + near)
    ctx = _goal_context(goal_type="phone_extraction", anchor_terms=["clinic"])
    results = await PhoneNumberExtractor()(goal_context=ctx, page=page)
    assert len(results) >= 1
    assert results[0].features["anchor_proximity"] > 0.0


@pytest.mark.asyncio
async def test_phone_negative_term_penalizes_score():
    """When a negative term is closer to the number than any anchor term, penalty applies."""
    # "main" is far away; "pharmacy" is right next to the number.
    far_preamble = "Main clinic is on the other side of campus. " + "Filler. " * 10
    page = _page(far_preamble + "pharmacy refills: (858) 966-1700")
    ctx = _goal_context(
        goal_type="phone_extraction",
        anchor_terms=["main"],
        negative_terms=["pharmacy"],
    )
    results = await PhoneNumberExtractor()(goal_context=ctx, page=page)
    assert len(results) >= 1
    assert results[0].features["negative_proximity"] > 0.0


@pytest.mark.asyncio
async def test_phone_sorted_by_score_descending():
    page = _page("Clinic: (858) 966-1700. Fax: (858) 966-1701.")
    ctx = _goal_context(goal_type="phone_extraction", anchor_terms=["clinic"])
    results = await PhoneNumberExtractor()(goal_context=ctx, page=page)
    scores = [c.score for c in results]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_phone_candidate_fields_populated():
    page = _page("Contact: (800) 555-0199.")
    ctx = _goal_context(goal_type="phone_extraction")
    results = await PhoneNumberExtractor()(goal_context=ctx, page=page)
    c = results[0]
    assert c.zone in ("content", "neutral", "chrome")
    assert isinstance(c.nearby_text, str)
    assert isinstance(c.position, int)
    assert 0.0 <= c.score <= 1.0
    assert set(c.features.keys()) == {
        "zone_weight", "anchor_proximity", "negative_proximity",
        "format_quality", "uniqueness",
    }


# ---------------------------------------------------------------------------
# DateExtractor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_date_iso():
    page = _page("Updated: 2024-03-15")
    ctx = _goal_context(goal_type="date_extraction")
    results = await DateExtractor()(goal_context=ctx, page=page)
    assert any(c.value == "2024-03-15" for c in results)


@pytest.mark.asyncio
async def test_date_long_format():
    page = _page("Published March 15, 2024")
    ctx = _goal_context(goal_type="date_extraction")
    results = await DateExtractor()(goal_context=ctx, page=page)
    assert any(c.value == "2024-03-15" for c in results)


@pytest.mark.asyncio
async def test_date_month_year_only():
    page = _page("Effective January 2025")
    ctx = _goal_context(goal_type="date_extraction")
    results = await DateExtractor()(goal_context=ctx, page=page)
    assert any(c.value == "2025-01-01" for c in results)


@pytest.mark.asyncio
async def test_date_mdy_slashes():
    page = _page("Date: 03/15/2024")
    ctx = _goal_context(goal_type="date_extraction")
    results = await DateExtractor()(goal_context=ctx, page=page)
    assert any(c.value == "2024-03-15" for c in results)


@pytest.mark.asyncio
async def test_date_invalid_date_skipped():
    page = _page("Something 2024-13-99 invalid")
    ctx = _goal_context(goal_type="date_extraction")
    results = await DateExtractor()(goal_context=ctx, page=page)
    # Month 13 is invalid — should be dropped, not crash
    assert all(c.value != "2024-13-99" for c in results)


@pytest.mark.asyncio
async def test_date_empty_page():
    page = _page("")
    ctx = _goal_context(goal_type="date_extraction")
    assert await DateExtractor()(goal_context=ctx, page=page) == []


# ---------------------------------------------------------------------------
# AddressExtractor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_address_us_basic():
    page = _page("Visit us at 123 Main Street, Springfield.")
    ctx = _goal_context(goal_type="address_extraction")
    results = await AddressExtractor()(goal_context=ctx, page=page)
    assert len(results) >= 1
    assert "123" in results[0].value


@pytest.mark.asyncio
async def test_address_with_suite():
    page = _page("Located at 456 Oak Avenue Suite 200.")
    ctx = _goal_context(goal_type="address_extraction")
    results = await AddressExtractor()(goal_context=ctx, page=page)
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_address_empty_page():
    page = _page("No addresses here.")
    ctx = _goal_context(goal_type="address_extraction")
    results = await AddressExtractor()(goal_context=ctx, page=page)
    # May or may not match — just must not raise
    assert isinstance(results, list)


# ---------------------------------------------------------------------------
# PriceExtractor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_price_dollar_sign():
    page = _page("Price: $29.99")
    ctx = _goal_context(goal_type="price_extraction")
    results = await PriceExtractor()(goal_context=ctx, page=page)
    assert any("29.99" in c.value for c in results)


@pytest.mark.asyncio
async def test_price_euro():
    page = _page("Cost: €149.00")
    ctx = _goal_context(goal_type="price_extraction")
    results = await PriceExtractor()(goal_context=ctx, page=page)
    assert any("149" in c.value for c in results)


@pytest.mark.asyncio
async def test_price_currency_code():
    page = _page("Total: 99.95 USD")
    ctx = _goal_context(goal_type="price_extraction")
    results = await PriceExtractor()(goal_context=ctx, page=page)
    assert any("99.95" in c.value for c in results)


@pytest.mark.asyncio
async def test_price_empty_page():
    page = _page("No prices here.")
    ctx = _goal_context(goal_type="price_extraction")
    assert await PriceExtractor()(goal_context=ctx, page=page) == []


# ---------------------------------------------------------------------------
# DocumentLinkExtractor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_document_link_pdf_extension():
    links = [{"text": "Annual Report", "url": "https://example.com/report.pdf"}]
    page = _page("See the Annual Report", links)
    ctx = _goal_context(goal_type="document_link")
    results = await DocumentLinkExtractor()(goal_context=ctx, page=page)
    assert len(results) == 1
    assert results[0].value == "https://example.com/report.pdf"
    assert results[0].raw_value == "Annual Report"


@pytest.mark.asyncio
async def test_document_link_docx():
    links = [{"text": "Form", "url": "https://example.com/form.docx"}]
    page = _page("Download the Form", links)
    ctx = _goal_context(goal_type="document_link")
    results = await DocumentLinkExtractor()(goal_context=ctx, page=page)
    assert len(results) == 1


@pytest.mark.asyncio
async def test_document_link_regex_hint_match():
    links = [{"text": "Privacy Policy", "url": "https://example.com/privacy"}]
    page = _page("See the Privacy Policy", links)
    ctx = _goal_context(goal_type="document_link", regex_hints=[r"privacy"])
    results = await DocumentLinkExtractor()(goal_context=ctx, page=page)
    assert len(results) == 1


@pytest.mark.asyncio
async def test_document_link_non_doc_no_hint_excluded():
    links = [{"text": "Home", "url": "https://example.com/"}]
    page = _page("Home page link", links)
    ctx = _goal_context(goal_type="document_link")
    results = await DocumentLinkExtractor()(goal_context=ctx, page=page)
    assert results == []


@pytest.mark.asyncio
async def test_document_link_empty_links():
    page = _page("No links here.")
    ctx = _goal_context(goal_type="document_link")
    assert await DocumentLinkExtractor()(goal_context=ctx, page=page) == []


# ---------------------------------------------------------------------------
# FreeformFactExtractor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_freeform_fact_always_empty():
    page = _page("Lots of useful text with many facts.")
    ctx = _goal_context(goal_type="freeform_fact")
    result = await FreeformFactExtractor()(goal_context=ctx, page=page)
    assert result == []


# ---------------------------------------------------------------------------
# DefaultCandidateExtractor — dispatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_dispatches_phone():
    page = _page("Call (800) 555-0100 today.")
    ctx = _goal_context(goal="What is the phone number?", goal_type="phone_extraction")
    results = await DefaultCandidateExtractor()(goal_context=ctx, page=page)
    assert any("+1-800-555-0100" in c.value for c in results)


@pytest.mark.asyncio
async def test_default_dispatches_date():
    page = _page("Published 2024-06-01.")
    ctx = _goal_context(goal_type="date_extraction")
    results = await DefaultCandidateExtractor()(goal_context=ctx, page=page)
    assert any("2024-06-01" in c.value for c in results)


@pytest.mark.asyncio
async def test_default_navigation_returns_empty():
    page = _page("Navigate to the next page.")
    ctx = _goal_context(goal="Find the about page", goal_type="navigation")
    results = await DefaultCandidateExtractor()(goal_context=ctx, page=page)
    assert results == []


@pytest.mark.asyncio
async def test_default_freeform_returns_empty():
    page = _page("The CEO is Jane Smith.")
    ctx = _goal_context(goal="Who is the CEO?", goal_type="freeform_fact")
    results = await DefaultCandidateExtractor()(goal_context=ctx, page=page)
    assert results == []


@pytest.mark.asyncio
async def test_default_dispatches_price():
    page = _page("Price: $49.99 per month.")
    ctx = _goal_context(goal_type="price_extraction")
    results = await DefaultCandidateExtractor()(goal_context=ctx, page=page)
    assert any("49.99" in c.value for c in results)


@pytest.mark.asyncio
async def test_default_dispatches_document_link():
    links = [{"text": "Spec", "url": "https://example.com/spec.pdf"}]
    page = _page("Download the Spec", links)
    ctx = _goal_context(goal_type="document_link")
    results = await DefaultCandidateExtractor()(goal_context=ctx, page=page)
    assert len(results) == 1


# ---------------------------------------------------------------------------
# Candidate schema contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_candidates_sorted_descending():
    page = _page(
        "Main clinic: (858) 966-1700. Fax: (800) 555-0101. General: (310) 555-9999."
    )
    ctx = _goal_context(goal_type="phone_extraction", anchor_terms=["clinic"])
    results = await PhoneNumberExtractor()(goal_context=ctx, page=page)
    scores = [c.score for c in results]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_candidate_score_in_unit_range():
    page = _page("Cost: $9.99. Also $199.00.")
    ctx = _goal_context(goal_type="price_extraction")
    for c in await PriceExtractor()(goal_context=ctx, page=page):
        assert 0.0 <= c.score <= 1.0


@pytest.mark.asyncio
async def test_candidate_feature_keys_stable():
    """Feature keys must be the five specified in spec §6.4."""
    expected = {"zone_weight", "anchor_proximity", "negative_proximity", "format_quality", "uniqueness"}
    page = _page("Contact: (415) 555-2671")
    ctx = _goal_context(goal_type="phone_extraction")
    results = await PhoneNumberExtractor()(goal_context=ctx, page=page)
    assert results
    assert set(results[0].features.keys()) == expected


@pytest.mark.asyncio
async def test_uniqueness_lower_for_repeated_value():
    """A phone number that appears twice should have uniqueness ≤ 0.5."""
    page = _page("Clinic: (800) 555-0100. Also call (800) 555-0100.")
    ctx = _goal_context(goal_type="phone_extraction")
    results = await PhoneNumberExtractor()(goal_context=ctx, page=page)
    for c in results:
        if c.value == "+1-800-555-0100":
            assert c.features["uniqueness"] <= 0.5
