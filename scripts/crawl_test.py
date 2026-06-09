"""
Crawl test — runs the full crawl() BFS loop, prints streaming events,
and writes a structured JSON log to crawl_logs/ for later comparison.

Usage:
    python3 crawl_test.py [URL] [GOAL]

Defaults to https://news.ycombinator.com / "Find a story about AI".

Set CHARLOTTE_LOCAL_MODEL to override the model:
    CHARLOTTE_LOCAL_MODEL=llama3:8b python3 crawl_test.py
    CHARLOTTE_LOCAL_MODEL=llama3:8b python3 crawl_test.py https://docs.python.org/3/ "Find the tutorial"

Optional env vars:
    CHARLOTTE_MAX_PAGES      — page budget (default: 10)
    CHARLOTTE_MAX_DEPTH      — hop limit   (default: 3)
    CHARLOTTE_MAX_RESULTS    — result cap, 0 = unlimited (default: 1)
    CHARLOTTE_RENDER_JS      — use Playwright headless Chromium (default: false);
                               requires: python3 -m pip install playwright &&
                                         python3 -m playwright install chromium
    CHARLOTTE_CHROMIUM_EXECUTABLE — path to a Chromium/Chrome binary; use when
                               Playwright's bundled Chromium doesn't support the
                               current OS (e.g. Ubuntu 26.04+)
    CHARLOTTE_RESPECT_ROBOTS — obey robots.txt (default: true); set false for
                               domains you own or have explicit crawl permission
    CHARLOTTE_MODEL_TIMEOUT  — seconds before a model call is abandoned (default:
                               none); local reasoning models can take several
                               minutes per page — use a large value (e.g. 1000)
                               just to guard against a complete freeze
    CHARLOTTE_MODEL_VERBOSE  — stream model tokens to stderr in real time so you
                               can see the model is alive during long calls
                               (default: false)

Log files land in crawl_logs/ (gitignored). Each run writes one file
named <timestamp>_<hostname>.json for easy before/after comparison.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

from charlotte import crawl
from charlotte.adapters.local import LocalAdapter
from charlotte.models import (
    BudgetExhausted,
    CrawlComplete,
    CrawlStarted,
    ModelDecision,
    PageFetched,
    PageSkipped,
    ResultFound,
)

URL = sys.argv[1] if len(sys.argv) > 1 else "https://news.ycombinator.com"
GOAL = sys.argv[2] if len(sys.argv) > 2 else "Find a story about AI"

MAX_PAGES = int(os.environ.get("CHARLOTTE_MAX_PAGES", "10"))
MAX_DEPTH = int(os.environ.get("CHARLOTTE_MAX_DEPTH", "3"))
_max_results_env = os.environ.get("CHARLOTTE_MAX_RESULTS", "1")
MAX_RESULTS = None if _max_results_env == "0" else int(_max_results_env)
RENDER_JS = os.environ.get("CHARLOTTE_RENDER_JS", "").strip().lower() == "true"
RESPECT_ROBOTS = os.environ.get("CHARLOTTE_RESPECT_ROBOTS", "true").strip().lower() != "false"
CHROMIUM_EXECUTABLE = os.environ.get("CHARLOTTE_CHROMIUM_EXECUTABLE") or None
_model_timeout_env = os.environ.get("CHARLOTTE_MODEL_TIMEOUT")
MODEL_TIMEOUT = float(_model_timeout_env) if _model_timeout_env else None
MODEL_VERBOSE = os.environ.get("CHARLOTTE_MODEL_VERBOSE", "").strip().lower() == "true"
CONFIDENCE_THRESHOLD = float(os.environ.get("CHARLOTTE_CONFIDENCE_THRESHOLD", "0.70"))

LOGS_DIR = Path("crawl_logs")


# ---------------------------------------------------------------------------
# Log writer
# ---------------------------------------------------------------------------

def write_log(log: dict, hostname: str) -> Path:
    LOGS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    path = LOGS_DIR / f"{ts}_{hostname}.json"
    path.write_text(json.dumps(log, indent=2))
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    base_hostname = urlsplit(URL).hostname or ""
    adapter = LocalAdapter(timeout=MODEL_TIMEOUT, verbose=MODEL_VERBOSE)

    print(f"URL:        {URL}")
    print(f"Goal:       {GOAL}")
    print(f"Model:      {adapter._model}")
    print(f"max_pages:  {MAX_PAGES}  max_depth:  {MAX_DEPTH}  max_results: {MAX_RESULTS}  render_js: {RENDER_JS}  respect_robots: {RESPECT_ROBOTS}  chromium: {CHROMIUM_EXECUTABLE or 'bundled'}  model_timeout: {MODEL_TIMEOUT or 'none'}  conf_threshold: {CONFIDENCE_THRESHOLD}")
    print()

    log: dict = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "url": URL,
        "goal": GOAL,
        "model": adapter._model,
        "params": {
            "max_pages": MAX_PAGES,
            "max_depth": MAX_DEPTH,
            "max_results": MAX_RESULTS,
            "render_js": RENDER_JS,
            "respect_robots": RESPECT_ROBOTS,
            "chromium_executable": CHROMIUM_EXECUTABLE,
            "model_timeout": MODEL_TIMEOUT,
            "confidence_threshold": CONFIDENCE_THRESHOLD,
        },
        "events": [],
        "result": None,
    }

    run_start = monotonic()
    answers_collected: list[str | None] = []

    print("── crawl ───────────────────────────────")

    gen = crawl(
        URL,
        GOAL,
        model=adapter,
        max_pages=MAX_PAGES,
        max_depth=MAX_DEPTH,
        max_results=MAX_RESULTS,
        render_js=RENDER_JS,
        respect_robots=RESPECT_ROBOTS,
        chromium_executable=CHROMIUM_EXECUTABLE,
        confidence_threshold=CONFIDENCE_THRESHOLD,
        stream=True,
        default_delay=1.0,
    )

    async for event in gen:
        elapsed_ms = int((monotonic() - run_start) * 1000)

        if isinstance(event, CrawlStarted):
            print(
                f"  [start]   max_pages={event.max_pages}  "
                f"max_depth={event.max_depth}  max_results={event.max_results}"
            )
            log["events"].append({
                "type": "CrawlStarted",
                "elapsed_ms": elapsed_ms,
                "max_pages": event.max_pages,
                "max_depth": event.max_depth,
                "max_results": event.max_results,
            })

        elif isinstance(event, PageFetched):
            print(f"  [fetch]   depth={event.depth}  HTTP {event.http_status}  {event.url}  [{event.fetch_ms}ms]")
            log["events"].append({
                "type": "PageFetched",
                "elapsed_ms": elapsed_ms,
                "url": event.url,
                "depth": event.depth,
                "http_status": event.http_status,
                "fetch_ms": event.fetch_ms,
            })

        elif isinstance(event, ModelDecision):
            print(
                f"  [model]   found={event.found}  conf={event.confidence:.2f}  "
                f"{event.links_queued}/{len(event.links_suggested)} links queued/suggested  [{elapsed_ms}ms]"
            )
            print(f"            {textwrap.shorten(event.reasoning, width=100)}")
            if event.links_suggested:
                print(f"            suggested: {', '.join(textwrap.shorten(u, width=60) for u in event.links_suggested)}")
            print(f"            saw {len(event.links_available)} links on page")
            log["events"].append({
                "type": "ModelDecision",
                "elapsed_ms": elapsed_ms,
                "url": event.url,
                "found": event.found,
                "confidence": event.confidence,
                "links_queued": event.links_queued,
                "links_suggested": event.links_suggested,
                "links_available": event.links_available,
                "reasoning": event.reasoning,
            })

        elif isinstance(event, ResultFound):
            answers_collected.append(event.answer)
            print(f"  [result]  #{event.result_index}  conf={event.confidence:.2f}  {event.url}")
            if event.answer is not None:
                print(f"            answer: {event.answer}")
            else:
                print("            answer: (none)")
            log["events"].append({
                "type": "ResultFound",
                "elapsed_ms": elapsed_ms,
                "url": event.url,
                "confidence": event.confidence,
                "result_index": event.result_index,
                "answer": event.answer,
            })

        elif isinstance(event, PageSkipped):
            print(f"  [skip]    {event.url}  — {textwrap.shorten(event.reason, width=80)}")
            log["events"].append({
                "type": "PageSkipped",
                "elapsed_ms": elapsed_ms,
                "url": event.url,
                "reason": event.reason,
                "error_type": event.error_type,
            })

        elif isinstance(event, BudgetExhausted):
            print(f"  [budget]  exhausted after {event.pages_visited} pages  best={event.best_candidate}")
            log["events"].append({
                "type": "BudgetExhausted",
                "elapsed_ms": elapsed_ms,
                "pages_visited": event.pages_visited,
                "depth_reached": event.depth_reached,
                "best_candidate": event.best_candidate,
            })

        elif isinstance(event, CrawlComplete):
            print()
            print("── result ──────────────────────────────")
            print(f"  found:         {event.found}")
            print(f"  results:       {event.result_count}")
            if answers_collected:
                for i, ans in enumerate(answers_collected, 1):
                    print(f"  answer #{i}:     {ans if ans is not None else '(none)'}")
            print(f"  pages visited: {event.pages_visited}")
            print(f"  depth reached: {event.depth_reached}")
            print(f"  elapsed:       {event.elapsed_ms:,}ms")
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
                "found": event.found,
                "result_count": event.result_count,
                "answers": answers_collected if answers_collected else None,
                "pages_visited": event.pages_visited,
                "depth_reached": event.depth_reached,
                "elapsed_ms": event.elapsed_ms,
            }

    print()
    path = write_log(log, base_hostname)
    print(f"Log written → {path}")


asyncio.run(main())
