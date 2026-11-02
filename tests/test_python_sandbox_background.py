# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""python_sandbox as simple as a coding agent's shell tool: unbuffered output, what a run
printed survives its timeout, a failure shows its output, and background=true starts long
work through EnvironmentManager.start_process and returns at once.

Docker is never asked: a fake manager stands in for the scratch environment, and the one
test that runs the tool's real command runs it on this host. The mutation that turns each
test red is named in its docstring."""
import os
import re
import signal
import subprocess
import types
from pathlib import Path

import pytest

import vaf.core.environments as envmod
from vaf.tools.python_sandbox import PythonSandboxTool

HANDLE = "e-s-ab12cd34ef56-p1a2b3c4d"
SCRATCH = types.SimpleNamespace(id="s-ab12cd34ef56", container="vaf-env-ab12cd34ef56-scratch")


def _is_main_run(argv):
    return argv[:2] == ["sh", "-c"] and "base64 -d | python3" in argv[-1]


class _Mgr:
    """The scratch environment as the tool sees it: every call recorded, the main run's
    result scripted."""

    def __init__(self, main=None, start_error=None):
        self.main = main or envmod.ExecResult(0, "", "")
        self.start_error = start_error
        self.calls = []

    def scratch_for(self, scope):
        self.calls.append(("scratch_for", (scope,), {}))
        return SCRATCH

    def exec_in(self, env, argv, **kw):
        self.calls.append(("exec_in", (list(argv),), kw))
        return self.main if _is_main_run(argv) else envmod.ExecResult(0, "", "")

    def start_process(self, scope, env_id, command, **kw):
        self.calls.append(("start_process", (scope, env_id, command), kw))
        if self.start_error:
            raise self.start_error
        return HANDLE

    def named(self, name):
        return [c for c in self.calls if c[0] == name]

    def removed(self):
        return [c[1][0][2] for c in self.named("exec_in") if c[1][0][:2] == ["rm", "-rf"]]


@pytest.fixture
def sandbox(monkeypatch):
    """Install a fake manager; returns a function that runs the tool against one."""
    monkeypatch.setattr(PythonSandboxTool, "_ensure_docker_available",
                        staticmethod(lambda: (True, "")))
    monkeypatch.setenv("VAF_SANDBOX_ENV_PROCESS_MAX_HOURS", "8")
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    import vaf.core.task_queue as tq
    monkeypatch.setattr(tq, "_wake_runners", {"test"})          # as in the web runner
    state = {}

    def _run(mgr=None, **kwargs):
        mgr = mgr or _Mgr()
        state["mgr"] = mgr
        monkeypatch.setattr(envmod, "get_environment_manager", lambda: mgr)
        kwargs.setdefault("user_scope_id", "scope-alice")
        return PythonSandboxTool().run(**kwargs), mgr
    return _run


# -- AC1: unbuffered, and a killed run keeps what it printed ---------------------------

def test_every_python_the_sandbox_starts_is_unbuffered():
    """MUTATION: drop PYTHONUNBUFFERED=1 from _run_env_prefix - red."""
    assert "PYTHONUNBUFFERED=1" in PythonSandboxTool._run_env_prefix("/tmp/vaf_run_ab12cd")
    assert "PYTHONUNBUFFERED=1" in PythonSandboxTool._run_env_prefix(
        "/tmp/vaf_run_ab12cd", extra_pythonpath="/tmp/vaf_run_ab12cd")


@pytest.mark.skipif(os.name == "nt", reason="runs the tool's sh command on a POSIX host")
def test_a_killed_run_keeps_what_it_printed(sandbox, tmp_path):
    """The live incident's shape: a loop that prints without flush, killed at its limit.
    The tool's REAL command runs on this host, its run folder moved into tmp_path, in a
    process group of its own that is SIGKILLed when the time is up, as `timeout -s KILL`
    does in the container. MUTATION: drop PYTHONUNBUFFERED=1 from _run_env_prefix - the
    three lines are lost in Python's buffer, red."""

    class _HostMgr(_Mgr):
        def exec_in(self, env, argv, **kw):
            self.calls.append(("exec_in", (list(argv),), kw))
            argv = [a.replace("/tmp/vaf_run_", f"{tmp_path}/vaf_run_") for a in argv]
            env_values = {**os.environ, **(kw.get("env_values") or {})}
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, start_new_session=True, env=env_values)
            try:
                out, err = proc.communicate(timeout=min(float(kw.get("timeout") or 2), 2.0))
                return envmod.ExecResult(proc.returncode, out, err)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                out, err = proc.communicate(timeout=10)
                return envmod.ExecResult(-1, out, err, timed_out=True)

    code = ("import time\n"
            "for i in range(3):\n"
            "    print('line', i)\n"
            "time.sleep(30)\n")
    out, _ = sandbox(_HostMgr(), code=code, timeout=2, session_id="chat-1")
    assert out.startswith("[timed out after 2 s]"), out
    for i in range(3):
        assert f"line {i}" in out, out


# -- AC2-AC5: the answers ------------------------------------------------------------------

def _timed_out(stdout="line 0\nline 1\n", stderr="warn\n"):
    # What exec_bounded's backstop returns: its own note appended to the code's stderr.
    return envmod.ExecResult(-1, stdout, f"{stderr}timed out after 30s", timed_out=True)


def test_a_timeout_answers_with_the_output_so_far_and_the_hint(sandbox):
    """MUTATION: restore `stderr or stdout` (stdout lost); drop the hint; offer background
    on a channel - red."""
    out, _ = sandbox(_Mgr(_timed_out()), code="print(1)", timeout=30, session_id="chat-1")
    lines = out.splitlines()
    assert lines[0] == "[timed out after 30 s] The code was stopped before it finished."
    assert lines[1:4] == ["Output so far:", "line 0", "line 1"]
    assert lines[4:6] == ["[stderr]", "warn"], "the code's stderr, without the executor's note"
    assert "timed out after 30s" not in out and "Execution timed out" not in out
    assert "export_files" not in out
    assert "larger timeout (at most 600)" in lines[-1] and "background=true" in lines[-1]
    assert lines[-1].index("larger timeout") < lines[-1].index("background=true"), \
        "a short run is offered more time first"

    out, _ = sandbox(_Mgr(_timed_out()), code="print(1)", timeout=120, session_id="chat-1")
    last = out.splitlines()[-1]
    assert last.startswith("For long work, call python_sandbox again with background=true"), \
        "a run that already had the default is offered background first"
    assert "larger timeout (at most 600)" in last

    out, _ = sandbox(_Mgr(_timed_out()), code="print(1)", timeout=600, session_id="chat-1")
    last = out.splitlines()[-1]
    assert "larger timeout" not in out, "600 s is the most a run may ask for"
    assert last.startswith("For work longer than 600 s") and "background=true" in last

    out, _ = sandbox(_Mgr(_timed_out()), code="print(1)", timeout=600,
                     session_id="telegram_9001")
    assert out.splitlines()[-1] == "Split the work into shorter runs."
    assert "background" not in out, "never offered where it is refused"
    out, _ = sandbox(_Mgr(_timed_out()), code="print(1)", timeout=30,
                     session_id="telegram_9001")
    assert "background" not in out and "larger timeout" in out.splitlines()[-1]

    out, _ = sandbox(_Mgr(_timed_out("", "")), code="print(1)", timeout=30,
                     session_id="chat-1", export_files=["chart.png"])
    lines = out.splitlines()
    assert lines[1:3] == ["Output so far:", "(nothing was printed)"]
    assert "[stderr]" not in out
    assert lines[-2] == "export_files were not copied: the run did not finish."
    assert "background" not in lines[-1], "export_files cannot run in the background"


def test_a_stop_answers_with_the_output_so_far_and_no_hint(sandbox):
    """MUTATION: use the timeout wording for a cancel - red."""
    stopped = envmod.ExecResult(-1, "step 1\n", "cancelled by stop request", cancelled=True)
    out, _ = sandbox(_Mgr(stopped), code="print(1)", session_id="chat-1")
    assert out.splitlines() == ["[cancelled by stop request] The code was stopped before it "
                                "finished.", "Output so far:", "step 1"]


def test_the_timeout_answer_does_not_trip_the_retry_blocker(sandbox):
    """The chat loop refuses ANY next call of a tool whose last result reads as a failure
    (vaf/core/agent.py, the inline check before "Blocked retry of failed tool": "error:",
    "failed" with "tool" or "execution", a leading "error"). The model's next call is the
    one with background=true, so the answers to a timeout and a Stop must not read so.
    MUTATION: answer a timeout with the "[ERROR] Sandbox execution failed" lead - red."""
    stopped = envmod.ExecResult(-1, "step 1\n", "", cancelled=True)
    for result in (_timed_out(), stopped):
        out, _ = sandbox(_Mgr(result), code="print(1)", session_id="chat-1")
        low = out.lower()
        assert "error:" not in low
        assert not ("failed" in low and ("tool" in low or "execution" in low))
        assert not low.startswith("error") and "error executing tool" not in low
    from vaf.core.context import tool_result_is_error
    assert tool_result_is_error(out), "still a failure for the summarizer and the UI"


def test_a_failed_run_shows_stdout_and_stderr(sandbox):
    """MUTATION: `stderr or stdout` - red."""
    failed = envmod.ExecResult(1, "loaded 3 rows\n",
                               "Traceback (most recent call last):\nZeroDivisionError\n")
    out, _ = sandbox(_Mgr(failed), code="print(1)", session_id="chat-1")
    assert out.splitlines() == ["[ERROR] Sandbox execution failed (exit=1):", "loaded 3 rows",
                                "[stderr]", "Traceback (most recent call last):",
                                "ZeroDivisionError"]
    out, _ = sandbox(_Mgr(envmod.ExecResult(2, "", "")), code="print(1)")
    assert out == "[ERROR] Sandbox execution failed (exit=2):\n(no output)"


# -- AC6-AC7: the schema -------------------------------------------------------------------

def test_the_schema_states_default_and_max():
    """MUTATION: default back to 30 - red."""
    t = PythonSandboxTool._run_timeout
    assert PythonSandboxTool.DEFAULT_TIMEOUT_SECONDS == 120
    assert PythonSandboxTool.MAX_TIMEOUT_SECONDS == 600
    assert t({}) == t({"timeout": 0}) == t({"timeout": "soon"}) == 120
    timeout = PythonSandboxTool.parameters["properties"]["timeout"]
    assert timeout["default"] == 120
    assert "Default 120, max 600" in timeout["description"]
    assert "120 s, max 600" in PythonSandboxTool.description


def test_the_schema_is_small_and_names_background():
    """MUTATION: rename or remove `background`; make it required - red."""
    params = PythonSandboxTool.parameters
    assert params["required"] == ["code"]
    props = params["properties"]
    assert list(props)[:3] == ["code", "timeout", "background"]
    bg = props["background"]
    assert bg["type"] == "boolean" and bg["default"] is False
    assert "8 hours" in bg["description"]
    assert "not with background=true" in props["with_vaf_tools"]["description"]
    assert "not with background=true" in props["export_files"]["description"]
    assert [e for e in PythonSandboxTool.input_examples if e.get("background") is True]
    assert "background=true" in PythonSandboxTool.description


# -- AC8-AC11: background -----------------------------------------------------------------

def test_background_starts_and_returns_at_once(sandbox):
    """MUTATION: run the foreground path; put the code into the command; remove the
    workdir in the finally on this path - red."""
    code = "\nimport subprocess\nfor i in range(10):\n    print('done', i)\n"
    out, mgr = sandbox(code=code, background=True, packages=["requests"], session_id="chat-1",
                       username="alice", user_role="user")
    assert out.splitlines()[0] == f"[started in the background] {HANDLE}"
    assert "nothing to wait for" in out and "8 h" in out
    [(_, (scope, env_id, command), kw)] = mgr.named("start_process")
    assert (scope, env_id) == ("scope-alice", SCRATCH.id)
    workdir = kw["cwd"]
    assert re.fullmatch(r"/tmp/vaf_run_[0-9a-f]{12}", workdir)
    assert kw["cleanup_dir"] == workdir
    assert kw["session_id"] == "chat-1" and kw["username"] == "alice" and kw["user_role"] == "user"
    assert kw["label"] == "python_sandbox: import subprocess"
    assert "subprocess" not in command, "the code goes in on stdin, never in the command"
    assert "PYTHONUNBUFFERED=1" in command and command.endswith("python3 -u main.py")
    assert f"--target {workdir}/_pkgs requests && " in command
    [write] = [c for c in mgr.named("exec_in") if c[2].get("input_text") is not None]
    assert write[2]["input_text"] == code.strip() and f"{workdir}/main.py" in write[1][0][-1]
    assert not any(_is_main_run(c[1][0]) for c in mgr.named("exec_in")), "nothing in the foreground"
    assert mgr.removed() == [], "the folder is the process's now"
    out, mgr = sandbox(_Mgr(envmod.ExecResult(0, "ok\n", "")), code="print(1)",
                       background="false", session_id="chat-1")
    assert out == "ok" and mgr.named("start_process") == [], "the string 'false' means no"


@pytest.mark.parametrize("case", ["with_vaf_tools", "export_files", "no_chat", "channel",
                                  "sub_agent", "training", "terminal_lane"])
def test_background_is_refused_where_it_cannot_report_back(sandbox, monkeypatch, case):
    """Refused before anything is created or run. MUTATION: delete any one refusal - its
    parameter goes red."""
    kwargs = {"code": "print(1)", "background": True, "session_id": "chat-1"}
    if case == "with_vaf_tools":
        kwargs["with_vaf_tools"] = True
    elif case == "export_files":
        kwargs["export_files"] = ["chart.png"]
    elif case == "no_chat":
        kwargs["session_id"] = None
    elif case == "channel":
        kwargs["session_id"] = "telegram_9001"
    elif case == "sub_agent":
        monkeypatch.setenv("VAF_IN_SUBAGENT_TERMINAL", "1")
    elif case == "training":
        kwargs["_agent"] = types.SimpleNamespace(_ww_training=True)
    elif case == "terminal_lane":
        import vaf.core.task_queue as tq
        monkeypatch.setattr(tq, "_wake_runners", set())     # `vaf run --web` declares nothing
    out, mgr = sandbox(**kwargs)
    assert out.startswith("[BLOCKED]") and "Nothing ran" in out, out
    assert mgr.calls == [], "no scratch_for, no exec_in, no start_process"


def test_background_hands_host_process_over(sandbox, monkeypatch):
    """MUTATION: remove the advise_tools call; name host_process when it was not accepted
    - red."""
    import vaf.core.tool_dispatch as td
    asked = []

    def _advise(answer):
        def _f(*names):
            asked.append(names)
            return answer
        return _f

    for answer, named in ((("host_process",), True), ((), False), (None, True)):
        monkeypatch.setattr(td, "advise_tools", _advise(answer))
        out, _ = sandbox(code="print(1)", background=True, session_id="chat-1")
        assert ('host_process(action="stop"' in out) is named, (answer, out)
    assert asked == [("host_process",)] * 3


def test_a_full_sandbox_answers_blocked_and_leaves_no_folder(sandbox):
    """start_process refuses past the cap; the run folder goes. MUTATION: drop the cleanup
    on a refused start - red."""
    refused = envmod.EnvironmentRefused("this sandbox already runs 8 background commands "
                                        "(the limit is 8); stop one first")
    out, mgr = sandbox(_Mgr(start_error=refused), code="print(1)", background=True,
                       session_id="chat-1")
    assert out.startswith("[BLOCKED] this sandbox already runs 8") and out.endswith("Nothing ran.")
    [(_, _, kw)] = mgr.named("start_process")
    assert mgr.removed() == [kw["cwd"]]


# -- AC17: one "where can it report back" check --------------------------------------------

def test_background_refusal_reasons(monkeypatch):
    """MUTATION: re-inline the check in host_bash - the source check is red; drop the
    wake-runner check - the terminal case is red."""
    from vaf.core import processes, task_queue
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    monkeypatch.setattr(task_queue, "_wake_runners", {"headless-1"})
    assert processes.background_refusal("chat-1") is None
    assert "has none" in processes.background_refusal("")
    assert "has none" in processes.background_refusal(None)
    assert "messaging-channel" in processes.background_refusal("telegram_9001")
    assert "messaging-channel" in processes.background_refusal("chat-1", source="telegram")
    monkeypatch.setattr(task_queue, "_wake_runners", set())
    assert "this terminal does not run" in processes.background_refusal("chat-1")
    task_queue.declare_wake_runner("tui")
    assert processes.background_refusal("chat-1") is None
    task_queue.withdraw_wake_runner("tui")
    assert processes.background_refusal("chat-1") is not None
    monkeypatch.setenv("VAF_IN_SUBAGENT_TERMINAL", "1")
    assert "report that the main chat should start it" in processes.background_refusal("chat-1")
    import vaf.tools.host_bash as hb
    src = Path(hb.__file__).read_text(encoding="utf-8")
    assert "background_refusal(" in src
    assert "VAF_IN_SUBAGENT_TERMINAL" not in src and "messaging-channel" not in src


def test_host_bash_background_answers_with_the_shared_reason(monkeypatch):
    from vaf.core.subagent_ipc import session_context
    from vaf.tools.host_bash import HostBashTool
    with session_context("telegram_9001"):
        out = HostBashTool._start_background("sleep 1", {})
    assert out == ("[BLOCKED] nobody could be told when it ends in a messaging-channel chat. "
                   "Run it without background.")


# -- AC18: the declared chat ----------------------------------------------------------------

def test_export_uses_the_declared_session(sandbox, monkeypatch):
    """MUTATION: read `_session_id` in export again - red."""
    seen = []
    monkeypatch.setattr(PythonSandboxTool, "_export_artifacts",
                        lambda self, files, workdir, container, sid: seen.append(sid) or [])
    out, _ = sandbox(_Mgr(envmod.ExecResult(0, "ok\n", "")), code="print(1)",
                     export_files=["a.png"], session_id="chat-42", _session_id="other-chat")
    assert seen == ["chat-42"]
    assert "session_id" in PythonSandboxTool.identity_kwargs
    assert "username" in PythonSandboxTool.identity_kwargs
    assert "user_role" in PythonSandboxTool.identity_kwargs


def test_every_background_lane_refuses_in_a_terminal_that_declines_wakes(sandbox, monkeypatch):
    """`vaf run --web` takes the queue but declines every wake turn (run.py
    _declined_queue_task), so a background job started there would promise a message that
    never comes. host_bash, sandbox_exec and python_sandbox refuse it there alike, and run
    once a lane of the process runs wakes. MUTATION: drop the wake-runner check from
    background_refusal - red."""
    import vaf.core.task_queue as tq
    from vaf.core import processes
    from vaf.core.subagent_ipc import session_context
    from vaf.tools.environments import SandboxExecTool
    from vaf.tools.host_bash import HostBashTool
    monkeypatch.setattr(tq, "_wake_runners", set())
    monkeypatch.setattr(processes, "_registry", {})

    with session_context("chat-1"):
        out = HostBashTool._start_background("sleep 30", {})
    assert out.startswith("[BLOCKED] this terminal does not run the turns VAF starts by itself")
    assert out.endswith("Run it without background.") and processes._registry == {}

    mgr = _Mgr()
    monkeypatch.setattr(envmod, "get_environment_manager", lambda: mgr)
    out = SandboxExecTool().run(environment="0a1b2c3d", command="npm run dev", background=True,
                                user_scope_id="s", session_id="chat-1")
    assert out.startswith("[BLOCKED] this terminal") and "Nothing ran" in out
    assert mgr.calls == []

    out, mgr = sandbox(code="print(1)", background=True, session_id="chat-1")
    assert out.startswith("[BLOCKED] this terminal") and "Nothing ran" in out and mgr.calls == []

    monkeypatch.setattr(tq, "_wake_runners", {"tui"})
    out, mgr = sandbox(code="print(1)", background=True, session_id="chat-1")
    assert out.startswith("[started in the background]")
    out = SandboxExecTool().run(environment="0a1b2c3d", command="npm run dev", background=True,
                                user_scope_id="s", session_id="chat-1")
    assert out.startswith("Started e-")


def test_host_bash_background_without_a_chat_is_refused_now(monkeypatch):
    """Stricter than before: with no chat to report to, host_bash(background=true) used to
    start a command nobody would hear of; it is refused like the other lanes."""
    import vaf.core.task_queue as tq
    from vaf.core import processes
    from vaf.core.subagent_ipc import session_context
    from vaf.tools.host_bash import HostBashTool
    monkeypatch.setattr(tq, "_wake_runners", {"test"})
    monkeypatch.setattr(processes, "_registry", {})
    with session_context(""):
        out = HostBashTool._start_background("sleep 30", {})
    assert out == ("[BLOCKED] a background run needs a chat to report back to, and this call has "
                   "none. Run it without background.")
    assert processes._registry == {}
