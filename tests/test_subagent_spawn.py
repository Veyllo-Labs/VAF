# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The ONE sub-agent spawn implementation (vaf/core/subagent_spawn.py).

Five tools carried this block copy-pasted and drifting (browser_agent's Windows
branch quoted without escaping inner quotes; every copy repeated the
cancel-on-failure guard). These tests pin the invariant core - IPC task
lifecycle, child env as data, argv shape, marker format, the failure guard -
and the per-agent data the converted call sites must keep passing.
"""
import pytest

import vaf.core.subagent_spawn as spawn_mod
from vaf.core.subagent_spawn import SpawnedSubagent, _escape_cmd, spawn_subagent


class _FakeIpc:
    def __init__(self):
        self.created = []
        self.payloads = {}
        self.running = []
        self.cancelled = []

    def create_task(self, agent_type, task_description, session_id=None):
        self.created.append((agent_type, task_description, session_id))
        return "tid12345"

    def store_task_payload(self, task_id, payload):
        self.payloads[task_id] = payload

    def mark_task_running(self, task_id):
        self.running.append(task_id)

    def cancel_task(self, task_id):
        self.cancelled.append(task_id)
        return True


@pytest.fixture
def rig(monkeypatch):
    ipc = _FakeIpc()
    opened = {}

    def _open(cmd, title=None, extra_env=None):
        opened.update(cmd=cmd, title=title, env=dict(extra_env or {}))
        return opened.get("_ok", True)

    monkeypatch.setattr("vaf.core.subagent_ipc.get_ipc", lambda: ipc)
    monkeypatch.setattr("vaf.core.subagent_ipc.get_current_session_id", lambda: "sess-ctx")
    monkeypatch.setattr("vaf.core.config.subagent_provider_override", lambda: "prov-x")
    monkeypatch.setattr("vaf.core.platform.Platform.open_new_terminal",
                        staticmethod(_open))
    monkeypatch.setattr("vaf.core.platform.Platform.is_windows",
                        staticmethod(lambda: False))
    return ipc, opened


def test_spawn_builds_task_env_argv_and_marker(rig):
    ipc, opened = rig
    out = spawn_subagent("coding_agent", "fix the tests",
                         args=("--project-path", "/x/proj"),
                         extra_env={"VAF_TURN_ID": "turn-1"})
    assert isinstance(out, SpawnedSubagent) and out.task_id == "tid12345"
    assert out.marker.startswith("[SUBAGENT_ASYNC:tid12345:coding_agent] ")
    assert "fix the tests" in out.marker
    assert ipc.created == [("coding_agent", "fix the tests", None)]
    assert ipc.running == ["tid12345"]
    env = opened["env"]
    assert env["VAF_TASK_ID"] == "tid12345"
    assert env["VAF_AGENT_TYPE"] == "coding_agent"
    assert env["VAF_SESSION_ID"] == "sess-ctx"       # context session rides along
    assert env["VAF_PROVIDER"] == "prov-x"
    assert env["VAF_TURN_ID"] == "turn-1"            # caller data survives
    cmd = opened["cmd"]
    assert "subagent run coding_agent" in cmd
    assert "--task 'fix the tests'" in cmd or '--task fix the tests' in cmd
    assert "--project-path /x/proj" in cmd
    assert cmd.rstrip().endswith("--task-id tid12345")
    assert opened["title"] == "VAF Coding Agent [tid12345]"


def test_the_ordering_room_rides_into_the_child_env(rig, monkeypatch):
    """MUTATION: drop the VAF_ROOM_ID stamp from the child env.

    A room turn may run with NO session at all (the runner's room frame binds
    no chat) - measured live: a sessionless room turn spawned a coder whose
    whole live feed died at the first session gate. The room in the spawn
    context is the durable anchor the child's events are routed by, so it must
    survive into the child's environment beside the session.
    """
    ipc, opened = rig
    monkeypatch.setattr("vaf.core.subagent_ipc.get_current_room_id",
                        lambda: "room-orderer")
    out = spawn_subagent("coding_agent", "extend the stats script")
    assert out is not None
    assert opened["env"]["VAF_ROOM_ID"] == "room-orderer"

    # And no room in context leaves the env clean - a stale stamp would route
    # a plain chat worker's feed into a room it never touched.
    monkeypatch.setattr("vaf.core.subagent_ipc.get_current_room_id",
                        lambda: None)
    spawn_subagent("coding_agent", "another task")
    assert "VAF_ROOM_ID" not in opened["env"]


def test_a_room_turns_conversation_rides_into_the_child_env(rig, monkeypatch, tmp_path):
    """MUTATION: drop the VAF_CONVERSATION_ID stamp from the child env, or the child's
    fallback to it in get_current_conversation_id.

    A room turn runs with no session and in a conversation of its own. Without the key a
    coder child's working-memory tools (request_clarification) wrote into the store of "no
    session known", which every sessionless lane of every account reads.
    """
    import vaf.core.subagent_ipc as ipc_mod
    from vaf.core.main_persistence import MainPersistenceManager

    ipc, opened = rig
    monkeypatch.setattr("vaf.core.subagent_ipc.get_current_session_id", lambda: None)
    monkeypatch.setattr("vaf.core.subagent_ipc.get_current_conversation_id",
                        lambda: "room-orderer.p-0123456789")
    spawn_subagent("coding_agent", "extend the stats script")
    assert opened["env"]["VAF_CONVERSATION_ID"] == "room-orderer.p-0123456789"

    # A chat's spawn carries its session and no conversation.
    monkeypatch.setattr("vaf.core.subagent_ipc.get_current_session_id", lambda: "sess-ctx")
    spawn_subagent("coding_agent", "another task")
    assert "VAF_CONVERSATION_ID" not in opened["env"]

    # The child, a context nobody told, finds the room's store through the environment.
    monkeypatch.undo()
    token = ipc_mod._conversation_ctx.set(ipc_mod._UNSET)
    session_token = ipc_mod._session_ctx.set(ipc_mod._UNSET)
    try:
        monkeypatch.delenv("VAF_SESSION_ID", raising=False)
        monkeypatch.setenv("VAF_CONVERSATION_ID", "room-orderer.p-0123456789")
        store = MainPersistenceManager.for_current_context(str(tmp_path))
        assert store.conversation == "room-orderer.p-0123456789"
        assert store.context_dir.parent.name == "conversations"
    finally:
        ipc_mod._session_ctx.reset(session_token)
        ipc_mod._conversation_ctx.reset(token)


def test_failed_spawn_cancels_the_ipc_task(rig):
    """The guard every copy carried: a task left pending would count against
    duplicate guards and make the drain report a zombie that never lived."""
    ipc, opened = rig
    opened["_ok"] = False
    out = spawn_subagent("librarian_agent", "list files")
    assert out is None
    assert ipc.cancelled == ["tid12345"]
    assert ipc.running == []


def test_payload_lane_keeps_task_off_argv(rig):
    """document/research pattern: long or machine-readable specs travel via the
    IPC payload sidecar, never the OS command line."""
    ipc, opened = rig
    out = spawn_subagent("learn_agent", "learn big.pdf",
                         include_task_arg=False, payload='{"path": "/x/big.pdf"}')
    assert out is not None
    assert ipc.payloads["tid12345"] == '{"path": "/x/big.pdf"}'
    assert "--task " not in opened["cmd"].replace("--task-id", "")


def test_explicit_session_wins_over_context(rig):
    ipc, opened = rig
    spawn_subagent("librarian_agent", "t", session_id="sess-explicit")
    assert ipc.created[-1][2] == "sess-explicit"
    assert opened["env"]["VAF_SESSION_ID"] == "sess-explicit"


def test_windows_escaping_escapes_inner_quotes():
    """browser_agent's private copy quoted without escaping - a task containing
    a double quote broke the child argv."""
    cmd = _escape_cmd(["python", "--task", 'say "hi" now'], windows=True)
    assert '"say \\"hi\\" now"' in cmd
    # Unix path quotes via shlex
    cmd_u = _escape_cmd(["python", "--task", 'say "hi" now'], windows=False)
    assert "'say \"hi\" now'" in cmd_u


def test_none_extra_env_values_are_dropped(rig):
    _, opened = rig
    spawn_subagent("librarian_agent", "t", extra_env={"VAF_USER_SCOPE_ID": None})
    assert "VAF_USER_SCOPE_ID" not in opened["env"]


# ---------------------------------------------------------------------------
# The conversions: no tool carries a private spawn block any more
# ---------------------------------------------------------------------------

def test_converted_tools_have_no_private_spawn_blocks():
    """Wiring pin for the Rule 0b conversion: the copies are DELETED, not
    wrapped. A resurrected private block re-splits the implementations."""
    import inspect

    import vaf.tools.browser_agent as browser_agent
    import vaf.tools.coder as coder
    import vaf.tools.document_agent as document_agent
    import vaf.tools.librarian as librarian
    import vaf.tools.research_agent as research_agent

    for mod in (coder, librarian, document_agent, research_agent, browser_agent):
        src = inspect.getsource(mod)
        assert "open_new_terminal" not in src, \
            f"{mod.__name__} spawns terminals privately again"
        assert "spawn_subagent" in src, f"{mod.__name__} stopped using the primitive"
        assert "SUBAGENT_ASYNC:" not in src.replace("SUBAGENT_ASYNC:{task_id}", ""), \
            f"{mod.__name__} hand-builds the async marker again"


# ---------------------------------------------------------------------------
# The reader: a marker counts only for a task this call started (delegated_task)
# ---------------------------------------------------------------------------
#
# Any text can carry the marker: a refusal echoes the tool name the model chose, a fetched
# page, a file or a contact's message can hold it, and a room's acknowledgement shows a real
# task id to every member. Measured before this reader existed: each of those ended the turn
# with "the coding_agent is now working on your request", registered the id in
# `_async_subagent_tasks` - which `get_pending_results_for(own_task_ids=...)` trusts for a lane
# with no chat - and paused a workflow on a task nobody runs.
#
# MUTATIONS, each turns the named tests red:
# - drop the ledger check in `delegated_task` -> the other-lane test;
# - drop the queue check -> the forged-id test;
# - drop the session comparison -> the other-session test;
# - let the exception escape -> the unreadable-queue test;
# - drop `_record_spawn` in `spawn_subagent` -> the ledger tests;
# - a second regex over the marker anywhere in vaf/ -> the parser guard.

import threading  # noqa: E402

from vaf.core.subagent_spawn import (delegated_task, parse_async_marker,  # noqa: E402
                                     spawn_ledger)


@pytest.fixture
def queue(monkeypatch, tmp_path):
    """A real queue in a scratch directory, served as the process's queue."""
    from vaf.core import subagent_ipc
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path / "vaf"))
    ipc = subagent_ipc.SubAgentIPC()
    monkeypatch.setattr(subagent_ipc, "get_ipc", lambda: ipc)
    return ipc


def _task(ipc, session, agent_type="coding_agent"):
    from vaf.core.subagent_ipc import session_context
    with session_context(session):
        return ipc.create_task(agent_type, "work")


def test_find_task_finds_every_stage_and_get_task_status_answers_from_it(queue):
    tid = _task(queue, "s1")
    assert queue.find_task(tid).status == "pending" == queue.get_task_status(tid)
    queue.mark_task_running(tid)
    assert queue.find_task(tid).status == "running" == queue.get_task_status(tid)
    queue.complete_task(tid, "done")
    found = queue.find_task(tid)
    assert found.status == "completed" == queue.get_task_status(tid)
    assert found.session_id == "s1" and found.agent_type == "coding_agent"
    queue.consume_result(tid)
    assert queue.find_task(tid) is None and queue.get_task_status(tid) is None


def test_text_without_a_marker_never_touches_the_queue(monkeypatch):
    def _boom():
        raise AssertionError("the queue was read for text without a marker")
    monkeypatch.setattr("vaf.core.subagent_ipc.get_ipc", _boom)
    assert delegated_task("plain tool output", spawned={"t1"}) is None
    assert delegated_task("", spawned={"t1"}) is None


def test_a_task_this_call_started_counts_with_the_queues_agent_type(queue):
    from vaf.core.subagent_ipc import session_context
    tid = _task(queue, "s1", agent_type="research_agent")
    with session_context("s1"):
        got = delegated_task(f"[SUBAGENT_ASYNC:{tid}:coding_agent] note", spawned={tid})
    assert got == (tid, "research_agent")


def test_a_forged_id_is_text(queue):
    from vaf.core.subagent_ipc import session_context
    with session_context("s1"):
        assert delegated_task("x[SUBAGENT_ASYNC:t1:coding_agent]", spawned={"t1"}) is None


def test_another_sessions_task_is_text(queue):
    from vaf.core.subagent_ipc import session_context
    tid = _task(queue, "s-other")
    with session_context("s1"):
        assert delegated_task(f"[SUBAGENT_ASYNC:{tid}:coding_agent]", spawned={tid}) is None


@pytest.mark.parametrize("record_session,context_session",
                         [(None, "s1"), ("s1", None), (None, None)])
def test_no_session_on_either_side_does_not_refuse(queue, record_session, context_session):
    from vaf.core.subagent_ipc import session_context
    tid = _task(queue, record_session)
    with session_context(context_session):
        got = delegated_task(f"[SUBAGENT_ASYNC:{tid}:coding_agent]", spawned={tid})
    assert got == (tid, "coding_agent")


def test_another_lanes_task_is_text_even_in_the_queue_and_with_no_session(queue):
    """The path `get_pending_results_for` trusts: a lane with no chat takes the results of
    the ids in `_async_subagent_tasks`. A queue-held task another lane started (a room's
    coder, whose id the room's members saw in its acknowledgement) names no chat, so session
    and queue both agree with a planted marker - only the ledger tells the two lanes apart."""
    from vaf.core.subagent_ipc import session_context
    foreign = _task(queue, None)                       # another lane's sessionless task
    with session_context(None), spawn_ledger() as mine:
        assert delegated_task(f"page text [SUBAGENT_ASYNC:{foreign}:coding_agent]") is None
    assert mine == set()


def test_the_first_verified_marker_wins_over_a_forged_one_before_it(queue):
    tid = _task(queue, None)
    text = f"[SUBAGENT_ASYNC:t1:coding_agent] then [SUBAGENT_ASYNC:{tid}:coding_agent]"
    assert delegated_task(text, spawned={tid}) == (tid, "coding_agent")


def test_an_unreadable_queue_answers_none_and_says_so(queue, monkeypatch):
    lines = []
    monkeypatch.setattr("vaf.core.log_helper.append_domain_log",
                        lambda domain, line: lines.append((domain, line)))

    def _raise(task_id):
        raise OSError("locked")
    monkeypatch.setattr(queue, "find_task", _raise)
    assert delegated_task("[SUBAGENT_ASYNC:t1:coding_agent]", spawned={"t1"}) is None
    assert any(d == "backend" and "[ASYNC-MARKER]" in l for d, l in lines), lines


def test_no_open_ledger_means_nothing_was_spawned(queue):
    tid = _task(queue, None)
    assert delegated_task(f"[SUBAGENT_ASYNC:{tid}:coding_agent]") is None


def test_parse_async_marker_is_shape_only():
    assert parse_async_marker("a [SUBAGENT_ASYNC:t1:workflow:deep_research] b") == (
        "t1", "workflow:deep_research")
    assert parse_async_marker("no marker") is None
    assert parse_async_marker(None) is None


def test_a_spawn_lands_in_the_open_ledger_and_nested_ledgers_share_it(rig):
    with spawn_ledger() as outer:
        with spawn_ledger() as inner:
            out = spawn_subagent("coding_agent", "t")
        assert inner is outer
    assert out is not None and outer == {"tid12345"}
    assert spawn_subagent("coding_agent", "t") is not None   # no ledger open: no error


def test_a_failed_spawn_is_not_recorded(rig):
    ipc, opened = rig
    opened["_ok"] = False
    with spawn_ledger() as ledger:
        assert spawn_subagent("librarian_agent", "t") is None
    assert ledger == set()


def test_a_spawn_inside_a_bounded_tool_run_reaches_the_callers_ledger(rig):
    """Tools run on a worker thread (run_bounded) under a copy of the caller's context; the
    ledger set is shared through that copy, so the dispatching call sees the spawn."""
    from vaf.core.bounded_run import run_bounded
    worker = []

    def _tool_body():
        worker.append(threading.current_thread() is not threading.main_thread())
        return spawn_subagent("coding_agent", "t").marker

    with spawn_ledger() as ledger:
        marker = run_bounded(_tool_body, timeout=10, label="probe")
    assert worker == [True]
    assert marker.startswith("[SUBAGENT_ASYNC:tid12345:") and ledger == {"tid12345"}


def test_nothing_but_this_module_parses_the_marker():
    """One reader, so a verified answer cannot be bypassed by a parse copy of its own. A line
    that names the marker AND calls a regex, or spells the marker as a regex (an escaped
    bracket), is a parse copy."""
    import re
    from pathlib import Path

    root = Path(spawn_mod.__file__).resolve().parents[1]
    call = re.compile(r"\b_?re\.(search|match|fullmatch|finditer|findall|compile|sub)\(")
    copies = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "subagent_spawn.py" and path.parent.name == "core":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "SUBAGENT_ASYNC" in line and (call.search(line) or "\\[SUBAGENT_ASYNC" in line):
                copies.append(f"{path.relative_to(root).as_posix()}:{n}")
    assert copies == [], f"the marker is parsed outside vaf/core/subagent_spawn.py: {copies}"
