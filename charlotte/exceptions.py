"""
Charlotte exception hierarchy.

All Charlotte exceptions inherit from CharlotteError. Raw third-party exceptions
(httpx, playwright, groq) are caught at component boundaries and re-raised as the
appropriate subclass here. They never reach the caller.

Exceptions that propagate to callers: CharlotteConfigError, CharlotteInternalError.
All others are caught internally, logged, and result in a skipped page or found=False.
See spec §18.
"""


class CharlotteError(Exception):
    """Base for all Charlotte exceptions."""


class CharlotteConfigError(CharlotteError):
    """Invalid configuration detected before the crawl begins.

    Raised immediately — no pages are fetched. The caller must correct the
    configuration before retrying. Examples: Playwright not installed when
    render_js=True; start_url that cannot be parsed as a URL.
    """


class CharlotteNetworkError(CharlotteError):
    """Network-level failure while fetching a page.

    Handled internally: the affected page is skipped and the crawl continues.
    Wraps underlying httpx network errors at the fetcher boundary.
    """


class CharlotteTimeoutError(CharlotteError):
    """A timeout threshold was exceeded.

    Covers all four timeout types: connect_timeout, read_timeout,
    render_timeout (Playwright), and model_timeout. Handled internally:
    the affected page or model call is skipped and the crawl continues.
    A model timeout triggers one retry before the page is skipped.
    """


class CharlotteRedirectError(CharlotteError):
    """A redirect could not be followed safely.

    Raised internally for three conditions:
    - Redirect chain exceeded 5 hops
    - Redirect destination is outside allowed_domains
    - Redirect loop detected (A → B → A)
    Handled internally: the page is skipped and the crawl continues.
    """


class RobotsError(CharlotteError):
    """robots.txt disallowed the crawl, or was unreachable or malformed.

    Not a failure in the strict sense — this is a policy result. Handled
    internally by returning found=False with a plain-language explanation.
    The sole exception: a 404 for robots.txt is treated as no restrictions.
    """


class AdapterOutputError(CharlotteError):
    """Model output failed schema validation after two attempts.

    Handled internally: the page is treated as unevaluable and the crawl
    continues. The page is not added to the visited set — it may be reached
    again via a different path.
    """


class CharlotteSSRFError(CharlotteConfigError):
    """URL targets a private, loopback, link-local, or reserved address.

    Raised immediately before any network request is made. The caller must
    supply a publicly routable URL. See spec §8.
    """


class CharlotteResponseTooLargeError(CharlotteNetworkError):
    """Response body exceeded the configured max_response_bytes limit.

    Handled internally: the affected page is skipped and the crawl continues.
    """


class CharlotteInternalError(CharlotteError):
    """Unexpected internal state that Charlotte could not recover from.

    Should not occur in normal use. Always includes a message asking the caller
    to file a bug report at https://github.com/Boss-Button-Studios/charlotte/issues
    """
