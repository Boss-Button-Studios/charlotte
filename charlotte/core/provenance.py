"""
URL provenance check for Charlotte (spec §9.4).

The final integrity gate before any URL is promoted to trusted result data.
Applied after the plausibility check, before the engine enqueues links or
records a result.

Two checks are applied using normalized URL comparison (spec §9.5):

  result_url     — when found=True, the result_url must appear in the extracted
                   link list after normalization. Failure is a hard rejection:
                   the result is discarded, the page is treated as found=False,
                   and the failure detail is logged. No retry.

  links_to_follow — every URL is cross-checked against the extracted link list.
                   URLs not present in the extracted list are silently dropped;
                   the remaining URLs are returned for enqueuing.

A URL the model did not observe is a URL Charlotte will not touch.

Public function: check_provenance(...) -> ProvenanceResult
Public type:     ProvenanceResult
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from charlotte.core.normalizer import normalize_url
from charlotte.exceptions import CharlotteConfigError, CharlotteInternalError


@dataclass
class ProvenanceResult:
    """Outcome of the URL provenance check for one page evaluation.

    Attributes:
        result_url_accepted: True if result_url passed the check (or was not
            applicable — found=False or result_url=None). False means the
            model's claimed result URL was not present in the extracted link
            list and has been hard-rejected.
        links_to_follow: Model-recommended URLs filtered to only those that
            appear in the extracted link list. May be empty.
        rejection_detail: Human-readable log detail when result_url is
            rejected. None when result_url_accepted=True. Hostname only —
            never a full URL, to avoid leaking query-string tokens.
    """

    result_url_accepted: bool
    links_to_follow: list[str] = field(default_factory=list)
    rejection_detail: str | None = None


def check_provenance(
    found: bool,
    result_url: str | None,
    links_to_follow: list[str],
    extracted_urls: list[str],
) -> ProvenanceResult:
    """Verify that model-output URLs were actually observed on the current page.

    Builds a normalized set from extracted_urls and checks each model URL
    against it. Normalization is applied to both sides before comparison, so
    trivially equivalent URLs (trailing slash, fragment, query order) match
    correctly.

    result_url is only checked when found=True and result_url is not None.
    A rejection is a hard failure — the caller must treat the page as
    found=False and must not follow or return the rejected URL.

    links_to_follow URLs not present in the extracted set are silently
    dropped. The returned list contains only confirmed URLs in their original
    (non-normalized) form as supplied by the model.

    Args:
        found:           The model's found flag (from validated adapter output).
        result_url:      The model's claimed result URL; None when found=False.
        links_to_follow: Model-recommended URLs to enqueue, best first.
        extracted_urls:  URLs from the content extractor for the current page.
                         Malformed entries are silently skipped.

    Returns:
        ProvenanceResult with acceptance status, filtered link list, and
        optional rejection detail for logging.

    Raises:
        CharlotteInternalError: Unexpected internal failure during the check.
    """
    try:
        extracted_norm = _build_normalized_set(extracted_urls)

        result_url_accepted, rejection_detail = _check_result_url(
            found, result_url, extracted_norm
        )

        filtered_links = _filter_links(links_to_follow, extracted_norm)

        return ProvenanceResult(
            result_url_accepted=result_url_accepted,
            links_to_follow=filtered_links,
            rejection_detail=rejection_detail,
        )

    except CharlotteInternalError:
        raise
    except Exception as exc:
        raise CharlotteInternalError(
            "Provenance check failed unexpectedly — please report this at "
            "https://github.com/Boss-Button-Studios/charlotte/issues: "
            f"{exc}"
        ) from exc


def _build_normalized_set(extracted_urls: list[str]) -> set[str]:
    """Return a set of normalized forms of all valid extracted URLs."""
    normalized: set[str] = set()
    for url in extracted_urls:
        try:
            normalized.add(normalize_url(url))
        except CharlotteConfigError:
            continue
    return normalized


def _check_result_url(
    found: bool,
    result_url: str | None,
    extracted_norm: set[str],
) -> tuple[bool, str | None]:
    """Return (accepted, rejection_detail) for the result_url check."""
    if not found or result_url is None:
        return True, None

    try:
        norm = normalize_url(result_url)
    except CharlotteConfigError:
        return False, "result_url could not be normalized (malformed URL)"

    if norm not in extracted_norm:
        # Log hostname only — full URL may carry query-string tokens.
        hostname = urlsplit(result_url).hostname or result_url
        return False, (
            f"result_url not found in extracted link list "
            f"(host: {hostname}) — possible hallucination or injection"
        )

    return True, None


def _filter_links(
    links_to_follow: list[str],
    extracted_norm: set[str],
) -> list[str]:
    """Return only the links_to_follow URLs present in the extracted set."""
    confirmed: list[str] = []
    for url in links_to_follow:
        try:
            if normalize_url(url) in extracted_norm:
                confirmed.append(url)
        except CharlotteConfigError:
            continue
    return confirmed
