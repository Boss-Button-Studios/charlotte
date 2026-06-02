"""
Score a suite run against the answer key.

Loads _summary.json from a suite run directory, compares each trial's result
against suite_answers.json, and prints a scored table.

Usage:
    python3 score_suite.py                          # score the latest run
    python3 score_suite.py crawl_logs/suites/<ts>/  # score a specific run

Exit code: 0 if all expected trials passed, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ANSWERS_FILE = Path("suite_answers.json")
SUITES_DIR = Path("crawl_logs") / "suites"


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _latest_run() -> Path:
    runs = sorted(SUITES_DIR.iterdir()) if SUITES_DIR.exists() else []
    if not runs:
        print(f"No suite runs found in {SUITES_DIR}/")
        sys.exit(1)
    return runs[-1]


def _check_answer(actual: str | None, answer_contains: list[str] | None) -> bool:
    """True if actual answer satisfies the answer_contains spec."""
    if answer_contains is None:
        return True  # navigation goal — no answer value expected
    if actual is None:
        return False
    actual_lower = actual.lower()
    return any(expected.lower() in actual_lower for expected in answer_contains)


def _check_url(actual_url: str | None, url_contains: str | None) -> bool:
    if url_contains is None:
        return True
    if actual_url is None:
        return False
    return url_contains.lower() in actual_url.lower()


def score_trial(trial: dict, key: dict) -> tuple[str, str]:
    """Return (verdict, note) for one trial.

    verdict: "PASS", "FAIL", "SKIP" (trial not in answer key), "ERROR"
    """
    if trial.get("error"):
        return "ERROR", trial["error"]

    found_ok    = trial["found"] == key["found"]
    answer_ok   = _check_answer(trial.get("answer"), key["answer_contains"])
    url_ok      = _check_url(trial.get("result_url"), key.get("result_url_contains"))

    if found_ok and answer_ok and url_ok:
        answer_str = trial.get("answer") or "(no answer)"
        return "PASS", answer_str

    parts: list[str] = []
    if not found_ok:
        parts.append(f"found={trial['found']} (expected {key['found']})")
    if not answer_ok:
        parts.append(
            f"answer={trial.get('answer')!r} (expected one of {key['answer_contains']})"
        )
    if not url_ok:
        parts.append(
            f"url={trial.get('result_url')!r} (expected to contain {key['result_url_contains']!r})"
        )
    return "FAIL", "; ".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    run_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else _latest_run()
    summary_path = run_dir / "_summary.json"

    if not summary_path.exists():
        print(f"No _summary.json found in {run_dir}")
        return 1
    if not ANSWERS_FILE.exists():
        print(f"Answer key not found: {ANSWERS_FILE}")
        return 1

    summary = json.loads(summary_path.read_text())
    answers = json.loads(ANSWERS_FILE.read_text())
    key_map: dict[str, dict] = answers["trials"]

    trials = summary["trials"]
    verdicts: list[tuple[str, dict, str, str]] = []  # (verdict, trial, note, key_note)

    col_name = max(len(t["name"]) for t in trials)

    print(f"Run:    {run_dir}")
    print(f"Model:  {summary.get('model', '?')}")
    print(f"Trials: {len(trials)}")
    print()

    header = f"  {'NAME':<{col_name}}  {'VERDICT':<7}  DETAIL"
    print("── scores " + "─" * max(0, len(header) - 10))
    print(header)
    print("  " + "─" * (len(header) - 2))

    for trial in trials:
        name = trial["name"]
        key = key_map.get(name)
        if key is None:
            verdict, note = "SKIP", "not in answer key"
        else:
            verdict, note = score_trial(trial, key)
            if verdict == "PASS" and key.get("note"):
                note = f"{note}  [{key['note']}]"
        verdicts.append((verdict, trial, note, key.get("note", "") if key else ""))
        print(f"  {name:<{col_name}}  {verdict:<7}  {note}")

    passed  = sum(1 for v, *_ in verdicts if v == "PASS")
    failed  = sum(1 for v, *_ in verdicts if v == "FAIL")
    errored = sum(1 for v, *_ in verdicts if v == "ERROR")
    skipped = sum(1 for v, *_ in verdicts if v == "SKIP")

    print()
    print(f"  {passed} passed  {failed} failed  {errored} errored  {skipped} skipped  ({len(verdicts)} total)")
    print(f"  Score: {passed}/{passed + failed + errored} ({100 * passed // max(1, passed + failed + errored)}%)")
    print()
    print(f"Answer key: {ANSWERS_FILE}")

    return 0 if (failed == 0 and errored == 0) else 1


sys.exit(main())
