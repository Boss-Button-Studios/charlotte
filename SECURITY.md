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

## Known Limitations

### DNS Rebinding (partial mitigation)

`validate_url_safety()` performs static SSRF checks on the URL string before any DNS
resolution occurs. A DNS rebinding attack — where a hostname resolves to a public IP
at validation time and a private IP at request time — is not detected by this check.
Operators who deploy Charlotte in environments where DNS rebinding is a realistic threat
should apply network-level mitigations (e.g. DNS response filtering, egress firewall
rules).

### Caller-Controlled Parameters

Charlotte is a library. Its `start_url`, `goal`, `allowed_domains`, and other parameters
are trusted inputs — Charlotte assumes they come from the operator, not from end users.
If your application allows untrusted users to supply these values, you must validate and
sanitize them before passing them to Charlotte. Key footguns:

- `start_url` with a data URI or JavaScript URL is blocked by the SSRF check, but
  unusual schemes that Charlotte doesn't recognize will also be blocked.
- `goal` containing adversarial text could influence model reasoning; use
  `navigation_hint` to provide operator-level constraints that appear outside the
  untrusted content boundary.
- `allowed_domains` should always be set explicitly when the caller's input influences
  which site is crawled.

### Wall-Clock Crawl Budget

Charlotte enforces a page budget (`max_pages`) and depth budget (`max_depth`) but does
not enforce a wall-clock timeout on the entire crawl. A `total_timeout` parameter is
planned for a future release. Until then, callers should wrap `crawl()` in
`asyncio.wait_for()` if they need a hard time limit.

### Unbalanced `<think>` Tags (Low Risk)

Charlotte's `<think>`/`</think>` stripping in model output uses a regex that requires a
matching open tag. A model endpoint that emits only a closing tag without an opening one
will have its reasoning prefix stripped, but malformed tag sequences are not currently
tested exhaustively. This is low risk because it requires a compromised or misconfigured
model endpoint.

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
