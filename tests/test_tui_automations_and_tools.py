# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The last two dead settings rows come alive: Automations and the tool list.

The automations submenu carries the classic menu's contract: one line per
automation with the enabled mark, `frequency @ time` and the next run;
activating a row FLIPS enabled (the classic's primary action); the storage
folder opens from a row of its own. Reads are cached on entry and
invalidated on back, like the microphone submenu - `_menu_rows` runs on
every stack move and must never touch the disk itself.

"Show All Tools" pushes the existing ToolsScreen ON TOP of the settings
modal (like About), so the settings stack survives.
"""
import sys as _sys
from types import SimpleNamespace

from vaf.cli.tui_app.screens import SettingsScreen


class _Task:
    def __init__(self, tid, name, enabled):
        self.id = tid
        self.name = name
        self.enabled = enabled
        self.frequency = "daily"
        self.time = "09:00"
        self.next_run = "2026-08-07T09:00:00"


def _mgr_module(monkeypatch, tasks, updates):
    class _Mgr:
        storage_dir = "/home/user/.vaf/automations"

        def list(self, enabled_only=False):
            return tasks

        def get(self, tid):
            return next((t for t in tasks if t.id == tid), None)

        def update(self, tid, **kw):
            updates.append((tid, kw))
            return self.get(tid)

    monkeypatch.setitem(_sys.modules, "vaf.core.automation",
                        SimpleNamespace(AutomationManager=_Mgr))


def _screen(monkeypatch, tasks, updates=None):
    updates = [] if updates is None else updates
    _mgr_module(monkeypatch, tasks, updates)

    fake_app = SimpleNamespace(notified=[], tools_opened=[])
    fake_app.notify = lambda msg, **kw: fake_app.notified.append(msg)
    fake_app.action_tools = lambda: fake_app.tools_opened.append(True)

    class _S(SettingsScreen):
        app = property(lambda s: fake_app)

    s = _S.__new__(_S)
    s._cfg = lambda key, default=None: default
    s._refresh_labels = lambda: None
    s._rebuild = lambda: None
    s._stack = ["main", "automations"]
    s._mic_devices = None
    s._automations = None
    return s, fake_app, updates


def test_the_rows_carry_the_classic_table(monkeypatch):
    s, _, _ = _screen(monkeypatch, [_Task("a1", "Backup", True),
                                    _Task("a2", "Report", False)])
    s._load_automations()
    rows = s._menu_rows("automations")
    body = [label for kind, _, label in rows if kind == "automation"]
    assert len(body) == 2
    assert "●" in body[0] and "Backup" in body[0] and "daily @ 09:00" in body[0]
    assert "2026-08-07T09:00" in body[0], "next run missing or not trimmed"
    assert "○" in body[1] and "Report" in body[1]
    kinds = [k for k, _, _ in rows]
    assert "automation_folder" in kinds and "back" in kinds


def test_no_automations_is_an_honest_note_with_the_way_to_create_one(monkeypatch):
    s, _, _ = _screen(monkeypatch, [])
    s._load_automations()
    rows = s._menu_rows("automations")
    assert rows[0][0] == "note" and "no automations yet" in rows[0][2]


def test_activating_a_row_flips_enabled(monkeypatch):
    s, fake_app, updates = _screen(monkeypatch, [_Task("a1", "Backup", True)])
    s._load_automations()
    s._rows = [("automation", "a1", "")]
    s._activate(0)
    assert updates == [("a1", {"enabled": False})], (
        "the toggle wrote something other than the flipped enabled flag")
    assert fake_app.notified and "disabled" in fake_app.notified[0]


def test_a_broken_store_is_a_note_not_a_crash(monkeypatch):
    monkeypatch.setitem(_sys.modules, "vaf.core.automation", None)
    fake_app = SimpleNamespace()

    class _S(SettingsScreen):
        app = property(lambda s: fake_app)

    s = _S.__new__(_S)
    s._automations = None
    s._load_automations()
    assert isinstance(s._automations, str) and "unavailable" in s._automations
    rows = s._menu_rows("automations")
    assert rows[0][0] == "note"


def test_the_menu_never_reads_the_disk_itself(monkeypatch):
    """_menu_rows renders from the cache only - it runs on every stack move."""
    s, _, _ = _screen(monkeypatch, [_Task("a1", "Backup", True)])
    monkeypatch.setitem(_sys.modules, "vaf.core.automation", None)  # any import would blow
    s._automations = [("a1", "Backup", True, "daily @ 09:00", "2026-08-07T09:00")]
    rows = s._menu_rows("automations")
    assert any(k == "automation" for k, _, _ in rows)


def test_going_back_invalidates_the_cache(monkeypatch):
    s, _, _ = _screen(monkeypatch, [_Task("a1", "Backup", True)])
    s._load_automations()
    s._stack = ["main", "automations"]
    s.dismiss = lambda *a: None
    s.action_go_back()
    assert s._automations is None, "stale rows would survive a re-entry"


def test_show_all_tools_opens_the_existing_screen_on_top(monkeypatch):
    s, fake_app, _ = _screen(monkeypatch, [])
    s._rows = [("tools", None, "Show All Tools")]
    s._activate(0)
    assert fake_app.tools_opened == [True]


def test_the_two_rows_left_the_dead_set():
    s = SettingsScreen.__new__(SettingsScreen)
    s._cfg = lambda k, d=None: d
    s._mic_devices = None
    s._automations = None
    rows = s._menu_rows("main")
    later = {arg for kind, arg, _ in rows if kind == "later"}
    assert "tools" not in later and "automations" not in later
    kinds = {(k, a) for k, a, _ in rows}
    assert ("submenu", "automations") in kinds
    assert ("tools", None) in kinds
