"""
Suite test — runs a battery of crawl trials and collates results.

Trials cover navigation goals, direct landing-zone (LZ) fact extraction, and
multi-hop navigation. Every run lands in its own timestamped folder under
crawl_logs/suites/, with one JSON log per trial and a _summary.json.

Usage:
    python3 suite_test.py                  # run all trials
    python3 suite_test.py --list           # show numbered trial list and exit
    python3 suite_test.py 12              # run trial #12 by number
    python3 suite_test.py 10-13           # run trials #10 through #13
    python3 suite_test.py 10-13 lz        # combine numeric and name/tag filters
    python3 suite_test.py iana lz        # run trials whose name or tag contains "iana" or "lz"

Filters are OR'd: a trial is included if it matches any filter argument.
Numeric and substring filters can be freely mixed.

Env vars (same as crawl_test.py):
    CHARLOTTE_LOCAL_MODEL         — model name (default: llama3:8b)
    CHARLOTTE_MODEL_TIMEOUT       — seconds before a model call is abandoned
    CHARLOTTE_MODEL_VERBOSE       — stream model tokens to stderr (default: false)
    CHARLOTTE_CONFIDENCE_THRESHOLD — minimum confidence to record a result (default: 0.70)
    CHARLOTTE_INTER_TRIAL_DELAY   — seconds to wait between trials (default: 2.0)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from charlotte import crawl
from charlotte.adapters.local import LocalAdapter
from charlotte.models import (
    BudgetExhausted,
    CandidatesExtracted,
    CrawlComplete,
    CrawlStarted,
    DestinationVerificationFailed,
    GoalPreprocessed,
    LinksRanked,
    ModelDecision,
    ModelEvaluating,
    PageFetched,
    PageSkipped,
    ResultFound,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_model_timeout_env = os.environ.get("CHARLOTTE_MODEL_TIMEOUT")
MODEL_TIMEOUT = float(_model_timeout_env) if _model_timeout_env else None
MODEL_VERBOSE = os.environ.get("CHARLOTTE_MODEL_VERBOSE", "").strip().lower() == "true"
CONFIDENCE_THRESHOLD = float(os.environ.get("CHARLOTTE_CONFIDENCE_THRESHOLD", "0.70"))
INTER_TRIAL_DELAY = float(os.environ.get("CHARLOTTE_INTER_TRIAL_DELAY", "2.0"))

SUITES_DIR = Path("crawl_logs") / "suites"

# ---------------------------------------------------------------------------
# Trial definitions
# ---------------------------------------------------------------------------

@dataclass
class Trial:
    name: str
    url: str
    goal: str
    max_pages: int = 10
    max_depth: int = 3
    max_results: int | None = 1
    tags: list[str] = field(default_factory=list)
    verify_destination: str = "relevance"  # default matches crawl()
    # Extra domains the crawler may follow beyond the start domain.  Used when
    # the target is on a related subdomain (e.g. docs.python.org from python.org).
    allowed_domains: list[str] | None = None
    # When set, the result URL must contain this substring for the trial to pass.
    # Prevents false positives where found=True but the wrong page was returned.
    expected_url_contains: str | None = None


# What makes a good trial
# -----------------------
# Charlotte is designed for targeted, specific goals — the kind a mission planner
# would produce after translating a concrete user need. Good trials have:
#   - One correct answer (a specific page, fact, or URL the planner had in mind)
#   - A verifiable expected result in suite_answers.json
#   - A start URL whose domain the crawler can navigate within
#
# Poor trial patterns to avoid:
#   - Vague or open-ended goals ("find a story about AI") — no single correct
#     answer, so Charlotte cannot meaningfully succeed or fail, and local models
#     hallucinate plausible-sounding URLs instead of reading the page
#   - Link-aggregator start URLs (HN, Reddit) where every result link is
#     off-domain — Charlotte's allowed_domains constraint means it can never
#     follow the results, and the model correctly returns found=False with 0
#     queued links every time
#   - JS-rendered pages (Swagger UIs, SPAs) where static HTML has no content

TRIALS: list[Trial] = [
    # --- Navigation: find a page or link ---
    Trial(
        name="python_tutorial_page",
        url="https://docs.python.org/3/",
        goal="Find the tutorial page for Python beginners",
        max_pages=5,
        tags=["navigation", "docs"],
        expected_url_contains="tutorial",
    ),
    Trial(
        name="python_pep_index",
        url="https://www.python.org",
        goal="Find the PEP index page listing Python Enhancement Proposals",
        max_pages=8,
        max_depth=4,
        tags=["navigation", "multi-hop"],
    ),
    Trial(
        name="python_glossary",
        url="https://docs.python.org/3/",
        goal="Find the Python glossary page",
        max_pages=5,
        tags=["navigation", "docs"],
        expected_url_contains="glossary",
    ),
    Trial(
        name="iana_about",
        url="https://www.iana.org/",
        goal="Find the About IANA page describing what IANA does",
        max_pages=5,
        tags=["navigation", "simple"],
    ),
    # --- Direct LZ: land on the answer page, extract a fact ---
    Trial(
        name="lz_zen_author",
        url="https://peps.python.org/pep-0020/",
        goal="Who is the author of the Zen of Python?",
        max_pages=3,
        max_depth=1,
        tags=["fact", "lz", "peps"],
        # LZ trials land directly on the answer page; relevance check is redundant
        # and fails when anchor_terms describe the sought value rather than the page.
        verify_destination="existence",
    ),
    Trial(
        name="lz_pep8_title",
        url="https://peps.python.org/pep-0008/",
        goal="What is the title of this PEP?",
        max_pages=3,
        max_depth=1,
        tags=["fact", "lz", "peps"],
        verify_destination="existence",
    ),
    Trial(
        name="lz_iana_root_count",
        url="https://www.iana.org/domains/root/servers",
        goal="How many root name servers are there?",
        max_pages=3,
        max_depth=1,
        tags=["fact", "lz", "iana"],
        verify_destination="existence",
    ),
    # --- Multi-hop fact: navigate to the page, then extract ---
    Trial(
        name="lz_pep8_created_year",
        url="https://peps.python.org/pep-0008/",
        goal="What year was this PEP first created?",
        max_pages=3,
        max_depth=1,
        tags=["fact", "lz", "peps"],
        verify_destination="existence",
    ),
    Trial(
        name="python_json_parse_fn",
        url="https://docs.python.org/3/library/json.html",
        goal="What is the name of the function used to parse a JSON string in Python?",
        max_pages=3,
        max_depth=1,
        tags=["fact", "lz", "docs"],
        verify_destination="existence",
    ),
    # --- Deep navigation: target is 2-3 hops from start, many wrong links at each step ---
    Trial(
        name="nav_itertools_page",
        url="https://docs.python.org/3/",
        goal="Find the itertools module reference page",
        max_pages=8,
        max_depth=3,
        tags=["navigation", "docs", "multi-hop"],
        expected_url_contains="itertools",
    ),
    Trial(
        name="nav_whatsnew_312",
        url="https://docs.python.org/3/",
        goal="Find the What's New in Python 3.12 page",
        max_pages=6,
        max_depth=3,
        tags=["navigation", "docs", "multi-hop"],
        expected_url_contains="3.12",
    ),
    Trial(
        name="nav_iana_port_assignments",
        url="https://www.iana.org/",
        goal="Find the page listing service name and port number assignments",
        max_pages=8,
        max_depth=3,
        tags=["navigation", "iana", "multi-hop"],
        expected_url_contains="service-names-port-numbers",
    ),
    # --- Multi-hop fact: navigate 2-3 hops to the answer page, then extract ---
    Trial(
        name="mh_functools_cache",
        url="https://docs.python.org/3/",
        goal="What is the name of the functools function that caches a function's return values to avoid recomputing them?",
        max_pages=8,
        max_depth=3,
        tags=["fact", "docs", "multi-hop"],
    ),
]

# ---------------------------------------------------------------------------
# Trial runner
# ---------------------------------------------------------------------------

@dataclass
class TrialResult:
    name: str
    url: str
    goal: str
    tags: list[str]
    found: bool = False
    answer: str | None = None
    result_url: str | None = None
    result_count: int = 0
    pages_visited: int = 0
    depth_reached: int = 0
    elapsed_ms: int = 0
    budget_exhausted: bool = False
    error: str | None = None
    expected_url_contains: str | None = None
    events: list[dict] = field(default_factory=list)

    def passed(self) -> bool:
        """True if the trial succeeded, including optional URL validation."""
        if self.error or not self.found:
            return False
        if self.expected_url_contains and not (
            self.result_url and self.expected_url_contains in self.result_url
        ):
            return False
        return True


async def run_trial(trial: Trial, adapter: LocalAdapter) -> TrialResult:
    result = TrialResult(name=trial.name, url=trial.url, goal=trial.goal, tags=trial.tags,
                         expected_url_contains=trial.expected_url_contains)
    run_start = monotonic()

    answers_collected: list[str | None] = []

    try:
        crawl_kwargs: dict = dict(
            model=adapter,
            max_pages=trial.max_pages,
            max_depth=trial.max_depth,
            max_results=trial.max_results,
            confidence_threshold=CONFIDENCE_THRESHOLD,
            stream=True,
            default_delay=1.0,
            verify_destination=trial.verify_destination,
        )
        if trial.allowed_domains is not None:
            crawl_kwargs["allowed_domains"] = trial.allowed_domains
        gen = crawl(trial.url, trial.goal, **crawl_kwargs)

        async for event in gen:
            elapsed_ms = int((monotonic() - run_start) * 1000)

            if isinstance(event, CrawlStarted):
                result.events.append({"type": "CrawlStarted", "elapsed_ms": elapsed_ms,
                                       "max_pages": event.max_pages, "max_depth": event.max_depth})

            elif isinstance(event, GoalPreprocessed):
                ctx = event.goal_context
                result.events.append({"type": "GoalPreprocessed", "elapsed_ms": elapsed_ms,
                                       "duration_ms": event.duration_ms, "source": event.source,
                                       "goal_type": ctx.goal_type if ctx else None,
                                       "goal_type_confidence": ctx.goal_type_confidence if ctx else None,
                                       "anchor_terms": ctx.anchor_terms if ctx else []})

            elif isinstance(event, LinksRanked):
                result.events.append({"type": "LinksRanked", "elapsed_ms": elapsed_ms,
                                       "page_url": event.page_url, "total_links": event.total_links,
                                       "duration_ms": event.duration_ms,
                                       "top_links": [{"url": lk.url, "text": lk.text, "score": lk.score}
                                                      for lk in event.top_links]})

            elif isinstance(event, CandidatesExtracted):
                result.events.append({"type": "CandidatesExtracted", "elapsed_ms": elapsed_ms,
                                       "page_url": event.page_url,
                                       "candidate_count": len(event.candidates),
                                       "duration_ms": event.duration_ms})

            elif isinstance(event, DestinationVerificationFailed):
                print(f"         verifier rejected {event.url}"
                      f"  score={event.result.score}  reason={event.result.reason}", flush=True)
                result.events.append({"type": "DestinationVerificationFailed", "elapsed_ms": elapsed_ms,
                                       "url": event.url, "mode": event.result.mode,
                                       "score": event.result.score, "reason": event.result.reason})

            elif isinstance(event, PageFetched):
                result.events.append({"type": "PageFetched", "elapsed_ms": elapsed_ms,
                                       "url": event.url, "depth": event.depth,
                                       "http_status": event.http_status, "fetch_ms": event.fetch_ms})

            elif isinstance(event, ModelEvaluating):
                print("         thinking...", flush=True)

            elif isinstance(event, ModelDecision):
                result.events.append({"type": "ModelDecision", "elapsed_ms": elapsed_ms,
                                       "url": event.url, "found": event.found,
                                       "confidence": event.confidence,
                                       "links_queued": event.links_queued,
                                       "links_suggested": event.links_suggested,
                                       "reasoning": event.reasoning})

            elif isinstance(event, ResultFound):
                answers_collected.append(event.answer)
                if result.result_url is None:
                    result.result_url = event.url
                result.events.append({"type": "ResultFound", "elapsed_ms": elapsed_ms,
                                       "url": event.url, "confidence": event.confidence,
                                       "result_index": event.result_index, "answer": event.answer})

            elif isinstance(event, PageSkipped):
                result.events.append({"type": "PageSkipped", "elapsed_ms": elapsed_ms,
                                       "url": event.url, "reason": event.reason,
                                       "error_type": event.error_type})

            elif isinstance(event, BudgetExhausted):
                result.budget_exhausted = True
                result.events.append({"type": "BudgetExhausted", "elapsed_ms": elapsed_ms,
                                       "pages_visited": event.pages_visited,
                                       "depth_reached": event.depth_reached,
                                       "best_candidate": event.best_candidate})

            elif isinstance(event, CrawlComplete):
                result.found = event.found
                result.result_count = event.result_count
                result.pages_visited = event.pages_visited
                result.depth_reached = event.depth_reached
                result.elapsed_ms = event.elapsed_ms
                result.events.append({"type": "CrawlComplete", "elapsed_ms": elapsed_ms,
                                       "found": event.found, "result_count": event.result_count,
                                       "pages_visited": event.pages_visited,
                                       "depth_reached": event.depth_reached,
                                       "crawl_elapsed_ms": event.elapsed_ms,
                                       "failure_mode": event.failure_mode.value if event.failure_mode else None,
                                       "failure_reason": event.failure_reason})

    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.elapsed_ms = int((monotonic() - run_start) * 1000)

    result.answer = answers_collected[0] if answers_collected else None
    return result


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def write_trial_log(run_dir: Path, idx: int, trial: Trial, result: TrialResult,
                    model_name: str) -> Path:
    path = run_dir / f"{idx:02d}_{trial.name}.json"
    log = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "trial": trial.name,
        "url": trial.url,
        "goal": trial.goal,
        "tags": trial.tags,
        "model": model_name,
        "params": {
            "max_pages": trial.max_pages,
            "max_depth": trial.max_depth,
            "max_results": trial.max_results,
            "confidence_threshold": CONFIDENCE_THRESHOLD,
            "verify_destination": trial.verify_destination,
            "allowed_domains": trial.allowed_domains,
        },
        "result": {
            "found": result.found,
            "answer": result.answer,
            "result_url": result.result_url,
            "result_count": result.result_count,
            "pages_visited": result.pages_visited,
            "depth_reached": result.depth_reached,
            "elapsed_ms": result.elapsed_ms,
            "budget_exhausted": result.budget_exhausted,
            "error": result.error,
        },
        "events": result.events,
    }
    path.write_text(json.dumps(log, indent=2))
    return path


def write_summary(run_dir: Path, results: list[TrialResult], model_name: str,
                  suite_elapsed_ms: int) -> Path:
    path = run_dir / "_summary.json"
    trials_data = [
        {
            "name": r.name,
            "url": r.url,
            "goal": r.goal,
            "tags": r.tags,
            "found": r.found,
            "answer": r.answer,
            "result_url": r.result_url,
            "pages_visited": r.pages_visited,
            "depth_reached": r.depth_reached,
            "elapsed_ms": r.elapsed_ms,
            "budget_exhausted": r.budget_exhausted,
            "error": r.error,
        }
        for r in results
    ]
    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "model": model_name,
        "confidence_threshold": CONFIDENCE_THRESHOLD,
        "suite_elapsed_ms": suite_elapsed_ms,
        "total": len(results),
        "passed": sum(1 for r in results if r.passed()),
        "failed": sum(1 for r in results if not r.passed() and not r.error),
        "errored": sum(1 for r in results if r.error),
        "trials": trials_data,
    }
    path.write_text(json.dumps(summary, indent=2))
    return path


_STATUS = {True: "PASS", False: "FAIL"}

def _tag_str(tags: list[str]) -> str:
    return " ".join(f"[{t}]" for t in tags)


def print_summary_table(results: list[TrialResult]) -> None:
    col_name  = max(len(r.name)   for r in results)

    header = (
        f"  {'NAME':<{col_name}}  {'STATUS':<6}  {'PGS':>3}  {'MS':>7}  "
        f"{'ANSWER / NOTE'}"
    )
    print()
    print("── suite results " + "─" * max(0, len(header) - 17))
    print(header)
    print("  " + "─" * (len(header) - 2))

    for r in results:
        if r.error:
            status = "ERROR "
        elif r.passed():
            status = "PASS"
        elif r.found:
            status = "BADURL"  # found=True but URL validation failed
        else:
            status = "FAIL"
        if r.error:
            note = textwrap.shorten(r.error, width=60)
        elif r.answer:
            note = textwrap.shorten(r.answer, width=60)
        elif r.found and not r.passed():
            note = f"wrong url: {(r.result_url or '')[-50:]}"
        elif r.found:
            note = "(no answer — navigation goal)"
        elif r.budget_exhausted:
            note = "(budget exhausted)"
        else:
            note = "(not found)"
        print(
            f"  {r.name:<{col_name}}  {status:<6}  {r.pages_visited:>3}  "
            f"{r.elapsed_ms:>7,}  {note}"
        )

    passed  = sum(1 for r in results if r.passed())
    failed  = sum(1 for r in results if not r.passed() and not r.error)
    errored = sum(1 for r in results if r.error)
    print()
    print(f"  {passed} passed  {failed} failed  {errored} errored  ({len(results)} total)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _print_trial_list() -> None:
    """Print the numbered trial list and exit."""
    print(f"  {'#':>3}  {'NAME':<30}  {'TAGS'}")
    print("  " + "─" * 60)
    for idx, trial in enumerate(TRIALS, 1):
        print(f"  {idx:>3}  {trial.name:<30}  {_tag_str(trial.tags)}")
    print()
    print(f"  {len(TRIALS)} trials total")


def _parse_args(args: list[str]) -> list[Trial]:
    """Parse CLI arguments into a list of trials to run.

    Numeric args (``12``, ``10-13``) select by 1-based position.
    String args filter by substring match against trial name or tags.
    Filters are OR'd; a trial is included if any filter matches.
    """
    if not args:
        return list(TRIALS)

    selected_indices: set[int] = set()
    text_filters: list[str] = []

    for arg in args:
        arg_lower = arg.lower()
        # Range: "10-13"
        if "-" in arg and not arg.startswith("-"):
            parts = arg.split("-", 1)
            try:
                lo, hi = int(parts[0]), int(parts[1])
                selected_indices.update(range(lo, hi + 1))
                continue
            except ValueError:
                pass
        # Single number: "12"
        try:
            selected_indices.add(int(arg))
            continue
        except ValueError:
            pass
        # Substring filter against name / tags
        text_filters.append(arg_lower)

    trials: list[Trial] = []
    for idx, trial in enumerate(TRIALS, 1):
        by_number = idx in selected_indices
        by_text = any(
            f in trial.name or f in " ".join(trial.tags)
            for f in text_filters
        )
        if by_number or by_text:
            trials.append(trial)

    return trials


async def main() -> None:
    args = sys.argv[1:]

    if "--list" in args:
        _print_trial_list()
        return

    trials = _parse_args(args)

    if not trials:
        print(f"No trials matched: {args}")
        print("Run with --list to see available trials.")
        sys.exit(1)

    adapter = LocalAdapter(timeout=MODEL_TIMEOUT, verbose=MODEL_VERBOSE)

    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    run_dir = SUITES_DIR / ts
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"Model:      {adapter._model}")
    print(f"Run dir:    {run_dir}")
    print(f"Trials:     {len(trials)}")
    print(f"Threshold:  {CONFIDENCE_THRESHOLD}")
    print()

    suite_start = monotonic()
    results: list[TrialResult] = []

    for idx, trial in enumerate(trials, 1):
        tags = _tag_str(trial.tags)
        print(f"[{idx:02d}/{len(trials):02d}] {trial.name}  {tags}")
        print(f"         {trial.url}")
        print(f"         {trial.goal}")

        result = await run_trial(trial, adapter)

        if result.error:
            status = "ERROR "
        elif result.passed():
            status = "PASS"
        elif result.found:
            status = "BADURL"
        else:
            status = "FAIL"
        answer_line = (
            f"answer: {textwrap.shorten(result.answer, width=70)}"
            if result.answer else
            ("error: " + textwrap.shorten(result.error, width=60) if result.error else "")
        )
        print(
            f"         {status}  pages={result.pages_visited}  "
            f"depth={result.depth_reached}  {result.elapsed_ms:,}ms"
            + (f"  {answer_line}" if answer_line else "")
        )
        print()

        write_trial_log(run_dir, idx, trial, result, adapter._model)
        results.append(result)

        if idx < len(trials):
            await asyncio.sleep(INTER_TRIAL_DELAY)

    suite_elapsed_ms = int((monotonic() - suite_start) * 1000)
    summary_path = write_summary(run_dir, results, adapter._model, suite_elapsed_ms)

    print_summary_table(results)
    print()
    print(f"Suite elapsed:  {suite_elapsed_ms:,}ms")
    print(f"Logs written →  {run_dir}/")
    print(f"Summary →       {summary_path}")


asyncio.run(main())
