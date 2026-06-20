# Field-test scripts

These scripts drive `crawl()` against real websites to check Charlotte end-to-end.
They are developer tooling, not part of the published library.

| Script | What it does | Output |
|---|---|---|
| `suite_test.py` | A battery of navigation / fact-extraction / multi-hop trials | `crawl_logs/suites/<timestamp>/` |
| `school_calendar_test.py` | Navigate to school calendars (JS-rendered), then resolve the calendar resource | `crawl_logs/school_calendars/<timestamp>/` |
| `parish_bulletin_test.py` | Retrieve the latest parish bulletin PDF | `crawl_logs/bulletins/<timestamp>/` |

Each run lands in its own timestamped folder with a per-trial log and a summary JSON
(the summary records the `provider:model` that ran, so cross-model runs are
distinguishable).

## Choosing the model

All three scripts pick their adapter the same way, through `adapter_factory.build_adapter()`.
Set **`CHARLOTTE_ADAPTER`**:

- `local` (default) → `LocalAdapter` (Ollama; no API key). Uses `CHARLOTTE_LOCAL_MODEL`
  (default `deepseek-r1:14b`).
- `groq` → `GroqAdapter`. Uses `GROQ_MODEL` and needs `GROQ_API_KEY`.

```bash
# Local (default) — nothing extra needed if Ollama is running
python3 scripts/suite_test.py

# Groq
CHARLOTTE_ADAPTER=groq GROQ_MODEL=llama-3.3-70b-versatile python3 scripts/suite_test.py
```

The full environment-variable contract lives in
[`adapter_factory.py`](adapter_factory.py)'s module docstring — that's the source of truth.

## Running against Groq

**1. Give it the key.** There's no secret store here, so put the key in a gitignored
`.env` at the repo root:

```
GROQ_API_KEY=gsk_your_key_here
```

`build_adapter()` loads `.env` automatically on the Groq path. An API key already
exported in your shell **wins over `.env`** — if a stale `GROQ_API_KEY` (e.g. from
`~/.bashrc`) differs from `.env`, the script prints a `WARNING:` and uses the shell
value. `unset GROQ_API_KEY` (or fix the rc file) to let `.env` win.

**2. Pick the model.** On the **free tier**, use a strong non-reasoning model:

```bash
CHARLOTTE_ADAPTER=groq GROQ_MODEL=llama-3.3-70b-versatile \
  python3 scripts/parish_bulletin_test.py
```

A **reasoning** model (`qwen/qwen3-32b`, closest to the local `deepseek-r1:14b`) is the
better comparison, but its thinking tokens plus a full page prompt overflow the
free-tier per-request token ceiling. Reasoning models need a paid (Dev) Groq tier.

**3. Pace it.** The free tier's per-minute and per-day token limits are tight. Raise
the pause between trials so the token window can refill:

```bash
CHARLOTTE_INTER_TRIAL_DELAY=60   # seconds between trials (default 3)
```

Even so, a multi-page crawl can exhaust the per-minute window mid-trial (a 429), and
heavy back-to-back runs can hit the daily quota (a 429 with a long `retry-after`). The
adapter retries rate limits, but the free tier genuinely struggles with multi-page
JS-rendered runs — a paid tier is the reliable path for a full comparison. Every Groq
failure is reported as a named `AdapterOutputError` with its HTTP status, so the cause
(401 key, 413 too-large, 429 rate limit, …) is always visible in the run's `PageSkipped`
events.

## JavaScript-rendered trials (`render_js`)

`school_calendar_test.py` renders pages with Playwright. That needs the `[playwright]`
extra and a browser (`pip install charlotte-crawler[playwright]` then
`playwright install chromium`), run with an interpreter that has them. On an OS
Playwright doesn't package a browser for, install Playwright into a virtualenv that
can reach a working Chromium and run the script with that interpreter, e.g.:

```bash
CHARLOTTE_ADAPTER=groq GROQ_MODEL=llama-3.3-70b-versatile CHARLOTTE_INTER_TRIAL_DELAY=60 \
  scripts/playwright_env/bin/python scripts/school_calendar_test.py
```
