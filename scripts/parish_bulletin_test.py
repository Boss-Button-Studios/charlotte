"""
Parish bulletin retrieval — live test of G7 document_link content delivery.

Charlotte navigates to each parish website, finds the latest weekly bulletin,
and downloads it.  The downloaded file lands in the same per-parish folder as
the diagnostic event log for that run, making it easy to correlate what
Charlotte saw with what it retrieved.

All output goes under crawl_logs/bulletins/<timestamp>/:
    <slug>/events.jsonl       — per-event diagnostic log
    <slug>/<bulletin file>    — downloaded bulletin (PDF or otherwise)
    _summary.json             — pass/fail table for the whole run

Usage:
    python3 scripts/parish_bulletin_test.py
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

import charlotte
from charlotte import crawl
from charlotte.adapters.local import LocalAdapter
from charlotte.models import (
    BudgetExhausted,
    CrawlComplete,
    CrawlStarted,
    DestinationVerificationFailed,
    GoalPreprocessed,
    LinksRanked,
    ModelDecision,
    ModelEvaluating,
    PageFetched,
    PageSkipped,
    ResultFound,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BULLETINS_DIR = Path("crawl_logs") / "bulletins"
CONFIDENCE_THRESHOLD = 0.70
INTER_PARISH_DELAY = 3.0

GOAL = "Find and download the latest weekly parish bulletin PDF"
NAVIGATION_HINT = (
    "Look for a link labelled 'bulletin', 'weekly bulletin', 'parish bulletin', "
    "or 'newsletter'. It is usually a PDF file. Download it. "
    "Parish bulletins are published in advance for the coming Sunday, so the most "
    "recent bulletin may carry a date up to 7 days in the future — that is correct."
)

PARISHES = [
    # marystarlajolla.org: bulletins hosted on parishesonline.com (JS-only SPA),
    # unreachable without Playwright. Removed until JS rendering is added.
    {
        "name": "St. Anne",
        "slug": "st_anne_sd",
        "url":  "https://stannesd.com/",
    },
    {
        "name": "St. John of the Cross",
        "slug": "st_john_cross_lg",
        "url":  "https://www.sjcparishlg.org/",
    },
    {
        "name": "St. Martin of Tours",
        "slug": "st_martin_tours_tlm",
        "url":  "https://www.smtlm.org/",
    },
    {
        "name": "Our Lady of Guadalupe",
        "slug": "olg_calexico",
        "url":  "https://www.ourladyofguadalupeparish-calexico.org/",
    },
]

# ---------------------------------------------------------------------------
# Per-parish result
# ---------------------------------------------------------------------------

@dataclass
class ParishResult:
    name: str
    slug: str
    url: str
    found: bool = False
    result_url: str | None = None
    content_type: str | None = None
    content_length: int = 0
    suggested_filename: str | None = None
    file_path: Path | None = None
    pages_visited: int = 0
    elapsed_ms: int = 0
    error: str | None = None
    events: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_parish(
    parish: dict, run_dir: Path, adapter: LocalAdapter,
) -> ParishResult:
    result = ParishResult(name=parish["name"], slug=parish["slug"], url=parish["url"])
    parish_dir = run_dir / parish["slug"]
    parish_dir.mkdir(parents=True, exist_ok=True)

    run_start = monotonic()

    print(f"\n{'─' * 64}", flush=True)
    print(f"  {parish['name']:<30}  {parish['url']}", flush=True)
    print(f"{'─' * 64}", flush=True)

    try:
        gen = crawl(
            parish["url"],
            GOAL,
            navigation_hint=NAVIGATION_HINT,
            model=adapter,
            max_pages=10,
            max_depth=4,
            confidence_threshold=CONFIDENCE_THRESHOLD,
            stream=True,
            default_delay=1.5,
            result_to_file=parish_dir,
            max_result_bytes=50 * 1024 * 1024,  # 50 MB; default 10 MB too small for some bulletins
        )

        async for event in gen:
            elapsed_ms = int((monotonic() - run_start) * 1000)
            t = f"[{elapsed_ms // 1000:>3}s]"

            if isinstance(event, CrawlStarted):
                result.events.append({"type": "CrawlStarted", "elapsed_ms": elapsed_ms,
                                      "max_pages": event.max_pages, "max_depth": event.max_depth})

            elif isinstance(event, GoalPreprocessed):
                ctx = event.goal_context
                goal_type = ctx.goal_type if ctx else None
                anchors = ctx.anchor_terms if ctx else []
                print(f"  {t} goal_type={goal_type}  anchors={anchors}", flush=True)
                result.events.append({"type": "GoalPreprocessed", "elapsed_ms": elapsed_ms,
                                      "source": event.source, "goal_type": goal_type,
                                      "anchor_terms": anchors})

            elif isinstance(event, PageFetched):
                print(f"  {t} fetched  {event.url}", flush=True)
                result.events.append({"type": "PageFetched", "elapsed_ms": elapsed_ms,
                                      "url": event.url, "depth": event.depth,
                                      "http_status": event.http_status,
                                      "fetch_ms": event.fetch_ms})

            elif isinstance(event, LinksRanked):
                top = [{"url": lk.url, "text": lk.text, "score": round(lk.score, 4)}
                       for lk in event.top_links[:5]]
                result.events.append({"type": "LinksRanked", "elapsed_ms": elapsed_ms,
                                      "page_url": event.page_url,
                                      "total_links": event.total_links,
                                      "top_links": top})

            elif isinstance(event, ModelEvaluating):
                print(f"  {t} thinking...", flush=True)

            elif isinstance(event, ModelDecision):
                icon = "✓" if event.found else "·"
                print(f"  {t} {icon} conf={event.confidence:.2f}  {event.url}", flush=True)
                result.events.append({"type": "ModelDecision", "elapsed_ms": elapsed_ms,
                                      "url": event.url, "found": event.found,
                                      "confidence": event.confidence,
                                      "links_queued": event.links_queued,
                                      "reasoning": event.reasoning})

            elif isinstance(event, DestinationVerificationFailed):
                print(f"  {t} verifier ✗  {event.url}  ({event.result.reason})", flush=True)
                result.events.append({"type": "DestinationVerificationFailed",
                                      "elapsed_ms": elapsed_ms,
                                      "url": event.url, "reason": event.result.reason,
                                      "score": event.result.score})

            elif isinstance(event, ResultFound):
                meta = event.content_metadata
                if meta and meta.content_length:
                    kb = meta.content_length // 1024
                    fname = meta.suggested_filename or "(unnamed)"
                    print(f"  {t} FOUND  {event.url}", flush=True)
                    print(f"         {meta.content_type}  {kb} KB  →  {fname}", flush=True)
                    result.content_type = meta.content_type
                    result.content_length = meta.content_length
                    result.suggested_filename = meta.suggested_filename
                    if meta.suggested_filename:
                        result.file_path = parish_dir / meta.suggested_filename
                else:
                    print(f"  {t} FOUND  {event.url}  (goal type: no file captured)", flush=True)
                result.result_url = event.url
                result.events.append({"type": "ResultFound", "elapsed_ms": elapsed_ms,
                                      "url": event.url, "confidence": event.confidence,
                                      "content_type": meta.content_type if meta else None,
                                      "content_length": meta.content_length if meta else None,
                                      "suggested_filename": meta.suggested_filename if meta else None})

            elif isinstance(event, PageSkipped):
                result.events.append({"type": "PageSkipped", "elapsed_ms": elapsed_ms,
                                      "url": event.url, "reason": event.reason,
                                      "error_type": event.error_type})

            elif isinstance(event, BudgetExhausted):
                print(f"  {t} budget exhausted after {event.pages_visited} pages", flush=True)
                result.events.append({"type": "BudgetExhausted", "elapsed_ms": elapsed_ms,
                                      "pages_visited": event.pages_visited,
                                      "best_candidate": event.best_candidate})

            elif isinstance(event, CrawlComplete):
                result.found = event.found
                result.pages_visited = event.pages_visited
                result.elapsed_ms = event.elapsed_ms
                result.events.append({"type": "CrawlComplete", "elapsed_ms": elapsed_ms,
                                      "found": event.found,
                                      "pages_visited": event.pages_visited,
                                      "failure_mode": (
                                          event.failure_mode.value if event.failure_mode else None
                                      )})

    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.elapsed_ms = int((monotonic() - run_start) * 1000)
        print(f"  ERROR: {result.error}", flush=True)

    # Write event log to the same directory as the downloaded file
    events_path = parish_dir / "events.jsonl"
    try:
        with events_path.open("w", encoding="utf-8") as fh:
            for ev in result.events:
                fh.write(json.dumps(ev) + "\n")
    except OSError as exc:
        print(f"  WARNING: could not write events log: {exc}", flush=True)

    status = "FOUND" if result.found else "NOT FOUND"
    print(f"  → {status}  {result.pages_visited} pages  {result.elapsed_ms // 1000}s", flush=True)
    if result.file_path:
        print(f"  → {result.file_path}", flush=True)

    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = BULLETINS_DIR / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    adapter = LocalAdapter()

    print(f"\nCharlotte {charlotte.__version__}  —  parish bulletin retrieval")
    print(f"run dir : {run_dir}")
    print(f"goal    : {GOAL}")

    results: list[ParishResult] = []
    for i, parish in enumerate(PARISHES):
        if i > 0:
            await asyncio.sleep(INTER_PARISH_DELAY)
        parish_result = await run_parish(parish, run_dir, adapter)
        results.append(parish_result)

    # Summary JSON
    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "charlotte_version": charlotte.__version__,
        "goal": GOAL,
        "found": sum(1 for r in results if r.found),
        "total": len(results),
        "parishes": [
            {
                "name":               r.name,
                "url":                r.url,
                "found":              r.found,
                "result_url":         r.result_url,
                "content_type":       r.content_type,
                "content_length":     r.content_length,
                "suggested_filename": r.suggested_filename,
                "file_path":          str(r.file_path) if r.file_path else None,
                "pages_visited":      r.pages_visited,
                "elapsed_ms":         r.elapsed_ms,
                "error":              r.error,
            }
            for r in results
        ],
    }
    try:
        (run_dir / "_summary.json").write_text(json.dumps(summary, indent=2))
    except OSError as exc:
        print(f"WARNING: could not write summary: {exc}", flush=True)

    # Final table
    found_n = summary["found"]
    print(f"\n{'═' * 64}")
    print(f"  {found_n}/{len(results)} bulletins retrieved   run: {run_dir}")
    print(f"{'═' * 64}")
    for r in results:
        icon = "✓" if r.found else "✗"
        detail = (r.suggested_filename or r.result_url or "not found")
        if r.error:
            detail = f"ERROR: {r.error[:50]}"
        print(f"  {icon}  {r.name:<28}  {detail}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
