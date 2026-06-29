# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""PUT /api/user/user-identity must persist the per-user quiet-hours fields.

Pins the fix for the bug where saving quiet hours did nothing: the fields were missing from the
UserIdentityUpdate Pydantic model, so FastAPI dropped them before they reached the handler.
Network-free: the handler is called directly with a fake in-memory workspace.
"""
import asyncio

from vaf.api import user_persona_routes as upr


class _FakeWS:
    def __init__(self):
        self.data = {
            "name": "u", "preferred_language": None, "preferences": [], "dos": [], "donts": [],
            "main_messenger": None, "city": None, "country": None,
            "timezone": None, "date_format": None, "time_format": None,
            "quiet_hours_enabled": None, "quiet_hours_start": None, "quiet_hours_end": None,
            "change_log": [],
        }

    def get_user_identity(self):
        return dict(self.data)

    def save_user_identity(self, d):
        self.data = d


def _run(ws, monkeypatch, **fields):
    monkeypatch.setattr(upr, "get_user_workspace", lambda u: ws)
    data = upr.UserIdentityUpdate(**fields)
    return asyncio.run(upr.update_user_identity(data, username="TESTUSER"))


def test_put_persists_quiet_hours(monkeypatch):
    ws = _FakeWS()
    _run(ws, monkeypatch, quiet_hours_enabled=True, quiet_hours_start="22:00", quiet_hours_end="07:00")
    assert ws.data["quiet_hours_enabled"] is True
    assert ws.data["quiet_hours_start"] == "22:00"
    assert ws.data["quiet_hours_end"] == "07:00"


def test_disable_persists_false(monkeypatch):
    ws = _FakeWS()
    ws.data["quiet_hours_enabled"] = True
    _run(ws, monkeypatch, quiet_hours_enabled=False)
    assert ws.data["quiet_hours_enabled"] is False


def test_partial_put_does_not_reset_quiet_hours(monkeypatch):
    ws = _FakeWS()
    ws.data.update({"quiet_hours_enabled": True, "quiet_hours_start": "22:00", "quiet_hours_end": "07:00"})
    # A PUT that omits quiet-hours (e.g. the announcement modal) must leave them untouched.
    _run(ws, monkeypatch, last_seen_announcement_version="1.2")
    assert ws.data["quiet_hours_enabled"] is True
    assert ws.data["quiet_hours_start"] == "22:00"


def test_invalid_time_is_dropped(monkeypatch):
    ws = _FakeWS()
    _run(ws, monkeypatch, quiet_hours_enabled=True, quiet_hours_start="25:99", quiet_hours_end="07:00")
    assert ws.data["quiet_hours_start"] is None       # invalid HH:MM -> None
    assert ws.data["quiet_hours_end"] == "07:00"
