# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The coding agent works only in folders its caller may write.

Measured before the change, as account A (role "user") against account B's project: the
only screen on the coder's folder was `is_unsafe_project_dir`, which knows the home
directory and VAF's own data but no account. `coding_agent(task="history",
project_path=<B>/proj)` through the main funnel printed B's commit subjects; a rollback
wrote two commits into B's repository; a run's `bash` (rooted at its folder) printed B's
files and created new ones there; `update_codex` wrote B's `.vaf/codex.md`; and the
wrapped `project_history` inside a run read whatever `project_path` the model wrote.

Every folder the coder may work in now asks `project_dir_refusal` for the caller, which is
the file jail's own `jail_allows(mode="write")` on top of the unsafe check. An admin, the
machine owner and a scope-less caller keep their behaviour. No LLM, no network: the paths
under test end before a model is asked, and the spawn is a recorder.
"""
import os
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

import vaf.tools.filesystem as fs
from vaf.core.workspace_guard import OUTSIDE_ACCOUNT, project_dir_refusal

A = "ab12cd34-0000-4000-8000-00000000000a"
B = "cd34ef56-0000-4000-8000-00000000000b"
GATE = "is outside the folders this account may write (its own project folder"


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _count(repo):
    return _git("rev-list", "--count", "HEAD", cwd=repo).stdout.strip()


def _files(repo):
    return sorted(os.listdir(repo))


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A scratch home with two accounts' project trees and one git project in B's."""
    if _git("--version", cwd=tmp_path).returncode != 0:
        pytest.skip("git not available")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for var in ("VAF_USER_SCOPE_ID", "VAF_USER_ROLE", "VAF_IN_SUBAGENT_TERMINAL",
                "VAF_SESSION_ID", "VAF_PARENT_CWD"):
        monkeypatch.delenv(var, raising=False)
    fs._shared_room_roots_cache.clear()
    monkeypatch.setattr(fs, "_shared_room_roots", lambda scope: [])
    import vaf.core.subagent_ipc as ipc
    monkeypatch.setattr(ipc, "get_current_session_id", lambda: None)

    from vaf.core.session import get_user_projects_root
    a_root, b_root = get_user_projects_root(A), get_user_projects_root(B)
    a_root.mkdir(parents=True)
    b_proj = b_root / "proj"
    b_proj.mkdir(parents=True)
    (b_proj / "secret.txt").write_text("B-SECRET\n")
    _git("init", "-q", cwd=b_proj)
    _git("config", "user.name", "B", cwd=b_proj)
    _git("config", "user.email", "b@example.invalid", cwd=b_proj)
    _git("add", "-A", cwd=b_proj)
    _git("commit", "-qm", "B private commit", cwd=b_proj)

    from vaf.core.tool_dispatch import set_account_allowlist_resolver
    set_account_allowlist_resolver(None)
    yield SimpleNamespace(home=home, a_root=a_root, b_root=b_root, b_proj=b_proj)
    set_account_allowlist_resolver(None)
    fs._shared_room_roots_cache.clear()


def _main_funnel(scope, role):
    from vaf.core.tool_dispatch import ToolCaller
    from vaf.tools.coder import CodingAgentTool
    return ToolCaller({"coding_agent": CodingAgentTool()}, user_scope_id=scope, user_role=role,
                      gate_enabled=False, max_result_chars=None)


def _first_sha(repo):
    return _git("rev-list", "--max-parents=0", "HEAD", cwd=repo).stdout.strip()[:7]


# ── AC1: the main funnel, tenant A against B's project ─────────────────────────────────

def test_a_tenant_can_neither_read_nor_roll_back_another_accounts_history(world):
    """MUTATION: delete the top gate in run() - red (the shortcut prints B's history)."""
    from vaf.core.tool_dispatch import set_account_allowlist_resolver
    set_account_allowlist_resolver(lambda scope: {"coding_agent"} if scope == A else None)
    funnel = _main_funnel(A, "user")

    out = funnel.execute("coding_agent", {"task": "zeig die History",
                                          "project_path": str(world.b_proj)})
    assert GATE in out and "B private commit" not in out
    # A first commit id that is not the head, so a performed rollback would write.
    (world.b_proj / "secret.txt").write_text("B-SECRET v2\n")
    _git("commit", "-qam", "B second commit", cwd=world.b_proj)
    before_count, before_files = _count(world.b_proj), _files(world.b_proj)
    out = funnel.execute("coding_agent", {"task": f"rollback auf {_first_sha(world.b_proj)}",
                                          "project_path": str(world.b_proj)})
    assert GATE in out
    assert _count(world.b_proj) == before_count and _files(world.b_proj) == before_files
    assert (world.b_proj / "secret.txt").read_text() == "B-SECRET v2\n"


def test_without_a_project_path_the_chats_stored_folder_is_judged_for_the_caller(world,
                                                                                 monkeypatch):
    """No project_path: the history tool falls back to the chat's stored folder, which the
    top gate never saw. A record written before the gate existed can name B's project.
    MUTATION: drop the identity the shortcut hands to ProjectHistoryTool - red."""
    import vaf.core.session as session
    import vaf.core.subagent_ipc as ipc
    monkeypatch.setattr(ipc, "get_current_session_id", lambda: "chat1")
    monkeypatch.setattr(session.SessionManager, "load", lambda self, sid: SimpleNamespace(
        runtime_state={"last_project_path": str(world.b_proj)}, project_path="", metadata={}))
    out = _main_funnel(A, "user").execute("coding_agent", {"task": "zeig die History"})
    assert "outside the folders this account may write" in out
    assert "B private commit" not in out


# ── AC2: admin and no scope behave as before ───────────────────────────────────────────

@pytest.mark.parametrize("scope,role", [(A, "admin"), (None, None)])
def test_an_admin_and_a_scopeless_caller_keep_history_and_rollback(world, scope, role):
    """MUTATION: drop the role from the gate's jail question - the admin row goes red."""
    funnel = _main_funnel(scope, role)
    out = funnel.execute("coding_agent", {"task": "zeig die History",
                                          "project_path": str(world.b_proj)})
    assert "B private commit" in out
    (world.b_proj / "secret.txt").write_text("v2\n")
    _git("commit", "-qam", "B second commit", cwd=world.b_proj)
    out = funnel.execute("coding_agent", {"task": f"rollback auf {_first_sha(world.b_proj)}",
                                          "project_path": str(world.b_proj)})
    assert "restored" in out
    assert (world.b_proj / "secret.txt").read_text() == "B-SECRET\n"


def test_an_unsafe_folder_keeps_its_old_answer_for_the_owner(world):
    """The gate asks only the account half: the home directory for the owner is still the
    history tool's own refusal, not the account text.
    MUTATION: let the gate ask project_dir_refusal (unsafe half included) - red."""
    out = _main_funnel(None, None).execute(
        "coding_agent", {"task": "zeig die History", "project_path": str(world.home)})
    assert "not a valid project directory" in out
    assert "outside the folders" not in out


# ── AC3: refused before the spawn and before the shortcut ──────────────────────────────

@pytest.fixture
def spawn_recorder(monkeypatch):
    calls = []
    import vaf.core.subagent_spawn as spawn

    def _record(agent_type, task, **kw):
        calls.append((agent_type, task, kw))
        return SimpleNamespace(marker="[SUBAGENT_ASYNC:t1:coding_agent]")

    monkeypatch.setattr(spawn, "spawn_subagent", _record)
    from vaf.core.config import Config
    real_get = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: True if key == "sub_agents_in_separate_terminals"
        else real_get(key, default)))
    return calls


def _foreign_targets(world):
    a_link = world.a_root / "link-into-b"
    b_file_link = world.b_proj / "page.html"
    (world.a_root / "own.html").write_text("a")
    targets = {
        "b_project": world.b_proj,
        "b_root": world.b_root,
        "projects_root": world.b_root.parent,
        "documents_sub": world.home / "Documents" / "x",
        "desktop_sub": world.home / "Desktop" / "x",
        "file_in_b": world.b_proj / "secret.txt",
    }
    try:
        os.symlink(world.b_proj, a_link)
        targets["link_in_a_to_b"] = a_link
        # A file in B's project that is a link into A's tree: judged unsplit it would pass
        # (its target is A's), but the run works in its FOLDER, which is B's.
        os.symlink(world.a_root / "own.html", b_file_link)
        targets["b_file_linking_into_a"] = b_file_link
    except (OSError, NotImplementedError):
        pass
    return targets


TARGETS = ["b_project", "b_root", "projects_root", "documents_sub", "desktop_sub",
           "file_in_b", "link_in_a_to_b", "b_file_linking_into_a"]


@pytest.mark.parametrize("name", TARGETS)
def test_a_foreign_project_path_is_refused_before_anything_starts(world, spawn_recorder, name):
    """MUTATION: move the gate below the spawn block - red (the recorder is called).
    MUTATION: compare unresolved path strings instead of asking the jail - the link row red.
    MUTATION: drop the file-to-folder split - the b_file_linking_into_a row red."""
    from vaf.tools.coder import CodingAgentTool
    targets = _foreign_targets(world)
    if name not in targets:
        pytest.skip("symlinks are not available here")
    given = targets[name]
    out = CodingAgentTool().run(task="build a small page", project_path=str(given),
                                user_scope_id=A, user_role="user")
    assert out.startswith(f"Error: project_path '{os.path.abspath(given)}' ")
    assert GATE in out
    assert "Leave project_path out" in out
    assert spawn_recorder == []
    assert not (world.b_root / "proj" / ".vaf").exists()


@pytest.mark.parametrize("where", ["own_root", "own_file", "room_folder"])
def test_a_folder_the_account_may_write_reaches_the_spawn(world, spawn_recorder, monkeypatch,
                                                          where):
    from vaf.tools.coder import CodingAgentTool
    room = world.b_root / "room-shared"
    room.mkdir()
    monkeypatch.setattr(fs, "_shared_room_roots", lambda scope: [room] if scope == A else [])
    target = {"own_root": world.a_root / "chat" / "site",
              "own_file": world.a_root / "chat" / "site" / "index.html",
              "room_folder": room}[where]
    out = CodingAgentTool().run(task="build a small page", project_path=str(target),
                                user_scope_id=A, user_role="user")
    assert out.startswith("[SUBAGENT_ASYNC:"), out
    assert len(spawn_recorder) == 1


def test_the_gate_stands_before_the_shortcut_and_the_spawn():
    """Source order, the house pattern for 'refused before X started'."""
    import inspect
    from vaf.tools.coder import CodingAgentTool
    src = inspect.getsource(CodingAgentTool.run)
    # The return, not the assignment: moving only the return below the spawn is the
    # regression this pins.
    gate = src.index("return _pp_refusal")
    assert src.index("_pp_refusal = _project_path_refusal(") < gate
    assert gate < src.index("_detect_history_rollback_intent(task)")
    assert gate < src.index("spawned = spawn_subagent(")
    assert gate < src.index("live.start()")


def test_a_gate_that_cannot_ask_the_jail_refuses(world, spawn_recorder, monkeypatch):
    """Fail-closed like project_dir_refusal: an error in the jail question ends the call.
    MUTATION: drop the try around the jail question in _project_path_refusal - red."""
    from vaf.tools.coder import CodingAgentTool

    def _broken(*a, **k):
        raise RuntimeError("jail store unreadable")

    monkeypatch.setattr(fs, "jail_allows", _broken)
    out = CodingAgentTool().run(task="build a small page",
                                project_path=str(world.a_root / "chat" / "site"),
                                user_scope_id=A, user_role="user")
    assert GATE in out
    assert spawn_recorder == []


# ── AC4: the gate holds in the spawned child ───────────────────────────────────────────

def test_the_child_refuses_with_the_identity_from_its_environment(world, monkeypatch):
    """MUTATION: let the gate read the identity from the kwargs only - red."""
    import requests
    import vaf.tools.coder as coder

    def _no_network(*a, **k):
        raise requests.exceptions.ConnectionError("no network in this test")

    monkeypatch.setattr(coder.requests, "get", _no_network)
    monkeypatch.setenv("VAF_IN_SUBAGENT_TERMINAL", "1")
    monkeypatch.setenv("VAF_USER_SCOPE_ID", A)
    monkeypatch.setenv("VAF_USER_ROLE", "user")
    before = _files(world.b_proj)
    out = coder.CodingAgentTool().run(task="build a small page",
                                      project_path=str(world.b_proj))
    assert GATE in out
    assert _files(world.b_proj) == before and _count(world.b_proj) == "1"


# ── AC5 and AC8: the folder choice for every source ────────────────────────────────────

def _choose(task, project_path="", *, scope=A, role="user", content_only=False, tool=None):
    from vaf.tools.coder import CodingAgentTool, _choose_base_dir
    return _choose_base_dir(tool or CodingAgentTool(), task, project_path, scope=scope,
                            role=role, content_only=content_only)


def _inside(path, root):
    return os.path.realpath(path).startswith(os.path.realpath(root) + os.sep)


def test_an_explicit_foreign_project_path_is_refused_by_the_choice_too(world):
    """The backstop behind the top gate.
    MUTATION: ask is_unsafe_project_dir for project_path here - red."""
    choice = _choose("build a page", str(world.b_proj))
    assert choice.refusal.startswith(f"Error: cannot work in '{world.b_proj}'")
    assert OUTSIDE_ACCOUNT in choice.refusal


def test_a_foreign_path_in_the_task_falls_through_to_the_accounts_folder(world):
    """MUTATION: ask is_unsafe_project_dir for the task path - red (B's folder, then the
    backstop's refusal)."""
    choice = _choose(f"write the page {world.b_proj}/landing.html please")
    assert choice.refusal == ""
    assert _inside(choice.base_dir, world.a_root), choice.base_dir
    assert choice.target_file_hint == "landing.html"
    assert any("outside the folders this account may write - creating 'landing.html' in "
               "this account's own project folder instead" in n
               for n in choice.notes), choice.notes


def test_a_foreign_last_project_of_the_chat_is_skipped(world, monkeypatch):
    """MUTATION: ask is_unsafe_project_dir in _get_session_project_path - red."""
    import vaf.core.session as session
    import vaf.core.subagent_ipc as ipc
    monkeypatch.setattr(ipc, "get_current_session_id", lambda: "chat1")
    monkeypatch.setattr(session.SessionManager, "load", lambda self, sid: SimpleNamespace(
        runtime_state={"last_project_path": str(world.b_proj)}, metadata={}))
    choice = _choose("fix the bug on the website")
    assert choice.refusal == ""
    assert not _inside(choice.base_dir, world.b_root)
    assert _inside(choice.base_dir, world.a_root / "chat1"), choice.base_dir


def test_a_foreign_cwd_is_not_used_as_the_project(world, monkeypatch):
    """MUTATION: ask is_unsafe_project_dir for the cwd in _determine_base_dir - red."""
    (world.b_proj / "pyproject.toml").write_text("[project]\nname='b'\n")
    monkeypatch.chdir(world.b_proj)
    choice = _choose("fix the failing tests")
    assert choice.refusal == ""
    assert _inside(choice.base_dir, world.a_root), choice.base_dir
    # The owner in the same cwd still works there, as before.
    owner = _choose("fix the failing tests", scope=None, role=None)
    assert owner.base_dir == str(world.b_proj)


def test_a_new_project_of_an_account_lands_in_its_own_folder_without_a_session(world):
    """The scope is not saved on a chat's first turn, so the session gave no prefix and the
    folder landed in the flat shared root.
    MUTATION: remove the caller-prefix override in _generate_project_directory - red."""
    choice = _choose("Erstelle eine Webseite Baeckerei")
    assert choice.refusal == ""
    assert _inside(choice.base_dir, world.a_root), choice.base_dir


@pytest.mark.parametrize("scope,role", [(A, "admin"), (None, None)])
def test_an_admin_and_the_owner_keep_the_sessions_prefix(world, monkeypatch, scope, role):
    """MUTATION: apply the caller-prefix override to admins as well - the admin row red."""
    import vaf.core.session as session
    import vaf.core.subagent_ipc as ipc
    other = "ef56ab12-0000-4000-8000-00000000000c"
    monkeypatch.setattr(ipc, "get_current_session_id", lambda: "chat2")
    monkeypatch.setattr(session.SessionManager, "load", lambda self, sid: SimpleNamespace(
        runtime_state={}, metadata={"user_scope_id": other}))
    choice = _choose("Erstelle eine Webseite Baeckerei", scope=scope, role=role)
    projects = world.home / "Documents" / "VAF_Projects"
    assert choice.refusal == ""
    assert os.path.dirname(choice.base_dir) == str(projects / other[:8] / "chat2")


def test_the_folder_settled_on_is_asked_once_more(world, monkeypatch):
    """A later regression in the generated folder must not reach another account's tree.
    MUTATION: drop the final project_dir_refusal in _choose_base_dir - red."""
    from vaf.tools.coder import CodingAgentTool
    tool = CodingAgentTool()
    monkeypatch.setattr(tool, "_determine_base_dir",
                        lambda task, **kw: str(world.b_root / "generated"))
    choice = _choose("build something", tool=tool)
    assert OUTSIDE_ACCOUNT in choice.refusal
    assert not (world.b_root / "generated").exists()


def test_a_content_only_run_is_not_refused(world):
    """Its folder is a fresh private temporary one that run() makes and removes, outside
    every account's tree, so the account half would refuse every content-only run.
    MUTATION: drop the content-only branch of _choose_base_dir - red."""
    choice = _choose("CONTENT_ONLY: return the html", content_only=True)
    assert choice.refusal == "" and choice.source == "content_only"
    tmp = tempfile.mkdtemp(prefix="vaf_content_")
    try:
        assert project_dir_refusal(tmp, user_scope_id=A, user_role="user") == OUTSIDE_ACCOUNT
    finally:
        os.rmdir(tmp)


# ── AC6: the wrapped history tools inside a run ────────────────────────────────────────

@pytest.mark.parametrize("tool_name", ["project_history", "project_rollback"])
def test_inside_a_run_a_model_written_project_path_is_judged_for_the_caller(world, tool_name):
    """MUTATION: drop the identity_kwargs the wrapper shows the funnel - red (B's history)."""
    from vaf.tools.coder import _base_dir_tool, _coder_funnel
    from vaf.tools.project_git import ProjectHistoryTool, ProjectRollbackTool
    a_proj = world.a_root / "chat" / "mine"
    a_proj.mkdir(parents=True)
    tools = {"project_history": _base_dir_tool(ProjectHistoryTool, str(a_proj)),
             "project_rollback": _base_dir_tool(ProjectRollbackTool, str(a_proj))}
    funnel = _coder_funnel(tools, scope=A, role="user", session_id=None)
    (world.b_proj / "secret.txt").write_text("v2\n")
    _git("commit", "-qam", "B second commit", cwd=world.b_proj)
    before = _count(world.b_proj)
    args = {"project_path": str(world.b_proj)}
    if tool_name == "project_rollback":
        args["commit"] = _first_sha(world.b_proj)
    out = funnel.execute(tool_name, args)
    assert "outside the folders this account may write" in out
    assert "B private commit" not in out
    assert _count(world.b_proj) == before
