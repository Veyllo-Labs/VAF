# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Only the user, in their own chat, answers a background question - and the
question is in that chat's transcript, not only in a latch.

Live incident 2026-09-02. A background pass asked the user on Telegram whether it
should prepare drafts every evening. Before the user answered, an A2A room wake
ran through chat_step on the same scope: the ROOM PROMPT was recorded as the
user's reply to that question and the waiting latch was cleared. An hour later
the user answered "Nein bitte nicht aber coole Idee" on Telegram, and the agent
that received it had no trace of the question anywhere - the latch was gone and
the question had never been written into the Telegram session (delivery opted
out of recording on the theory that the latch would reconstruct it). The agent
asked the user what they were declining.

Two properties are pinned:
- `_turn_is_from_the_user` is the one test both "the user replied" latches read,
  and a drain turn, a non-human queue turn and a background run's own prompt
  all fail it;
- a background question and its nudge are recorded in the channel session with
  their kind, so the transcript carries the question even when the latch is gone.
"""
import ast
import re
from pathlib import Path
from types import SimpleNamespace

from vaf.core.agent import Agent

ROOT = Path(__file__).resolve().parents[1]


def _agent(**attrs) -> SimpleNamespace:
    fake = SimpleNamespace(**attrs)
    fake._BACKGROUND_RUN_KINDS = Agent._BACKGROUND_RUN_KINDS
    fake._turn_is_from_the_user = Agent._turn_is_from_the_user.__get__(fake)
    return fake


# ── the predicate ──────────────────────────────────────────────────────────

def test_a_person_typing_with_no_marker_is_the_user():
    """The terminal and an embedder set no marker at all; absence means a person."""
    assert _agent()._turn_is_from_the_user("Nein bitte nicht aber coole Idee") is True


def test_a_human_queue_turn_is_the_user():
    assert _agent(_turn_is_human=True, _synthetic_drain_turn=False,
                  _run_kind="chat")._turn_is_from_the_user("ja") is True


def test_a_room_wake_is_not_the_user():
    """MUTATION: drop the `_synthetic_drain_turn` clause. The incident's exact shape:
    the runner marks a room wake as a drain turn and hands the room prompt to
    chat_step as user_input."""
    room = _agent(_synthetic_drain_turn=True, _run_kind="chat")
    assert room._turn_is_from_the_user("YOU ARE IN AN AGENT ROOM RIGHT NOW. ...") is False


def test_a_turn_the_queue_knows_was_not_a_person_is_not_the_user():
    """MUTATION: drop the `_turn_is_human` clause. A timer and an automation arrive
    through the queue with the user's own text and no other marker."""
    timer = _agent(_turn_is_human=False, _run_kind="chat")
    assert timer._turn_is_from_the_user("remind me about the invoice") is False


def test_a_background_runs_own_prompt_is_not_the_user():
    """MUTATION: drop the run-kind clause. The waiting latch is scope-keyed on disk
    and shared across Agent instances, so a thinking or automation run's own
    prompt would consume the answer slot of the question it (or its sibling) asked."""
    for kind in ("thinking", "automation"):
        run = _agent(_run_kind=kind)
        assert run._turn_is_from_the_user("You are running a background check ...") is False, kind


def test_no_input_or_a_skipped_input_is_never_the_user():
    assert _agent()._turn_is_from_the_user("") is False
    assert _agent()._turn_is_from_the_user(None) is False
    assert _agent()._turn_is_from_the_user("ja", skip_input=True) is False


# ── the wiring: both latches read the predicate ────────────────────────────

def _chat_step_source() -> str:
    return (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")


def test_both_latches_are_gated_by_the_predicate():
    """MUTATION: put either `if` back on `user_input and not skip_input`.

    The ask-first latch already had the drain and human clauses inline; the
    thinking-mode pickup three lines below it had neither, which is how a room
    wake filed a room prompt as the user's reply. Both now read the one predicate,
    so the next lane added to one is added to the other.
    """
    tree = ast.parse(_chat_step_source())
    gated = {"ask_first": False, "thinking_pickup": False}
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
        test = ast.dump(node.test)
        if "_pending_user_question" in body and "Constant(value=None)" in body:
            gated["ask_first"] = "_turn_is_from_the_user" in test
        if "get_waiting_for_reply" in body and "clear_waiting_for_reply" in body:
            gated["thinking_pickup"] = "_turn_is_from_the_user" in test
    assert gated["ask_first"], "the ask-first latch no longer reads _turn_is_from_the_user"
    assert gated["thinking_pickup"], (
        "the thinking-mode reply pickup no longer reads _turn_is_from_the_user - "
        "a room wake or an automation run consumes the user's answer slot again")


def test_the_predicate_keeps_every_marker():
    """MUTATION: drop any one clause from the predicate body. The three markers
    are three incidents; the source must read all of them."""
    src = _chat_step_source()
    body = src.split("def _turn_is_from_the_user(")[1].split("\n    def ")[0]
    assert "_synthetic_drain_turn" in body
    assert 'getattr(self, "_turn_is_human", True)' in body, "the no-queue lanes must default to a person"
    assert "_BACKGROUND_RUN_KINDS" in body
    assert Agent._BACKGROUND_RUN_KINDS == ("thinking", "automation")


# ── the record: the question lives in the channel transcript ───────────────

def test_thinking_callers_record_their_messages_with_a_kind():
    """MUTATION: put `record=False` back on either call, or drop the kind.

    The latch is one scope-keyed slot any turn on the scope can consume; the
    transcript is the record. The question carries kind="thinking" and the nudge
    kind="nudge" - the same tags the Web UI path persists - so the chat renders
    them as the proactive bubbles they were.
    """
    import vaf.core.thinking_mode as tm
    src = Path(tm.__file__).read_text(encoding="utf-8")
    calls = re.findall(r"send_to_main_messenger\([^)]*\)", src)
    assert calls, "thinking-mode no longer calls send_to_main_messenger?"
    assert not [c for c in calls if "record=False" in c], (
        "a thinking-mode send opts out of recording again - the question would live "
        "only in the latch")
    kinds = sorted(re.findall(r'kind="([a-z]+)"', " ".join(calls)))
    assert kinds == ["nudge", "thinking"], kinds


def test_the_dead_history_sync_lane_is_gone():
    """The loop used to 'persist the question to the main session' by scanning the
    run history for send_telegram/send_whatsapp/send_discord calls - tools that
    are stripped before every run. The docs promised that persistence for months
    while nothing could ever fire it. The record now happens at delivery, through
    the background-append primitive; the scanner must not come back."""
    import vaf.core.thinking_mode as tm
    src = Path(tm.__file__).read_text(encoding="utf-8")
    assert "_detect_and_set_waiting_for_reply" not in src
    assert "_try_emit_to_web_ui_and_wait" not in src
    assert "_waiting_already_set" not in src
