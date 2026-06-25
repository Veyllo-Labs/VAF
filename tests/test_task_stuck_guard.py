# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Task-stuck guard: stop a weak model from redoing an already-finished step forever.

The pending-task auto-continue only fires on a final TEXT answer. A model that finished a step's
work but never called mark_task_done lands there every time, so without a brake it redoes the same
step up to the hard cap and carries the unmarked task into the next run. _task_stuck_step counts
CONSECUTIVE auto-continues on the SAME step (resets on a different step), nudges at the threshold,
then signals 'autodone' so the caller force-completes the step. Pins _task_stuck_step in isolation.
"""
from vaf.core.agent import Agent


def _bare() -> Agent:
    a = Agent.__new__(Agent)
    a._autocontinue_step_sig = None
    a._autocontinue_stuck = 0
    return a


def test_verifies_once_then_autoconfirms():
    a = _bare()
    # defaults: nudge_at=1 (verify once), autodone_at=2 (trust + auto-confirm) — same step each time
    assert a._task_stuck_step(0, "do the thing") == "nudge"      # 1st no-progress final -> verify
    assert a._task_stuck_step(0, "do the thing") == "autodone"   # 2nd -> trust the model, resolve
    assert a._autocontinue_stuck == 2


def test_different_step_resets_streak():
    a = _bare()
    a._task_stuck_step(0, "step one")
    a._task_stuck_step(0, "step one")
    assert a._autocontinue_stuck == 2
    # progress: a different step becomes current -> streak resets (fresh verification for the new step)
    assert a._task_stuck_step(1, "step two") == "nudge"
    assert a._autocontinue_stuck == 1


def test_same_index_different_text_is_a_new_step():
    a = _bare()
    a._task_stuck_step(0, "old step")
    assert a._task_stuck_step(0, "rewritten step") == "nudge"
    assert a._autocontinue_stuck == 1


def test_disabled_via_config_never_autodones(monkeypatch):
    from vaf.core.config import Config
    # Return the caller's default for every key except the kill-switch (avoids the fragile
    # call-through pattern; _task_stuck_step only reads these three keys).
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, key, default=None: False if key == "task_stuck_guard_enabled" else default
    ))
    a = _bare()
    for _ in range(8):
        assert a._task_stuck_step(0, "x") == "continue"
    # streak still tracked, but never escalates/auto-completes while disabled
    assert a._autocontinue_stuck == 8


def test_custom_thresholds(monkeypatch):
    from vaf.core.config import Config
    overrides = {"task_stuck_nudge_turns": 2, "task_stuck_autodone_turns": 3}
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, key, default=None: overrides.get(key, default)
    ))
    a = _bare()
    seq = [a._task_stuck_step(0, "t") for _ in range(3)]
    assert seq == ["continue", "nudge", "autodone"]
