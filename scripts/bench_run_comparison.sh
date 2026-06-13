#!/usr/bin/env bash
# bench_run_comparison.sh — run back-to-back bench for v1.1.0 and current,
# then print the comparison table.
#
# Usage:
#   ./scripts/bench_run_comparison.sh
#
# What it does:
#   1. Creates a v1.1.0 venv at .bench-v110 if it doesn't already exist.
#   2. Runs bench.py under that venv (baseline).
#   3. Runs bench.py under the current Python (current dev build).
#   4. Prints the comparison table via bench_compare.py.
#
# Output files land in crawl_logs/bench/compare/<timestamp>/.
# Exit code mirrors bench_compare.py: 0 = equal or improved, 1 = regression.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="$REPO_ROOT/crawl_logs/bench/compare/$TIMESTAMP"
VENV_DIR="$REPO_ROOT/.bench-v110"
BASELINE_JSON="$OUT_DIR/v110.json"
CURRENT_JSON="$OUT_DIR/current.json"

mkdir -p "$OUT_DIR"

# ── 1. Ensure v1.1.0 venv ───────────────────────────────────────────────────
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "==> Creating v1.1.0 venv at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet "charlotte-crawler==1.1.0"
    echo "==> v1.1.0 installed."
else
    echo "==> Reusing existing venv at $VENV_DIR"
fi

# ── 2. Baseline run (v1.1.0) ────────────────────────────────────────────────
echo
echo "==> Running baseline (v1.1.0) ..."
"$VENV_DIR/bin/python" "$REPO_ROOT/scripts/bench.py" \
    --label v1.1.0 \
    --output "$BASELINE_JSON"

# ── 3. Current run ──────────────────────────────────────────────────────────
echo
echo "==> Running current build ..."
python3 "$REPO_ROOT/scripts/bench.py" \
    --label current \
    --output "$CURRENT_JSON"

# ── 4. Compare ──────────────────────────────────────────────────────────────
echo
echo "==> Results written to $OUT_DIR"
echo
python3 "$REPO_ROOT/scripts/bench_compare.py" "$BASELINE_JSON" "$CURRENT_JSON"
