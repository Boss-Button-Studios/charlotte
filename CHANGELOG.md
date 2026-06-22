# Changelog

All notable changes to `charlotte-crawler` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The package version is independent of the technical specification it implements: 1.2.0
implements spec **v2.0.2** (see `docs/charlotte-spec-v2.0.2.md` in the repository).

## [1.2.0] - 2026-06-22

First public release. Implements the v2.0.2 specification.

### Added

- Goal-directed crawling via `crawl()` and link discovery via `find_link()`, with a
  typed streaming-event API (`stream=True`) and `CrawlResult` / `LinkResult` dataclasses.
- Model adapters: `GroqAdapter` (cloud) and `LocalAdapter` (any OpenAI-compatible local
  endpoint — Ollama, LM Studio, llama.cpp), plus a documented bring-your-own-model
  protocol.
- Goal preprocessor, link ranker, candidate extractor, and destination verifier
  (existence / relevance / full modes).
- Result content delivery: bytes for `document_link` goals by default, with
  `result_to_file` for streamed file delivery and `max_result_bytes` bounding.
- `total_timeout` — an optional wall-clock budget for the whole crawl (checked between
  pages), in addition to `max_pages` and the per-request timeouts.

### Security

Hardening from the June 2026 security audit (passes 1–3, all findings closed):

- **SSRF — connection-target validation.** The page fetcher, destination verifier, and
  robots.txt fetch validate the URL *and* pin the connection to a resolved, policy-checked
  IP (`pinning_transport.py`), closing DNS-name-to-private-IP and DNS-rebinding. Alternate
  IP encodings (decimal/octal/hex) and carrier-grade-NAT ranges are blocked; the verifier
  re-validates every redirect hop and no longer follows a redirect into private/metadata
  space or returns internal bytes.
- **Result files** are written to unique, non-overwriting paths with control-character-safe
  names, and streamed to disk instead of fully buffered in memory.
- **Sanitizer** hidden-region coverage broadened (stylesheet `display:none`/`visibility:hidden`,
  `<noscript>`, off-screen `position`/`text-indent`); residual scope documented.
- **ReDoS** removed from the price extractor (linear-time matching).
- Goal-context validation hardened (synonym-key hard rejection, byte-accurate size cap).

See `SECURITY.md` for the threat model, supported versions, and documented residuals.
