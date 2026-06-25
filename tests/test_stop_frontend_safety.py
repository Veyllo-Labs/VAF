# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""stop_frontend must never take the tray down with the frontend.

The "Killed" right after "Stopping frontend..." crash was os.killpg signalling the TRAY's own process
group: a silently-dead frontend's PID gets recycled (or shares our group), and killpg on it hits VAF
itself. These tests pin the two safety rules: reap-if-dead (never killpg a possibly-recycled PID), and
never killpg our own group.
"""
import types

import vaf.core.frontend_manager as fmmod
from vaf.core.frontend_manager import FrontendManager


class _FakeProc:
    def __init__(self, alive=True, pid=777777):
        self._alive = alive
        self.pid = pid
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def _fm(proc, monkeypatch):
    fm = FrontendManager()
    fm.process = proc
    fm.port = 0  # falsy → skip the port-wait / lsof cleanup paths
    monkeypatch.setattr(fmmod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(fmmod.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr=""))
    monkeypatch.setattr(fm, "get_port_file", lambda: "/nonexistent/vaf-port", raising=False)
    return fm


def test_dead_frontend_is_reaped_never_killpg(monkeypatch):
    calls = []
    monkeypatch.setattr(fmmod.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    monkeypatch.setattr(fmmod.os, "getpgid", lambda pid: 4242)
    fm = _fm(_FakeProc(alive=False), monkeypatch)
    fm.stop_frontend(wait_for_exit=False)
    assert calls == []  # dead → reap only


def test_never_killpg_when_frontend_shares_our_group(monkeypatch):
    calls = []
    monkeypatch.setattr(fmmod.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    monkeypatch.setattr(fmmod.os, "getpgid", lambda pid: 1234)  # proc.pid AND 0 → same group
    proc = _FakeProc(alive=True)
    fm = _fm(proc, monkeypatch)
    fm.stop_frontend(wait_for_exit=False)
    assert calls == []          # must NOT killpg our own group
    assert proc.terminated      # only its own PID was signalled


def test_killpg_only_when_frontend_is_isolated(monkeypatch):
    proc = _FakeProc(alive=True, pid=777)
    calls = []
    monkeypatch.setattr(fmmod.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    monkeypatch.setattr(fmmod.os, "getpgid", lambda pid: 555 if pid == 777 else 1234)
    fm = _fm(proc, monkeypatch)
    fm.stop_frontend(wait_for_exit=False)
    assert calls and calls[0][0] == 555   # killed the frontend's OWN group, not ours


def test_lsof_port_cleanup_never_kills_our_own_pid(monkeypatch):
    """The real "Killed on every live restart" bug: the tray is CONNECTED to :3000 (its proxy forwards
    there + the webview renders it), so `lsof -ti :3000` returned the tray's own pid and `kill -9` took
    VAF down. The cleanup must use -sTCP:LISTEN and skip our own pid."""
    import os as _os
    my_pid = _os.getpid()
    kill_targets = []

    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["lsof", "-ti"]:
            assert "-sTCP:LISTEN" in cmd  # only LISTENERS, never connected clients
            return types.SimpleNamespace(returncode=0, stdout=f"{my_pid}\n888888\n", stderr="")
        if cmd[:2] == ["kill", "-9"]:
            kill_targets.append(cmd[2])
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(fmmod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(fmmod.subprocess, "run", fake_run)
    fm = FrontendManager()
    fm.process = _FakeProc(alive=False)  # frontend already dead → straight to the port cleanup
    fm.port = 3000                       # truthy → run the lsof cleanup
    monkeypatch.setattr(fm, "is_port_in_use", lambda p: False, raising=False)
    monkeypatch.setattr(fm, "get_port_file", lambda: "/nonexistent/vaf-port", raising=False)

    fm.stop_frontend(wait_for_exit=False)

    assert str(my_pid) not in kill_targets   # NEVER kill ourselves (the crash)
    assert "888888" in kill_targets          # but DO free the port from a genuine leftover listener
