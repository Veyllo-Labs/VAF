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


def _method_code(source: str, name: str) -> str:
    """The statements of an Agent method, its docstring left out: a docstring that names a
    marker must not stand in for the code that reads it."""
    found = [n for n in ast.walk(ast.parse(source))
             if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(found) == 1, f"{name} is defined {len(found)} times"
    body = found[0].body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    return "\n".join(ast.get_source_segment(source, stmt) or "" for stmt in body)


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
    # with the thinking-mode reply pickup; the `if` must read it. That predicate is
    # "a person sent it" (`_turn_is_from_a_person`, where both markers live) and "no
    # allowed set holds": a contact's turn is a person, never the user.
    for test in clearing:
        assert "_turn_is_from_the_user" in test, (
            "the latch is cleared by a condition a timer also satisfies")
    person = _method_code(source, "_turn_is_from_a_person")
    assert "_turn_is_human" in person
    assert "_synthetic_drain_turn" in person
    user = _method_code(source, "_turn_is_from_the_user")
    assert "self._turn_is_from_a_person(" in user
    assert "_allowed_tools" in user, "a contact's turn clears the owner's latch again"


def test_the_queue_boundary_decides_and_excludes_timers_and_automations():
    """MUTATION: compute the flag without the timer or the task-class half.

    Only the queue still knows what the task was. Both halves are load-bearing: the
    class keeps automations out, the metadata keeps timers out, and dropping either one
    puts the leak back.
    """
    import inspect

    import vaf.core.headless_runner as hr

    # The decision lives in the queue task's turn binding, the one producer the lane reads.
    source = inspect.getsource(hr._task_turn_binding).replace("\r\n", "\n")
    # Up to the line that closes the bool(...) call, not to the first ")" - the
    # expression has nested calls of its own.
    assert "_turn_is_human = bool(\n" in source and "\n    )\n" in source
    decision = source.split("_turn_is_human = bool(")[1].split("\n    )")[0]

    assert 'task_class", "") == "interactive"' in decision
    # The metadata half: every wake kind (a timer, a finished background command) is kept
    # out, read once from the metadata right above the decision.
    assert "not _wake" in decision
    assert "_wake = _wake_kind(task_meta_for_env)" in source.split("_turn_is_human = bool(")[0][-300:]
    from vaf.core.task_queue import wake_kind
    assert wake_kind({"timer": True}) == "timer" and wake_kind({"wake": "process"}) == "process"
    assert "task.input_text" in decision


def test_the_flag_is_cleared_on_every_exit_path():
    """MUTATION: drop the `_turn_is_human` clear from the turn binding's marker writer, or
    the `end_turn` from the runner loop's finally.

    A flag left standing colours the NEXT task, which is the env-hygiene failure this
    repo already has a rule about: one automation would silently mark every following
    turn as non-human, and the latch would then never clear at all. The task's binding
    sets it, `end_turn` takes it away again (absent: a person typing, the engine's
    default), and the loop's per-iteration finally runs `end_turn` on every exit path.
    """
    import inspect

    import vaf.core.headless_runner as hr
    from vaf.core.turn_binding import end_turn

    assert "is_human=_turn_is_human" in inspect.getsource(hr._task_turn_binding)

    class _Agent:
        def set_allowed_tools(self, names):
            self._allowed_tools = names

    stub = _Agent()
    stub._turn_is_human = False
    end_turn(stub)
    assert not hasattr(stub, "_turn_is_human"), "end_turn leaves the flag standing"

    loop = ast.parse(inspect.getsource(hr.run_headless_agent).replace("\r\n", "\n"))
    whiles = [n for n in ast.walk(loop) if isinstance(n, ast.While)]
    outer = [n.body[0] for n in whiles if n.body and isinstance(n.body[0], ast.Try)]
    assert any("end_turn" in ast.dump(ast.Module(body=t.finalbody, type_ignores=[]))
               for t in outer), "the loop does not end every iteration's turn"


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
    assert "is_human=_turn_is_human" in runner
