"""
Compare two bench.py result files and print a side-by-side table.

Usage:
    python3 scripts/bench_compare.py crawl_logs/bench/v110.json crawl_logs/bench/current.json

Exit code 0 — newer is better or equal on pass rate.
Exit code 1 — newer has a lower pass rate (regression).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


_W = 32  # trial name column width


_REQUIRED_KEYS = frozenset({
    "label", "charlotte_version", "adapter_endpoint", "run_at",
    "passed", "total", "trials", "suite_elapsed_ms",
})


def _load(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read {path!r}: {exc}", file=sys.stderr)
        sys.exit(1)
    missing = _REQUIRED_KEYS - data.keys()
    if missing:
        print(
            f"error: {path!r} is missing required keys: {', '.join(sorted(missing))}",
            file=sys.stderr,
        )
        sys.exit(1)
    return data


def _pct(n: int, d: int) -> str:
    return f"{100*n//d}%" if d else "—"


def _delta(a: int | float, b: int | float, *, lower_is_better: bool = False) -> str:
    diff = b - a
    if diff == 0:
        return "  ="
    better = diff < 0 if lower_is_better else diff > 0
    sign = "+" if diff > 0 else ""
    marker = "▲" if better else "▼"
    return f"{marker} {sign}{diff:,}" if isinstance(diff, int) else f"{marker} {sign}{diff:.0f}"


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: bench_compare.py <baseline.json> <current.json>", file=sys.stderr)
        sys.exit(2)

    base = _load(sys.argv[1])
    curr = _load(sys.argv[2])

    base_by_name = {t["name"]: t for t in base["trials"]}
    curr_by_name = {t["name"]: t for t in curr["trials"]}
    all_names = list({t["name"] for t in base["trials"] + curr["trials"]})

    # ── header ──────────────────────────────────────────────────────────────
    label_a = base.get("label", Path(sys.argv[1]).stem)
    label_b = curr.get("label", Path(sys.argv[2]).stem)

    print()
    print(f"  charlotte benchmark comparison")
    print(f"  baseline : {label_a}  (charlotte {base.get('charlotte_version','?')})")
    print(f"           : endpoint {base.get('adapter_endpoint','?')}")
    print(f"  current  : {label_b}  (charlotte {curr.get('charlotte_version','?')})")
    print(f"           : endpoint {curr.get('adapter_endpoint','?')}")
    print(f"  run      : {base.get('run_at','?')[:19]}  vs  {curr.get('run_at','?')[:19]}")
    print()

    # ── per-trial table ──────────────────────────────────────────────────────
    col = f"{'':>{_W}}  {'STATUS':>8}  {'PAGES':>10}  {'MS':>16}"
    header_a = f"{'':>{_W}}  {'──BASELINE──':>8}  {'──BASELINE──':>10}  {'──BASELINE──':>16}"
    header_b = f"{'':>{_W}}  {'──CURRENT──':>8}  {'──CURRENT──':>10}  {'──CURRENT──':>16}"

    sep = "  " + "─" * (_W + 2 + 8 + 2 + 10 + 2 + 16)

    print(f"  {'TRIAL':<{_W}}  {'─PASS─':>8}  {'─PAGES─':>10}  {'─ELAPSED ms─':>16}")
    print(sep)

    regressions: list[str] = []

    for name in all_names:
        a = base_by_name.get(name)
        b = curr_by_name.get(name)

        def _status(t: dict | None) -> str:
            if t is None:
                return "—"
            if t.get("error"):
                return "ERR"
            return "PASS" if t.get("passed") else "FAIL"

        def _pages(t: dict | None) -> str:
            return str(t["pages_visited"]) if t else "—"

        def _ms(t: dict | None) -> str:
            return f"{t['elapsed_ms']:,}" if t else "—"

        sa, sb = _status(a), _status(b)
        pa = a["pages_visited"] if a else None
        pb = b["pages_visited"] if b else None
        ma = a["elapsed_ms"] if a else None
        mb = b["elapsed_ms"] if b else None

        # page and ms deltas
        pg_delta = _delta(pa, pb, lower_is_better=True) if pa is not None and pb is not None else "  —"
        ms_delta = _delta(ma, mb, lower_is_better=True) if ma is not None and mb is not None else "  —"

        # regression flag
        if sa == "PASS" and sb != "PASS":
            regressions.append(name)
            flag = " ◀ REGRESSION"
        elif sa != "PASS" and sb == "PASS":
            flag = " ◀ fixed"
        else:
            flag = ""

        print(f"  {name:<{_W}}  {sa:>4} → {sb:<4}  "
              f"{_pages(a):>4} → {_pages(b):<4} {pg_delta:>6}  "
              f"{_ms(a):>7} → {_ms(b):<7} {ms_delta:>8}{flag}")

    print(sep)

    # ── summary row ─────────────────────────────────────────────────────────
    bp = base.get("passed", 0)
    bt = base.get("total", len(all_names))
    cp = curr.get("passed", 0)
    ct = curr.get("total", len(all_names))

    bms = base.get("suite_elapsed_ms", 0)
    cms = curr.get("suite_elapsed_ms", 0)

    pass_delta = _delta(bp, cp)
    time_delta = _delta(bms // 1000, cms // 1000, lower_is_better=True)

    print(f"  {'TOTAL':<{_W}}  "
          f"{bp}/{bt} → {cp}/{ct}  {_pct(bp,bt)} → {_pct(cp,ct)} {pass_delta}  "
          f"{bms//1000:>4}s → {cms//1000:<4}s  {time_delta}")
    print()

    if regressions:
        print(f"  ⚠  regressions: {', '.join(regressions)}")
        print()
        sys.exit(1)


if __name__ == "__main__":
    main()
