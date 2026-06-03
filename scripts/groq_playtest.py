"""
Groq adapter playtest — runs crawl() against real websites using GroqAdapter.

Purpose: surface prompt/model issues and plausibility threshold problems that
only show up against real-world page content. Results are logged to crawl_logs/.

Usage:
    GROQ_API_KEY=gsk_... python3 scripts/groq_playtest.py

Or load the key from the environment before running:
    eval "$(grep 'GROQ_API_KEY' ~/.bashrc)"
    python3 scripts/groq_playtest.py

The script runs a small set of diverse goals, one at a time, and prints a
pass/fail summary at the end. Each run also writes a JSON log.

Optional env vars:
    GROQ_MODEL            — override model (default: llama-3.1-8b-instant)
    CHARLOTTE_MAX_PAGES   — pages per crawl (default: 8)
    CHARLOTTE_MAX_DEPTH   — hop limit (default: 3)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

from charlotte import crawl
from charlotte.adapters.groq import GroqAdapter
from charlotte.models import (
    BudgetExhausted,
    CrawlComplete,
    CrawlStarted,
    ModelDecision,
    PageFetched,
    PageSkipped,
    ResultFound,
)

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _clean(value: str | None) -> str | None:
    """Strip control characters from untrusted text before printing or logging."""
    if value is None:
        return None
    return _CONTROL_CHARS_RE.sub(" ", value).strip()


def _read_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer (got {raw!r})") from None
    if value < 1:
        raise ValueError(f"{name} must be >= 1 (got {value})")
    return value


GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
MAX_PAGES = _read_positive_int("CHARLOTTE_MAX_PAGES", 8)
MAX_DEPTH = _read_positive_int("CHARLOTTE_MAX_DEPTH", 3)

LOGS_DIR = Path("crawl_logs")

# ---------------------------------------------------------------------------
# Test cases — diverse goal types to stress the prompt and plausibility guard
# ---------------------------------------------------------------------------

CASES: list[tuple[str, str]] = [
    # (start_url, goal)
    # 1. URL-finding on the start page — Downloads link is in the navigation bar
    ("https://www.python.org", "Find the URL of the Python downloads page"),
    # 2. Fact extraction from a Wikipedia article — answer is in the infobox
    ("https://en.wikipedia.org/wiki/Python_(programming_language)", "Who created the Python programming language?"),
    # 3. Multi-hop navigation — docs index → named section
    #    NOTE: docs.python.org/3/tutorial/index.html says "designed for programmers
    #    new to Python, NOT beginners who are new to programming" — so the goal must
    #    match the page's own description.
    ("https://docs.python.org/3/", "Find the Python Tutorial"),
    # 4. Navigation with explicit link text — tests that the model follows clear links
    #    "jobs" link is visible in HN's nav bar with text "jobs".
    ("https://news.ycombinator.com", "Find the Hacker News jobs board"),
]


# ---------------------------------------------------------------------------
# Single-case runner
# ---------------------------------------------------------------------------

async def run_case(
    adapter: GroqAdapter,
    url: str,
    goal: str,
    case_num: int,
    total: int,
) -> dict:
    hostname = urlsplit(url).hostname or url
    print(f"\n{'─'*60}")
    print(f"Case {case_num}/{total}: {goal}")
    print(f"  Start: {url}")
    print(f"  Model: {GROQ_MODEL}  max_pages={MAX_PAGES}  max_depth={MAX_DEPTH}")
    print()

    log: dict = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "url": url,
        "goal": goal,
        "model": GROQ_MODEL,
        "events": [],
        "result": None,
    }

    t0 = monotonic()
    answers_collected: list[str | None] = []
    outcome = "unknown"

    try:
        async for event in crawl(
            url,
            goal,
            model=adapter,
            max_pages=MAX_PAGES,
            max_depth=MAX_DEPTH,
            max_results=1,
            respect_robots=True,
            confidence_threshold=0.70,
            stream=True,
            default_delay=1.0,
        ):
            elapsed_ms = int((monotonic() - t0) * 1000)

            if isinstance(event, CrawlStarted):
                log["events"].append({"type": "CrawlStarted", "elapsed_ms": elapsed_ms})

            elif isinstance(event, PageFetched):
                print(f"  [fetch]  depth={event.depth}  HTTP {event.http_status}  {event.url}  [{event.fetch_ms}ms]")
                log["events"].append({
                    "type": "PageFetched",
                    "elapsed_ms": elapsed_ms,
                    "url": event.url,
                    "depth": event.depth,
                    "http_status": event.http_status,
                    "fetch_ms": event.fetch_ms,
                })

            elif isinstance(event, ModelDecision):
                reasoning = _clean(event.reasoning) or ""
                print(
                    f"  [model]  found={event.found}  conf={event.confidence:.2f}  "
                    f"{event.links_queued}/{len(event.links_suggested)} queued  [{elapsed_ms}ms]"
                )
                print(f"           {textwrap.shorten(reasoning, width=100)}")
                log["events"].append({
                    "type": "ModelDecision",
                    "elapsed_ms": elapsed_ms,
                    "url": event.url,
                    "found": event.found,
                    "confidence": event.confidence,
                    "links_queued": event.links_queued,
                    "links_suggested": event.links_suggested,
                    "reasoning": reasoning,
                })

            elif isinstance(event, ResultFound):
                answer = _clean(event.answer)
                answers_collected.append(answer)
                print(f"  [result] conf={event.confidence:.2f}  {event.url}")
                if answer:
                    print(f"           answer: {textwrap.shorten(answer, width=100)}")
                log["events"].append({
                    "type": "ResultFound",
                    "elapsed_ms": elapsed_ms,
                    "url": event.url,
                    "confidence": event.confidence,
                    "result_index": event.result_index,
                    "answer": answer,
                })

            elif isinstance(event, PageSkipped):
                reason = _clean(event.reason) or ""
                print(f"  [skip]   {event.url}  — {textwrap.shorten(reason, width=70)}")
                log["events"].append({
                    "type": "PageSkipped",
                    "elapsed_ms": elapsed_ms,
                    "url": event.url,
                    "reason": reason,
                    "error_type": event.error_type,
                })

            elif isinstance(event, BudgetExhausted):
                outcome = "budget_exhausted"
                print(f"  [budget] exhausted after {event.pages_visited} pages")
                log["events"].append({
                    "type": "BudgetExhausted",
                    "elapsed_ms": elapsed_ms,
                    "pages_visited": event.pages_visited,
                    "best_candidate": event.best_candidate,
                })

            elif isinstance(event, CrawlComplete):
                outcome = "found" if event.found else "not_found"
                print()
                print(f"  → found={event.found}  results={event.result_count}  "
                      f"pages={event.pages_visited}  elapsed={event.elapsed_ms}ms")
                if answers_collected:
                    for i, ans in enumerate(answers_collected, 1):
                        print(f"  → answer #{i}: {ans or '(none)'}")
                log["events"].append({
                    "type": "CrawlComplete",
                    "elapsed_ms": elapsed_ms,
                    "found": event.found,
                    "result_count": event.result_count,
                    "pages_visited": event.pages_visited,
                    "depth_reached": event.depth_reached,
                    "crawl_elapsed_ms": event.elapsed_ms,
                })
                log["result"] = {
                    "outcome": outcome,
                    "found": event.found,
                    "result_count": event.result_count,
                    "answers": answers_collected,
                    "pages_visited": event.pages_visited,
                    "elapsed_ms": event.elapsed_ms,
                }

    except Exception as exc:
        safe_exc = _clean(str(exc)) or ""
        outcome = f"error: {type(exc).__name__}: {safe_exc}"
        print(f"  [ERROR]  {outcome}")
        log["result"] = {"outcome": outcome}

    LOGS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    log_path = LOGS_DIR / f"{ts}_{hostname}.json"
    log_path.write_text(json.dumps(log, indent=2))
    print(f"  Log → {log_path}")

    return {"url": url, "goal": goal, "outcome": outcome}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    try:
        adapter = GroqAdapter(model=GROQ_MODEL)
    except Exception as exc:
        print(f"ERROR: Could not create GroqAdapter: {exc}")
        sys.exit(1)

    print(f"Groq playtest  model={GROQ_MODEL}  cases={len(CASES)}")
    print(f"max_pages={MAX_PAGES}  max_depth={MAX_DEPTH}")

    results: list[dict] = []
    for i, (url, goal) in enumerate(CASES, 1):
        result = await run_case(adapter, url, goal, i, len(CASES))
        results.append(result)

    print(f"\n{'═'*60}")
    print("Summary")
    print(f"{'═'*60}")
    passed = 0
    for r in results:
        status = "PASS" if r["outcome"] in ("found", "not_found", "budget_exhausted") else "FAIL"
        if status == "PASS":
            passed += 1
        print(f"  {status}  {r['outcome']:20s}  {r['goal'][:55]}")
    print(f"\n  {passed}/{len(results)} cases completed without errors")


asyncio.run(main())
