"""
Adapter factory for the field-test scripts (suite_test, parish_bulletin_test,
school_calendar_test).

One job: read CHARLOTTE_ADAPTER and hand back the right model adapter, already
configured, plus a short label for the run header. This is the single place that
knows how to build each provider, so every field script switches between local
and Groq the same way — set one environment variable, run the same suite.

    CHARLOTTE_ADAPTER=local   (default)  → LocalAdapter   (Ollama; no API key)
    CHARLOTTE_ADAPTER=groq               → GroqAdapter    (needs GROQ_API_KEY)

Why a factory and not just `LocalAdapter()` in each script: the same field suite
must run unchanged against a different model so we can compare behaviour. Putting
the selection here keeps that swap honest — identical trials, only the model
moves — and keeps the provider-specific construction details out of every script.

Environment variables
---------------------
Shared:
    CHARLOTTE_ADAPTER         provider to use: "local" (default) or "groq".

Local path (Ollama):
    CHARLOTTE_LOCAL_MODEL     model name        (LocalAdapter default: deepseek-r1:14b)
    CHARLOTTE_LOCAL_BASE_URL  Ollama base URL   (LocalAdapter default: localhost)
    CHARLOTTE_MODEL_TIMEOUT   seconds per model call before it is abandoned
    CHARLOTTE_MODEL_VERBOSE   "true" streams model tokens to stderr

Groq path:
    GROQ_API_KEY              required; the GroqAdapter raises a clear, named
                              CharlotteConfigError if it is missing. If not already
                              exported, it is read from a gitignored .env at the
                              project root (NAME=value lines; an exported value wins).
    GROQ_MODEL                model id (default: llama-3.1-8b-instant).
                              On the free tier (6 000 tokens/request), use a strong
                              non-reasoning model: llama-3.3-70b-versatile. A reasoning
                              model (qwen/qwen3-32b) matches the local deepseek-r1:14b
                              more closely, but its thinking tokens plus a full page
                              prompt overflow the 6 000-token per-request cap (413) —
                              reasoning models need a paid Groq tier.

Groq rate limits: the free tier shares a ~6 000 tokens-per-minute sliding window
across all requests. The GroqAdapter already retries 429s (max_retries=3, honours
retry-after), but a dense suite can still outrun the window. When running a suite
against Groq, raise the script's inter-trial pause — e.g. CHARLOTTE_INTER_TRIAL_DELAY=15
— so the window has time to refill between trials.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Default Groq model. Kept here (not imported from the adapter) so the suite log
# header can record the exact id that will be used even before the adapter is built.
_GROQ_DEFAULT_MODEL = "llama-3.1-8b-instant"

# Project root holds an optional .env (gitignored) with GROQ_API_KEY / GROQ_MODEL.
# adapter_factory.py lives in scripts/, so the root is its parent's parent.
_DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _load_dotenv_for_groq() -> None:
    """Populate os.environ from the project-root .env, for the Groq path only.

    There is no secret store on this box, so the Groq key lives in a gitignored
    .env. We parse simple NAME=value lines ourselves rather than add a dependency
    (python-dotenv) for one file. An already-set environment variable always wins,
    so an explicit `GROQ_API_KEY=... python3 ...` on the command line overrides the
    file. Values are never logged — they flow straight into os.environ and on to
    the adapter, which reads GROQ_API_KEY itself.
    """
    if not _DOTENV_PATH.is_file():
        return
    try:
        lines = _DOTENV_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return  # An unreadable .env is not fatal: the adapter will report a missing key.
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        # Strip surrounding quotes a user may have added; keep the value otherwise intact.
        value = value.strip().strip('"').strip("'")
        if not name:
            continue
        if name not in os.environ:
            os.environ[name] = value
        elif os.environ[name] != value:
            # The environment already has a *different* value, which wins. For the API
            # key this is a silent footgun: a stale GROQ_API_KEY exported from ~/.bashrc
            # shadows the fresh one in .env, and every call then 401s. Warn loudly (no
            # secret printed) instead of letting it pass unnoticed.
            print(
                f"WARNING: {name} is set in your shell and differs from .env — the "
                f"shell value is being used. `unset {name}` to use .env instead.",
                file=sys.stderr,
            )


def _build_local() -> tuple[object, str]:
    """Construct a LocalAdapter (Ollama) from the CHARLOTTE_MODEL_* environment.

    Timeout and verbose are read here so the local path honours the same env vars
    the suite scripts have always documented; the adapter reads model name and
    base URL itself.
    """
    from charlotte.adapters.local import LocalAdapter

    timeout_env = os.environ.get("CHARLOTTE_MODEL_TIMEOUT")
    timeout = float(timeout_env) if timeout_env else None
    verbose = os.environ.get("CHARLOTTE_MODEL_VERBOSE", "").strip().lower() == "true"

    adapter = LocalAdapter(timeout=timeout, verbose=verbose)
    return adapter, f"local:{adapter._model}"


def _build_groq() -> tuple[object, str]:
    """Construct a GroqAdapter from GROQ_API_KEY / GROQ_MODEL.

    A missing key surfaces as the adapter's own named CharlotteConfigError, which
    already explains how to fix it — we do not pre-empt or reword that message.
    """
    from charlotte.adapters.groq import GroqAdapter

    # Pick up GROQ_API_KEY / GROQ_MODEL from the project-root .env if not already
    # exported, so the field suites run without the caller managing the environment.
    _load_dotenv_for_groq()

    model = os.environ.get("GROQ_MODEL", _GROQ_DEFAULT_MODEL).strip() or _GROQ_DEFAULT_MODEL
    adapter = GroqAdapter(model=model)
    return adapter, f"groq:{model}"


def build_adapter() -> tuple[object, str]:
    """Build the model adapter selected by CHARLOTTE_ADAPTER.

    Returns:
        (adapter, label) — the adapter to pass as ``model=`` to ``crawl()``, and a
        short "provider:model" label for the run header and summary metadata.

    Raises:
        ValueError: CHARLOTTE_ADAPTER is set to an unknown provider. Listing the
            valid choices is friendlier than a later, more cryptic failure.
        CharlotteConfigError: the chosen provider is misconfigured (e.g. Groq
            without an API key) — raised by the adapter itself.
    """
    # Treat the input as untrusted: normalise case/whitespace, default to local.
    provider = os.environ.get("CHARLOTTE_ADAPTER", "local").strip().lower() or "local"

    if provider == "local":
        return _build_local()
    if provider == "groq":
        return _build_groq()

    raise ValueError(
        f"Unknown CHARLOTTE_ADAPTER={provider!r}. Use 'local' (Ollama) or 'groq'."
    )
