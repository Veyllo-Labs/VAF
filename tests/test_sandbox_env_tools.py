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


def test_exec_in_the_background_returns_a_handle(mgr, monkeypatch):
    import vaf.core.task_queue as tq
    monkeypatch.setattr(tq, "_wake_runners", {"test"})
    out = tools.SandboxExecTool().run(environment="0a1b2c3d", command="npm run dev", background=True,
                                      user_scope_id="s", session_id="chat-1", username="alice")
    assert "e-0a1b2c3d-p12345678" in out and "host_process" in out
    name, args, kw = mgr.calls[-1]
    assert name == "start_process" and kw["session_id"] == "chat-1" and kw["username"] == "alice"


def test_transfer_checks_the_host_side_first(mgr, monkeypatch):
    """MUTATION: drop the is_safe_path check - red."""
    import vaf.tools.filesystem as fs
    monkeypatch.setattr(fs, "is_safe_path", lambda p, **_k: (False, "Access denied: outside your own data"))
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
    assert len(woken) == 1 and "ended after 1 h, the longest a background command may run" in woken[0]["text"]
    assert "sandbox_env_process_max_hours" not in woken[0]["text"] and env.id + " " not in woken[0]["text"]


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


# -- the cap, the label, the run folder, one report per process, the watcher -------------

def _ended(fake, handle, code="0"):
    proc = handle.split("-")[-1]
    fake.alive.discard(proc)
    fake.exits[proc] = code
    return proc


def test_the_wake_names_the_label_and_how_long_it_ran(procs):
    """MUTATION: build the wake text from the command instead of the label, or drop the
    137 note - red."""
    import time as _t
    m, env, fake, woken = procs
    h = m.start_process("scope-alice", env.id, "sh -c 'python3 -u main.py'", session_id="chat-1",
                        label="python_sandbox: import subprocess")
    [row] = m.processes("scope-alice")
    assert row["label"] == "python_sandbox: import subprocess"
    _ended(fake, h)
    m._watch_processes(env, now=_t.time() + 42)
    text = woken[0]["text"]
    assert "python_sandbox: import subprocess" in text and "main.py" not in text
    assert h in text and "exit 0" in text and "after 4" in text      # 42 s, give or take one
    assert "out of memory" not in text
    h2 = m.start_process("scope-alice", env.id, "python3 big.py", session_id="chat-1")
    _ended(fake, h2, "137")
    m._watch_processes(env, now=_t.time())
    assert "exit 137 (killed: out of memory or ended from outside)" in woken[1]["text"]
    assert "python3 big.py" in woken[1]["text"]                        # no label: the command


def test_start_process_is_capped_per_environment(procs):
    """MUTATION: remove the cap check - red."""
    from vaf.core.processes import MAX_PER_CHAT
    m, env, fake, _ = procs
    handles = [m.start_process("scope-alice", env.id, f"sleep {i}", session_id="chat-1",
                               label=f"job {i}") for i in range(MAX_PER_CHAT)]
    with pytest.raises(envmod.EnvironmentRefused) as e:
        m.start_process("scope-alice", env.id, "sleep 99", session_id="chat-1")
    msg = str(e.value)
    assert f"the limit is {MAX_PER_CHAT}" in msg and 'host_process(action="stop"' in msg
    assert all(h in msg for h in handles) and "job 3" in msg
    assert len(fake.detached) == MAX_PER_CHAT, "the ninth never reached docker"
    _ended(fake, handles[0])
    m.start_process("scope-alice", env.id, "sleep 99", session_id="chat-1")   # room again


def test_two_watchers_wake_once(procs, monkeypatch):
    """Two lookers (the starting process's watcher and a reaper) see the same exit. The
    record is claimed under the lock before anyone is woken. MUTATION: wake first and
    write `notified` afterwards, without re-reading under the lock - two wakes, red."""
    m, env, fake, woken = procs
    h = m.start_process("scope-alice", env.id, "npm test", session_id="chat-1")
    proc = _ended(fake, h)
    real = m._proc_status
    nested = []

    def _status(e):
        if not nested:
            nested.append(1)
            m._watch_processes(env, now=50.0)          # the other watcher, in between
        return real(e)

    monkeypatch.setattr(m, "_proc_status", _status)
    m._watch_processes(env, now=50.0)
    assert len(woken) == 1 and h in woken[0]["text"]
    assert m._read_procs(env.id)[proc]["notified"] is True


def test_the_reaper_leaves_a_record_another_live_process_watches(procs, monkeypatch):
    """MUTATION: ignore watcher_pid - the first case wakes, red."""
    import os
    from vaf.core.platform import Platform
    m, env, fake, woken = procs
    h = m.start_process("scope-alice", env.id, "npm test", session_id="chat-1")
    proc = _ended(fake, h)
    recs = m._read_procs(env.id)
    recs[proc]["watcher_pid"] = os.getppid()                # another process, alive
    m._write_procs(env.id, recs)
    m._watch_processes(env, now=50.0)
    assert woken == [], "its wake belongs in the watching process's queue"
    monkeypatch.setattr(Platform, "is_process_running", staticmethod(lambda pid: False))
    m._watch_processes(env, now=50.0)
    assert len(woken) == 1, "a dead watcher's record is taken over"
    h2 = m.start_process("scope-alice", env.id, "npm run build", session_id="chat-1")
    p2 = _ended(fake, h2)
    assert "watcher_pid" not in m._read_procs(env.id)[p2]   # housekeeping is off in the suite
    m._watch_processes(env, now=50.0)
    assert len(woken) == 2, "a record without a watcher is anybody's"


def test_start_process_registers_with_the_watcher(procs, monkeypatch):
    """With housekeeping on, the starting process watches what it started: the record names
    it, one tick after the end queues the wake, and the reported record is dropped from the
    watch. MUTATION: skip the registration; keep a claimed record registered - red."""
    import os
    m, env, fake, woken = procs
    monkeypatch.delenv("VAF_SANDBOX_ENV_HOUSEKEEPING_OFF", raising=False)
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    started = []
    monkeypatch.setattr(m, "_start_watch_thread", lambda: started.append(1))
    monkeypatch.setattr(m, "_docker_rows", lambda owner=None, strict=False: [
        {"container": env.container, "state": "running", "id": env.id, "owner": env.owner,
         "kind": env.kind, "network": env.network}])
    monkeypatch.setattr(m, "_from_row", lambda row: env)
    h1 = m.start_process("scope-alice", env.id, "npm test", session_id="chat-1")
    h2 = m.start_process("scope-alice", env.id, "npm run dev", session_id="chat-1")
    p1, p2 = h1.split("-")[-1], h2.split("-")[-1]
    assert started == [1], "one thread for the manager, not one per process"
    assert m._read_procs(env.id)[p1]["watcher_pid"] == os.getpid()
    assert m._watched[env.id]["procs"] == {p1, p2}
    _ended(fake, h1)
    m._watch_tick(now=60.0)
    assert len(woken) == 1 and h1 in woken[0]["text"]
    assert m._watched[env.id]["procs"] == {p2}
    m.stop_process("scope-alice", h2)
    m._watch_tick(now=70.0)
    assert env.id not in m._watched and len(woken) == 1


def test_no_watcher_without_a_chat_in_a_sub_agent_or_with_housekeeping_off(procs, monkeypatch):
    """No watcher where nobody would be told: housekeeping off (the suite), no chat, or a
    sub-agent child whose wake queue nobody drains. The reaper takes those records."""
    m, env, fake, _ = procs
    started = []
    monkeypatch.setattr(m, "_start_watch_thread", lambda: started.append(1))
    m.start_process("scope-alice", env.id, "a", session_id="chat-1")
    monkeypatch.delenv("VAF_SANDBOX_ENV_HOUSEKEEPING_OFF", raising=False)
    m.start_process("scope-alice", env.id, "b", session_id="")
    monkeypatch.setenv("VAF_IN_SUBAGENT_TERMINAL", "1")
    m.start_process("scope-alice", env.id, "c", session_id="chat-1")
    assert started == [] and m._watched == {}
    assert not any("watcher_pid" in r for r in m._read_procs(env.id).values())


def test_cleanup_dir_is_checked_and_removed_on_end_and_on_stop(procs):
    """MUTATION: accept any path; skip the rm in _finish or in stop_process - red."""
    m, env, fake, woken = procs
    for bad in ("/workspace", "/tmp/x; rm -rf /", "/tmp/vaf_run_xyz", "/tmp/vaf_run_ab12cd/..",
                "/home/user/vaf_run_ab12cd"):
        with pytest.raises(ValueError):
            m.start_process("scope-alice", env.id, "x", session_id="chat-1", cleanup_dir=bad)
    assert fake.detached == [], "a refused folder starts nothing"

    def _removed():
        return [c[-1] for c in fake.calls if c[:1] == ["exec"] and c[-3:-1] == ["rm", "-rf"]]

    h1 = m.start_process("scope-alice", env.id, "python3 -u main.py", session_id="chat-1",
                         cleanup_dir="/tmp/vaf_run_ab12cd34ef56")
    m._watch_processes(env, now=10.0)
    assert _removed() == [], "not while it runs"
    _ended(fake, h1)
    m._watch_processes(env, now=20.0)
    assert _removed() == ["/tmp/vaf_run_ab12cd34ef56"] and len(woken) == 1
    h2 = m.start_process("scope-alice", env.id, "python3 -u main.py", session_id="chat-1",
                         cleanup_dir="/tmp/vaf_run_0011223344aa")
    m.stop_process("scope-alice", h2)
    assert _removed()[-1] == "/tmp/vaf_run_0011223344aa"
    m.start_process("scope-alice", env.id, "sleep 1", session_id="chat-1")
    _ended(fake, m.processes("scope-alice")[-1]["handle"])
    before = len(_removed())
    m._watch_processes(env, now=30.0)
    assert len(_removed()) == before, "no folder, nothing removed"


def test_starts_at_the_same_moment_cannot_pass_the_cap_together(procs, monkeypatch):
    """Three starts meet after the count (a barrier inside the status read), and docker takes
    its time to answer a start: with the count and a placeholder in one step under the state
    lock, only one gets the last place. MUTATION: count outside the lock and write the
    record after docker answered (the first version), or do not count the placeholder -
    two or three pass, red."""
    import threading
    import time as _t
    from vaf.core.processes import MAX_PER_CHAT
    m, env, fake, _ = procs
    for i in range(MAX_PER_CHAT - 1):
        m.start_process("scope-alice", env.id, f"sleep {i}", session_id="chat-1")

    def _slow(args, timeout=60, **kw):
        if args[:2] == ["exec", "-d"]:
            _t.sleep(0.3)                  # the start is not running yet while others count
        return fake(args, timeout=timeout, **kw)

    monkeypatch.setattr(containers, "docker", _slow)
    barrier = threading.Barrier(3, timeout=1.0)
    real = m._proc_status

    def _status(e):
        out = real(e)
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return out

    monkeypatch.setattr(m, "_proc_status", _status)
    results = []

    def _start(n):
        try:
            results.append(m.start_process("scope-alice", env.id, f"job {n}", session_id="chat-1"))
        except envmod.EnvironmentRefused as e:
            results.append(e)

    threads = [threading.Thread(target=_start, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    started = [r for r in results if isinstance(r, str)]
    assert len(results) == 3 and len(started) == 1, results
    assert len(fake.alive) == MAX_PER_CHAT


def test_a_start_docker_refuses_leaves_no_record(procs, monkeypatch):
    """The placeholder that reserved the place goes when `docker exec -d` fails. MUTATION:
    keep the placeholder - it counts as running and a record is left, red."""
    m, env, fake, _ = procs

    def _refusing(args, timeout=60, **kw):
        if args[:2] == ["exec", "-d"]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="no such container")
        return fake(args, timeout=timeout, **kw)

    monkeypatch.setattr(containers, "docker", _refusing)
    with pytest.raises(envmod.EnvironmentRefused) as e:
        m.start_process("scope-alice", env.id, "sleep 9", session_id="chat-1")
    assert "no such container" in str(e.value)
    assert m._read_procs(env.id) == {}


def test_a_stale_placeholder_stops_counting(procs):
    """A start that crashed between its placeholder and docker's answer must not hold a
    place for ever. MUTATION: count every "starting" record whatever its age - red."""
    from vaf.core.processes import MAX_PER_CHAT
    m, env, fake, _ = procs
    m._write_procs(env.id, {f"p{i:08x}": {"command": "x", "starting": True, "started": 1.0}
                            for i in range(MAX_PER_CHAT)})
    m.start_process("scope-alice", env.id, "sleep 9", session_id="chat-1")


def test_the_limit_holds_for_a_process_another_live_process_watches(procs):
    """The max-hours kill does not wait for a watcher that may have stopped looking (a pid
    reused after a crash, a dead watcher thread). The report is left to the watcher for that
    look and taken by whoever looks next. MUTATION: skip a record watched elsewhere before
    the limit check (the first version) - no kill, red."""
    import os
    m, env, fake, woken = procs
    h = m.start_process("scope-alice", env.id, "python3 -u main.py", session_id="chat-1")
    proc = h.split("-")[-1]
    recs = m._read_procs(env.id)
    recs[proc].update(watcher_pid=os.getppid(), started=1.0)
    m._write_procs(env.id, recs)
    m._watch_processes(env, now=10 * 3600.0)
    assert proc not in fake.alive, "ended at the limit"
    assert woken == [], "its report is its watcher's on this look"
    assert m._read_procs(env.id)[proc]["ended_at_limit"] is True
    fake.exits[proc] = "137"
    m._watch_processes(env, now=10 * 3600.0 + 60)
    assert len(woken) == 1
    assert "ended after 8 h, the longest a background command may run" in woken[0]["text"]
    assert "out of memory" not in woken[0]["text"]


def test_the_cleanup_folder_is_matched_whole():
    """A trailing newline passed `$`. MUTATION: match() instead of fullmatch() - red."""
    assert envmod._CLEANUP_RE.fullmatch("/tmp/vaf_run_abcdef")
    assert not envmod._CLEANUP_RE.fullmatch("/tmp/vaf_run_abcdef\n")
    with pytest.raises(ValueError):
        envmod.EnvironmentManager().start_process("scope-alice", "0a1b2c3d", "x",
                                                  cleanup_dir="/tmp/vaf_run_abcdef\n")


# -- round 2: stop during start, a failed limit kill, the lock, a start that never ran ------

def test_a_stop_during_startup_ends_the_process(procs, monkeypatch):
    """The handle is listed from the placeholder on, so a stop can come while docker is
    still starting it: nothing to kill yet, and the record is marked reported. MUTATION: do
    not recheck `stopped` after docker answered - the process runs on unreported and past
    every limit, red."""
    import threading
    m, env, fake, woken = procs
    gate, entered = threading.Event(), threading.Event()

    def _held(args, timeout=60, **kw):
        if args[:2] == ["exec", "-d"]:
            entered.set()
            gate.wait(10)
        return fake(args, timeout=timeout, **kw)

    monkeypatch.setattr(containers, "docker", _held)
    out = []

    def _start():
        try:
            out.append(m.start_process("scope-alice", env.id, "sleep 999", session_id="chat-1"))
        except envmod.EnvironmentRefused as e:
            out.append(e)

    t = threading.Thread(target=_start)
    t.start()
    assert entered.wait(10)
    [row] = m.processes("scope-alice")
    assert row["state"] == "starting"
    m.stop_process("scope-alice", row["handle"])
    gate.set()
    t.join(10)
    assert isinstance(out[0], envmod.EnvironmentRefused) and "stopped while it was starting" in str(out[0])
    proc = row["handle"].split("-")[-1]
    assert proc not in fake.alive, "ended once docker had started it"
    assert m._read_procs(env.id)[proc]["notified"] is True and woken == []


def test_a_stop_during_startup_whose_kill_fails_stays_tracked(procs, monkeypatch):
    """MUTATION: leave it reported, or leave it marked stopped, when the kill failed - red."""
    import threading
    m, env, fake, _ = procs
    monkeypatch.delenv("VAF_SANDBOX_ENV_HOUSEKEEPING_OFF", raising=False)
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    monkeypatch.setattr(m, "_start_watch_thread", lambda: None)
    monkeypatch.setattr(m, "_docker_rows", lambda owner=None, strict=False: [
        {"container": env.container, "state": "running", "id": env.id, "owner": env.owner,
         "kind": env.kind, "network": env.network}])
    monkeypatch.setattr(m, "_from_row", lambda row: env)
    gate, entered = threading.Event(), threading.Event()

    def _held(args, timeout=60, **kw):
        if args[:2] == ["exec", "-d"]:
            entered.set()
            gate.wait(10)
        if args[0] == "exec" and "kill" in args[-1]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="busy")
        return fake(args, timeout=timeout, **kw)

    monkeypatch.setattr(containers, "docker", _held)
    out = []
    t = threading.Thread(target=lambda: out.append(
        m.start_process("scope-alice", env.id, "sleep 999", session_id="chat-1")))
    t.start()
    assert entered.wait(10)
    [row] = m.processes("scope-alice")
    m.stop_process("scope-alice", row["handle"])
    gate.set()
    t.join(10)
    proc = row["handle"].split("-")[-1]
    rec = m._read_procs(env.id)[proc]
    assert proc in fake.alive and rec["notified"] is False
    assert rec["stopped"] is False, "it runs, so it reads as running (the watcher keeps it)"
    assert out == [row["handle"]], "the start answers as a start that worked"
    [row] = m.processes("scope-alice")
    assert row["state"] == "running"
    m._watch_tick(now=200.0)
    assert proc in m._watched[env.id]["procs"], "the watcher keeps it"


def test_an_overdue_process_is_reported_only_once_it_ended(procs, monkeypatch):
    """A kill at the limit that fails, or one the process survives, claims nothing: the
    next look tries again. MUTATION: claim after the kill without checking it, or ignore
    the kill's own answer - red."""
    m, env, fake, woken = procs
    h = m.start_process("scope-alice", env.id, "python3 -u main.py", session_id="chat-1")
    proc = h.split("-")[-1]
    recs = m._read_procs(env.id)
    recs[proc]["started"] = 1.0
    m._write_procs(env.id, recs)
    for kill_rc in (1, 0):                              # refused; "worked" but it survived
        def _kill(args, timeout=60, rc=kill_rc, **kw):
            if args[0] == "exec" and "kill" in args[-1]:
                return types.SimpleNamespace(returncode=rc, stdout="", stderr="")
            return fake(args, timeout=timeout, **kw)
        monkeypatch.setattr(containers, "docker", _kill)
        m._watch_processes(env, now=10 * 3600.0)
        rec = m._read_procs(env.id)[proc]
        assert woken == [] and not rec.get("notified") and not rec.get("ended_at_limit"), kill_rc
    # A refused kill and a status read that then fails: an unreadable status reads as
    # "nothing runs", so only the kill's own answer keeps it from being claimed.
    reads = []

    def _refused_then_blind(args, timeout=60, **kw):
        if args[0] == "exec" and "kill" in args[-1]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        if args[0] == "exec" and "alive" in args[-1]:
            reads.append(1)
            if len(reads) > 1:
                raise RuntimeError("docker did not answer")
        return fake(args, timeout=timeout, **kw)

    monkeypatch.setattr(containers, "docker", _refused_then_blind)
    m._watch_processes(env, now=10 * 3600.0)
    assert woken == [] and not m._read_procs(env.id)[proc].get("ended_at_limit")
    monkeypatch.setattr(containers, "docker", fake)
    m._watch_processes(env, now=10 * 3600.0)
    assert proc not in fake.alive and len(woken) == 1
    assert "ended after 8 h" in woken[0]["text"]


def test_the_status_read_is_outside_the_lock_and_the_cap_still_holds(procs, monkeypatch):
    """The status read (a docker exec, up to 20 s) runs before the state lock. A start that
    docker answered after that read counts as running whatever the read saw. MUTATION:
    read under the lock again (the first assert), or drop the running_since rule - a start
    finished between the read and the count is missed and nine run, red."""
    from vaf.core.processes import MAX_PER_CHAT
    m, env, fake, _ = procs
    for i in range(MAX_PER_CHAT - 1):
        m.start_process("scope-alice", env.id, f"sleep {i}", session_id="chat-1")
    real = m._proc_status
    nested = []

    def _status(e):
        assert not m._thread_lock._is_owned(), "a docker exec under the state lock"
        seen = real(e)
        if not nested:                          # another start runs to its end in between
            nested.append(None)
            nested[0] = m.start_process("scope-alice", env.id, "other", session_id="chat-1")
        return seen

    monkeypatch.setattr(m, "_proc_status", _status)
    with pytest.raises(envmod.EnvironmentRefused):
        m.start_process("scope-alice", env.id, "late", session_id="chat-1")
    assert len(nested) == 1 and len(fake.alive) == MAX_PER_CHAT


def test_a_placeholder_that_never_ran_is_removed_on_the_next_look(procs):
    """MUTATION: leave a never-started placeholder in place - it is listed for ever, red."""
    m, env, fake, woken = procs
    m._write_procs(env.id, {"p0011aabb": {"command": "x", "session_id": "chat-1",
                                          "starting": True, "started": 1.0, "notified": False,
                                          "cleanup": "/tmp/vaf_run_0011aabbccdd"}})
    [row] = m.processes("scope-alice")
    assert row["state"] == "did not start"
    m._watch_processes(env, now=100.0)
    assert m._read_procs(env.id) == {} and woken == []
    assert ["exec", env.container, "rm", "-rf", "/tmp/vaf_run_0011aabbccdd"] in fake.calls


def test_a_file_lock_that_fails_releases_the_thread_lock(tmp_path, monkeypatch):
    """MUTATION: no release in __enter__ - the next operation waits for good, red."""
    import threading
    m = envmod.EnvironmentManager(state_dir=tmp_path)

    class _Timeout:
        def acquire(self):
            raise TimeoutError("lock held elsewhere")

    monkeypatch.setattr(m, "_file_lock", lambda: _Timeout())
    with pytest.raises(TimeoutError):
        with m._locked():
            pass
    got = []
    t = threading.Thread(target=lambda: got.append(m._thread_lock.acquire(timeout=1)))
    t.start()
    t.join(5)
    assert got == [True]



def test_a_stop_whose_kill_ran_before_docker_answered_still_ends_it(procs, monkeypatch):
    """The stop's kill finds nothing (docker has not started the process yet), docker then
    answers, and only after that does the stop finish. Because the stop marks `stopped`
    BEFORE its kill, the start sees it when docker answers. MUTATION: mark after the kill
    (the earlier order) - the start returns a handle and the process runs untracked, red."""
    import threading
    m, env, fake, woken = procs
    exec_gate, exec_entered = threading.Event(), threading.Event()
    kill_gate, kill_ran = threading.Event(), threading.Event()
    kills = []

    def _held(args, timeout=60, **kw):
        if args[:2] == ["exec", "-d"]:
            exec_entered.set()
            exec_gate.wait(10)
        out = fake(args, timeout=timeout, **kw)
        if args[0] == "exec" and "kill" in args[-1]:
            kills.append(1)
            if len(kills) == 1:                    # the stop's own kill: done, then held
                kill_ran.set()
                kill_gate.wait(10)
        return out

    monkeypatch.setattr(containers, "docker", _held)
    started = []
    ts = threading.Thread(target=lambda: started.append(_try_start(m, env)))
    ts.start()
    assert exec_entered.wait(10)
    [row] = m.processes("scope-alice")
    tk = threading.Thread(target=lambda: m.stop_process("scope-alice", row["handle"]))
    tk.start()
    assert kill_ran.wait(10)
    exec_gate.set()
    ts.join(10)
    kill_gate.set()
    tk.join(10)
    proc = row["handle"].split("-")[-1]
    assert isinstance(started[0], envmod.EnvironmentRefused)
    assert proc not in fake.alive and woken == []


def test_a_start_whose_placeholder_was_removed_meanwhile_does_not_run_on(procs, monkeypatch):
    """A slow start: a look removes its placeholder as a start that never ran while docker is
    still answering. MUTATION: return the handle when the record is gone (the round-2
    version) - an unlisted process runs past the cap and the limit, red."""
    import threading
    import time as _t
    m, env, fake, woken = procs
    gate, entered = threading.Event(), threading.Event()

    def _held(args, timeout=60, **kw):
        if args[:2] == ["exec", "-d"]:
            entered.set()
            gate.wait(10)
        return fake(args, timeout=timeout, **kw)

    monkeypatch.setattr(containers, "docker", _held)
    out = []
    t = threading.Thread(target=lambda: out.append(_try_start(m, env, cleanup="/tmp/vaf_run_00aa11bb22cc")))
    t.start()
    assert entered.wait(10)
    m._watch_processes(env, now=_t.time() + 120)          # past the grace: removed
    assert m._read_procs(env.id) == {}
    gate.set()
    t.join(10)
    assert isinstance(out[0], envmod.EnvironmentRefused) and "given up" in str(out[0])
    assert fake.alive == set() and m.processes("scope-alice") == [] and woken == []


def test_the_grace_counts_from_the_reservation(procs, monkeypatch):
    """MUTATION: keep the time stamped before the status read - a slow read eats the grace,
    red."""
    import time as _t
    m, env, fake, _ = procs
    m.start_process("scope-alice", env.id, "sleep 1", session_id="chat-1")
    real = m._proc_status

    def _slow(e):
        _t.sleep(0.3)
        return real(e)

    monkeypatch.setattr(m, "_proc_status", _slow)
    before = _t.time()
    h = m.start_process("scope-alice", env.id, "sleep 2", session_id="chat-1")
    assert m._read_procs(env.id)[h.split("-")[-1]]["started"] >= before + 0.3


def _try_start(m, env, cleanup=None):
    try:
        return m.start_process("scope-alice", env.id, "sleep 999", session_id="chat-1",
                               cleanup_dir=cleanup)
    except envmod.EnvironmentRefused as e:
        return e


def test_a_look_during_a_stop_does_not_report_its_kill(procs):
    """Between the stop's `stopped` mark and its `notified` mark the process is killed: a
    look in that moment must not report the kill as an exit. MUTATION: drop the `stopped`
    skip from the looks - exit 137 is reported, red."""
    m, env, fake, woken = procs
    h = m.start_process("scope-alice", env.id, "sleep 999", session_id="chat-1")
    proc = h.split("-")[-1]
    recs = m._read_procs(env.id)
    recs[proc]["stopped"] = True                           # the stop's first step
    m._write_procs(env.id, recs)
    fake.alive.discard(proc)
    fake.exits[proc] = "137"                               # its kill
    m._watch_processes(env, now=100.0)
    assert woken == []


def test_a_stop_does_not_overwrite_a_start_that_took_its_process_back(procs, monkeypatch):
    """Ordering: the stop marks, its kill finds nothing (docker has not started it), docker
    answers, the start sees the mark and its own kill fails, so it takes the record back as
    running; only then does the stop finish. The stop must not report the record now, or
    the process runs on with no limit and no report. MUTATION: report unconditionally in
    the stop's second step - red."""
    import threading
    import time as _t
    m, env, fake, woken = procs
    exec_gate, exec_entered = threading.Event(), threading.Event()
    m2_gate, stop_kill_ran = threading.Event(), threading.Event()
    kills = []

    def _docker(args, timeout=60, **kw):
        if args[:2] == ["exec", "-d"]:
            exec_entered.set()
            exec_gate.wait(10)
        if args[0] == "exec" and "kill" in args[-1]:
            kills.append(1)
            if len(kills) == 1:                    # the stop's kill: nothing to kill yet
                out = fake(args, timeout=timeout, **kw)
                stop_kill_ran.set()
                m2_gate.wait(10)
                return out
            if len(kills) == 2:                    # the start's kill fails
                return types.SimpleNamespace(returncode=1, stdout="", stderr="busy")
        return fake(args, timeout=timeout, **kw)

    monkeypatch.setattr(containers, "docker", _docker)
    started = []
    ts = threading.Thread(target=lambda: started.append(_try_start(m, env)))
    ts.start()
    assert exec_entered.wait(10)
    [row] = m.processes("scope-alice")
    tk = threading.Thread(target=lambda: m.stop_process("scope-alice", row["handle"]))
    tk.start()
    assert stop_kill_ran.wait(10)
    exec_gate.set()
    ts.join(10)
    m2_gate.set()
    tk.join(10)
    proc = row["handle"].split("-")[-1]
    assert started == [row["handle"]]
    rec = m._read_procs(env.id)[proc]
    assert proc in fake.alive and rec["stopped"] is False and rec["notified"] is False
    m._watch_processes(env, now=_t.time() + 10 * 3600)
    assert proc not in fake.alive, "the limit still ends it"
    assert len(woken) == 1 and "ended after 8 h" in woken[0]["text"]


def test_a_stop_whose_kill_raises_leaves_the_process_tracked(procs, monkeypatch):
    """A docker that times out on the kill: the process runs on, so the record must not stay
    marked as being stopped (every look would skip it). MUTATION: do not clear `stopped`
    when the kill raises - red."""
    import subprocess
    import time as _t
    m, env, fake, woken = procs
    h = m.start_process("scope-alice", env.id, "sleep 999", session_id="chat-1")
    proc = h.split("-")[-1]

    def _timeout(args, timeout=60, **kw):
        if args[0] == "exec" and "kill" in args[-1]:
            raise subprocess.TimeoutExpired(args, timeout)
        return fake(args, timeout=timeout, **kw)

    monkeypatch.setattr(containers, "docker", _timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        m.stop_process("scope-alice", h)
    rec = m._read_procs(env.id)[proc]
    assert rec["stopped"] is False and rec["notified"] is False
    assert m.processes("scope-alice")[0]["state"] == "running"
    monkeypatch.setattr(containers, "docker", fake)
    m._watch_processes(env, now=_t.time() + 10 * 3600)
    assert proc not in fake.alive and len(woken) == 1
