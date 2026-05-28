"""
Single-page smoke test — chains all available components manually.

Does NOT run a full crawl (CHAR-013 not built yet). Exercises one page:
    fetch → strip hidden → extract → call LocalAdapter → validate output

Usage:
    python3 smoke_test.py [URL] [GOAL]

Defaults to https://news.ycombinator.com with goal "Find a story about AI".

The default model is llama3:8b, which requires a GPU or a very long wait on
CPU. Set CHARLOTTE_LOCAL_MODEL to override:

    CHARLOTTE_LOCAL_MODEL=phi3:mini python3 smoke_test.py
    CHARLOTTE_LOCAL_MODEL=phi3:mini python3 smoke_test.py https://python.org "Find the download page"
    CHARLOTTE_LOCAL_MODEL=phi3:mini python3 smoke_test.py https://en.wikipedia.org/wiki/Turing_machine "Find the inventor"

phi3:mini is the recommended CPU model — completes in under 120 s on most
hardware. Warm it up first if Ollama has just started:

    curl -s http://localhost:11434/api/generate \\
         -d '{"model":"phi3:mini","prompt":"Hi","stream":false}' | grep -o '"response":"[^"]*"'
"""

import asyncio
import sys
import textwrap

from charlotte.adapters.local import LocalAdapter
from charlotte.core.engine import call_with_validation
from charlotte.core.extractor import extract
from charlotte.core.fetcher import PageFetcher
from charlotte.core.sanitizer import strip_hidden

URL  = sys.argv[1] if len(sys.argv) > 1 else "https://news.ycombinator.com"
GOAL = sys.argv[2] if len(sys.argv) > 2 else "Find a story about AI"


async def main() -> None:
    from urllib.parse import urlsplit
    hostname = urlsplit(URL).hostname or ""

    adapter = LocalAdapter()

    print(f"URL:    {URL}")
    print(f"Goal:   {GOAL}")
    print(f"Model:  {adapter._model}")
    print()

    # 1 — Fetch
    print("Fetching page...")
    fetcher = PageFetcher(allowed_domains={hostname}, polite_delay=0.0)
    page = await fetcher.fetch(URL, visited_urls=set())
    print(f"  HTTP {page.status_code}  ({len(page.html):,} bytes)")

    # 2 — Sanitize (Layer 1: strip hidden content)
    clean_html = strip_hidden(page.html)

    # 3 — Extract text and links
    extracted = extract(clean_html, page_url=page.url, allowed_domains={hostname})
    print(f"  Extracted {len(extracted.text):,} chars of text, {len(extracted.links)} links")
    print()

    # 4 — Call LocalAdapter (with validation + one retry on schema failure)
    print("Calling local model...")
    result = await call_with_validation(
        adapter,
        goal=GOAL,
        navigation_hint=None,
        page_title="",
        page_url=page.url,
        page_summary=extracted.text,
        available_links=extracted.links,
        visit_history=[],
        results_so_far=0,
    )

    # 5 — Print decision
    print("Model decision:")
    print(f"  found:      {result.found}")
    print(f"  confidence: {result.confidence:.2f}")
    print(f"  result_url: {result.result_url}")
    print(f"  reasoning:  {textwrap.shorten(result.reasoning, width=120)}")
    print(f"  links ({len(result.links_to_follow)} suggested):")
    for link in result.links_to_follow[:5]:
        print(f"    {link}")
    if len(result.links_to_follow) > 5:
        print(f"    ... and {len(result.links_to_follow) - 5} more")


asyncio.run(main())
