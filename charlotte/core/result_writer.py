"""
Safe, unique result-file writing for destination delivery (spec §7.7.4).

When ``result_to_file`` is set, a verified document is written to the caller's
directory. The filename is derived from the server's ``Content-Disposition`` or the
URL — both untrusted — so it is reduced to a safe basename (no path traversal, no
control characters), and the write **never overwrites** an existing file. Periodic
recrawls of dated documents therefore accumulate as distinct versions instead of one
silently clobbering another (the consolidation-service shape in §7.7.4), and a
malicious destination cannot choose a name that overwrites a sibling file the caller
keeps in that directory.

The verifier and the engine's render_js document path both deliver files; both go
through here so the safety logic lives in exactly one place.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from charlotte.exceptions import CharlotteConfigError

# Upper bound on the " (2)", " (3)", … disambiguation search. A directory already
# holding this many same-named results is almost certainly a misconfiguration; fail
# loudly rather than spin.
_MAX_DISAMBIGUATION: int = 10_000


def _sanitize_basename(name: str | None) -> str | None:
    """Reduce an untrusted name to a safe filesystem basename, or None if unusable.

    Strips directory components (traversal defence), removes control characters
    including NUL (which would otherwise crash ``open`` with a raw ``ValueError``),
    and rejects empty or dot-leading names so they fall through to the next source.
    """
    if not name:
        return None
    # Strip any directory components an attacker embedded (../, absolute paths, …).
    name = Path(name).name
    # Drop control characters (C0 range and DEL) — notably NUL, which open() rejects.
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f").strip()
    if not name or name.startswith("."):
        return None
    return name


def safe_result_basename(suggested_filename: str | None, url: str) -> str:
    """Pick a safe, non-empty basename for a result file.

    Precedence (spec §7.7.4): the ``Content-Disposition`` / caller-supplied suggestion,
    then the URL path basename, then the literal ``"result"``. Each candidate is
    sanitized; the first usable one wins.
    """
    url_basename = (urlsplit(url).path.rsplit("/", 1)[-1] or None) if url else None
    for candidate in (suggested_filename, url_basename):
        safe = _sanitize_basename(candidate)
        if safe:
            return safe
    return "result"


def write_result_file(
    directory: Path,
    body: bytes,
    suggested_filename: str | None,
    url: str,
) -> Path:
    """Write ``body`` to a guaranteed-unique file in ``directory`` and return the path.

    Never overwrites: the file is created with ``O_EXCL`` (exclusive create), and on a
    name collision the basename is disambiguated as ``name (2).ext``, ``name (3).ext``,
    … until a free name is found. This is race-safe — a concurrent writer that wins the
    name simply pushes us to the next candidate.

    Raises:
        CharlotteConfigError: the directory is missing/unwritable, or no unique name
            could be allocated. Never a raw ``OSError``/``ValueError`` — per the
            trust/exception model, ``result_to_file`` is caller config and its failures
            surface as a named Charlotte error.
    """
    base = safe_result_basename(suggested_filename, url)
    stem, suffix = Path(base).stem, Path(base).suffix

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CharlotteConfigError(
            f"Could not write result to {directory!r}: {exc}"
        ) from exc

    for attempt in range(_MAX_DISAMBIGUATION):
        candidate = base if attempt == 0 else f"{stem} ({attempt + 1}){suffix}"
        path = directory / candidate
        try:
            # "xb" → open for exclusive creation; fails if the path already exists.
            with open(path, "xb") as handle:
                handle.write(body)
            return path
        except FileExistsError:
            continue  # name taken — try the next disambiguated candidate
        except OSError as exc:
            raise CharlotteConfigError(
                f"Could not write result to {directory!r}: {exc}"
            ) from exc

    raise CharlotteConfigError(
        f"Could not allocate a unique result filename in {directory!r} "
        f"after {_MAX_DISAMBIGUATION} attempts"
    )
