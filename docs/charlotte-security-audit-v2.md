# Charlotte — Security Audit (Follow-up)

**Audit date:** 2026-06-01
**Code audited:** `Boss-Button-Studios/charlotte` `main` (post-security-audit-v1 + Groq playtest)
**v1 audit:** preserved at `docs/charlotte-security-audit.md`
**Test suite:** 563 tests, all passing (up from 537)

---

## Executive summary

**The security audit findings have been addressed comprehensively, and the response went notably beyond what the audit literally asked for.** All Critical and High findings are fixed at the structural level. Two Medium and two Low findings are explicitly deferred with documented risk levels, workarounds, and planned fixes — the right shape for a 1.0 library that wants to be honest about its limits.

Of the twelve findings in security audit v1 (2 Critical, 3 High, 4 Medium, 3 Low):

- **8 fully resolved** — S-C1, S-C2, S-H1, S-H2, S-H3, S-M1, S-M2, S-L3
- **4 deferred with explicit documentation** — S-M3, S-M4, S-L1, S-L2 (all in `SECURITY.md` with risk assessments and either a workaround or a planned fix)

A few things that stand out as going beyond the literal asks:

- **SHA-pinned GitHub Actions** (`actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6`). Supply-chain hardening I didn't ask for. Right call.
- **`persist-credentials: false` on checkout.** Standard hardening that most repos don't bother with.
- **Defense-in-depth on the SSRF check** — fires not only on entry but also on every redirect destination, even when the caller has explicitly placed the internal IP in `allowed_domains`. Empirically verified: a public URL redirecting to `169.254.169.254` with `allowed_domains={"public.example.com", "169.254.169.254"}` is still blocked.
- **DNS rebinding** is explicitly acknowledged as a known partial-mitigation gap in both code docstrings and `SECURITY.md`, with operator-level guidance (DNS response filtering, egress firewall rules). The right move for a library — you can't defend against DNS rebinding inside a single process without re-resolving and pinning, which is an unusual architectural commitment.
- **`SECURITY.md` is genuinely good.** Scope, supported versions, vulnerability disclosure email, deferred-findings register with risk levels, "fixed in v1.1" entries — this is what a maintained security policy looks like. Better than most 1.0 libraries.
- **`scripts/groq_playtest.py`** — the live Groq validation harness that produced the prompt-cap fixes. Reads as evidence that the live testing actually surfaced and fixed real issues (4 new tests, `max_page_chars=7000`, `max_prompt_links=50`, `max_completion_tokens=700`, retry hardening 1→3 for 429 cooldowns). This is exactly the kind of finding the v1 audit predicted live testing would surface.

**Severity counts for the follow-up: 0 Critical, 0 High, 0 Medium, 3 Low.** All three are documentation hygiene rather than functional issues. The library is genuinely ready to be called 1.0 from a security standpoint.

---

## Resolution status of v1 findings

| v1 # | Title | Status | Verification |
|---|---|---|---|
| S-C1 | SSRF wide open | ✅ Resolved | `validate_url_safety()` rejects all v1-flagged URLs; also blocks `localhost.` (trailing dot bypass); fires on redirect destinations even when caller permits the internal IP via `allowed_domains` |
| S-C2 | No max response size | ✅ Resolved | `client.stream("GET", url)` + `aiter_bytes()` + size cap during streaming; default 10 MB; configurable; `CharlotteResponseTooLargeError` |
| S-H1 | Deps unbounded; no scanning | ✅ Resolved | All deps have upper bounds (`<1.0`, `<5.0`, `<3.0`, `<2.0`); `pip-audit` job in CI; SHA-pinned actions; `persist-credentials: false` |
| S-H2 | Reasoning unbounded | ✅ Resolved | 4 KB cap on reasoning, 1 KB on answer, 50-item cap on `links_to_follow`; control chars and ANSI escapes stripped; verified empirically with 10 MB inputs |
| S-H3 | IPv6 normalizer broken | ✅ Resolved | Round-trips correctly with brackets; `http://[::1]/admin` stays `http://[::1]/admin`; port detection works on `http://[2001:db8::1]:8080/x` |
| S-M1 | UA missing contact | ✅ Resolved | `charlotte-crawler/1.0 (+https://github.com/Boss-Button-Studios/charlotte)`; configurable per-fetcher |
| S-M2 | API key on `_client` | ✅ Resolved | `__repr__` excludes `_client`; `__reduce_ex__` blocks pickle with a useful error; parallel treatment given to `LocalAdapter` (even though it doesn't currently hold an API key). Direct attribute access (`adapter._client.api_key`) still works, but the documented exfiltration paths (repr, pickle, debugger inspection of `__dict__`) are closed — see "Acceptable residual" below |
| S-M3 | No wall-clock budget | ⏸️ Deferred | Documented in `SECURITY.md` with `asyncio.wait_for()` workaround and planned `total_timeout` parameter |
| S-M4 | Caller-parameter trust floor | ⏸️ Deferred | Documented in `SECURITY.md` with caller guidance (validate parameters from untrusted input, set `allowed_domains` explicitly, etc.) |
| S-L1 | Nested `<think>` tags | ⏸️ Deferred | Documented in `SECURITY.md`, Low risk noted |
| S-L2 | Sanitizer recursion | ⏸️ Deferred | Documented in `SECURITY.md`, Low risk noted |
| S-L3 | No `SECURITY.md` | ✅ Resolved | Present, 129 lines, comprehensive |

---

## Acceptable residual

A few notes on findings where the resolution isn't 100% mechanical but the chosen line is defensible.

**S-M2 (adapter client introspection).** The fix closes the three paths I'd expect a secret to leak via in practice: `repr()` in a logger, `pickle.dumps()` in a task queue, and `vars(adapter)` in a debugger summary. Direct attribute access (`adapter._client.api_key`) still works, but defending against deliberate introspection of an underscore-prefixed attribute isn't feasible in Python — anyone with that level of debugger access is already inside the trust boundary. The single-leading-underscore convention is the right place to draw the line, and the implemented defenses cover the realistic accident vectors. Worth a brief note in `SECURITY.md` documenting the line, though — see SF-L2 below.

**S-M3 (wall-clock budget) deferred.** Reasonable for now. The `asyncio.wait_for()` workaround is a real escape hatch and the consolidation service shape will be wrapping `crawl()` in a scheduler that has its own timeout anyway. Worth implementing properly before broader adoption, but doesn't block the MVP.

**S-M4 (caller-parameter trust floor) deferred.** Also reasonable. The `safe_mode` opt-in I suggested in v1 is the right shape but adds API surface that may not be earning its keep until there's a concrete caller demanding it. The `SECURITY.md` operator guidance is a sufficient interim answer.

**DNS rebinding accepted as partial mitigation.** Correct architectural call. Re-resolving at fetch time and pinning the IP is an unusual choice and breaks legitimate uses (CDN failover, geo-DNS). Operator-level network controls are the right answer. The acknowledgment in `SECURITY.md` is exemplary.

---

## New findings

Three small ones. All documentation hygiene; none of them functional security concerns.

### SF-L1 — `SECURITY.md` says "fixed in v1.1" but `pyproject.toml` says `1.0.0`

**Spec/code:** `SECURITY.md` line 102: *"Previously, `GroqAdapter._client` (a `groq.AsyncGroq` object holding the API key) was accessible via `vars()`, `__dict__`, or a debugger. Fixed: `__repr__` now excludes `_client`, and `__getstate__` raises `TypeError` on pickle."* The heading says *"(fixed in v1.1)"*.

**`pyproject.toml` line 7:** `version = "1.0.0"`. `charlotte/__init__.py`: `__version__ = "1.0.0"`.

**Why it matters:** Same pattern as v2-M2 in the functional audit — documentation gets ahead of the version label. A user installing `charlotte-crawler==1.0.0` and reading `SECURITY.md` is told the fix landed in `v1.1`, which doesn't exist as a published version. Minor confusion, easily fixed.

**Where to fix:** Pick one. Either: (a) bump the version to 1.1.0 to match what `SECURITY.md` already claims (the changes since 1.0.0 — SSRF, response cap, prompt caps, default-adapter switch — easily justify a minor bump under SemVer); or (b) edit `SECURITY.md` to say "fixed in this release" or "fixed pre-1.0.0" since the audit and fixes happened before any external user installed 1.0.0. Option (a) is more honest to the changelog narrative — there was a meaningful security release after the initial 1.0 cut.

---

### SF-L2 — `SECURITY.md` doesn't document the `adapter._client.api_key` residual

**Code:** Direct attribute access on `adapter._client.api_key` still returns the key in plaintext. The repr/pickle defenses are correctly implemented and tested.

**`SECURITY.md` line 102-105:** Describes the fix but doesn't explicitly state where the line is drawn. A reader could reasonably believe the key is fully hidden, then discover the underscore-prefixed access path and wonder if it's a missed case.

**Why it matters:** Security documentation that's slightly over-promised is harder to maintain trust in. A one-sentence note ("The API key remains accessible via deliberate attribute traversal of the underscore-prefixed client; this is the standard Python convention for private state, and defending against it is not feasible without rejecting Python's introspection model") would set expectations correctly.

**Where to fix:** `SECURITY.md`, under "Adapter Client Introspection". One sentence. Same principle as the DNS rebinding section: name the residual, scope it, explain why it's the right line.

---

### SF-L3 — No Dependabot or equivalent auto-PR for dependency bumps

**Code:** `.github/workflows/ci.yml` includes a `security` job that runs `pip-audit` non-blocking. There's no `.github/dependabot.yml` and no Renovate config.

**Why it matters:** `pip-audit` is detection. Without an auto-PR mechanism, every flagged CVE becomes a manual triage task. For a 1.0 library that's intended to be imported by other code, having upstream bumps land as PRs (which CI then verifies) is the standard hygiene. The bar is low — Dependabot is free, requires no infrastructure, and three lines of YAML.

**Where to fix:** Add `.github/dependabot.yml` with `package-ecosystem: pip` watching `pyproject.toml` weekly, and `package-ecosystem: github-actions` watching `.github/workflows/` weekly. The GitHub Actions watching is the one that catches SHA-pin updates when the actions they point to release security fixes — which closes the loop on the SHA-pinning that's already done.

---

## What's working well

The list of things that went well in this revision is long enough that it's worth being specific about the most impressive parts:

- **The SSRF fix is multi-layered.** Static check at `crawl()`/`find_link()` entry, re-check on every redirect destination, separate cloud-metadata hostname denylist, FQDN-trailing-dot bypass closed, `localhost` blocked regardless of OS resolution. Empirically verified across all v1-flagged attack vectors. The architectural choice to put the validation outside the normalizer (which stays a pure normalization function) is the right separation.
- **Response-size streaming with size cap.** `client.stream()` + `aiter_bytes()` + early termination is the textbook implementation. Default 10 MB matches my recommendation. Configurable for callers who need more (long-form government PDFs, say — relevant for the consolidation service use case).
- **CI hygiene.** SHA-pinned actions, `persist-credentials: false`, separate `security` job that runs `pip-audit`, separate `lint` job with `ruff`. The CI workflow file alone tells me someone thought carefully about supply-chain hardening, not just feature completeness.
- **LocalAdapter got parallel S-M2 treatment** even though it doesn't currently hold an API key. That's defensive coding — the next time someone adds credential support to `LocalAdapter` (e.g. for hosted Ollama deployments with auth), the defenses are already in place.
- **`SECURITY.md` distinguishes between fixes, accepted residuals, and deferred items.** Each deferred item has a risk assessment, a workaround, and a planned fix. This is the right shape for honest 1.0 security documentation.
- **Live testing surfaced real issues** (Groq free-tier 6000 TPM cap, 413 errors on dense pages, 429 retry handling). The prompt caps that came out of that work are themselves security-positive — a 50 MB page can no longer cause Groq token costs to balloon, even if the upstream response cap somehow let it through.
- **The `scripts/groq_playtest.py` harness** captures four representative goals against real sites with JSON-log output and control-char stripping in the harness itself (per CodeRabbit feedback). Reusable validation infrastructure for any future adapter work.

---

## Recommended next steps

Three small items, none of which gate anything important:

1. **Version label hygiene (SF-L1).** Bump to 1.1.0 to honor what `SECURITY.md` already claims. The changes since 1.0.0 are minor-version-worthy under SemVer (new error classes, new parameters with defaults, no breaking changes to public APIs).
2. **Documentation polish (SF-L2).** One sentence in `SECURITY.md` documenting the `adapter._client.api_key` residual.
3. **Dependabot config (SF-L3).** Three lines of YAML. Closes the loop on the SHA-pinning that's already in CI.

After those three, this audit cycle is done from a security standpoint, and you can move on to the consolidation service with confidence that Charlotte itself isn't the weak link.

For the eventual S-M3 / S-M4 work, the natural pairing is to do them together when you start integrating Charlotte into the scheduler — `total_timeout` and `safe_mode` are both more useful when there's a concrete production caller exercising them, and your scheduler will surface exactly the right defaults for both.

Some Pig — earned.
