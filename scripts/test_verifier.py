"""
Destination verifier playtest — run a real URL through DefaultDestinationVerifier
and display the verification result, BM25 score, and (optionally) captured content.

Usage:
    python3 test_verifier.py URL GOAL
    python3 test_verifier.py URL GOAL --mode existence
    python3 test_verifier.py URL GOAL --mode full
    python3 test_verifier.py URL GOAL --threshold 0.5
    python3 test_verifier.py URL GOAL --fetch-content
    python3 test_verifier.py URL GOAL --result-to-file /tmp/downloads
    python3 test_verifier.py URL GOAL --preprocessor hybrid --hint "staff directory"
    python3 test_verifier.py URL GOAL --locale en_US

Modes:
    existence   HTTP 2xx + no login wall + non-empty body
    relevance   existence + BM25 score ≥ threshold (default 0.3)
    full        existence + embeddings (or strict BM25 when extras absent)

Env vars:
    CHARLOTTE_LOCAL_MODEL    — model for HybridPreprocessor (default: deepseek-r1:14b)
    CHARLOTTE_LOCAL_BASE_URL — inference server base URL (default: http://localhost:11434)
    CHARLOTTE_MODEL_TIMEOUT  — seconds before model call is abandoned
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from time import monotonic

from charlotte.core.destination_verifier import DefaultDestinationVerifier
from charlotte.core.goal_preprocessor import DeterministicPreprocessor, HybridPreprocessor
from charlotte.models import GoalContext, ResultContent

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("CHARLOTTE_LOCAL_BASE_URL", "http://localhost:11434")
MODEL = os.environ.get("CHARLOTTE_LOCAL_MODEL", "deepseek-r1:14b")
_timeout_env = os.environ.get("CHARLOTTE_MODEL_TIMEOUT")
try:
    TIMEOUT = float(_timeout_env) if _timeout_env else None
except ValueError:
    print(f"Error: CHARLOTTE_MODEL_TIMEOUT must be a number, got {_timeout_env!r}", file=sys.stderr)
    sys.exit(1)

_BAR = "─" * 60

# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _fmt_list(items: list[str]) -> str:
    return ", ".join(repr(s) for s in items) if items else "(none)"


def _fmt_synonyms(synonyms: dict[str, list[str]]) -> str:
    if not synonyms:
        return "(none)"
    parts = []
    for k, vs in synonyms.items():
        vals = ", ".join(repr(v) for v in vs) if vs else "(no expansions)"
        parts.append(f"  {k!r:20s} → {vals}")
    return "\n" + "\n".join(parts)


def print_context(ctx: GoalContext, elapsed_ms: int) -> None:
    source_tag = f"[{ctx.source}]"
    if ctx.model_used:
        source_tag += f" model={ctx.model_used}"
    print(f"\n{_BAR}")
    print(f"  GoalContext  {source_tag}  ({elapsed_ms:,}ms)")
    print(f"{_BAR}")
    print(f"  goal_type    {ctx.goal_type}  (confidence {ctx.goal_type_confidence:.2f})")
    print(f"  anchor_terms {_fmt_list(ctx.anchor_terms)}")
    print(f"  synonyms     {_fmt_synonyms(ctx.synonyms)}")
    print(f"  negatives    {_fmt_list(ctx.negative_terms)}")
    print(f"  regex_hints  {_fmt_list(ctx.regex_hints)}")
    if ctx.validation_warnings:
        for w in ctx.validation_warnings:
            print(f"  ⚠ {w}")


def print_result(result, content: ResultContent | None, elapsed_ms: int) -> None:
    status = "✓  PASSED" if result.passed else "✗  FAILED"
    print(f"\n{_BAR}")
    print(f"  VerificationResult  ({elapsed_ms:,}ms)")
    print(f"{_BAR}")
    print(f"  status   {status}")
    print(f"  mode     {result.mode}")
    score_str = f"{result.score:.4f}" if result.score is not None else "n/a"
    print(f"  score    {score_str}")
    print(f"  reason   {result.reason}")

    if content is not None:
        print(f"\n  ResultContent:")
        print(f"    content_type      {content.content_type or '(none)'}")
        print(f"    content_length    {content.content_length:,} bytes")
        print(f"    suggested_file    {content.suggested_filename or '(none)'}")
        print(f"    etag              {content.etag or '(none)'}")
        print(f"    fetched_at        {content.fetched_at.isoformat()}")
        if content.file_path is not None:
            print(f"    file_path         {content.file_path}")
        elif content.content is not None:
            preview = content.content[:80]
            try:
                preview_str = preview.decode("utf-8", errors="replace")
            except Exception:
                preview_str = repr(preview)
            print(f"    content[0:80]     {preview_str!r}")
    else:
        print(f"\n  ResultContent: (not captured)")


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

async def run(
    url: str,
    goal: str,
    *,
    mode: str,
    threshold: float,
    fetch_content: bool | None,
    result_to_file: Path | None,
    preprocessor: str,
    hint: str | None,
    locale: str,
) -> None:
    try:
        t0 = monotonic()
        if preprocessor == "hybrid":
            print(f"\nBuilding GoalContext via HybridPreprocessor  model={MODEL}  base_url={BASE_URL}")
            ctx = HybridPreprocessor(base_url=BASE_URL, model=MODEL, timeout=TIMEOUT)(
                goal, hint, locale
            )
        else:
            ctx = DeterministicPreprocessor()(goal, hint, locale)
        ctx_ms = int((monotonic() - t0) * 1000)
    except Exception as exc:
        print(f"\n  ✗ Could not build GoalContext: {type(exc).__name__}: {exc}")
        if preprocessor == "hybrid":
            print(f"    Is {BASE_URL} reachable? Try --preprocessor deterministic.")
        return
    print_context(ctx, ctx_ms)

    verifier = DefaultDestinationVerifier(
        mode=mode,
        verify_threshold=threshold,
        fetch_result_content=fetch_content,
        result_to_file=result_to_file,
    )

    print(f"\nVerifying  url={url}")
    print(f"  mode={mode}  threshold={threshold}  fetch_content={fetch_content}")
    if result_to_file:
        print(f"  result_to_file={result_to_file}")

    try:
        t0 = monotonic()
        result, content = await verifier(url=url, goal_context=ctx)
        verify_ms = int((monotonic() - t0) * 1000)
    except Exception as exc:
        print(f"\n  ✗ Unexpected verifier error: {type(exc).__name__}: {exc}")
        return
    print_result(result, content, verify_ms)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run DefaultDestinationVerifier against a real URL."
    )
    parser.add_argument("url", help="URL to verify")
    parser.add_argument("goal", help="Navigation or extraction goal")
    parser.add_argument("--mode", default="relevance",
                        choices=["off", "existence", "relevance", "full"],
                        help="Verification mode (default: relevance)")
    parser.add_argument("--threshold", type=float, default=0.3, metavar="N",
                        help="Relevance threshold 0.0–1.0 (default: 0.3)")
    parser.add_argument("--fetch-content", action="store_true", default=None,
                        help="Capture result bytes (default: goal-type dependent)")
    parser.add_argument("--no-fetch-content", dest="fetch_content", action="store_false",
                        help="Suppress content capture even for document_link goals")
    parser.add_argument("--result-to-file", default=None, metavar="DIR",
                        help="Write captured bytes to this directory instead of memory")
    parser.add_argument("--preprocessor", default="deterministic",
                        choices=["deterministic", "hybrid"],
                        help="GoalContext source (default: deterministic)")
    parser.add_argument("--hint", default=None, metavar="HINT",
                        help="Optional navigation hint passed to preprocessor")
    parser.add_argument("--locale", default="en_US", metavar="LOCALE",
                        help="BCP 47 locale (default: en_US)")
    args = parser.parse_args()

    if args.threshold < 0.0 or args.threshold > 1.0:
        parser.error("--threshold must be between 0.0 and 1.0")

    result_to_file: Path | None = None
    if args.result_to_file:
        result_to_file = Path(args.result_to_file)
        if not result_to_file.is_dir():
            parser.error(f"--result-to-file {result_to_file!r} is not a directory")

    fetch_content: bool | None = args.fetch_content

    print(f"URL:    {args.url}")
    print(f"Goal:   {args.goal}")
    if args.hint:
        print(f"Hint:   {args.hint}")
    print(f"Locale: {args.locale}")

    asyncio.run(
        run(
            args.url,
            args.goal,
            mode=args.mode,
            threshold=args.threshold,
            fetch_content=fetch_content,
            result_to_file=result_to_file,
            preprocessor=args.preprocessor,
            hint=args.hint,
            locale=args.locale,
        )
    )
    print()


if __name__ == "__main__":
    main()
