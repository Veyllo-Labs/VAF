# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A saved workflow file is written by one writer, as a Python literal with a one-line header.

A user workflow file (~/.vaf/workflows/<id>.py) is Python that the template loader executes.
Its three writers (the agent's `create`, the web UI's create and update) each built it by hand:
the name went unescaped into a comment line, and the dict was written as JSON, whose
true/false/null are not Python, so a step with `optional: true` made the file unloadable while
`create` reported "saved successfully".

MUTATIONS, each turns the named tests red:
- `write_workflow_file` back on json.dumps -> test_a_step_with_optional_true_loads,
  test_the_literal_round_trips;
- the header check in `write_workflow_file` dropped -> test_a_header_line_is_one_line;
- the name check in `create` dropped -> test_create_refuses_a_name_with_a_line_break;
- a hand-built writer back in web_server.py -> test_one_writer_for_workflow_files.
"""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.workflows.templates import _load_workflow_from_file, workflow_text_problem, write_workflow_file

_REPO = Path(__file__).resolve().parents[1]
WORKFLOW = {"name": "Brief", "description": "Morgens, mit Umlauten: äöü", "triggers": [],
            "steps": [{"input": "x", "tool": "web_search", "output": "a", "optional": True,
                       "condition": None, "max_assertion_retries": 2}]}


def test_the_literal_round_trips(tmp_path):
    path = tmp_path / "brief.py"
    write_workflow_file(str(path), WORKFLOW, ["created_by: agent", "Workflow: Brief"])
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# created_by: agent\n# Workflow: Brief\nWORKFLOW = ")
    assert ast.literal_eval(text.split("WORKFLOW = ", 1)[1]) == WORKFLOW
    assert not (tmp_path / "brief.py.tmp").exists()


def test_a_step_with_optional_true_loads(tmp_path):
    path = tmp_path / "brief.py"
    write_workflow_file(str(path), WORKFLOW, ["Workflow: Brief"])
    workflow_id, loaded = _load_workflow_from_file(path)
    assert workflow_id == "brief" and loaded == WORKFLOW


@pytest.mark.parametrize("text", ["Brief\nsecond line", "Brief\rx", "Brief\x00", "Tab\there"])
def test_a_header_line_is_one_line(tmp_path, text):
    assert workflow_text_problem("name", text)
    with pytest.raises(ValueError):
        write_workflow_file(str(tmp_path / "x.py"), WORKFLOW, [f"Workflow: {text}"])
    assert not (tmp_path / "x.py").exists()
    assert workflow_text_problem("name", "Morgen-Briefing (DE) äöü") == ""


@pytest.fixture
def builder(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from vaf.tools.agent_workflow_builder import AgentWorkflowBuilderTool
    agent = SimpleNamespace(tools={}, visible_tools=lambda: {}, current_session_id=None,
                            _current_user_role="admin", _current_user_scope_id=None,
                            _current_username="alice")
    return lambda **kw: AgentWorkflowBuilderTool().run(action="create", _agent=agent, **kw)


def test_create_refuses_a_name_with_a_line_break(builder, tmp_path):
    out = builder(workflow_id="brief", name="Brief\nsecond line",
                  steps=[{"tool": "web_search", "input": "x"}, {"tool": "web_search", "input": "y"}])
    assert out.startswith("Error:") and "single line" in out, out
    assert not (tmp_path / ".vaf" / "workflows" / "brief.py").exists()


def test_create_saves_an_optional_step_that_loads(builder, tmp_path):
    out = builder(workflow_id="brief", name="Brief",
                  steps=[{"tool": "web_search", "input": "x", "optional": True},
                         {"tool": "web_search", "input": "y"}])
    assert "saved successfully" in out, out
    _, loaded = _load_workflow_from_file(tmp_path / ".vaf" / "workflows" / "brief.py")
    assert loaded["steps"][0]["optional"] is True


def test_one_writer_for_workflow_files():
    """Every file in vaf/ that writes a workflow file goes through write_workflow_file."""
    import re
    built = re.compile(r"""f["']WORKFLOW = \{""")
    writers = sorted(path.relative_to(_REPO).as_posix() for path in (_REPO / "vaf").rglob("*.py")
                     if built.search(path.read_text(encoding="utf-8")))
    assert writers == ["vaf/workflows/templates.py"], f"hand-built workflow file writers: {writers}"
    server = (_REPO / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    assert server.count("write_workflow_file(") == 2
