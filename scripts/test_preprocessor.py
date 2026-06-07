"""
Preprocessor comparison tool — run a goal through both preprocessors and
print the results side-by-side so you can see what each produces.

Usage:
    python3 test_preprocessor.py "Find the contact page"
    python3 test_preprocessor.py "Find the contact page" --hint "top nav"
    python3 test_preprocessor.py "What is the price?" --locale en_US
    python3 test_preprocessor.py "Find the tutorial" --hybrid-only
    python3 test_preprocessor.py "Find the tutorial" --deterministic-only
    python3 test_preprocessor.py "Find the tutorial" --verbose

Env vars:
    CHARLOTTE_LOCAL_MODEL    — model for HybridPreprocessor (default: deepseek-r1:14b)
    CHARLOTTE_LOCAL_BASE_URL — inference server base URL (default: http://localhost:11434)
    CHARLOTTE_MODEL_TIMEOUT  — seconds before the model call is abandoned
"""

from __future__ import annotations

import argparse
import json
import os
import textwrap
from time import monotonic

import httpx

from charlotte.core.goal_preprocessor import (
    DeterministicPreprocessor,
    HybridPreprocessor,
    _COMPLETIONS_PATH,
    _CTRL_RE,
    _HYBRID_SYSTEM,
    _LONE_CLOSE_THINK_RE,
    _THINK_RE,
    _extract_json,
    _validate_hybrid_output,
)
from charlotte.models import GoalContext

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("CHARLOTTE_LOCAL_BASE_URL", "http://localhost:11434")
MODEL = os.environ.get("CHARLOTTE_LOCAL_MODEL", "deepseek-r1:14b")
_timeout_env = os.environ.get("CHARLOTTE_MODEL_TIMEOUT")
TIMEOUT = float(_timeout_env) if _timeout_env else None

# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _fmt_list(items: list[str]) -> str:
    if not items:
        return "(none)"
    return ", ".join(repr(s) for s in items)


def _fmt_synonyms(synonyms: dict[str, list[str]]) -> str:
    if not synonyms:
        return "(none)"
    lines = []
    for k, vs in synonyms.items():
        vals = ", ".join(repr(v) for v in vs) if vs else "(no expansions)"
        lines.append(f"  {k!r:20s} → {vals}")
    return "\n" + "\n".join(lines)


def _fmt_warnings(warnings: list[str]) -> str:
    if not warnings:
        return "(none)"
    return "\n" + "\n".join(f"  ! {w}" for w in warnings)


def print_context(label: str, ctx: GoalContext, elapsed_ms: int) -> None:
    source_tag = f"[{ctx.source}]"
    if ctx.model_used:
        source_tag += f" model={ctx.model_used}"
    print(f"\n{'─' * 60}")
    print(f"  {label}  {source_tag}  ({elapsed_ms:,}ms)")
    print(f"{'─' * 60}")
    print(f"  goal_type      {ctx.goal_type}  (confidence {ctx.goal_type_confidence:.2f})")
    print(f"  anchor_terms   {_fmt_list(ctx.anchor_terms)}")
    print(f"  synonyms       {_fmt_synonyms(ctx.synonyms)}")
    print(f"  negative_terms {_fmt_list(ctx.negative_terms)}")
    print(f"  regex_hints    {_fmt_list(ctx.regex_hints)}")
    print(f"  description    {ctx.description or '(none)'}")
    print(f"  warnings       {_fmt_warnings(ctx.validation_warnings)}")


def run_hybrid_verbose(goal: str, hint: str | None, locale: str) -> None:
    """Call the model directly, print the raw response, then run validation."""
    hint_line = f"\nNavigation hint: {hint}" if hint else ""
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": _HYBRID_SYSTEM},
            {"role": "user", "content": f"Goal: {goal}{hint_line}"},
        ],
        "format": "json",
    }

    print(f"\nCalling HybridPreprocessor  model={MODEL}  base_url={BASE_URL}")
    if TIMEOUT:
        print(f"  (timeout={TIMEOUT}s)")

    t0 = monotonic()
    try:
        with httpx.Client(timeout=TIMEOUT or 30.0) as client:
            resp = client.post(f"{BASE_URL.rstrip('/')}{_COMPLETIONS_PATH}", json=payload)
        resp.raise_for_status()
        elapsed_ms = int((monotonic() - t0) * 1000)

        raw_content = resp.json()["choices"][0]["message"]["content"]
        stripped = _THINK_RE.sub("", raw_content).strip()
        stripped = _LONE_CLOSE_THINK_RE.sub("", stripped).strip()

        think_len = len(raw_content) - len(stripped)
        if think_len > 0:
            print(f"  (stripped {think_len} chars of <think> content)")

        print(f"\n── raw model output ({elapsed_ms:,}ms) ──")
        try:
            parsed = _extract_json(stripped)
            print(json.dumps(parsed, indent=2))
        except ValueError:
            print(textwrap.indent(stripped, "  "))
            print("\n  ✗ Could not parse as JSON — HybridPreprocessor will fall back.")
            return

        print("\n── validation ──")
        try:
            ctx = _validate_hybrid_output(parsed, goal, hint, locale, MODEL)
            print("  ✓ Validation passed")
            print_context("HybridPreprocessor", ctx, elapsed_ms)
        except ValueError as exc:
            print(f"  ✗ Validation rejected: {exc}")
            print("    HybridPreprocessor will fall back to DeterministicPreprocessor.")

    except httpx.ConnectError as exc:
        print(f"\n  ✗ Connection failed: {exc}")
        print("    Is Ollama (or your inference server) running?")
    except Exception as exc:
        elapsed_ms = int((monotonic() - t0) * 1000)
        print(f"\n  ✗ {type(exc).__name__}: {exc}  ({elapsed_ms:,}ms)")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a goal through Charlotte's preprocessors and compare output."
    )
    parser.add_argument("goal", help="Navigation or extraction goal")
    parser.add_argument("--hint", default=None, metavar="HINT",
                        help="Optional navigation hint")
    parser.add_argument("--locale", default="en_US", metavar="LOCALE",
                        help="BCP 47 locale tag (default: en_US)")
    parser.add_argument("--hybrid-only", action="store_true",
                        help="Skip DeterministicPreprocessor output")
    parser.add_argument("--deterministic-only", action="store_true",
                        help="Skip HybridPreprocessor (no model call)")
    parser.add_argument("--verbose", action="store_true",
                        help="Show raw model response and validation step by step")
    args = parser.parse_args()

    print(f"Goal:   {args.goal}")
    if args.hint:
        print(f"Hint:   {args.hint}")
    print(f"Locale: {args.locale}")

    if not args.hybrid_only:
        t0 = monotonic()
        det_ctx = DeterministicPreprocessor()(args.goal, args.hint, args.locale)
        det_ms = int((monotonic() - t0) * 1000)
        print_context("DeterministicPreprocessor", det_ctx, det_ms)

    if not args.deterministic_only:
        if args.verbose:
            run_hybrid_verbose(args.goal, args.hint, args.locale)
        else:
            print(f"\nCalling HybridPreprocessor  model={MODEL}  base_url={BASE_URL}")
            if TIMEOUT:
                print(f"  (timeout={TIMEOUT}s)")
            t0 = monotonic()
            hyb_ctx = HybridPreprocessor(base_url=BASE_URL, model=MODEL, timeout=TIMEOUT)(
                args.goal, args.hint, args.locale
            )
            hyb_ms = int((monotonic() - t0) * 1000)
            print_context("HybridPreprocessor", hyb_ctx, hyb_ms)
            if hyb_ctx.source == "deterministic":
                print("\n  ⚠  HybridPreprocessor fell back — use --verbose to see why.")

    print()


main()
