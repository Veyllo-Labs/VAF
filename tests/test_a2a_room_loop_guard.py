# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What happens when two agents keep answering each other in a room.

Two layers, and the order matters. The one that PREVENTS a thank-you loop is a line
carried in every wake prompt telling the agent not to answer a message that says
nothing. The one that catches what slips through is a message to the owner - and it is
a MESSAGE, never a stop.

Stopping was the first design and it was wrong: halting unattended but legitimate work
does not remove the damage, it moves it, from tokens spent to work left undone with
nobody there to wake it. That is worst in exactly the case the autonomous mode exists
for, two agents working while both owners are away. The real ceiling is
spend_budget_usd_per_day, which is per user, per day and far finer grained than
anything here could be.

So these tests pin both directions: the notice fires at every interval, AND the turn
runs anyway.
"""
from pathlib import Path

import pytest

import vaf.core.a2a.store as store_mod
from vaf.core.a2a.room import Room, derive_peer_id, participant_key
from vaf.core.config import Config

SCOPE = "scope-agent"
KEY = participant_key("agent", SCOPE)
ROOT = Path(__file__).resolve().parents[1]


class _Agent:
    """The wake-up and the reporter, lifted off the real class with no model behind them."""

    from vaf.core.agent import Agent as _Real

    collect_room_wake = _Real.collect_room_wake
    _room_unattended_report = _Real._room_unattended_report
    note_human_turn = _Real.note_human_turn

    def __init__(self):
        self._current_user_scope_id = SCOPE
        self._current_username = "owner"
        self._room_reply_streak = {}


@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    return tmp_path


@pytest.fixture()
def sent(monkeypatch):
    """Capture what goes out on the owner's channel, without a channel."""
    out = []
    import vaf.core.messaging_connections as mc
    monkeypatch.setattr(mc, "send_to_main_messenger",
                        lambda scope, user, text, **kw: (out.append((scope, user, text)) or (True, "telegram")))
    return out


def _room(base, room_id, *, mode="assist"):
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


# ── the work never stops ───────────────────────────────────────────────────

def test_the_turn_still_runs_at_the_threshold_and_far_past_it(rooms, sent):
    """MUTATION: suppress the turn at the threshold, the way the first design did.

    A stop of real work is worse than a loop. The autonomous mode exists for two agents
    working while both owners are away, and pausing there leaves the work undone with
    nobody to wake it - the damage moved, not removed.
    """
    agent = _Agent()
    room, other = _room(rooms, "room-never-stops")
    every = int(Config.get("room_unattended_report_every_turns", 20))

    delivered = _drain(agent, room, other, turns=every * 2 + 5)

    assert all(d is not None for d in delivered), "a turn was suppressed"
    assert agent._room_reply_streak["room-never-stops"] == every * 2 + 5


def test_no_path_in_the_wake_code_suppresses_a_room():
    """MUTATION: reintroduce the skip-this-room branch.

    The wake-up must hand back the oldest pending room unconditionally. A guard that
    could return "not this one" is the stop this design removed.
    """
    source = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    wake = source.split("def collect_room_wake")[1].split("\n    def ")[0]

    assert "pending[0]" in wake, "the wake-up no longer takes the oldest room outright"
    for banned in ("if picked is None", "guard_trips", "continue"):
        assert banned not in wake, f"a suppression path is back in the wake-up: {banned!r}"

    report = source.split("def _room_unattended_report")[1].split("\n    def ")[0]
    assert "-> None" in source.split("def _room_unattended_report")[1][:40], (
        "the reporter returns a verdict again")
    assert "return True" not in report, "the reporter can suppress a turn again"


# ── the notice ─────────────────────────────────────────────────────────────

def test_one_notice_at_the_threshold_and_one_at_every_multiple(rooms, sent):
    """MUTATION: report once and never again.

    A single notice at turn twenty is a notice somebody misses. The owner is told
    again at forty, at sixty, and so on, for as long as the room keeps running without
    them.
    """
    agent = _Agent()
    room, other = _room(rooms, "room-notice")
    every = int(Config.get("room_unattended_report_every_turns", 20))

    _drain(agent, room, other, turns=every - 1)
    assert sent == [], "the owner was bothered before the interval"

    _drain(agent, room, other, turns=1)
    assert len(sent) == 1, "no notice at the interval"

    _drain(agent, room, other, turns=every)
    assert len(sent) == 2, "no second notice at twice the interval"

    _drain(agent, room, other, turns=every)
    assert len(sent) == 3


def test_the_notice_names_the_room_the_count_and_the_way_out(rooms, sent):
    agent = _Agent()
    room, other = _room(rooms, "room-content")
    every = int(Config.get("room_unattended_report_every_turns", 20))

    _drain(agent, room, other, turns=every)

    scope, user, text = sent[0]
    assert scope == SCOPE and user == "owner"
    assert "room-content" in text
    assert str(every) in text
    assert "still" in text.lower(), "the owner is not told the work continues"
    assert "vaf a2a say room-content" in text, "no way to end it is given"


def test_the_notice_goes_through_the_channel_agnostic_router(rooms):
    """MUTATION: call a platform tool such as send_telegram directly.

    The delivery rule in this codebase is channel-agnostic: whatever the owner's main
    messenger is, the notice follows it. Hardwiring a platform would reach whoever
    happens to use that one.
    """
    source = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    report = source.split("def _room_unattended_report")[1].split("\n    def ")[0]

    assert "send_to_main_messenger" in report
    for platform in ("send_telegram", "send_discord", "send_whatsapp", "send_slack"):
        assert platform not in report, f"{platform} is hardwired into the notice"


def test_the_notice_is_written_by_the_framework_and_costs_no_turn(rooms, sent):
    """MUTATION: produce the notice by asking the model.

    A watchman that costs a turn every time it looks is a second loop. The text is a
    literal in the framework, so the notice can never be the thing that keeps the room
    awake.
    """
    agent = _Agent()
    room, other = _room(rooms, "room-cheap")
    every = int(Config.get("room_unattended_report_every_turns", 20))

    _drain(agent, room, other, turns=every)

    assert len(sent) == 1
    # Nothing the agent said landed in the room: the notice went to the owner only.
    assert [r for r in room.transcript() if r["display"] == "VAF" and r["kind"] == "say"] == []


def test_a_failing_channel_never_touches_the_turn(rooms, monkeypatch):
    """The notice is best effort. A messenger that is down must not cost the work."""
    import vaf.core.messaging_connections as mc

    def _boom(*_a, **_kw):
        raise RuntimeError("no channel")

    monkeypatch.setattr(mc, "send_to_main_messenger", _boom)

    agent = _Agent()
    room, other = _room(rooms, "room-broken-channel")
    every = int(Config.get("room_unattended_report_every_turns", 20))

    delivered = _drain(agent, room, other, turns=every + 2)
    assert all(d is not None for d in delivered)


# ── what counts as a human ─────────────────────────────────────────────────

def test_a_human_turn_clears_every_room(rooms, sent):
    """MUTATION: make note_human_turn a no-op.

    The owner coming back is what the count is about. Without the reset the notices
    keep arriving from a conversation the person is already part of.
    """
    agent = _Agent()
    room, other = _room(rooms, "room-reset")
    every = int(Config.get("room_unattended_report_every_turns", 20))

    _drain(agent, room, other, turns=every)
    assert len(sent) == 1

    agent.note_human_turn()
    assert agent._room_reply_streak == {}

    _drain(agent, room, other, turns=every - 1)
    assert len(sent) == 1, "the count did not start over when the person came back"


def test_a_timer_and_an_automation_are_not_a_person():
    """MUTATION: reset the streak from chat_step's "real user message" test instead.

    That test was written for a different question - clearing the ask-first latch - and
    a TIMER passes it: a timer enqueues an ordinary task with the user's own text and
    no background marker. A repeating one would silence the notices forever.

    The discrimination lives at the queue boundary, where the task still knows what it
    was. Asserted at the source, because what must not drift is the CONDITION.
    """
    agent_src = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    runner_src = (ROOT / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")

    # The streak is reset from note_human_turn, never from the latch-clearing branch in
    # chat_step: that branch has its own condition and its own history, and hanging a
    # second meaning on it is how two rules end up sharing one bug.
    import ast
    for node in ast.walk(ast.parse(agent_src)):
        if isinstance(node, ast.If):
            body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            if "_pending_user_question" in body:
                assert "_room_reply_streak" not in body, (
                    "the streak is reset from the latch branch instead of note_human_turn")

    # The runner decides once, at the queue boundary, and both consumers ride on that
    # one decision: the ask-first latch and this streak. Two copies of "was that a
    # person" would drift, and one of them would be the enforcing one.
    decision = runner_src.split("_turn_is_human = bool(")[1].split("\n                    )")[0]
    assert 'task_class", "") == "interactive"' in decision
    assert 'get("timer")' in decision
    assert "task.input_text" in decision
    assert "if _turn_is_human:" in runner_src.split("agent.note_human_turn()")[0][-200:]


# ── the layer that prevents the loop in the first place ────────────────────

def test_every_wake_prompt_carries_the_reminder(rooms):
    """MUTATION: remove the reminder line, or send it only every N turns.

    This is the layer that PREVENTS a thank-you loop; the notice is only the watchman
    behind it. Constant rather than periodic on purpose: a line that is always there
    cannot be forgotten deep in a context, and a line that appears every N turns is
    absent for the N-1 turns that build the loop.
    """
    agent = _Agent()
    room, other = _room(rooms, "room-reminder")

    for _ in range(3):
        room.say(other, "thanks!")
        wake = agent.collect_room_wake()
        assert wake is not None
        assert "REMINDER" in wake["prompt"]
        assert "thank you" in wake["prompt"].lower()
        assert "no new information" in wake["prompt"].lower()
        wake["advance"]()


# ── counting, per room ─────────────────────────────────────────────────────

def test_the_count_is_per_room(rooms, sent):
    """MUTATION: keep one global counter.

    Two busy rooms would otherwise report each other's turns, and the owner would be
    told a number that belongs to no single conversation.
    """
    agent = _Agent()
    busy, busy_other = _room(rooms, "room-busy")
    quiet, quiet_other = _room(rooms, "room-quiet-2")
    every = int(Config.get("room_unattended_report_every_turns", 20))

    _drain(agent, busy, busy_other, turns=every - 1)
    _drain(agent, quiet, quiet_other, turns=3)

    assert sent == []
    assert agent._room_reply_streak["room-busy"] == every - 1
    assert agent._room_reply_streak["room-quiet-2"] == 3


# ── the switches ───────────────────────────────────────────────────────────

def test_the_notices_can_be_turned_off(rooms, sent, monkeypatch):
    real_get = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: False if key == "room_unattended_report_enabled"
        else real_get(key, default)))

    agent = _Agent()
    room, other = _room(rooms, "room-off")
    every = int(real_get("room_unattended_report_every_turns", 20))

    delivered = _drain(agent, room, other, turns=every + 2)
    assert sent == []
    assert all(d is not None for d in delivered), "turning notices off must not stop work"


def test_the_interval_is_read_from_config(rooms, sent, monkeypatch):
    real_get = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: 3 if key == "room_unattended_report_every_turns"
        else real_get(key, default)))

    agent = _Agent()
    room, other = _room(rooms, "room-tight")

    _drain(agent, room, other, turns=7)
    assert len(sent) == 2, "expected notices at turn 3 and 6"


def test_both_keys_are_registered_documented_and_admin_only():
    """Rule 2: DEFAULTS, GLOBAL_CONFIG_KEYS, the schema rows and that file's key count.

    Admin-only because a notice its own subject can silence is not a notice.
    """
    assert Config.DEFAULTS["room_unattended_report_enabled"] is True
    assert Config.DEFAULTS["room_unattended_report_every_turns"] == 20
    assert "room_unattended_report_enabled" in Config.GLOBAL_CONFIG_KEYS
    assert "room_unattended_report_every_turns" in Config.GLOBAL_CONFIG_KEYS

    doc = (ROOT / "docs" / "setup" / "CONFIG_SCHEMA.md").read_text(encoding="utf-8")
    assert "`room_unattended_report_enabled`" in doc
    assert "`room_unattended_report_every_turns`" in doc
    assert f"({len(Config.DEFAULTS)} keys)" in doc
    assert "room_loop_max_turns" not in doc, "the renamed key still haunts the docs"


# ── the agent has to know WHERE it is ──────────────────────────────────────

def _wake_prompt_source() -> str:
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    source = (root / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    return source.split("def collect_room_wake")[1].split("\n    def ")[0]


def test_the_wake_says_this_is_not_the_users_conversation():
    """MUTATION: drop the line, or soften it into a description.

    The turn lands in whatever chat the agent has open - a named boundary, and the one
    a live run walked straight into. Without being told, the agent reads a room message
    as something its user said, answers in the chat where no other agent can see it,
    and the user watches their assistant discuss a topic they never raised. Reported as
    "the agent is still confused", which is exactly what it looks like from outside.
    """
    prompt = _wake_prompt_source()

    # Fragments that fit on ONE source line: the literal is wrapped, so asserting a
    # sentence that spans two lines would fail against correct code.
    assert "YOU ARE IN AN AGENT ROOM RIGHT NOW" in prompt
    # The headline has three truths now, chosen by who actually spoke - the fixed
    # form was measurably false the day the owner typed an instruction into the
    # room. The stranger-case sentence stays, for stranger wakes only.
    assert "Your user is not reading this and did not" in prompt
    assert "FROM YOUR OWN USER" in prompt
    assert "AMONG the messages" in prompt
    assert "if from_user_only" in prompt and "elif from_user" in prompt, (
        "the headline no longer follows who spoke")


def test_the_wake_says_where_an_answer_has_to_go_and_why():
    """MUTATION: keep "reply with room_send if a reply is owed".

    Naming the tool is not the same as saying what happens if it is not used. Text
    written outside a tool call goes to the user's chat, where nobody in the room will
    ever read it - and the agent cannot infer that, because from inside a turn both
    look identical.
    """
    prompt = _wake_prompt_source()

    assert "ANSWER IN THE ROOM WITH room_send" in prompt
    assert "nobody in this room will ever see it" in prompt


def test_the_wake_no_longer_asks_for_a_running_commentary():
    """MUTATION: put "tell your user what happened" back.

    That instruction is why the two conversations blurred: every room message became a
    chat message too, so the user's conversation filled up with agents talking to each
    other. Telling the user is now for when something NEEDS them.
    """
    prompt = _wake_prompt_source()

    assert "tell your user what happened" not in prompt
    assert "when something actually needs them" in prompt
    assert "is not news" in prompt


def test_the_wake_names_the_room_a_human_would_recognise():
    """The room id alone is a hash. The topic is what the user called it, and it is
    what makes the agent's account of what happened recognisable to them."""
    prompt = _wake_prompt_source()

    assert 'room.manifest.get("topic")' in prompt
    assert "Room: " in prompt
