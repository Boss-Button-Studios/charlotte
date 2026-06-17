# Charlotte — Security Audit

**Audit date:** 2026-06-01
**Code audited:** `Boss-Button-Studios/charlotte` `main` (post-v2 functional audit), version `1.0.0`
**Scope:** Security review beyond what the spec covers — threat modeling, dependency hygiene, resource bounds, secret handling, data flowing out of Charlotte into caller systems
**Method:** Code review with security eyes + empirical probing of the live engine
**Test suite at audit time:** 537 tests passing, no security-specific tests beyond the §9 sanitizer/plausibility/provenance coverage

---

## Executive summary

The spec covers the security risks Charlotte's authors were thinking about: prompt injection from page content, secret leakage in adapter exceptions, robots.txt etiquette. Those are well-handled. What the spec doesn't cover are the security risks of Charlotte being a **library called by other software** — where the threats come from Charlotte's *caller* being attacker-influenced, not from the pages being navigated.

Severity counts: **2 Critical, 3 High, 4 Medium, 3 Low.**

The Critical findings are the two I'd block a 1.0 release for if I were grading strictly:

- **S-C1: SSRF wide open.** Charlotte will happily fetch cloud-metadata endpoints (`169.254.169.254`, `metadata.google.internal`), RFC1918 ranges, loopback, and IPv6 link-local addresses. The fetcher has no allowlist/denylist for IP ranges. For a library that will be deployed in cloud environments and may take user input as `start_url`, this is the largest single risk surface.
- **S-C2: No max response size on the fetcher.** A target site (or a redirect chain ending at one) can return arbitrarily large response bodies. The fetcher reads the full body into memory via `response.text` before handing it off. A malicious or misconfigured target can OOM a Charlotte caller with a single fetch.

The Highs are: dependencies have no upper bounds and aren't pinned, the `reasoning` field is unbounded model output that flows into caller log systems with no size limit, and the IPv6 URL normalizer is structurally broken in a way that creates security-adjacent correctness bugs.

The good news: prompt injection, the thing the spec spends pages of design on, is handled well. The injection threat model is correct, the layered defences work, and the empirical probes I ran couldn't get past them.

---

## Threat model — who attacks Charlotte?

The spec's implicit threat model is: *the target web page is the adversary, the caller and the model are trusted*. That's reasonable for the navigation problem. But Charlotte is a library, not a service, which means the trust model needs an additional axis: **the caller may be untrusted-ish too**, in two senses.

**Sense 1: The caller's *inputs* are attacker-controlled.** Charlotte is going to end up inside services where `goal`, `start_url`, `navigation_hint`, or `allowed_domains` are derived from end-user input. A consolidation service taking "tell me what's new at $URL" is exactly the shape that creates SSRF risk.

**Sense 2: Charlotte's *outputs* flow into other systems.** The `reasoning` field, the `note` field on `LinkResult`, the URL strings in `result_urls` — these flow into the caller's logs, dashboards, databases, and possibly back into other LLM calls. A page that successfully evades Charlotte's input sanitization controls Charlotte's outputs, and through them, controls something one layer downstream.

The spec considers neither sense. Everything below is what falls out of taking them seriously.

---

## Critical findings

### S-C1 — No SSRF protection: cloud metadata and internal addresses are reachable

**Empirically verified:**

```
normalize_url('http://169.254.169.254/latest/meta-data/')  → accepted
normalize_url('http://metadata.google.internal/')          → accepted
normalize_url('http://127.0.0.1:8080/')                    → accepted
normalize_url('http://10.0.0.1/')                          → accepted
normalize_url('http://192.168.1.1/')                       → accepted
normalize_url('http://localhost/admin')                    → accepted
normalize_url('file:///etc/passwd')                        → accepted
normalize_url('gopher://evil.com:25/')                     → accepted
crawl(start_url='http://169.254.169.254/', goal='...')     → no eager rejection
```

The normalizer rejects unparseable URLs but accepts every dangerous one. The fetcher will then attempt to fetch them. The only thing protecting the caller is `allowed_domains` — but `allowed_domains` is *caller-supplied*, so when the caller is the attacker (or is forwarding attacker input), it doesn't help.

**What this enables:**

- **AWS metadata exfiltration.** `start_url=http://169.254.169.254/latest/meta-data/iam/security-credentials/`, `goal="find the credentials JSON"`, `allowed_domains=["169.254.169.254"]`, `respect_robots=False`. Charlotte fetches it, the model evaluates it, the credentials end up in `result.answers` or `extracted.text`. Same shape works for GCP, Azure, DigitalOcean metadata.
- **Internal network reconnaissance.** Any host on the caller's RFC1918 network is reachable. Use Charlotte to crawl an internal Jira or wiki.
- **Localhost service exploitation.** Anything running on `127.0.0.1` (admin panels, debug endpoints, the caller's own database admin UI) is reachable.
- **Non-HTTP scheme abuse.** `file:///etc/passwd` and `gopher://` are accepted by the normalizer. httpx itself will reject most non-http schemes at fetch time, but `file://` is a known footgun on some httpx versions. Better to reject at the boundary.

**Why this matters in Charlotte's context specifically:** the consolidation-service shape you described — periodic recrawls of registered targets — is exactly the shape that ends up taking URLs from a database, a config file, or in the worst case a web form. Every additional layer between "end user types something" and "Charlotte fetches it" is a chance for someone to inject a metadata URL.

**Severity:** Critical. This is the largest single risk surface and it's the one a security review of any HTTP-fetching library will flag first.

**Where to fix:** `charlotte/core/normalizer.py` is the right boundary — it's the single chokepoint every URL passes through before fetching. Add a validation step that:

- Rejects non-`http`/`https` schemes outright (the extractor already does this for *links*, but the normalizer doesn't for `start_url` or for `result_url`).
- Rejects hostnames that resolve to (or are literal) IPs in the RFC1918, loopback, link-local, multicast, or reserved ranges. The `ipaddress` stdlib module has `is_private`, `is_loopback`, `is_link_local`, `is_multicast`, `is_reserved` properties. Do the check on `urlsplit().hostname` and on the resolved IP after DNS (to defeat DNS rebinding — a hostname that resolves to a public IP at validation time and a private IP at fetch time).
- Rejects literal cloud-metadata hostnames (`metadata.google.internal`, `metadata.azure.com`, etc.) regardless of resolution. Maintain a small denylist.
- Provides an explicit opt-in (`allow_private_addresses=True` parameter on `crawl()`/`find_link()`) for callers who legitimately need to crawl their own internal infrastructure. This is the same shape as `respect_robots=False` — the spec already accepts this pattern.

This is one new function (`_validate_url_safety`), one call site in the normalizer, and one parameter on the public API. Maybe 50 lines including tests. Should be a `CharlotteSSRFError` subclass of `CharlotteConfigError` so the failure mode is distinguishable.

---

### S-C2 — Fetcher has no maximum response size

**Code (`charlotte/core/fetcher.py` lines 176-189):** After redirects resolve, the fetcher reads the entire response body via `response.text`:

```
if not response.is_redirect:
    try:
        html = response.text
    except httpx.DecodingError as exc:
        raise CharlotteNetworkError(...)
    return FetchResult(url=current_url, html=html, ...)
```

There's no `Content-Length` check, no streaming with a byte ceiling, no `httpx` size limit configured. Whatever the server sends, Charlotte reads.

**Empirically verified:** the sanitizer and extractor handle a 1MB page in ~6 seconds. They scale roughly linearly. A 100MB page would take ~10 minutes of pure CPU per fetch, and would be held entirely in memory three times over (raw HTML → parsed soup → cleaned soup → extracted text). The Playwright path has the same problem — `page.content()` returns the full rendered DOM.

**What this enables:**

- A target site can OOM the Charlotte caller with a single response. Doesn't even have to be malicious — a misconfigured CDN returning a 500MB error page would do it.
- A slow-streaming response of unbounded size (chunked transfer encoding, 1KB/s, 1GB total) pins the worker for hours without triggering `read_timeout` because data keeps trickling in. (Whether `read_timeout` is per-chunk or total depends on the httpx version; worth checking.)
- A redirect chain ending at a known-huge URL — for example, the start_url is in allowed_domains, redirects to a different page in allowed_domains that serves a huge body — bypasses any caller-level URL screening.

**Severity:** Critical for a 1.0 library deployed in automated pipelines. Less severe for a one-off interactive use case, but the spec positions Charlotte as the former ("safe to call in automated pipelines").

**Where to fix:** `charlotte/core/fetcher.py`. Two changes:

1. Add a `max_response_bytes` parameter to `PageFetcher.__init__` and to `crawl()`/`find_link()` (default something like 10 MB — enough for any reasonable HTML page, small enough to bound damage). Plumb it through.
2. In the httpx path, switch from `await client.get(url)` to a streaming pattern: `async with client.stream("GET", url) as response:`, then `async for chunk in response.aiter_bytes(): ... if total > limit: raise`. Same in the Playwright path — check `len(html)` after `page.content()` returns and raise if it exceeds the limit. (Playwright doesn't easily support streaming caps, but the post-hoc length check at least bounds memory damage to ~2× the limit instead of unbounded.)

New error class: `CharlotteResponseTooLargeError(CharlotteNetworkError)`.

---

## High findings

### S-H1 — Dependencies have no upper bounds; no automated vulnerability scanning

**Code (`pyproject.toml`):**

```
dependencies = [
    "httpx>=0.27.0",
    "beautifulsoup4>=4.12.0",
]
playwright = ["playwright>=1.40.0"]
groq = ["groq>=0.5.0"]
```

Lower bounds only, no upper bounds. No `requirements.txt` lock file, no `uv.lock`, no `pip-compile`-generated pin.

**Empirically:** `pip-audit` against the current installed versions reports zero known vulnerabilities, so we're not sitting on a known CVE today. That's not the issue.

**The issues:**

1. A new major version of any dep can break Charlotte in production at install time. `httpx 1.0` will eventually happen and may not be API-compatible. For a library being depended on by other libraries, unbounded versions propagate transitive instability.
2. There's no documented "we checked the dependency tree" cadence. For a 1.0 release that aspires to be `Production/Stable` in `pyproject.toml`, dependency hygiene should have a story.
3. `groq>=0.5.0` is particularly loose — the SDK is young and the API surface has shifted. A user installing `charlotte-crawler[groq]` fresh today gets `groq==1.4.0`, which works, but only because nothing meaningful broke between 0.5 and 1.4.

**Severity:** High because it compounds with the other findings — when you do find a vulnerability in a dep, the upgrade path is whatever the dep's authors decided, with no tested version range to fall back to.

**Where to fix:** Three small pieces:

1. Add upper bounds to all deps: `"httpx>=0.27.0,<1.0"`, `"beautifulsoup4>=4.12.0,<5.0"`, etc. Bumping the upper bound becomes a deliberate decision in a future release.
2. Wire `pip-audit` (or `safety`) into CI as a non-blocking job. Generates a heads-up when a CVE drops in a transitive dep.
3. Add a `SECURITY.md` documenting the dependency cadence and where to report issues. GitHub recognizes this file and surfaces it in the repo.

---

### S-H2 — `reasoning` field is unbounded model output flowing into caller logs

**Code:**

- `charlotte/models.py` `VisitLogEntry.reasoning: str` — no size limit
- `charlotte/models.py` `ModelDecision.reasoning: str` — emitted in stream events
- `charlotte/core/adapter_validation.py` validator accepts arbitrary-size reasoning (empirically verified: 10 MB reasoning passes validation)

The model's `reasoning` output is freeform text. It enters Charlotte semi-trusted, never gets promoted to trusted, but **also never gets sanitized for downstream consumers**. It flows directly into:

- `CrawlResult.visit_log` — which callers will log, persist, or display
- `ModelDecision` stream events — which callers will log or pipe to dashboards
- `PageSkipped.reason` — which embeds plausibility flag detail that may itself include matched-pattern text

**Why this matters:**

A page that successfully influences the model's `reasoning` (even without flipping the plausibility check) controls a string that ends up in the caller's logs. Concrete attacks this enables:

- **Log injection.** A page convinces the model to write `reasoning="navigation OK\n[CRITICAL] Database offline, paging on-call"`. If the caller writes `visit_log` to a structured log system that doesn't escape newlines, the fake alert lands in monitoring.
- **ANSI/terminal escapes.** If the caller prints `visit_log` to a terminal (e.g. during debugging), `reasoning` containing escape sequences can rewrite earlier terminal output, hide content, or in extreme cases (xterm) execute commands.
- **Downstream prompt injection.** If the caller pipes `reasoning` into another LLM call ("summarize what Charlotte found"), the page now indirectly controls that second LLM's input. Charlotte's own input wrapping (Layer 2) doesn't extend past the first model call.
- **Storage exhaustion via the visit_log.** Nothing caps the size of `reasoning` per page or the cumulative size of `visit_log` per crawl. A long crawl with verbose-reasoning models can produce visit logs of arbitrary size, which a caller may then write to a database.

**Severity:** High because it's a real injection vector into systems that *aren't Charlotte* — and Charlotte has no way to know what those systems are. The defence has to live in Charlotte because the caller can't reasonably be expected to anticipate every transformation the model might apply to its own reasoning string.

**Where to fix:** Two boundaries.

1. **In the validator (`adapter_validation.py`):** Cap `reasoning` and `answer` at sensible sizes (e.g. 4 KB for reasoning, 1 KB for answer — both far more than legitimate values need). Truncate with a marker (`... [truncated]`) rather than reject, so a verbose model doesn't lose the whole decision. Similarly cap `links_to_follow` list length (e.g. 50 items max).
2. **In the validator or in a new sanitization step:** Strip ANSI escape sequences, normalize newlines (replace `\r` and `\n` runs with a single space — visit-log reasoning shouldn't need multi-line formatting), reject NUL bytes. This is one regex per concern.

Document in the spec that `reasoning` is sanitized of control characters and bounded — the spec's §13.3 trust model talks about promoting model output but doesn't address sanitizing it for caller-side safety.

---

### S-H3 — IPv6 URL normalization is structurally broken

**Empirically verified:**

```
normalize_url('http://[::1]/admin')              → 'http://::1/admin'         [unparseable]
normalize_url('http://[2001:db8::1]:8080/path')  → 'http://2001:db8::1:8080/' [ambiguous, unparseable]
normalize_url('http://[fe80::1%25eth0]/')        → 'http://fe80::1%25eth0/'   [unparseable]
```

The normalizer uses `urlsplit().hostname` to extract the host, which strips the brackets RFC 3986 requires around IPv6 literals. Then `urlunsplit` reassembles without putting the brackets back. The output is a string that `urlsplit` cannot re-parse correctly — port detection in particular goes wrong, since the colons in `2001:db8::1:8080` are indistinguishable from a port separator.

**What this affects:**

1. **Visited-set deduplication breaks for IPv6 hosts.** Since the normalized form isn't round-trippable, two equivalent IPv6 URLs may not compare equal after one pass through the normalizer.
2. **The provenance check's URL comparison breaks.** Same root cause.
3. **Security-adjacent:** if Charlotte is used against IPv6 internal infrastructure (link-local, ULA), the brokenness combines with S-C1 to make the threat surface harder to reason about — you can't trust the normalized form to compare correctly against an SSRF denylist.

**Severity:** High because it's a correctness bug in the URL layer that the rest of the engine depends on. Not exclusively a security issue, but security-relevant because the layer is supposed to be a trust boundary.

**Where to fix:** `charlotte/core/normalizer.py`. Reassemble the netloc explicitly when the parsed hostname looks like an IPv6 literal (contains `:`), wrapping it in brackets: `[{hostname}]`. Add tests for the three cases above and for the round-trip property `normalize_url(normalize_url(x)) == normalize_url(x)` for IPv6 inputs.

---

## Medium findings

### S-M1 — User-Agent has no contact information

**Code (`charlotte/config.py` line 15):** `HTTP_USER_AGENT: str = "CareNavigator/0.1"`

**Why this matters in security context:** Every modern crawler-etiquette guide recommends a user-agent that includes either a URL or an email so an operator can contact you before they ban you. Charlotte's UA is also stale — it says `0.1`, the package is `1.0.0`. Operationally this means: when (not if) Charlotte hits a misconfigured rate limiter or trips bot detection, the operator has nothing to go on but the UA. Best case they block `CareNavigator/*` globally; worst case they blackhole the IP. Either is bad for the consolidation service that's going to be hitting government sites on a schedule.

**Severity:** Medium. Not a vulnerability; an operational hardening item with security-adjacent implications (becomes a denial-of-service against yourself).

**Where to fix:** `charlotte/config.py`. Make `HTTP_USER_AGENT` configurable via env var (`CHARLOTTE_USER_AGENT`) and parameter on `crawl()`/`find_link()`. Default to `CareNavigator/1.0 (+https://github.com/Boss-Button-Studios/charlotte)`. Update the spec's §11 user-agent matching to mention the new default. Tests checking for `CareNavigator` (case-sensitive prefix) will still work.

---

### S-M2 — GroqAdapter exposes API key on the client object

**Empirically verified:** after `adapter = GroqAdapter()` with `GROQ_API_KEY=gsk_...`:

```
adapter._client.api_key  →  'gsk_SECRET_KEY_DO_NOT_LEAK_12345'  [accessible plain-text]
```

The adapter itself doesn't expose the key in `__repr__` or in any direct attribute on `GroqAdapter`. But the underlying `AsyncGroq` client stores `api_key` as a public attribute, and `adapter._client` is reachable via standard Python attribute access.

**What this affects:**

- Anything that pickles or `repr()`s the Groq SDK client (some debuggers, some structured loggers, some test frameworks)
- Any caller that walks adapter attributes for introspection
- Tracebacks that include the client object in their locals (unlikely but possible — Python tracebacks can include locals when sys.excepthook is customized)

**Severity:** Medium. Not a direct leak — requires deliberate introspection — but the adapter is a public API surface and "the key is hidden" isn't quite true.

**Where to fix:** Two options. (a) Override `__repr__` and `__getstate__` on `GroqAdapter` to suppress the client. (b) Store the client behind a property that errors on serialization attempts. (a) is simpler and consistent with how `__repr__` is being used as the secrets boundary elsewhere. Add a regression test that asserts the API key string doesn't appear in `repr(adapter)`, `pickle.dumps(adapter)` (should raise, not succeed silently), or `str(adapter.__dict__)`.

---

### S-M3 — No wall-clock budget on the crawl as a whole

**Code:** The engine enforces `max_pages` and per-request timeouts (`connect_timeout`, `read_timeout`, `render_timeout`, `model_timeout`). It does not enforce a total-elapsed budget.

**What this enables:**

- A target site responding at exactly `read_timeout - 1` seconds per request will let Charlotte run for `max_pages × read_timeout` ≈ 10 minutes on defaults, pinning a worker without ever tripping any timeout.
- A model endpoint that responds slowly-but-validly compounds the same effect.
- An adversarial site combining slow responses with the existing plausibility-retry path can double-charge the time budget (page + retry) without anything looking wrong from inside Charlotte.

**Severity:** Medium. Not exploitable in isolation, but a real DoS vector in adversarial contexts. Especially relevant for the consolidation service, where one slow target shouldn't be able to delay the rest of the schedule.

**Where to fix:** Add `total_timeout: float | None = None` parameter to `crawl()`/`find_link()`. Inside `_crawl_core`, after each iteration of the main loop, check `monotonic() - start_time` against the budget. If exceeded, set `budget_exhausted = True`, emit a `BudgetExhausted` event with a reason field that distinguishes "time" from "pages," and break the loop cleanly. Existing tests don't need to change since `total_timeout` defaults to None.

---

### S-M4 — `respect_robots=False` and `allowed_domains` provide no defense-in-depth when the caller is compromised

**Code review observation:** Every safety boundary in Charlotte is controlled by a parameter to `crawl()`/`find_link()`. If the caller is hostile or attacker-influenced, every safety can be turned off:

- `respect_robots=False` disables robots.txt enforcement
- `allowed_domains=['169.254.169.254']` permits cloud metadata
- `confidence_threshold=0.0` accepts any model output
- `max_pages=100000` removes the budget
- `default_delay=0` removes polite delay

This is the right shape for a library — the caller has to be able to override defaults — but it means **there is no Charlotte-level safety floor**.

**What this matters for:** the consolidation service shape, where Charlotte may be invoked with parameters derived from configuration that's edited by humans, possibly via a UI. A single bad config row can turn Charlotte into an SSRF tool that ignores robots.txt and hammers a target.

**Severity:** Medium. It's a shape issue rather than a bug. Worth being explicit about in the spec, and worth offering an opt-in "safe mode" that locks the dangerous overrides off.

**Where to fix:** Optional. One pattern: add a `safe_mode: bool = False` parameter (or environment variable `CHARLOTTE_SAFE_MODE`) that, when true, forces `respect_robots=True`, applies SSRF protection regardless of `allowed_domains`, caps `max_pages` and `default_delay` at sane values, and rejects `confidence_threshold < 0.5`. Deployment environments can set the env var and not worry about config drift.

Alternative: don't add this; document clearly in `SECURITY.md` that callers are responsible for parameter validation and that `respect_robots=False` + `allowed_domains` is a foot-gun pair.

---

## Low findings

### S-L1 — `_THINK_TAG_RE` in adapters strips reasoning-model thoughts but doesn't validate they're balanced

**Code (`charlotte/adapters/local.py` lines 44-48, `groq.py` lines 25-26):** The `<think>...</think>` stripping uses non-greedy regex with `re.DOTALL`. A model emitting `<think>` content `<think>` content `</think>` (nested or unbalanced) will have the outer one stripped correctly but the inner content may survive. A malicious model output could carry instructions hidden in the survived fragment.

**Severity:** Low. Requires a compromised model endpoint, which is already game-over for other reasons. Worth noting because the regex is presented as a security-adjacent boundary in the docstrings.

**Where to fix:** Either add a test confirming nested/unbalanced think tags produce sensible output, or replace the regex with a small explicit state machine. Either is fine.

---

### S-L2 — Sanitizer recursion depth limit not asserted

**Empirically:** strip_hidden handles 5000-deep nesting in 0.07s without issue. Python's default recursion limit is 1000 for direct calls; BeautifulSoup's `html.parser` is iterative for parsing but the `find_all(True)` walk may go recursive depending on internals. Worth a guard.

**Severity:** Low. The empirical test didn't break anything.

**Where to fix:** Add an `sys.setrecursionlimit` guard or — better — set a `bs4` maximum depth via `SoupStrainer` for the rare pathological page. Or just add a test asserting 10,000-deep nesting doesn't blow up.

---

### S-L3 — `SECURITY.md` doesn't exist; no documented vulnerability disclosure process

The repo has `LICENSE`, `README.md`, `CLAUDE.md`, but no `SECURITY.md`. GitHub auto-renders `SECURITY.md` as the "Security" tab and surfaces it when a researcher tries to file a CVE report.

**Severity:** Low — process item, not a vulnerability.

**Where to fix:** Add `SECURITY.md` at repo root. Standard template: supported versions, how to report (email, not public issue), expected response timeline, scope. Github has a generator.

---

## What's working well

Worth saying explicitly — the security spec is genuinely thoughtful and a lot of the defenses work as designed:

- **Layered injection defenses (sanitizer / plausibility / provenance / new answer-content-gate).** Empirically robust against the simple attack patterns I tried. The provenance check in particular is a real differentiator from other agents in the space.
- **API key suppression in adapter exceptions.** The `raise ... from None` pattern and `logger.debug(..., type(exc).__name__)` discipline are correctly applied throughout the adapters. The S-M2 finding is about a different surface (client object introspection), not about logging.
- **`allowed_domains` matching is strict.** Suffix attacks, subdomain attacks, and case-confusion attacks are all correctly rejected. Empirically verified.
- **Plausibility regexes do not have ReDoS pathologies.** Empirically verified against pathological 10K-char inputs — all eight patterns return in < 1ms.
- **Dependency tree is small and current.** No known CVEs in `httpx`, `beautifulsoup4`, `groq`, `playwright` at audited versions. `pip-audit` clean.
- **Polite-delay and budget controls protect target sites.** The spec explicitly considers target-site welfare, which is unusual for this class of library.
- **The trust model in §13.3 is well-articulated** and the four levels (Trusted / Untrusted / Semi-Trusted / Promoted) correspond to real code boundaries. The audit findings are mostly about extending this model to caller-facing concerns, not about fixing it.

---

## Recommended order of operations

For an MVP that bundles this with the live Groq validation:

1. **S-C1 (SSRF protection) and S-C2 (max response size).** These are the two findings that turn "0.1 alpha that says 1.0 on the tin" into "actually 1.0." Roughly half a day's work combined. Both have natural Charlotte-shaped fixes (one new validation function in normalizer; one streaming-with-cap rewrite in fetcher). Both deserve their own error classes (`CharlotteSSRFError`, `CharlotteResponseTooLargeError`).

2. **S-H1 (dependency upper bounds + pip-audit in CI).** Half an hour of `pyproject.toml` edits plus a GitHub Action. While you're there, add `SECURITY.md` (S-L3).

3. **S-H2 (bound the reasoning/answer fields and strip control chars).** Small change in `validate_adapter_output` plus tests. The spec needs a paragraph documenting that semi-trusted model output is sanitized before promotion to caller-visible fields.

4. **S-H3 (IPv6 normalizer).** Small targeted fix, but its absence is a latent bug that compounds with S-C1. Worth doing in the same security pass.

5. **S-M1 (User-Agent with contact)** during the Groq validation. Government webmasters are exactly the audience this is for.

6. **S-M2, S-M3, S-M4, S-L1, S-L2.** Lower-urgency. Fold into the next release.

After S-C1, S-C2, S-H1, S-H2, S-H3, and S-M1, Charlotte is genuinely ready to be a 1.0 library that other teams import. Before that, the `Production/Stable` classifier in `pyproject.toml` is aspirational.

The good news: none of these findings change Charlotte's architecture. They're all additions at existing boundaries — the normalizer gets stricter, the fetcher gets a size cap, the validator gets length limits. The shape of the library is right; it just needs to be a bit more paranoid about who's calling it.
