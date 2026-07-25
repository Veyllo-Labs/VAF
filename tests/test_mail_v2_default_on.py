# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What survived the engine flag's graduation.

`mail_engine_v2_enabled` is gone: the engine is the only mail lane, so there is
nothing left to switch. `mail_engine_write_enabled` deliberately STAYED - it is the
separate safety valve for server-side MAILBOX writes (flags/move/append), and it is
the thing most at risk of being swept up with the flag it used to sit next to. This
file pins it: still present, still False by default, still admin-only, still
documented with a matching default cell - plus the key count, which must decrement
by exactly one.
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


def test_fresh_install_keeps_mailbox_writes_off(tmp_path, monkeypatch):
    """A fresh install must not write to anyone's mailbox until asked.

    Config.load() reads the CONFIG_FILE class attribute, so the file must be
    redirected THERE. Patching the module-level constant instead leaves the test
    reading the developer's real ~/.vaf/config.json - a vacuous pass.
    """
    monkeypatch.setattr(Config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(Config, "APP_DIR", tmp_path, raising=False)
    assert not Config.CONFIG_FILE.exists()  # fresh install / CI: DEFAULTS decide
    assert Config.get("mail_engine_write_enabled", False) is False
    assert "mail_engine_v2_enabled" not in Config.DEFAULTS   # graduated, not renamed


def test_write_flag_stays_off_by_default():
    """Only the read/serve flag graduates. Server-side mailbox writes keep their
    own switch by design - do not fold these two together."""
    assert Config.DEFAULTS["mail_engine_write_enabled"] is False


def test_schema_doc_default_cell_matches_the_code():
    """No CI guard compares the doc's Default CELL to DEFAULTS (only row presence
    and the total key count), so a stale `False` would ship silently."""
    for key in ("mail_engine_write_enabled",):
        cells = [c.strip() for c in _schema_row(key).split("|")]
        documented = cells[2]                      # | key | Default | Meaning |
        expected = f"`{Config.DEFAULTS[key]}`"     # `True` / `False`
        assert documented == expected, f"{key}: doc says {documented}, code says {expected}"


def test_key_count_line_still_matches_defaults():
    """CI-enforced: the doc line must equal len(DEFAULTS). Graduating the engine
    flag moved it 280 -> 279; a deletion without the decrement turns CI red."""
    m = re.search(r"`Config\.DEFAULTS` \((\d+) keys\)", _SCHEMA.read_text(encoding="utf-8"))
    assert m, "key-count line missing from CONFIG_SCHEMA.md"
    assert int(m.group(1)) == len(Config.DEFAULTS)


def test_the_write_flag_stays_admin_only():
    """Instance-wide resource policy: writable by the admin lane only, never from a
    per-user config write."""
    assert Config.is_global_config_key("mail_engine_write_enabled") is True
