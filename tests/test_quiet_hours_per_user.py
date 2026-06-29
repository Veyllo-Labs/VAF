# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Per-user, timezone-aware quiet hours for proactive (thinking) runs.

Pins that is_in_quiet_hours():
  - evaluates the window in the USER's timezone (user_identity.timezone = SSOT),
  - lets per-user quiet_hours_* override the global config (None = inherit),
  - and a per-user enabled=False suppresses even when the global flag is on.

Network-free: the username resolver, the user workspace, and Config.get are stubbed.
"""
from datetime import timedelta

import pytest

from vaf.core import thinking_mode as tm
from vaf.core.user_time import user_now


class _FakeWS:
    def __init__(self, identity):
        self._i = identity

    def get_user_identity(self):
        return self._i


@pytest.fixture
def patch_scope(monkeypatch):
    def _setup(identity):
        monkeypatch.setattr(tm, "_resolve_username_for_scope", lambda s: "TESTUSER")
        monkeypatch.setattr("vaf.auth.user_workspace.get_user_workspace", lambda u: _FakeWS(identity))
    return _setup


def _hhmm(identity, hours_from_now):
    return (user_now(identity=identity) + timedelta(hours=hours_from_now)).strftime("%H:%M")


def test_per_user_window_active_in_user_tz(patch_scope):
    ident = {"timezone": "Europe/Berlin", "quiet_hours_enabled": True}
    ident["quiet_hours_start"] = _hhmm(ident, -1)
    ident["quiet_hours_end"] = _hhmm(ident, +1)
    patch_scope(ident)
    assert tm.is_in_quiet_hours("scope-x") is True


def test_per_user_window_inactive(patch_scope):
    ident = {"timezone": "Europe/Berlin", "quiet_hours_enabled": True}
    ident["quiet_hours_start"] = _hhmm(ident, +2)   # window [now+2h, now+3h] -> now not inside
    ident["quiet_hours_end"] = _hhmm(ident, +3)
    patch_scope(ident)
    assert tm.is_in_quiet_hours("scope-x") is False


def test_per_user_disabled_overrides_global(patch_scope, monkeypatch):
    monkeypatch.setattr("vaf.core.config.Config.get",
                        staticmethod(lambda key, default=None: True if key == "thinking_quiet_hours_enabled" else default))
    ident = {"timezone": "Europe/Berlin", "quiet_hours_enabled": False}
    patch_scope(ident)
    assert tm.is_in_quiet_hours("scope-x") is False


def test_global_fallback_when_user_unset(patch_scope, monkeypatch):
    ident = {"timezone": "Europe/Berlin"}  # no per-user quiet_hours_* -> inherit global
    start, end = _hhmm(ident, -1), _hhmm(ident, +1)
    table = {"thinking_quiet_hours_enabled": True,
             "thinking_quiet_hours_start": start,
             "thinking_quiet_hours_end": end}
    monkeypatch.setattr("vaf.core.config.Config.get",
                        staticmethod(lambda key, default=None: table.get(key, default)))
    patch_scope(ident)
    assert tm.is_in_quiet_hours("scope-x") is True
