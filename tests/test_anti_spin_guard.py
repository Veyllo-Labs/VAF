"""Anti-spin guard: stop a weak model from churning the bookkeeping tools
(update_working_memory / update_intent / add_task) forever without ever acting.

Counts CONSECUTIVE bookkeeping calls; any real tool resets the streak. Nudges at the
threshold, then forces a tools-off turn two steps later. Pins _anti_spin_step in isolation.
"""
from vaf.core.agent import Agent, _BOOKKEEPING_TOOLS


def _bare() -> Agent:
    a = Agent.__new__(Agent)
    a._anti_spin_streak = 0
    return a


def test_bookkeeping_set_is_narrow():
    assert _BOOKKEEPING_TOOLS == {"update_working_memory", "update_intent", "add_task"}


def test_nudge_at_threshold_then_force():
    a = _bare()
    results = [a._anti_spin_step("update_working_memory") for _ in range(7)]
    msgs = [m for m, _ in results]
    forces = [f for _, f in results]

    # default threshold = 4: no nudge for the first 3
    assert msgs[0] is None and msgs[1] is None and msgs[2] is None
    # firm nudge exactly at the 4th consecutive call, tools still on
    assert msgs[3] is not None and "STOP PLANNING" in msgs[3]
    assert forces[3] is False
    # force (tools off) at threshold+2 = the 6th call
    assert forces[5] is True and msgs[5] is not None
    # streak reset after a force, so the 7th call is a fresh streak of 1
    assert forces[6] is False and a._anti_spin_streak == 1


def test_real_tool_resets_streak():
    a = _bare()
    for _ in range(3):
        a._anti_spin_step("update_working_memory")
    assert a._anti_spin_streak == 3
    a._anti_spin_step("web_search")          # real work -> reset
    assert a._anti_spin_streak == 0
    msg, force = a._anti_spin_step("update_intent")
    assert msg is None and force is False and a._anti_spin_streak == 1


def test_non_bookkeeping_never_spins():
    a = _bare()
    for _ in range(10):
        msg, force = a._anti_spin_step("document_agent")
        assert msg is None and force is False
    assert a._anti_spin_streak == 0


def test_disabled_via_config(monkeypatch):
    from vaf.core.config import Config
    orig = Config.get.__func__ if hasattr(Config.get, "__func__") else Config.get
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, key, default=None: False if key == "anti_spin_enabled" else orig(key, default)
    ))
    a = _bare()
    for _ in range(8):
        msg, force = a._anti_spin_step("update_working_memory")
        assert msg is None and force is False
