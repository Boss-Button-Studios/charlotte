"""
Small helpers extracted from engine.py to keep that file under the 600-line cap.

These are private to the engine layer — not public API.
"""

from __future__ import annotations

import logging
import re
from time import monotonic
from typing import TYPE_CHECKING

from charlotte.core.provenance import check_provenance
from charlotte.exceptions import CharlotteError, CharlotteInternalError
from charlotte.models import (
    CrawlResult,
    LinksRanked,
    RankedLink,
    ResultContentMetadata,
    VerificationResult,
)

if TYPE_CHECKING:
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
