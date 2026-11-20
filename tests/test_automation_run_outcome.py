# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An automation run's outcome is decided once, and a failed or timed-out run is never done.

MEASURED BEFORE THE FIX (the real `run_task`, stub agent and stub workflow engine): a workflow
whose engine returned `success=False` was stamped, logged and pushed as `success`; a prompt run
past its time limit and grace was stamped; a turn whose backend failed was delivered as
`(success, "Completed")` when nothing streamed and as a success with the raw failure when it did;
a turn that answered twice delivered only its last answer (the joined stream is cleaned as one
text); a turn that raised was run a second time without a time limit; and `vaf automation run`
closed its window as a success whatever happened.

Every row drives the real `AutomationManager.run_task` against a scratch store. The MUTATIONS
that turn each test red are named in its docstring.
"""
import ast
import inspect
import json
import os
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.core import automation
from vaf.core.agent import Agent
from vaf.core.automation import (AutomationManager, AutomationTask, RunOutcome, _prompt_outcome,
                                 load_run_log)
from vaf.core.session import TurnRound, TurnStatus

TID = "0c0e0001"
FAILED_TURN = TurnStatus(failed=True)
ANSWERED_TWICE = TurnStatus(rounds=(TurnRound(1, (), True), TurnRound(2, (), True)))
TOOL_ROUND = TurnStatus(rounds=(TurnRound(1, ("call-1",), False),))

# What the person reads, written out here and not taken from the module, so the wording is pinned.
NEXT = {"daily": "Die Automation läuft zum nächsten Termin wieder.",
        "once": "Diese einmalige Automation ist damit verbraucht. Lege sie bei Bedarf neu an.",
        "on_event": "Die Automation läuft beim nächsten passenden Ereignis wieder."}
NO_ANSWER = "Das Modell hat keine Antwort geliefert. Prüfe das Modell und seine Verbindung."


def _failed(reason, frequency="daily"):
    head = f"Der Lauf ist fehlgeschlagen: {reason}" if reason else NO_ANSWER
    return f"{head}\n\n{NEXT[frequency]}"


# ── the scratch world ────────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    """A scratch store and output folder; deliveries and workspace syncs are recorded."""
    from vaf.core.lock_manager import LockManager
    from vaf.core.platform import Platform

    real_lock = (LockManager.__dict__["acquire"], LockManager.__dict__["release"])
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path / "vaf"))
    monkeypatch.setattr(automation, "bind_identity", lambda *a, **k: None)
    monkeypatch.setattr(automation, "resolve_scope_identity", lambda *a, **k: None)
    monkeypatch.setattr(automation, "_resolve_username", lambda scope: "alice")
    monkeypatch.setattr("vaf.memory.rag.turn_memory_context", lambda *a, **k: "")
    monkeypatch.setattr(LockManager, "acquire", lambda lock_id: True)
    monkeypatch.setattr(LockManager, "release", lambda lock_id: None)
    pushed, synced = [], []
    monkeypatch.setattr(automation, "_push_result_to_web_ui",
                        lambda task, status, summary, **k: pushed.append((status, summary, k)))
    monkeypatch.setattr(AutomationManager, "_sync_workspace_automation_state",
                        lambda self, task, run_status="", summary="", event="":
                        event == "automation_run" and synced.append(run_status))
    out = tmp_path / "out"
    out.mkdir()
    return SimpleNamespace(store=tmp_path / "tasks", out=out, pushed=pushed, synced=synced,
                           monkeypatch=monkeypatch, real_lock=real_lock, tmp=tmp_path)


def _task(env, frequency="daily", **fields):
    """A root task on disk (no scope, so no account lookup), with an output folder."""
    AutomationManager(storage_dir=str(env.store)).create(AutomationTask(
        id=TID, name="report", prompt="Write the report.", frequency=frequency, time="06:00",
        output_path=str(env.out), output_format="markdown", **fields))
    manager = AutomationManager(storage_dir=str(env.store))
    return manager, manager.get(TID)


def _record(env):
    path = env.store / f"{TID}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _log(env):
    return [r["status"] for r in load_run_log(env.store / f"{TID}.json")]


def _pushed(env):
    return [(status, text) for status, text, _k in env.pushed]


def _assert_not_done(env, frequency):
    record = _record(env)
    if frequency == "once":
        assert record is None, "a one-time task outlived its run"
        return
    assert record is not None
    assert record.get("last_run") is None, "a run that did not complete was stamped"
    assert record.get("last_completed_local_date") is None
    assert _log(env) == ["error"]


def _prompt_agent(env, *, returns=None, chunks=(), turn=TurnStatus(), raises=None, during=None):
    """Installs a stub `Agent` for the prompt lane; returns its call record."""
    import vaf.core.agent as agent_mod
    seen = {"calls": 0, "shutdowns": 0}

    class _Agent:
        tools = {}
        _current_username = "alice"
        _tool_authorizer = None
        _clean_reasoning = Agent._clean_reasoning
        _sanitize_response = Agent._sanitize_response

        def __init__(self, *a, **k):
            self.history = []
            self._turn = TurnStatus()

        @property
        def last_turn(self):
            return self._turn

        def load_model(self):
            pass

        def init_chat(self):
            self.history = [{"role": "system", "content": "system"}]

        def chat_step(self, prompt, stream_callback=None, **k):
            seen["calls"] += 1
            if during is not None:
                during()
            self.history.append({"role": "user", "content": prompt})
            for chunk in chunks:
                if stream_callback:
                    stream_callback(chunk)
            if raises is not None:
                raise raises
            self._turn = turn
            return returns

        def shutdown(self):
            seen["shutdowns"] += 1

    env.monkeypatch.setattr(agent_mod, "Agent", _Agent)
    return seen


def _workflow(env, **result):
    """Installs a stub `Agent` and `WorkflowEngine` for the workflow lane."""
    import vaf.core.agent as agent_mod
    import vaf.workflows.engine as engine_mod

    class _Agent:
        tools = {}
        _current_username = "alice"
        _tool_authorizer = None
        history = []

        def __init__(self, *a, **k):
            pass

        def load_model(self):
            pass

        def init_chat(self):
            pass

        def shutdown(self):
            pass

    class _Engine:
        def __init__(self, *a, callback=None, **k):
            self.callback = callback

        def execute(self, steps, variables=None, check_stop=None, **k):
            step = steps[0]
            step.result, step.error = "Message sent to the user via Telegram.", None
            self.callback("start", step, 1, len(steps))
            self.callback("success" if result.get("success") else "error", step, 1, len(steps))
            values = dict(success=False, paused=False, final_output="", error=None)
            values.update(result)
            return SimpleNamespace(**values)

    env.monkeypatch.setattr(agent_mod, "Agent", _Agent)
    env.monkeypatch.setattr(engine_mod, "WorkflowEngine", _Engine)


STEPS = [{"tool": "send_to_user", "args": {"message": "hi"}}]


# ── AC1: a failed workflow is not done ───────────────────────────────────────

@pytest.mark.parametrize("frequency", ["daily", "once"])
def test_failed_workflow_is_not_done(env, frequency):
    """MUTATIONS: `completed` true for `failed`; the unconditional stamp back; the workflow
    failure mapped to `ok`."""
    _workflow(env, success=False, error="write refused")
    manager, task = _task(env, frequency, workflow_steps=STEPS)
    out = manager.run_task(task, new_terminal=False)
    assert out.startswith("# Workflow Failed") and "write refused" in out
    assert _pushed(env) == [("error", f"{out.rstrip()}\n\n{NEXT[frequency]}")]
    assert env.synced == ["error"]
    assert list(env.out.iterdir()) == [], "a failed workflow's report became an output file"
    _assert_not_done(env, frequency)


def test_successful_workflow_is_done(env):
    """The success path now runs through the common tail: stamped, logged and pushed once,
    with its short summary, and its send step still suppresses the second messenger push."""
    _workflow(env, success=True)
    manager, task = _task(env, workflow_steps=STEPS)
    out = manager.run_task(task, new_terminal=False)
    assert out.startswith("# Workflow Execution Report")
    [(status, text, kwargs)] = env.pushed
    assert status == "success" and "ist fertig" in text and "1 Schritte" in text
    assert kwargs["deliver_messenger"] is False and kwargs["output_file"] is None
    assert env.synced == ["success"] and _log(env) == ["success"]
    assert _record(env)["last_run"] and _record(env)["last_completed_local_date"]
    assert list(env.out.iterdir()) == []


# ── AC2: a timed-out prompt run is not done ──────────────────────────────────

def _time_out(env, *, finish_in_grace=False):
    from vaf.core import bounded_run

    def bounded(fn, *, timeout, stop_check=None, poll=0.5, label="tool"):
        if finish_in_grace:
            fn()
        return f"{bounded_run.TIMEOUT_PREFIX} ran past {timeout}s"

    env.monkeypatch.setattr(bounded_run, "run_bounded", bounded)
    if not finish_in_grace:
        env.monkeypatch.setattr(automation, "_wait_for_abandoned_run", lambda *a, **k: False)


@pytest.mark.parametrize("frequency", ["daily", "once"])
def test_timed_out_prompt_is_not_done(env, frequency):
    """MUTATIONS: stamp a `timed_out` run; the old note back."""
    _prompt_agent(env, returns="never seen", chunks=["Half a sent"])
    _time_out(env)
    manager, task = _task(env, frequency)
    out = manager.run_task(task, new_terminal=False)
    [(status, text)] = _pushed(env)
    assert status == "error" and text == out
    assert text == (
        "Zeitlimit überschritten: Die Automation 'report' war nach 600 s Zeitlimit und 300 s "
        "Nachfrist noch nicht fertig. Es gibt kein Teilergebnis. Der Lauf kann im Hintergrund "
        "noch weiterarbeiten, aber was er dann noch tut, wird nicht gemeldet. Kürze den Auftrag "
        "oder erhöhe das Zeitlimit (Einstellung automation_run_timeout_seconds, nur für Admins)."
        f"\n\n{NEXT[frequency]}")
    assert "meldet er sich selbst" not in text and not text.startswith("Error:")
    assert "Half a sent" not in text, "a partial stream was delivered"
    assert list(env.out.iterdir()) == []
    _assert_not_done(env, frequency)


def test_a_turn_finished_in_the_grace_is_judged_like_any_turn(env):
    """A run that finished within the grace window is no timeout, and its turn decides: a
    failed one is failed, not a completion."""
    _prompt_agent(env, returns="[Error] backend down", turn=FAILED_TURN)
    _time_out(env, finish_in_grace=True)
    manager, task = _task(env)
    manager.run_task(task, new_terminal=False)
    assert _pushed(env) == [("error", _failed("[Error] backend down"))]
    _assert_not_done(env, "daily")


# ── AC3 / AC4: a failed turn is failed ───────────────────────────────────────

API_ERROR = "[API Error from groq: 500]"


@pytest.mark.parametrize("returns, chunks, shown", [
    pytest.param("[Error] backend down", [], _failed("[Error] backend down"), id="unstreamed"),
    pytest.param(API_ERROR, [API_ERROR], _failed(API_ERROR), id="streamed"),
    pytest.param("", [], _failed(""), id="empty"),
])
def test_failed_turn_is_failed(env, returns, chunks, shown):
    """MUTATION: drop the `returned_turn_copy` meta check, judging from the stream again."""
    turn = TurnStatus(failed=True, failure=API_ERROR if chunks else "")
    _prompt_agent(env, returns=returns, chunks=chunks, turn=turn)
    manager, task = _task(env)
    manager.run_task(task, new_terminal=False)
    assert _pushed(env) == [("error", shown)]
    assert env.synced == ["error"]
    assert list(env.out.iterdir()) == []
    _assert_not_done(env, "daily")


def test_system_log_only_is_failed(env):
    """The engine's notice after its empty retries ran out is no answer.
    MUTATION: judge `turn.failed` alone."""
    _prompt_agent(env, returns="[SYSTEM_LOG_ONLY]API returned empty response after retries.")
    manager, task = _task(env)
    manager.run_task(task, new_terminal=False)
    assert _pushed(env) == [("error", _failed("API returned empty response after retries."))]
    _assert_not_done(env, "daily")


# ── AC5 / AC6: what an answered turn and an unanswered turn deliver ──────────

@pytest.mark.parametrize("returns, chunks, turn, shown", [
    pytest.param("Antwort eins.\n\nAntwort zwei.",
                 ["<think>a</think>\n\nAntwort eins.", "<think>b</think>\n\nAntwort zwei."],
                 ANSWERED_TWICE, "Antwort eins.\n\nAntwort zwei.", id="two-answers"),
    pytest.param("[ASYNC_ACK] Ich habe einen Helfer gestartet.", ["Moment, ich schaue nach."],
                 TOOL_ROUND, "Ich habe einen Helfer gestartet.", id="async-ack"),
])
def test_answered_turn_delivers_its_return_value(env, returns, chunks, turn, shown):
    """MUTATIONS: back to the cleaned joined stream for an answered turn (the first answer is
    lost); drop the `ASYNC_ACK` clause (the narration is delivered instead)."""
    _prompt_agent(env, returns=returns, chunks=chunks, turn=turn)
    manager, task = _task(env)
    out = manager.run_task(task, new_terminal=False)
    assert out == shown and _pushed(env) == [("success", shown)]
    assert _record(env)["last_run"], "an answered run was not stamped"
    [written] = list(env.out.iterdir())
    assert written.read_text(encoding="utf-8").endswith(shown)


def test_answerless_turn_keeps_the_stream_path(env):
    """A turn closed by a tool returns a status line; what it streamed is the result, as before.
    MUTATION: deliver the return value for an unanswered turn ("..." is pushed)."""
    _prompt_agent(env, returns="...", chunks=["Bericht gespeichert."], turn=TOOL_ROUND)
    manager, task = _task(env)
    manager.run_task(task, new_terminal=False)
    assert _pushed(env) == [("success", "Bericht gespeichert.")]


# ── AC7 / AC8 ────────────────────────────────────────────────────────────────

def test_a_raising_turn_runs_once(env):
    """MUTATION: call `chat_step` again in the fallback after an exception."""
    seen = _prompt_agent(env, raises=RuntimeError("provider exploded"))
    manager, task = _task(env)
    out = manager.run_task(task, new_terminal=False)
    assert seen["calls"] == 1, "a failed turn was started a second time"
    assert out == _failed("Error: provider exploded")
    assert _pushed(env) == [("error", _failed("Error: provider exploded"))]
    assert seen["shutdowns"] >= 1, "the agent of a failed turn was not shut down"
    _assert_not_done(env, "daily")


def test_an_answer_starting_with_error_is_ok(env):
    """The text no longer carries the status. MUTATION: the `startswith("Error:")` status back."""
    answer = "Error: 3 von 5 Tests sind rot, die Details stehen unten."
    _prompt_agent(env, returns=answer, chunks=[answer],
                  turn=TurnStatus(rounds=(TurnRound(1, (), True),)))
    manager, task = _task(env)
    manager.run_task(task, new_terminal=False)
    assert _pushed(env) == [("success", answer)]
    assert _log(env) == ["success"] and env.synced == ["success"]
    assert _record(env)["last_run"]


# ── AC9: a paused workflow ───────────────────────────────────────────────────

@pytest.mark.parametrize("frequency", ["daily", "once"])
def test_paused_workflow_counts_and_writes_no_file(env, frequency):
    """MUTATION: the file gate reads `completed` instead of `status == "ok"`."""
    _workflow(env, success=False, paused=True, waiting_for_agent="librarian_agent",
              waiting_for_task="t-1")
    manager, task = _task(env, frequency, workflow_steps=STEPS)
    out = manager.run_task(task, new_terminal=False)
    assert out.startswith("# Workflow Still Running")
    assert _pushed(env) == [("success", out)]
    assert list(env.out.iterdir()) == [], "a paused workflow's report became an output file"
    if frequency == "once":
        assert _record(env) is None
    else:
        assert _record(env)["last_run"] and _log(env) == ["success"]


# ── AC10: the room-trigger lane ──────────────────────────────────────────────

def test_room_triggered_failed_turn_is_not_done(env):
    """A room-triggered prompt run is the same lane with a trigger. MUTATION: as for AC3."""
    import vaf.core.turn_binding as turn_binding
    bound = []
    env.monkeypatch.setattr(automation, "_room_trigger_binding", lambda task, trigger: "binding")
    env.monkeypatch.setattr(turn_binding, "bind_turn", lambda agent, b: bound.append(("bind", b)))
    env.monkeypatch.setattr(turn_binding, "end_turn", lambda agent: bound.append(("end",)))
    seen = _prompt_agent(env, returns="[Error] backend down", turn=FAILED_TURN)
    trigger = {"kind": "room_message", "room_id": "room-t"}
    manager, task = _task(env, "on_event", trigger=dict(trigger))
    stored_trigger = _record(env)["trigger"]
    manager.run_task(task, new_terminal=False,
                     trigger={"room_id": "room-t", "frames": [], "text": "deploy it"})
    assert seen["calls"] == 1, "the frames of a failed run were run again"
    assert bound == [("bind", "binding"), ("end",)]
    assert _pushed(env) == [("error", _failed("[Error] backend down", "on_event"))]
    record = _record(env)
    assert record["last_run"] is None and record["trigger"] == stored_trigger
    assert _log(env) == ["error"]


# ── AC11: `vaf automation run` ───────────────────────────────────────────────

@pytest.mark.parametrize("status, success", [
    ("failed", False), ("timed_out", False), ("ok", True), ("paused", True), (None, False),
])
def test_cli_run_holds_the_window_on_failure(monkeypatch, status, success):
    """The window is held with "the output above is the log", so the reason is printed into
    it, as written. MUTATIONS: `ok = True` whatever the outcome; never call `on_outcome`; drop
    the reason print. `None` is a run that ended without an outcome (skipped, locked, revoked),
    whose reason is what run_task answered."""
    from typer.testing import CliRunner
    import vaf.cli.autoclose as autoclose

    task = SimpleNamespace(id=TID, name="report")
    reason = f"[i]reason of {status}[/i]: Der Lauf ist fehlgeschlagen"
    answered = "[LOCK] Automation 'report' (0c0e0001) is already running. Skipping."

    def run_task(t, callback=None, new_terminal=True, trigger=None, *, on_outcome=None):
        assert new_terminal is False
        if status is not None and on_outcome is not None:
            on_outcome(RunOutcome(status, reason))
        return answered if status is None else reason

    finished = []
    monkeypatch.setattr(automation, "get_manager",
                        lambda: SimpleNamespace(get=lambda tid: task, run_task=run_task))
    monkeypatch.setattr(autoclose, "finish_terminal", lambda **k: finished.append(k))
    result = CliRunner().invoke(automation.automation_app, ["run", TID])
    assert result.exception is None, result.output
    assert finished == [{"success": success}]
    shown = answered if status is None else reason
    if success:
        assert shown not in result.output, "a completed run printed a failure reason"
    else:
        assert shown in result.output, result.output


def test_cli_run_is_no_success_when_access_was_taken_away_after_the_outcome(monkeypatch):
    """The outcome was decided `ok`, then access was taken away and the run delivered nothing:
    the window is held with the revoked note. MUTATION: drop the revocation check in the CLI."""
    from typer.testing import CliRunner
    import vaf.cli.autoclose as autoclose
    import vaf.core.revocation as revocation

    revoked = {"now": False}
    monkeypatch.setattr(revocation, "is_revoked", lambda scope: revoked["now"])
    task = SimpleNamespace(id=TID, name="report", user_scope_id="ab12cd34-0000-4000-8000-0000000000b1")
    note = "[REVOKED] Automation 'report' (0c0e0001) stopped: nothing is delivered."

    def run_task(t, callback=None, new_terminal=True, trigger=None, *, on_outcome=None):
        on_outcome(RunOutcome("ok", "Fertig."))
        revoked["now"] = True
        return note

    finished = []
    monkeypatch.setattr(automation, "get_manager",
                        lambda: SimpleNamespace(get=lambda tid: task, run_task=run_task))
    monkeypatch.setattr(autoclose, "finish_terminal", lambda **k: finished.append(k))
    result = CliRunner().invoke(automation.automation_app, ["run", TID])
    assert result.exception is None, result.output
    assert finished == [{"success": False}]
    assert note in result.output and "Fertig." not in result.output.split("Running", 1)[1]


def test_run_task_hands_its_outcome_to_the_caller_once(env):
    """The real run_task calls `on_outcome` once with the decided outcome, after the run log, and
    a callback that raises does not cost the delivery; a run that never started has none.
    MUTATION: drop the `on_outcome(outcome)` call."""
    from vaf.core.lock_manager import LockManager
    _prompt_agent(env, returns="[Error] backend down", turn=FAILED_TURN)
    manager, task = _task(env)
    seen = []

    def hook(outcome):
        seen.append((outcome.status, outcome.text, _log(env)))
        raise RuntimeError("the caller's own bug")

    manager.run_task(task, new_terminal=False, on_outcome=hook)
    assert seen == [("failed", _failed("[Error] backend down"), ["error"])]
    assert _pushed(env) == [("error", _failed("[Error] backend down"))]

    env.monkeypatch.setattr(LockManager, "acquire", lambda lock_id: False)
    later = []
    out = manager.run_task(task, new_terminal=False, on_outcome=later.append)
    assert out.startswith("[LOCK]") and later == []


# ── what a delegation acknowledgement delivers ───────────────────────────────

@pytest.mark.parametrize("returns, shown", [
    pytest.param("[ASYNC_ACK]<think>Translate: secret plan</think>Le helper travaille [Task: t1].",
                 "Le helper travaille [Task: t1].", id="closed-think"),
    pytest.param("[ASYNC_ACK]<think>secret plan, never closed", "Completed", id="unclosed-think"),
    pytest.param('[ASYNC_ACK]Le helper travaille. {"name": "web_search", "arguments": '
                 '{"query": "secret plan"}}', "Le helper travaille.", id="leaked-tool-call"),
])
def test_a_delegation_acknowledgement_is_cleaned(env, returns, shown):
    """The acknowledgement of a delegation can be the model's own translation, from a call that
    keeps its marked reasoning; it reaches chat, notification and messenger only cleaned.
    MUTATION: hand `_prompt_outcome` no cleaner."""
    _prompt_agent(env, returns=returns, turn=TOOL_ROUND)
    manager, task = _task(env)
    manager.run_task(task, new_terminal=False)
    assert _pushed(env) == [("success", shown)]
    assert "secret plan" not in _pushed(env)[0][1]


@pytest.mark.parametrize("answer, turn, status", [
    pytest.param("[ASYNC_ACK]<think>secret plan</think>Le helper travaille.", TOOL_ROUND, "ok",
                 id="ack"),
    pytest.param("Antwort.<think>secret plan</think>", ANSWERED_TWICE, "ok", id="answered"),
    pytest.param("[Error] <think>secret plan</think>", FAILED_TURN, "failed", id="failed"),
])
def test_a_cleaner_that_raises_leaves_no_text(answer, turn, status):
    """Never the uncleaned copy. MUTATION: return the text when the cleaner raises."""
    def boom(text):
        raise RuntimeError("cleaner failed")

    outcome = _prompt_outcome(answer, turn, lambda: "", timed_out=False, frequency="daily",
                              clean=boom)
    assert outcome.status == status and "secret plan" not in outcome.text
    if status == "failed":
        assert outcome.text == _failed("")


# ── a one-time task fires once, whatever its outcome ─────────────────────────

def test_a_failed_one_time_run_is_never_run_again(env):
    """A one-time run that may have acted and then failed is not stamped; when both attempts
    to remove it fail too (a busy store), its mark of consumption, written before it ran, still
    keeps it from being armed or run again at a refresh. MUTATIONS: drop the
    `_mark_once_consumed` call; leave `consumed_at` out of `_once_already_ran`; let `update`
    clear it."""
    from vaf.core.automation import AutomationStoreBusy

    _prompt_agent(env, returns="[Error] backend down", turn=FAILED_TURN)
    manager, task = _task(env, "once")

    def busy(self, *a, **k):
        raise AutomationStoreBusy("automation store is busy")

    env.monkeypatch.setattr(AutomationManager, "_delete", busy)
    manager.run_task(task, new_terminal=False)
    record = _record(env)
    assert record is not None, "the busy store let the removal through"
    assert record["last_run"] is None and record["consumed_at"], record

    later = AutomationManager(storage_dir=str(env.store))         # a refresh or a restart
    assert later.update(TID, consumed_at=None, description="edited") is not None
    again = later.get(TID)
    assert again.consumed_at, "an update cleared the run's mark"
    later._schedule_task(again)
    assert TID not in later._clock_jobs, "a consumed one-time task was armed again"
    assert later._clock_record_verdict(again)[:2] == ("drop", "once already ran")


def test_the_mark_of_consumption_is_coerced_at_the_boundary():
    """Any value a file holds marks the task as consumed (fails closed), a 0 included; none,
    an empty text or False does not. MUTATION: compare by equality (`0 == False`)."""
    for value in ("2026-11-20T06:00:00", 1, True, "x", 0, 0.0, "0", []):
        assert AutomationTask(frequency="once", consumed_at=value).consumed_at
    for value in (None, "", False):
        assert AutomationTask(frequency="once", consumed_at=value).consumed_at is None


def test_the_mark_is_on_disk_before_the_run_executes(env):
    """A run killed mid-turn (a crash, a restart) must already have marked its one-time task.
    MUTATION: write the mark after the run."""
    marks = []
    _prompt_agent(env, returns="Fertig.", turn=ANSWERED_TWICE,
                  during=lambda: marks.append((_record(env) or {}).get("consumed_at")))
    manager, task = _task(env, "once")
    manager.run_task(task, new_terminal=False)
    assert len(marks) == 1 and marks[0], "the turn started on an unmarked one-time task"


def test_the_mark_lands_only_on_the_task_that_fired(env):
    """A task created anew under the id (a retried approval) has not run: the old run's mark
    must not make the scheduler delete it unrun. MUTATION: drop `same_incarnation=True`."""
    manager, old = _task(env, "once")
    assert manager.delete(TID, permanent=True)
    AutomationManager(storage_dir=str(env.store)).create(AutomationTask(
        id=TID, name="report again", prompt="Write it.", frequency="once", time="06:00"))
    assert manager._mark_once_consumed(old) == "refused"
    assert _record(env)["name"] == "report again" and _record(env).get("consumed_at") is None


def test_a_mark_that_cannot_be_written_does_not_stop_the_run(env):
    """MUTATION: let `_mark_once_consumed` raise instead of logging."""
    seen = _prompt_agent(env, returns="Fertig.", turn=ANSWERED_TWICE)
    manager, task = _task(env, "once")

    def broken(self, *a, **k):
        raise OSError("store not writable")

    env.monkeypatch.setattr(AutomationManager, "_mutate_record", broken)
    out = manager.run_task(task, new_terminal=False)
    assert seen["calls"] == 1 and out == "Fertig."
    assert _pushed(env) == [("success", "Fertig.")]


def test_a_run_whose_mark_was_not_written_still_marks_its_own_copy(env):
    """The store refused the mark, the stamp and both removals; the run went (it may have sent)
    and failed. Once the store recovers, the same captured object must not run again: it
    carries the mark in memory. MUTATION: return "unwritten" without marking the copy."""
    from vaf.core.automation import AutomationStoreBusy
    seen = _prompt_agent(env, returns="[Error] backend down", turn=FAILED_TURN)
    manager, task = _task(env, "once")
    broken = {"on": True}
    real_mutate, real_delete = AutomationManager._mutate_record, AutomationManager._delete

    def mutate(self, *a, **k):
        if broken["on"]:
            raise OSError("store not writable")
        return real_mutate(self, *a, **k)

    def delete(self, *a, **k):
        if broken["on"]:
            raise AutomationStoreBusy("automation store is busy")
        return real_delete(self, *a, **k)

    env.monkeypatch.setattr(AutomationManager, "_mutate_record", mutate)
    env.monkeypatch.setattr(AutomationManager, "_delete", delete)
    manager.run_task(task, new_terminal=False)
    assert seen["calls"] == 1 and _record(env)["consumed_at"] is None
    broken["on"] = False                                          # the store recovers
    out = manager.run_task(task, new_terminal=False)
    assert seen["calls"] == 1, "the same one-time run went a second time"
    assert SKIPPED in out


@pytest.mark.parametrize("status", ["unconsumed", "consumed"])
def test_a_manual_run_of_a_consumed_one_time_task_is_refused(env, status):
    """`vaf automation run` loads the record again; one whose removal failed after its run
    carries the mark and must not run a second time. MUTATION: drop the check under the lock."""
    from typer.testing import CliRunner
    import vaf.cli.autoclose as autoclose

    seen = _prompt_agent(env, returns="Fertig.", turn=ANSWERED_TWICE)
    manager, task = _task(env, "once")
    if status == "consumed":
        manager._mark_once_consumed(task)
    finished = []
    env.monkeypatch.setattr(automation, "get_manager",
                            lambda: AutomationManager(storage_dir=str(env.store)))
    env.monkeypatch.setattr(autoclose, "finish_terminal", lambda **k: finished.append(k))
    result = CliRunner().invoke(automation.automation_app, ["run", TID])
    assert result.exception is None, result.output
    if status == "consumed":
        assert seen["calls"] == 0, "a consumed one-time task ran again"
        assert finished == [{"success": False}] and _pushed(env) == []
        assert "this one-time automation has already run" in " ".join(result.output.split())
    else:
        assert seen["calls"] == 1 and finished == [{"success": True}]


def test_a_consumed_one_time_task_is_not_restored(env, monkeypatch):
    """Moved to the trash while its run went, it carries the mark; restored, the scheduler would
    delete it for good while the restore reported it active. MUTATION: drop the check in
    `restore_from_trash`; separately, in the tool."""
    from vaf.tools.automation import RestoreAutomationTool

    manager, task = _task(env, "once")
    manager._mark_once_consumed(task)
    assert manager.delete(TID)                                   # into the trash
    assert manager.restore_from_trash(TID) is False
    assert (manager.trash_dir / f"{TID}.json").exists() and _record(env) is None

    trashed = AutomationTask.from_dict(
        json.loads((manager.trash_dir / f"{TID}.json").read_text(encoding="utf-8")))

    class _Access:                     # the caller's trash: this one task
        def __init__(self, *a, **k):
            pass

        def trash(self):
            return [(manager, trashed)]

    monkeypatch.setattr("vaf.tools.automation.AutomationAccess", _Access)
    answer = RestoreAutomationTool().run(task_id=TID)
    assert answer.startswith("Error:") and "already run" in answer, answer
    assert (manager.trash_dir / f"{TID}.json").exists()


SKIPPED = "this one-time automation has already run"


def test_a_copy_whose_record_was_removed_is_refused(env):
    """A copy loaded before another run of the task finished and removed it cannot tell that
    run apart from none: it fails closed, as the clock does ('task gone').
    MUTATION: answer False for a missing record."""
    seen = _prompt_agent(env, returns="Fertig.", turn=ANSWERED_TWICE)
    manager, copy_ = _task(env, "once")
    assert AutomationManager(storage_dir=str(env.store)).delete(TID, permanent=True)
    out = manager.run_task(copy_, new_terminal=False)
    assert SKIPPED in out and seen["calls"] == 0 and env.pushed == []


def test_a_copy_whose_record_was_marked_after_loading_is_refused(env):
    """MUTATION: decide from the captured copy only."""
    seen = _prompt_agent(env, returns="Fertig.", turn=ANSWERED_TWICE)
    manager, copy_ = _task(env, "once")
    other = AutomationManager(storage_dir=str(env.store))
    assert other._mark_once_consumed(other.get(TID)) == "marked"
    assert copy_.consumed_at is None                      # loaded before the mark
    out = manager.run_task(copy_, new_terminal=False)
    assert SKIPPED in out and seen["calls"] == 0


def test_an_old_copy_never_runs_on_a_task_created_anew(env):
    """The id now names a task created anew: the old copy is refused ('created anew'), the new
    task is neither marked nor removed by it, and the new task's own run still goes.
    MUTATION: answer False for a record of another incarnation."""
    seen = _prompt_agent(env, returns="Fertig.", turn=ANSWERED_TWICE)
    manager, old = _task(env, "once")
    assert manager.delete(TID, permanent=True)
    fresh = AutomationManager(storage_dir=str(env.store)).create(AutomationTask(
        id=TID, name="report again", prompt="Write it.", frequency="once", time="06:00"))
    out = manager.run_task(old, new_terminal=False)
    assert SKIPPED in out and seen["calls"] == 0
    assert _record(env)["name"] == "report again" and _record(env).get("consumed_at") is None
    AutomationManager(storage_dir=str(env.store)).run_task(fresh, new_terminal=False)
    assert seen["calls"] == 1


@pytest.mark.parametrize("since_loading", ["marked", "removed", "created-anew"])
def test_a_fired_one_time_task_spawns_no_terminal(env, since_loading):
    """Only the check before the lane stands between a stale copy and a spawned terminal (the
    mark is asked in the child). The parent asks the record as it is now, for a copy loaded
    before the task was marked, removed or created anew. MUTATIONS: drop the check before the
    terminal branch; decide from the captured copy only; let a missing record or one of
    another incarnation through."""
    from vaf.core.platform import Platform
    spawned = []
    env.monkeypatch.setattr(Platform, "open_new_terminal",
                            staticmethod(lambda *a, **k: spawned.append(a) or True))
    manager, copy_ = _task(env, "once")
    other = AutomationManager(storage_dir=str(env.store))
    if since_loading == "marked":
        assert other._mark_once_consumed(other.get(TID)) == "marked"
    else:
        assert other.delete(TID, permanent=True)
        if since_loading == "created-anew":
            other.create(AutomationTask(id=TID, name="again", prompt="p", frequency="once",
                                        time="06:00"))
    out = manager.run_task(copy_, new_terminal=True)
    assert SKIPPED in out and spawned == []


@pytest.mark.parametrize("where", ["before-the-lane", "at-the-mark"])
def test_a_refused_run_leaves_no_run_lock(env, where):
    """The real run lock, in a scratch folder: a refused run takes it and gives it back.
    MUTATIONS: drop the release in either refusal."""
    from vaf.core.lock_manager import LockManager
    locks = env.tmp / "locks"
    env.monkeypatch.setattr(LockManager, "LOCK_DIR", locks)
    env.monkeypatch.setattr(LockManager, "acquire", env.real_lock[0])
    env.monkeypatch.setattr(LockManager, "release", env.real_lock[1])
    seen = _prompt_agent(env, returns="Fertig.", turn=ANSWERED_TWICE)
    manager, copy_ = _task(env, "once")
    other = AutomationManager(storage_dir=str(env.store))
    assert other._mark_once_consumed(other.get(TID)) == "marked"
    if where == "at-the-mark":                 # the check before the lane saw it unmarked
        env.monkeypatch.setattr(AutomationManager, "_once_spent_on_disk", lambda self, t: False)
    out = manager.run_task(copy_, new_terminal=False)
    assert SKIPPED in out and seen["calls"] == 0
    assert list(locks.glob("*.lock")) == [], "a refused run left its run lock behind"


def test_two_starts_at_the_same_instant_run_once(env):
    """The run lock is check-then-write, so two starts at the same instant can both take it
    (measured); the mark is a compare-and-set and lets exactly one of them run.
    MUTATION: write the mark without asking whether the record has fired."""
    import threading
    seen = _prompt_agent(env, returns="Fertig.", turn=ANSWERED_TWICE)
    _task(env, "once")
    both_checked = threading.Barrier(2, timeout=10)
    real_check = AutomationManager._once_spent_on_disk

    def check_then_wait(self, task):
        answer = real_check(self, task)
        both_checked.wait()                   # both are past the check before either marks
        return answer

    env.monkeypatch.setattr(AutomationManager, "_once_spent_on_disk", check_then_wait)
    copies = [AutomationManager(storage_dir=str(env.store)) for _ in range(2)]
    outs = [None, None]

    def start(i):
        outs[i] = copies[i].run_task(copies[i].get(TID), new_terminal=False)

    threads = [threading.Thread(target=start, args=(i,), daemon=True) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert seen["calls"] == 1, outs
    assert sum(SKIPPED in str(o) for o in outs) == 1, outs


def test_a_trash_copy_marked_while_the_restore_waited_is_not_restored(env):
    """The restore asks the trash copy again under the store lock. MUTATION: decide from the
    copy read before the lock."""
    from contextlib import contextmanager
    manager, task = _task(env, "once")
    assert manager.delete(TID)                                   # into the trash, unmarked
    trash = manager.trash_dir / f"{TID}.json"
    real = manager._store_lock

    @contextmanager
    def marked_while_waiting(directory):
        data = json.loads(trash.read_text(encoding="utf-8"))
        data["consumed_at"] = "2026-11-20T06:00:00"              # another lane ran it meanwhile
        trash.write_text(json.dumps(data), encoding="utf-8")
        with real(directory):
            yield

    env.monkeypatch.setattr(manager, "_store_lock", marked_while_waiting)
    assert manager.restore_from_trash(TID) is False
    assert trash.exists() and _record(env) is None


def test_a_manual_run_of_a_daily_task_that_ran_still_goes(env):
    """Only a one-time task is refused for having fired. MUTATION: mark and check every
    frequency."""
    from typer.testing import CliRunner
    import vaf.cli.autoclose as autoclose
    seen = _prompt_agent(env, returns="Fertig.", turn=ANSWERED_TWICE)
    manager, task = _task(env, "daily")
    assert manager._record_successful_run(task) is not None
    assert _record(env)["last_run"]
    finished = []
    env.monkeypatch.setattr(automation, "get_manager",
                            lambda: AutomationManager(storage_dir=str(env.store)))
    env.monkeypatch.setattr(autoclose, "finish_terminal", lambda **k: finished.append(k))
    result = CliRunner().invoke(automation.automation_app, ["run", TID])
    assert result.exception is None, result.output
    assert seen["calls"] == 1 and finished == [{"success": True}]


# ── access taken away while the caller's callback ran ────────────────────────

def test_access_taken_away_during_the_callback_stops_the_delivery(env):
    """MUTATION: drop the check after `on_outcome`."""
    import vaf.core.revocation as revocation
    revoked = {"now": False}
    env.monkeypatch.setattr(revocation, "is_revoked", lambda scope: revoked["now"])
    _prompt_agent(env, returns="Fertig.", turn=ANSWERED_TWICE)
    manager, task = _task(env)
    out = manager.run_task(task, new_terminal=False,
                           on_outcome=lambda outcome: revoked.update(now=True))
    assert out.startswith("[REVOKED]")
    assert env.pushed == [] and env.synced == []


# ── a turn that raises inside the grace window ───────────────────────────────

def test_a_turn_that_raises_in_the_grace_is_failed(env):
    """The bounded run timed out, and the abandoned turn then raised inside the grace window:
    the run is failed with that exception, not a completion. MUTATIONS: drop
    `_turn_error = _chat_done["error"]`; set `done` only after a turn that returned."""
    from vaf.core import bounded_run

    def bounded(fn, *, timeout, stop_check=None, poll=0.5, label="tool"):
        try:
            fn()
        except Exception:
            pass
        return f"{bounded_run.TIMEOUT_PREFIX} ran past {timeout}s"

    env.monkeypatch.setattr(bounded_run, "run_bounded", bounded)
    env.monkeypatch.setattr(automation, "_wait_for_abandoned_run",
                            lambda chat_done, **k: bool(chat_done.get("done")))
    _prompt_agent(env, raises=RuntimeError("boom in grace"))
    manager, task = _task(env)
    manager.run_task(task, new_terminal=False)
    assert _pushed(env) == [("error", _failed("Error: boom in grace"))]
    _assert_not_done(env, "daily")


def test_a_turn_that_raises_in_the_grace_is_failed_at_runtime(env):
    """The same with the real bounded run and grace wait, shortened: a 1 s limit, a 3 s grace,
    a turn that raises after 1.5 s."""
    import time as _time
    from vaf.core.config import Config

    real_get, real_wait = Config.get, automation._wait_for_abandoned_run
    env.monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, key, default=None: 1 if key == "automation_run_timeout_seconds"
        else real_get(key, default)))
    env.monkeypatch.setattr(automation, "_wait_for_abandoned_run",
                            lambda chat_done, **k: real_wait(chat_done, grace_seconds=3, poll=0.1))
    import vaf.core.agent as agent_mod
    seen = _prompt_agent(env)
    stub = agent_mod.Agent

    def late_raise(self, prompt, stream_callback=None, **k):
        seen["calls"] += 1
        _time.sleep(1.5)
        raise RuntimeError("provider exploded late")

    env.monkeypatch.setattr(stub, "chat_step", late_raise)
    manager, task = _task(env)
    started = _time.monotonic()
    manager.run_task(task, new_terminal=False)
    assert _time.monotonic() - started < 3.0, "the grace was waited out for a turn that ended"
    assert seen["calls"] == 1
    assert _pushed(env) == [("error", _failed("Error: provider exploded late"))]
    _assert_not_done(env, "daily")


# ── any other exception in the run ───────────────────────────────────────────

def test_an_output_file_that_cannot_be_written_fails_the_run(env):
    """The turn answered, then writing its output file raised: the run is failed, with the
    exception, and not stamped. MUTATION: keep the outcome already decided in the handler."""
    blocker = env.out / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    _prompt_agent(env, returns="Fertig.", turn=ANSWERED_TWICE)
    manager, task = _task(env)
    manager.update(TID, output_path=str(blocker))
    manager.run_task(manager.get(TID), new_terminal=False)
    [(status, text)] = _pushed(env)
    assert status == "error" and text.startswith("Der Lauf ist fehlgeschlagen: Error: "), text
    assert text.endswith(NEXT["daily"])
    _assert_not_done(env, "daily")


# ── AC12: decided once ───────────────────────────────────────────────────────

def _run_task_node():
    tree = ast.parse(Path(automation.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "AutomationManager":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "run_task":
                    return tree, item
    raise AssertionError("AutomationManager.run_task not found")


def _name(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


def test_outcome_is_decided_once():
    """MUTATIONS: a literal status passed to the run log, the delivery or the workspace sync;
    a second `_record_successful_run` call, or one outside `if outcome.completed`; a `return`
    back in the workflow lane; any `startswith("Error:")` in the module."""
    tree, run_task = _run_task_node()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _name(node) == "startswith":
            for arg in node.args:
                assert not (isinstance(arg, ast.Constant) and str(arg.value).startswith("Error")), \
                    f"a run's status is read from its text again (line {node.lineno})"

    calls = [n for n in ast.walk(run_task) if isinstance(n, ast.Call)]
    status_args = {"append_run_log": ("status", None), "_push_result_to_web_ui": (None, 1),
                   "_sync_workspace_automation_state": ("run_status", None)}
    checked = 0
    for call in calls:
        if _name(call) not in status_args:
            continue
        keyword, position = status_args[_name(call)]
        values = [k.value for k in call.keywords if k.arg == keyword] if keyword else []
        if position is not None and len(call.args) > position:
            values.append(call.args[position])
        assert values, f"{_name(call)} (line {call.lineno}) passes no status"
        for value in values:
            assert not isinstance(value, ast.Constant), \
                f"{_name(call)} (line {call.lineno}) is given a literal status"
        checked += 1
    assert checked == 3, "each consumer is called once, for both lanes"

    stamps = [c for c in calls if _name(c) == "_record_successful_run"]
    assert len(stamps) == 1, "the run is stamped in more than one place"
    guarded = [n for n in ast.walk(run_task) if isinstance(n, ast.If)
               and isinstance(n.test, ast.Attribute) and n.test.attr == "completed"
               and any(c is stamps[0] for c in ast.walk(n))]
    assert guarded, "the stamp is not guarded by the outcome's `completed`"

    lanes = [n for n in ast.walk(run_task) if isinstance(n, ast.If)
             and "workflow_steps" in ast.unparse(n.test)]
    assert lanes, "the workflow lane moved - re-anchor this guard"
    returns = [n for stmt in lanes[0].body for n in ast.walk(stmt) if isinstance(n, ast.Return)]
    assert returns == [], "the workflow lane returns before the common tail again"


# ── the outcome itself ───────────────────────────────────────────────────────

def test_run_outcome_coerces_at_the_boundary():
    """An unknown status is a failure, so it never counts as done."""
    for status in ("done", "", None, 1, "OK"):
        outcome = RunOutcome(status, None)
        assert outcome.status == "failed" and outcome.text == "" and not outcome.completed
        assert outcome.wire_status == "error"
    assert [RunOutcome(s).completed for s in automation.RUN_STATUSES] == [True, True, False, False]
    assert RunOutcome("ok", "short", report="long").returned == "long"
    assert RunOutcome("ok", "short").returned == "short"


# ── fuzz: the prompt classifier against an independent rule ──────────────────

_BLANKS = ("", " ", "\n", "  \n", "\t ")
_BODIES = ("", "Fertig.", "Error: steht im Text.", "[Error] backend down",
           "[API Error from x: 500]", "Siehe [ASYNC_ACK] im Text.",
           "Zitat: [SYSTEM_LOG_ONLY] mitten drin.", "Zeile eins\nZeile zwei", "   ",
           "<think>Übersetze: geheimer Plan</think>Le helper travaille.",
           "<think>nie geschlossen, geheimer Plan", "</think>Rest",
           'Text. {"name": "web_search", "arguments": {"q": 1}}')
_FREQUENCIES = ("daily", "once", "on_event")
_HEADS = ("", "[ASYNC_ACK]", "[SYSTEM_LOG_ONLY]", "[Error] ", "[API Error from x: 503] ")


def _draw(rng):
    if rng.random() < 0.08:
        answer = None
    else:
        answer = rng.choice(_BLANKS) + rng.choice(_HEADS) + rng.choice(_BODIES)
        if rng.random() < 0.2:
            answer += rng.choice(_HEADS) + rng.choice(_BODIES)
        answer = answer[:4096]
    rounds = tuple(TurnRound(i + 1, ("c",) if rng.random() < 0.5 else (), rng.random() < 0.4)
                   for i in range(rng.randint(0, 3)))
    turn = TurnStatus(failed=rng.random() < 0.25, rounds=rounds)
    return answer, turn, rng.random() < 0.15, rng.choice(_FREQUENCIES)


class _Cleaner:
    """The agent's own reasoning cleaner, as `run_task` hands it over."""
    _sanitize_response = Agent._sanitize_response

    def __call__(self, text):
        return Agent._clean_reasoning(self, text)


CLEAN = _Cleaner()


def _raising_cleaner(text):
    raise RuntimeError("cleaner failed")


def _reference(answer, turn, timed_out, frequency):
    """The rule, written apart from the code: timeout first, then a failed turn or a
    `[SYSTEM_LOG_ONLY]` head is failed, everything else ok. The text after a control head is
    cleaned of marked reasoning and leaked tool calls. An answered turn (an answering round or
    an `[ASYNC_ACK]` head) with text left delivers it; the rest delivers the stream."""
    if timed_out:
        return "timed_out", "NOTE"
    text = "" if answer is None else answer
    lead = text.lstrip()
    head = next((m for m in ("[ASYNC_ACK]", "[SYSTEM_LOG_ONLY]") if lead.startswith(m)), "")
    rest = CLEAN(lead[len(head):] if head else text).strip()
    if turn.failed or head == "[SYSTEM_LOG_ONLY]":
        return "failed", _failed(rest, frequency)
    if (any(r.answer for r in turn.rounds) or head == "[ASYNC_ACK]") and rest:
        return "ok", rest
    return "ok", "STREAM"


def test_prompt_outcome_fuzz():
    """Seeded draws in the suite; `VAF_FUZZ_DRAWS` runs more outside it."""
    rng = random.Random(458)
    draws = int(os.environ.get("VAF_FUZZ_DRAWS", "400"))
    mismatches = []
    for n in range(draws):
        answer, turn, timed_out, frequency = _draw(rng)
        streamed = []

        def stream():
            streamed.append(1)
            return "STREAM"

        outcome = _prompt_outcome(answer, turn, stream, timed_out=timed_out, timeout_note="NOTE",
                                  frequency=frequency, clean=CLEAN)
        want = _reference(answer, turn, timed_out, frequency)
        if (outcome.status, outcome.text) != want:
            mismatches.append((n, answer, turn, timed_out, (outcome.status, outcome.text), want))
        assert outcome.completed == (outcome.status in ("ok", "paused"))
        if streamed:
            assert outcome.status == "ok", "a status other than ok came from the stream path"
        else:
            # Nothing the turn returned reaches the person with reasoning or a leaked tool
            # call left in it: the delivered text is what the cleaner would leave of it.
            assert CLEAN(outcome.text) == outcome.text, (n, answer, outcome.text)
        for secret in ("geheimer Plan", "Übersetze", '"web_search"'):
            assert secret not in outcome.text, (n, answer, outcome.text)
        # I7: a cleaner that raises lets none of it through either.
        raised = _prompt_outcome(answer, turn, stream, timed_out=timed_out, timeout_note="NOTE",
                                 frequency=frequency, clean=_raising_cleaner)
        assert raised.status == outcome.status, (n, answer)
        for secret in ("geheimer Plan", "Übersetze", '"web_search"', "<think>"):
            assert secret not in raised.text, (n, answer, raised.text)
    assert mismatches == [], mismatches[:5]
