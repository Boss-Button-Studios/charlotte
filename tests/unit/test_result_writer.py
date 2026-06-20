"""Tests for charlotte.core.result_writer — safe, unique result-file delivery.

Covers the audit findings the writer rework addresses:
  FUN-2 — unique filename, URL-derived fallback (not a literal "result").
  SEC-3 — never overwrite a sibling file.
  SEC-5 — control characters (incl. NUL) in a server-supplied name don't crash.
"""

import pytest

from charlotte.core.result_writer import safe_result_basename, write_result_file
from charlotte.exceptions import CharlotteConfigError


# --- safe_result_basename ------------------------------------------------------

def test_basename_prefers_suggested_filename():
    assert safe_result_basename("calendar.pdf", "http://x.com/whatever/") == "calendar.pdf"


def test_basename_falls_back_to_url_path(monkeypatch):
    assert safe_result_basename(None, "http://x.com/files/handbook.pdf") == "handbook.pdf"


def test_basename_final_fallback_is_result():
    # No suggestion and a directory-style URL with no basename → the literal fallback.
    assert safe_result_basename(None, "http://x.com/calendar/") == "result"


def test_basename_strips_path_traversal():
    assert safe_result_basename("../../etc/passwd", "http://x.com/") == "passwd"


def test_basename_strips_control_chars_including_nul():
    # SEC-5: a NUL/control char must be removed, not carried into a filesystem path.
    assert "\x00" not in safe_result_basename("evil\x00.pdf", "http://x.com/")
    assert safe_result_basename("ev\x01il\x7f.pdf", "http://x.com/") == "evil.pdf"


def test_basename_dot_leading_name_falls_through():
    # A ".bashrc"-style name is not a usable result basename → fall back.
    assert safe_result_basename(".hidden", "http://x.com/doc.pdf") == "doc.pdf"


# --- write_result_file ---------------------------------------------------------

def test_write_returns_path_with_expected_bytes(tmp_path):
    p = write_result_file(tmp_path, b"%PDF-1.4 data", "doc.pdf", "http://x.com/doc.pdf")
    assert p == tmp_path / "doc.pdf"
    assert p.read_bytes() == b"%PDF-1.4 data"


def test_write_never_overwrites_disambiguates(tmp_path):
    """SEC-3: a second result with the same name must not clobber the first; both
    survive as distinct files."""
    first = write_result_file(tmp_path, b"version-A", "cal.pdf", "http://x.com/cal.pdf")
    second = write_result_file(tmp_path, b"version-B", "cal.pdf", "http://x.com/cal.pdf")
    third = write_result_file(tmp_path, b"version-C", "cal.pdf", "http://x.com/cal.pdf")
    assert first == tmp_path / "cal.pdf"
    assert second == tmp_path / "cal (2).pdf"
    assert third == tmp_path / "cal (3).pdf"
    assert first.read_bytes() == b"version-A"   # original untouched
    assert second.read_bytes() == b"version-B"
    assert third.read_bytes() == b"version-C"


def test_write_nul_byte_name_does_not_crash(tmp_path):
    """SEC-5: a NUL in the name used to raise an uncaught ValueError from open()."""
    p = write_result_file(tmp_path, b"data", "bad\x00name.pdf", "http://x.com/")
    assert p.read_bytes() == b"data"
    assert "\x00" not in p.name


def test_write_to_unwritable_target_raises_config_error(tmp_path):
    """A directory path that is actually a file is a config error, named not raw."""
    blocked = tmp_path / "not_a_dir"
    blocked.write_text("i am a file")
    with pytest.raises(CharlotteConfigError):
        write_result_file(blocked, b"data", "doc.pdf", "http://x.com/doc.pdf")
