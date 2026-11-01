# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A user workflow file is DATA: its WORKFLOW literal is read, the file is never executed.

A workflow name needs no control character to turn the saved file into code: the header
comment carried the name, and a name like `coding: utf-7` is a PEP 263 source-encoding
declaration, so the loader read the whole file as UTF-7, where `+ACc-` decodes to a quote and
closes the string literal around an expression (reproduced through both writers). Two halves
close it: the header lines carry no model or user text, and user workflow files load through
`ast.literal_eval` of their `WORKFLOW = {...}` value, with the bytes decoded as UTF-8, instead
of exec. The built-in templates shipped in the package stay Python.

MUTATIONS, each turns the named tests red:
- the user directory back on the exec loader (`_discover_workflows` without literal_only)
  -> test_a_user_file_is_not_executed, test_the_encoding_name_runs_nothing;
- the header line back to carrying the name -> test_the_encoding_name_runs_nothing;
- the literal loader accepting more than one statement -> test_a_user_file_is_not_executed.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

import vaf.workflows.templates as templates
from vaf.tools.agent_workflow_builder import AgentWorkflowBuilderTool


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".vaf" / "workflows").mkdir(parents=True)
    return tmp_path


def _user_file(home, name, text):
    (home / ".vaf" / "workflows" / name).write_bytes(text.encode("utf-8"))


def test_a_pure_literal_user_file_loads(home):
    _user_file(home, "ok.py",
               'WORKFLOW = {"name": "N", "description": "d", "triggers": [], '
               '"steps": [{"tool": "web_search", "input": "x", "optional": True}]}\n')
    templates.reload_workflows()
    assert "ok" in templates.get_workflow_templates()
    assert templates.get_template("ok")["steps"][0]["optional"] is True


def test_a_user_file_is_not_executed(home, tmp_path):
    """A .py dropped in by any means (a write_file, a crafted name) is data: a module-level
    statement that is not the WORKFLOW literal means the file is skipped, never run."""
    marker = tmp_path / "ran.txt"
    _user_file(home, "evil.py",
               "import pathlib\n"
               f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
               'WORKFLOW = {"name": "N", "description": "d", "triggers": [], "steps": []}\n')
    templates.reload_workflows()
    assert "evil" not in templates.get_workflow_templates()
    assert not marker.exists(), "the user file was executed"


def test_the_encoding_name_runs_nothing(home, tmp_path):
    """The reported attack end to end: a name that is an encoding declaration plus a
    description that would close the literal under that encoding. Nothing runs, and the saved
    data round-trips byte for byte."""
    marker = tmp_path / "pwned.txt"
    payload = f"x+ACc-,+ACc-p+ACc-:__import__(+ACc-pathlib+ACc-).Path(+ACc-{marker}+ACc-)"
    agent = SimpleNamespace(tools={}, visible_tools=lambda: {}, current_session_id=None,
                            _current_user_role="admin", _current_user_scope_id=None,
                            _current_username="alice")
    out = AgentWorkflowBuilderTool().run(
        action="create", _agent=agent, workflow_id="brief", name="Brief coding: utf-7",
        description=payload,
        steps=[{"tool": "web_search", "input": "x"}, {"tool": "web_search", "input": "y"}])
    assert "saved successfully" in out, out
    assert not marker.exists(), "the description ran as code"
    loaded = templates.get_template("brief")
    assert loaded["name"] == "Brief coding: utf-7"
    assert loaded["description"] == payload


def test_a_non_literal_file_is_skipped_with_a_log_line(home, caplog):
    _user_file(home, "bad.py", "WORKFLOW = 1 + 1\n")
    with caplog.at_level("WARNING"):
        templates.reload_workflows()
    assert "bad" not in templates.get_workflow_templates()
    assert any("bad.py" in r.getMessage() for r in caplog.records)


def test_the_builtin_templates_still_load():
    """The package templates use imports and a helper variable; they stay Python."""
    import importlib
    importlib.reload(templates) if False else None
    names = set(templates.get_workflow_templates())
    for built_in in ("deep_research", "research_and_document", "youtube_summary"):
        assert built_in in names, built_in
