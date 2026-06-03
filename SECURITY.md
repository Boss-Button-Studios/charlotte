# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.0.x   | Yes       |

## Reporting a Vulnerability

Email **jarrod.oz@gmail.com** with the subject line `[charlotte-crawler] Security Vulnerability`.

Please include:
- A description of the vulnerability and its potential impact
- Steps to reproduce
- Any suggested fix or mitigation

We aim to acknowledge reports within 48 hours and provide an initial assessment within 7 days.
Please do not open a public GitHub issue for security vulnerabilities.

## Scope

The following are in scope:

- The `charlotte-crawler` Python library and all modules under `charlotte/`
- The adapters (`GroqAdapter`, `LocalAdapter`) and their prompt construction
- The SSRF protection layer (`validate_url_safety()`)
- The model output sanitization layer (`adapter_validation.py`)
- Dependency vulnerabilities (we run `pip-audit` in CI)

## Known Limitations and Deferred Findings

The following findings were identified in the v1.0 security audit and deferred to a
future release. They do not block current use but should be addressed before Charlotte
is deployed in high-assurance environments.

---

### S-M3 — No wall-clock crawl budget *(Medium, deferred)*

Charlotte enforces a page budget (`max_pages`) and per-request timeouts
(`connect_timeout`, `read_timeout`, `render_timeout`) but not a total elapsed time.
A target site that responds at exactly `read_timeout - 1` seconds per request can pin
a worker for `max_pages × read_timeout` ≈ 10 minutes on defaults. A slow model
endpoint compounds this. The plausibility retry path can double the time budget for a
single page.

**Workaround:** Wrap `crawl()` in `asyncio.wait_for()` if you need a hard wall-clock
limit. A `total_timeout` parameter is planned for a future release.

---

### S-M4 — No defense-in-depth when caller parameters are attacker-influenced *(Medium, deferred)*

Charlotte is a library. Every safety boundary is controlled by a parameter to
`crawl()`/`find_link()`. If an attacker can influence these values, they can disable
safety controls:

- `respect_robots=False` disables robots.txt enforcement
- `allowed_domains=['169.254.169.254']` — the SSRF check blocks this specific address,
  but an attacker supplying `allowed_domains` that Charlotte's SSRF check doesn't
  recognise could succeed
- `confidence_threshold=0.0` accepts any model output
- `max_pages=100000` removes the budget
- `default_delay=0` removes polite delay

**Recommendation:** Validate and sanitize all caller-supplied parameters before passing
them to Charlotte when those values originate from untrusted user input. In particular:

- Always set `allowed_domains` explicitly; do not allow callers to supply arbitrary domains
- Apply your own `max_pages`/`max_depth` ceiling before calling Charlotte
- Do not expose `respect_robots`, `confidence_threshold`, or delay parameters to end users

---

### S-L1 — `<think>` tag stripping regex is not validated for balanced/nested tags *(Low, deferred)*

Both adapters use `_THINK_TAG_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL)` to strip reasoning-model thoughts before JSON parsing. The non-greedy match handles the common case but does not validate that tags are balanced or non-nested. A model output of the form `<think>outer <think>inner</think> outer continued` will strip the inner pair and leave `outer  outer continued` in the content stream.

**Risk:** Low. Requires a compromised model endpoint, which is already game-over for other reasons. Planned fix: add a test asserting nested/unbalanced `<think>` tags produce sensible output, or replace the regex with an explicit state machine.

---

### S-L2 — Sanitizer recursion depth not guarded *(Low, deferred)*

BeautifulSoup's `find_all(True)` walk in `strip_hidden()` may recurse deeply on adversarially nested HTML. Empirical testing with 5 000-deep nesting completes in ≈0.07 s without hitting Python's default recursion limit, but the limit is not explicitly asserted or enforced.

**Risk:** Low on realistic pages. Planned fix: add a test asserting 10 000-deep nesting does not blow up, or add a depth guard via `SoupStrainer`.

---

### DNS Rebinding *(partial mitigation)*

`validate_url_safety()` performs static checks on the URL string before DNS resolution.
A DNS rebinding attack — hostname resolves to a public IP at validation time, then a
private IP at request time — is not detected. Operators in environments where DNS
rebinding is a realistic threat should apply network-level mitigations (e.g. DNS
response filtering, egress firewall rules).

---

### Adapter Client Introspection *(fixed in v1.1.0)*

Previously, `GroqAdapter._client` (a `groq.AsyncGroq` object holding the API key) was
accessible via `vars()`, `__dict__`, or a debugger. Fixed: `__repr__` now excludes
`_client`, and `__getstate__` raises `TypeError` on pickle.

The API key remains accessible via deliberate attribute traversal of the
underscore-prefixed client (e.g. `adapter._client.api_key`). This is the standard Python
convention for private state; defending against it is not feasible without rejecting
Python's introspection model entirely. The defenses above cover the realistic accident
paths (logging, serialization, debugger summaries).

## Security Architecture Notes

- **SSRF protection**: `validate_url_safety()` in `charlotte/core/normalizer.py` blocks
  non-http/https schemes, cloud metadata endpoints, private IP ranges, loopback, and
  `localhost`. It is called before every HTTP request (initial URL and redirect
  destinations) as well as on `start_url` before the crawl generator starts.

- **Model output sanitization**: Adapter output is sanitized in
  `charlotte/core/adapter_validation.py` before any field reaches caller-visible
  structures. Control characters and ANSI escape sequences are stripped from `reasoning`
  (capped at 4 KB) and `answer` (capped at 1 KB). `links_to_follow` is capped at 50
  items. This limits the blast radius of a compromised or adversarial model endpoint.

- **Adapter introspection**: `GroqAdapter.__repr__()` and `LocalAdapter.__repr__()`
  are explicitly implemented to exclude the underlying HTTP client (which may hold API
  credentials). Both adapters raise `TypeError` on `pickle.dumps()` to prevent accidental
  serialization of credentials.

- **Secret-safe logging**: Raw exceptions from `groq`, `httpx`, and `playwright` are
  caught at component boundaries and re-raised as named `CharlotteError` subclasses.
  Exception chains that could carry API keys or provider response bodies are suppressed
  with `raise … from None`. Debug-level logs record only exception types, never values.
