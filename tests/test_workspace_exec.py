# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Coder bash workspace confinement.

The coding agent's bash gets full access to its project workspace (run scripts,
installs; never docker, whose socket is host-root) but VAF's own source, secrets and the
agent itself must be structurally out of reach - confined by the kernel (bubblewrap),
not by a bypassable command string filter. These tests pin the guard logic and, where
bubblewrap is present, prove real escape attempts fail.
"""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import vaf.tools.workspace_exec as wx
from vaf.tools.workspace_exec import (
    _assert_safe_workspace,
    _build_bwrap_argv,
    _invokes_docker,
    run_in_workspace,
)

from vaf.core.workspace_guard import _VAF_ROOT

VAF_ROOT = str(_VAF_ROOT)
# A caller with no account (the machine owner's freedoms): the confinement these tests pin
# is the kernel jail, not the account half, which has tests of its own below.
_NO_ACCOUNT = {"user_scope_id": None, "user_role": None}


# --- guard logic (no bwrap/docker needed) ----------------------------------

def test_refuses_workspace_inside_vaf_source():
    with pytest.raises(ValueError):
        _assert_safe_workspace(VAF_ROOT)
    with pytest.raises(ValueError):
        _assert_safe_workspace(os.path.join(VAF_ROOT, "vaf", "core"))


def test_refuses_home_and_root_workspace():
    with pytest.raises(ValueError):
        _assert_safe_workspace(str(Path.home()))
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
        "docker run --rm --privileged -v/srv/project:/h alpine cat /h/x",
        "docker run --rm -v/:/hostfs alpine ls",
        "docker run -v $(pwd)/../../..:/x alpine ls",
    ):
        rc, out, err, mode = run_in_workspace(str(tmp_path), cmd, **_NO_ACCOUNT)
        assert rc == -2 and mode == "refused", cmd
    assert not ran["x"], "a docker command must never reach the executor"


def test_missing_workspace_is_refused():
    rc, out, err, mode = run_in_workspace("/no/such/dir", "echo x", **_NO_ACCOUNT)
    assert rc == -2 and mode == "refused"


def test_refuses_when_no_sandbox_available(tmp_path, monkeypatch):
    monkeypatch.setattr(wx, "_bwrap_available", lambda: False)
    monkeypatch.setattr(wx, "_docker_available", lambda: False)
    rc, out, err, mode = run_in_workspace(str(tmp_path), "echo x", **_NO_ACCOUNT)
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
    rc, out, err, mode = run_in_workspace(str(tmp_path), "echo hi > f.txt", **_NO_ACCOUNT)
    assert rc == 0, (rc, err)
    assert (tmp_path / "f.txt").read_text().strip() == "hi"


@pytest.mark.skipif(not _HAS_BWRAP, reason="bubblewrap not available")
def test_real_vaf_core_write_is_blocked(tmp_path):
    target = os.path.join(VAF_ROOT, "vaf", "core", "agent.py")
    before = open(target).read()
    rc, out, err, mode = run_in_workspace(str(tmp_path), f"echo HACKED > {target}; echo done", **_NO_ACCOUNT)
    # The redirect's error is the shell's own wording, and /bin/sh differs by system: bash
    # says "No such file or directory", dash (Debian, Ubuntu) "cannot create ...: Directory
    # nonexistent". Either way the shell names the path it could not write.
    assert target in err or rc != 0
    assert open(target).read() == before, "VAF core must be untouched"


@pytest.mark.skipif(not _HAS_BWRAP, reason="bubblewrap not available")
def test_real_vaf_source_is_invisible(tmp_path):
    rc, out, err, mode = run_in_workspace(str(tmp_path), f"ls {VAF_ROOT}", **_NO_ACCOUNT)
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
    rc, out, err, mode = run_in_workspace(str(tmp_path), code, **_NO_ACCOUNT)
    assert "REACHED_HOST_DB" not in out, (out, err)
    assert "blocked" in out, (out, err)


@pytest.mark.skipif(not _HAS_BWRAP, reason="bubblewrap not available")
def test_real_env_secrets_not_leaked(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-not-leak")
    rc, out, err, mode = run_in_workspace(str(tmp_path), "env", **_NO_ACCOUNT)
    assert "sk-should-not-leak" not in (out + err), "tray env secrets leaked into the jail"


def test_refuses_a_folder_above_home(tmp_path, monkeypatch):
    """/home holds every account's home: bound read-write, it carried this user's ~/.vaf
    and everybody else's. MUTATION: back to `p == home` - red.

    A home of its own under tmp_path: on a CI runner the checkout lies inside the real home
    (/home/runner/work/...), so the real home's parent also overlaps VAF's source tree and
    that refusal answered first, with another reason."""
    homes = tmp_path / "homes"
    (homes / "alice").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(homes / "alice"))
    monkeypatch.setenv("USERPROFILE", str(homes / "alice"))    # Path.home() on Windows
    assert not _VAF_ROOT.is_relative_to(homes.resolve())
    with pytest.raises(ValueError, match="above"):
        _assert_safe_workspace(str(homes))


# --- whose folder the shell runs in (the account half) ---------------------

_A = "ab12cd34-0000-4000-8000-00000000000a"
_B = "cd34ef56-0000-4000-8000-00000000000b"


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    """Two accounts' project trees under a scratch home, a system temp folder of its own
    (the test's home lies in the real temp folder), and a shell that records instead of
    running."""
    import vaf.core.workspace_guard as wg
    import vaf.tools.filesystem as fs
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    systemp = tmp_path / "systemp"
    systemp.mkdir()
    monkeypatch.setattr(wg, "_temp_roots", lambda: [str(systemp)])
    fs._shared_room_roots_cache.clear()
    monkeypatch.setattr(fs, "_shared_room_roots", lambda scope: [])
    from vaf.core.session import get_user_projects_root
    a_proj = get_user_projects_root(_A) / "chat" / "site"
    b_proj = get_user_projects_root(_B) / "chat" / "site"
    a_proj.mkdir(parents=True)
    b_proj.mkdir(parents=True)
    ran = []
    monkeypatch.setattr(wx, "_bwrap_available", lambda: True)
    monkeypatch.setattr(wx, "_run", lambda argv, *a, **k: ran.append(argv) or (0, "ok", ""))
    yield SimpleNamespace(a_proj=a_proj, b_proj=b_proj, systemp=systemp, ran=ran)
    fs._shared_room_roots_cache.clear()


def test_the_identity_keywords_have_no_default(accounts):
    """A caller that forgets whose run it is gets no shell.
    MUTATION: give user_scope_id/user_role a default of None - red."""
    with pytest.raises(TypeError):
        run_in_workspace(str(accounts.a_proj), "echo x")
    assert accounts.ran == []


def test_another_accounts_folder_is_refused_and_nothing_runs(accounts):
    """MUTATION: remove the project_dir_refusal call from run_in_workspace - red."""
    rc, out, err, mode = run_in_workspace(str(accounts.b_proj), "touch marker",
                                          user_scope_id=_A, user_role="user")
    assert (rc, mode) == (-2, "refused")
    assert "outside the folders this account may write" in err
    assert accounts.ran == []
    rc, _, _, mode = run_in_workspace(str(accounts.a_proj), "true",
                                      user_scope_id=_A, user_role="user")
    assert mode != "refused" and len(accounts.ran) == 1
    # An admin reaches every tree, as before.
    rc, _, _, mode = run_in_workspace(str(accounts.b_proj), "true",
                                      user_scope_id=_A, user_role="admin")
    assert mode != "refused" and len(accounts.ran) == 2


def test_a_scratch_folder_runs_only_inside_temp(accounts):
    """The coder's content-only folder (tempfile.mkdtemp) lies outside every account's tree.
    MUTATION: drop the temp containment of scratch - the B case goes red."""
    scratch = accounts.systemp / "vaf_content_x"
    scratch.mkdir()
    _, _, err, mode = run_in_workspace(str(scratch), "true", user_scope_id=_A, user_role="user")
    assert mode == "refused"  # without the flag the account half answers
    _, _, err, mode = run_in_workspace(str(scratch), "true", user_scope_id=_A,
                                       user_role="user", scratch=True)
    assert mode != "refused", err
    _, _, err, mode = run_in_workspace(str(accounts.b_proj), "touch marker",
                                       user_scope_id=_A, user_role="user", scratch=True)
    assert mode == "refused"
    assert len(accounts.ran) == 1


def test_bash_asks_the_folder_against_its_bound_identity(accounts):
    """The identity is bound at construction by trusted code, never taken from the model, so
    the frozen declaration (no identity_kwargs, no file_access) holds.
    MUTATION: stop passing owner_scope/owner_role to run_in_workspace in BashTool.run - red."""
    from vaf.tools.bash import BashTool
    assert BashTool.identity_kwargs == () and BashTool.file_access is None
    out = BashTool(str(accounts.b_proj), owner_scope=_A, owner_role="user").run(
        command="touch marker")
    assert "outside the folders this account may write" in out
    assert accounts.ran == []
    BashTool(str(accounts.a_proj), owner_scope=_A, owner_role="user").run(command="ls")
    assert len(accounts.ran) == 1


def test_every_workflow_lane_runs_bash_as_the_engines_identity(accounts):
    """The runners build their tools bare (workflow_primitives: the saved-template executor,
    the @workflow subprocess, the workflow builder) and seven lanes construct an engine; the
    engine binds bash to the run's identity, so a folder of another account is refused.
    MUTATION: return ``tools`` unchanged from _bind_shell_owner - red."""
    from vaf.workflows.engine import WorkflowEngine
    from vaf.workflows.tool_overlay import workflow_primitives
    tools = workflow_primitives()
    primitive = tools["bash"]
    assert primitive.owner_scope is None and primitive.owner_role is None
    engine = WorkflowEngine(tools, user_scope_id=_A, user_role="user")
    shell = engine.tools["bash"]
    out = shell.run(command="touch marker", base_dir=str(accounts.b_proj))
    assert "outside the folders this account may write" in out
    assert accounts.ran == []
    shell.run(command="ls", base_dir=str(accounts.a_proj))
    assert len(accounts.ran) == 1
    # The registry's own instance is never changed (a live agent may share it).
    assert primitive.owner_scope is None and tools["bash"] is primitive
    # A run with no identity keeps the tool as it was.
    assert WorkflowEngine(tools).tools["bash"] is primitive


@pytest.mark.skipif(not _HAS_BWRAP, reason="bubblewrap not available")
def test_real_jail_never_runs_in_another_accounts_folder(tmp_path, monkeypatch):
    import vaf.tools.filesystem as fs
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(fs, "_shared_room_roots", lambda scope: [])
    fs._shared_room_roots_cache.clear()
    from vaf.core.session import get_user_projects_root
    b_proj = get_user_projects_root(_B) / "proj"
    b_proj.mkdir(parents=True)
    rc, out, err, mode = run_in_workspace(str(b_proj), "touch marker",
                                          user_scope_id=_A, user_role="user")
    assert mode == "refused"
    assert not (b_proj / "marker").exists()
    fs._shared_room_roots_cache.clear()
