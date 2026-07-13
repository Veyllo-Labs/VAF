# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Scheduler single-owner guard (live 2026-07-13: double TRIGGER on every task).

The `schedule` registry is module-global while AutomationManager._running is
per-instance: the create_automation tool auto-started the scheduler on ITS OWN
manager instance (whose _running was False even though the process scheduler
ran), re-registering every job a second time and spinning up a second loop -
each task then triggered twice and only the run lock prevented double
execution. Callers must go through ensure_scheduler_started; start_scheduler
additionally refuses to run on a non-singleton instance.
"""
from pathlib import Path
from types import SimpleNamespace

import vaf.core.automation as auto_mod
from vaf.core.automation import AutomationManager


def test_start_scheduler_refuses_foreign_instance(monkeypatch):
    # A running process singleton exists -> a DIFFERENT manager instance calling
    # start_scheduler must be a no-op (no second registration, no second loop).
    events = []
    foreign = AutomationManager.__new__(AutomationManager)
    foreign._running = False
    monkeypatch.setattr(foreign, "_log_scheduler_event", lambda msg: events.append(msg), raising=False)
    monkeypatch.setattr(auto_mod, "_scheduler_manager",
                        SimpleNamespace(_running=True, _scheduler_thread=None))
    foreign.start_scheduler()
    assert foreign._running is False
    assert any("another manager instance" in e for e in events), events


def test_singleton_itself_still_starts(monkeypatch):
    # The guard must not block the singleton's own (re)start path: when the
    # process singleton IS self, the method proceeds past the guard.
    mgr = AutomationManager.__new__(AutomationManager)
    mgr._running = False
    events = []
    monkeypatch.setattr(mgr, "_log_scheduler_event", lambda msg: events.append(msg), raising=False)
    monkeypatch.setattr(mgr, "list", lambda enabled_only=False: [], raising=False)
    mgr.storage_dir = Path("/tmp")
    monkeypatch.setattr(auto_mod, "_scheduler_manager", mgr)
    mgr.start_scheduler()
    try:
        assert mgr._running is True
        assert any(e.startswith("START task_count=0") for e in events), events
    finally:
        mgr._running = False  # stop the loop thread


def test_create_tool_uses_the_ensure_helper():
    import vaf.tools.automation as tool_mod
    src = Path(tool_mod.__file__).read_text(encoding="utf-8")
    assert "ensure_scheduler_started(origin=" in src, (
        "create_automation lost the singleton-aware auto-start"
    )
    assert "manager.start_scheduler()" not in src, (
        "create_automation starts the scheduler on its own manager instance again "
        "- this is exactly the double-registration bug"
    )
