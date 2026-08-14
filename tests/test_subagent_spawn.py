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
