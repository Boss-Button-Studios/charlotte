"""
Pipeline harness — runs each component step-by-step, prints a summary,
and writes a structured JSON log to harness_logs/ for later comparison.

Usage:
    python3 harness.py [URL] [GOAL]

Defaults to https://news.ycombinator.com / "Find a story about AI".

Set CHARLOTTE_LOCAL_MODEL to override the model:
    CHARLOTTE_LOCAL_MODEL=phi3:mini python3 harness.py
    CHARLOTTE_LOCAL_MODEL=phi3:mini python3 harness.py https://python.org "Find the download page"

phi3:mini is the recommended CPU model. Warm it up first if Ollama just started:
    curl -s http://localhost:11434/api/generate \\
         -d '{"model":"phi3:mini","prompt":"Hi","stream":false}' | grep -o '"response":"[^"]*"'

Log files land in harness_logs/ (gitignored). Each run writes one file
named <timestamp>_<hostname>.json for easy before/after comparison.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

from charlotte.adapters.local import LocalAdapter
from charlotte.core.adapter_validation import call_with_validation
from charlotte.core.extractor import ExtractedPage, extract
from charlotte.core.fetcher import FetchResult, PageFetcher
from charlotte.core.robots import RobotsHandler
from charlotte.core.sanitizer import strip_hidden
from charlotte.exceptions import RobotsError

URL = sys.argv[1] if len(sys.argv) > 1 else "https://news.ycombinator.com"
GOAL = sys.argv[2] if len(sys.argv) > 2 else "Find a story about AI"

LOGS_DIR = Path("harness_logs")


# ---------------------------------------------------------------------------
# Individual pipeline steps — each returns (log_entry, output_value)
# ---------------------------------------------------------------------------

async def step_robots(url: str, default_delay: float) -> tuple[dict, float]:
    """Fetch and check robots.txt for *url*.

    Returns:
        (log_entry, effective_crawl_delay)

    Raises:
        RobotsError: URL is blocked by robots.txt or robots.txt is unreachable.
    """
    handler = RobotsHandler()
    t0 = monotonic()
    crawl_delay = await handler.check(url, default_delay)
    elapsed_ms = int((monotonic() - t0) * 1000)
    entry = {"allowed": True, "crawl_delay": crawl_delay, "elapsed_ms": elapsed_ms}
    print(f"  Allowed  (crawl_delay={crawl_delay}s)  [{elapsed_ms}ms]")
    return entry, crawl_delay


async def step_fetch(url: str, allowed_domains: set[str], polite_delay: float) -> tuple[dict, FetchResult]:
    """Fetch the page at *url*.

    Returns:
        (log_entry, FetchResult)
    """
    fetcher = PageFetcher(allowed_domains=allowed_domains, polite_delay=polite_delay)
    page = await fetcher.fetch(url, visited_urls=set())
    entry = {
        "status_code": page.status_code,
        "html_chars": len(page.html),
        "fetch_ms": page.fetch_ms,
        "redirect_chain": page.redirect_chain,
    }
    print(f"  HTTP {page.status_code}  ({len(page.html):,} chars,  {page.fetch_ms}ms)")
    if page.redirect_chain:
        for status, dest in page.redirect_chain:
            print(f"    → {status} {dest}")
    return entry, page


def step_sanitize(html: str) -> tuple[dict, str]:
    """Strip hidden content from *html*.

    Returns:
        (log_entry, clean_html)
    """
    t0 = monotonic()
    clean = strip_hidden(html)
    elapsed_ms = int((monotonic() - t0) * 1000)
    before = len(html)
    after = len(clean)
    reduction = (before - after) / before * 100 if before else 0.0
    entry = {
        "html_before_chars": before,
        "html_after_chars": after,
        "reduction_pct": round(reduction, 1),
        "elapsed_ms": elapsed_ms,
    }
    print(f"  {before:,} → {after:,} chars  ({reduction:.1f}% stripped)  [{elapsed_ms}ms]")
    return entry, clean


def step_extract(clean_html: str, page_url: str, allowed_domains: set[str]) -> tuple[dict, ExtractedPage]:
    """Extract text and links from sanitized HTML.

    Returns:
        (log_entry, ExtractedPage)
    """
    t0 = monotonic()
    result = extract(clean_html, page_url=page_url, allowed_domains=allowed_domains)
    elapsed_ms = int((monotonic() - t0) * 1000)
    entry = {
        "text_chars": len(result.text),
        "link_count": len(result.links),
        "elapsed_ms": elapsed_ms,
        "text": result.text,
        "link_urls": [link["url"] for link in result.links],
    }
    print(f"  {len(result.text):,} chars text,  {len(result.links)} links  [{elapsed_ms}ms]")
    return entry, result


async def step_model(
    adapter: LocalAdapter,
    goal: str,
    page_url: str,
    extracted: ExtractedPage,
) -> tuple[dict, object]:
    """Call the local model and validate the response.

    Returns:
        (log_entry, NavResult)
    """
    t0 = monotonic()
    nav = await call_with_validation(
        adapter,
        goal=goal,
        navigation_hint=None,
        page_title="",
        page_url=page_url,
        page_summary=extracted.text,
        available_links=extracted.links,
        visit_history=[],
        results_so_far=0,
    )
    elapsed_ms = int((monotonic() - t0) * 1000)
    entry = {
        "found": nav.found,
        "confidence": nav.confidence,
        "result_url": nav.result_url,
        "links_to_follow": nav.links_to_follow,
        "reasoning": nav.reasoning,
        "elapsed_ms": elapsed_ms,
    }
    print(f"  found:      {nav.found}  [{elapsed_ms}ms]")
    print(f"  confidence: {nav.confidence:.2f}")
    print(f"  result_url: {nav.result_url}")
    print(f"  reasoning:  {textwrap.shorten(nav.reasoning, width=120)}")
    print(f"  links ({len(nav.links_to_follow)} suggested):")
    for link in nav.links_to_follow[:5]:
        print(f"    {link}")
    if len(nav.links_to_follow) > 5:
        print(f"    ... and {len(nav.links_to_follow) - 5} more")
    return entry, nav


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
# Orchestrator
# ---------------------------------------------------------------------------

async def main() -> None:
    base_hostname = urlsplit(URL).hostname or ""
    www_counterpart = base_hostname[4:] if base_hostname.startswith("www.") else f"www.{base_hostname}"
    allowed_domains = {base_hostname, www_counterpart}
    adapter = LocalAdapter()

    print(f"URL:    {URL}")
    print(f"Goal:   {GOAL}")
    print(f"Model:  {adapter._model}")
    print()

    log: dict = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "url": URL,
        "goal": GOAL,
        "model": adapter._model,
        "steps": {},
    }

    # 1 — Robots
    print("── robots ──────────────────────────────")
    try:
        log["steps"]["robots"], crawl_delay = await step_robots(URL, default_delay=1.0)
    except RobotsError as exc:
        log["steps"]["robots"] = {"allowed": False, "reason": str(exc)}
        print(f"  BLOCKED: {exc}")
        write_log(log, base_hostname)
        return
    print()

    try:
        # 2 — Fetch
        print("── fetch ───────────────────────────────")
        log["steps"]["fetch"], page = await step_fetch(URL, allowed_domains, crawl_delay)
        final_hostname = (urlsplit(page.url).hostname or base_hostname).lower()
        final_counterpart = (
            final_hostname[4:] if final_hostname.startswith("www.")
            else f"www.{final_hostname}"
        )
        final_allowed_domains = {final_hostname, final_counterpart}
        print()

        # 3 — Sanitize
        print("── sanitize ────────────────────────────")
        log["steps"]["sanitize"], clean_html = step_sanitize(page.html)
        print()

        # 4 — Extract
        print("── extract ─────────────────────────────")
        log["steps"]["extract"], extracted = step_extract(clean_html, page.url, final_allowed_domains)
        print()

        # 5 — Model
        print("── model ───────────────────────────────")
        log["steps"]["model"], _ = await step_model(adapter, GOAL, page.url, extracted)
        print()

    except Exception as exc:
        log["error"] = str(type(exc).__name__)
        print(f"  Pipeline failed: {type(exc).__name__}")

    finally:
        path = write_log(log, base_hostname)
        print(f"Log written → {path}")


asyncio.run(main())
