# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Background host commands: start detached, read, write, stop - and wake the chat on exit.

The measured gap: a long command (a local test server, an upload, a build) blocked the
agent's whole turn, and anything past the tool's budget was abandoned. The agent it is
modelled on starts such work in the background, keeps working, and is told when it ends.

Pinned here, each boundary on its own:
- a natural exit wakes THE chat that started it, as THAT person, with the end of the output;
- a process the agent stopped wakes nobody (the agent already knows);
- another chat or another person cannot see, read, type into or stop it;
- the log is private (0600 in an owner-only folder) and removed when VAF ends them all;
- the per-chat cap holds;
- stop takes the whole tree, not only the shell;
- the tool refuses where nobody could be woken: a messaging channel, a sub-agent's process.
"""
import os
import sys
import time

import pytest

from vaf.core import processes
from vaf.core.subagent_ipc import session_context
from vaf.core.task_queue import wake_kind

SCOPE = "ab12cd34-0000-4000-8000-000000000001"
OTHER_SCOPE = "ab12cd34-0000-4000-8000-000000000002"
CHAT = "web_chat-1"
PY = f'"{sys.executable}"'


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Logs in a scratch folder, an empty registry, and the wake turns recorded."""
    from vaf.core import task_queue
    from vaf.core.platform import Platform

    monkeypatch.setattr(Platform, "config_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(processes, "_registry", {})
    woken = []
    monkeypatch.setattr(task_queue.TaskQueue, "add",
                        lambda self, **kw: woken.append(kw))
    yield woken
    processes.terminate_all()


def _wait(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _start(command, **kw):
    kw.setdefault("session_id", CHAT)
    kw.setdefault("user_scope_id", SCOPE)
    return processes.start(command, username="tenant", role="user", **kw)


# ── the wake ─────────────────────────────────────────────────────────────────

def test_a_natural_exit_wakes_the_chat_it_came_from_as_its_person(_isolated):
    record = _start(f'{PY} -c "print(\'build ok\')"')
    assert _wait(lambda: _isolated), "the finished command woke nobody"
    wake = _isolated[0]
    assert wake["session_id"] == CHAT
    meta = wake["metadata"]
    assert wake_kind(meta) == "process" and meta["process_id"] == record.id
    assert meta["user_scope_id"] == SCOPE and meta["username"] == "tenant" and meta["role"] == "user"
    assert meta["enqueue_session_id"] == CHAT
    assert "exit 0" in wake["input_text"] and "build ok" in wake["input_text"]


def test_a_process_the_agent_stopped_wakes_nobody(_isolated):
    record = _start(f'{PY} -c "import time; time.sleep(30)"')
    assert "stopped" in processes.stop(record)
    assert not record.running
    time.sleep(0.3)
    assert _isolated == [], "a stop the agent asked for came back as a wake turn"


# ── one chat, one person ─────────────────────────────────────────────────────

def test_another_chat_or_person_cannot_see_it():
    record = _start(f'{PY} -c "import time; time.sleep(30)"')
    assert processes.get(record.id, session_id=CHAT, user_scope_id=SCOPE) is record
    assert processes.get(record.id, session_id="web_chat-2", user_scope_id=SCOPE) is None
    assert processes.get(record.id, session_id=CHAT, user_scope_id=OTHER_SCOPE) is None
    assert processes.list_for(session_id="web_chat-2", user_scope_id=SCOPE) == []
    assert processes.list_for(session_id=CHAT, user_scope_id=OTHER_SCOPE) == []


def test_the_cap_per_chat_holds(monkeypatch):
    monkeypatch.setattr(processes, "MAX_PER_CHAT", 1)
    _start(f'{PY} -c "import time; time.sleep(30)"')
    with pytest.raises(processes.ProcessRefused):
        _start(f'{PY} -c "import time; time.sleep(30)"')
    # Another chat has its own cap.
    _start(f'{PY} -c "import time; time.sleep(30)"', session_id="web_chat-2")


def test_no_chat_no_start():
    with pytest.raises(processes.ProcessRefused):
        _start(f'{PY} -c "pass"', session_id="")


# ── the log, the input, the stop ─────────────────────────────────────────────

@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_the_log_is_private():
    record = _start(f'{PY} -c "print(1)"')
    assert (record.log_path.stat().st_mode & 0o777) == 0o600
    assert (record.log_path.parent.stat().st_mode & 0o777) == 0o700


def test_a_line_written_reaches_the_command_and_its_answer_the_log():
    record = _start(f'{PY} -c "import sys; print(\'got:\' + sys.stdin.readline().strip(), flush=True)"')
    assert processes.write(record, "hello console").startswith("sent to")
    assert _wait(lambda: not record.running)
    assert "got:hello console" in processes.read_tail(record)


def test_stop_takes_the_whole_tree_not_only_the_shell():
    import psutil

    record = _start(f'{PY} -c "import subprocess, sys, time; '
                    f'subprocess.Popen([sys.executable, \'-c\', \'import time; time.sleep(60)\']); '
                    f'time.sleep(60)"')
    # The shell may exec the command in its own place, so the command's child is at least one.
    assert _wait(lambda: len(psutil.Process(record.popen.pid).children(recursive=True)) >= 1)
    tree = [record.popen.pid] + [c.pid for c in psutil.Process(record.popen.pid).children(recursive=True)]
    processes.stop(record)
    assert _wait(lambda: not any(psutil.pid_exists(p) and psutil.Process(p).status() != psutil.STATUS_ZOMBIE
                                 for p in tree)), "a child of the command outlived the stop"


def test_ending_vaf_stops_them_all_and_removes_the_logs():
    a = _start(f'{PY} -c "import time; time.sleep(30)"')
    b = _start(f'{PY} -c "import time; time.sleep(30)"', session_id="web_chat-2")
    assert processes.terminate_all() == 2
    assert _wait(lambda: a.popen.poll() is not None and b.popen.poll() is not None)
    assert not a.log_path.exists() and not b.log_path.exists()


def test_a_long_log_is_read_from_its_end():
    record = _start(f'{PY} -c "print(\'x\' * 50000); print(\'THE END\')"')
    assert _wait(lambda: not record.running)
    tail = processes.read_tail(record, max_chars=500)
    assert "THE END" in tail and len(tail) < 700


# ── the tools ────────────────────────────────────────────────────────────────

def _host_bash(**args):
    from vaf.tools.host_bash import HostBashTool

    args.setdefault("user_scope_id", SCOPE)
    return HostBashTool().run(**args)


def _host_process(**args):
    from vaf.tools.host_process import HostProcessTool

    args.setdefault("user_scope_id", SCOPE)
    return HostProcessTool().run(**args)


def test_host_bash_background_returns_at_once_with_an_id():
    started = time.monotonic()
    with session_context(CHAT):
        out = _host_bash(command=f'{PY} -c "import time; time.sleep(30)"', background=True)
    assert time.monotonic() - started < 5
    assert out.startswith("[HOST BACKGROUND] started p-")
    proc_id = out.split("started ", 1)[1].split()[0]
    with session_context(CHAT):
        assert proc_id in _host_process(action="list")
        assert "stopped" in _host_process(action="stop", id=proc_id)


def test_host_process_refuses_an_id_from_another_person():
    with session_context(CHAT):
        out = _host_bash(command=f'{PY} -c "import time; time.sleep(30)"', background=True)
        proc_id = out.split("started ", 1)[1].split()[0]
        foreign = _host_process(action="stop", id=proc_id, user_scope_id=OTHER_SCOPE)
    assert foreign.startswith("[ERROR]")
    assert processes.list_for(session_id=CHAT, user_scope_id=SCOPE)[0].running


def test_background_is_refused_in_a_messaging_channel_chat():
    with session_context("telegram_9001"):
        out = _host_bash(command=f'{PY} -c "pass"', background=True)
    assert out.startswith("[BLOCKED]") and processes.list_for(session_id="telegram_9001",
                                                             user_scope_id=SCOPE) == []


def test_background_is_refused_inside_a_sub_agent_process(monkeypatch):
    monkeypatch.setenv("VAF_IN_SUBAGENT_TERMINAL", "1")
    with session_context(CHAT):
        out = _host_bash(command=f'{PY} -c "pass"', background=True)
    assert out.startswith("[BLOCKED]")


def test_a_background_start_is_not_held_for_the_command_budget():
    from vaf.tools.host_bash import HostBashTool

    assert HostBashTool().budget_seconds({"background": True, "timeout": 600}) <= 60


# ── the wake vocabulary ──────────────────────────────────────────────────────

def test_the_wake_kind_is_read_from_metadata_never_from_text():
    assert wake_kind({"wake": "process"}) == "process"
    assert wake_kind({"timer": True}) == "timer"      # what timers have always carried
    assert wake_kind({"wake": "somethingelse"}) is None
    assert wake_kind({}) is None and wake_kind(None) is None


def test_a_fired_timer_goes_through_the_same_lane(_isolated):
    from vaf.core.timers import Timer, _fire

    _fire(Timer(id="t1", fire_at=0, session_id=CHAT, user_scope_id=SCOPE, message="ping"))
    meta = _isolated[0]["metadata"]
    assert meta["wake"] == "timer" and meta["timer"] is True and meta["timer_id"] == "t1"
    assert meta["user_scope_id"] == SCOPE


def test_the_terminal_app_has_a_card_for_it():
    from vaf.cli.tui_app.widgets import WakeMessage

    assert "process" in WakeMessage.LABELS


# ── the tree stop, from the audit ────────────────────────────────────────────

def test_stopping_a_process_that_already_ended_is_not_an_error():
    from vaf.core.platform import Platform

    record = _start(f'{PY} -c "pass"')
    assert _wait(lambda: not record.running)
    Platform.terminate_process_tree(record.popen.pid)   # gone already: no exception


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_a_grandchild_that_left_the_tree_is_stopped_with_the_group():
    """`( server & )` in a shell: the server is nobody's child any more, so the recursive
    child list cannot see it - but it keeps the process group the command started.
    MUTATION: drop the process-group signal - this goes red."""
    import psutil

    record = _start(f'( {PY} -c "import time; time.sleep(61)" & ) ; {PY} -c "import time; time.sleep(61)"')
    group = record.popen.pid

    def members():
        out = []
        for proc in psutil.process_iter(["pid"]):
            try:
                if os.getpgid(proc.info["pid"]) == group and proc.status() != psutil.STATUS_ZOMBIE:
                    out.append(proc.info["pid"])
            except (ProcessLookupError, psutil.Error):
                pass
        return out

    assert _wait(lambda: len(members()) >= 2), "the detached grandchild never started"
    processes.stop(record)
    assert _wait(lambda: members() == []), f"left running in the group: {members()}"



# ── the recorded group, from the third audit ─────────────────────────────────

def _group_members(group):
    import psutil

    out = []
    for proc in psutil.process_iter(["pid"]):
        try:
            if os.getpgid(proc.info["pid"]) == group and proc.status() != psutil.STATUS_ZOMBIE:
                out.append(proc.info["pid"])
        except (ProcessLookupError, psutil.Error):
            pass
    return out


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_a_server_left_behind_by_an_ended_shell_is_still_stoppable():
    """The shell exits at once and leaves a detached server in its group. The leader is
    gone, so a lookup of its group fails - the group recorded at start still reaches it.
    MUTATION: stop() returns for an ended command without signalling the group - red."""
    record = _start(f'( {PY} -c "import time; time.sleep(61)" & )')
    assert record.pgid == record.popen.pid
    assert _wait(lambda: not record.running), "the shell did not end"
    assert _wait(lambda: len(_group_members(record.pgid)) >= 1), "the detached server never started"
    out = processes.stop(record)
    assert "already ended" in out
    assert _wait(lambda: _group_members(record.pgid) == []), "the detached server outlived the stop"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_ending_vaf_also_reaches_what_an_ended_shell_left_behind():
    record = _start(f'( {PY} -c "import time; time.sleep(61)" & )')
    assert _wait(lambda: not record.running)
    assert _wait(lambda: len(_group_members(record.pgid)) >= 1)
    processes.terminate_all()
    assert _wait(lambda: _group_members(record.pgid) == [])


def test_a_group_stop_never_touches_our_own_group_or_nothing():
    """Signalling our own group would end this test run right here."""
    from vaf.core.platform import Platform

    Platform.terminate_process_group(None)
    if os.name != "nt":
        Platform.terminate_process_group(os.getpgrp(), grace=0.1)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_the_tree_stop_reaches_the_group_when_the_leader_is_already_gone():
    """The race between "still running" and the signal: the shell exits in between. The
    tree stop finds no process and must still stop the recorded group.
    MUTATION: return on NoSuchProcess without the group stop - red."""
    import subprocess

    from vaf.core.platform import Platform

    shell = subprocess.Popen(["/bin/sh", "-c", f'( {PY} -c "import time; time.sleep(61)" & )'],
                             start_new_session=True)
    shell.wait(timeout=10)
    assert _wait(lambda: len(_group_members(shell.pid)) >= 1)
    Platform.terminate_process_tree(shell.pid, grace=1.0, pgid=shell.pid)
    assert _wait(lambda: _group_members(shell.pid) == [])


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_a_group_member_that_ignores_sigterm_is_killed():
    """MUTATION: drop the SIGKILL in terminate_process_group - red."""
    import subprocess

    from vaf.core.platform import Platform

    child = subprocess.Popen(
        [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                               "print('ready', flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE, start_new_session=True)
    assert child.stdout.readline().strip() == b"ready"
    Platform.terminate_process_group(child.pid, grace=0.3)
    assert _wait(lambda: child.poll() is not None, timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_without_psutil_a_process_that_ignores_sigterm_is_still_killed(monkeypatch):
    """The fallback sent SIGTERM and stopped there. MUTATION: drop the SIGKILL - red."""
    import builtins
    import subprocess

    from vaf.core.platform import Platform

    child = subprocess.Popen(
        [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                               "print('ready', flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE, start_new_session=True)
    assert child.stdout.readline().strip() == b"ready"
    real_import = builtins.__import__

    def _no_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("hidden for this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    Platform.terminate_process_tree(child.pid, grace=0.5)
    monkeypatch.setattr(builtins, "__import__", real_import)
    assert _wait(lambda: child.poll() is not None, timeout=5), "SIGTERM was ignored and nothing followed"
