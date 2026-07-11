# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""write_file as a main-agent tool (blue378604 audit, Fix 1).

The python_sandbox persistence guard redirects the model to write_file, but the
tool was excluded from the main agent (dead signpost: a perfectly formed
write_file call got "Unknown tool"). These tests pin the new contract:

- registration: write_file is no longer in MAIN_AGENT_EXCLUDED_TOOLS, but IS
  excluded in thinking mode (propose-only runs must not create files)
- execute_tool wires session workspace, session id and user scope for the call
- a relative path joins the injected chat workspace; an explicit absolute path
  is honored (home-wide policy, user decision D1=C)
- non-admin (remote) scopes are jailed to their own VAF_Projects/<uid8>
- Web-UI emits use the injected session id, never the process-global fallback
- write_file no longer trips the legacy confirmation gate (plan gate only)
- CI guard: the sandbox redirect must always point at a registered tool

Hermetic: HOME/cwd point at a pytest tmp dir (Platform.documents_dir follows
Path.home()).
"""
import re
from pathlib import Path

import pytest

import vaf.core.agent as agent_mod
from vaf.core.trust import should_gate_tool
from vaf.tools.filesystem import WriteFileTool
from vaf.tools.python_sandbox import PythonSandboxTool

AGENT_SRC = Path(agent_mod.__file__).read_text(encoding="utf-8")


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(home)
    return home


def _exclusion_literal():
    m = re.search(r"MAIN_AGENT_EXCLUDED_TOOLS = \[(.*?)\]", AGENT_SRC, re.S)
    assert m, "MAIN_AGENT_EXCLUDED_TOOLS literal not found in agent.py"
    return m.group(1)


# ── Registration ─────────────────────────────────────────────────────────────

def test_write_file_not_in_main_agent_exclusion_list():
    excluded = _exclusion_literal()
    assert '"write_file"' not in excluded
    assert '"move_file"' in excluded  # move stays sub-agent-only


def test_write_file_excluded_in_thinking_mode():
    assert '"set_timer", "write_file")' in AGENT_SRC, (
        "thinking-mode exclusion tuple must contain write_file - a propose-only "
        "background run must not create files"
    )


def test_execute_tool_injection_block_present():
    # Losing this branch would leave every unit test green while the workspace
    # join, emit scoping and jail silently die - guard the wiring itself.
    m = re.search(r'if name == "write_file":(.*?)\n                if name in \(', AGENT_SRC, re.S)
    assert m, "write_file injection block missing in execute_tool"
    block = m.group(1)
    for needle in ('tool_args["user_scope_id"]',
                   'tool_args["_session_id"]',
                   'tool_args["_session_workspace"]'):
        assert needle in block, f"injection block lost {needle}"


# ── Workspace join (relative paths) vs explicit targets ─────────────────────

def test_relative_path_joins_injected_workspace(fake_home):
    ws = fake_home / "Documents" / "VAF_Projects" / "sess1"
    ws.mkdir(parents=True)
    out = WriteFileTool().run(path="chart.svg", content="<svg/>", _session_workspace=str(ws))
    assert "successfully" in out.lower(), out
    assert (ws / "chart.svg").read_text() == "<svg/>"


def test_relative_subpath_creates_parents_inside_workspace(fake_home):
    ws = fake_home / "Documents" / "VAF_Projects" / "sess1"
    ws.mkdir(parents=True)
    WriteFileTool().run(path="report/summary.md", content="# S", _session_workspace=str(ws))
    assert (ws / "report" / "summary.md").read_text() == "# S"


def test_explicit_absolute_path_wins_over_workspace(fake_home):
    # Home-wide policy (user decision D1=C): an explicit absolute target is honored.
    ws = fake_home / "Documents" / "VAF_Projects" / "sess1"
    ws.mkdir(parents=True)
    (fake_home / "Documents").mkdir(exist_ok=True)
    target = fake_home / "Documents" / "explicit.txt"
    WriteFileTool().run(path=str(target), content="x", _session_workspace=str(ws))
    assert target.read_text() == "x"
    assert not (ws / "explicit.txt").exists()


def test_unscoped_call_keeps_legacy_reroute(fake_home):
    # Direct consumers (coder/engine/librarian) pass no injected kwargs - the
    # home-reroute guard must behave exactly as pinned in test_write_file_reroute.py.
    WriteFileTool().run(path="bare_draft", content="x")
    assert not (fake_home / "bare_draft").exists()
    assert (fake_home / "Documents" / "VAF" / "bare_draft").read_text() == "x"


# ── Per-user jail (remote/non-admin sessions) ────────────────────────────────

def test_jail_blocks_foreign_user_projects(fake_home):
    foreign = fake_home / "Documents" / "VAF_Projects" / "cafe1234" / "x.txt"
    out = WriteFileTool().run(
        path=str(foreign), content="x",
        user_scope_id="deadbeef-0000-0000-0000-000000000000",
    )
    assert "outside your own data" in out.lower(), out
    assert not foreign.exists()


def test_jail_allows_own_user_root(fake_home):
    own = fake_home / "Documents" / "VAF_Projects" / "deadbeef" / "chat1" / "ok.txt"
    out = WriteFileTool().run(
        path=str(own), content="x",
        user_scope_id="deadbeef-0000-0000-0000-000000000000",
    )
    assert "successfully" in out.lower(), out
    assert own.read_text() == "x"


def test_jail_blocks_home_paths_outside_projects(fake_home):
    # A remote user's allowed roots are ONLY their own VAF_Projects/<uid8> -
    # personal folders stay off-limits (fail-closed, librarian precedent).
    (fake_home / "Documents").mkdir(exist_ok=True)
    target = fake_home / "Documents" / "private.txt"
    out = WriteFileTool().run(
        path=str(target), content="x",
        user_scope_id="deadbeef-0000-0000-0000-000000000000",
    )
    assert "outside your own data" in out.lower(), out
    assert not target.exists()


def test_admin_scope_none_is_not_jailed(fake_home):
    # Local admin (user_scope_id None): home-wide access per D1=C.
    (fake_home / "Documents").mkdir(exist_ok=True)
    target = fake_home / "Documents" / "admin.txt"
    out = WriteFileTool().run(path=str(target), content="x", user_scope_id=None)
    assert target.read_text() == "x"


# ── Emit-site session scoping ────────────────────────────────────────────────

def test_emits_use_injected_session_id(fake_home, monkeypatch):
    captured = {}
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_file_created",
                        lambda sid, path, title=None: captured.setdefault("file_sid", sid))
    monkeypatch.setattr(wi, "notify_document_created",
                        lambda sid, path, title=None: captured.setdefault("doc_sid", sid))
    monkeypatch.setenv("VAF_SESSION_ID", "env-session")
    ws = fake_home / "Documents" / "VAF_Projects" / "sess1"
    ws.mkdir(parents=True)
    WriteFileTool().run(path="note.txt", content="x",
                        _session_workspace=str(ws), _session_id="injected-session")
    assert captured.get("file_sid") == "injected-session"
    assert captured.get("doc_sid") == "injected-session"


# ── Gates ────────────────────────────────────────────────────────────────────

def test_write_file_no_longer_trips_legacy_confirmation_gate():
    assert should_gate_tool("write_file") is False
    assert should_gate_tool("move_file") is True


# ── CI guard: no dead signposts ──────────────────────────────────────────────

def test_sandbox_redirect_points_at_registered_tool():
    msg = PythonSandboxTool._blocked_persistence_write(
        "open('/home/u/Documents/out.txt', 'w').write('hi')"
    )
    assert msg and "write_file" in msg, "sandbox persistence guard changed shape"
    # Every tool the redirect tells the model to call must exist for the main
    # agent - a redirect to an excluded tool is the dead-signpost bug class.
    referenced = set(re.findall(r"call (\w+)\(", msg))
    assert referenced, "redirect no longer names a concrete tool call"
    excluded = _exclusion_literal()
    for tool in referenced:
        assert f'"{tool}"' not in excluded, (
            f"python_sandbox redirect points at '{tool}', which is excluded "
            f"from the main agent (dead signpost)"
        )
