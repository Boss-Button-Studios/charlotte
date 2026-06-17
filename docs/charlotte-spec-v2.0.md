# Charlotte — Specification

**Version:** 2.0
**Status:** Final (review complete; ready for implementation in three phases per §14)
**Supersedes:** `charlotte-spec-v1.4.md` (preserved as historical reference)
**Author:** Boss Button Studios
**Date:** 2026-06-01

---

## Document scope

This document specifies Charlotte v2.0 as a delta from v1.4. Sections of v1.4 that carry over unchanged are referenced, not repeated — specifically the URL normalizer, sanitizer (§9.1), robots.txt policy (§11), error class hierarchy (§18), and the streaming-events foundation (§17).

This spec was produced via a deliberate spec-first review cycle. The decision log for the review is in Appendix A — every contentious design call in v2 has a recorded rationale.

---

## 1. Thesis

Charlotte v1 used a navigator model to make every page-level decision: which links to follow, whether the goal is satisfied, what answer to return, what its confidence is. The model was on the critical path for every page, doing all the work — finding candidates, contextualizing them, weighing them against the goal, producing structured output — in one pass.

Live testing with 7B and 14B local models surfaced three correlated problems with this shape:

1. **Latency is dominated by model calls**, not fetches. A crawl with N pages takes roughly N × (fetch + sanitize + model). The fetch and sanitize stages are fast and predictable; the model stage is slow and variable.
2. **Smaller models do worse on larger inputs.** A 7B model handed 47 links and a 50KB page summary frequently underweights important information, confuses the goal with similar-but-wrong content, or hallucinates fields it doesn't have evidence for.
3. **Model reasoning is invisible and inconsistent.** When the model picks the wrong link, the failure is opaque. When it picks the right link, the reasoning may be different on each page of the same crawl. There is no artifact capturing "what does Charlotte think this goal means" — the interpretation lives only in transient model calls.

Charlotte v2's thesis: **the model is most useful when it makes a small judgment between well-prepared alternatives, not when it reasons about the page from scratch.** Most of the work — extracting candidates, scoring relevance, applying goal semantics — can be done deterministically, with the model called only when deterministic methods can't decide.

This shifts Charlotte from a model-in-the-loop architecture to a **deterministic-first architecture with model fallback**. The model is an exception handler, not the main thread.

Four new components encode this shift:

- **Goal preprocessor** — runs once per crawl, expands the goal into a structured `GoalContext` (synonyms, anchor terms, negative terms, goal type, regex hints).
- **Link ranker** — runs once per page for navigation goals, scores links against the goal context, presents the top N to the model or short-circuits the model when confidence is high.
- **Candidate extractor** — runs once per page for fact-extraction goals, finds candidate values with structural features. The model picks among 2-3 contextualized candidates rather than reading the whole page.
- **Destination verifier** — runs once per result candidate, fetches and scans the proposed destination link before promoting it to a returned result. Makes "the URL we returned actually leads somewhere relevant" a first-class invariant.

Each new component is **pluggable** (Protocol-based, same shape as adapters), **deterministic by default** (no ML dependencies in the default install), and **inspectable** (its outputs are part of the public crawl result).

The model interface from v1.4 is unchanged. Existing adapters work without modification. The model is now called less often, with smaller and more structured inputs.

---

## 2. Goals & Non-goals (delta from v1.4 §2-3)

### 2.1 New goals

- **G1.** Reduce per-page latency by an order of magnitude on common goal shapes, primarily by eliminating model calls when deterministic methods are confident.
- **G2.** Make Charlotte's interpretation of a goal **inspectable** — `GoalContext` is part of the public API, available in `CrawlResult` and streaming events.
- **G3.** Support fact-extraction goals (phone numbers, dates, addresses, prices, document links) with first-class deterministic extractors and a curated-candidates model interface, not freeform page-reading.
- **G4.** Make the new components **pluggable** via Protocols, with sensible deterministic defaults.
- **G5.** Preserve the v1.4 security model. New components must not weaken Layer 1 sanitization, Layer 2 input wrapping, Layer 3 plausibility, URL provenance, or the answer content gate. They may *extend* the model where new trust boundaries are introduced.
- **G6.** Return **structured failure modes** so a caller can distinguish "no candidates found" from "candidates rejected" from "budget exhausted" from "site unavailable." Honest, diagnosable failures are a first-class concern.

### 2.2 Goals carried over unchanged from v1.4

§2 of v1.4 in full. The library shape (no service, no daemon, no persistent storage), the polite-by-default stance, and the BYOM commitment all hold.

### 2.3 New non-goals

- **NG1.** Charlotte v2 does not train any models. All ML components use pre-trained models.
- **NG2.** Charlotte does not provide persistent goal-context caching to disk. Callers may supply a `GoalContextCache` (in-memory default ships with the library) or pass a context directly via the API.
- **NG3.** Charlotte does not handle JavaScript-rendered candidate extraction beyond what Playwright already provides at the fetch layer.
- **NG4.** Charlotte does not self-heal across crawl boundaries. When a crawl fails, the failure is reported honestly with structured reasons; recovery (re-preprocess, retry with relaxed parameters, alert operator) is the caller's responsibility.

---

## 3. System overview (delta from v1.4 §4)

### 3.1 Pipeline

```
caller calls crawl()
    ↓
preprocess goal → GoalContext (cached, caller-supplied, or fresh)
    ↓
for each page in priority queue:
    fetch → sanitize → extract (v1.4 §8-10, unchanged)
    ↓
    [navigation goal path]                [fact-extraction goal path]
    rank links against goal context       extract candidates with features
    ↓                                     ↓
    deterministic plausibility check      candidate scoring & sort
    ↓                                     ↓
    score top candidate → confident?      zero candidates? freeform model call
    ├─ yes → use as decision              one candidate confident? use directly
    └─ no  → ask model among top N        multiple? ask model to pick (with
                                             "none of these" option)
    ↓                                     ↓
    URL provenance check (v1.4 §9.4)      answer content gate (v1.4)
    ↓                                     ↓
    destination verification (§7)          → ResultFound
    ├─ pass → ResultFound
    └─ fail → pop next priority-queue item, retry
```

The model is no longer on the critical path for every page. It is called when:

- The link ranker's top candidate isn't confidently better than the runner-up (navigation goal)
- The candidate extractor finds multiple candidates with similar feature scores (fact goal)
- The extractor finds no candidates and falls back to model reading (fact goal)
- The plausibility-retry path needs reinforced reasoning (carried over from v1.4 §9.3)

### 3.2 Trust model (extended from v1.4 §13)

v1.4 defined four levels: Trusted, Untrusted, Semi-Trusted, Promoted. v2 adds one new level and refines the boundary for cached model output:

- **Trusted (unchanged)** — caller code, Charlotte's own code.
- **Trusted-Deterministic (new)** — output of Charlotte's deterministic components (ranker scores, candidate extraction results, deterministic preprocessor output). Sits alongside Trusted in downstream handling. Distinguished from Trusted because its content is data-dependent and a future audit may want to distinguish "Charlotte's static code" from "Charlotte's data-dependent code."
- **Untrusted (unchanged)** — page content from the web.
- **Semi-Trusted (unchanged, scope clarified)** — per-page model output, and model-backed preprocessor output, pre-validation. The preprocessor case is explicitly Semi-Trusted: it gets validated once at preprocessing time per §4.5 and promoted to Trusted-Deterministic if it passes. Cached promoted contexts retain their Trusted-Deterministic status as long as the cache key (which includes the preprocessor model identifier) remains valid.
- **Promoted (unchanged)** — model output that passed all per-page checks.

The proposed "Trusted-Model-Inferred" level from the v2 draft was collapsed into Semi-Trusted with explicit cache-invalidation rules (§4.6). Reasoning: cached preprocessor output is structurally equivalent to per-page model output that has been validated and promoted — there's no need for a distinct trust level if the validation is rigorous and the cache invalidates correctly on preprocessor changes.

---

## 4. Goal preprocessor

### 4.1 Purpose

Transforms a freeform `goal` string (with optional `navigation_hint`) into a structured `GoalContext` that downstream components use deterministically. Runs **once per crawl**, before any pages are fetched.

The preprocessor is where Charlotte's interpretation of the goal becomes **explicit and inspectable**. After preprocessing, every downstream stage works against `GoalContext` fields, not against the raw goal string. If a crawl fails, the user can look at `result.goal_context` (when the caller opts in per §9.4) and see what Charlotte thought the goal meant.

### 4.2 `GoalContext` schema

```python
@dataclass(frozen=True)
class GoalContext:
    # Original inputs, preserved verbatim
    goal: str
    navigation_hint: str | None

    # Inferred goal type — routes to the right downstream path
    goal_type: Literal[
        "navigation",          # find a URL
        "phone_extraction",    # find a phone number
        "date_extraction",     # find a date
        "address_extraction",  # find a street address
        "price_extraction",    # find a price/cost
        "document_link",       # find a link to a specific document (often dated PDFs)
        "freeform_fact",       # fact extraction not matching the above — model-fallback
    ]
    goal_type_confidence: float  # 0.0 to 1.0

    # Lexical expansion: keys are tokens from the original goal text;
    # values are model-inferred (or empty) synonyms. The ranker treats
    # BOTH keys AND values as search terms — keys preserve the original
    # goal terms, values broaden the semantic net.
    # Example: {"CEO": ["president", "executive", "leader"]}
    synonyms: dict[str, list[str]]

    # Anchor terms: phrases the candidate scorer uses for proximity scoring.
    # Typically a subset of synonym keys; may include multi-token phrases.
    anchor_terms: list[str]

    # Negative terms: terms that look relevant but indicate the WRONG content.
    # Subject to the no-overlap rule (§4.5).
    negative_terms: list[str]

    # Goal-type-specific hints
    regex_hints: list[str]       # e.g. ["(\\d{3}) \\d{3}-\\d{4}"] for phone goals

    # Human-readable interpretation summary — diagnostic use only;
    # downstream code MUST NOT branch on this field.
    description: str

    # Provenance
    source: Literal["model", "deterministic", "caller_supplied"]
    model_used: str | None       # e.g. "groq:llama-3.1-8b-instant" when source == "model"
    created_at: datetime
```

**On the synonyms structure.** The dictionary is read as a one-to-many mapping from original goal tokens (keys) to semantic expansions (values). The ranker treats every key and every value as a search term — there is no privileged passthrough mechanism needed, because the original goal terms are always search terms by virtue of being keys.

**On `description`.** This is the most model-like field in an otherwise structured object. It exists for human inspection and debugging. Downstream Charlotte code does not read it. The field is marked "diagnostic use only" in its docstring, and integration tests verify nothing in the production path branches on it.

**On `goal_type_confidence`.** Used for auditing and for `HybridPreprocessor`'s fallback logic (§4.4). Downstream code may use this to choose between strict and freeform paths but should not gate on it for security purposes.

### 4.3 `GoalPreprocessorProtocol`

```python
class GoalPreprocessorProtocol(Protocol):
    async def __call__(
        self, *, goal: str, navigation_hint: str | None
    ) -> GoalContext: ...
```

Same shape as v1.4's `AdapterProtocol`. Keyword-only arguments for forward compatibility. Async to match adapter call sites.

### 4.4 Default preprocessors

Charlotte ships three preprocessors.

**`DeterministicPreprocessor`** — no model dependency, no extras required. Uses pattern matching on the goal text to infer goal type (presence of "phone" → `phone_extraction`, etc.). Tokenizes the goal for `anchor_terms`. Leaves `synonyms` populated with `{token: [token]}` for each goal token (keys-as-search-terms invariant preserved trivially); leaves `negative_terms` empty. Cheap, deterministic, narrow. Default when no other preprocessor is configured.

**`ModelPreprocessor`** — uses a configured adapter (`GroqAdapter` or `LocalAdapter`) to expand the goal via a single structured-output call. Returns a fully populated `GoalContext`. Caches results in-memory per crawl; callers may supply a `GoalContextCache` (§4.6) for persistence.

**`HybridPreprocessor`** (recommended) — runs `DeterministicPreprocessor` first. If `goal_type_confidence ≥ 0.9`, returns its result. Otherwise calls `ModelPreprocessor` and merges (deterministic structure, model synonyms and negatives). Cost-bounded by goal complexity; falls through to model only when the deterministic path is uncertain.

**Default selection:** `DeterministicPreprocessor`. `HybridPreprocessor` is the **recommended** setting (shown in README quickstart, used by the consolidation service shape). The choice mirrors v1.4's local-vs-Groq adapter handling: the lighter default ships, the better-quality one is one parameter away.

### 4.5 GoalContext validation

Every `GoalContext` — whether produced by a preprocessor or supplied by the caller — passes through validation before being used. This is the moment a `ModelPreprocessor`'s Semi-Trusted output is promoted to Trusted-Deterministic.

**Structural rules:**

- `goal_type` must be in the enum.
- `goal_type_confidence` must be in `[0.0, 1.0]`.
- `synonyms` keys must each appear (case-insensitive, token-boundary aware) in `goal` or `navigation_hint`. The model cannot introduce concepts the user didn't mention. Violations cause hard rejection of the context (`CharlotteGoalContextValidationError`).
- `synonyms` values are model-inferred and are NOT required to appear in the original goal.
- `anchor_terms` must each be tokens or token sequences from `goal` or `navigation_hint`. Same justification as the synonym-keys rule.
- `regex_hints` must each compile as valid regex; invalid patterns are dropped silently with a log warning.

**Security rules (adversarial preprocessor defenses):**

- `negative_terms` must NOT overlap (case-insensitive) with `synonyms` keys, `synonyms` values, or `anchor_terms`. A negative term that conflicts with a positive term would actively hide correct results. Violations cause hard rejection.
- `negative_terms` must NOT appear in the original `goal` or `navigation_hint`. If the user asked about "executive leadership," the model cannot add "leadership" or "executive" to negatives. Violations cause hard rejection.

**Sanitization rules:**

- All string-typed fields (`description`, all entries in `synonyms`/`anchor_terms`/`negative_terms`) are stripped of ANSI escape sequences and ASCII control characters per v1.4 §H2.
- Total serialized context size is capped (default 4 KB). Oversized contexts are rejected.

**Caller-supplied contexts** pass through the same validation. Trusting the caller (per v1.4 §13) doesn't mean trusting their data structures to be well-formed — validation protects downstream code from malformed input regardless of source.

### 4.6 GoalContext caching

Charlotte exposes two hooks for caching, neither required:

**Outlet (caller receives context):** When `return_goal_context=True` (§9.4), every `CrawlResult` and `CrawlComplete` event carries the final `GoalContext`. Callers can persist it externally for reuse.

**Inlet (caller supplies context):** When `goal_context=...` is passed to `crawl()`, preprocessing is skipped entirely; the supplied context is validated per §4.5 and used directly.

For callers who want library-managed caching, the `GoalContextCacheProtocol` allows plugging a cache backend:

```python
class GoalContextCacheProtocol(Protocol):
    async def get(self, key: str) -> GoalContext | None: ...
    async def put(self, key: str, value: GoalContext) -> None: ...
    async def invalidate(self, key: str) -> None: ...
```

Charlotte ships a default `InMemoryGoalContextCache` keyed on `(goal, navigation_hint, preprocessor_identifier, preprocessor_model)`. The preprocessor model identifier is part of the key — switching from `LocalAdapter` to `GroqAdapter` invalidates cached contexts automatically.

The cache is a library convenience; the canonical pattern for the consolidation service is to cache externally and pass via the inlet.

---

## 5. Link ranker

### 5.1 Purpose

For navigation goals, ranks page links by relevance to `GoalContext` before they reach the model — or replaces the model entirely when ranker confidence is high. Operates on the output of v1.4's structural-zone extractor (§10) — links arrive already ordered by structural region.

### 5.2 `LinkRanker` protocol and `RankedLink` schema

```python
@dataclass(frozen=True)
class RankedLink:
    text: str                       # post-sanitization (per §11.2)
    url: str
    score: float                    # 0.0 to 1.0
    zone: Literal["content", "neutral", "chrome"]
    features: dict[str, float]      # per-link feature breakdown for debugging

class LinkRankerProtocol(Protocol):
    async def __call__(
        self,
        *,
        goal_context: GoalContext,
        links: list[Link],
        visited_urls: set[str],
    ) -> list[RankedLink]: ...      # sorted by score descending; ties broken by zone
```

The ranker returns **all** links scored, not a truncated top-N. Truncation is the engine's responsibility (controlled by `max_prompt_links`), so the ranker remains a pure scoring function.

### 5.3 Default rankers

**`BM25Ranker`** (default) — pure lexical. Builds a BM25 index from the synonym keys + values + anchor terms. Scores each link's text and URL path tokens. No ML dependencies. Microseconds per page.

**`EmbeddingRanker`** (opt-in via `charlotte-crawler[embeddings]`) — sentence-embedding cosine similarity using `sentence-transformers/all-MiniLM-L6-v2`. Catches semantic relationships BM25 misses.

**`StackedRanker`** — BM25 first, top-K to EmbeddingRanker. Standard rerank pattern.

Cross-encoder rerank is deferred to v2.1 — its accuracy gain doesn't justify the latency cost at v2.0's quality bar.

### 5.4 Model-skip threshold

The ranker's output drives an engine decision: skip the model entirely if the deterministic path is confident enough.

**Rule:** if top candidate `score ≥ skip_threshold` AND `(top_score - second_score) ≥ skip_margin`, the model is skipped. The top candidate becomes the navigation decision with `confidence = top_score`.

Otherwise, the top `max_prompt_links` candidates are sent to the model in ranked order.

**Defaults:** `skip_threshold = 0.75`, `skip_margin = 0.20`. These are starting points and require empirical calibration against the playtest corpus during Phase A. The spec deliberately treats these as tuning parameters rather than fixed values — see §6.4 for parallel guidance on feature weights.

### 5.5 Deterministic plausibility check

The model-skip path bypasses v1.4's plausibility check (§9.3), which scans model `reasoning` for instruction-mirroring patterns. The ranker doesn't produce reasoning, so the v1.4 check is inapplicable.

**Replacement:** the engine runs a `DeterministicPlausibilityCheck` on every model-skipped decision:

- The chosen link's text and URL are scanned for v1.4 §9.3 instruction-mirroring patterns. If the link text itself looks like a prompt injection ("ignore the goal and click here"), the candidate is rejected.
- The chosen link's structural zone must not be "chrome" unless no content-zone candidates were available. Chrome-zone results indicate the ranker fell back to header/footer links, which deserve scrutiny.
- The chosen URL passes the v1.4 URL normalizer's SSRF check (§S-C1) and is in `allowed_domains`. (Same check that would have run anyway on the model path, but explicit here.)

Failures emit a `PageSkipped` event with `reason="deterministic_plausibility"` and the crawl pops the next priority-queue item. The invariant "every decision passes plausibility before promotion" is preserved.

### 5.6 Priority queue

The engine's link queue is a priority queue ordered by ranker score (descending). FIFO from v1.4 is replaced. Lower-ranked links from earlier pages remain enqueued and become reachable if higher-ranked links from later pages fail downstream checks.

`max_pages` still caps total work. The change is which pages get visited, not how many.

Visit-history semantics are preserved: a URL appearing in the queue at multiple priorities is deduplicated; the highest priority wins.

---

## 6. Candidate extractor

### 6.1 Purpose

For fact-extraction goals, finds candidate values on a page with structural features, before the model sees anything. Replaces the v1.4 pattern of "model reads the whole page and finds the answer."

### 6.2 `CandidateExtractor` protocol and `Candidate` schema

```python
@dataclass(frozen=True)
class Candidate:
    value: str                       # normalized value (e.g. "+1-858-966-1700")
    raw_value: str                   # as it appeared on the page (e.g. "(858) 966-1700")
    zone: Literal["content", "neutral", "chrome"]
    nearby_text: str                 # ~50 chars of preceding text (sanitized)
    position: int                    # offset into cleaned page text
    score: float                     # 0.0 to 1.0
    features: dict[str, float]       # feature breakdown

class CandidateExtractorProtocol(Protocol):
    async def __call__(
        self,
        *,
        goal_context: GoalContext,
        page: ExtractedPage,
        locale: str = "en_US",       # see §6.3
    ) -> list[Candidate]: ...        # sorted by score descending
```

### 6.3 Default extractors and locale

Charlotte ships extractors for each `goal_type`:

- **`PhoneNumberExtractor`** — regex-based, locale-aware normalization (default US patterns; international parsing via opt-in `phonenumbers` library).
- **`DateExtractor`** — handles ISO 8601, `Month YYYY`, `Mon D, YYYY`, locale-dependent numeric formats. Normalizes internally to `date` objects, exports as ISO 8601 strings. For "current"/"latest"/"most recent" goals, selects the maximum-dated candidate.
- **`AddressExtractor`** — US postal heuristics; international addresses out of scope for v2.0 but the locale parameter reserves space.
- **`PriceExtractor`** — currency-symbol-prefixed numerics.
- **`DocumentLinkExtractor`** — for `document_link` goals; matches `regex_hints` against filenames and anchor text.
- **`FreeformFactExtractor`** — for `freeform_fact` goals; produces no candidates, signals the engine to fall back to v1.4-style whole-page model reading.

**Locale handling.** Default is `"en_US"`. The locale parameter is exposed at the public API (`crawl(..., locale="en_GB")`) and threaded through to extractors. Date and price extractors must respect it; phone and address extractors may use it as a hint. The default reflects the initial use case (US-based consolidation service); the parameter ensures international users aren't trapped.

### 6.4 Scoring features

Candidate `score` is a weighted sum of features:

- `zone_weight` — content > neutral > chrome
- `anchor_proximity` — inverse distance to nearest `anchor_terms` occurrence
- `negative_proximity` — negative contribution if `negative_terms` is closer than `anchor_terms`
- `format_quality` — match strength against `regex_hints`
- `uniqueness` — candidates appearing once score higher than candidates repeated many times

**Initial weights are hand-tuned and ship as defaults.** They will require empirical experimentation against the playtest corpus — this is the part of the design most likely to need iteration after first contact with real-world pages. The aggregation is structured as a weighted sum (`score = sum(weight_i * feature_i)`) and the weights are overridable per crawl via a configuration object, so tuning doesn't require code changes.

**Future v2.x: feedback-driven weight tuning.** A telemetry loop logging cases where the model overrides the deterministic top candidate (or vice versa) gives a labeled signal that can be used to refine weights. This is explicitly scoped out of v2.0 but the architecture supports adding it later without breaking changes.

### 6.5 Model invocation on fact goals

After extraction, the engine routes by candidate count:

- **Zero candidates** — model is called in freeform mode (same shape as v1.4 fact extraction). The page may have the answer in a form the extractor missed.
- **One candidate, confident** — `score ≥ confident_extraction_threshold` (default 0.70). The candidate is used directly without model involvement.
- **One candidate, not confident** — model is asked to confirm, with the candidate presented as a single option.
- **Multiple candidates** — top K (default 3) sent to the model with a structured prompt:

> *"Three candidate phone numbers were found on this page. Candidate A is in the page header. Candidate B is two paragraphs after the text 'Respiratory Clinic'. Candidate C is in the footer. The user wants the respiratory clinic's phone number. Reply with the letter of the correct candidate, or 'NONE' if none of these are correct."*

The model returns a candidate letter and a confidence value, or `NONE`. On `NONE`, the engine falls back to freeform mode for that page.

The v1.4 answer content gate is preserved by construction — candidate values were extracted from the page, so they verifiably appear in it.

---

## 7. Destination verification

### 7.1 Purpose

For navigation goals, verifies that a proposed result URL actually leads somewhere relevant before promoting it to `ResultFound`. Closes a gap in v1.4: the model could claim "this URL satisfies the goal" without Charlotte ever fetching that URL to verify.

The verifier fetches the proposed destination, runs a lightweight relevance check, and either promotes the result or rejects it. On rejection, the engine pops the next item from the priority queue.

Destination verification applies only to **navigation goals**. Fact-extraction goals already verify their answers via the answer content gate against the current page — there is no separate "destination" to verify.

### 7.2 `DestinationVerifier` protocol

```python
@dataclass(frozen=True)
class VerificationResult:
    url: str
    passed: bool
    mode: Literal["off", "existence", "relevance", "full"]
    score: float | None              # relevance score if mode includes scoring
    reason: str                      # human-readable rejection reason if passed=False

class DestinationVerifierProtocol(Protocol):
    async def __call__(
        self,
        *,
        url: str,
        goal_context: GoalContext,
        fetcher: PageFetcher,
        sanitizer: Sanitizer,
        extractor: ContentExtractor,
        ranker: LinkRankerProtocol,  # used to score destination content against goal_context
    ) -> VerificationResult: ...
```

The verifier is given access to the fetch/sanitize/extract/rank stack — it reuses the same machinery the main crawl loop uses, so its behavior is consistent with how the rest of the engine sees pages.

### 7.3 Verification modes

Set via `verify_destination` parameter on `crawl()` / `find_link()`. Default: `"relevance"`.

- **`"off"`** — no verification; promote the chosen URL directly. Matches v1.4 behavior. Use when speed matters more than correctness or when downstream callers will do their own validation.
- **`"existence"`** — fetch the URL; reject on non-2xx, on apparent login walls (heuristic detection: forms with password fields, redirects to known auth paths), or on empty/error pages. No relevance scoring. Catches dead links and authentication walls.
- **`"relevance"`** (default) — existence checks plus a BM25 scoring pass of the destination's content against `GoalContext.anchor_terms` and synonyms. Rejects if the destination's relevance score falls below `verify_threshold` (default 0.3 — calibrated during Phase A). Catches "the link said 'academic calendar' but the destination is actually athletics news."
- **`"full"`** — existence + relevance + a full re-evaluation via the link ranker (treats the destination as a new page in the crawl). Most thorough; useful when the destination is itself a hub page and the caller wants confidence the relevant content is reachable from there.

### 7.4 Verification loop

Verification is **not recursive**. The fetch performed by the verifier is a leaf operation — destinations of destinations are not themselves verified. This bounds the work per crawl: at most one extra fetch per promoted result.

### 7.5 Failure handling

When verification fails:

1. The candidate is recorded in `CrawlResult.verified_candidates` with its `VerificationResult` (so the caller sees what was tried and why).
2. A `DestinationVerificationFailed` event is emitted.
3. The engine pops the next item from the priority queue and re-runs the navigation logic.
4. If the queue empties before a candidate verifies, the crawl ends with `found=False` and `failure_mode=ALL_CANDIDATES_REJECTED` (§9.3).

### 7.6 Known limitation: partial circularity

The `BM25Ranker` operating against `anchor_terms` is used both to score links on the source page (driving the candidate selection) and to score destination content during verification. This is not a fully independent check — a link that scored high because its text contained anchor terms is being verified against a destination that's also scored against the same anchor terms.

The check still has value: link text can lie about destination content; page content less commonly does. But it's not fully independent. The v2.x answer is to use embedding similarity for destination verification (independent semantic signal) — explicitly deferred to a future release rather than implemented as a placeholder now.

---

## 8. Public API (delta from v1.4 §5)

### 8.1 `crawl()` and `find_link()` signature changes

New keyword-only parameters (all with defaults that approximate v1.4 behavior where applicable):

```python
async def crawl(
    start_url: str,
    goal: str,
    *,
    # ...all v1.4 parameters retained...

    # Pluggable components
    goal_preprocessor: GoalPreprocessorProtocol | None = None,
    link_ranker: LinkRankerProtocol | None = None,
    candidate_extractor: CandidateExtractorProtocol | None = None,
    destination_verifier: DestinationVerifierProtocol | None = None,

    # Caching
    goal_context: GoalContext | None = None,
    goal_context_cache: GoalContextCacheProtocol | None = None,

    # Diagnostic returns
    return_goal_context: bool = True,

    # Model-skip behavior
    skip_threshold: float = 0.75,
    skip_margin: float = 0.20,
    confident_extraction_threshold: float = 0.70,

    # Ranker
    max_prompt_links: int = 8,

    # Destination verification
    verify_destination: Literal["off", "existence", "relevance", "full"] = "relevance",
    verify_threshold: float = 0.3,

    # Locale
    locale: str = "en_US",
) -> AsyncIterator[Event] | CrawlResult: ...
```

When `goal_context` is supplied, preprocessing is skipped (§4.6 inlet).
When `goal_context_cache` is supplied, contexts persist across crawls in the same process.
When `return_goal_context=True` (default), the resolved context is part of every `CrawlResult` and `CrawlComplete` event.

### 8.2 Backward compatibility

v1.4-style calls (`crawl(start_url, goal)` with no v2 parameters) continue to work. The defaults produce v1.4-compatible behavior in shape: the public API surface is preserved, the streaming-event protocol is preserved, the `AdapterProtocol` is unchanged, and existing adapters work without modification.

Behavior may differ: v2 will produce different decisions on some crawls because the architecture has more information and more validation. That's the point of the rewrite. The spec deliberately does not claim bit-for-bit equivalence with v1.4 — "better decisions, same-shaped interfaces" is the compatibility goal.

There are no users of v1.4 outside the audit/development cycle, so the compatibility surface area is shallow. Future deprecations of v1.4-only behaviors can land in v2.x minor releases.

---

## 9. Crawl result and failure modes

### 9.1 `CrawlResult` extensions

```python
@dataclass(frozen=True)
class CrawlResult:
    # ...all v1.4 fields retained: found, urls, answers, visit_log,
    #    best_candidate_url, return_content, etc...

    # NEW: structured failure reason (populated when found=False)
    failure_mode: FailureMode | None

    # NEW: inspectable goal interpretation (when return_goal_context=True)
    goal_context: GoalContext | None

    # NEW: destination verification attempts (navigation goals)
    verified_candidates: list[VerificationResult]
```

`best_candidate_url` from v1.4 is retained with its v1.4 semantics: the highest-confidence link the model or ranker scored, regardless of verification outcome. The new `verified_candidates` list separately tracks every candidate that reached the verification stage, with each attempt's pass/fail and reason. The two fields together let a caller distinguish "we never had a confident candidate" from "we had confident candidates that didn't verify."

### 9.2 `FailureMode` enum

```python
class FailureMode(StrEnum):
    NO_CANDIDATES_FOUND = "no_candidates_found"
    # Pages were fetched but ranker/extractor never produced a scorable candidate.
    # Likely causes: goal interpretation mismatch (check goal_context), content
    # genuinely not present on target site.

    ALL_CANDIDATES_REJECTED = "all_candidates_rejected"
    # Candidates were produced and reached destination verification, but none
    # passed. Likely causes: site structure changed, anchor terms drifted from
    # current content. Worth re-preprocessing the goal.

    BUDGET_EXHAUSTED = "budget_exhausted"
    # max_pages or wall-clock budget reached before reaching a confident answer.
    # Likely fixes: increase budget, refine navigation_hint.

    PLAUSIBILITY_FAILURES = "plausibility_failures"
    # Pages were fetched but plausibility checks repeatedly rejected decisions.
    # Likely causes: target site is adversarial, sanitizer is missing patterns.
    # Worth operator review.

    FETCH_FAILURES = "fetch_failures"
    # Repeated network errors prevented meaningful progress.
    # Likely causes: target site unavailable, DNS issues, rate limiting.
    # Operational — retry with backoff.
```

The failure mode is recorded in `CrawlResult.failure_mode` when `found=False` and emitted in the `CrawlComplete` event. It is `None` when `found=True`.

### 9.3 Failure-mode assignment

The engine maintains running counters during a crawl. When the queue empties or budget is hit:

- If `budget_exhausted` flag is set: `BUDGET_EXHAUSTED` (takes precedence)
- Else if `verified_candidates` is non-empty (all entries `passed=False`): `ALL_CANDIDATES_REJECTED`
- Else if `plausibility_failures_count > plausibility_threshold` (default 3): `PLAUSIBILITY_FAILURES`
- Else if `fetch_failures_count > fetch_threshold` (default 3): `FETCH_FAILURES`
- Else: `NO_CANDIDATES_FOUND`

Thresholds are tunable per crawl.

### 9.4 `return_goal_context` parameter

`GoalContext` is small (≤4 KB per §4.5) and useful for both success diagnosis and failure recovery. It is returned by default.

Callers who don't need it (e.g. high-throughput pipelines where every byte counts) can opt out via `return_goal_context=False`. When opted out, `CrawlResult.goal_context` is `None` and the `GoalPreprocessed` event still fires but with the context fields elided.

---

## 10. Streaming events (delta from v1.4 §17)

### 10.1 New events

```python
@dataclass(frozen=True)
class GoalPreprocessed:
    type: Literal["goal_preprocessed"] = "goal_preprocessed"
    timestamp: datetime
    goal_context: GoalContext | None    # None when return_goal_context=False
    duration_ms: int
    source: Literal["fresh", "cached", "caller_supplied"]

@dataclass(frozen=True)
class LinksRanked:
    type: Literal["links_ranked"] = "links_ranked"
    timestamp: datetime
    page_url: str
    total_links: int
    top_links: list[RankedLink]         # capped at 10 for event-size sanity
    duration_ms: int

@dataclass(frozen=True)
class CandidatesExtracted:
    type: Literal["candidates_extracted"] = "candidates_extracted"
    timestamp: datetime
    page_url: str
    candidates: list[Candidate]
    duration_ms: int

@dataclass(frozen=True)
class ModelSkipped:
    type: Literal["model_skipped"] = "model_skipped"
    timestamp: datetime
    page_url: str
    reason: Literal["ranker_confident", "single_candidate_confident"]
    decision: str                       # URL or extracted value
    confidence: float

@dataclass(frozen=True)
class DestinationVerificationFailed:
    type: Literal["destination_verification_failed"] = "destination_verification_failed"
    timestamp: datetime
    url: str
    result: VerificationResult
```

### 10.2 Updated `CrawlComplete`

v1.4's `CrawlComplete` is extended with failure information:

```python
@dataclass(frozen=True)
class CrawlComplete:
    type: Literal["crawl_complete"] = "crawl_complete"
    timestamp: datetime
    found: bool
    failure_mode: FailureMode | None    # NEW — None when found=True
    failure_reason: str | None          # NEW — human-readable elaboration
    goal_context: GoalContext | None    # NEW — when return_goal_context=True
    # ...existing v1.4 fields...
```

A dedicated `CrawlFailed` event was considered and rejected: structured failure information on `CrawlComplete` is sufficient, and the simpler event stream is easier for callers to handle.

### 10.3 Event ordering

A typical navigation page:

```
PageFetched → LinksRanked → (ModelDecision | ModelSkipped) →
DestinationVerificationFailed* → ResultFound | (loop continues)
```

A typical fact-extraction page:

```
PageFetched → CandidatesExtracted → (ModelDecision | ModelSkipped) → ResultFound
```

`GoalPreprocessed` fires exactly once per crawl, between `CrawlStarted` and the first `PageFetched`.

`CrawlComplete` carries the final failure mode when applicable.

---

## 11. Security model (delta from v1.4 §13)

The v1.4 security model holds in full. The new components introduce three considerations.

### 11.1 GoalContext as a cached trust boundary

`GoalContext` from a model preprocessor is Semi-Trusted until it passes §4.5 validation, then Trusted-Deterministic. The cache key includes the preprocessor model identifier, so switching adapters invalidates cached contexts automatically.

The validation rules in §4.5 are the load-bearing defense. In particular:

- The strict synonym-keys rule prevents the preprocessor from introducing concepts the user didn't ask about.
- The no-negative-overlap rule prevents an adversarial preprocessor from populating negatives that demote correct results.
- The negative-terms-not-in-goal rule prevents the preprocessor from suppressing user-supplied search terms.

These three rules together bound the harm a misaligned or compromised preprocessor can do: it can produce noise (worse quality) but not actively misdirect (security regression).

### 11.2 Ranker scores and candidate features

Both are produced by Charlotte's code from page content. Page content is Untrusted; the scoring functions are Trusted code; the outputs are Trusted-Deterministic — they cannot themselves be prompt-injected.

However, the `text`/`nearby_text` fields of `RankedLink` and `Candidate` are derived from page content and must be sanitized (control chars, ANSI escapes, NUL bytes, normalized whitespace) before being included in events or returned via `CrawlResult`. Same rules as v1.4 §H2 model output sanitization.

### 11.3 Model-skip and deterministic plausibility

§5.5's `DeterministicPlausibilityCheck` preserves the v1.4 invariant: every decision passes plausibility before promotion to a `Promoted` result. The deterministic check is shape-matched to the ranker path (no `reasoning` to scan; instead scan link text and verify zone appropriateness).

### 11.4 Caller-supplied goal text and preprocessor inputs

Goal text is caller-supplied and treated as Trusted per v1.4. Callers forwarding end-user input into `goal` should sanitize control characters and bound length before passing to Charlotte. This guidance is documented in `SECURITY.md` rather than enforced in code, consistent with v1.4's stance on caller responsibility.

### 11.5 New error classes

```python
class CharlottePreprocessError(CharlotteError): ...
class CharlotteGoalContextValidationError(CharlotteError): ...
class CharlotteRankerError(CharlotteError): ...
class CharlotteCandidateExtractionError(CharlotteError): ...
class CharlotteDestinationVerificationError(CharlotteError): ...
```

All inherit from `CharlotteError`, matching v1.4 §18.

### 11.6 Deferred items from earlier audits

The v1.x deferred items remain deferred:

- `total_timeout` (S-M3 from security audit v1) — workaround documented; planned for v2.1.
- `safe_mode` (S-M4) — caller guidance in `SECURITY.md`.
- DNS rebinding (acknowledged in `SECURITY.md`) — operator-level network controls remain the answer.

---

## 12. Migration path

### 12.1 Versioning

v2.0 is a major version bump. `pyproject.toml`: `version = "2.0.0"`, `Development Status :: 4 - Beta` for the initial 2.0 release. Production/Stable returns at 2.1 once the playtest corpus validates new defaults.

### 12.2 Compatibility shim

v1.4-style calls work without modification. The `AdapterProtocol` is unchanged. New parameters have defaults that produce the closest equivalent to v1.4 behavior the new architecture allows.

### 12.3 Recommended pattern for the consolidation service

1. At registry-entry creation: call a goal preprocessor (or `HybridPreprocessor`) once; persist the resulting `GoalContext` alongside the entry.
2. On scheduled runs: pass the persisted context as `goal_context=...` to `crawl()`.
3. On structured failure: branch on `failure_mode`. `NO_CANDIDATES_FOUND` and `ALL_CANDIDATES_REJECTED` after N consecutive failures should trigger re-preprocessing and a fresh context. `BUDGET_EXHAUSTED` should adjust budget. `PLAUSIBILITY_FAILURES` and `FETCH_FAILURES` should alert operators.
4. On success: continue using the cached context until a structural change triggers invalidation.

This is the pattern the spec is designed for.

---

## 13. Test matrix (delta from v1.4 §19)

v1.4 T-01..T-33 carries over. v2 adds:

| # | Title | Verifies |
|---|---|---|
| T-34 | Deterministic preprocessing | Goal-type inference from clear cues |
| T-35 | Model preprocessing | `ModelPreprocessor` produces a valid `GoalContext` |
| T-36 | Context validation — structural | Each §4.5 structural rule rejects its violation |
| T-37 | Context validation — security | No-negative-overlap and negative-not-in-goal rules enforced |
| T-38 | Context caching — inlet | Caller-supplied context skips preprocessing |
| T-39 | Context caching — outlet | `return_goal_context=True` populates result and event |
| T-40 | Context caching — invalidation | Switching preprocessor model invalidates cached entries |
| T-41 | Link ranker — BM25 | Known input pages produce known rankings |
| T-42 | Link ranker — synonym expansion | Synonym values expand the search query |
| T-43 | Model-skip threshold | High-confidence skips; low-confidence doesn't |
| T-44 | Deterministic plausibility | Skipped decisions pass instruction-mirroring checks |
| T-45 | Priority queue | Higher-scored links processed before lower-scored ones |
| T-46 | Candidate extractor — phone (two-number disambiguation) | The playtest case |
| T-47 | Candidate extractor — date (latest-dated-document) | Selects max-dated candidate |
| T-48 | Candidate extractor — zero/one/multi paths | §6.5 routing |
| T-49 | Candidate extractor — "none of these" | Model rejection falls back to freeform |
| T-50 | Locale | Date and price extractors honor `locale` parameter |
| T-51 | Destination verification — relevance | Irrelevant destination rejected |
| T-52 | Destination verification — existence | 404 and login wall rejected |
| T-53 | Destination verification — fallback | Failed verification pops next priority-queue item |
| T-54 | Failure mode — `NO_CANDIDATES_FOUND` | Correctly assigned when no candidates emerge |
| T-55 | Failure mode — `ALL_CANDIDATES_REJECTED` | Correctly assigned when verifications all fail |
| T-56 | Failure mode — `BUDGET_EXHAUSTED` | Takes precedence over other modes |
| T-57 | New error classes | Each new exception raised by the right component |
| T-58 | Event ordering | New events emit in §10.3 order |
| T-59 | v1.4 compatibility | The v1.4 README quickstart works unchanged |
| T-60 | `verified_candidates` and `best_candidate_url` split | Both fields populated correctly on partial failure |

`scripts/groq_playtest.py` is extended to:
- Run both v1.4 and v2 corpus
- Detect quality regressions (any case where v1.4 found but v2 didn't, or vice versa)
- Calibrate `skip_threshold`, `skip_margin`, `confident_extraction_threshold`, and `verify_threshold` empirically

---

## 14. Sequencing

Three implementation phases. Each ends with the relevant slice of the test matrix passing and a working state worth tagging.

**Phase A — Link ranker (v2.0-alpha).** `LinkRankerProtocol`, `BM25Ranker`, model-skip threshold, deterministic plausibility, priority queue. New events: `LinksRanked`, `ModelSkipped`. Tests: T-41 through T-45, T-58, T-59. No preprocessor (use a stub `DeterministicPreprocessor` returning a minimal `GoalContext`), no candidate extractor, no destination verification (`verify_destination="off"` default for the alpha).

Smallest safe insertion. Proves the architecture and gives empirical data for `skip_threshold` calibration.

**Phase B — Goal preprocessor (v2.0-beta).** `GoalPreprocessorProtocol`, three default preprocessors, `GoalContext` schema, validation (§4.5), caching protocol, `InMemoryGoalContextCache`. Phase A's stub preprocessor is replaced. New events: `GoalPreprocessed`. Tests: T-34 through T-40, T-50, T-57.

Unlocks fact extraction in Phase C and makes Charlotte's reasoning inspectable.

**Phase C — Candidate extractor + destination verification (v2.0).** All six default candidate extractors, scoring features, model invocation rules, `DestinationVerifierProtocol`, four verification modes, failure-mode assignment, `CrawlResult` extensions. New events: `CandidatesExtracted`, `DestinationVerificationFailed`. Tests: T-46 through T-49, T-51 through T-56, T-60. `verify_destination="relevance"` becomes the default.

Completes the v2 architecture. Tag `v2.0.0`. Move to Production/Stable at `v2.1` once playtest is clean.

---

## 15. Things deliberately not addressed in v2.0

- **Operational concerns deferred from v1.x audits** — wall-clock timeout, `safe_mode`. Independent of v2 architecture; can land in v1.5 or v2.1.
- **Hosted/authenticated `LocalAdapter`.** The repr/pickle defenses anticipate it without implementing it.
- **Persistent disk-backed `GoalContextCache`.** Protocol exposed; caller implements.
- **Domain-specific knowledge bases** (UMLS, MeSH, custom thesauri). The `GoalPreprocessor` protocol is the extension point; Charlotte ships no specialist preprocessors.
- **Web UI for inspecting `GoalContext` and rankings.** Library returns inspectable data; UIs are downstream tooling.
- **Multi-step actions (form fill, click sequences).** Out of scope; Charlotte remains a navigation-and-extraction library.
- **Cross-encoder rerank.** Deferred to v2.1.
- **Feedback-driven feature-weight tuning.** Architecturally supported, deferred to v2.x.
- **Embedding-based destination verification.** Acknowledged in §7.6, deferred to v2.x.

---

## 16. Glossary

- **Goal context / `GoalContext`** — structured interpretation of a freeform goal, produced once per crawl.
- **Preprocessor** — component producing `GoalContext` from goal text.
- **Ranker** — component scoring links against `GoalContext` for navigation goals.
- **Candidate extractor** — component finding candidate values on a page for fact-extraction goals.
- **Candidate** — single extracted value with structural features and a score.
- **Destination verifier** — component validating that a proposed result URL leads somewhere relevant.
- **Model-skip** — path where deterministic confidence is high enough that the model is not called.
- **Deterministic plausibility** — analog of v1.4 §9.3 plausibility for model-skip decisions.
- **Trusted-Deterministic** — trust level for outputs of Charlotte's deterministic components.
- **Failure mode** — structured reason a crawl returned `found=False`, exposed as `CrawlResult.failure_mode`.
- **Synonyms (in `GoalContext`)** — dictionary mapping original goal tokens (keys) to model-inferred semantic equivalents (values); both keys and values are used as ranker search terms.

---

## Version History

| Version | Code target | Notes |
|---|---|---|
| 2.0-draft | v2.0.0-alpha | Initial draft for review; 15 open questions inline |
| 2.0 | v2.0.0 | Review cycle complete; all open questions resolved (see Appendix A); destination verification and failure-mode framework added during review |

---

## Appendix A — Decision log

Decisions made during the v2.0 review cycle, in section order.

| Section | Question | Decision | Rationale |
|---|---|---|---|
| §2.3 | Charlotte ships `GoalContextCache`? | Yes — protocol + `InMemoryGoalContextCache` default | Outlet/inlet pattern matches the consolidation service shape. Library doesn't require a cache; makes one easy to plug in. |
| §3.2 | New "Trusted-Model-Inferred" level vs cached Semi-Trusted? | Cached Semi-Trusted, promoted via §4.5 | Simpler; the validation does the work the new level would have done. |
| §4.2 | Include `description` field? | Yes, "diagnostic use only" | Auditable; downstream code must not branch on it. |
| §4.2 | Include `goal_type_confidence`? | Yes | Auditing value, drives `HybridPreprocessor` fallback. |
| §4.4 | Default preprocessor? | `DeterministicPreprocessor` (library default), `HybridPreprocessor` (recommended) | Matches v1.4 adapter pattern: lighter default ships, better-quality one is one parameter away. |
| §4.5 | Strict vs loose synonym-keys rule? | Strict — keys must appear in goal | User's clarification: keys ARE search terms by virtue of being keys (the ranker uses both keys and values). The original goal terms are always preserved. |
| §4.5 | Adversarial-preprocessor defenses? | Added: no-overlap between negatives and positives, negatives not in goal | Bounds the harm a misaligned preprocessor can do to "noise" rather than "active misdirection." |
| §5.3 | Cross-encoder rerank default? | Defer to v2.1 | Quality bar met by BM25 + optional embeddings; cross-encoder is a v2.x optimization. |
| §5.4 | Plausibility on model-skip pages? | Deterministic plausibility check | Preserves the v1.4 invariant; the check is shape-matched to ranker output. |
| §5.5 | Priority queue? | Yes | The ranker's work is wasted if the queue ignores it. |
| §6.3 | Default locale? | `"en_US"`, configurable per crawl | Reflects initial use case; parameter ensures international users aren't trapped. |
| §6.4 | Hand-tuned vs learned candidate weights? | Hand-tuned for v2.0, overridable per crawl; feedback loop is a v2.x feature | Acknowledged as the fuzziest part of the design; bears empirical work. |
| §6.5 | "None of these" option on candidate selection? | Yes | Try it and see; honest failure better than forced wrong answer. |
| §7 | Final fetch-and-scan validation? | Yes — `DestinationVerifier` with four modes, `"relevance"` default | Added during review. Closes the gap that v1.4 returned URLs without ever visiting them. |
| §7.3 | v1.4 compatibility goal? | Same-shape API, better decisions, new diagnostic fields | "Bit-for-bit equivalence" was never the goal; v2 is supposed to produce different (better) decisions. |
| §9 | `best_candidate_url` vs `verified_candidates`? | Split into two fields | Different concepts: best by score, vs every verification attempt with reason. |
| §9 | In-crawl self-healing on verification failure? | No — pop next priority-queue item, then fail honestly with structured reason | Charlotte stays narrowly focused; recovery is the caller's job. |
| §9.4 | `goal_context` always returned on failure? | Caller choice via `return_goal_context` (default `True`) | Small object, useful for diagnostics by default; opt-out for high-throughput pipelines. |
| §10 | `CrawlFailed` event vs structured `CrawlComplete`? | Structured `CrawlComplete` | Fewer event types is cleaner; failure_mode field carries the structure. |
| §14 | Three phases, or fold preprocessor + candidate extractor? | Three phases | Phase B's preprocessor unlocks inspectability independent of fact extraction. |
