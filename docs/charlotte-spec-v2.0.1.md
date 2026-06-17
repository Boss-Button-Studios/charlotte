# Charlotte — Specification

**Version:** 2.0.1
**Status:** Final (third-party review cycle complete — the second review cycle overall; ready for Phase A implementation)
**Supersedes:** `charlotte-spec-v2.0.md` (preserved as historical reference). v2.0 audit trail at `docs/charlotte-spec-v2.0.md` Appendix A.
**Author:** Boss Button Studios
**Date:** 2026-06-01

---

## Document scope

This document specifies Charlotte v2.0.1 — a patch revision over v2.0 incorporating findings from a third-party technical and security review. The architecture from v2.0 is unchanged; this revision tightens normalization, cache invalidation, structured diagnostics, adversarial-page handling, and forward-compatibility guarantees.

Sections of v1.4 that carry over unchanged remain referenced rather than repeated. The decision log for both review cycles (v2.0 draft → v2.0 → v2.0.1) is in Appendix A.

---

## 1. Thesis

Carries over from v2.0 §1 unchanged.

Charlotte v1 used a navigator model to make every page-level decision. Live testing surfaced that this approach is latency-bound by model calls, brittle on dense pages with small models, and produces invisible/inconsistent reasoning. Charlotte v2's thesis: **the model is most useful when it makes a small judgment between well-prepared alternatives, not when it reasons about the page from scratch.**

Four new components encode this shift: goal preprocessor, link ranker, candidate extractor, destination verifier. Each is pluggable, deterministic by default, and inspectable. The model is called less often, on smaller and more structured inputs.

---

## 2. Goals & Non-goals (delta from v1.4 §2-3)

Carries over from v2.0 §2 unchanged. The six goals (G1-G6) and four non-goals (NG1-NG4) hold as stated.

---

## 3. System overview (delta from v1.4 §4)

Carries over from v2.0 §3 unchanged. Pipeline diagram, trust model, and component relationships hold as stated.

---

## 4. Goal preprocessor

### 4.1 Purpose

Carries over from v2.0 §4.1 unchanged.

### 4.2 `GoalContext` schema

```python
@dataclass(frozen=True)
class GoalContext:
    # Original inputs, preserved verbatim
    goal: str
    navigation_hint: str | None

    # Inferred goal type
    goal_type: Literal[
        "navigation",
        "phone_extraction",
        "date_extraction",
        "address_extraction",
        "price_extraction",
        "document_link",
        "freeform_fact",
    ]
    goal_type_confidence: float

    # Lexical expansion
    synonyms: dict[str, list[str]]
    anchor_terms: list[str]
    negative_terms: list[str]
    regex_hints: list[str]

    # Human-readable interpretation summary — diagnostic use only
    description: str

    # Provenance
    source: Literal["model", "deterministic", "caller_supplied"]
    model_used: str | None
    created_at: datetime
    locale: str

    # NEW in v2.0.1: structured validation diagnostics
    # Populated by §4.5 validation; surfaces silent drops, near-miss
    # validations, and sanitization actions for downstream visibility.
    # Each entry is a short human-readable string with a stable prefix
    # tag (e.g. "regex_dropped:", "sanitization:", "near_miss:").
    validation_warnings: list[str]
```

Field additions from v2.0:

- **`locale`** — promoted from a `crawl()` parameter to a `GoalContext` field. Required because locale affects extractor behavior and must invalidate caches when changed (see §4.6).
- **`validation_warnings`** — structured surface for the silent-drop diagnostics the v2.0 spec only emitted to logs. Entries use stable prefix tags so callers can filter or pattern-match programmatically.

### 4.3 `GoalPreprocessorProtocol`

Carries over from v2.0 §4.3 unchanged.

### 4.4 Default preprocessors

Carries over from v2.0 §4.4 unchanged. `DeterministicPreprocessor` is the library default; `HybridPreprocessor` is the recommended setting.

### 4.5 GoalContext validation

Validation runs on every `GoalContext` before use — whether preprocessor-produced or caller-supplied. The structure of validation is preserved from v2.0; this section adds explicit normalization and timing semantics.

#### 4.5.1 Normalization (NEW in v2.0.1)

Before any validation rule fires, all string-typed fields and dict keys/values in the candidate `GoalContext` pass through canonical normalization:

1. **Unicode NFKC normalization** — collapses compatibility forms (fullwidth/halfwidth, ligatures, various decomposed forms) into canonical equivalents.
2. **Whitespace folding** — runs of whitespace become single ASCII spaces; leading and trailing whitespace stripped.
3. **Casefolding for comparisons only** — the stored values retain original case; the validation comparisons (synonym-key-in-goal, no-negative-overlap) use casefolded forms.

**Why this matters.** Without normalization, equivalent inputs in different Unicode forms are treated as distinct, and a compatibility-form variant can sneak a synonym key past the strict rule — e.g. a goal containing fullwidth `ＣＥＯ` with a preprocessor key of ASCII `CEO`, or vice versa. NFKC collapses these to a canonical form so the strict synonym-keys rule (§4.5.2) compares like with like; casefolded comparison handles capitalization variants.

Note what normalization does **not** do: it does not map cross-script look-alikes (e.g. Cyrillic `С` → Latin `C`). A preprocessor that injects a Cyrillic-`С` `"СEO"` key is still rejected — but by the **membership rule** in §4.5.2, not by normalization: the key does not appear in the Latin-script goal, so it fails the "keys must appear in goal" check. Full Unicode TR39 confusables-mapping is heavier and deferred (§15).

Normalization is **applied symmetrically downstream**: the ranker, candidate extractor, and destination verifier all normalize their inputs the same way before scoring against `GoalContext`. This prevents a normalized validation from passing followed by a non-normalized scoring path missing the match.

When normalization changes a field, the change is recorded in `validation_warnings` with prefix `normalization:` (e.g., `"normalization: 'CEO' field whitespace folded"`).

#### 4.5.2 Structural rules

Unchanged from v2.0:

- `goal_type` must be in the enum.
- `goal_type_confidence` must be in `[0.0, 1.0]`.
- `synonyms` keys must each appear (after normalization, case-insensitive, token-boundary aware) in `goal` or `navigation_hint`. Violations cause hard rejection.
- `synonyms` values are model-inferred; no requirement to appear in goal.
- `anchor_terms` must be tokens or token sequences from `goal` or `navigation_hint` (after normalization).
- `regex_hints` must each compile; invalid patterns are dropped, with each drop recorded in `validation_warnings` as `regex_dropped: <reason>: <pattern>` (NEW in v2.0.1 — previously only logged).

#### 4.5.3 Security rules

Unchanged from v2.0:

- `negative_terms` must NOT overlap (after normalization, case-insensitive) with `synonyms` keys, `synonyms` values, or `anchor_terms`. Hard rejection on violation.
- `negative_terms` must NOT appear (after normalization) in `goal` or `navigation_hint`. Hard rejection on violation.

#### 4.5.4 Sanitization rules

Unchanged from v2.0:

- All string-typed fields stripped of ANSI escape sequences and ASCII control characters (per v1.4 §H2).
- Sanitization actions that removed content are recorded in `validation_warnings` with prefix `sanitization:`.

#### 4.5.5 Size cap and timing (NEW clarification in v2.0.1)

The 4 KB serialized context size cap is applied **after normalization and sanitization, before caching**. The cached value is the post-normalization serialized form. This means:

- The cache key and the cached value derive from the same normalized representation.
- Two semantically equivalent inputs that normalize to the same form share a cache entry.
- The cap measures the post-normalization size, which may be smaller than the raw model output (good — bounds memory).

Oversized post-normalization contexts are hard-rejected; the rejection is reported with the size that triggered it.

### 4.6 GoalContext caching

Cache key (REVISED in v2.0.1):

```
key = hash((
    goal_normalized,
    navigation_hint_normalized,
    preprocessor_class_name,
    preprocessor_model_identifier,
    locale,
    cache_format_version,
))
```

Field additions from v2.0:

- **`locale`** — locale-sensitive extractors (date, price, address) produce different `regex_hints` per locale; cached contexts must invalidate when locale changes.
- **`cache_format_version`** — a module-level constant in Charlotte (currently `CACHE_FORMAT_VERSION = 1`). Bumped on any release that changes preprocessor logic, default regex patterns, locale-sensitive extractor behavior, normalization rules, or validation rules. Coarse but safe — invalidates all cached entries on relevant releases.

Charlotte ships `InMemoryGoalContextCache` as the default. The `GoalContextCacheProtocol` is otherwise unchanged from v2.0.

The cache key uses normalized forms of `goal` and `navigation_hint` so that semantically equivalent inputs share a cache slot.

---

## 5. Link ranker

Carries over from v2.0 §5 unchanged. The ranker receives `GoalContext` after §4.5 normalization, so its BM25 index over synonyms is built on normalized terms; scoring against link text applies the same normalization.

---

## 6. Candidate extractor

### 6.1-6.4

Carries over from v2.0 §6.1-§6.4 unchanged. The `Candidate.nearby_text` and `value`/`raw_value` fields are normalized (per §4.5.1) before scoring.

### 6.5 Model invocation on fact goals (REVISED in v2.0.1)

The v2.0 spec said "zero candidates → model called in freeform mode." The reviewer flagged this as wasteful when the page is blank or an error page. Revised rule, goal-type-aware:

**For `freeform_fact` goal type:**

- Always fall back to model freeform reading on zero candidates. The `freeform_fact` type is the explicit escape hatch for goals where Charlotte's extractors can't apply; the model is meant to read the page.

**For all other fact-extraction goal types (`phone_extraction`, `date_extraction`, `address_extraction`, `price_extraction`, `document_link`):**

- **Zero candidates AND** `len(extracted.text) >= min_text_for_freeform_fallback` (default 200 chars after sanitization): fall back to model freeform reading. The page has content; the extractor may have missed.
- **Zero candidates AND** `len(extracted.text) < min_text_for_freeform_fallback`: skip the page. Emit `PageSkipped` with reason `"insufficient_content_for_fallback"`. No model call.
- One candidate, confident (`score >= confident_extraction_threshold`, default 0.70): use directly without model.
- One candidate, not confident: model is asked to confirm.
- Multiple candidates: top K (default 3) sent to the model with the structured candidate-comparison prompt from v2.0 §6.5.

`min_text_for_freeform_fallback` is a new parameter on `crawl()`/`find_link()` with default 200. Empirical calibration during Phase C.

The "none of these" option on multi-candidate model selection (v2.0 §6.5) is unchanged: the model may return `NONE`, triggering freeform fallback for that page subject to the same content threshold.

---

## 7. Destination verification

### 7.1-7.2

Carries over from v2.0 §7.1-§7.2 unchanged.

### 7.3 Verification modes (REVISED in v2.0.1)

Set via `verify_destination` parameter. Default: `"relevance"`.

- **`"off"`** — no verification; promote chosen URL directly. Matches v1.4 behavior.
- **`"existence"`** — fetch + reject on non-2xx, apparent login walls, empty/error pages. No relevance scoring.
- **`"relevance"`** (default) — existence checks plus BM25 scoring against `GoalContext.anchor_terms` and synonyms. Rejects if relevance score falls below `verify_threshold` (default 0.3).
- **`"full"`** — existence + relevance via the strongest available signal:
  - When `charlotte-crawler[embeddings]` is installed: uses embedding similarity (via `sentence-transformers/all-MiniLM-L6-v2`) as the verification scoring signal. This provides **signal independence** from the BM25 ranker that drove the original candidate selection.
  - When embeddings are not installed: falls back to BM25 with `verify_threshold` raised by 0.15 (stricter threshold compensating for the shared-signal partial circularity acknowledged in §7.6).

The behavior change relative to v2.0: `"full"` mode now means "strongest signal available," not "ranker re-evaluation." This addresses the reviewer's verifier-independence concern by giving callers a path to true signal independence without forcing the embeddings dependency on the default install.

### 7.4-7.5

Carries over from v2.0 §7.4-§7.5 unchanged.

### 7.6 Partial circularity (REVISED in v2.0.1)

The partial circularity acknowledged in v2.0 §7.6 still applies to `"existence"` and `"relevance"` modes (which use BM25). The `"full"` mode with embeddings extras provides signal independence.

Callers prioritizing verification independence should:

1. Install `charlotte-crawler[embeddings]`.
2. Set `verify_destination="full"`.

This is documented in `SECURITY.md` and the README configuration guide.

---

## 8. Public API (delta from v1.4 §5)

### 8.1 `crawl()` and `find_link()` signature changes (REVISED in v2.0.1)

New parameters since v2.0:

```python
async def crawl(
    start_url: str,
    goal: str,
    *,
    # ...all v1.4 and v2.0 parameters retained...

    # NEW in v2.0.1: fact-extraction fallback threshold
    min_text_for_freeform_fallback: int = 200,
) -> AsyncIterator[Event] | CrawlResult: ...
```

`locale` is unchanged at the API level but is now propagated into `GoalContext.locale` so it participates in cache invalidation.

### 8.2 Backward compatibility

Carries over from v2.0 §8.2 unchanged. v1.4-style calls continue to work; v2.0-style calls continue to work. v2.0.1 adds optional parameters with defaults.

---

## 9. Crawl result and failure modes

Carries over from v2.0 §9 unchanged. The five-mode `FailureMode` enum, `verified_candidates` list, `goal_context` field, and assignment priority remain as stated.

Note that `GoalContext.validation_warnings` (NEW in v2.0.1, see §4.2) is part of the `goal_context` returned in `CrawlResult` when `return_goal_context=True`. Callers consuming `CrawlResult` for diagnostics now have access to the structured warning surface without needing log access.

---

## 10. Streaming events (delta from v1.4 §17)

### 10.1-10.3

Carries over from v2.0 §10.1-§10.3 unchanged.

### 10.4 Event schema forward compatibility (NEW in v2.0.1)

Charlotte events are defined as frozen Python dataclasses. The forward-compatibility contract:

- **The `type` field is the dispatch discriminator.** Consumers should pattern-match on the `type` literal to route events.
- **Required field semantics are stable across minor versions.** Required fields will not be renamed, removed, or have their types changed within a major version.
- **Optional fields may be added in minor versions** (e.g., `2.1`, `2.2`). Consumers must tolerate the presence of fields they don't recognize. Pattern: deserialize what you know; ignore the rest.
- **Required field changes or removals require a major version bump.** A breaking event-schema change in a `3.x` release will be explicit in the release notes.
- **New event types may be added in minor versions.** Consumers using an exhaustive `match` on event types should include a default case that ignores unknown event types rather than raising.

This contract applies to all events listed in v1.4 §17 and v2.0 §10, plus any added in v2.0.1+ minor releases.

---

## 11. Security model (delta from v1.4 §13)

### 11.1-11.3

Carries over from v2.0 §11.1-§11.3 unchanged.

### 11.4 Caller-supplied goal text and preprocessor inputs

Carries over from v2.0 §11.4 unchanged.

### 11.5 New error classes

Carries over from v2.0 §11.5 unchanged.

### 11.6 Adversarial page handling (NEW in v2.0.1)

This section names defenses for three classes of adversarial-page attacks that v1.4 and v2.0 already address but the v2.0 spec didn't enumerate explicitly:

**Hidden-region attacks.** Pages may place valid-looking answers in CSS-hidden regions (`display:none`, `visibility:hidden`, `opacity:0`, off-screen positioning), `<meta>` tags, scripts, comments, or HTML attributes not normally rendered. **Defense:** v1.4 §9.1 Layer 1 sanitization strips all of these before content reaches the extractor or ranker. The sanitizer's coverage is the load-bearing defense; v2 inherits it.

**Duplicate-answer attacks.** Pages may repeat the same answer in multiple regions to manipulate the candidate extractor's scoring — for example, listing a misleading phone number 5 times throughout the page to outrank a correct number that appears once. **Defense:** §6.4's `uniqueness` feature reduces the score of candidates appearing many times, preferring distinctive candidates. This is structural: a department-specific phone listed once will outrank a switchboard number listed five times even with otherwise similar features.

**Confusable-character attacks on goal interpretation.** A model preprocessor returning keys that look like the user's goal terms but use different code points (Latin `C` vs Cyrillic `С` in "CEO") could try to inject a key the user never typed. **Defense:** the strict synonym-keys rule (§4.5.2) rejects any key that doesn't appear in the goal after normalization — a cross-script key won't match the goal's script, so it's dropped for non-membership. NFKC normalization plus casefolded comparison (§4.5.1) additionally collapses compatibility-form and capitalization variants, so legitimate keys aren't falsely rejected and fullwidth/ligature variants can't sneak a false match through. Full Unicode TR39 confusables mapping is deferred (§15); page-content confusables are addressed separately below.

**Confusable-character attacks on page content.** Pages may use visually similar but distinct characters in candidate values (e.g., a phone number with a Cyrillic О in place of zero) to evade candidate extractors that expect ASCII. **Status:** v2.0.1 does not defend against this. The candidate extractors' regex patterns are ASCII-anchored, so confusables in candidate values will simply fail to extract — degrading recall but not producing wrong results. Documented in `SECURITY.md` as a known limitation.

### 11.7 Deferred items from earlier audits

Carries over from v2.0 §11.6 unchanged.

---

## 12. Migration path

Carries over from v2.0 §12 unchanged.

---

## 13. Test matrix (delta from v1.4 §19, additions to v2.0 §13)

v1.4 T-01..T-33 and v2.0 T-34..T-60 carry over. v2.0.1 adds:

| # | Title | Verifies |
|---|---|---|
| T-61 | Unicode normalization in validation | A cross-script synonym key (Cyrillic `С`) is rejected by the §4.5.2 membership rule; a fullwidth synonym key (`ＣＥＯ`) is collapsed by NFKC and matches the goal. Confirms the two mechanisms are distinct. |
| T-62 | Cache invalidation — `cache_format_version` | Bumping the constant invalidates all cached entries |
| T-63 | Cache invalidation — locale | Same `(goal, hint, preprocessor, model)` with different locales gets different cache entries |
| T-64 | `freeform_fact` always falls back | Zero candidates on a 50-char page still calls the model for `freeform_fact` goals |
| T-65 | Non-freeform threshold | Zero candidates on a 50-char page emits `PageSkipped("insufficient_content_for_fallback")` for `phone_extraction` etc. |
| T-66 | `verify_destination="full"` uses embeddings when installed | With extras: embedding path. Without extras: BM25 with raised threshold. |
| T-67 | `validation_warnings` on regex drop | Invalid `regex_hints` produces structured warning, not just log line |
| T-68 | Event forward compatibility | Consumer pattern-matching on `type` tolerates an event with an unknown extra field |
| T-69 | Normalization symmetry | Ranker, candidate extractor, and verifier all apply §4.5.1 normalization on their inputs |

Total v2 test additions: 36 (T-34 through T-69).

`scripts/groq_playtest.py` extended to verify:

- `validation_warnings` surface across the playtest corpus (regex-drop warnings should be rare on well-formed goals; high counts signal preprocessor issues).
- `freeform_fact` threshold doesn't suppress legitimate fallbacks.
- Empirical calibration of `min_text_for_freeform_fallback` against real pages.

---

## 14. Sequencing

Carries over from v2.0 §14 unchanged. Three-phase plan (link ranker → preprocessor → candidate extractor + destination verifier) holds.

v2.0.1 patches apply across phases:

- **§4.5.1 normalization** is implemented as a utility module first (`charlotte.core.normalization`) used by all phases. No phase boundary impact.
- **§4.6 cache key changes** land in Phase B (preprocessor phase).
- **§6.5 freeform fallback threshold** lands in Phase C (candidate extractor phase).
- **§7.3 embedding-based `"full"` verification** lands in Phase C.
- **§10.4 forward-compat contract** is documentation only; no implementation work.

---

## 15. Things deliberately not addressed

Carries over from v2.0 §15, with one addition:

- **Unicode TR39 confusables mapping (full skeleton-based homoglyph detection).** NFKC normalization handles fullwidth/halfwidth and most ligatures; casefolded comparison handles capitalization. Cross-script confusables **in page content** (Latin/Cyrillic look-alikes inside candidate values) are NOT detected. *(Cross-script keys on the goal side are a separate matter, handled by the strict membership rule in §4.5.2 — see §11.6.)* Status: deferred. If real confusable attacks surface in deployed crawls, this becomes a candidate for v2.x.

All other deferred items from v2.0 (cross-encoder rerank, feedback-driven feature tuning, hosted/authenticated LocalAdapter, persistent disk cache, web UI, multi-step actions) remain deferred.

---

## 16. Glossary

Carries over from v2.0 §16, with additions:

- **Cache format version** — module-level constant in Charlotte bumped on releases that change preprocessor logic, default regex patterns, locale-sensitive extractor behavior, normalization rules, or validation rules. Coarse invalidation mechanism.
- **NFKC normalization** — Unicode normalization form that decomposes compatibility characters and recomposes canonical forms. Applied to all `GoalContext` strings before validation.
- **Validation warnings** — structured surface in `GoalContext` capturing silent regex drops, normalization changes, sanitization actions, and near-miss validations. Replaces v2.0's log-only diagnostics.
- **Verifier signal independence** — property of destination verification using a different scoring signal than the ranker that selected the candidate. Achieved via `verify_destination="full"` with embeddings extras.

---

## Version History

| Version | Code target | Notes |
|---|---|---|
| 2.0-draft | v2.0.0-alpha | Initial draft for review; 15 open questions inline |
| 2.0 | v2.0.0 | First review cycle complete; destination verification and failure-mode framework added during review |
| 2.0.1 | v2.0.1 | Third-party review cycle complete; Unicode normalization, expanded cache key, structured validation warnings, goal-type-aware freeform fallback, embedding-based full-mode verification, adversarial-page section, event forward-compat contract |

---

## Appendix A — Decision log

Decisions made across both review cycles, in section order. Earlier rows from the v2.0 review preserved verbatim; new rows from the v2.0.1 review marked with their origin.

| Section | Question | Decision | Rationale |
|---|---|---|---|
| §2.3 | Charlotte ships `GoalContextCache`? | Yes — protocol + `InMemoryGoalContextCache` default | Outlet/inlet pattern matches the consolidation service shape. |
| §3.2 | New "Trusted-Model-Inferred" level vs cached Semi-Trusted? | Cached Semi-Trusted, promoted via §4.5 | Simpler; validation does the work the new level would have done. |
| §4.2 | Include `description` field? | Yes, "diagnostic use only" | Auditable; downstream code must not branch on it. |
| §4.2 | Include `goal_type_confidence`? | Yes | Auditing value; drives `HybridPreprocessor` fallback. |
| §4.2 (v2.0.1) | Include `validation_warnings`? | Yes | Silent log-only diagnostics aren't operationally useful; structured surface lets callers triage. |
| §4.2 (v2.0.1) | Promote `locale` to a `GoalContext` field? | Yes | Required for cache invalidation when locale changes. |
| §4.4 | Default preprocessor? | `DeterministicPreprocessor` (default), `HybridPreprocessor` (recommended) | Matches v1.4 adapter pattern. |
| §4.5 | Strict vs loose synonym-keys rule? | Strict — keys must appear in goal | Keys ARE search terms; the ranker uses both keys and values. |
| §4.5 | Adversarial-preprocessor defenses? | Added: no-overlap between negatives and positives, negatives not in goal | Bounds harm to noise rather than active misdirection. |
| §4.5 (v2.0.1) | Unicode normalization on validation? | Yes — NFKC + whitespace folding + casefolded comparison | Closes compatibility-form and case bypasses of the strict synonym-keys rule; cross-script keys are rejected by the rule's membership check, not by normalization. Page-content confusables (TR39) deferred. |
| §4.5 (v2.0.1) | 4KB cap timing? | After normalization, before caching | Cache key and rejection paths must derive from the same form. |
| §4.6 (v2.0.1) | Cache key beyond model identifier? | Expand to include locale + `cache_format_version` constant | Model identifier alone misses logic, regex, and locale changes. |
| §5.3 | Cross-encoder rerank default? | Defer to v2.1 | Quality bar met by BM25 + optional embeddings. |
| §5.4 | Plausibility on model-skip pages? | Deterministic plausibility check | Preserves the v1.4 invariant. |
| §5.5 | Priority queue? | Yes | Ranker's work is wasted if the queue ignores it. |
| §6.3 | Default locale? | `"en_US"`, configurable per crawl | Reflects initial use case; parameter prevents trapping international users. |
| §6.4 | Hand-tuned vs learned candidate weights? | Hand-tuned for v2.0, overridable per crawl; feedback loop is v2.x | Fuzziest part of the design; needs empirical work. |
| §6.5 | "None of these" on candidate selection? | Yes | Try it and see; honest failure better than forced wrong answer. |
| §6.5 (v2.0.1) | Always fall back to model on zero candidates? | Goal-type-aware: `freeform_fact` always; others respect `min_text_for_freeform_fallback` threshold | `freeform_fact` is the explicit escape hatch; threshold prevents wasted calls on blank/error pages for typed extractors. |
| §7 | Final fetch-and-scan validation? | Yes — `DestinationVerifier` with four modes, `"relevance"` default | Closes the gap that v1.4 returned URLs without ever visiting them. |
| §7.3 (v2.0.1) | Verifier independence for v2.0? | `"full"` mode uses embeddings when extras installed | Provides opt-in signal independence without forcing dep. |
| §7.3 | v1.4 compatibility goal? | Same-shape API, better decisions, new diagnostic fields | Bit-for-bit equivalence was never the goal. |
| §9 | `best_candidate_url` vs `verified_candidates`? | Split into two fields | Different concepts: best by score, vs every verification attempt with reason. |
| §9 | In-crawl self-healing on verification failure? | No — pop next priority-queue item, then fail honestly | Charlotte stays narrowly focused; recovery is the caller's job. |
| §9.4 | `goal_context` always returned on failure? | Caller choice via `return_goal_context` (default `True`) | Small object, useful by default; opt-out for high-throughput pipelines. |
| §10 | `CrawlFailed` event vs structured `CrawlComplete`? | Structured `CrawlComplete` | Fewer event types is cleaner; failure_mode field carries the structure. |
| §10.4 (v2.0.1) | Event schema forward compatibility contract? | Documented explicitly: type-field dispatch, optional new fields in minor versions, major bump for breaking changes | Downstream consumers need a deserialization stability promise. |
| §11.6 (v2.0.1) | Explicit adversarial-page section? | Yes — name hidden-region, duplicate-answer, and confusable attacks with defenses | Defenses existed via v1.4 carryover + §6.4 uniqueness, but spec needed to enumerate them. |
| §14 | Three phases, or fold preprocessor + candidate extractor? | Three phases | Phase B's preprocessor unlocks inspectability independent of fact extraction. |
| §15 (v2.0.1) | Confusables (TR39) defense scope? | Deferred to v2.x | NFKC + casefolding cover compatibility-form and case variants; cross-script goal-side keys are caught by the §4.5.2 membership rule. Page-content confusables not currently a known threat. |
