# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A config flag stored as text must read the same as one stored as a bool.

`config.json` is hand-editable, the HTTP save passes JSON through, and an
installer can write `"true"` as a string, so a flag arrives as a bool on one
machine and as text on the next. `bool("false")` is True in Python, so the
obvious coercion reads a disabled switch as enabled - which is why four call
sites had each written the same careful comparison by hand instead.

Four hand copies is the measurement that earned `Config.get_bool`: one bad
spelling of that comparison would have turned a switch off for one provider
lane and left it on for the others, with nothing failing. Environment variables
are a different source with a different reader (`display_platform._is_truthy`)
and are deliberately not folded in here.
"""
import pathlib
import re

import pytest

from vaf.core.config import Config

_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Every site that reads a config flag as a bool. Kept as data so a fifth hand
# copy has to be added here to pass, which is the moment to use the helper.
_CALLERS = (
    ("vaf/core/api_backend.py", "anthropic_prompt_cache"),
    ("vaf/core/api_backend.py", "anthropic_thinking"),
    ("vaf/core/api_backend.py", "google_thinking"),
    ("vaf/core/speaker_confirm.py", "speaker_id_confirmation_enabled"),
)


@pytest.fixture
def stored(monkeypatch):
    """Serve a config dict without touching the user's config.json."""
    def _serve(value):
        monkeypatch.setattr(Config, "load", classmethod(lambda cls: {"flag": value}))
    return _serve


@pytest.mark.parametrize("value", [True, "true", "True", " ON ", "yes", "1", 1])
def test_a_flag_that_means_on_reads_as_on(stored, value):
    stored(value)
    assert Config.get_bool("flag", False) is True


@pytest.mark.parametrize("value", [False, "false", "False", "off", "no", "0", 0, "", "  "])
def test_a_flag_that_means_off_reads_as_off(stored, value):
    """MUTATION: replace the comparison with `bool(value)` and the string cases
    go red, because every non-empty string is truthy in Python."""
    stored(value)
    assert Config.get_bool("flag", True) is False


def test_an_absent_flag_falls_back_to_the_given_default(monkeypatch):
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {}))
    assert Config.get_bool("flag", True) is True
    assert Config.get_bool("flag", False) is False


def test_a_stored_null_is_off_not_the_default(stored):
    """`None` in the file is a written value, not an absent key: a flag someone
    cleared must not silently come back as its default."""
    stored(None)
    assert Config.get_bool("flag", True) is False


def test_the_result_is_a_bool_not_a_truthy_value(stored):
    """Callers pass it straight into provider payloads, where `"true"` and True
    are not the same thing on the wire."""
    stored("true")
    assert Config.get_bool("flag", False) is True
    stored("nonsense")
    assert Config.get_bool("flag", True) is False


@pytest.mark.parametrize("path,key", _CALLERS)
def test_a_flag_reader_uses_the_helper_rather_than_its_own_comparison(path, key):
    """MUTATION: hand-roll the coercion back at any site and its case goes red."""
    text = (_ROOT / path).read_bytes().decode()
    assert re.search(rf'Config\.get_bool\(\s*["\']{re.escape(key)}["\']', text), (
        f"{path} no longer reads `{key}` through Config.get_bool")
    assert f'Config.get("{key}"' not in text, (
        f"{path} reads `{key}` through Config.get, which returns whatever the "
        f"file holds rather than a bool")
