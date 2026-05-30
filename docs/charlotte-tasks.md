# Charlotte — Task Decomposition
**Based on spec v1.3**

Tasks are ordered by dependency. Items marked **parallel** can be worked simultaneously once their prerequisites are done. Each task references the spec section it implements. Each task includes writing its own unit tests — integration tests against the public API are written as a dedicated task once the engine exists.

---

## Phase 1 — Foundation
*Nothing else can start until these are done.*

**CHAR-001 — Repo setup**
Create the `Boss-Button-Studios/charlotte` repository. Set up `pyproject.toml` with package metadata (`charlotte-crawler`), required dependencies (`httpx`, `beautifulsoup4`), and optional dependency groups (`playwright`, `groq`, `ollama`). Add licence, `.gitignore`, README stub, and CI skeleton. Establish the package directory structure:
```
charlotte/
  __init__.py          # exports crawl() and find_link()
  adapters/
    __init__.py
    base.py            # adapter Protocol / ABC
    groq.py
    local.py
  core/
    fetcher.py         # HTTP + Playwright
    sanitizer.py       # all three layers
    extractor.py
    normalizer.py      # URL normalization
    engine.py          # crawl loop
    robots.py          # robots.txt handling
  models.py            # CrawlResult, LinkResult, all event types
  exceptions.py        # CharlotteError hierarchy
  config.py            # env var handling
tests/
  unit/
  integration/
```
*Spec ref: §14, §16*

---

**CHAR-002 — Data models and exception hierarchy**
Define all stable public types before any component is written. Everything else depends on these.

- `CrawlResult` and `LinkResult` as formal dataclasses (not dicts). Field names and types exactly as specified. `result_urls` is always a list.
- All seven streaming event dataclasses: `CrawlStarted`, `PageFetched`, `ModelDecision`, `ResultFound`, `PageSkipped`, `BudgetExhausted`, `CrawlComplete`. Each with `type` and `timestamp` fields.
- Full exception hierarchy: `CharlotteError` and its six subclasses (`CharlotteConfigError`, `CharlotteNetworkError`, `CharlotteTimeoutError`, `CharlotteRedirectError`, `RobotsError`, `AdapterOutputError`, `CharlotteInternalError`).
- Trust level enum.
- `config.py`: reads all `CHARLOTTE_*` env vars with correct defaults and precedence (direct parameter > env var > default).

*Spec ref: §5.2, §6.3, §7, §17, §18*
*Prerequisite: CHAR-001*

---

## Phase 2 — Core Components
*These can be worked in parallel once Phase 1 is done.*

**CHAR-003 — URL Normalizer** *(parallel)*
Implement the URL normalization module. Must be ready before the fetcher, extractor, and provenance check — all three depend on normalized URL equality.

Applies all eight normalization rules in order: lowercase scheme and host, remove default ports, resolve relative URLs, decode safe percent-encoding, remove fragments, normalize path separators, sort query parameters, remove trailing slashes. Applied to `start_url` at crawl init, every extracted URL, every model-output URL before provenance check, every visited-set operation. Result URLs returned to callers are *not* normalized — returned as-found.

Write unit tests covering T-13 and T-14 from the test matrix (fragment deduplication, query param ordering), plus edge cases for each normalization rule.

*Spec ref: §9.5*
*Prerequisite: CHAR-001*

---

**CHAR-004 — Page Fetcher** *(parallel)*
Implement the async `httpx` fetcher with the full timeout policy and redirect policy.

**Timeouts:** Four separate timeouts — `connect_timeout` (10s), `read_timeout` (30s), `render_timeout` (15s, Playwright only), `model_timeout` (30s). Each raises `CharlotteTimeoutError` on expiry. All four configurable via `crawl()` / `find_link()` parameters.

**Redirects:** Follow automatically up to 5 hops. Cross-domain redirects blocked and logged as `CharlotteRedirectError`. Redirect loops detected via visited-set check. Each hop logged with status code and destination. robots.txt rechecked independently on cross-domain redirect within `allowed_domains`. Polite request delay between fetches.

**Playwright path:** Stub only at this stage — raises `CharlotteConfigError` with install instructions if `render_js=True` is passed.

Write unit tests covering T-15 through T-20 from the test matrix.

*Spec ref: §8, §8.1, §8.2*
*Prerequisite: CHAR-001, CHAR-002, CHAR-003*

---

**CHAR-005 — Sanitizer Layer 1: Hidden content stripping** *(parallel)*
Implement the HTML sanitizer. Strips: zero-width and invisible Unicode characters, non-printable control characters (except newline and tab), HTML elements hidden via `display:none` / `visibility:hidden` / `opacity:0` / `font-size:0` / `hidden` attribute, off-screen positioned elements, script and style content, HTML comments, meta tag content fields. Applies the same pass to link anchor text.

Returns sanitized HTML for the extractor. This component has no knowledge of trust levels — it just strips.

Write unit tests covering T-23 from the test matrix (hidden injection text), plus individual tests for each stripping rule.

*Spec ref: §9.1*
*Prerequisite: CHAR-001*

---

**CHAR-006 — Adapter interface and GroqAdapter** *(parallel)*
Define the adapter `Protocol` (or ABC) in `adapters/base.py`. Specifies the exact callable signature and return type — the contract every adapter must satisfy.

Implement `GroqAdapter`: constructs the per-page prompt with goal, navigation_hint, current page summary, available links, visit history, and results-so-far count. Calls Groq API with JSON mode. Handles API errors with one retry and backoff before raising `AdapterOutputError`.

Implement adapter output validation in `core/engine.py` (Charlotte's responsibility, not the adapter's): validates the model response against the strict schema in §6.5 — required fields present, correct types, `result_url` non-null only when `found=True`, URLs are strings, confidence in 0.0–1.0 range, `links_to_follow` is a list. Malformed output triggers retry with reinforced prompt; two failures raises `AdapterOutputError` for that page.

Write unit tests covering T-09 and T-10 from the test matrix.

*Spec ref: §6.1, §6.2, §6.3, §6.4, §6.5*
*Prerequisite: CHAR-001, CHAR-002*

---

## Phase 3 — Integration Layer
*Depends on Phase 2 components.*

**CHAR-007 — Content Extractor**
Implement the content extractor operating on sanitized HTML. Extracts visible text and all links as `{text, url}` pairs resolved to absolute URLs using the normalizer. Filters links to `allowed_domains`, deduplicates (using normalized URLs), and truncates to a token budget.

*Spec ref: §10*
*Prerequisite: CHAR-003, CHAR-005*

---

**CHAR-008 — Sanitizer Layer 2: Input wrapping**
Implement the `<page_content>` wrapping system. Constructs the full model input: system prompt containing goal and navigation_hint established outside the tags; page content enclosed and marked as untrusted data containing no instructions. The preamble text is exactly as specified in §9.2. Used by the engine before every model call.

*Spec ref: §9.2*
*Prerequisite: CHAR-007*

---

**CHAR-009 — Sanitizer Layer 3: Navigation plausibility check**
Implement the plausibility check on model output. Five flag conditions: off-domain links, already-visited links, instruction-mirroring language in `reasoning`, confidence spike on thin content, zero-links with no explanation. Skip-and-log on failure; retry once with reinforced system prompt before abandoning page.

Write unit tests covering T-24 from the test matrix.

*Spec ref: §9.3*
*Prerequisite: CHAR-006, CHAR-008*

---

**CHAR-010 — URL Provenance Check**
Implement the provenance check as the final integrity gate. `result_url` must appear verbatim in the normalized extracted link list — hard rejection on failure, no retry. All `links_to_follow` URLs cross-checked against normalized extracted list; non-matching URLs silently dropped. Logs full detail on any rejection.

Write unit tests covering T-11 and T-12 from the test matrix.

*Spec ref: §9.4*
*Prerequisite: CHAR-003, CHAR-007, CHAR-009*

---

**CHAR-011 — LocalAdapter**
Implement `LocalAdapter` for any OpenAI-compatible local inference endpoint. Configurable `base_url` (default `http://localhost:11434`) and `model_name` (default `llama3:8b`) via constructor arguments or `CHARLOTTE_LOCAL_BASE_URL` / `CHARLOTTE_LOCAL_MODEL` env vars. Satisfies the same adapter contract as `GroqAdapter`. This is a fully supported production path — not a development tool. Document accordingly.

*Spec ref: §6.3, §6.4*
*Prerequisite: CHAR-006*

---

**CHAR-012 — robots.txt handler**
Implement robots.txt fetching, parsing, per-domain caching, and full edge case handling.

- 404 response → no restrictions, crawl proceeds
- Non-200, unreachable, or timeout → `RobotsError`, `found=False`
- Malformed but present → `RobotsError`, `found=False`; no partial parsing
- User-agent matching: `CareNavigator` first, then `*`
- `Crawl-delay` directive respected; uses whichever is larger between directive and Charlotte's default
- Cross-domain redirect: each domain's robots.txt checked independently; permissions do not inherit across domain boundaries
- Enforced in the fetcher before any page is retrieved; respects `respect_robots` parameter

Write unit tests covering T-06, T-07, and T-08 from the test matrix.

*Spec ref: §11, §11.1*
*Prerequisite: CHAR-004*

---

## Phase 4 — Crawl Engine
*The main loop. Requires all Phase 3 components.*

**CHAR-013 — Crawl Engine**
Implement the main crawl loop orchestrating the full pipeline. The engine handles both `max_results=1` (single result, stop on first match) and `max_results=N` or `None` (continue collecting after a match, continue navigating from result pages). When `max_results > 1`, the model may return both `found=True` and non-empty `links_to_follow` — the engine enqueues those links and continues.

Enforces at every step: `max_pages`, `max_depth`, visited-set (using normalized URLs), `allowed_domains`. Returns `CrawlResult` always. Never raises to the caller except `CharlotteConfigError` (pre-crawl) and `CharlotteInternalError` (unexpected state). All other exceptions caught, logged, and handled per the failure table in §12.

Emits streaming events via a generator when `stream=True`; runs silently when `stream=False`.

*Spec ref: §4, §5.1, §12, §17*
*Prerequisite: CHAR-004 through CHAR-012*

---

**CHAR-014 — `find_link()` wrapper**
Implement the `find_link()` public function. Calls the crawl engine with `find_link()`-specific defaults (`max_results=None`, `return_content` forced False). Converts `CrawlResult` to `LinkResult`, populating only the fields defined in §5.2. Emits the same event stream as `crawl()` — no additional event types.

`find_link()` is a thin wrapper. Its value is ergonomic, not structural.

*Spec ref: §5.2*
*Prerequisite: CHAR-013*

---

## Phase 5 — Streaming and Polish

**CHAR-015 — Playwright path** *(parallel with CHAR-016)*
Replace the CHAR-004 stub with the full Playwright implementation. Launches headless Chromium, navigates to the URL, waits for JS to settle within `render_timeout`, captures rendered DOM, passes to the sanitizer. Raises `CharlotteConfigError` with install instructions if the `playwright` package is not installed — immediate, before any crawl begins.

Write unit tests covering T-05 and T-26 from the test matrix.

*Spec ref: §8*
*Prerequisite: CHAR-013*

---

**CHAR-016 — Secret sanitization in exception handling** *(parallel with CHAR-015)*
Implement the exception sanitization layer for the adapter boundary. Exceptions raised by `groq`, `httpx`, `playwright`, or any other dependency that may contain API keys, provider error responses, or request payloads are caught at component boundaries, sanitized, and re-raised as the appropriate `CharlotteError` subclass. Raw third-party exceptions never reach the caller. Debug-level logging of raw exceptions available but off by default.

Write unit tests covering T-25 from the test matrix.

*Spec ref: §12, §18*
*Prerequisite: CHAR-013*

---

## Phase 6 — Testing and Release

**CHAR-017 — Integration test suite**
Write integration tests covering all 30 scenarios in the test matrix (§19). Tests run against Charlotte's public interface — `crawl()` and `find_link()` — not internal components. T-01 and T-02 may use a local HTTP server fixture; T-03 through T-30 use mocked HTTP, model responses, and filesystem.

Organise by scenario number. Each test should be independently runnable and leave no state. This is the acceptance gate before release.

*Spec ref: §19*
*Prerequisite: CHAR-014, CHAR-015, CHAR-016*

---

**CHAR-018 — Answer field (factual extraction)**
Implement the `answer` field through the full stack, as specified in §6.2, §6.5, §7, and §17.

- `models.py`: add `answer: str | None` to `ResultFound`; add `answers: list[str | None] | None` to `CrawlResult`, parallel to `result_urls`
- `adapter_validation.py`: add validation rules for `answer` — optional field, non-null requires non-empty string, `found=False` with non-null `answer` rejected
- Both adapters (`groq.py`, `local.py`): extend the system prompt to explain the `answer` field. Instruction: for factual goals (phone number, address, email, price, hours, name) copy the value verbatim from the page into `answer`; for navigation goals (find a page, find a PDF) leave `answer` null
- `engine.py`: thread `answer` from validated model output through the provenance and plausibility gates into `ResultFound` events and `CrawlResult.answers`
- Integration tests: T-31 (factual goal, model populates `answer`), T-32 (navigation goal, `answer=null`), T-33 (`answer` present with `found=False` rejected)

The `answer` field is optional in the schema — existing adapters that omit it are not broken. The feature is additive.

*Spec ref: §6.2, §6.5, §7, §17*
*Prerequisite: CHAR-017*

---

**CHAR-019 — Graceful failure audit**
Walk every row of the failure table in §12 and verify the exact behaviour in running code — not just that the code exists, but that the failure produces the correct outcome: correct exception logged, page skipped or crawl ended appropriately, `CrawlResult` returned (never raised), API keys absent from log output, `visit_log` free of raw page content. Document any discrepancies found and resolve them.

This is a manual verification step, not a test run. It complements the integration test suite rather than replacing it.

*Spec ref: §12, §13.1*
*Prerequisite: CHAR-017*

---

**CHAR-020 — Packaging and PyPI**
Finalise `pyproject.toml`. Write the full README covering: installation, quickstart with both `crawl()` and `find_link()`, both adapters, all parameters, all env vars, BYOM adapter authoring guide, robots.txt policy, streaming events reference, and error classes reference. Tag `v1.0.0` as `SOME PIG`. Publish `charlotte-crawler` to PyPI.

*Spec ref: §16*
*Prerequisite: CHAR-019*

---

## Summary

| Phase | Tasks | Can parallelise |
|---|---|---|
| 1 — Foundation | CHAR-001, 002 | No |
| 2 — Core Components | CHAR-003, 004, 005, 006 | Yes — all four |
| 3 — Integration | CHAR-007 through 012 | Partial — see dependencies |
| 4 — Crawl Engine | CHAR-013, 014 | No |
| 5 — Streaming & Polish | CHAR-015, 016 | Yes — both |
| 6 — Testing & Release | CHAR-017, 018, 019, 020 | No |

**20 tasks total.**

**Minimum solo path:**
CHAR-001 → 002 → 003 + 005 + 006 (parallel) → 004 → 007 → 008 → 009 → 010 → 011 → 012 → 013 → 014 → 015 → 016 → 017 → 018 → 019 → 020

**Key additions vs. prior decomposition:**
- CHAR-003 (URL Normalizer) is new and foundational — three other components depend on it
- CHAR-004 (Fetcher) is significantly heavier — full timeout policy and redirect policy
- CHAR-006 now includes adapter output validation (§6.5), which is Charlotte's responsibility
- CHAR-012 (robots.txt) now covers all §11.1 edge cases including crawl-delay and user-agent matching
- CHAR-013 (Engine) now handles multi-result mode
- CHAR-014 (`find_link()`) is new
- CHAR-016 (Secret sanitization) is new — exception boundaries need explicit implementation
- CHAR-017 (Integration tests) is now a dedicated task with 30 defined scenarios
