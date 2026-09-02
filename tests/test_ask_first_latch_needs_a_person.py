# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Only a person can answer a question the agent asked its person.

The ask-first latch means "the agent asked its user something and is waiting", and while
it is set, background turns may not start write actions. A timer and an automation both
reach chat_step looking exactly like a user turn - the user's own text, no background
marker - so a scheduled job firing ten minutes later used to open a gate that promised
to stay shut until the user replied.

Nothing about how timers and automations RUN changes here. The only change is that they
no longer count as the answer.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class _Tool:
    def __init__(self, permission_level="write"):
        self.permission_level = permission_level


class _Agent:
    from vaf.core.agent import Agent as _Real

    _DELEGATION_TOOLS = _Real._DELEGATION_TOOLS
    _ROOM_TALK_TOOLS = _Real._ROOM_TALK_TOOLS
    _ask_first_gate_decision = _Real._ask_first_gate_decision

    def __init__(self, *, synthetic, pending=True):
        self._synthetic_drain_turn = synthetic
        self._pending_user_question = {"preview": "shall I delete it?"} if pending else None


# ── an automation is not blanket-blocked ───────────────────────────────────

def test_an_automation_turn_is_not_blocked_by_the_latch():
    """MUTATION: drop the _synthetic_drain_turn check from the gate.

    The gate exists for BACKGROUND drain turns that would act on a stale intention. An
    automation runs its own turn on its own schedule and must keep working exactly as
    before - the change in this commit is only that it no longer counts as the user's
    answer. A gate that blocked automations too would have turned a leak into an outage.
    """
    automation = _Agent(synthetic=False, pending=True)

    assert automation._ask_first_gate_decision("write_file", _Tool("write")) is None
    assert automation._ask_first_gate_decision("coding_agent", _Tool("read")) is None


def test_a_drain_turn_is_still_blocked_while_a_question_is_open():
    drain = _Agent(synthetic=True, pending=True)
    assert drain._ask_first_gate_decision("write_file", _Tool("write")) is not None


def test_a_drain_turn_is_free_once_no_question_is_open():
    drain = _Agent(synthetic=True, pending=False)
    assert drain._ask_first_gate_decision("write_file", _Tool("write")) is None


# ── a timer no longer closes the question ──────────────────────────────────

def test_the_latch_is_cleared_only_for_a_turn_a_person_sent():
    """MUTATION: put the clearing back on the old condition.

    The old line was `user_input and not skip_input and not _synthetic_drain_turn`, and
    a timer satisfies all three: it enqueues an ordinary task carrying the user's own
    text with no marker of its own. Ten minutes after the agent asked "shall I delete
    it?", an unrelated reminder answered on the user's behalf.
    """
    source = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    clearing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
        if "_pending_user_question" in body and "Constant(value=None)" in body:
            clearing.append(ast.dump(node.test))

    assert clearing, "nothing clears the latch any more"
    # The condition lives in ONE predicate now (`_turn_is_from_the_user`), shared
    # with the thinking-mode reply pickup; the `if` must read it, and the
    # predicate must still carry both markers.
    for test in clearing:
        assert "_turn_is_from_the_user" in test, (
            "the latch is cleared by a condition a timer also satisfies")
    predicate = source.split("def _turn_is_from_the_user(")[1].split("\n    def ")[0]
    assert "_turn_is_human" in predicate
    assert "_synthetic_drain_turn" in predicate


def test_the_queue_boundary_decides_and_excludes_timers_and_automations():
    """MUTATION: compute the flag without the timer or the task-class half.

    Only the queue still knows what the task was. Both halves are load-bearing: the
    class keeps automations out, the metadata keeps timers out, and dropping either one
    puts the leak back.
    """
    source = (ROOT / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")
    # Up to the line that closes the bool(...) call, not to the first ")" - the
    # expression has nested calls of its own.
    decision = source.split("_turn_is_human = bool(")[1].split("\n                    )")[0]

    assert 'task_class", "") == "interactive"' in decision
    assert 'get("timer")' in decision
    assert "task.input_text" in decision


def test_the_flag_is_restored_on_every_exit_path():
    """MUTATION: set the flag without restoring it.

    A flag left standing colours the NEXT task, which is the env-hygiene failure this
    repo already has a rule about: one automation would silently mark every following
    turn as non-human, and the latch would then never clear at all.
    """
    source = (ROOT / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    restored = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and node.finalbody:
            body = ast.dump(ast.Module(body=node.finalbody, type_ignores=[]))
            if "_turn_is_human" in body and "_prev_turn_is_human" in body:
                restored = True
    assert restored, "_turn_is_human is not restored from a finally block"


# ── the lanes without a queue must not change ──────────────────────────────

def test_a_lane_with_no_queue_still_clears_the_latch():
    """The terminal and an embedder call chat_step directly, and there a message IS a
    person typing. The flag is absent there, and absence must keep the old behaviour -
    otherwise this fix would leave `vaf run` unable to ever answer its own agent.

    MUTATION: default the flag to False. Everything outside the runner would go mute.
    """
    source = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    assert 'getattr(self, "_turn_is_human", True)' in source, (
        "the no-queue lanes no longer default to 'a person typed this'")

    runner = (ROOT / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")
    assert "agent._turn_is_human = _turn_is_human" in runner
