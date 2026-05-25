"""Unit tests for the Charlotte exception hierarchy (CHAR-002)."""

import pytest

from charlotte.exceptions import (
    AdapterOutputError,
    CharlotteConfigError,
    CharlotteError,
    CharlotteInternalError,
    CharlotteNetworkError,
    CharlotteRedirectError,
    CharlotteTimeoutError,
    RobotsError,
)

_ALL_SUBCLASSES = (
    CharlotteConfigError,
    CharlotteNetworkError,
    CharlotteTimeoutError,
    CharlotteRedirectError,
    RobotsError,
    AdapterOutputError,
    CharlotteInternalError,
)


def test_charlotte_error_is_exception():
    assert issubclass(CharlotteError, Exception)


def test_all_subclasses_inherit_charlotte_error():
    for cls in _ALL_SUBCLASSES:
        assert issubclass(cls, CharlotteError), f"{cls.__name__} must inherit CharlotteError"


def test_all_subclasses_are_raisable():
    for cls in _ALL_SUBCLASSES:
        with pytest.raises(cls):
            raise cls("test message")


def test_catching_by_base_class():
    """Any Charlotte exception should be catchable as CharlotteError."""
    for cls in _ALL_SUBCLASSES:
        with pytest.raises(CharlotteError):
            raise cls("caught by base")


def test_exception_message_preserved():
    msg = "something went wrong"
    err = CharlotteNetworkError(msg)
    assert str(err) == msg


def test_subclasses_not_interchangeable():
    """Distinct subclasses must not match each other's except clauses."""
    with pytest.raises(CharlotteTimeoutError):
        try:
            raise CharlotteTimeoutError("timeout")
        except CharlotteNetworkError:
            pytest.fail("CharlotteNetworkError should not catch CharlotteTimeoutError")
