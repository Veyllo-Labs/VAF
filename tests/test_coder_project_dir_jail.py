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

The last section, "The same tools on the main agent", pins the six tools the main agent
has as well (the four git tools, update_codex, add_memory) per identity with real git; its
mutations are listed at its head.
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
    tools = {"project_history": _base_dir_tool(ProjectHistoryTool, str(a_proj),
                                               owner_scope=A, owner_role="user"),
             "project_rollback": _base_dir_tool(ProjectRollbackTool, str(a_proj),
                                                owner_scope=A, owner_role="user")}
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


# ── the base-dir wrappers ask the folder themselves ────────────────────────────────────

@pytest.mark.parametrize("tool_name", ["GitStatusTool", "GitLogTool", "GitInitTool"])
def test_a_wrapped_git_tool_never_runs_in_another_accounts_folder(world, monkeypatch, tool_name):
    """Today only _choose_base_dir keeps a run's folder in the caller's tree; the wrapper now
    asks again, so a future caller that hands it a foreign folder fails closed.
    MUTATION: remove the project_dir_refusal check in GitToolWrapper.run - red."""
    import vaf.core.git_runner as git_runner
    import vaf.tools.coder as coder

    def _no_git(*a, **k):
        raise AssertionError("git ran in another account's folder")

    monkeypatch.setattr(git_runner, "run_project_git", _no_git)
    wrapped = coder._base_dir_tool(getattr(coder, tool_name), str(world.b_proj),
                                   owner_scope=A, owner_role="user")
    out = wrapped.run()
    assert out.startswith(f"Error: cannot work in '{world.b_proj}'")
    assert OUTSIDE_ACCOUNT in out
    # The caller's own folder still works.
    mine = world.a_root / "chat" / "site"
    mine.mkdir(parents=True)
    monkeypatch.setattr(git_runner, "run_project_git",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert not coder._base_dir_tool(getattr(coder, tool_name), str(mine), owner_scope=A,
                                    owner_role="user").run().startswith("Error: cannot work")


def test_the_wrapper_requires_the_callers_identity(world):
    """MUTATION: give owner_scope/owner_role a default - red."""
    from vaf.tools.coder import GitStatusTool, _base_dir_tool
    with pytest.raises(TypeError):
        _base_dir_tool(GitStatusTool, str(world.b_proj))


def test_the_coder_shell_comment_no_longer_promises_docker():
    """run_in_workspace refuses every docker command; the comment said the opposite."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vaf" / "tools" / "coder.py").read_text(
        encoding="utf-8")
    assert "incl. host docker" not in src


# ── The same tools on the main agent ───────────────────────────────────────────────────
#
# The four git tools, update_codex and add_memory are registered for the main agent, its
# messengers, automations and workflows too, and a content-only coder run has the notes tools
# unwrapped; there base_dir is the model's. Measured before the change, as account A (role
# "user") through the chat lane's funnel: git_log printed B's history, git_add_commit committed
# into B's repository, the notes tools wrote B's .vaf, git_init and the notes tools created
# .git and .vaf in any folder, and a call naming no folder worked in the process's working
# folder, which for the installed service is VAF's own checkout.
#
# Mutations, each turning the named test red:
# - delete identity_kwargs from GitLogTool -> test_the_six_tools_declare_their_caller_and_
#   their_folder (and the git_log row of the tenant test: B's history printed);
# - restore the share-only tool_work_folder -> test_a_tenant_or_guest_stays_out_of_other_
#   folders (B's log printed, a commit made, .vaf written);
# - make tool_work_folder refuse always -> test_a_tenant_works_in_its_own_project;
# - pass user_role=None to project_dir_refusal in tool_work_folder -> the (A, "admin") row of
#   test_an_admin_the_owner_and_a_direct_consumer_keep_every_folder;
# - skip the check when base_dir == "." -> test_nobody_works_in_home_or_the_program_folder;
# - read the role from kwargs.get("role") -> test_a_model_written_admin_role_does_not_open_
#   a_folder;
# - delete identity_kwargs from AddMemoryTool -> test_the_unwrapped_notes_tools_in_a_coder_
#   run_are_confined;
# - delete identity_kwargs from UpdateCodexTool -> test_a_workflow_step_is_confined_to_its_
#   owner;
# - drop "git_init" from the thinking tuple -> test_thinking_runs_get_no_file_creating_
#   project_tools;
# - add "git_log" to FRONT_OFFICE_ALLOWED_TOOLS -> test_the_contact_lane_never_offers_them.

SIX = ("git_init", "git_add_commit", "git_status", "git_log", "update_codex", "add_memory")
# Every required argument of the six, so each call reaches run().
SIX_ARGS = {"message": "probe commit", "title": "probe title", "content": "probe content",
            "note": "probe note"}
UNSAFE = "not a project folder"


def _six():
    from vaf.tools.coder import GitAddCommitTool, GitInitTool, GitLogTool, GitStatusTool
    from vaf.tools.knowledge import AddMemoryTool, UpdateCodexTool
    return [GitInitTool(), GitAddCommitTool(), GitStatusTool(), GitLogTool(), UpdateCodexTool(),
            AddMemoryTool()]


def _six_funnel(scope, role):
    """The chat lane's funnel, as `Agent.execute_tool` and an embedder's ToolCaller run it."""
    from vaf.core.tool_dispatch import ToolCaller
    return ToolCaller({t.name: t for t in _six()}, user_scope_id=scope, user_role=role,
                      gate_enabled=False, max_result_chars=None)


def _repo(path, subject):
    path.mkdir(parents=True)
    (path / "file.txt").write_text(subject + "\n")
    _git("init", "-q", cwd=path)
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", subject, cwd=path)
    return path


def _log(repo):
    return _git("log", "--format=%H %s", cwd=repo).stdout


@pytest.fixture
def main_world(world, tmp_path, monkeypatch):
    """`world` plus an outside folder, an outside repository, a stand-in for VAF's program
    folder and a recorder around the real git runner."""
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(var, str(world.home / ".xdg" / var.lower()))
    for var, value in (("GIT_AUTHOR_NAME", "Probe"), ("GIT_COMMITTER_NAME", "Probe"),
                       ("GIT_AUTHOR_EMAIL", "probe@example.invalid"),
                       ("GIT_COMMITTER_EMAIL", "probe@example.invalid")):
        monkeypatch.setenv(var, value)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_repo = _repo(tmp_path / "outside-repo", "outside commit")
    # A stand-in, so a mutation that lets a call through writes here and never into the
    # checkout the tests run from.
    program = _repo(tmp_path / "vafroot", "program commit")
    import vaf.core.workspace_guard as workspace_guard
    monkeypatch.setattr(workspace_guard, "_VAF_ROOT", program.resolve())

    import vaf.core.git_runner as git_runner
    real_run = git_runner.run_project_git   # captured before the patch
    steps = []

    def _recording(args, cwd, **kw):
        steps.append((list(args), str(cwd)))
        return real_run(args, cwd, **kw)

    monkeypatch.setattr(git_runner, "run_project_git", _recording)
    world.outside, world.outside_repo, world.program, world.steps = (outside, outside_repo,
                                                                     program, steps)
    return world


def test_the_six_tools_declare_their_caller_and_their_folder():
    import inspect

    from vaf.tools.filesystem import tool_work_folder
    for tool in _six():
        cls = type(tool)
        assert cls.identity_kwargs == ("user_scope_id", "user_role"), cls.__name__
        assert cls.file_access is None, cls.__name__
        prop = cls.parameters["properties"].get("base_dir")
        assert prop and prop["type"] == "string", cls.__name__
        assert "base_dir" not in cls.parameters.get("required", []), cls.__name__
    src = inspect.getsource(tool_work_folder)
    assert "project_dir_refusal(" in src
    # The share question is project_dir_refusal's first one, not a second copy here.
    assert "is_remote_path" not in src


@pytest.mark.parametrize("role", ["user", "guest"])
def test_a_tenant_or_guest_stays_out_of_other_folders(main_world, monkeypatch, role):
    w = main_world
    funnel = _six_funnel(A, role)
    before = (_count(w.b_proj), _files(w.b_proj), _log(w.b_proj))
    outside_before = (_count(w.outside_repo), _log(w.outside_repo))
    for target in (w.b_proj, w.outside):
        for name in SIX:
            out = funnel.execute(name, {"base_dir": str(target), **SIX_ARGS})
            assert out.startswith(f"❌ Refused: cannot work in '{target}'"), (name, out)
            assert OUTSIDE_ACCOUNT in out and "base_dir" in out, (name, out)
            assert "B private commit" not in out
    # No folder named: the working folder, here another repository.
    monkeypatch.chdir(w.outside_repo)
    for name in SIX:
        out = funnel.execute(name, dict(SIX_ARGS))
        assert out.startswith("❌ Refused: cannot work in '.'"), (name, out)
        assert OUTSIDE_ACCOUNT in out
        # The folder is named as given: no absolute path of the working folder.
        assert str(w.outside_repo) not in out
    assert w.steps == [], f"a git step ran: {w.steps}"
    assert (_count(w.b_proj), _files(w.b_proj), _log(w.b_proj)) == before
    assert not (w.b_proj / ".vaf").exists()
    assert not (w.outside / ".git").exists() and not (w.outside / ".vaf").exists()
    assert (_count(w.outside_repo), _log(w.outside_repo)) == outside_before
    assert not (w.outside_repo / ".vaf").exists()


def test_a_tenant_works_in_its_own_project(main_world):
    w = main_world
    mine = w.a_root / "chat" / "site"
    mine.mkdir(parents=True)
    (mine / "index.html").write_text("<p>a</p>\n")
    funnel = _six_funnel(A, "user")
    at = {"base_dir": str(mine)}

    assert funnel.execute("git_init", at).startswith("✅"), "git_init"
    assert (mine / ".git").is_dir()
    assert funnel.execute("update_codex", {**at, "title": "Rule",
                                           "content": "codex-line"}).startswith("✅")
    assert "codex-line" in (mine / ".vaf" / "codex.md").read_text(encoding="utf-8")
    assert funnel.execute("add_memory", {**at, "note": "memory-line"}).startswith("✅")
    assert "memory-line" in (mine / ".vaf" / "memory.md").read_text(encoding="utf-8")
    out = funnel.execute("git_add_commit", {**at, "message": "own page",
                                            "files": ["index.html"]})
    assert out.startswith("✅"), out
    assert _count(mine) == "1"
    (mine / "new.txt").write_text("x\n")
    assert "new.txt" in funnel.execute("git_status", at)
    assert "own page" in funnel.execute("git_log", at)


def _local_admin():
    from vaf.core.config import get_local_admin_scope_id
    return get_local_admin_scope_id()


@pytest.mark.parametrize("who", ["admin", "local_admin", "no_account"])
def test_an_admin_the_owner_and_a_direct_consumer_keep_every_folder(main_world, who):
    w = main_world
    scope, role = {"admin": (A, "admin"), "local_admin": (_local_admin(), None),
                   "no_account": (None, None)}[who]
    funnel = _six_funnel(scope, role)
    b = {"base_dir": str(w.b_proj)}

    assert "B private commit" in funnel.execute("git_log", b)
    (w.b_proj / "new.txt").write_text("x\n")
    assert "new.txt" in funnel.execute("git_status", b)
    out = funnel.execute("git_add_commit", {**b, "message": "admin commit", "files": ["new.txt"]})
    assert out.startswith("✅"), out
    assert _count(w.b_proj) == "2"
    assert funnel.execute("update_codex", {**b, "title": "t", "content": "c"}).startswith("✅")
    assert funnel.execute("add_memory", {**b, "note": "n"}).startswith("✅")
    assert (w.b_proj / ".vaf" / "codex.md").is_file()
    assert (w.b_proj / ".vaf" / "memory.md").is_file()

    o = {"base_dir": str(w.outside)}
    assert funnel.execute("git_init", o).startswith("✅")
    assert (w.outside / ".git").is_dir()
    assert funnel.execute("add_memory", {**o, "note": "n"}).startswith("✅")
    assert (w.outside / ".vaf" / "memory.md").is_file()


IDENTITIES = {"user": (A, "user"), "guest": (A, "guest"), "admin": (A, "admin"),
              "local_admin": None, "no_account": (None, None)}


@pytest.mark.parametrize("who", sorted(IDENTITIES))
def test_nobody_works_in_home_or_the_program_folder(main_world, monkeypatch, who):
    w = main_world
    scope, role = IDENTITIES[who] or (_local_admin(), None)
    funnel = _six_funnel(scope, role)
    program_before = (_count(w.program), _log(w.program))
    for target in (w.home, w.program):
        for name in SIX:
            out = funnel.execute(name, {"base_dir": str(target), **SIX_ARGS})
            assert out.startswith(f"❌ Refused: cannot work in '{target}'"), (name, out)
            assert UNSAFE in out, (name, out)
    monkeypatch.chdir(w.program)
    for name in SIX:
        out = funnel.execute(name, dict(SIX_ARGS))
        assert out.startswith("❌ Refused: cannot work in '.'"), (name, out)
        assert UNSAFE in out, (name, out)
    assert w.steps == [], f"a git step ran: {w.steps}"
    assert (_count(w.program), _log(w.program)) == program_before
    assert not (w.program / ".vaf").exists()
    assert not (w.home / ".git").exists() and not (w.home / ".vaf").exists()


def test_a_model_written_admin_role_does_not_open_a_folder(main_world):
    """The dispatcher assigns the declared keys over the model's; a key it does not assign
    ("role") must not be read either."""
    w = main_world
    funnel = _six_funnel(A, "user")
    spoof = {"base_dir": str(w.b_proj), "user_role": "admin", "user_scope_id": None,
             "role": "admin", **SIX_ARGS}
    for name in SIX:
        out = funnel.execute(name, dict(spoof))
        assert OUTSIDE_ACCOUNT in out, (name, out)
        assert "B private commit" not in out
    assert w.steps == []
    assert not (w.b_proj / ".vaf").exists() and _count(w.b_proj) == "1"


def test_the_unwrapped_notes_tools_in_a_coder_run_are_confined(main_world):
    """A content-only run registers the raw notes tools (no base-folder wrapper); its funnel
    assigns the run's caller like the chat lane's."""
    from vaf.tools.coder import _coder_funnel
    from vaf.tools.knowledge import AddMemoryTool, UpdateCodexTool
    w = main_world
    funnel = _coder_funnel({"add_memory": AddMemoryTool(), "update_codex": UpdateCodexTool()},
                           scope=A, role="user", session_id=None)
    for name in ("add_memory", "update_codex"):
        out = funnel.execute(name, {"base_dir": str(w.b_proj), **SIX_ARGS})
        assert OUTSIDE_ACCOUNT in out, (name, out)
    assert not (w.b_proj / ".vaf").exists()


def test_a_workflow_step_is_confined_to_its_owner(main_world):
    from unittest.mock import patch

    from vaf.tools.knowledge import UpdateCodexTool
    from vaf.workflows.engine import WorkflowEngine, WorkflowStep
    w = main_world
    answers = []

    class _Recorded(UpdateCodexTool):
        def run(self, **kwargs):
            answers.append(super().run(**kwargs))
            return answers[-1]

    with patch("vaf.core.config.Config.get",
               side_effect=lambda k, d=None: "declared" if k == "workflow_identity_injection"
               else d):
        engine = WorkflowEngine(tools={"update_codex": _Recorded()}, callback=lambda *a, **k: None,
                                user_scope_id=A, username="tenant", user_role="user")
        engine.execute([WorkflowStep(tool="update_codex", input_template="", output_name="o",
                                     args_template={"base_dir": str(w.b_proj), "title": "t",
                                                    "content": "c"})], variables={})
    assert len(answers) == 1 and OUTSIDE_ACCOUNT in answers[0], answers
    assert not (w.b_proj / ".vaf").exists()


def test_thinking_runs_get_no_file_creating_project_tools():
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "agent.py").read_bytes().decode()
    block = re.search(r"if _rk_thinking:\s*\n\s*if instance\.name in \(([^)]*)\)", src)
    assert block, "the thinking exclusion tuple moved"
    excluded = set(re.findall(r'"([a-z_]+)"', block.group(1)))
    assert set(SIX) <= excluded, sorted(set(SIX) - excluded)


def test_the_contact_lane_never_offers_them():
    from vaf.core.front_office_tools import FRONT_OFFICE_ALLOWED_TOOLS
    assert not set(SIX) & set(FRONT_OFFICE_ALLOWED_TOOLS)


# ── The folder as judged, and links in a project's .vaf ────────────────────────────────
#
# Measured after the folder rule, as account A (role "user") in its OWN project: a
# .vaf/memory.md linked to ~/.bashrc took add_memory's note as a shell line, a .vaf/codex.md
# linked to B's file took update_codex's text, and a .vaf linked to B's .vaf took both; the
# coder reads codex.md and memory.md back into its prompt. And a '~/...' folder was judged
# expanded but used as written, so the notes landed in a '~' folder below the working folder.
#
# Mutations, each turning the named test red:
# - PersistenceManager._append_file opens with open(path, "a") again -> test_a_link_in_
#   the_accounts_own_notes_folder_is_not_written_through[memory_to_home_config/codex_to_
#   other_account];
# - _own_folder drops its islink and realpath checks -> test_a_linked_notes_folder_is_
#   not_used;
# - _open_write drops expect_folder -> test_a_notes_folder_swapped_after_the_check_is_
#   refused;
# - _project_notes drops the jail question -> test_the_notes_tools_ask_the_jail_for_the_
#   real_notes_folder;
# - _read_file opens the given path with open() again -> test_the_coder_never_reads_notes_
#   through_a_link;
# - _read_file opens the file by its path instead of relative to the checked folder handle
#   -> test_a_notes_folder_swapped_around_the_read_is_not_followed;
# - notes_folder no longer makes a deleted folder again -> test_a_deleted_notes_folder_is_
#   made_again;
# - a deleted folder is asked _own_folder again (resolved anew) -> test_a_deleted_notes_
#   folder_is_not_made_again_behind_a_swapped_project_folder;
# - _own_folder drops its realpath comparison -> test_a_notes_folder_that_resolves_elsewhere_
#   is_not_used;
# - the read's Windows branch opens without its link and folder checks -> test_the_read_
#   without_folder_handles_reads_its_own_file_and_no_link;
# - tool_work_folder returns the raw base_dir again -> test_a_home_relative_folder_is_
#   the_folder_that_was_judged;
# - the context tools take base_dir from the call again -> test_the_store_tools_take_no_
#   folder_from_the_model.


def _symlink_or_skip(link, target):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")


@pytest.fixture
def own_project(main_world):
    mine = main_world.a_root / "chat" / "site"
    (mine / ".vaf").mkdir(parents=True)
    main_world.mine = mine
    (main_world.b_proj / ".vaf").mkdir()
    (main_world.b_proj / ".vaf" / "codex.md").write_text("B-CODEX\n")
    return main_world


@pytest.mark.parametrize("shape", ["memory_to_home_config", "codex_to_other_account"])
def test_a_link_in_the_accounts_own_notes_folder_is_not_written_through(own_project, shape):
    w = own_project
    funnel = _six_funnel(A, "user")
    if shape == "memory_to_home_config":
        target = w.home / ".bashrc"
        target.write_text("# rc\n")
        _symlink_or_skip(w.mine / ".vaf" / "memory.md", target)
        out = funnel.execute("add_memory", {"base_dir": str(w.mine), "note": "touch /tmp/x"})
    else:
        target = w.b_proj / "secret.txt"
        _symlink_or_skip(w.mine / ".vaf" / "codex.md", target)
        out = funnel.execute("update_codex", {"base_dir": str(w.mine), "title": "t",
                                              "content": "INJECTED"})
    before = target.read_text()
    assert out.startswith("❌"), out
    assert target.read_text() == before
    assert "touch /tmp/x" not in before and "INJECTED" not in before


def test_a_linked_notes_folder_is_not_used(own_project):
    from vaf.core.persistence import PersistenceManager
    w = own_project
    linked = w.a_root / "chat" / "linked"
    linked.mkdir(parents=True)
    _symlink_or_skip(linked / ".vaf", w.b_proj / ".vaf")
    assert PersistenceManager(str(linked)).notes_folder() is None
    funnel = _six_funnel(A, "user")
    for name, args in (("update_codex", {"title": "t", "content": "X"}),
                       ("add_memory", {"note": "X"})):
        out = funnel.execute(name, {"base_dir": str(linked), **args})
        assert out.startswith("❌ Refused: the project's .vaf is a link"), (name, out)
    assert sorted(os.listdir(w.b_proj / ".vaf")) == ["codex.md"]
    assert (w.b_proj / ".vaf" / "codex.md").read_text() == "B-CODEX\n"
    # The account's own ordinary notes folder still works.
    assert funnel.execute("add_memory", {"base_dir": str(w.mine), "note": "mine"}).startswith("✅")
    assert "mine" in (w.mine / ".vaf" / "memory.md").read_text(encoding="utf-8")


def test_a_notes_folder_swapped_after_the_check_is_refused(own_project):
    """The folder is asked once; a .vaf swapped for a link afterwards is refused by the open
    (open_for_write's expect_folder), not followed."""
    from vaf.core.path_jail import WriteRefused
    from vaf.core.persistence import PersistenceManager
    w = own_project
    pm = PersistenceManager(str(w.mine))
    assert pm.notes_folder() == os.path.join(os.path.realpath(w.mine), ".vaf")
    os.rename(w.mine / ".vaf", w.mine / ".vaf-old")
    _symlink_or_skip(w.mine / ".vaf", w.b_proj / ".vaf")
    with pytest.raises(WriteRefused):
        pm.append_codex("SWAPPED")
    assert (w.b_proj / ".vaf" / "codex.md").read_text() == "B-CODEX\n"


def test_the_notes_tools_ask_the_jail_for_the_real_notes_folder(own_project, monkeypatch):
    """A project folder that leads elsewhere after it was judged (simulated by the judged
    folder itself) meets the caller's jail again on the notes folder's real path."""
    import vaf.tools.knowledge as knowledge
    w = own_project
    monkeypatch.setattr(knowledge, "tool_work_folder", lambda kwargs: (str(w.b_proj), ""))
    funnel = _six_funnel(A, "user")
    out = funnel.execute("update_codex", {"base_dir": str(w.mine), "title": "t", "content": "X"})
    assert out.startswith("❌ Refused: the project's .vaf leads outside"), out
    assert (w.b_proj / ".vaf" / "codex.md").read_text() == "B-CODEX\n"


def test_the_coder_never_reads_notes_through_a_link(own_project):
    """The coder puts codex.md and memory.md into its prompt (PersistenceManager.get_codex /
    get_memory); a link there would carry another account's file into the run."""
    from vaf.core.persistence import PersistenceManager
    w = own_project
    _symlink_or_skip(w.mine / ".vaf" / "codex.md", w.b_proj / "secret.txt")
    (w.mine / ".vaf" / "memory.md").write_text("own memory\n")
    pm = PersistenceManager(str(w.mine))
    assert pm.get_codex() == ""
    assert pm.get_memory() == "own memory\n"
    linked = w.a_root / "chat" / "linked"
    linked.mkdir(parents=True)
    os.symlink(w.b_proj / ".vaf", linked / ".vaf")
    assert PersistenceManager(str(linked)).get_codex() == ""


def test_a_notes_folder_swapped_around_the_read_is_not_followed(own_project, monkeypatch):
    """.vaf swapped for a link into B's .vaf right before the file is opened and restored
    right after: the read is relative to the folder handle checked before, so it still reads
    A's own file. Driven deterministically through os.open."""
    from vaf.core import path_jail
    from vaf.core.persistence import PersistenceManager
    if not path_jail._DIR_FD:
        pytest.skip("no folder handles here (the Windows branch is a named boundary)")
    w = own_project
    vaf, aside = w.mine / ".vaf", w.mine / ".vaf-aside"
    (vaf / "codex.md").write_text("A-OWN\n")
    try:
        os.symlink(w.b_proj / "secret.txt", w.mine / "probe-link")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    pm = PersistenceManager(str(w.mine))
    assert pm.notes_folder()
    real_open = os.open        # captured before the patch
    swapped = []

    def _swapping(path, flags, *args, **kwargs):
        if os.fspath(path).endswith("codex.md") and not swapped:
            swapped.append(path)
            os.rename(vaf, aside)
            os.symlink(w.b_proj / ".vaf", vaf)
            try:
                return real_open(path, flags, *args, **kwargs)
            finally:
                os.unlink(vaf)
                os.rename(aside, vaf)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _swapping)
    text = pm.get_codex()
    monkeypatch.setattr(os, "open", real_open)
    assert swapped, "the swap never ran"
    assert "B-CODEX" not in text
    assert text == "A-OWN\n"


def test_the_read_without_folder_handles_reads_its_own_file_and_no_link(own_project,
                                                                       monkeypatch):
    """The Windows branch (no folder handles, no O_NOFOLLOW), taken on any host.
    MUTATION: open the joined path without the islink and resolved-folder checks there - red
    (B's file read)."""
    from vaf.core import path_jail
    from vaf.core.persistence import PersistenceManager
    w = own_project
    monkeypatch.setattr(path_jail, "_DIR_FD", False)
    (w.mine / ".vaf" / "memory.md").write_text("own memory\n")
    _symlink_or_skip(w.mine / ".vaf" / "codex.md", w.b_proj / "secret.txt")
    pm = PersistenceManager(str(w.mine))
    assert pm.get_memory() == "own memory\n"
    assert pm.get_codex() == ""


def test_a_deleted_notes_folder_is_made_again(own_project):
    import shutil

    from vaf.core.persistence import PersistenceManager, ProjectState
    w = own_project
    pm = PersistenceManager(str(w.mine))
    assert pm.notes_folder()
    shutil.rmtree(w.mine / ".vaf")
    pm.save_state(ProjectState(project_name="site"))
    assert (w.mine / ".vaf" / "tasks.json").is_file()
    shutil.rmtree(w.mine / ".vaf")
    pm.append_memory("again")
    assert "again" in (w.mine / ".vaf" / "memory.md").read_text(encoding="utf-8")


def test_a_deleted_notes_folder_is_not_made_again_behind_a_swapped_project_folder(own_project):
    """Deleted, and the project folder swapped for a link into B meanwhile: the folder is made
    again only at the real path that was judged, never resolved anew into B's tree."""
    import shutil

    from vaf.core.path_jail import WriteRefused
    from vaf.core.persistence import PersistenceManager
    w = own_project
    pm = PersistenceManager(str(w.mine))
    assert pm.notes_folder()
    shutil.rmtree(w.mine / ".vaf")
    # A folder of B's with no .vaf: the pinned path is then gone too, and only making it
    # again decides where the note lands.
    other = w.b_root / "other"
    other.mkdir()
    os.rename(w.mine, w.mine.parent / "site-aside")
    _symlink_or_skip(w.mine, other)
    with pytest.raises(WriteRefused):
        pm.append_memory("SWAPPED")
    assert not (other / ".vaf").exists()


def test_a_notes_folder_that_resolves_elsewhere_is_not_used(own_project, monkeypatch):
    """A junction is no link to islink; resolved, it lands elsewhere. Junctions exist on
    Windows, which has no folder handles: the path branch, taken on any host, simulated by the
    resolution of the .vaf path (real os.path.realpath captured before the patch)."""
    from vaf.core import path_jail
    from vaf.core.persistence import PersistenceManager
    w = own_project
    monkeypatch.setattr(path_jail, "_DIR_FD", False)
    vaf = str(w.mine / ".vaf")
    real_realpath = os.path.realpath
    monkeypatch.setattr(os.path, "realpath",
                        lambda p, *a, **k: str(w.b_proj / ".vaf") if os.fspath(p) == vaf
                        else real_realpath(p, *a, **k))
    assert PersistenceManager(str(w.mine)).notes_folder() is None


@pytest.mark.parametrize("name", ["update_codex", "git_init"])
def test_a_home_relative_folder_is_the_folder_that_was_judged(main_world, monkeypatch, name):
    w = main_world
    monkeypatch.chdir(w.program)
    spelled = f"~/Documents/VAF_Projects/{A[:8]}/tproj"
    proj = w.a_root / "tproj"
    if name == "git_init":
        proj.mkdir()            # git_init works in an existing folder only, as before
    args = {"base_dir": spelled, "title": "t", "content": "home-relative"}
    out = _six_funnel(A, "user").execute(name, args)
    assert out.startswith("✅"), out
    assert not (w.program / "~").exists()
    if name == "update_codex":
        assert "home-relative" in (proj / ".vaf" / "codex.md").read_text(encoding="utf-8")
    else:
        assert (proj / ".git").is_dir()
    assert (_count(w.program), _log(w.program).count("\n")) == ("1", 1)


STORE_TOOLS = (("update_intent", {"intent": "probe intent"}),
               ("update_working_memory", {"add_task": "probe task"}),
               ("add_task", {"task": "probe task two"}))


@pytest.mark.parametrize("who", ["user", "admin", "no_account"])
def test_the_store_tools_take_no_folder_from_the_model(main_world, monkeypatch, who):
    """update_intent, update_working_memory and add_task keep the chat's store under the
    process's working folder, where every reader of it looks; a base_dir in the call is not
    a folder they use."""
    from vaf.core.tool_dispatch import ToolCaller
    from vaf.tools.context_tools import (AddTaskAliasTool, RequestClarificationTool,
                                         UpdateIntentTool, UpdateWorkingMemoryTool)
    w = main_world
    server = w.home.parent / "server"
    server.mkdir()
    monkeypatch.chdir(server)
    scope, role = {"user": (A, "user"), "admin": (A, "admin"), "no_account": (None, None)}[who]
    tools = (UpdateIntentTool(), UpdateWorkingMemoryTool(), AddTaskAliasTool())
    funnel = ToolCaller({t.name: t for t in tools}, user_scope_id=scope, user_role=role,
                        gate_enabled=False, max_result_chars=None)
    for name, args in STORE_TOOLS:
        out = funnel.execute(name, {"base_dir": str(w.b_proj), **args})
        assert "✅" in out, (name, out)
    out = RequestClarificationTool().run(base_dir=str(w.b_proj), question="q", task_id="t1")
    assert out.startswith("✅"), out
    assert not (w.b_proj / ".vaf").exists()
    from vaf.core.main_persistence import MainPersistenceManager
    store = MainPersistenceManager(str(server))
    assert store.get_user_intent() == "probe intent"
    assert [t.get("text") for t in store.get_working_memory().get("tasks", [])] == [
        "probe task", "probe task two"]
