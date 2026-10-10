# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The agent's sandbox environment tools and their background processes.

The tools are thin: they hand the caller's identity to EnvironmentManager and turn its
refusals into "[ERROR] <tool>: <reason>". What they decide themselves is pinned here:
whose environment, the host side of a transfer through is_safe_path, the attributes the
dispatcher reads. The processes (sandbox_exec background=true, read and stopped through
host_process) are pinned against a scripted docker."""
import types
from pathlib import Path

import pytest

from vaf.core import containers
from vaf.core import environments as envmod
from vaf.tools import environments as tools
from vaf.tools.host_process import HostProcessTool


class _Mgr:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _call(*a, **k):
            self.calls.append((name, a, k))
            if name == "create":
                return types.SimpleNamespace(id="0a1b2c3d", degraded="",
                                             describe=lambda: "0a1b2c3d, kind=temporary")
            if name == "exec":
                return envmod.ExecResult(0, "hi\n", "")
            if name in ("stop", "delete"):
                return types.SimpleNamespace(id=a[1])
            if name == "list":
                return []
            if name == "start_process":
                return "e-0a1b2c3d-p12345678"
            if name == "copy_out":
                return []
            return "ok"
        return _call


@pytest.fixture
def mgr(monkeypatch):
    m = _Mgr()
    monkeypatch.setattr(envmod, "get_environment_manager", lambda: m)
    return m


def test_every_tool_acts_as_the_caller_and_never_on_a_channel():
    for cls in (tools.SandboxManageTool, tools.SandboxExecTool, tools.SandboxFilesTool,
                tools.SandboxTransferTool):
        assert cls.identity_kwargs == ("user_scope_id", "username", "user_role", "session_id")
        assert cls.channel_restrictions == ("channel",)
        assert cls.category == "code" and cls.permission_level == "write"
    assert tools.SandboxManageTool.file_access == "write"
    assert tools.SandboxTransferTool.file_access == "write"
    assert tools.SandboxExecTool.self_supervised is True


def test_thinking_runs_get_none_of_them():
    """A background thinking run must not create containers or run code in them.
    MUTATION: drop them from the thinking exclusion - red."""
    import re
    src = (Path(tools.__file__).resolve().parents[1] / "core" / "agent.py").read_text(encoding="utf-8")
    block = re.search(r"if _rk_thinking:\s*\n\s*if instance\.name in \(([^)]*)\)", src).group(1)
    for name in ("sandbox_manage", "sandbox_exec", "sandbox_files", "sandbox_transfer"):
        assert f'"{name}"' in block, name


def test_manage_hands_the_callers_scope_and_session(mgr):
    out = tools.SandboxManageTool().run(action="create", kind="temporary", network="none",
                                        user_scope_id="scope-alice", session_id="chat-1",
                                        user_role="user")
    assert "Created" in out and 'sandbox_exec(environment="0a1b2c3d"' in out
    name, args, kw = mgr.calls[-1]
    assert name == "create" and args == ("scope-alice",)
    assert kw["session_id"] == "chat-1" and kw["user_role"] == "user" and kw["network"] == "none"
    tools.SandboxManageTool().run(action="delete", environment="0a1b2c3d", user_scope_id="scope-alice")
    assert mgr.calls[-1][:2] == ("delete", ("scope-alice", "0a1b2c3d"))


def test_a_refusal_is_an_error_line_with_the_reason(monkeypatch):
    class _Refusing:
        def create(self, *a, **k):
            raise envmod.EnvironmentRefused("you have 3 environments, the limit is 3")
    monkeypatch.setattr(envmod, "get_environment_manager", lambda: _Refusing())
    out = tools.SandboxManageTool().run(action="create", user_scope_id="s")
    assert out == "[ERROR] sandbox_manage: you have 3 environments, the limit is 3"


def test_a_project_path_runs_through_is_safe_path(mgr, monkeypatch):
    import vaf.tools.filesystem as fs
    monkeypatch.setattr(fs, "is_safe_path", lambda p: (False, "Access denied: VAF's own data directory"))
    out = tools.SandboxManageTool().run(action="create", kind="project", project_path="/x",
                                        user_scope_id="s")
    assert "Access denied" in out and not [c for c in mgr.calls if c[0] == "create"]


class _ProjectMgr:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def create(self, scope, **kw):
        self.calls.append(kw)
        if self.fail:
            raise envmod.EnvironmentRefused("not enough free memory")
        return types.SimpleNamespace(id="0a1b2c3d", degraded="", kind=kw["kind"],
                                     project_path=kw["project_path"] or "",
                                     describe=lambda: "0a1b2c3d, kind=project")


@pytest.fixture
def chat_area(monkeypatch, tmp_path):
    import vaf.core.session as session_mod
    area = tmp_path / "VAF_Projects" / "ab12cd34" / "chat-1"

    def _ws(session_id=None, create=False, *, user_scope_id=None):
        if not session_id:
            return None
        area.mkdir(parents=True, exist_ok=True)
        return area

    monkeypatch.setattr(session_mod, "get_session_workspace_dir", _ws)
    return area


def test_a_project_without_a_folder_gets_one_in_the_chats_own_area(monkeypatch, chat_area):
    """MUTATION: drop _fresh_project_folder from sandbox_manage - red: the live agent searched
    the whole home directory with host_bash for a folder to give it."""
    m = _ProjectMgr()
    monkeypatch.setattr(envmod, "get_environment_manager", lambda: m)
    run = lambda: tools.SandboxManageTool().run(action="create", kind="project", name="Mein Zaehler!",
                                                user_scope_id="scope-alice", session_id="chat-1")
    out = run()
    assert m.calls[-1]["project_path"] == str(chat_area / "mein-zaehler")
    assert (chat_area / "mein-zaehler").is_dir()
    assert f'coding_agent(task=..., project_path="{chat_area / "mein-zaehler"}", environment="0a1b2c3d")' in out
    run()                                                     # a second one never reuses it
    assert m.calls[-1]["project_path"] == str(chat_area / "mein-zaehler-2")


def test_without_a_chat_a_project_needs_its_path(monkeypatch, chat_area):
    m = _ProjectMgr()
    monkeypatch.setattr(envmod, "get_environment_manager", lambda: m)
    out = tools.SandboxManageTool().run(action="create", kind="project", user_scope_id="s")
    assert "needs project_path" in out and m.calls == []


def test_a_refused_create_leaves_no_empty_folder_behind(monkeypatch, chat_area):
    monkeypatch.setattr(envmod, "get_environment_manager", lambda: _ProjectMgr(fail=True))
    out = tools.SandboxManageTool().run(action="create", kind="project", name="x",
                                        user_scope_id="s", session_id="chat-1")
    assert "not enough free memory" in out and not (chat_area / "x").exists()


def test_the_manager_and_the_coder_point_at_each_other():
    """Each lane learned of the other only one way: coding_agent's environment parameter named
    sandbox_manage, sandbox_manage never named the coder."""
    assert "coding_agent with environment=" in tools.SandboxManageTool.description
    from vaf.tools.coder import CodingAgentTool
    assert "sandbox_manage" in CodingAgentTool.parameters["properties"]["environment"]["description"]


def test_exec_bounds_the_timeout_and_reports_the_exit(mgr):
    out = tools.SandboxExecTool().run(environment="0a1b2c3d", command="echo hi", timeout=99999,
                                      user_scope_id="s")
    assert out.startswith("[exit 0]") and "hi" in out
    name, args, kw = mgr.calls[-1]
    assert name == "exec" and args == ("s", "0a1b2c3d", "echo hi")
    assert kw["timeout"] == tools.SandboxExecTool.MAX_TIMEOUT_SECONDS


def test_a_depth_that_is_not_a_number_is_the_default(mgr):
    """MUTATION: back to a bare int() - red: "two" read as a sandbox that could not be reached."""
    out = tools.SandboxFilesTool().run(environment="0a1b2c3d", action="list", depth="two",
                                       user_scope_id="s")
    assert "could not be reached" not in out
    assert mgr.calls[-1][0] == "list_files" and mgr.calls[-1][2]["depth"] == 2
    tools.SandboxFilesTool().run(environment="0a1b2c3d", action="list", depth=99, user_scope_id="s")
    assert mgr.calls[-1][2]["depth"] == 6


def test_preview_reads_numbers_it_cannot_parse_as_the_defaults(monkeypatch):
    """MUTATION: back to bare int() for the preview - red: "wide" read as a sandbox that
    could not be reached."""
    seen = {}

    class _M:
        def render(self, scope, env_id, target, width, height, wait_ms):
            seen.update(width=width, height=height, wait_ms=wait_ms)
            return {"ok": False, "error": "stop"}

    monkeypatch.setattr(envmod, "get_environment_manager", lambda: _M())
    out = tools.SandboxPreviewTool().run(environment="0a1b2c3d", target="index.html", width="wide",
                                         height="", wait_ms="soon", user_scope_id="s")
    assert "could not be reached" not in out
    assert seen == {"width": 1280, "height": 800, "wait_ms": 1500}


def test_exec_in_the_background_returns_a_handle(mgr):
    out = tools.SandboxExecTool().run(environment="0a1b2c3d", command="npm run dev", background=True,
                                      user_scope_id="s", session_id="chat-1", username="alice")
    assert "e-0a1b2c3d-p12345678" in out and "host_process" in out
    name, args, kw = mgr.calls[-1]
    assert name == "start_process" and kw["session_id"] == "chat-1" and kw["username"] == "alice"


def test_transfer_checks_the_host_side_first(mgr, monkeypatch):
    """MUTATION: drop the is_safe_path check - red."""
    import vaf.tools.filesystem as fs
    monkeypatch.setattr(fs, "is_safe_path", lambda p: (False, "Access denied: outside your own data"))
    out = tools.SandboxTransferTool().run(action="copy_out", environment="0a1b2c3d",
                                          host_path="/etc", path="out", user_scope_id="s")
    assert "outside your own data" in out and mgr.calls == []


def test_host_process_routes_environment_ids_and_refuses_write(mgr, monkeypatch):
    import vaf.core.subagent_ipc as ipc
    monkeypatch.setattr(ipc, "get_current_session_id", lambda: "chat-1")
    tool = HostProcessTool()
    assert "no input" in tool.run(action="write", id="e-0a1b2c3d-p12345678", text="x", user_scope_id="s")
    tool.run(action="stop", id="e-0a1b2c3d-p12345678", user_scope_id="s")
    assert mgr.calls[-1][:2] == ("stop_process", ("s", "e-0a1b2c3d-p12345678"))


# -- the processes themselves, against a scripted docker ---------------------------------

class _ProcDocker:
    """Enough docker for one running environment and its processes."""

    def __init__(self, container):
        self.container = container
        self.alive, self.exits, self.calls, self.detached = set(), {}, [], []

    def __call__(self, args, timeout=60, **kw):
        self.calls.append(list(args))
        if args[:2] == ["exec", "-d"]:
            marker = next(a for a in args if a.startswith("VAF_PROC_ID="))
            proc = marker.split("=", 1)[1]
            self.alive.add(proc)
            self.detached.append((args, kw.get("env") or {}))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "exec" and "sh" in args and "VAF_PROC_ID" in args[-1] and "kill" in args[-1]:
            proc = args[-1].split("VAF_PROC_ID=")[1].split('"')[0]
            self.alive.discard(proc)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "exec" and "sh" in args and "alive" in args[-1]:
            lines = [f"alive {p}" for p in sorted(self.alive)]
            lines += [f"exit {p} {c}" for p, c in self.exits.items()]
            return types.SimpleNamespace(returncode=0, stdout="\n".join(lines), stderr="")
        if args[0] == "exec" and "tail" in args:
            return types.SimpleNamespace(returncode=0, stdout="Listening on :5173\n", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def procs(monkeypatch, tmp_path):
    m = envmod.EnvironmentManager(state_dir=tmp_path / "state")
    env = envmod.Environment(id="0a1b2c3d", kind="project", network="open",
                             owner=containers.scope_hash("scope-alice"),
                             container="vaf-env-x-0a1b2c3d", volume="v", net="n",
                             created=1.0, last_used=1.0, state="running")
    m._write_state(env)
    monkeypatch.setattr(m, "get", lambda owner, env_id, admin=False: (
        env if env_id == env.id and owner in ("scope-alice", None) else
        (_ for _ in ()).throw(envmod.EnvironmentRefused(f"no environment {env_id!r}"))))
    monkeypatch.setattr(m, "list", lambda owner, everyone=False: [env] if owner == "scope-alice" else [])
    fake = _ProcDocker(env.container)
    monkeypatch.setattr(containers, "docker", fake)
    woken = []
    import vaf.core.task_queue as tq
    monkeypatch.setattr(tq, "enqueue_wake_turn", lambda **kw: woken.append(kw))
    return m, env, fake, woken


def test_a_background_process_carries_its_marker_and_its_command_off_the_command_line(procs):
    m, env, fake, _ = procs
    handle = m.start_process("scope-alice", env.id, "npm run dev -- --port 5173", session_id="chat-1")
    assert m.parse_process_handle(handle) == (env.id, handle.split("-")[-1])
    args, values = fake.detached[0]
    assert "npm run dev" not in " ".join(args), "the command travels in the environment"
    assert values["VAF_CMD"] == "npm run dev -- --port 5173"
    [p] = m.processes("scope-alice", session_id="chat-1")
    assert p["handle"] == handle and p["state"] == "running"
    assert m.processes("scope-alice", session_id="another-chat") == []


def test_someone_elses_process_reads_as_missing(procs):
    m, env, _, _ = procs
    handle = m.start_process("scope-alice", env.id, "sleep 99", session_id="chat-1")
    with pytest.raises(envmod.EnvironmentRefused):
        m.stop_process("scope-bob", handle)
    with pytest.raises(envmod.EnvironmentRefused):
        m.process_log("scope-alice", "e-0a1b2c3d-p00000000")


def test_an_ended_process_wakes_its_chat_once(procs):
    """MUTATION: never mark it notified - two wakes, red."""
    m, env, fake, woken = procs
    handle = m.start_process("scope-alice", env.id, "npm test", session_id="chat-1", username="alice")
    proc = handle.split("-")[-1]
    m._watch_processes(env, now=10.0)
    assert woken == []                                    # still running
    fake.alive.discard(proc)
    fake.exits[proc] = "0"
    m._watch_processes(env, now=20.0)
    m._watch_processes(env, now=30.0)
    assert len(woken) == 1
    w = woken[0]
    assert w["kind"] == "process" and w["session_id"] == "chat-1"
    assert w["user_scope_id"] == "scope-alice" and w["username"] == "alice"
    assert handle in w["text"] and "exit 0" in w["text"] and "Listening" in w["text"]


def test_a_stopped_process_does_not_wake_and_an_old_one_is_ended(procs, monkeypatch):
    m, env, fake, woken = procs
    h1 = m.start_process("scope-alice", env.id, "sleep 999", session_id="chat-1")
    m.stop_process("scope-alice", h1)
    fake.exits[h1.split("-")[-1]] = "137"
    m._watch_processes(env, now=1e12)
    assert woken == []
    monkeypatch.setenv("VAF_SANDBOX_ENV_PROCESS_MAX_HOURS", "1")
    h2 = m.start_process("scope-alice", env.id, "vite", session_id="chat-1")
    p2 = h2.split("-")[-1]
    import time as _t
    m._watch_processes(env, now=_t.time() + 7200)
    assert p2 not in fake.alive, "past the limit it is ended"
    assert len(woken) == 1 and "sandbox_env_process_max_hours" in woken[0]["text"]


def test_the_shell_scripts_carry_no_raw_control_characters(procs):
    """A "\\0" written into a non-raw Python string becomes a real NUL in the script, and
    the shell then never matches a marker: every process read as ended (measured live).
    MUTATION: turn the escaped \\\\0 back into \\0 - red."""
    m, env, fake, _ = procs
    m.start_process("scope-alice", env.id, "sleep 9", session_id="chat-1")
    m.processes("scope-alice")
    scripts = [c[-1] for c in fake.calls if c[0] == "exec" and "sh" in c]
    assert scripts
    for s in scripts + [containers.MARKED_PROCESSES_CMD,
                        containers.kill_marked_cmd("VAF_RUN_ID", "ab12")]:
        assert "\x00" not in s and "\n" not in s, repr(s)
