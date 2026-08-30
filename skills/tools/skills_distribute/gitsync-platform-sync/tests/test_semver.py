"""Tests for semver helpers (two-digit rollover)."""
import pytest

from gitsync.semver import bump, highest, next_patch, parse_version


def test_parse_and_bump_patch():
    assert bump("1.0.0") == "1.0.1"
    assert bump("1.0.99") == "1.1.0"
    assert bump("1.99.99") == "2.0.0"


def test_bump_minor_major():
    assert bump("1.5.7", "minor") == "1.6.0"
    assert bump("1.99.0", "minor") == "2.0.0"
    assert bump("1.5.7", "major") == "2.0.0"


def test_next_patch_rollover():
    assert next_patch("1.2.98") == "1.2.99"
    assert next_patch("1.2.99") == "1.3.0"


def test_highest():
    assert highest(["1.0.1", "1.0.0", "2.0.0"]) == "2.0.0"
    assert highest(["1.9.9", "1.10.0"]) == "1.10.0"
    assert highest([]) is None


def test_invalid_version():
    with pytest.raises(ValueError):
        parse_version("abc")
