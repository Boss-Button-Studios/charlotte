"""
School-calendar retrieval — exploratory test of Charlotte against calendar targets.

This deliberately draws Charlotte's identity boundary in the harness:

    Charlotte NAVIGATES to the resource pointer (a PDF link, a Google Drive
    /view link, a page carrying a Google Calendar embed). She does NOT parse
    structured data. A thin, platform-specific *resolver* (below, clearly
    downstream of Charlotte) turns that pointer into the machine-readable
    artifact — a PDF, a Drive download, or an .ics feed — and a real pipeline
    would then parse the artifact into JSON and filter by date range.

Trials (see docs / schools.txt):
  T1  St. John's Prep   — document_link to a Finalsite PDF (Charlotte downloads it).
  T2  Museum School     — navigation to a Google Drive /view link (resolver tries
                          the view->download transform; a learning probe).
  T4  Murdock (LMSV)    — navigation to a page with a Google Calendar embed
                          (resolver extracts the cid -> public .ics feed).
  (T3 Danvers is omitted — site-wide 403, even robots.txt; an honest decline.)

All output lands under crawl_logs/school_calendars/<timestamp>/:
    <slug>/events.jsonl    — per-event diagnostic log
    <slug>/<artifact>      — anything Charlotte or the resolver retrieved
    _summary.json          — per-trial outcome table for the whole run

Usage:
    .venv/bin/python scripts/school_calendar_test.py

Note: render_js=True needs the project virtualenv (Playwright 1.60.0) and the
Chromium binary (see scripts/parish_bulletin_test.py for setup notes).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from urllib.parse import quote, unquote, urlsplit

import httpx

import charlotte
from charlotte import crawl
from adapter_factory import build_adapter, env_float
from charlotte.adapters.base import AdapterProtocol
from charlotte.core import model_metrics
from charlotte.core.normalizer import validate_url_safety
from charlotte.models import (
    BudgetExhausted,
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

CHROMIUM_EXECUTABLE: str | None = None  # use Playwright's downloaded Chromium

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OUT_DIR = Path("crawl_logs") / "school_calendars"
CONFIDENCE_THRESHOLD = 0.70
# Seconds to wait between trials. Raise it (e.g. 30) to stay under Groq's free-tier
# 6 000 TPM window when running against a Groq model.
INTER_TRIAL_DELAY = env_float("CHARLOTTE_INTER_TRIAL_DELAY", 3.0)

# A browser-like UA for the downstream resolver's plain-HTTP fetches. The
# resolver is NOT Charlotte; it is the platform-transform stage that a real
# pipeline would run after Charlotte hands off the pointer.
_RESOLVER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

TRIALS = [
    {
        "name": "St. John's Prep (Finalsite PDF)",
        "slug": "stjohns_prep",
        "url":  "https://www.stjohnsprep.org/",
        # "PDF" + "download" trip the document_link goal type, so Charlotte
        # captures and delivers the bytes (the PDF link IS the artifact).
        "goal": "Find and download the preliminary 2026-2027 calendar PDF",
        "navigation_hint": (
            "School calendars usually live under an 'About', 'Academics', "
            "'Calendar', or 'Resources' section and are often a PDF. Download it."
        ),
        "render_js": True,
        "resolver": "pdf",
    },
    {
        "name": "Museum School (Google Drive)",
        "slug": "museum_school",
        "url":  "https://www.museumschool.org/",
        # Navigation: Charlotte returns the Drive /view pointer; the resolver
        # attempts the view->download transform. A learning probe.
        "goal": "Find the link to the current 2026-2027 school calendar",
        "navigation_hint": (
            "Look for a 'Calendar' link. The calendar may be hosted on Google "
            "Drive — returning the link to it is enough."
        ),
        "render_js": True,
        "resolver": "gdrive",
    },
    {
        "name": "Murdock / LMSV (Google Calendar)",
        "slug": "murdock_lmsv",
        "url":  "https://www.lmsvschools.org/murdock/",
        # Navigation: the calendar is an embedded Google Calendar widget; Charlotte
        # returns the page carrying it, and the resolver extracts the cid -> .ics.
        "goal": "Find the school's events calendar",
        "navigation_hint": (
            "The events calendar may be an embedded widget on a page rather than "
            "a separate document. Returning the page that shows the calendar is enough."
        ),
        "render_js": True,
        "resolver": "gcal",
    },
]

# ---------------------------------------------------------------------------
# Per-trial result
# ---------------------------------------------------------------------------

@dataclass
class TrialResult:
    name: str
    slug: str
    url: str
    goal_type: str | None = None          # how Charlotte classified the goal
    found: bool = False
    result_url: str | None = None
    suggested_filename: str | None = None
    content_length: int = 0
    pages_visited: int = 0
    navigate_ms: int = 0                   # Charlotte's navigation time
    resolve_ms: int = 0                    # downstream resolver time
    elapsed_ms: int = 0                    # navigate + resolve
    model_calls: dict = field(default_factory=dict)
    model_calls_total: int = 0
    resolution: dict = field(default_factory=dict)  # downstream resolver outcome
    error: str | None = None
    events: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Downstream resolvers — the platform-transform hand-off (NOT Charlotte)
# ---------------------------------------------------------------------------

_DRIVE_ID_RE = re.compile(r"/file/d/([^/]+)/|[?&]id=([^&]+)")
# Google Calendar identifies the calendar via cid= (share link) OR src= (embed).
# Both carry the calendar id, sometimes percent-encoded (e.g. %40 for @).
_GCAL_CALID_RE = re.compile(r"[?&](?:cid|src)=([^\"'&\s]+)")


async def _resolve_gdrive(result_url: str, out_dir: Path) -> dict:
    """Transform a Drive /view link into a download and report what comes back.

    Public Drive files download directly; non-public ones return an HTML login
    or 'confirm download' interstitial. Either way we learn whether the pointer
    Charlotte found is actually retrievable.
    """
    m = _DRIVE_ID_RE.search(result_url or "")
    file_id = (m.group(1) or m.group(2)) if m else None
    if not file_id:
        return {"ok": False, "reason": "no Drive file id in result_url", "result_url": result_url}
    dl = f"https://drive.google.com/uc?export=download&id={quote(file_id, safe='')}"
    try:
        # The resolver fetches outside Charlotte's engine, so re-apply the SSRF gate
        # to these semi-trusted, model-derived URLs (CR, PR #47).
        validate_url_safety(dl)
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": _RESOLVER_UA}) as c:
            r = await c.get(dl)
            ctype = r.headers.get("content-type", "")
            body = r.content
            is_pdf = body[:5] == b"%PDF-"
            looks_html = b"<html" in body[:2000].lower()
            out = {
                "ok": is_pdf, "file_id": file_id, "download_url": dl,
                "status": r.status_code, "content_type": ctype, "bytes": len(body),
                "is_pdf": is_pdf,
                "note": ("public PDF retrieved" if is_pdf else
                         "HTML interstitial (login / virus-scan confirm) — not public-downloadable"
                         if looks_html else "non-PDF binary"),
            }
            if is_pdf:
                path = out_dir / "calendar_from_drive.pdf"
                path.write_bytes(body)
                out["saved"] = str(path)
            return out
    except Exception as exc:  # noqa: BLE001 — exploratory probe, report any failure
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "download_url": dl}


async def _resolve_gcal(result_url: str, out_dir: Path) -> dict:
    """Resolve a Google Calendar pointer to its public .ics feed.

    The calendar id may be in the pointer itself (a calendar.google.com embed/share
    URL with src=/cid=) OR embedded in an in-scope page Charlotte returned. Try the
    pointer first, then fall back to scanning the page. Demonstrates the
    navigation->feed->artifact path.
    """
    try:
        # SSRF gate on these out-of-engine, model-derived fetches (CR, PR #47).
        validate_url_safety(result_url)
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": _RESOLVER_UA}) as c:
            ids = _GCAL_CALID_RE.findall(result_url)         # pointer carries it directly?
            source = "result_url"
            if not ids:
                page = await c.get(result_url)               # else scan the page it points at
                ids = _GCAL_CALID_RE.findall(page.text)
                source = "result page"
            ids = list(dict.fromkeys(unquote(i) for i in ids))
            if not ids:
                return {"ok": False, "reason": "no Google Calendar id in pointer or page",
                        "result_url": result_url}
            cal_id = ids[0]
            ics_url = (f"https://calendar.google.com/calendar/ical/"
                       f"{quote(cal_id, safe='')}/public/basic.ics")
            validate_url_safety(ics_url)
            ics = await c.get(ics_url)
            is_ical = "BEGIN:VCALENDAR" in ics.text
            events = ics.text.count("BEGIN:VEVENT")
            jun = len(re.findall(r"DTSTART[^:]*:202606\d{2}", ics.text))  # demo slice
            out = {
                "ok": is_ical, "calendar_id": cal_id, "id_source": source,
                "ics_url": ics_url, "is_ical": is_ical,
                "total_events": events, "june_2026_events": jun,
            }
            if is_ical:
                path = out_dir / "calendar.ics"
                path.write_text(ics.text, encoding="utf-8")
                out["saved"] = str(path)
            return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "result_url": result_url}


def _resolve_pdf(result: TrialResult, out_dir: Path) -> dict:
    """For document_link trials Charlotte already downloaded the PDF; just confirm it."""
    if result.suggested_filename:
        path = out_dir / result.suggested_filename
        is_pdf = path.exists() and path.read_bytes()[:5] == b"%PDF-"
        return {"ok": is_pdf, "file": str(path) if path.exists() else None,
                "bytes": result.content_length, "is_pdf": is_pdf}
    return {"ok": False, "reason": "Charlotte returned no downloaded file",
            "result_url": result.result_url}


def _detect_platform(result: TrialResult) -> str:
    """Pick the resolver from the pointer Charlotte actually returned — the real
    landscape isn't known ahead of time, so don't trust the per-trial hint."""
    url = result.result_url or ""
    host = (urlsplit(url).hostname or "").lower()
    if "calendar.google.com" in host or "cid=" in url or "src=" in url:
        return "gcal"
    if host.endswith("drive.google.com") or host.endswith("docs.google.com"):
        return "gdrive"
    if result.suggested_filename or url.lower().rsplit(".", 1)[-1] in {"pdf", "doc", "docx"}:
        return "pdf"
    return "gcal"  # an in-scope page pointer most likely hosts a calendar embed


async def resolve(trial: dict, result: TrialResult, out_dir: Path) -> dict:
    if not result.found or not result.result_url:
        return {"ok": False, "reason": "Charlotte found no pointer to resolve"}
    kind = _detect_platform(result)
    if kind == "pdf":
        return _resolve_pdf(result, out_dir)
    if kind == "gdrive":
        return await _resolve_gdrive(result.result_url, out_dir)
    return await _resolve_gcal(result.result_url, out_dir)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_trial(trial: dict, run_dir: Path, adapter: AdapterProtocol) -> TrialResult:
    result = TrialResult(name=trial["name"], slug=trial["slug"], url=trial["url"])
    trial_dir = run_dir / trial["slug"]
    trial_dir.mkdir(parents=True, exist_ok=True)
    run_start = monotonic()

    js_flag = "  [render_js]" if trial.get("render_js") else ""
    print(f"\n{'─' * 70}", flush=True)
    print(f"  {trial['name']}{js_flag}", flush=True)
    print(f"  goal: {trial['goal']}", flush=True)
    print(f"{'─' * 70}", flush=True)

    try:
        render_js = trial.get("render_js", False)
        gen = crawl(
            trial["url"],
            trial["goal"],
            navigation_hint=trial.get("navigation_hint"),
            model=adapter,
            max_pages=10,
            max_depth=4,
            confidence_threshold=CONFIDENCE_THRESHOLD,
            stream=True,
            default_delay=1.5,
            result_to_file=trial_dir,
            max_result_bytes=50 * 1024 * 1024,
            max_response_bytes=50 * 1024 * 1024,
            render_js=render_js,
            render_timeout=trial.get("render_timeout", 20.0),
            chromium_executable=CHROMIUM_EXECUTABLE if render_js else None,
            allowed_domains=trial.get("allowed_domains"),  # None → start-domain only
            follow_linked_resources=True,                   # autonomous: off-domain docs the site links to
        )

        async for event in gen:
            elapsed_ms = int((monotonic() - run_start) * 1000)
            t = f"[{elapsed_ms // 1000:>3}s]"

            if isinstance(event, CrawlStarted):
                result.events.append({"type": "CrawlStarted", "elapsed_ms": elapsed_ms})
            elif isinstance(event, GoalPreprocessed):
                ctx = event.goal_context
                gt = ctx.goal_type if ctx else None
                result.goal_type = gt
                print(f"  {t} goal_type={gt}  anchors={ctx.anchor_terms if ctx else []}", flush=True)
                result.events.append({"type": "GoalPreprocessed", "elapsed_ms": elapsed_ms,
                                      "goal_type": gt,
                                      "anchor_terms": ctx.anchor_terms if ctx else []})
            elif isinstance(event, PageFetched):
                print(f"  {t} fetched  {event.url}", flush=True)
                result.events.append({"type": "PageFetched", "elapsed_ms": elapsed_ms,
                                      "url": event.url, "http_status": event.http_status})
            elif isinstance(event, LinksRanked):
                top = [{"url": lk.url, "text": lk.text, "score": round(lk.score, 4)}
                       for lk in event.top_links[:5]]
                result.events.append({"type": "LinksRanked", "elapsed_ms": elapsed_ms,
                                      "page_url": event.page_url, "top_links": top})
            elif isinstance(event, ModelEvaluating):
                print(f"  {t} thinking...", flush=True)
            elif isinstance(event, ModelDecision):
                icon = "✓" if event.found else "·"
                print(f"  {t} {icon} conf={event.confidence:.2f}  {event.url}", flush=True)
                result.events.append({"type": "ModelDecision", "elapsed_ms": elapsed_ms,
                                      "url": event.url, "found": event.found,
                                      "confidence": event.confidence,
                                      "links_queued": event.links_queued,
                                      "reasoning": event.reasoning})
            elif isinstance(event, DestinationVerificationFailed):
                print(f"  {t} verifier ✗  {event.url}  ({event.result.reason})", flush=True)
                result.events.append({"type": "DestinationVerificationFailed",
                                      "elapsed_ms": elapsed_ms, "url": event.url,
                                      "reason": event.result.reason})
            elif isinstance(event, ResultFound):
                meta = event.content_metadata
                result.result_url = event.url
                if meta and meta.content_length:
                    result.content_length = meta.content_length
                    result.suggested_filename = meta.suggested_filename
                    print(f"  {t} FOUND  {event.url}", flush=True)
                    print(f"         {meta.content_type}  {meta.content_length // 1024} KB"
                          f"  →  {meta.suggested_filename}", flush=True)
                else:
                    print(f"  {t} FOUND (pointer)  {event.url}", flush=True)
                result.events.append({"type": "ResultFound", "elapsed_ms": elapsed_ms,
                                      "url": event.url,
                                      "suggested_filename": meta.suggested_filename if meta else None,
                                      "content_length": meta.content_length if meta else None})
            elif isinstance(event, PageSkipped):
                result.events.append({"type": "PageSkipped", "elapsed_ms": elapsed_ms,
                                      "url": event.url, "reason": event.reason,
                                      "error_type": event.error_type})
            elif isinstance(event, BudgetExhausted):
                print(f"  {t} budget exhausted after {event.pages_visited} pages", flush=True)
            elif isinstance(event, CrawlComplete):
                result.found = event.found
                result.pages_visited = event.pages_visited
                result.navigate_ms = event.elapsed_ms
                result.model_calls = model_metrics.snapshot()
                result.model_calls_total = model_metrics.total()
                result.events.append({"type": "CrawlComplete", "elapsed_ms": elapsed_ms,
                                      "found": event.found, "pages_visited": event.pages_visited,
                                      "model_calls": result.model_calls,
                                      "model_calls_total": result.model_calls_total,
                                      "failure_mode": (event.failure_mode.value
                                                       if event.failure_mode else None)})

    except Exception as exc:  # noqa: BLE001 — exploratory harness
        result.error = f"{type(exc).__name__}: {exc}"
        # Record the navigation time spent before the failure; elapsed_ms is
        # recomputed below as navigate_ms + resolve_ms, so set navigate_ms (not
        # elapsed_ms) or the total would collapse to just the resolver time.
        result.navigate_ms = int((monotonic() - run_start) * 1000)
        print(f"  ERROR: {result.error}", flush=True)

    # ---- downstream hand-off: resolve Charlotte's pointer to the artifact ----
    resolve_t0 = monotonic()
    try:
        result.resolution = await resolve(trial, result, trial_dir)
    except Exception as exc:  # noqa: BLE001
        result.resolution = {"ok": False, "reason": f"resolver crashed: {type(exc).__name__}: {exc}"}
    result.resolve_ms = int((monotonic() - resolve_t0) * 1000)
    result.elapsed_ms = result.navigate_ms + result.resolve_ms

    # event log
    try:
        with (trial_dir / "events.jsonl").open("w", encoding="utf-8") as fh:
            for ev in result.events:
                fh.write(json.dumps(ev) + "\n")
    except OSError as exc:
        print(f"  WARNING: could not write events log: {exc}", flush=True)

    nav = "FOUND" if result.found else "NOT FOUND"
    res_ok = "✓" if result.resolution.get("ok") else "✗"
    mc = " ".join(f"{k}={v}" for k, v in sorted(result.model_calls.items())) or "—"
    print(f"  → navigate: {nav}  goal_type={result.goal_type}  "
          f"{result.pages_visited} pages  {result.navigate_ms / 1000:.1f}s  "
          f"model_calls={result.model_calls_total} ({mc})", flush=True)
    print(f"  → resolve : {res_ok}  {result.resolve_ms / 1000:.1f}s  {result.resolution}", flush=True)
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _sum_calls(results: list[TrialResult]) -> dict:
    """Aggregate per-trial model-call tallies into a run-wide breakdown by reason."""
    totals: dict[str, int] = {}
    for r in results:
        for reason, n in r.model_calls.items():
            totals[reason] = totals.get(reason, 0) + n
    return totals


async def main() -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = OUT_DIR / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)
    adapter, adapter_label = build_adapter()

    print(f"\nCharlotte {charlotte.__version__}  —  school calendar retrieval")
    print(f"run dir : {run_dir}")
    print(f"model   : {adapter_label}")

    results: list[TrialResult] = []
    for i, trial in enumerate(TRIALS):
        if i > 0:
            await asyncio.sleep(INTER_TRIAL_DELAY)
        results.append(await run_trial(trial, run_dir, adapter))

    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "charlotte_version": charlotte.__version__,
        "model": adapter_label,
        "navigated": sum(1 for r in results if r.found),
        "resolved": sum(1 for r in results if r.resolution.get("ok")),
        "total": len(results),
        "model_calls_total": sum(r.model_calls_total for r in results),
        "model_calls_by_reason": _sum_calls(results),
        "trials": [
            {
                "name": r.name, "url": r.url,
                "goal": next(t["goal"] for t in TRIALS if t["slug"] == r.slug),
                "goal_type": r.goal_type,
                "navigated": r.found, "result_url": r.result_url,
                "navigate_ms": r.navigate_ms, "resolve_ms": r.resolve_ms,
                "model_calls": r.model_calls, "model_calls_total": r.model_calls_total,
                "resolution": r.resolution, "error": r.error,
            }
            for r in results
        ],
    }
    try:
        (run_dir / "_summary.json").write_text(json.dumps(summary, indent=2))
    except OSError as exc:
        print(f"WARNING: could not write summary: {exc}", flush=True)

    mc_breakdown = " ".join(f"{k}={v}" for k, v in sorted(summary["model_calls_by_reason"].items())) or "—"
    print(f"\n{'═' * 70}")
    print(f"  navigated {summary['navigated']}/{summary['total']}   "
          f"resolved {summary['resolved']}/{summary['total']}   "
          f"{summary['model_calls_total']} model calls ({mc_breakdown})   run: {run_dir}")
    print(f"{'═' * 70}")
    for r in results:
        nav = "✓" if r.found else "✗"
        res = "✓" if r.resolution.get("ok") else "✗"
        detail = r.resolution.get("note") or r.resolution.get("reason") or ""
        print(f"  nav {nav}  resolve {res}  {(r.goal_type or '?'):<13} "
              f"{r.model_calls_total:>2} calls  {r.name:<32}  {detail[:38]}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
