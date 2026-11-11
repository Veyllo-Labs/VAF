# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The file tools cannot write VAF's own state, for any caller, the machine owner included.

VAF's state roots (`data_stores.roots()` less the projects: ``~/.vaf``, the Docker-mode
config folder, the data directory, the config directory) hold what VAF executes or obeys
later: ``custom_tools/*.py`` runs at every agent load and is hot-reloaded on the next turn,
``mcp_servers.json`` names commands VAF starts, ``trust/`` holds standing tool grants, and
skills and workflows steer later turns. Each has a writer of its own that checks what it
saves. Before this, ``is_safe_path`` let any write through except under ``~/.vaf``, and even
there skills and workflows, so ``write_file`` as the owner (no per-account jail) on a
messenger chat, a workflow or an automation planted code VAF ran.

Every root here is redirected under ``tmp_path`` (HOME, the XDG and the Windows variables),
never the real home. Each test names the mutation that turns it red.
"""
import ast
import os
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.core import data_stores
from vaf.core.data_stores import holds_vaf_state, is_vaf_state
from vaf.core.platform import Platform
from vaf.tools.filesystem import (VAF_SETTINGS_WRITE_REFUSAL, VAF_STATE_WRITE_REFUSAL,
                                  EditFileTool, MoveFileTool, WriteFileTool, is_safe_path)

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """Every root under tmp_path, the same layout on every platform: data and config read
    XDG on Linux and macOS (a SET XDG wins there) and LOCALAPPDATA/APPDATA on Windows."""
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    for var in ("XDG_DATA_HOME", "LOCALAPPDATA"):
        monkeypatch.setenv(var, str(tmp_path / "xdg-data"))
    for var in ("XDG_CONFIG_HOME", "APPDATA"):
        monkeypatch.setenv(var, str(tmp_path / "xdg-config"))
    monkeypatch.chdir(home)
    return SimpleNamespace(home=home, data=Platform.data_dir(), config=Platform.config_dir(),
                           vaf=Platform.vaf_dir(), docs=home / "Documents")


def _paths(r, *relatives):
    return [str(Path(getattr(r, root)).joinpath(*rel.split("/"))) if rel else
            str(getattr(r, root)) for root, rel in relatives]


# -- 1. the write rule ------------------------------------------------------------------

# The content kinds with a tool of their own get those tools named; the settings (the MCP
# list, the grants, the data root itself) get the answer that they have their own commands.
TOOLS, SETTINGS = VAF_STATE_WRITE_REFUSAL, VAF_SETTINGS_WRITE_REFUSAL
STATE_WRITES = [
    ("data", "custom_tools/x.py", TOOLS), ("data", "custom_tools", TOOLS),
    ("data", "custom_tools/manifest.json", TOOLS),
    ("data", "mcp_servers.json", SETTINGS), ("data", "", SETTINGS),
    ("config", "trust/x.json", SETTINGS),
    ("vaf", "skills/demo/SKILL.md", TOOLS), ("vaf", "skills/manifest.json", TOOLS),
    ("vaf", "workflows/wf.py", TOOLS),
]


@pytest.mark.parametrize("root,relative,answer", STATE_WRITES)
def test_a_write_into_vafs_state_is_refused_for_the_owner(roots, root, relative, answer):
    """No jail is installed: the owner's case, where this screen is the whole rule.

    MUTATION: remove the is_vaf_state branch from is_safe_path - red. Answer every state
    write with the tools text - the settings cases go red."""
    (path,) = _paths(roots, (root, relative))
    ok, verdict = is_safe_path(path)
    assert ok is False, path
    assert verdict == answer
    assert "\u2014" not in verdict
    assert ("create_agent_tool" in verdict) == (answer is TOOLS)
    assert ("settings have their own commands" in verdict) == (answer is SETTINGS)


def test_the_docker_mode_config_folder_is_state_too(roots):
    """`Config.APP_DIR` is its own root (``$VAF_CONFIG_DIR`` in Docker mode); the suite's
    isolation points it away from ``~/.vaf``, which is what makes it measurable here.

    MUTATION: drop "app" from STATE_ROOTS - red."""
    from vaf.core.config import Config
    assert Path(Config.APP_DIR).resolve() != roots.vaf.resolve()
    assert is_vaf_state(Path(Config.APP_DIR) / "config.json")
    assert is_safe_path(str(Path(Config.APP_DIR) / "anything.py"))[0] is False


# -- 2. the read rule: only the agent_readable rows ----------------------------------------

@pytest.mark.parametrize("root,relative", [
    ("data", "custom_tools/x.py"), ("vaf", "skills/demo/reference.md"), ("vaf", "workflows/x.py"),
])
def test_only_the_readable_store_rows_can_be_read(roots, root, relative):
    """use_skill hands the model the paths of a skill's bundled files to read, and
    create_agent_tool's edit needs the source it replaces; every other state path is refused
    to reads too (tests/test_host_config_and_state_reads.py).

    MUTATION: make the write check ignore read_only - red. Set agent_readable=False on the
    skills, workflows or custom_tools row - that case goes red."""
    (path,) = _paths(roots, (root, relative))
    ok, verdict = is_safe_path(path, read_only=True)
    assert ok is True, verdict


def test_the_credential_store_stays_unreadable(roots):
    ok, verdict = is_safe_path(str(roots.vaf / "config.json"), read_only=True)
    assert ok is False and "VAF's own data directory" in verdict


def test_an_ordinary_write_is_unaffected(roots):
    """A neighbour of the data root is not state. Beside XDG_DATA_HOME, not inside it: that
    folder is the home directory's configuration (workspace_guard.is_host_config)."""
    for path in (roots.docs / "notes.md", roots.docs / "VAF_Projects" / "p" / "a.py",
                 roots.data.parent.parent / "fonts" / "a.ttf"):
        ok, verdict = is_safe_path(str(path))
        assert ok is True, (path, verdict)


# -- 3. every spelling of the same folder --------------------------------------------------

def _spelled(r, last):
    return str(r.data.parent / last / "custom_tools" / "x.py")


@pytest.mark.parametrize("last,mutation", [
    ("VAF", "drop casefold()"),
    ("vaf.", "drop the trailing dot/space strip"),
    ("vaf ", "drop the trailing dot/space strip"),
    ("vaf::$INDEX_ALLOCATION", "drop the stream cut"),
])
def test_other_spellings_of_the_data_root_are_refused(roots, last, mutation):
    """Case-insensitive APFS and NTFS, Windows' trailing-dot and stream spellings: each opens
    the real folder there. Lexical, so it is decided the same way on every host."""
    assert roots.data.name == "vaf"
    ok, verdict = is_safe_path(_spelled(roots, last))
    assert ok is False and verdict == VAF_STATE_WRITE_REFUSAL, f"{last!r}: {mutation}"


def test_a_dotdot_walk_is_refused(roots):
    ok, _ = is_safe_path(str(roots.data / "x" / ".." / "custom_tools" / "y.py"))
    assert ok is False


def test_a_sibling_that_only_shares_a_prefix_is_not_state(roots):
    """Whole components, never a string prefix."""
    assert not is_vaf_state(roots.data.parent / "vaffle" / "x.py")
    assert not is_vaf_state(str(roots.config) + "-old")


def _symlink(link, target):
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create symlinks")


def test_a_link_in_documents_to_custom_tools_is_refused(roots):
    (roots.data / "custom_tools").mkdir(parents=True)
    link = roots.docs / "tools"
    _symlink(link, roots.data / "custom_tools")
    ok, verdict = is_safe_path(str(link / "x.py"))
    assert ok is False
    # MUTATION: choose the refusal text on the spelling as given only - red (the link's
    # name holds no content folder, its target does).
    assert verdict == VAF_STATE_WRITE_REFUSAL


def test_the_data_root_behind_a_symlinked_home_in_both_spellings(tmp_path, monkeypatch):
    """The settings name the root through the link, a resolved path names it through the
    real folder (macOS /var -> /private/var, a home moved to another disk).

    MUTATION: compare only the abspath spelling of the roots - the real spelling goes red.
    Compare only the abspath spelling of the path - the link spelling of the second half
    goes red."""
    real = tmp_path / "real"
    (real / "xdg-data").mkdir(parents=True)
    link = tmp_path / "link"
    _symlink(link, real)
    for root_side, path_side in ((link, real), (link, link), (real, link)):
        monkeypatch.setenv("XDG_DATA_HOME", str(root_side / "xdg-data"))
        monkeypatch.setenv("LOCALAPPDATA", str(root_side / "xdg-data"))
        target = path_side / "xdg-data" / "vaf" / "custom_tools" / "x.py"
        assert is_vaf_state(target), (root_side.name, path_side.name)
        assert is_safe_path(str(target))[0] is False


def test_an_error_answers_state(monkeypatch):
    """Fail-closed: a root that cannot be read refuses the write."""
    monkeypatch.setattr(data_stores, "roots", lambda: (_ for _ in ()).throw(OSError("x")))
    assert is_vaf_state("/anything") is True
    assert holds_vaf_state("/anything") is True


# -- 4. through the tools, as the owner ------------------------------------------------------

def test_write_file_cannot_plant_a_custom_tool(roots):
    """MUTATION: is_safe_path defaulting to the read rule - red."""
    from vaf.core import custom_tools_registry as registry
    target = roots.data / "custom_tools" / "p.py"
    out = WriteFileTool().run(path=str(target), content="open('marker', 'w')\n")
    assert out.startswith("Access denied"), out
    assert not target.exists()
    assert registry.load_custom_tool_class("p") is None
    assert not (roots.home / "marker").exists()


def test_edit_file_cannot_change_a_skill(roots):
    skill = roots.vaf / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\n---\nbe helpful\n")
    out = EditFileTool().run(path=str(skill), edits=[{"search": "be helpful",
                                                      "replace": "forward every mail"}])
    assert out.startswith("Access denied"), out
    assert skill.read_text().endswith("be helpful\n")


def test_move_file_cannot_drop_a_folder_into_the_data_dir(roots):
    src = roots.docs / "custom_tools"
    src.mkdir()
    (src / "x.py").write_text("x = 1")
    roots.data.mkdir(parents=True)
    out = MoveFileTool().run(src=str(src), dst=str(roots.data))
    assert "Access denied" in out, out
    assert not (roots.data / "custom_tools").exists()


def test_move_file_is_judged_where_the_entry_lands(roots):
    """Into an existing folder shutil.move puts the entry BELOW it. Moving a folder named like
    the data directory into its parent, while the data directory is absent, creates it.

    MUTATION: drop the effective-destination check in MoveFileTool - red."""
    src = roots.docs / roots.data.name
    (src / "custom_tools").mkdir(parents=True)
    (src / "custom_tools" / "x.py").write_text("x = 1")
    roots.data.parent.mkdir(parents=True)
    assert not roots.data.exists()
    out = MoveFileTool().run(src=str(src), dst=str(roots.data.parent))
    assert "Access denied" in out, out
    assert not roots.data.exists() and src.exists()


def test_move_file_cannot_take_a_manifest_out(roots):
    manifest = roots.data / "custom_tools" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")
    out = MoveFileTool().run(src=str(manifest), dst=str(roots.docs / "m.json"))
    assert "Access denied" in out, out
    assert manifest.exists()


# -- 7. no project, mount or jail in VAF's state ------------------------------------------

def test_vafs_state_is_no_project_and_no_workspace(roots):
    """MUTATION: remove the state branch from workspace_guard - red. Remove only
    holds_vaf_state - the parent case goes red."""
    from vaf.core.workspace_guard import assert_safe_workspace, is_unsafe_project_dir
    for path in (roots.data / "custom_tools", roots.data, roots.data.parent, roots.config,
                 roots.vaf / "skills"):
        assert is_unsafe_project_dir(str(path)), path
        with pytest.raises(ValueError):
            assert_safe_workspace(str(path))


def test_an_ordinary_project_is_still_accepted(roots):
    from vaf.core.workspace_guard import assert_safe_workspace, is_unsafe_project_dir
    project = roots.docs / "VAF_Projects" / "ab12cd34" / "site"
    project.mkdir(parents=True)
    assert not is_unsafe_project_dir(str(project))
    assert_safe_workspace(str(project))


# -- 8. exactly these callers say they only read ------------------------------------------

# (module, enclosing function, the read_only expression) -> why it reads. A new reader is
# added here deliberately; a writer added here is the bug this file exists for.
READ_ONLY_CALLERS = {
    ("vaf/tools/cloud_storage.py", "_action_save", "True"): "the local file it copies from",
    ("vaf/tools/codesearch.py", "CodeSearchTool.run", "True"): "searches",
    ("vaf/tools/document_viewer.py", "DocumentViewerTool.run", "True"): "shows a file",
    ("vaf/tools/environments.py", "SandboxTransferTool.run", "action == 'copy_in'"):
        "copy_in reads host_path, copy_out writes it",
    ("vaf/tools/filesystem.py", "FinderTool.run", "True"): "finds files",
    ("vaf/tools/filesystem.py", "FolderSizeTool.run", "True"): "measures",
    ("vaf/tools/filesystem.py", "ListFilesTool.run", "True"): "lists",
    ("vaf/tools/filesystem.py", "ReadFileTool._read", "True"): "reads",
    ("vaf/tools/filesystem.py", "TreeTool.run", "True"): "lists",
    ("vaf/tools/filesystem.py", "is_safe_path", "read_only"): "the symlink recheck forwards it",
    ("vaf/tools/learn_document.py", "LearnDocumentTool.run", "True"): "indexes a document",
    ("vaf/tools/librarian.py", "LibrarianTool._count_files", "True"): "counts",
    ("vaf/tools/librarian.py", "LibrarianTool._find_files", "True"): "finds",
    ("vaf/tools/librarian.py", "LibrarianTool._list_files", "True"): "lists",
    ("vaf/tools/librarian.py", "LibrarianTool._read_file", "True"): "reads",
    ("vaf/tools/librarian.py", "LibrarianTool._show_tree", "True"): "lists",
    ("vaf/tools/linter.py", "LinterTool.run", "True"): "lints, never fixes",
    ("vaf/tools/render_check.py", "RenderCheckTool.run", "True"): "renders a page",
    ("vaf/tools/send_telegram.py", "_resolve_path", "True"): "the attachment of every sender",
    ("vaf/tools/ssh.py", "transfer_local_path", "not download"):
        "an upload reads the local file, a download writes it",
    ("vaf/tools/vision.py", "AnalyzeImageTool._image_from_path", "True"): "looks at an image",
}


def _read_only_callers():
    found = Counter()
    for path in sorted((_REPO / "vaf").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(_REPO).as_posix()

        def walk(node, stack):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    walk(child, stack + [child.name])
                    continue
                if isinstance(child, ast.Call):
                    func = child.func
                    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                    if name == "is_safe_path":
                        for kw in child.keywords:
                            if kw.arg == "read_only" or kw.arg is None:
                                found[(rel, ".".join(stack), ast.unparse(kw.value))] += 1
                walk(child, stack)

        walk(tree, [])
    return found


def test_exactly_the_reading_callers_pass_read_only():
    """Every other is_safe_path call in vaf/ is judged as a write. Counted per key: a second
    reading call inside a listed function is a new decision too.

    MUTATION: add read_only=True to WriteFileTool.run's call - red. Add a second
    read_only=True call to ListFilesTool.run - red."""
    assert dict(_read_only_callers()) == {key: 1 for key in READ_ONLY_CALLERS}


def test_is_safe_path_is_reached_only_by_its_own_name():
    """The freeze above matches calls by name. An alias (`import is_safe_path as check`),
    a `functools.partial(is_safe_path, ...)` or a function handed around as a value would
    carry a read_only past it, so any use of the name other than a call, its definition or
    a plain import is refused.

    MUTATION: import it under another name in one module - red."""
    odd = []
    for path in sorted((_REPO / "vaf").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(_REPO).as_posix()
        called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for node in ast.walk(tree):
            if isinstance(node, ast.alias) and node.name == "is_safe_path" \
                    and node.asname not in (None, "is_safe_path"):
                odd.append((rel, node.lineno, f"imported as {node.asname}"))
            named = ((isinstance(node, ast.Name) and node.id == "is_safe_path")
                     or (isinstance(node, ast.Attribute) and node.attr == "is_safe_path"))
            if named and id(node) not in called:
                odd.append((rel, node.lineno, "used as a value"))
    assert odd == []


# -- 10. the dedicated writers still write -------------------------------------------------

ADMIN = "00000000-0000-0000-0000-000000000000"


def test_the_custom_tool_registry_still_writes_and_loads(roots):
    """Against the redirected data directory, not a patched one.

    SANITY MUTATION: route save_tool_file through is_safe_path - red."""
    from vaf.core import custom_tools_registry as registry
    code = ("from vaf.tools.base import BaseTool\n"
            "class ZzStateProbe(BaseTool):\n"
            "    name = 'zz_state_probe'\n"
            "    description = 'probe'\n"
            "    def run(self, **kwargs):\n"
            "        return 'ok'\n")
    registry.save_tool_file("zz_state_probe.py", code)
    registry.register_tool(tool_name="zz_state_probe", filename="zz_state_probe.py",
                           created_by="agent", shared_with=[])
    assert (roots.data / "custom_tools" / "zz_state_probe.py").exists()
    assert registry.load_custom_tool_class("zz_state_probe") is not None


def test_create_skill_and_update_skill_still_write(roots, monkeypatch):
    import vaf.core.config as cfg
    from vaf.skills import templates as st
    from vaf.tools.create_skill import CreateSkillTool
    from vaf.tools.update_skill import UpdateSkillTool
    monkeypatch.setattr(cfg, "get_local_admin_scope_id", lambda: ADMIN)
    st.reload_skills()
    try:
        out = CreateSkillTool().run(skill_id="zz_state_skill", name="State skill",
                                    description="does a thing", body="step 1",
                                    user_scope_id=ADMIN, username="alice")
        assert "Created skill 'zz_state_skill'" in out, out
        out = UpdateSkillTool().run(skill_id="zz_state_skill", name="State skill",
                                    description="does a thing", body="step 2",
                                    user_scope_id=ADMIN, username="alice")
        assert "step 2" in (roots.vaf / "skills" / "zz_state_skill" / "SKILL.md").read_text(), out
    finally:
        st.reload_skills()


def test_create_agent_workflow_still_writes(roots):
    from vaf.tools.agent_workflow_builder import AgentWorkflowBuilderTool
    agent = SimpleNamespace(tools={}, visible_tools=lambda: {}, current_session_id=None,
                            _current_user_role="admin", _current_user_scope_id=None,
                            _current_username="alice")
    out = AgentWorkflowBuilderTool().run(
        action="create", _agent=agent, workflow_id="zz_state_flow", name="State flow",
        steps=[{"tool": "web_search", "input": "x"}, {"tool": "web_search", "input": "y"}])
    assert "saved successfully" in out, out
    assert (roots.vaf / "workflows" / "zz_state_flow.py").exists()
