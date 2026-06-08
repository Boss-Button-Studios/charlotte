"""
Batch preprocessor brainstorm — run a curated set of goals through both
preprocessors and write a timestamped log for review.

Each goal is tagged with its expected goal_type. The log notes mismatches
between expected, deterministic, and hybrid classifications and flags
cases where the deterministic heuristic is known to fall short.

Usage:
    python3 brainstorm_preprocessor.py
    python3 brainstorm_preprocessor.py --deterministic-only
    python3 brainstorm_preprocessor.py --output results.txt

Env vars:
    CHARLOTTE_LOCAL_MODEL    — model for HybridPreprocessor (default: deepseek-r1:14b)
    CHARLOTTE_LOCAL_BASE_URL — inference server base URL (default: http://localhost:11434)
    CHARLOTTE_MODEL_TIMEOUT  — seconds before the model call is abandoned
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from time import monotonic
from typing import TextIO

from charlotte.core.goal_preprocessor import (
    DeterministicPreprocessor,
    HybridPreprocessor,
)
from charlotte.models import GoalContext

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("CHARLOTTE_LOCAL_BASE_URL", "http://localhost:11434")
MODEL    = os.environ.get("CHARLOTTE_LOCAL_MODEL",    "deepseek-r1:14b")
_env_to  = os.environ.get("CHARLOTTE_MODEL_TIMEOUT")
try:
    TIMEOUT = float(_env_to) if _env_to else None
except ValueError:
    raise ValueError(
        f"CHARLOTTE_MODEL_TIMEOUT must be a number, got {_env_to!r}"
    ) from None

# ---------------------------------------------------------------------------
# Goal list
# ---------------------------------------------------------------------------

_VALID_GOAL_TYPES: frozenset[str] = frozenset({
    "navigation", "phone_extraction", "date_extraction", "address_extraction",
    "price_extraction", "document_link", "freeform_fact",
})


@dataclass(frozen=True)
class GoalSpec:
    expected_type: str
    goal:          str
    hint:          str | None
    notes:         str          # why this case is interesting

    def __post_init__(self) -> None:
        if self.expected_type not in _VALID_GOAL_TYPES:
            raise ValueError(f"invalid expected_type {self.expected_type!r} in GoalSpec")


GOALS: list[GoalSpec] = [
    # ── navigation ──────────────────────────────────────────────────────────
    GoalSpec("navigation", "Find the contact page", None,
             "baseline navigation"),
    GoalSpec("navigation", "Where do I sign up for the newsletter?", None,
             "question form; 'sign up' not in stop-words"),
    GoalSpec("navigation", "Go to the careers section", "main nav",
             "hint should surface 'careers' as anchor"),

    # ── phone_extraction ────────────────────────────────────────────────────
    GoalSpec("phone_extraction", "What is the customer service phone number?", None,
             "canonical phrasing"),
    GoalSpec("phone_extraction", "Find the assistant manager's phone numner", None,
             "real-world typo; multi-word entity"),
    GoalSpec("phone_extraction", "Get me the main office number", None,
             "'number' alone — det. should still trigger 'phone' keyword... wait, no"),

    # ── date_extraction ─────────────────────────────────────────────────────
    GoalSpec("date_extraction", "When was this article published?", None,
             "question form; no 'date' keyword — det. will miss"),
    GoalSpec("date_extraction", "Find the date of the next board meeting", None,
             "has 'date' keyword; det. should hit"),
    GoalSpec("date_extraction", "What is the effective date of the new policy?", None,
             "'date' present but buried"),

    # ── address_extraction ───────────────────────────────────────────────────
    GoalSpec("address_extraction", "What is the company's mailing address?", None,
             "has 'address' keyword"),
    GoalSpec("address_extraction", "Where is the nearest branch located?", "locations page",
             "no 'address' keyword — det. will miss; hint provided"),

    # ── price_extraction ────────────────────────────────────────────────────
    GoalSpec("price_extraction", "How much does the professional plan cost?", None,
             "has 'cost' keyword"),
    GoalSpec("price_extraction", "What is the membership fee?", None,
             "no 'price'/'cost' keyword — det. will misclassify as navigation"),
    GoalSpec("price_extraction", "Find the subscription pricing", "pricing page",
             "has 'price' substring"),

    # ── document_link ────────────────────────────────────────────────────────
    GoalSpec("document_link", "Download the latest annual report", None,
             "'download' trigger"),
    GoalSpec("document_link", "Find the latest bulletin", None,
             "no download/pdf keyword — det. will miss"),
    GoalSpec("document_link", "Get the employee handbook PDF", None,
             "'pdf' keyword"),

    # ── freeform_fact ────────────────────────────────────────────────────────
    GoalSpec("freeform_fact", "What are the business hours?", None,
             "no keyword — det. always misses freeform_fact"),
    GoalSpec("freeform_fact", "Who is the current CEO?", None,
             "named-entity extraction; no keyword"),
    GoalSpec("freeform_fact", "What is the return policy?", None,
             "prose fact; no keyword"),
]

# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _tick(actual: str, expected: str) -> str:
    return "✓" if actual == expected else f"✗ (expected {expected})"


def _fmt_list(items: list[str]) -> str:
    return ", ".join(repr(s) for s in items) if items else "(none)"


def _fmt_synonyms(synonyms: dict[str, list[str]]) -> str:
    if not synonyms:
        return "(none)"
    lines = []
    for k, vs in synonyms.items():
        vals = ", ".join(repr(v) for v in vs) if vs else "(no expansions)"
        lines.append(f"      {k!r:24s} → {vals}")
    return "\n" + "\n".join(lines)


def _fmt_warnings(warnings: list[str]) -> str:
    if not warnings:
        return "(none)"
    return "\n" + "\n".join(f"      ! {w}" for w in warnings)


def write_context(
    out: TextIO,
    label: str,
    ctx: GoalContext,
    elapsed_ms: int,
    expected_type: str,
) -> None:
    tag = f"[{ctx.source}]"
    if ctx.model_used:
        tag += f" model={ctx.model_used}"
    out.write(f"\n  ── {label}  {tag}  ({elapsed_ms:,}ms)\n")
    out.write(f"  goal_type      {ctx.goal_type}  {_tick(ctx.goal_type, expected_type)}"
              f"  (confidence {ctx.goal_type_confidence:.2f})\n")
    out.write(f"  anchor_terms   {_fmt_list(ctx.anchor_terms)}\n")
    out.write(f"  synonyms       {_fmt_synonyms(ctx.synonyms)}\n")
    out.write(f"  negative_terms {_fmt_list(ctx.negative_terms)}\n")
    out.write(f"  regex_hints    {_fmt_list(ctx.regex_hints)}\n")
    out.write(f"  warnings       {_fmt_warnings(ctx.validation_warnings)}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace, out: TextIO) -> None:
    det = DeterministicPreprocessor()
    hyb = HybridPreprocessor(base_url=BASE_URL, model=MODEL, timeout=TIMEOUT) \
          if not args.deterministic_only else None

    out.write(f"Charlotte preprocessor brainstorm\n")
    out.write(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
    out.write(f"Model:     {MODEL}  base_url={BASE_URL}\n")
    out.write(f"Goals:     {len(GOALS)}\n")
    out.write("=" * 70 + "\n")

    for spec in GOALS:
        out.write(f"\n{'═' * 70}\n")
        out.write(f"  [{spec.expected_type}]\n")
        out.write(f"  Goal:  {spec.goal!r}\n")
        if spec.hint:
            out.write(f"  Hint:  {spec.hint!r}\n")
        if spec.notes:
            out.write(f"  Notes: {spec.notes}\n")

        # Deterministic
        t0 = monotonic()
        det_ctx = det(spec.goal, spec.hint, "en_US")
        det_ms = int((monotonic() - t0) * 1000)
        write_context(out, "DeterministicPreprocessor", det_ctx, det_ms,
                      spec.expected_type)

        # Hybrid
        if hyb is not None:
            print(f"  → hybrid: {spec.goal[:60]!r}", file=sys.stderr)
            t0 = monotonic()
            hyb_ctx = hyb(spec.goal, spec.hint, "en_US")
            hyb_ms = int((monotonic() - t0) * 1000)
            write_context(out, "HybridPreprocessor", hyb_ctx, hyb_ms,
                          spec.expected_type)
            if hyb_ctx.source == "deterministic":
                out.write("  ⚠  HybridPreprocessor fell back — model call failed.\n")

        out.flush()

    out.write(f"\n{'=' * 70}\n")
    out.write("Done.\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run all brainstorm goals through Charlotte's preprocessors."
    )
    parser.add_argument("--deterministic-only", action="store_true",
                        help="Skip HybridPreprocessor (no model calls)")
    parser.add_argument("--output", metavar="FILE", default=None,
                        help="Write log to FILE in addition to stdout "
                             "(default: logs/preprocessor_brainstorm_<timestamp>.txt)")
    args = parser.parse_args()

    # Default output path
    if args.output is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        logs_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
        os.makedirs(logs_dir, exist_ok=True)
        args.output = os.path.join(logs_dir, f"preprocessor_brainstorm_{ts}.txt")

    buf = StringIO()
    run(args, buf)
    text = buf.getvalue()

    sys.stdout.write(text)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(text)

    print(f"\nLog written to: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
