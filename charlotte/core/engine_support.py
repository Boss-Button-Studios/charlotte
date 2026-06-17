"""
Small helpers extracted from engine.py to keep that file under the 600-line cap.

These are private to the engine layer — not public API.
"""

from __future__ import annotations

import logging
import re
from time import monotonic
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from charlotte.core.fetcher import _is_document_url
from charlotte.core.link_ranker import _extract_date
from charlotte.core.normalizer import normalize_url
from charlotte.core.provenance import check_provenance
from charlotte.exceptions import (
    CharlotteConfigError,
    CharlotteError,
    CharlotteInternalError,
)
from charlotte.models import (
    CrawlResult,
    LinksRanked,
    RankedLink,
    ResultContentMetadata,
    VerificationResult,
)

if TYPE_CHECKING:
    from datetime import date

    from charlotte.models import (
        FailureMode,
        GoalContext,
        ResultContent,
        VisitLogEntry,
    )

logger = logging.getLogger(__name__)


def _resolve_default_adapter():
    """Instantiate the default adapter from CharlotteConfig (spec §5.1).

    Consults CHARLOTTE_DEFAULT_ADAPTER ('local' or 'groq'). Falls back to
    LocalAdapter. Each constructor raises CharlotteConfigError with a clear
    message if its requirements are not met.
    """
    from charlotte.config import CharlotteConfig
    adapter_name = CharlotteConfig.default_adapter()
    if adapter_name == "groq":
        from charlotte.adapters.groq import GroqAdapter
        return GroqAdapter()
    from charlotte.adapters.local import LocalAdapter
    return LocalAdapter()


def _empty_result(*, budget_exhausted: bool) -> CrawlResult:
    return CrawlResult(
        found=False,
        result_urls=[],
        content=None,
        confidence=0.0,
        pages_visited=0,
        depth_reached=0,
        visit_log=[],
        best_candidate_url=None,
        budget_exhausted=budget_exhausted,
    )


def _elapsed_ms(start: float) -> int:
    return int((monotonic() - start) * 1000)


def _rank_links(ranker, goal_context: "GoalContext", links: list) -> list:
    """Call ranker, re-raising any exception as CharlotteInternalError."""
    try:
        return ranker(goal_context, links)
    except Exception as exc:
        raise CharlotteInternalError(
            f"Link ranker raised an unexpected error: {exc}. "
            "Please report this at https://github.com/Boss-Button-Studios/charlotte/issues"
        ) from exc


def _make_links_ranked(
    page_url: str,
    ranked: list[tuple[str, float]],
    url_to_link: dict,
    duration_ms: int,
) -> LinksRanked:
    """Build a LinksRanked event from raw ranker output (capped at 30 entries).

    30 matches _MAX_LINKS_IN_PROMPT in the local adapter so the event reflects
    exactly what the model sees.  Previously capped at 10, which created a log
    blind spot for links ranked 11-30.
    """
    top = [
        RankedLink(text=url_to_link.get(u, {}).get("text", ""), url=u, score=s)
        for u, s in ranked[:30]
    ]
    return LinksRanked(
        page_url=page_url,
        total_links=len(ranked),
        top_links=top,
        duration_ms=duration_ms,
    )


async def _run_extractor(extractor, goal_context: "GoalContext", page, locale: str) -> list:
    """Call the candidate extractor. CharlotteErrors are logged and return []; others propagate."""
    try:
        return await extractor(goal_context=goal_context, page=page, locale=locale)
    except CharlotteError:
        logger.exception("Candidate extractor failed (goal_type=%s, locale=%s)", goal_context.goal_type, locale)
        return []


async def _verify_candidate(
    verifier,
    url: str,
    goal_context: "GoalContext",
) -> tuple[VerificationResult, "ResultContent | None"]:
    """Call the destination verifier. CharlotteErrors are logged and return a rejection; others propagate."""
    try:
        return await verifier(url=url, goal_context=goal_context)
    except CharlotteError as exc:
        logger.exception("Destination verifier failed for %r", url)
        return (
            VerificationResult(
                url=url, passed=False, mode="existence", score=None,
                reason=f"verifier_error: {type(exc).__name__}",
            ),
            None,
        )


def _content_metadata(content: "ResultContent | None") -> "ResultContentMetadata | None":
    """Extract lightweight metadata for a ResultFound stream event."""
    if content is None:
        return None
    return ResultContentMetadata(
        content_type=content.content_type,
        content_length=content.content_length,
        suggested_filename=content.suggested_filename,
        etag=content.etag,
    )


def _build_crawl_result(
    *,
    found: bool,
    result_urls: list[str],
    answers_list: list,
    content_list: list[str],
    return_content: bool,
    visit_log: list["VisitLogEntry"],
    pages_visited: int,
    depth_reached: int,
    best_url: "str | None",
    budget_exhausted: bool,
    goal_context: "GoalContext",
    verified_candidates: list[VerificationResult],
    result_contents: list["ResultContent | None"],
    failure_mode: "FailureMode | None",
) -> CrawlResult:
    best_conf = max((e.confidence for e in visit_log if e.found), default=0.0)
    return CrawlResult(
        found=found,
        result_urls=result_urls,
        content=content_list if return_content else None,
        confidence=best_conf if found else max(
            (e.confidence for e in visit_log), default=0.0
        ),
        pages_visited=pages_visited,
        depth_reached=depth_reached,
        visit_log=visit_log,
        best_candidate_url=best_url if not found else None,
        budget_exhausted=budget_exhausted,
        answers=answers_list if found else None,
        goal_context=goal_context,
        verified_candidates=verified_candidates,
        result_contents=result_contents,
        failure_mode=failure_mode,
    )


def _check_result(output, page, extracted) -> tuple[bool, "str | None", list]:
    """Run provenance check and answer content gate.

    Returns (effective_found, effective_result_url, effective_links).
    """
    extracted_link_urls = [page.url] + [link["url"] for link in extracted.links]
    provenance_result_url = (
        page.url if (output.found and output.answer is not None) else output.result_url
    )
    prov = check_provenance(
        found=output.found,
        result_url=provenance_result_url,
        links_to_follow=output.links_to_follow,
        extracted_urls=extracted_link_urls,
    )
    effective_found = output.found and prov.result_url_accepted
    effective_result_url = provenance_result_url if effective_found else None

    if not prov.result_url_accepted and prov.rejection_detail:
        logger.debug("Provenance rejection at %r: %s", page.url, prov.rejection_detail)

    if effective_found and output.answer is not None:
        full_text = re.sub(
            r"\s+", " ", f"{extracted.title}\n{extracted.text}".strip()
        ).casefold()
        norm_answer = re.sub(r"\s+", " ", output.answer.strip()).casefold()
        # Python docs render dotted names across separate <span> tags, producing
        # "json . loads" or "functools. cache" instead of "json.loads". When the
        # answer contains a dot, use a pattern that allows optional whitespace.
        if "." in norm_answer:
            dot_flex = re.compile(re.escape(norm_answer).replace("\\.", "\\s*\\.\\s*"))
            answer_present = bool(dot_flex.search(full_text))
        else:
            answer_present = norm_answer in full_text
        if norm_answer and not answer_present:
            logger.debug(
                "Answer content gate rejected at %r: normalized answer missing "
                "from page text (answer_length=%d)",
                page.url, len(output.answer),
            )
            effective_found = False
            effective_result_url = None

    return effective_found, effective_result_url, prov.links_to_follow


def _domain_allowed(host: str, domains: frozenset[str]) -> bool:
    """True if host is in domains or is a subdomain of any domain in domains."""
    if host in domains:
        return True
    return any(host.endswith("." + d) for d in domains)


def _queue_has_unvisited(
    queue: "list[tuple[float, int, str, int]]",
    visited: "set[str]",
) -> bool:
    """True if the crawl queue holds at least one not-yet-visited URL.

    The crawl loop silently drops queue entries whose URL is already visited
    (a stale duplicate enqueued by an earlier page).  A literal ``queue`` emptiness
    check therefore overstates how much work remains: a queue containing only
    stale, already-visited entries is effectively empty — those entries become
    no-op pops.  The stranding fallback uses this to fire when the *live* queue is
    empty, not merely when the raw queue is, so a dead-ending page isn't masked by
    leftover duplicates (the St. Anne field-test failure).
    """
    for item in queue:
        try:
            if normalize_url(item[2]) not in visited:
                return True
        except CharlotteConfigError:
            continue
    return False


def _select_fallback_links(
    ranked: "list[tuple[str, float]]",
    *,
    depth: int,
    max_depth: int,
    visited: "set[str]",
    allowed_domains: frozenset[str],
    reference_date: "date | None" = None,
    limit: int = 3,
) -> "list[tuple[str, float]]":
    """Pick top-ranked eligible links to enqueue when the model strands the crawl.

    A small model sometimes returns found=False with no followable links on a
    non-terminal page (e.g. a parish homepage whose bulletin lives one hop away).
    With nothing left in the queue that dead-ends the crawl even though the
    ranker surfaced perfectly good navigation links.  This is the safety net:
    when the model contributes nothing and the queue would otherwise empty, we
    trust the BM25 ranker over the model's empty list and keep navigating.

    Returns up to ``limit`` ``(url, score)`` pairs from ``ranked`` (best-first)
    that are not yet visited, are domain-allowed, and respect ``max_depth``.
    URLs whose normalized form repeats are emitted once.  Returns an empty list
    when the next hop would exceed ``max_depth`` or nothing is eligible.

    When ``reference_date`` is set (a temporal "latest" goal), clearly-stale dated
    documents are skipped: enqueuing one would let the binary short-circuit accept
    an old bulletin as the result, the very thing the staleness guard prevents on
    the model-claim path.
    """
    if depth + 1 > max_depth:
        return []
    selected: list[tuple[str, float]] = []
    seen: set[str] = set()
    for url, score in ranked:
        try:
            norm = normalize_url(url)
        except CharlotteConfigError:
            continue
        if norm in visited or norm in seen:
            continue
        host = (urlsplit(norm).hostname or "").lower()
        if not _domain_allowed(host, allowed_domains):
            continue
        if reference_date is not None and _is_stale_dated_document(url, reference_date):
            continue
        seen.add(norm)
        selected.append((url, score))
        if len(selected) >= limit:
            break
    return selected


# Older than this (in days) is treated as "clearly not the latest" for a temporal
# "latest …" goal — about four weeks, comfortably past a normal weekly cadence so a
# legitimately recent bulletin is never downgraded.
_STALENESS_DAYS: int = 28


def _document_claim_age_days(url: str, reference_date: "date") -> "int | None":
    """Age in days of a dated document URL relative to reference_date.

    Returns None when the URL is not a document or carries no parseable date, so
    callers can distinguish "fresh enough / undated" from "old". Reuses the link
    ranker's date extraction so URL date formats stay recognised in one place.
    """
    if not _is_document_url(url):
        return None
    parsed = _extract_date("", url, reference_date)
    if parsed is None:
        return None
    return (reference_date - parsed).days


def _is_stale_dated_document(url: str, reference_date: "date") -> bool:
    """True if url is a dated document older than _STALENESS_DAYS."""
    age = _document_claim_age_days(url, reference_date)
    return age is not None and age > _STALENESS_DAYS


def _fresher_exploration_links(
    ranked: "list[tuple[str, float]]",
    *,
    stale_url: str,
    reference_date: "date",
    visited: "set[str]",
    allowed_domains: frozenset[str],
    limit: int = 3,
) -> "list[str]":
    """Goal-relevant links worth exploring instead of claiming a stale bulletin.

    Returns up to ``limit`` unvisited, domain-allowed links from ``ranked`` that
    carry positive goal relevance (BM25 score > 0) and are not themselves the
    stale claim or another clearly-stale dated document — i.e. pages that could
    lead to a newer result (an archive/"view all bulletins" link, an aggregator).
    Empty list when none qualify, which the caller treats as "no fresher path, let
    the stale claim stand".
    """
    try:
        stale_norm = normalize_url(stale_url)
    except CharlotteConfigError:
        stale_norm = None
    out: list[str] = []
    seen: set[str] = set()
    for url, score in ranked:
        if score <= 0:
            continue
        try:
            norm = normalize_url(url)
        except CharlotteConfigError:
            continue
        if norm in visited or norm in seen or norm == stale_norm:
            continue
        host = (urlsplit(norm).hostname or "").lower()
        if not _domain_allowed(host, allowed_domains):
            continue
        if _is_stale_dated_document(url, reference_date):
            continue
        seen.add(norm)
        out.append(url)
        if len(out) >= limit:
            break
    return out


def _build_binary_result(
    url: str,
    body: bytes,
    goal_context: "GoalContext",
) -> "tuple[VerificationResult, ResultContent | None]":
    """Build a verification result for binary content already fetched by the engine.

    Called when render_js=True and a document URL was fetched via Playwright's
    APIRequestContext — avoids a second httpx round-trip in the verifier, which
    would fail on servers whose bot-detection blocks plain httpx but allows
    real browser requests.
    """
    from datetime import datetime, timezone
    from urllib.parse import urlsplit

    from charlotte.models import ResultContent, VerificationResult

    if not body:
        return (
            VerificationResult(
                url=url, passed=False, mode="existence", score=None,
                reason="empty_response",
            ),
            None,
        )

    result = VerificationResult(
        url=url, passed=True, mode="existence", score=None, reason="ok_existence_binary"
    )

    if goal_context.goal_type != "document_link":
        return result, None

    path = urlsplit(url).path
    last_seg = path.rsplit("/", 1)[-1] if path else ""
    suggested_filename = last_seg if ("." in last_seg) else None

    content = ResultContent(
        content=body,
        content_type=None,
        content_length=len(body),
        suggested_filename=suggested_filename,
        etag=None,
        fetched_at=datetime.now(timezone.utc),
        file_path=None,
    )
    return result, content
