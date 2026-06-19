"""Browser-based fetching (Playwright) mixed into PageFetcher.

Provides the render_js paths — HTML render via page.goto and binary-document
fetch via APIRequestContext — plus the shared-browser lifecycle. Relies on
PageFetcher for _is_allowed/_hostname and the _* config attributes set in
PageFetcher.__init__."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from charlotte.core.fetch_util import (
    FetchResult,
    _MAX_REDIRECTS,
    _is_bot_challenge,
)
from charlotte.core.normalizer import normalize_url, validate_url_safety
from charlotte.exceptions import (
    CharlotteChallengeError,
    CharlotteConfigError,
    CharlotteNetworkError,
    CharlotteRedirectError,
    CharlotteResponseTooLargeError,
    CharlotteSSRFError,
    CharlotteTimeoutError,
    RobotsError,
)

if TYPE_CHECKING:
    from charlotte.core.fetcher import PageFetcher  # __aenter__ return type
    from charlotte.core.robots import RobotsHandler


class _PlaywrightFetchMixin:
    """Playwright render_js fetch paths and shared-browser lifecycle for PageFetcher."""

    async def _fetch_document_with_playwright(
        self, url: str, *, visited_urls: set[str],
        robots_handler: "RobotsHandler | None" = None,
        default_delay: float = 0.0,
    ) -> FetchResult:
        """Fetch a binary document using Playwright's APIRequestContext.

        When render_js=True, document URLs (PDFs, etc.) cannot be navigated to
        via page.goto() — Chromium either renders them inline or triggers a
        download. APIRequestContext issues the HTTP request through Chromium's
        network stack with the configured (identified) User-Agent. The caller
        receives status_code and raw_bytes; html is always empty for binary content.

        Note: this is a *fresh* request context — it does not carry cookies or a
        cleared challenge from any prior page render, so it does not defeat an
        active anti-bot challenge (e.g. Cloudflare). That is deliberate: Charlotte
        honours such refusals (CharlotteChallengeError) rather than circumventing
        them. Sites that merely require a real JS runtime (Wix SPAs, etc.) still
        work because the document itself is a plain file request.
        """
        validate_url_safety(url)
        start = time.monotonic()

        async def _do_request(browser) -> tuple[int, str, bytes]:
            context = await browser.new_context(
                extra_http_headers={"User-Agent": self._user_agent}
            )
            try:
                # Follow redirects manually (max_redirects=0) so each hop is SSRF-
                # and domain-checked BEFORE it is fetched — mirroring the httpx
                # path. Otherwise Playwright follows redirects internally, past
                # these gates, and a public document that redirects to a private or
                # off-scope address would be fetched (the follow_linked_resources
                # relaxation makes _is_allowed permit off-domain documents, so the
                # domain check alone can no longer catch a redirect to a private IP).
                current = url
                for _hop in range(_MAX_REDIRECTS + 1):
                    validate_url_safety(current)
                    if not self._is_allowed(current):
                        raise CharlotteRedirectError(
                            f"Redirect from {url!r} to {current!r} crosses into "
                            f"disallowed domain {self._hostname(current)!r}"
                        )
                    resp = await context.request.get(
                        current,
                        max_redirects=0,
                        timeout=(self._connect_timeout + self._render_timeout) * 1000,
                    )
                    if 300 <= resp.status < 400:
                        location = resp.headers.get("location", "")
                        if location:
                            destination = urljoin(current, location)
                            # Cross-host robots check — permissions don't inherit
                            # across domains (spec §11.1); mirrors the httpx path.
                            if (
                                robots_handler is not None
                                and self._hostname(destination) != self._hostname(current)
                            ):
                                await robots_handler.check(destination, default_delay)
                            current = destination
                            continue
                    body = await resp.body()
                    return resp.status, resp.url, body
                raise CharlotteRedirectError(
                    f"Redirect chain for {url!r} exceeded {_MAX_REDIRECTS} hops"
                )
            finally:
                await context.close()

        try:
            if self._browser is not None:
                status, final_url, body = await _do_request(self._browser)
            else:
                async with self._playwright_factory() as pw:
                    launch_kwargs: dict = {"headless": True}
                    if self._chromium_executable:
                        launch_kwargs["executable_path"] = self._chromium_executable
                    browser = await pw.chromium.launch(**launch_kwargs)
                    try:
                        status, final_url, body = await _do_request(browser)
                    finally:
                        await browser.close()
        except self._playwright_timeout_error as exc:
            raise CharlotteTimeoutError(
                f"Playwright document fetch of {url!r} timed out"
            ) from exc
        except (
            CharlotteTimeoutError,
            CharlotteNetworkError,
            CharlotteRedirectError,
            CharlotteResponseTooLargeError,
            CharlotteChallengeError,
            CharlotteSSRFError,
            RobotsError,
        ):
            raise
        except Exception as exc:
            raise CharlotteNetworkError(
                f"Playwright document fetch error for {url!r}: {type(exc).__name__}: {exc}"
            ) from exc

        # A challenge interstitial comes back as a small HTML body in place of the
        # document. Detect it before the size/domain checks so the refusal is
        # reported honestly rather than as a generic failure.
        if _is_bot_challenge(status, body[:4096].decode("utf-8", errors="replace")):
            raise CharlotteChallengeError(
                f"{url!r} is behind an anti-bot challenge — site declines automated access"
            )

        if len(body) > self._max_response_bytes:
            raise CharlotteResponseTooLargeError(
                f"Downloaded document from {url!r} exceeded "
                f"{self._max_response_bytes // (1024 * 1024)} MB limit"
            )
        if not self._is_allowed(final_url):
            raise CharlotteRedirectError(
                f"Redirect from {url!r} to {final_url!r} crosses into "
                f"disallowed domain {self._hostname(final_url)!r}"
            )
        try:
            norm_final = normalize_url(final_url)
        except CharlotteConfigError as exc:
            raise CharlotteRedirectError(
                f"Redirect destination {final_url!r} is not a valid URL: {exc}"
            ) from exc
        if norm_final in visited_urls:
            raise CharlotteRedirectError(
                f"Redirect loop detected: {final_url!r} already visited"
            )

        return FetchResult(
            url=final_url,
            html="",
            status_code=status,
            fetch_ms=int((time.monotonic() - start) * 1000),
            raw_bytes=body if (200 <= status < 300) else None,
        )

    async def __aenter__(self) -> "PageFetcher":
        """Launch a shared browser when render_js=True; no-op otherwise."""
        if self._render_js:
            self._pw_cm = self._playwright_factory()
            try:
                pw = await self._pw_cm.__aenter__()
            except Exception as exc:
                self._pw_cm = None
                raise CharlotteConfigError(
                    "Playwright failed to initialise — the installed version may be "
                    "incompatible with the browser binary. "
                    "Try running with the project virtual environment: "
                    f".venv/bin/python <script>. Underlying error: {exc}"
                ) from exc
            try:
                launch_kwargs: dict = {"headless": True}
                if self._chromium_executable:
                    launch_kwargs["executable_path"] = self._chromium_executable
                self._browser = await pw.chromium.launch(**launch_kwargs)
            except Exception as exc:
                await self._pw_cm.__aexit__(type(exc), exc, exc.__traceback__)
                self._pw_cm = None
                raise CharlotteConfigError(
                    f"Playwright browser launch failed: {exc}. "
                    "If using chromium_executable, verify the path points to the "
                    "real binary, not a snap stub (e.g. use "
                    "/snap/chromium/current/usr/lib/chromium-browser/chrome)."
                ) from exc
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Close the shared browser and Playwright context."""
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._pw_cm is not None:
            await self._pw_cm.__aexit__(exc_type, exc_val, exc_tb)
            self._pw_cm = None

    async def _render_page(self, browser, url: str) -> tuple:
        """Open a fresh page in browser, navigate to url, and return (html, final_url, status).

        Sets Charlotte's User-Agent so server-side logs see the same identifier
        as the httpx path. Prefers networkidle so SPA content is fully rendered;
        falls back to whatever has rendered when networkidle never settles (e.g.
        sites with persistent analytics or social-media background requests).
        """
        page = await browser.new_page()
        try:
            await page.set_extra_http_headers({"User-Agent": self._user_agent})
            response = None
            try:
                response = await page.goto(
                    url,
                    wait_until="networkidle",
                    timeout=self._render_timeout * 1000,
                )
            except self._playwright_timeout_error:
                # networkidle never settled — capture whatever has rendered.
                pass
            html = await page.content()
            if len(html.encode()) > self._max_response_bytes:
                raise CharlotteResponseTooLargeError(
                    f"Rendered page from {url!r} exceeded "
                    f"{self._max_response_bytes // (1024 * 1024)} MB limit"
                )
            status_code = response.status if response is not None else 200
            # If the headless browser was served a challenge instead of the page
            # (it could not transparently clear it), honour the refusal.
            if _is_bot_challenge(status_code, html):
                raise CharlotteChallengeError(
                    f"{url!r} is behind an anti-bot challenge — site declines automated access"
                )
            return html, page.url, status_code
        finally:
            await page.close()

    async def _fetch_with_playwright(
        self, url: str, *, visited_urls: set[str]
    ) -> FetchResult:
        """Fetch a JS-rendered page using headless Chromium via Playwright.

        Uses the shared browser from __aenter__ when available (fast path).
        Falls back to launching a per-call browser otherwise (backwards-compatible
        but ~2 s slower per page due to Chromium startup cost).
        Waits for networkidle so SPA content is fully rendered before capture.
        """
        # SSRF check before navigation — parity with the httpx and document paths,
        # which validate before any network activity. page.goto() follows redirects
        # internally, so we cannot vet each hop here; final_url is re-checked below.
        validate_url_safety(url)
        start = time.monotonic()
        try:
            if self._browser is not None:
                html, final_url, status_code = await self._render_page(self._browser, url)
            else:
                async with self._playwright_factory() as pw:
                    launch_kwargs: dict = {"headless": True}
                    if self._chromium_executable:
                        launch_kwargs["executable_path"] = self._chromium_executable
                    browser = await pw.chromium.launch(**launch_kwargs)
                    try:
                        html, final_url, status_code = await self._render_page(browser, url)
                    finally:
                        await browser.close()
        except self._playwright_timeout_error as exc:
            raise CharlotteTimeoutError(f"Render timeout fetching {url!r}") from exc
        except (
            CharlotteTimeoutError,
            CharlotteNetworkError,
            CharlotteRedirectError,
            CharlotteResponseTooLargeError,
            CharlotteChallengeError,
            CharlotteSSRFError,
            RobotsError,
        ):
            raise
        except Exception as exc:
            raise CharlotteNetworkError(
                f"Playwright error fetching {url!r}: {type(exc).__name__}: {exc}"
            ) from exc

        # Post-navigation SSRF re-check: page.goto() may have redirected to a
        # private address internally. Refuse to return its body even though the
        # navigation already happened — this blocks data exfiltration via redirect.
        validate_url_safety(final_url)
        # Post-navigation domain and loop checks — mirrors the httpx redirect policy.
        if not self._is_allowed(final_url):
            raise CharlotteRedirectError(
                f"Redirect from {url!r} to {final_url!r} crosses into "
                f"disallowed domain {self._hostname(final_url)!r}"
            )
        try:
            norm_final = normalize_url(final_url)
        except CharlotteConfigError as exc:
            raise CharlotteRedirectError(
                f"Redirect destination {final_url!r} is not a valid URL: {exc}"
            ) from exc
        if norm_final in visited_urls:
            raise CharlotteRedirectError(
                f"Redirect loop detected: {final_url!r} already visited"
            )

        return FetchResult(
            url=final_url,
            html=html,
            status_code=status_code,
            fetch_ms=int((time.monotonic() - start) * 1000),
            redirect_chain=[],  # Playwright follows redirects internally
        )

