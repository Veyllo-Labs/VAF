# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A workflow's project folder is always this run's own, whoever names it.

The folder is bash's workspace in a workflow and the place the temporary-file cleanup walks.
`execute()` used to take a `workflow_project_path` from its variables as it was, and the
variables of a model-built workflow are written by the model, so a plan could point bash and
the cleanup at any folder of the host. A value handed in is now accepted only below the run's
own Documents/VAF_Projects[/<uid8>] folder (the resume lane hands back the folder of the run it
continues), and the cleanup refuses any folder outside Documents/VAF_Projects.

A step could also NAME `workflow_project_path` as its output and overwrite the checked value
after the check, and the cleanup read the folder from those outputs (live finding: another
account's app.py, build.sh and index.js deleted by a non-admin's temporary workflow). Three
layers close that, each pinned on its own: the engine keeps the folder it decided outside the
outputs (`WorkflowResult.project_path`) and both cleanup callers read that; an engine-owned
output name is refused before anything runs (the builder's preflight and `execute()` itself);
and the cleanup checks against the caller's own `VAF_Projects/<uid8>`.

MUTATIONS, each turns the named tests red:
- `execute()` takes the supplied value unchecked again -> the foreign-folder tests (engine
  and run_temp);
- `remove_temp_intermediates` without its containment check -> the cleanup test;
- a cleanup caller back on `outputs["workflow_project_path"]` -> test_the_cleanup_uses_the_
  folder_the_engine_decided (the CLI caller: tests/test_workflow_background.py);
- `execute()` without the engine-owned output check -> test_the_engine_refuses_its_own_output_name;
- the preflight without it -> test_run_temp_refuses_the_engine_output_name;
- the cleanup without the caller's uid8 -> test_the_cleanup_stays_in_the_callers_folder;
- the refused-folder note dropped -> test_the_model_is_told_where_the_files_are.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.core.platform import Platform
from vaf.workflows.engine import WorkflowEngine, WorkflowStep, remove_temp_intermediates

OWN_SCOPE = "ab12cd34-0000-4000-8000-000000000001"
OTHER_UID8 = "ff00ee11"


class _Shell:
    """Stands in for bash: records the workspace each call was given."""
    parameters = {"type": "object", "properties": {"command": {"type": "string"}},
                  "required": ["command"]}

    def __init__(self):
        self.workspaces = []

    def run(self, **kwargs):
        self.workspaces.append(kwargs.get("base_dir"))
        return "ok"


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("VAF_SESSION_ID", raising=False)
    monkeypatch.setattr(Platform, "documents_dir", staticmethod(lambda: tmp_path / "Documents"))
    settings = tmp_path / ".vaf"
    settings.mkdir()
    (settings / "keep_me.py").write_text("x")
    return tmp_path


def _run(variables, *, scope=OWN_SCOPE):
    shell = _Shell()
    engine = WorkflowEngine(tools={"bash": shell}, callback=lambda *a, **k: None,
                            user_scope_id=scope, username="alice")
    engine._workflow_name = "Probe"
    steps = [WorkflowStep(tool="bash", input_template="", args_template={"command": "ls"},
                          output_name="a"),
             WorkflowStep(tool="bash", input_template="", args_template={"command": "pwd"},
                          output_name="b")]
    result = engine.execute(steps, variables=dict(variables))
    return shell, result


def _own_root(home):
    return (home / "Documents" / "VAF_Projects" / OWN_SCOPE[:8]).resolve()


@pytest.mark.parametrize("foreign", [".vaf", f"Documents/VAF_Projects/{OTHER_UID8}/Probe",
                                     "Documents/VAF_Projects"])
def test_a_foreign_folder_is_not_the_bash_workspace(home, foreign):
    target = home / foreign
    target.mkdir(parents=True, exist_ok=True)
    shell, result = _run({"workflow_project_path": str(target)})
    assert result.success, result.error
    assert shell.workspaces and all(w for w in shell.workspaces)
    for workspace in shell.workspaces:
        assert _own_root(home) in Path(workspace).resolve().parents, workspace
    assert result.outputs["workflow_project_path"] == shell.workspaces[0]
    assert (home / ".vaf" / "keep_me.py").exists()


def test_a_resumed_runs_own_folder_is_kept(home):
    own = _own_root(home) / "green123456" / "Probe"
    own.mkdir(parents=True)
    shell, result = _run({"workflow_project_path": str(own)})
    assert shell.workspaces == [str(own), str(own)]


def test_run_temp_cannot_choose_the_folder(home):
    from vaf.tools.agent_workflow_builder import AgentWorkflowBuilderTool
    shell = _Shell()
    agent = SimpleNamespace(tools={"bash": shell}, visible_tools=lambda: {}, current_session_id=None,
                            _current_user_role="admin", _current_user_scope_id=OWN_SCOPE,
                            _current_username="alice")
    out = AgentWorkflowBuilderTool().run(
        action="run_temp", name="Probe", _agent=agent, skip_validation=True,
        variables={"workflow_project_path": str(home / ".vaf")},
        steps=[{"tool": "bash", "args": {"command": "ls"}},
               {"tool": "bash", "args": {"command": "pwd"}}])
    assert "completed" in out, out
    assert shell.workspaces and all(_own_root(home) in Path(w).resolve().parents
                                    for w in shell.workspaces), shell.workspaces
    assert (home / ".vaf" / "keep_me.py").exists(), "the cleanup walked the named folder"


def test_the_cleanup_walks_only_project_folders(home):
    assert remove_temp_intermediates(str(home / ".vaf")) == 0
    assert (home / ".vaf" / "keep_me.py").exists()
    outside_link = home / "Documents" / "VAF_Projects" / "linked"
    outside_link.parent.mkdir(parents=True)
    try:
        outside_link.symlink_to(home / ".vaf", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks on this file system")
    assert remove_temp_intermediates(str(outside_link)) == 0
    assert (home / ".vaf" / "keep_me.py").exists()
    own = home / "Documents" / "VAF_Projects" / "Probe"
    own.mkdir()
    (own / "scratch.py").write_text("x")
    assert remove_temp_intermediates(str(own)) == 1


def _probe_steps(output_name):
    return [WorkflowStep(tool="bash", input_template="", args_template={"command": "ls"},
                         output_name=output_name),
            WorkflowStep(tool="bash", input_template="", args_template={"command": "pwd"},
                         output_name="b")]


def test_the_engine_refuses_its_own_output_name(home):
    shell = _Shell()
    engine = WorkflowEngine(tools={"bash": shell}, callback=lambda *a, **k: None,
                            user_scope_id=OWN_SCOPE, username="alice")
    result = engine.execute(_probe_steps("workflow_project_path"), variables={})
    assert not result.success and "nothing ran" in result.error
    assert "step 1 (bash)" in result.error and "workflow_project_path" in result.error
    assert shell.workspaces == [], "a step ran"


def _agent(shell, scope=OWN_SCOPE):
    return SimpleNamespace(tools={"bash": shell}, visible_tools=lambda: {}, current_session_id=None,
                           _current_user_role="user", _current_user_scope_id=scope,
                           _current_username="alice")


def test_run_temp_refuses_the_engine_output_name(home, monkeypatch):
    import vaf.workflows.engine as engine_module
    from vaf.tools.agent_workflow_builder import AgentWorkflowBuilderTool

    class _Never:
        def __init__(self, *a, **k):
            raise AssertionError("the engine was built for a refused plan")
    monkeypatch.setattr(engine_module, "WorkflowEngine", _Never)
    out = AgentWorkflowBuilderTool().run(
        action="run_temp", name="Probe", _agent=_agent(_Shell()),
        steps=[{"tool": "write_file", "args": {"path": "p.txt", "content": "x"}},
               {"tool": "read_file", "args": {"path": "p.txt"}, "output": "workflow_project_path"}])
    assert out.startswith("Error: nothing ran") and "step 2 (read_file)" in out, out
    assert "belongs to the workflow engine" in out


def _seed(folder):
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("app.py", "build.sh", "index.js", "notes.md"):
        (folder / name).write_text("x")


def test_the_cleanup_uses_the_folder_the_engine_decided(home, monkeypatch):
    """The outputs entry names another folder of the SAME account, which the uid8 check alone
    would let through: only reading the engine's own decision keeps it."""
    import vaf.workflows.engine as engine_module
    from vaf.tools.agent_workflow_builder import AgentWorkflowBuilderTool
    own_run = _own_root(home) / "green123456" / "Probe"
    decoy = _own_root(home) / "green123456" / "Another project"
    _seed(own_run)
    _seed(decoy)

    class _Engine:
        def __init__(self, *a, **k):
            pass

        def execute(self, steps, **kw):
            return SimpleNamespace(success=True, paused=False, error="", final_output="done",
                                   steps=steps, outputs={"workflow_project_path": str(decoy)},
                                   project_path=str(own_run), supplied_path_refused=False)
    monkeypatch.setattr(engine_module, "WorkflowEngine", _Engine)
    out = AgentWorkflowBuilderTool().run(
        action="run_temp", name="Probe", _agent=_agent(_Shell()), skip_validation=True,
        steps=[{"tool": "bash", "args": {"command": "ls"}},
               {"tool": "bash", "args": {"command": "pwd"}}])
    assert "completed" in out, out
    assert sorted(p.name for p in decoy.iterdir()) == ["app.py", "build.sh", "index.js", "notes.md"]
    assert sorted(p.name for p in own_run.iterdir()) == ["notes.md"]


def test_the_cleanup_stays_in_the_callers_folder(home):
    foreign = home / "Documents" / "VAF_Projects" / OTHER_UID8 / "Site"
    _seed(foreign)
    assert remove_temp_intermediates(str(foreign), user_scope_id=OWN_SCOPE) == 0
    assert len(list(foreign.iterdir())) == 4
    own = _own_root(home) / "Probe"
    _seed(own)
    assert remove_temp_intermediates(str(own), user_scope_id=OWN_SCOPE) == 3


def test_the_reported_attack_leaves_the_other_account_alone(home):
    """The finding as it was reproduced: a non-admin plan that reads another account's folder
    name into the engine's output key. Every layer is in place here."""
    from vaf.tools.agent_workflow_builder import AgentWorkflowBuilderTool
    foreign = home / "Documents" / "VAF_Projects" / OTHER_UID8 / "Site"
    _seed(foreign)
    agent = SimpleNamespace(tools={}, visible_tools=lambda: {}, current_session_id=None,
                            _current_user_role="user", _current_user_scope_id=OWN_SCOPE,
                            _current_username="alice")
    out = AgentWorkflowBuilderTool().run(
        action="run_temp", name="Probe", _agent=agent, skip_validation=True,
        steps=[{"tool": "write_file", "args": {"path": "p.txt", "content": str(foreign)}},
               {"tool": "read_file", "args": {"path": "{workflow_project_path}/p.txt"},
                "output": "workflow_project_path"}])
    assert out.startswith("Error:"), out
    assert len(list(foreign.iterdir())) == 4


def test_the_model_is_told_where_the_files_are(home):
    from vaf.workflows.engine import project_folder_note
    shell, result = _run({"workflow_project_path": str(home / ".vaf")})
    assert result.supplied_path_refused and result.project_path == shell.workspaces[0]
    note = project_folder_note(result)
    assert result.project_path in note and "was not used" in note
    _, kept = _run({"workflow_project_path": str(_own_root(home) / "green123456" / "Probe")})
    assert project_folder_note(kept) == ""
