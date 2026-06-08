"""
Destination verifier — spec §7.

Verifies that a candidate URL is accessible, not behind a login wall, and
(optionally) relevant to the goal before Charlotte records it as a result.

Modes (set at construction time):
  off        — skip all checks; always passes. No score.
  existence  — HTTP 2xx + no login wall + non-empty body.
  relevance  — existence + BM25 score ≥ verify_threshold (default 0.3).
  full       — existence + strongest available signal:
               embeddings when charlotte-crawler[embeddings] is installed;
               BM25 with threshold raised by 0.15 otherwise (§7.6).

Result content delivery (§7.7): the verification fetch is reused — no second
round trip. Bytes are buffered in memory (up to max_result_bytes) or written
to disk (result_to_file). document_link goals capture bytes by default; all
other goal types require fetch_result_content=True.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from charlotte.config import CharlotteConfig
from charlotte.core.normalizer import validate_url_safety
from charlotte.core.text_normalization import tokenize
from charlotte.exceptions import CharlotteNetworkError, CharlotteResponseTooLargeError, CharlotteTimeoutError
from charlotte.models import ResultContent, VerificationResult

if TYPE_CHECKING:
    from charlotte.models import GoalContext

# ---------------------------------------------------------------------------
# Login wall heuristics — spec §7.3
# ---------------------------------------------------------------------------

# URL path fragments that signal a login / auth redirect.
_AUTH_PATH_SEGMENTS: frozenset[str] = frozenset({
    "login", "signin", "sign-in", "sign_in",
    "auth", "authenticate", "authentication",
    "session/new", "users/sign_in", "account/login",
})

# Matches <input type="password"> regardless of attribute order or quoting.
_PASSWORD_FIELD_RE = re.compile(
    r"<input\b[^>]*\btype\s*=\s*[\"']?password[\"']?[^>]*>",
    re.IGNORECASE,
)

# Matches Content-Disposition filename, including filename* (RFC 5987).
_CD_FILENAME_RE = re.compile(
    r"filename\*?\s*=\s*(?:UTF-8'')?[\"']?([^\"';\r\n]+)[\"']?",
    re.IGNORECASE,
)


def _is_login_wall_redirect(history: list[httpx.Response]) -> bool:
    """True if any redirect destination path looks like a login page."""
    for resp in history:
        path_parts = urlsplit(str(resp.url)).path.strip("/").lower().split("/")
        for i in range(len(path_parts)):
            segment = "/".join(path_parts[i:])
            if segment in _AUTH_PATH_SEGMENTS:
                return True
    return False


def _has_password_form(html: str) -> bool:
    """True if the HTML contains a password input field."""
    return bool(_PASSWORD_FIELD_RE.search(html))


# ---------------------------------------------------------------------------
# Relevance scoring helpers
# ---------------------------------------------------------------------------

_MIN_CHUNK_CHARS: int = 30    # ignore blank or stub paragraphs
_EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"


def _extract_filename(content_disposition: str, url: str) -> str | None:
    """Return suggested filename from Content-Disposition header, or the URL path."""
    if content_disposition:
        m = _CD_FILENAME_RE.search(content_disposition)
        if m:
            return m.group(1).strip().rstrip(";")
    path = urlsplit(url).path
    if path and path != "/":
        name = path.rsplit("/", 1)[-1]
        if name and "." in name:
            return name
    return None


def _page_text(html: str) -> str:
    """Strip hidden content and return plain text for BM25 scoring.

    Applies the sanitizer to remove injection vectors before extracting text,
    so hidden anchor terms cannot artificially inflate relevance scores.
    """
    from charlotte.core.sanitizer import strip_hidden

    sanitized = strip_hidden(html)
    return BeautifulSoup(sanitized, "html.parser").get_text(separator=" ", strip=True)


def _bm25_score(text: str, anchor_terms: list[str], synonym_values: list[str]) -> float:
    """BM25 max-paragraph score for the page text against the goal query.

    Splits text into paragraph-sized chunks, scores each against
    anchor_terms + synonym_values, and returns the highest score.
    A score of 0.0 means no query terms appeared in any chunk.

    The ATIRE BM25 variant used by rank_bm25 computes:
        idf = log(N - df + 0.5) - log(df + 0.5)
    This is 0 when df = N/2 (e.g. one content chunk + one background doc with
    the term in the content chunk: N=2, df=1 → idf=0). We prevent this by
    adding len(chunks)+1 sentinel documents that contain no query terms,
    guaranteeing df < N/2 even when every content chunk matches.
    """
    from rank_bm25 import BM25Okapi

    query = anchor_terms + synonym_values
    if not query:
        return 0.0

    chunks = [p.strip() for p in re.split(r"\n+", text) if len(p.strip()) >= _MIN_CHUNK_CHARS]
    if not chunks:
        chunks = [text.strip()] if text.strip() else []
    if not chunks:
        return 0.0

    # Sentinel docs guarantee idf > 0 for any query term found in ≥1 content chunk.
    sentinel = ["__sentinel__"]
    n_sentinels = len(chunks) + 1
    corpus = [tokenize(chunk) or ["__empty__"] for chunk in chunks] + [sentinel] * n_sentinels

    query_tokens = [tok for term in query for tok in tokenize(term)]
    if not query_tokens:
        return 0.0

    scores = BM25Okapi(corpus).get_scores(query_tokens)
    page_scores = scores[: len(chunks)]   # sentinel doc scores excluded
    return float(max(page_scores)) if any(s > 0 for s in page_scores) else 0.0


def _embedding_score(
    text: str,
    anchor_terms: list[str],
    synonym_values: list[str],
    model: Any,
) -> float:
    """Cosine similarity score using a sentence-transformers model.

    Truncates text to 2048 chars so encoding stays fast on small hardware.
    """
    from sklearn.metrics.pairwise import cosine_similarity

    query = " ".join(anchor_terms + synonym_values).strip()
    if not query or not text.strip():
        return 0.0

    q_emb = model.encode([query])
    t_emb = model.encode([text[:2048]])
    return float(cosine_similarity(q_emb, t_emb)[0][0])


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class DestinationVerifierProtocol(Protocol):
    """Verify a candidate URL before Charlotte records it as a result.

    Configuration (mode, thresholds, content delivery options) is supplied
    at construction time. Call with the URL and goal_context for each candidate.

    Returns a ``(VerificationResult, ResultContent | None)`` pair.
    ``ResultContent`` is populated when content delivery was requested and the
    candidate passed. See spec §7, §7.7.
    """

    async def __call__(
        self,
        *,
        url: str,
        goal_context: "GoalContext",
    ) -> tuple[VerificationResult, ResultContent | None]: ...


# ---------------------------------------------------------------------------
# DefaultDestinationVerifier
# ---------------------------------------------------------------------------

_DEFAULT_MAX_RESULT_BYTES: int = 10_485_760   # 10 MB — matches v1.4 fetcher cap
_MAX_REDIRECTS: int = 5


class DefaultDestinationVerifier:
    """Four-mode destination verifier. See spec §7.3.

    Args:
        mode:                 Verification depth. Default ``"relevance"``.
        verify_threshold:     BM25 (or embedding) score below which a page fails.
                              Default 0.3. Raised by 0.15 for ``"full"`` without
                              embeddings (§7.6 partial-circularity compensation).
        fetch_result_content: Whether to capture response bytes. ``None`` (default)
                              means capture for ``document_link`` goals only.
        max_result_bytes:     Maximum bytes buffered or written per result.
                              Default 10 MB.
        result_to_file:       Directory to write result bytes to when set.
                              ``ResultContent.content`` will be ``None``.
        connect_timeout:      TCP connection timeout in seconds.
        read_timeout:         Response body read timeout in seconds.
        user_agent:           HTTP User-Agent header value.
    """

    def __init__(
        self,
        *,
        mode: Literal["off", "existence", "relevance", "full"] = "relevance",
        verify_threshold: float = 0.3,
        fetch_result_content: bool | None = None,
        max_result_bytes: int = _DEFAULT_MAX_RESULT_BYTES,
        result_to_file: Path | None = None,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        user_agent: str = "",
    ) -> None:
        self._mode = mode
        self._threshold = verify_threshold
        self._fetch_content = fetch_result_content
        self._max_bytes = max_result_bytes
        self._result_to_file = result_to_file
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._user_agent = user_agent or CharlotteConfig.user_agent()
        self._sentence_model: Any = None   # lazy-loaded for "full" mode with embeddings

    async def __call__(
        self,
        *,
        url: str,
        goal_context: "GoalContext",
    ) -> tuple[VerificationResult, ResultContent | None]:
        if self._mode == "off":
            return (
                VerificationResult(url=url, passed=True, mode="off", score=None, reason="verification_disabled"),
                None,
            )

        capture = self._should_capture(goal_context)

        # URL safety guard — defense in depth; engine should have validated already.
        try:
            validate_url_safety(url)
        except Exception as exc:
            return (
                VerificationResult(url=url, passed=False, mode=self._mode, score=None, reason=f"unsafe_url: {exc}"),
                None,
            )

        try:
            fetch = await self._fetch(url)
        except CharlotteResponseTooLargeError:
            return (
                VerificationResult(url=url, passed=False, mode=self._mode, score=None, reason="response_too_large"),
                None,
            )
        except (CharlotteNetworkError, CharlotteTimeoutError) as exc:
            return (
                VerificationResult(url=url, passed=False, mode=self._mode, score=None, reason=f"fetch_failed: {type(exc).__name__}"),
                None,
            )

        status, redirect_history, content_type, etag, suggested_filename, html, body = fetch

        # --- Existence checks (all non-off modes) ---
        if not (200 <= status < 300):
            return (
                VerificationResult(url=url, passed=False, mode=self._mode, score=None, reason=f"http_{status}"),
                None,
            )
        if _is_login_wall_redirect(redirect_history):
            return (
                VerificationResult(url=url, passed=False, mode=self._mode, score=None, reason="login_wall_redirect"),
                None,
            )
        if html and _has_password_form(html):
            return (
                VerificationResult(url=url, passed=False, mode=self._mode, score=None, reason="login_wall_form"),
                None,
            )
        if not body:
            return (
                VerificationResult(url=url, passed=False, mode=self._mode, score=None, reason="empty_response"),
                None,
            )

        if self._mode == "existence":
            result = VerificationResult(url=url, passed=True, mode="existence", score=None, reason="ok_existence")
            content = self._build_content(body, content_type, etag, suggested_filename) if capture else None
            return result, content

        # --- Relevance / full scoring ---
        text = _page_text(html) if html else ""
        synonym_values: list[str] = [v for vs in goal_context.synonyms.values() for v in vs]
        threshold = self._threshold

        if self._mode == "full":
            score, used_embeddings = self._full_mode_score(text, goal_context.anchor_terms, synonym_values)
            if not used_embeddings:
                threshold = min(1.0, threshold + 0.15)
        else:
            score = _bm25_score(text, goal_context.anchor_terms, synonym_values)

        passed = score >= threshold
        reason = "ok_relevance" if passed else f"score_below_threshold:{score:.3f}"
        result = VerificationResult(url=url, passed=passed, mode=self._mode, score=score, reason=reason)
        content = self._build_content(body, content_type, etag, suggested_filename) if (capture and passed) else None
        return result, content

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _should_capture(self, goal_context: "GoalContext") -> bool:
        if self._fetch_content is None:
            return goal_context.goal_type == "document_link"
        return self._fetch_content

    def _full_mode_score(
        self,
        text: str,
        anchor_terms: list[str],
        synonym_values: list[str],
    ) -> tuple[float, bool]:
        """Return (score, used_embeddings). Falls back to BM25 if extras absent."""
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import]

            if self._sentence_model is None:
                self._sentence_model = SentenceTransformer(_EMBEDDING_MODEL)
            score = _embedding_score(text, anchor_terms, synonym_values, self._sentence_model)
            return score, True
        except ImportError:
            return _bm25_score(text, anchor_terms, synonym_values), False

    async def _fetch(
        self,
        url: str,
    ) -> tuple[int, list[httpx.Response], str | None, str | None, str | None, str, bytes]:
        """Fetch url and return components needed for verification and content delivery.

        Returns:
            (status_code, redirect_history, content_type, etag, suggested_filename,
             html_str, body_bytes)

        Raises:
            CharlotteResponseTooLargeError: body exceeded max_result_bytes.
            CharlotteNetworkError: network-level failures.
            CharlotteTimeoutError: connect or read timeout.
        """
        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=10.0,
            pool=10.0,
        )
        headers = {"User-Agent": self._user_agent}
        chunks: list[bytes] = []
        total = 0

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=_MAX_REDIRECTS,
                timeout=timeout,
                headers=headers,
            ) as client:
                async with client.stream("GET", url) as resp:
                    async for chunk in resp.aiter_bytes(chunk_size=65_536):
                        total += len(chunk)
                        if total > self._max_bytes:
                            raise CharlotteResponseTooLargeError(
                                f"Response from {url} exceeded {self._max_bytes} bytes"
                            )
                        chunks.append(chunk)

                    body = b"".join(chunks)
                    status = resp.status_code
                    redirect_history = list(resp.history)
                    content_type = resp.headers.get("content-type")
                    etag = resp.headers.get("etag")
                    cd = resp.headers.get("content-disposition", "")
                    suggested_filename = self._extract_filename(cd, url)

        except CharlotteResponseTooLargeError:
            raise
        except httpx.TooManyRedirects as exc:
            raise CharlotteNetworkError(f"Too many redirects fetching {url}") from exc
        except httpx.TimeoutException as exc:
            raise CharlotteTimeoutError(f"Timeout fetching {url}") from exc
        except httpx.RequestError as exc:
            raise CharlotteNetworkError(f"Network error fetching {url}") from exc

        # Decode as text only for HTML responses; binary files stay bytes-only.
        html_str = ""
        if content_type and "html" in content_type:
            try:
                html_str = body.decode(resp.encoding or "utf-8", errors="replace")
            except Exception:
                html_str = body.decode("utf-8", errors="replace")

        return status, redirect_history, content_type, etag, suggested_filename, html_str, body

    @staticmethod
    def _extract_filename(content_disposition: str, url: str) -> str | None:
        return _extract_filename(content_disposition, url)

    def _build_content(
        self,
        body: bytes,
        content_type: str | None,
        etag: str | None,
        suggested_filename: str | None,
    ) -> ResultContent:
        """Wrap buffered bytes in a ResultContent, optionally writing to disk."""
        file_path: Path | None = None
        content: bytes | None = body

        if self._result_to_file is not None:
            # Sanitize to basename — Content-Disposition and URL paths are
            # attacker-controlled; strip parent components to prevent traversal.
            raw_name = Path(suggested_filename or "result").name
            filename = raw_name if raw_name and not raw_name.startswith(".") else "result"
            file_path = self._result_to_file / filename
            file_path.write_bytes(body)
            content = None

        return ResultContent(
            content=content,
            content_type=content_type,
            content_length=len(body),
            suggested_filename=suggested_filename,
            etag=etag,
            fetched_at=datetime.now(timezone.utc),
            file_path=file_path,
        )
