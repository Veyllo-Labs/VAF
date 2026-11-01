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

MUTATIONS, each turns the named tests red:
- `execute()` takes the supplied value unchecked again -> the foreign-folder tests (engine
  and run_temp);
- `remove_temp_intermediates` without its containment check -> the cleanup test.
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
