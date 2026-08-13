# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The brake that stops two agents from thanking each other forever.

Excluding a peer's OWN frames from its wake-up is not enough: two agents each ignore
themselves and still answer each other without end, because each is being woken by
somebody else. From the cross-machine step on, that burns two machines.

The counter is per room and counts room-driven turns since a real person spoke. The
tests below pin all three halves of that sentence - per room, room-driven, and real
person - because getting any one of them wrong makes the brake either useless or a
gag on a legitimate conversation.
"""
from pathlib import Path

import pytest

import vaf.core.a2a.store as store_mod
from vaf.core.a2a.room import Room, derive_peer_id, participant_key
from vaf.core.config import Config

SCOPE = "scope-agent"
KEY = participant_key("agent", SCOPE)


class _Agent:
    """The wake-up and the guard, lifted off the real class with no model behind them."""

    from vaf.core.agent import Agent as _Real

    collect_room_wake = _Real.collect_room_wake
    _room_loop_guard_trips = _Real._room_loop_guard_trips
    note_human_turn = _Real.note_human_turn

    def __init__(self):
        self._current_user_scope_id = SCOPE
        self._room_reply_streak = {}


@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    return tmp_path


def _room(base, room_id, *, mode="assist"):
    """A room the agent has joined, plus a second peer to talk at it."""
    room = Room.create(kind="round", owner_scope=None, base=base, room_id=room_id)
    room.join(display="VAF", scope_id=None, peer_id=derive_peer_id(KEY, room_id), mode=mode)
    other = room.join(display="Other", scope_id=None, peer_id="p-other")
    return room, other


def _drain(agent, room, other, *, turns):
    """Run N wake-ups, each preceded by a fresh message from the other peer."""
    delivered = []
    for n in range(turns):
        room.say(other, f"message {n}")
        wake = agent.collect_room_wake()
        delivered.append(wake)
        if wake:
            wake["advance"]()
    return delivered


# ── the loop stops ──────────────────────────────────────────────────────────

def test_a_runaway_room_stops_waking_the_agent(rooms):
    """MUTATION: never increment the streak.

    This is the whole point. Without it two agents answer each other until somebody
    reads a bill, and neither of them is doing anything wrong by its own rules.
    """
    agent = _Agent()
    room, other = _room(rooms, "room-loop")
    limit = int(Config.get("room_loop_max_turns", 6))

    delivered = _drain(agent, room, other, turns=limit + 3)

    assert all(d is not None for d in delivered[:limit]), "the brake bit too early"
    assert all(d is None for d in delivered[limit:]), "the brake never bit"


def test_the_pause_is_said_in_the_room_exactly_once(rooms):
    """MUTATION: announce on every blocked turn instead of only the first.

    A silent agent reads as a broken one to the peers waiting on it, so the pause is
    spoken. Saying it every turn would replace one runaway loop with another, quieter
    one - and this notice is written by the framework rather than the model, so it
    cannot itself cost a turn.
    """
    agent = _Agent()
    room, other = _room(rooms, "room-say-once")
    limit = int(Config.get("room_loop_max_turns", 6))

    _drain(agent, room, other, turns=limit + 4)

    notices = [r for r in room.transcript()
               if r["display"] == "VAF" and "Pausing here" in r["text"]]
    assert len(notices) == 1, [n["text"] for n in notices]


def test_an_observer_pauses_without_speaking(rooms):
    """An observer may not talk in the room, and the brake does not get to break that.

    MUTATION: write the pause notice regardless of mode. The user who chose observe
    asked for an agent that reads and stays out of it.
    """
    agent = _Agent()
    room, other = _room(rooms, "room-quiet", mode="observe")
    limit = int(Config.get("room_loop_max_turns", 6))

    delivered = _drain(agent, room, other, turns=limit + 2)

    assert delivered[-1] is None, "the brake must still bite for an observer"
    assert [r for r in room.transcript() if r["display"] == "VAF" and r["kind"] == "say"] == []


# ── what counts as a human ─────────────────────────────────────────────────

def test_a_message_from_the_user_lets_the_room_run_again(rooms):
    """MUTATION: do not reset the streak on a real user turn.

    Without the reset the brake is a one-way door: the room is dead for the rest of
    the process even after the person comes back and asks for exactly this.
    """
    agent = _Agent()
    room, other = _room(rooms, "room-reset")
    limit = int(Config.get("room_loop_max_turns", 6))

    _drain(agent, room, other, turns=limit + 1)
    assert agent.collect_room_wake() is None

    agent._room_reply_streak = {}          # what a real user turn does, see the wiring test
    room.say(other, "and now?")
    assert agent.collect_room_wake() is not None


def test_a_human_turn_clears_every_paused_room():
    """MUTATION: make note_human_turn a no-op.

    Without it the brake is a one-way door: the room stays dead for the rest of the
    process even after the person comes back and asks for exactly this.
    """
    agent = _Agent()
    agent._room_reply_streak = {"room-a": 9, "room-b": 3}

    agent.note_human_turn()

    assert agent._room_reply_streak == {}


def test_a_timer_and_an_automation_are_not_a_person():
    """MUTATION: reset the streak from chat_step's "real user message" test instead.

    That test was written for a different question - clearing the ask-first latch - and
    a TIMER passes it: a timer enqueues an ordinary task with the user's own text and no
    background marker. A repeating one would hold the brake open forever, which is the
    exact failure this brake exists to prevent, arriving through the reset.

    The discrimination therefore lives at the queue boundary, where the task still knows
    what it was. Asserted at the source, because what must not drift is the CONDITION,
    and building a whole runner to drive one branch would test less of it.
    """
    root = Path(__file__).resolve().parents[1]
    agent_src = (root / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    runner_src = (root / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")

    # chat_step must NOT touch the streak: its user-message test lets timers through.
    latch = agent_src.split('if user_input and not skip_input and not getattr(self, "_synthetic_drain_turn", False):')[1]
    assert "_room_reply_streak" not in latch.split("\n\n")[0], (
        "the streak is reset from a test that a timer also satisfies")

    # The runner composes all three halves before calling it.
    call = runner_src.split("agent.note_human_turn()")[0]
    tail = call[-700:]
    assert 'task_class", "") == "interactive"' in tail
    assert '_meta_h.get("timer")' in tail
    assert "task.input_text" in tail


# ── per room, not per agent ────────────────────────────────────────────────

def test_one_runaway_room_does_not_silence_another(rooms):
    """MUTATION: keep a single global counter instead of one per room.

    A conversation that ran away in one room is no reason to stop listening in another.
    The cost of this choice is real and named in the docs: an agent in five rooms can
    burn five budgets before anybody notices.
    """
    agent = _Agent()
    busy, busy_other = _room(rooms, "room-busy")
    quiet, quiet_other = _room(rooms, "room-quiet-2")
    limit = int(Config.get("room_loop_max_turns", 6))

    _drain(agent, busy, busy_other, turns=limit + 2)

    quiet.say(quiet_other, "anyone home?")
    wake = agent.collect_room_wake()
    assert wake is not None and wake["room_id"] == "room-quiet-2"


# ── the kill switch ────────────────────────────────────────────────────────

def test_the_guard_can_be_turned_off(rooms, monkeypatch):
    """MUTATION: ignore room_loop_guard_enabled.

    Every brake in this codebase has a switch, because the one time it fires wrongly is
    the time somebody needs to get work done without editing source.
    """
    real_get = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: False if key == "room_loop_guard_enabled" else real_get(key, default)))

    agent = _Agent()
    room, other = _room(rooms, "room-off")
    limit = int(real_get("room_loop_max_turns", 6))

    delivered = _drain(agent, room, other, turns=limit + 3)
    assert all(d is not None for d in delivered)


def test_the_threshold_is_read_from_config(rooms, monkeypatch):
    real_get = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: 2 if key == "room_loop_max_turns" else real_get(key, default)))

    agent = _Agent()
    room, other = _room(rooms, "room-tight")

    delivered = _drain(agent, room, other, turns=4)
    assert delivered[0] is not None and delivered[1] is not None
    assert delivered[2] is None and delivered[3] is None


def test_a_broken_guard_fails_open(rooms):
    """MUTATION: fail closed in the guard's except branch.

    The opposite polarity to the mode gate, on purpose. This one guards a BUDGET; the
    mode gate guards the machine. A broken budget check that silenced every room would
    be a worse failure than the loop it prevents, and a human is in the conversation
    either way.
    """
    agent = _Agent()

    class _Exploding:
        @property
        def room_id(self):
            raise RuntimeError("no")

    assert agent._room_loop_guard_trips(_Exploding(), None) is False


# ── the keys exist where the house keeps them ──────────────────────────────

def test_both_keys_are_registered_and_documented():
    """Rule 2: a config key lives in DEFAULTS, in CONFIG_SCHEMA.md, and in that file's
    key-count line. A key missing from any of the three drifts silently."""
    root = Path(__file__).resolve().parents[1]
    assert Config.DEFAULTS["room_loop_guard_enabled"] is True
    assert Config.DEFAULTS["room_loop_max_turns"] == 6

    doc = (root / "docs" / "setup" / "CONFIG_SCHEMA.md").read_text(encoding="utf-8")
    assert "`room_loop_guard_enabled`" in doc
    assert "`room_loop_max_turns`" in doc
    assert f"({len(Config.DEFAULTS)} keys)" in doc
