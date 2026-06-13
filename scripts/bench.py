"""
Standalone benchmark harness for charlotte-crawler.

Run this script in any Python environment that has charlotte-crawler installed.
It uses only the public API present since v1.1.0, so it works unchanged in both
the v1.1.0 release venv and the current development tree.

Usage:
    python3 scripts/bench.py --label current
    python3 scripts/bench.py --label v1.1.0 --output crawl_logs/bench/v110.json

Then compare two result files:
    python3 scripts/bench_compare.py crawl_logs/bench/v110.json crawl_logs/bench/current.json

Setup for v1.1.0 comparison:
    python3 -m venv .bench-v110
    .bench-v110/bin/pip install "charlotte-crawler==1.1.0"
    .bench-v110/bin/python scripts/bench.py --label v1.1.0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

# ---------------------------------------------------------------------------
# Trial definitions — v1.1.0-compatible parameters only (no verify_destination,
# preprocessor, ranker, or other Phase C kwargs).
# ---------------------------------------------------------------------------

@dataclass
class Trial:
    name: str
    url: str
    goal: str
    max_pages: int = 10
    max_depth: int = 3
    expected_found: bool = True
    answer_contains: list[str] | None = None
    result_url_contains: str | None = None


TRIALS: list[Trial] = [
    # --- Navigation: shallow (1–2 hops) ---
    Trial(
        name="python_tutorial_page",
        url="https://docs.python.org/3/",
        goal="Find the tutorial page for Python beginners",
        max_pages=5,
        result_url_contains="docs.python.org",
    ),
    Trial(
        name="python_pep_index",
        url="https://www.python.org",
        goal="Find the PEP index page listing Python Enhancement Proposals",
        max_pages=8,
        max_depth=4,
        result_url_contains="python.org",
    ),
    Trial(
        name="python_glossary",
        url="https://docs.python.org/3/",
        goal="Find the Python glossary page",
        max_pages=5,
        result_url_contains="docs.python.org",
    ),
    Trial(
        name="iana_about",
        url="https://www.iana.org/",
        goal="Find the About IANA page describing what IANA does",
        max_pages=5,
        result_url_contains="iana.org",
    ),
    # --- Landing-zone facts: start on answer page, extract ---
    Trial(
        name="lz_zen_author",
        url="https://peps.python.org/pep-0020/",
        goal="Who is the author of the Zen of Python?",
        max_pages=3,
        max_depth=1,
        answer_contains=["Tim Peters"],
    ),
    Trial(
        name="lz_pep8_title",
        url="https://peps.python.org/pep-0008/",
        goal="What is the title of this PEP?",
        max_pages=3,
        max_depth=1,
        answer_contains=["Style Guide for Python Code"],
    ),
    Trial(
        name="lz_iana_root_count",
        url="https://www.iana.org/domains/root/servers",
        goal="How many root name servers are there?",
        max_pages=3,
        max_depth=1,
        answer_contains=["13"],
    ),
    Trial(
        name="lz_pep8_created_year",
        url="https://peps.python.org/pep-0008/",
        goal="What year was this PEP first created?",
        max_pages=3,
        max_depth=1,
        answer_contains=["2001"],
    ),
    Trial(
        name="python_json_parse_fn",
        url="https://docs.python.org/3/library/json.html",
        goal="What is the name of the function used to parse a JSON string in Python?",
        max_pages=3,
        max_depth=1,
        answer_contains=["loads", "json.loads"],
    ),
    # --- Navigation: deep (2–4 hops) ---
    Trial(
        name="nav_itertools_page",
        url="https://docs.python.org/3/",
        goal="Find the itertools module reference page",
        max_pages=8,
        max_depth=3,
        result_url_contains="library/itertools",
    ),
    Trial(
        name="nav_whatsnew_312",
        url="https://www.python.org",
        goal="Find the What's New in Python 3.12 page",
        max_pages=10,
        max_depth=4,
        result_url_contains="whatsnew/3.12",
    ),
    Trial(
        name="nav_iana_port_assignments",
        url="https://www.iana.org/",
        goal="Find the page listing service name and port number assignments",
        max_pages=8,
        max_depth=3,
        result_url_contains="service-names-port-numbers",
    ),
    Trial(
        name="mh_functools_cache",
        url="https://docs.python.org/3/",
        goal="What is the name of the functools function that caches a function's"
             " return values to avoid recomputing them?",
        max_pages=8,
        max_depth=3,
        answer_contains=["cache", "lru_cache"],
    ),
]

CONFIDENCE_THRESHOLD = 0.70
INTER_TRIAL_DELAY = 2.0


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_trial(trial: Trial, result: object) -> bool:
    """Return True if the result satisfies the trial's expectations."""
    if bool(result.found) != trial.expected_found:      # type: ignore[union-attr]
        return False
    if not trial.expected_found:
        return True
    if trial.answer_contains:
        answer = (result.answers or [None])[0]          # type: ignore[union-attr]
        if not answer:
            return False
        answer_lower = answer.lower()
        if not any(s.lower() in answer_lower for s in trial.answer_contains):
            return False
    if trial.result_url_contains:
        urls = result.result_urls                       # type: ignore[union-attr]
        if not urls or trial.result_url_contains not in urls[0]:
            return False
    return True


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_trial(trial: Trial, adapter: object) -> dict:
    from charlotte import crawl  # imported inside so the right version is used

    start = monotonic()
    error: str | None = None
    result = None

    try:
        result = await crawl(
            trial.url,
            trial.goal,
            model=adapter,
            max_pages=trial.max_pages,
            max_depth=trial.max_depth,
            confidence_threshold=CONFIDENCE_THRESHOLD,
            stream=False,
            default_delay=1.0,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    elapsed_ms = int((monotonic() - start) * 1000)

    passed = _score_trial(trial, result) if result and not error else False
    answer = (result.answers or [None])[0] if result else None    # type: ignore[union-attr]
    result_url = (result.result_urls or [None])[0] if result else None  # type: ignore[union-attr]

    return {
        "name": trial.name,
        "url": trial.url,
        "goal": trial.goal,
        "passed": passed,
        "found": result.found if result else False,           # type: ignore[union-attr]
        "answer": answer,
        "result_url": result_url,
        "pages_visited": result.pages_visited if result else 0,  # type: ignore[union-attr]
        "depth_reached": result.depth_reached if result else 0,  # type: ignore[union-attr]
        "budget_exhausted": result.budget_exhausted if result else False,  # type: ignore[union-attr]
        "elapsed_ms": elapsed_ms,
        "error": error,
    }


async def run_bench(label: str, out_path: Path) -> None:
    import charlotte
    from charlotte.adapters.local import LocalAdapter

    adapter = LocalAdapter()
    version = getattr(charlotte, "__version__", "unknown")

    print(f"charlotte {version}  label={label}")
    print(f"adapter endpoint: {getattr(adapter, '_endpoint', '?')}")
    print(f"trials: {len(TRIALS)}\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    suite_start = monotonic()
    trial_rows: list[dict] = []

    for i, trial in enumerate(TRIALS, 1):
        print(f"  [{i:02d}/{len(TRIALS)}] {trial.name} ...", end=" ", flush=True)
        if i > 1:
            await asyncio.sleep(INTER_TRIAL_DELAY)

        row = await run_trial(trial, adapter)
        trial_rows.append(row)

        status = "PASS" if row["passed"] else ("ERR " if row["error"] else "FAIL")
        print(f"{status}  {row['pages_visited']}p  {row['elapsed_ms']:,}ms")

    suite_elapsed_ms = int((monotonic() - suite_start) * 1000)
    passed = sum(1 for r in trial_rows if r["passed"])

    summary = {
        "label": label,
        "charlotte_version": version,
        "adapter_endpoint": getattr(adapter, "_endpoint", "unknown"),
        "run_at": datetime.now(timezone.utc).isoformat(),
        "confidence_threshold": CONFIDENCE_THRESHOLD,
        "suite_elapsed_ms": suite_elapsed_ms,
        "total": len(TRIALS),
        "passed": passed,
        "failed": len(TRIALS) - passed,
        "trials": trial_rows,
    }

    out_path.write_text(json.dumps(summary, indent=2))

    print(f"\n{'─'*40}")
    print(f"  passed {passed}/{len(TRIALS)}  total {suite_elapsed_ms/1000:.0f}s")
    print(f"  saved → {out_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _default_output(label: str) -> Path:
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    safe = label.replace("/", "-").replace(" ", "_")
    return Path("crawl_logs/bench") / f"{safe}_{ts}.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="charlotte-crawler benchmark harness")
    parser.add_argument("--label", required=True,
                        help="Short label for this run, e.g. 'v1.1.0' or 'current'")
    parser.add_argument("--output", metavar="PATH",
                        help="Output JSON path (default: crawl_logs/bench/<label>_<ts>.json)")
    args = parser.parse_args()

    out_path = Path(args.output) if args.output else _default_output(args.label)
    asyncio.run(run_bench(args.label, out_path))


if __name__ == "__main__":
    main()
