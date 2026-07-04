# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Coder bash workspace confinement.

The coding agent's bash gets full access to its project workspace (run scripts,
installs, host docker for Docker projects) but VAF's own source, secrets and the
agent itself must be structurally out of reach - confined by the kernel (bubblewrap),
not by a bypassable command string filter. These tests pin the guard logic and, where
bubblewrap is present, prove real escape attempts fail.
"""
import os

import pytest

import vaf.tools.workspace_exec as wx
from vaf.tools.workspace_exec import (
    _assert_safe_workspace,
    _build_bwrap_argv,
    _invokes_docker,
    run_in_workspace,
)

VAF_ROOT = str(wx._VAF_PROJECT_ROOT)


# --- guard logic (no bwrap/docker needed) ----------------------------------

def test_refuses_workspace_inside_vaf_source():
    with pytest.raises(ValueError):
        _assert_safe_workspace(VAF_ROOT)
    with pytest.raises(ValueError):
        _assert_safe_workspace(os.path.join(VAF_ROOT, "vaf", "core"))


def test_refuses_home_and_root_workspace():
    with pytest.raises(ValueError):
        _assert_safe_workspace(str(wx.Path.home()))
    with pytest.raises(ValueError):
        _assert_safe_workspace("/")


def test_allows_normal_workspace(tmp_path):
    _assert_safe_workspace(str(tmp_path))  # must not raise


def test_docker_is_always_refused(tmp_path, monkeypatch):
    """The host docker socket is host-root-equivalent and cannot be safely policed by a
    command-string filter, so docker is refused up front and never reaches a sandbox."""
    assert _invokes_docker("docker build .")
    assert _invokes_docker("cd x && docker run --privileged -v /:/h alpine ls")
    assert not _invokes_docker("ls docker-compose.yml")  # substring, not an invocation
    monkeypatch.setattr(wx, "_bwrap_available", lambda: True)
    ran = {"x": False}
    monkeypatch.setattr(wx, "_run", lambda *a, **k: ran.__setitem__("x", True) or (0, "", ""))
    for cmd in (
        f"docker run --rm --privileged -v/mnt/veyllo1/VAF:/h alpine cat /h/x",
        "docker run --rm -v/:/hostfs alpine ls",
        "docker run -v $(pwd)/../../..:/x alpine ls",
    ):
        rc, out, err, mode = run_in_workspace(str(tmp_path), cmd)
        assert rc == -2 and mode == "refused", cmd
    assert not ran["x"], "a docker command must never reach the executor"


def test_missing_workspace_is_refused():
    rc, out, err, mode = run_in_workspace("/no/such/dir", "echo x")
    assert rc == -2 and mode == "refused"


def test_refuses_when_no_sandbox_available(tmp_path, monkeypatch):
    monkeypatch.setattr(wx, "_bwrap_available", lambda: False)
    monkeypatch.setattr(wx, "_docker_available", lambda: False)
    rc, out, err, mode = run_in_workspace(str(tmp_path), "echo x")
    assert rc == -2 and mode == "refused"
    assert "no sandbox" in err.lower()


def test_bwrap_argv_is_locked_down(tmp_path):
    ws = str(tmp_path)
    argv = _build_bwrap_argv(ws, "echo x", 60)
    # workspace is a read-write bind
    assert any(a == "--bind" and argv[i + 1] == ws for i, a in enumerate(argv)), "workspace must be --bind (rw)"
    # VAF source and docker socket are never mounted; env is cleared; network is unshared
    assert VAF_ROOT not in argv
    assert not any("docker.sock" in a for a in argv)
    assert "--clearenv" in argv, "env must be cleared so tray secrets do not leak"
    assert "--unshare-net" in argv, "host loopback (memory DB / API / secrets) must be unreachable"
    # no secret env var is re-injected
    assert not any(str(v).lower() in ("--setenv",) and False for v in argv)


# --- real confinement (bubblewrap required) --------------------------------

_HAS_BWRAP = wx._bwrap_available()


@pytest.mark.skipif(not _HAS_BWRAP, reason="bubblewrap not available")
def test_real_workspace_write_persists(tmp_path):
    rc, out, err, mode = run_in_workspace(str(tmp_path), "echo hi > f.txt")
    assert rc == 0, (rc, err)
    assert (tmp_path / "f.txt").read_text().strip() == "hi"


@pytest.mark.skipif(not _HAS_BWRAP, reason="bubblewrap not available")
def test_real_vaf_core_write_is_blocked(tmp_path):
    target = os.path.join(VAF_ROOT, "vaf", "core", "agent.py")
    before = open(target).read()
    rc, out, err, mode = run_in_workspace(str(tmp_path), f"echo HACKED > {target}; echo done")
    assert "No such file" in err or rc != 0
    assert open(target).read() == before, "VAF core must be untouched"


@pytest.mark.skipif(not _HAS_BWRAP, reason="bubblewrap not available")
def test_real_vaf_source_is_invisible(tmp_path):
    rc, out, err, mode = run_in_workspace(str(tmp_path), f"ls {VAF_ROOT}")
    assert VAF_ROOT.encode
    assert "No such file" in (out + err) or rc != 0


@pytest.mark.skipif(not _HAS_BWRAP, reason="bubblewrap not available")
def test_real_network_isolated_from_host_services(tmp_path):
    # Host loopback services (memory DB 5432) must be unreachable in the jail. Catch the
    # error so no traceback echoes the marker source line.
    code = (
        "python3 -c \"import socket\n"
        "try:\n"
        "    socket.create_connection(('127.0.0.1',5432),2); print('REACHED_HOST_DB')\n"
        "except OSError: print('blocked')\""
    )
    rc, out, err, mode = run_in_workspace(str(tmp_path), code)
    assert "REACHED_HOST_DB" not in out, (out, err)
    assert "blocked" in out, (out, err)


@pytest.mark.skipif(not _HAS_BWRAP, reason="bubblewrap not available")
def test_real_env_secrets_not_leaked(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-not-leak")
    rc, out, err, mode = run_in_workspace(str(tmp_path), "env")
    assert "sk-should-not-leak" not in (out + err), "tray env secrets leaked into the jail"
