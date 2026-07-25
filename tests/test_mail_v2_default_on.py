# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""P6.2 go/no-go guard: the v2 mail engine is the DEFAULT lane.

The flip is invisible to the rest of the suite - an instrumented run showed zero
unpatched reads of the flag, and the existing guards only check that a schema row
exists and that the total key count matches. So nothing else would catch a silent
revert, a stale doc cell, or the write flag being dragged along. That is what this
file locks.

It also pins the separation the owner asked to keep: only the read/serve flag
graduates here; `mail_engine_write_enabled` stays off as the safety valve for
server-side mailbox writes, and stays a registered admin-only key.
"""
import re
from pathlib import Path

from vaf.core.config import Config

_SCHEMA = Path(__file__).resolve().parents[1] / "docs" / "setup" / "CONFIG_SCHEMA.md"


def _schema_row(key: str) -> str:
    for line in _SCHEMA.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"| `{key}`"):
            return line
    raise AssertionError(f"no CONFIG_SCHEMA.md row for {key}")


def test_v2_engine_is_on_by_default():
    assert Config.DEFAULTS["mail_engine_v2_enabled"] is True


def test_fresh_install_resolves_to_on(tmp_path, monkeypatch):
    """The assertion that catches the 'explicit False default wins' trap: every
    call site passes `Config.get("mail_engine_v2_enabled", False)` and that False
    would win if the key were missing - it resolves to True only because load()
    merges DEFAULTS.

    Config.load() reads the CONFIG_FILE class attribute, so the file must be
    redirected THERE. Patching the module-level constant instead leaves the test
    reading the developer's real ~/.vaf/config.json, where the flag may already be
    set by hand - a vacuous pass that survives a revert of the default.
    """
    monkeypatch.setattr(Config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(Config, "APP_DIR", tmp_path, raising=False)
    assert not Config.CONFIG_FILE.exists()  # fresh install / CI: DEFAULTS decide
    assert Config.get("mail_engine_v2_enabled", False) is True
    assert Config.get("mail_engine_write_enabled", False) is False


def test_write_flag_stays_off_by_default():
    """Only the read/serve flag graduates. Server-side mailbox writes keep their
    own switch by design - do not fold these two together."""
    assert Config.DEFAULTS["mail_engine_write_enabled"] is False


def test_schema_doc_default_cell_matches_the_code():
    """No CI guard compares the doc's Default CELL to DEFAULTS (only row presence
    and the total key count), so a stale `False` would ship silently."""
    for key in ("mail_engine_v2_enabled", "mail_engine_write_enabled"):
        cells = [c.strip() for c in _schema_row(key).split("|")]
        documented = cells[2]                      # | key | Default | Meaning |
        expected = f"`{Config.DEFAULTS[key]}`"     # `True` / `False`
        assert documented == expected, f"{key}: doc says {documented}, code says {expected}"


def test_key_count_line_still_matches_defaults():
    """The flip must NOT change the key count - it is a value change, not a key
    add/remove. The count only moves when the flag is deleted in P7.4."""
    m = re.search(r"`Config\.DEFAULTS` \((\d+) keys\)", _SCHEMA.read_text(encoding="utf-8"))
    assert m, "key-count line missing from CONFIG_SCHEMA.md"
    assert int(m.group(1)) == len(Config.DEFAULTS)


def test_both_mail_flags_stay_admin_only():
    """Instance-wide rollout/resource policy: writable by the admin lane only, never
    from a per-user config write."""
    assert Config.is_global_config_key("mail_engine_v2_enabled") is True
    assert Config.is_global_config_key("mail_engine_write_enabled") is True
