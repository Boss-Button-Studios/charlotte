"""
Candidate extractor playtest — fetch a real page and run it through the
candidate extractor pipeline, printing every candidate with its score and
feature breakdown.

Usage:
    python3 test_extractor.py URL GOAL
    python3 test_extractor.py URL GOAL --goal-type phone_extraction
    python3 test_extractor.py URL GOAL --hint "respiratory clinic"
    python3 test_extractor.py URL GOAL --locale en_US --top 5
    python3 test_extractor.py URL GOAL --preprocessor hybrid

Goal types:
    navigation  phone_extraction  date_extraction  address_extraction
    price_extraction  document_link  freeform_fact

Env vars:
    CHARLOTTE_LOCAL_MODEL    — model for HybridPreprocessor (default: deepseek-r1:14b)
    CHARLOTTE_LOCAL_BASE_URL — inference server base URL   (default: http://localhost:11434)
    CHARLOTTE_MODEL_TIMEOUT  — seconds before model call is abandoned
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
from time import monotonic
from urllib.parse import urlsplit

from charlotte.core.candidate_extractor import DefaultCandidateExtractor
from charlotte.core.extractor import extract
from charlotte.core.fetcher import PageFetcher
from charlotte.core.goal_preprocessor import DeterministicPreprocessor, HybridPreprocessor
from charlotte.core.sanitizer import strip_hidden
from charlotte.models import Candidate, GoalContext

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("CHARLOTTE_LOCAL_BASE_URL", "http://localhost:11434")
MODEL = os.environ.get("CHARLOTTE_LOCAL_MODEL", "deepseek-r1:14b")
_timeout_env = os.environ.get("CHARLOTTE_MODEL_TIMEOUT")
try:
    TIMEOUT = float(_timeout_env) if _timeout_env else None
except ValueError:
    raise ValueError(
        f"CHARLOTTE_MODEL_TIMEOUT must be a number, got {_timeout_env!r}"
    ) from None

# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

_BAR = "─" * 60


def _fmt_features(features: dict[str, float]) -> str:
    parts = []
    for k, v in features.items():
        parts.append(f"{k}={v:.3f}")
    return "  ".join(parts)


def print_candidate(idx: int, c: Candidate) -> None:
    print(f"\n  [{idx}] score={c.score:.3f}  zone={c.zone}")
    print(f"       value      {c.value!r}")
    if c.raw_value != c.value:
        print(f"       raw_value  {c.raw_value!r}")
    print(f"       position   {c.position}")
    if c.nearby_text:
        nearby = c.nearby_text.replace("\n", " ")
        print(f"       nearby     …{nearby!r}")
    print(f"       features   {_fmt_features(c.features)}")


def print_page_summary(title: str, text: str, links: list) -> None:
    print(f"\n  title       {title!r}")
    print(f"  text_len    {len(text):,} chars")
    print(f"  links       {len(links)} extracted")
    snippet = text[:120].replace("\n", " ")
    print(f"  text[0:120] {snippet!r}")


def print_context(ctx: GoalContext, elapsed_ms: int) -> None:
    source_tag = f"[{ctx.source}]"
    if ctx.model_used:
        source_tag += f" model={ctx.model_used}"
    print(f"\n{_BAR}")
    print(f"  GoalContext  {source_tag}  ({elapsed_ms:,}ms)")
    print(f"{_BAR}")
    print(f"  goal_type    {ctx.goal_type}  (confidence {ctx.goal_type_confidence:.2f})")
    anchors = ", ".join(repr(t) for t in ctx.anchor_terms) or "(none)"
    print(f"  anchor_terms {anchors}")
    negatives = ", ".join(repr(t) for t in ctx.negative_terms) or "(none)"
    print(f"  negative     {negatives}")
    hints = ", ".join(repr(r) for r in ctx.regex_hints) or "(none)"
    print(f"  regex_hints  {hints}")
    if ctx.validation_warnings:
        for w in ctx.validation_warnings:
            print(f"  ! {w}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> None:
    url = args.url
    goal = args.goal
    locale = args.locale
    top_n = args.top

    hostname = (urlsplit(url).hostname or "").lower()

    print(f"\nURL:    {url}")
    print(f"Goal:   {goal}")
    if args.hint:
        print(f"Hint:   {args.hint}")
    print(f"Locale: {locale}")

    # --- Preprocess goal ---
    t0 = monotonic()
    if args.preprocessor == "hybrid":
        preprocessor = HybridPreprocessor(base_url=BASE_URL, model=MODEL, timeout=TIMEOUT)
        print(f"\nUsing HybridPreprocessor  model={MODEL}  base_url={BASE_URL}")
    else:
        preprocessor = DeterministicPreprocessor()

    ctx: GoalContext = preprocessor(goal, args.hint, locale)
    prep_ms = int((monotonic() - t0) * 1000)

    _VALID_GOAL_TYPES = {
        "navigation", "phone_extraction", "date_extraction", "address_extraction",
        "price_extraction", "document_link", "freeform_fact",
    }
    if args.goal_type:
        if args.goal_type not in _VALID_GOAL_TYPES:
            print(f"\n  ✗ Unknown --goal-type {args.goal_type!r}. "
                  f"Valid types: {', '.join(sorted(_VALID_GOAL_TYPES))}")
            return
        if args.goal_type != ctx.goal_type:
            ctx = dataclasses.replace(ctx, goal_type=args.goal_type)  # type: ignore[arg-type]
            print(f"  (goal_type overridden → {args.goal_type})")

    print_context(ctx, prep_ms)

    # --- Fetch page ---
    print(f"\n{_BAR}")
    print("  Fetching page…")
    fetcher = PageFetcher(
        allowed_domains={hostname},
        connect_timeout=15.0,
        read_timeout=30.0,
        polite_delay=0.0,
    )
    t0 = monotonic()
    try:
        result = await fetcher.fetch(url, visited_urls=set())
    except Exception as exc:
        print(f"\n  ✗ Fetch failed: {type(exc).__name__}: {exc}")
        return
    fetch_ms = int((monotonic() - t0) * 1000)
    print(f"  HTTP {result.status_code}  ({fetch_ms:,}ms)")

    # --- Sanitize + extract ---
    t0 = monotonic()
    sanitized = strip_hidden(result.html)
    page = extract(sanitized, result.url)
    extract_ms = int((monotonic() - t0) * 1000)

    print(f"\n{_BAR}")
    print(f"  Extracted  ({extract_ms:,}ms)")
    print(f"{_BAR}")
    print_page_summary(page.title, page.text, page.links)

    # --- Run candidate extractor ---
    t0 = monotonic()
    extractor = DefaultCandidateExtractor()
    candidates = await extractor(goal_context=ctx, page=page, locale=locale)
    cand_ms = int((monotonic() - t0) * 1000)

    print(f"\n{_BAR}")
    print(f"  Candidates  ({cand_ms:,}ms)  goal_type={ctx.goal_type}")
    print(f"{_BAR}")

    if not candidates:
        if ctx.goal_type in ("navigation", "freeform_fact"):
            print(f"\n  (no candidates — {ctx.goal_type} goals do not use the extractor)")
        else:
            print("\n  ✗ No candidates found on this page.")
        print()
        return

    display = candidates[:top_n]
    print(f"\n  {len(candidates)} candidate(s) found — showing top {len(display)}:")
    for i, c in enumerate(display, 1):
        print_candidate(i, c)

    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch a page and run Charlotte's candidate extractor against it."
    )
    parser.add_argument("url", help="Page URL to fetch")
    parser.add_argument("goal", help="Extraction goal")
    parser.add_argument("--hint", default=None, metavar="HINT",
                        help="Optional navigation hint passed to the preprocessor")
    parser.add_argument("--goal-type", default=None, metavar="TYPE",
                        help="Override goal_type inferred by the preprocessor")
    parser.add_argument("--locale", default="en_US", metavar="LOCALE",
                        help="BCP 47 locale tag (default: en_US)")
    parser.add_argument("--top", type=int, default=5, metavar="N",
                        help="How many candidates to display (default: 5)")
    parser.add_argument("--preprocessor", choices=["deterministic", "hybrid"],
                        default="deterministic",
                        help="Preprocessor to use (default: deterministic)")
    args = parser.parse_args()
    if args.top < 1:
        parser.error("--top must be a positive integer")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
