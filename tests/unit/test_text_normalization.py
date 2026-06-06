"""Unit tests for charlotte.core.text_normalization (spec §4.5.1)."""

from charlotte.core.text_normalization import normalize_text, tokenize


def test_nfkc_fullwidth_collapsed():
    # Fullwidth ASCII letters should collapse to ASCII equivalents.
    assert normalize_text("ＣＥＯ") == "ceo"


def test_nfkc_halfwidth_katakana():
    # Half-width katakana normalises to full-width via NFKC.
    result = normalize_text("ｴ")  # half-width katakana KI
    assert result != "ｴ"          # must have changed form


def test_whitespace_folding():
    assert normalize_text("  hello   world  ") == "hello world"


def test_whitespace_folding_tabs_and_newlines():
    assert normalize_text("foo\t\nbar") == "foo bar"


def test_casefold():
    assert normalize_text("CEO") == "ceo"
    assert normalize_text("Straße") == "strasse"  # German ß → ss


def test_normalize_empty():
    assert normalize_text("") == ""


def test_normalize_whitespace_only():
    assert normalize_text("   ") == ""


def test_tokenize_basic():
    assert tokenize("Find the Downloads page") == ["find", "the", "downloads", "page"]


def test_tokenize_fullwidth():
    # Fullwidth 'Ａ' should tokenize the same as ASCII 'a'.
    result = tokenize("ＡBC")  # ＡBC
    assert result == ["abc"]


def test_tokenize_empty():
    assert tokenize("") == []


def test_tokenize_whitespace_only():
    assert tokenize("   ") == []


def test_normalization_symmetry():
    """Normalizing an already-normalized string is idempotent."""
    for text in ["hello world", "ceo", "python downloads page"]:
        assert normalize_text(normalize_text(text)) == normalize_text(text)
