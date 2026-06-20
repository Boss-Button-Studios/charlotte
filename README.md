# Charlotte

`charlotte-crawler` is a goal-directed web navigation agent. Given a starting URL and a natural language goal, Charlotte navigates a website purposefully — evaluating each page and deciding which links to follow — until she finds what she is looking for or exhausts her search budget.

Charlotte is a **library, not a service.** Import it into any Python project. It has no server to run, no API to call, and no data leaves your environment unless you choose a cloud adapter.

---

## Installation

```bash
# Base install — httpx fetcher, BeautifulSoup extractor, no model adapter
pip install charlotte-crawler

# With Groq cloud adapter (recommended for cloud deployments)
pip install charlotte-crawler[groq]

# With JavaScript rendering (headless Chromium via Playwright)
pip install charlotte-crawler[playwright]
playwright install chromium
```

The `LocalAdapter` talks to any OpenAI-compatible local server (Ollama, LM Studio, llama.cpp) using `httpx`, which is already a required dependency. No extra install needed.

---

## Quick Start

### Find a link

```python
import asyncio
from charlotte import find_link
from charlotte.adapters.groq import GroqAdapter

async def main():
    result = await find_link(
        start_url="https://www.example.edu",
        goal="Find the academic calendar page",
        model=GroqAdapter(),  # requires GROQ_API_KEY env var
    )

    if result.found:
        print(result.urls[0])       # URL of the matching page
    else:
        print(result.note)          # explains why nothing was found

asyncio.run(main())
```

### Crawl with streaming events

```python
import asyncio
from charlotte import crawl, ResultFound, CrawlComplete
from charlotte.adapters.local import LocalAdapter

async def main():
    async for event in crawl(
        start_url="https://docs.example.com",
        goal="Find the API reference for the payments module",
        model=LocalAdapter(),       # connects to Ollama at localhost:11434
        stream=True,
    ):
        if isinstance(event, ResultFound):
            print(f"Found: {event.url}  (confidence {event.confidence:.0%})")
        elif isinstance(event, CrawlComplete):
            print(f"Done — {event.result_count} result(s), {event.pages_visited} page(s) visited")

asyncio.run(main())
```

### Extract a fact

```python
import asyncio
from charlotte import crawl
from charlotte.adapters.groq import GroqAdapter

async def main():
    result = await crawl(
        start_url="https://www.ucsd.edu/about/",
        goal="Find the main switchboard phone number",
        model=GroqAdapter(),
        stream=False,
    )

    if result.found and result.answers:
        print(result.answers[0])    # e.g. "(858) 534-2230"

asyncio.run(main())
```

---

## Adapters

An adapter is any async callable with the signature below. Charlotte ships two.

### GroqAdapter

Calls the [Groq API](https://console.groq.com) — fast, accurate, and free to start. Requires the `[groq]` extra and a `GROQ_API_KEY` environment variable. Defaults to **Llama 3.1 8B Instruct**.

```python
from charlotte.adapters.groq import GroqAdapter

model = GroqAdapter()                                 # llama-3.1-8b-instant, reads GROQ_API_KEY
model = GroqAdapter(api_key="gsk_…")                 # or pass the key directly
model = GroqAdapter(model="llama-3.3-70b-versatile")  # a stronger non-reasoning model
model = GroqAdapter(model="qwen/qwen3-32b")           # a reasoning model (see below)
```

**Reasoning models** (e.g. `qwen/qwen3-32b`, `openai/gpt-oss-*`) are recognised by name and given a larger completion budget automatically — their "thinking" tokens count against the response, so the non-reasoning default would starve them and fail.

**Prompt size** is bounded by `max_page_chars` (default 4500) and `max_prompt_links` (default 25); `max_completion_tokens` is sized per model (700 non-reasoning, 4096 reasoning). The defaults keep a single request under Groq's per-request token ceiling. On the **free tier** the per-minute and per-request token limits are tight — reasoning models and multi-page JS-rendered crawls can exhaust them; a paid (Dev) tier removes those walls.

**Failures** raise a named `AdapterOutputError` that identifies the cause — expired/invalid key (401), oversized request (413), rate limit (429), or another HTTP status — never an opaque error. The Groq response body (which may contain keys or page content) is never surfaced.

### LocalAdapter

Calls any **OpenAI-compatible local inference endpoint** — Ollama, LM Studio, llama.cpp server, text-generation-webui. Defaults to `deepseek-r1:14b` at `http://localhost:11434`.

```python
from charlotte.adapters.local import LocalAdapter

model = LocalAdapter()                          # deepseek-r1:14b @ localhost:11434
model = LocalAdapter(model_name="llama3.2:3b") # lighter model
model = LocalAdapter(
    base_url="http://gpu-box:11434",
    model_name="qwen2.5:14b",
    verbose=True,                               # stream tokens to stderr
)
```

Pull the default model with: `ollama pull deepseek-r1:14b`

### Bring Your Own Model (BYOM)

Any async callable that matches this signature works as a `model=` argument:

```python
from typing import Any

async def my_adapter(
    *,
    goal: str,
    navigation_hint: str | None,
    page_title: str,
    page_url: str,
    page_summary: str,
    available_links: list[dict[str, str]],   # [{"text": "…", "url": "…"}, …]
    visit_history: list[str],
    results_so_far: int,
    schema_hint: str | None = None,
) -> dict[str, Any]:
    ...
    return {
        "found": True,                        # bool
        "confidence": 0.95,                   # float 0.0–1.0
        "result_url": page_url,               # str when found=True, else null
        "links_to_follow": [],                # list[str] of URLs to visit next
        "reasoning": "Found it on this page.",  # non-empty str
        "answer": None,                       # str for facts, null for navigation
    }
```

Charlotte validates the response dict against this schema before use. Malformed output triggers one retry with a reinforced prompt; two failures skip the page with `AdapterOutputError`.

---

## `crawl()` — Parameters

```python
crawl(
    start_url,          # str  — absolute URL to start from
    goal,               # str  — natural language description of what to find
    *,
    model=None,         # AdapterProtocol | None — None resolves via CHARLOTTE_DEFAULT_ADAPTER
    max_pages=20,       # int  — hard ceiling on pages fetched
    max_depth=5,        # int  — max link-hops from start_url
    max_results=1,      # int | None — stop after N results; None = collect all
    confidence_threshold=0.70,  # float — minimum confidence to record a result
    render_js=False,    # bool — use Playwright for JS-rendered pages
    allowed_domains=None,       # list[str] | None — defaults to start_url domain
    return_content=False,       # bool — include sanitized page text in CrawlResult
    navigation_hint=None,       # str | None — extra context for the model
    stream=None,        # bool | None — None reads CHARLOTTE_STREAM (default True)
    respect_robots=None,        # bool | None — None reads CHARLOTTE_RESPECT_ROBOTS (default True)
    connect_timeout=10.0,       # float — TCP connection timeout (seconds)
    read_timeout=30.0,          # float — response body read timeout (seconds)
    render_timeout=15.0,        # float — JS settle timeout for Playwright (seconds)
    default_delay=1.0,          # float — floor for polite inter-request delay (seconds)
    chromium_executable=None,   # str | None — path to Chromium binary (Playwright)
)
```

**Returns:**
- `AsyncGenerator[StreamEvent, None]` when `stream=True`
- `Coroutine[CrawlResult]` when `stream=False` — use `await crawl(...)`

**`CrawlResult` fields:**

| Field | Type | Description |
|---|---|---|
| `found` | `bool` | Whether at least one result was confirmed |
| `result_urls` | `list[str]` | URLs of all confirmed results, in discovery order |
| `answers` | `list[str \| None] \| None` | Extracted facts parallel to `result_urls`; `None` if nothing found |
| `content` | `list[str] \| None` | Sanitized page text per result (only when `return_content=True`) |
| `confidence` | `float` | Confidence of the best result |
| `pages_visited` | `int` | Total pages fetched |
| `depth_reached` | `int` | Deepest link-hop reached |
| `visit_log` | `list[VisitLogEntry]` | Per-page URL, depth, found flag, confidence, reasoning |
| `best_candidate_url` | `str \| None` | Highest-confidence URL seen, even if below threshold |
| `budget_exhausted` | `bool` | True if `max_pages` or `max_depth` was hit before finding a result |

---

## `find_link()` — Parameters

`find_link()` is a thin wrapper around `crawl()` with two fixed differences: `max_results=None` (collect every match) and `return_content=False` (always). All other parameters are identical.

```python
find_link(
    start_url,          # str
    goal,               # str
    *,
    model=None,
    max_pages=20,
    max_depth=5,
    confidence_threshold=0.70,
    render_js=False,
    allowed_domains=None,
    navigation_hint=None,
    stream=None,
    respect_robots=None,
    connect_timeout=10.0,
    read_timeout=30.0,
    render_timeout=15.0,
    default_delay=1.0,
)
```

**Returns:**
- `AsyncGenerator[StreamEvent, None]` when `stream=True`
- `Coroutine[LinkResult]` when `stream=False` — use `await find_link(...)`

**`LinkResult` fields:**

| Field | Type | Description |
|---|---|---|
| `found` | `bool` | Whether at least one link was found |
| `urls` | `list[str]` | All matching URLs, in discovery order |
| `confidence` | `float` | Confidence of the best match |
| `pages_visited` | `int` | Total pages fetched |
| `best_candidate_url` | `str \| None` | Highest-confidence URL seen, even if below threshold |
| `budget_exhausted` | `bool` | True if the budget was exhausted before a match |
| `note` | `str \| None` | Human-readable explanation when `found=False` |

---

## Environment Variables

| Variable | Default | Effect |
|---|---|---|
| `CHARLOTTE_DEFAULT_ADAPTER` | `"groq"` | `"groq"` or `"local"` — adapter used when `model=None` |
| `CHARLOTTE_LOCAL_BASE_URL` | `"http://localhost:11434"` | Base URL for `LocalAdapter` |
| `CHARLOTTE_LOCAL_MODEL` | `"deepseek-r1:14b"` | Model name for `LocalAdapter` |
| `CHARLOTTE_STREAM` | `"true"` | `"true"` or `"false"` — default for `stream=None` |
| `CHARLOTTE_RESPECT_ROBOTS` | `"true"` | `"true"` or `"false"` — default for `respect_robots=None` |
| `GROQ_API_KEY` | *(none)* | Required when using `GroqAdapter` |

Direct `crawl()` / `find_link()` parameters always take precedence over env vars.

---

## robots.txt Policy

Charlotte fetches and obeys `robots.txt` before visiting any page, unless `respect_robots=False`.

- **404** response → no restrictions; crawl proceeds normally
- **401 / 403** → no restrictions; crawl proceeds normally
- **5xx / timeout / parse error** → `RobotsError`; crawl does not start
- **`Disallow` rule matched** → `RobotsError`; affected page skipped; other pages continue
- **`Crawl-delay` directive** → honoured; whichever is larger between the directive and `default_delay` is used
- **Cross-domain redirect** → each domain's `robots.txt` is checked independently; permissions never inherit across domain boundaries
- **User-agent matching** — `charlotte-crawler` first, then `*`

---

## Streaming Events

When `stream=True`, Charlotte yields the following events in order:

| Event | Fields | When emitted |
|---|---|---|
| `CrawlStarted` | `url`, `goal` | Once, immediately |
| `PageFetched` | `url`, `status_code`, `depth`, `render_js` | After each successful fetch |
| `ModelDecision` | `url`, `found`, `confidence`, `reasoning`, `links_queued`, `links_available`, `links_suggested` | After each model evaluation |
| `ResultFound` | `url`, `confidence`, `result_index`, `answer` | When a result is confirmed |
| `PageSkipped` | `url`, `reason`, `error_type` | When a page is skipped (fetch error, schema failure, plausibility, robots) |
| `BudgetExhausted` | `url`, `reason` | When `max_pages` or `max_depth` is reached without a result |
| `CrawlComplete` | `found`, `result_count`, `pages_visited`, `elapsed_seconds` | Once, always last |

All events include `type: str` and `timestamp: float` (Unix time) fields.

Import event types directly from the package:

```python
from charlotte import (
    CrawlStarted, PageFetched, ModelDecision, ResultFound,
    PageSkipped, BudgetExhausted, CrawlComplete,
)
```

---

## Error Classes

All Charlotte exceptions inherit from `CharlotteError`. Third-party exceptions (`httpx`, `groq`, `playwright`) are caught at component boundaries and re-raised as one of these — they never reach the caller.

| Exception | Raised when |
|---|---|
| `CharlotteConfigError` | Invalid configuration — bad URL, missing API key, Playwright not installed, invalid parameter |
| `CharlotteNetworkError` | HTTP error response (4xx / 5xx) that is not retried |
| `CharlotteTimeoutError` | Connect, read, render, or model timeout |
| `CharlotteRedirectError` | Cross-domain redirect to a disallowed host |
| `RobotsError` | robots.txt blocks a URL or cannot be fetched |
| `AdapterOutputError` | Model returned malformed JSON / failed schema validation after retry, or the model provider's API call failed (auth, rate limit, oversized request, …) |
| `CharlotteInternalError` | Unexpected engine-level state (should not occur; file a bug) |

`CharlotteConfigError` is raised eagerly — before any network I/O — when configuration is invalid. All others surface as `PageSkipped` events (stream mode) or as logged debug entries in the `visit_log` (non-stream mode). `crawl()` and `find_link()` never raise after the crawl has started.

---

## Specification

The full technical specification — adapter authoring guide, streaming events reference, security model, URL normalization rules — is at `docs/charlotte-spec-v2.0.2.md` (current; the v1.4 and v2.0/v2.0.1 documents are kept as historical reference).

---

## Licence

MIT — see `LICENSE`.
