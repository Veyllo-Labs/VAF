# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Workflow completion must show the real work, not just the last step.

Live incident: research_and_code ran perfectly (11-minute coder step, HTML
written) - and its FINAL template step asked librarian_agent (a FILESYSTEM
agent) to "write a short completion message ... where the file was saved".
The librarian parsed that prose as a file-search task and returned "No files
found matching '*was*'", which became the workflow's final output. The model
read that next to "completed successfully", concluded the workflow produced
nothing, and redid every step manually (42-step turn, three duplicate
deliverables).

Pinned here:
- No template ends with the librarian-as-completion-writer anti-pattern.
- summarize_run_steps renders one bounded line per step.
- Both completion messages (execute_workflow, run_temp) carry the step
  summary, so one weird step can never hide the actual deliverables.
"""
import types

from vaf.workflows.engine import WorkflowStep, StepStatus, summarize_run_steps


def test_no_template_uses_the_librarian_completion_antipattern():
    from vaf.workflows.templates import get_workflow_templates

    for wf_id, tpl in get_workflow_templates().items():
        steps = tpl.get("steps") or []
        for s in steps:
            if (s.get("tool") == "librarian_agent"
                    and "completion message" in str(s.get("input", "")).lower()):
                raise AssertionError(
                    f"{wf_id}: librarian_agent used as completion-message writer - "
                    "a filesystem agent misreads that prompt as a file search "
                    "(live incident: final output 'No files found matching *was*')"
                )


def test_summarize_run_steps_shows_every_step_bounded():
    s1 = WorkflowStep(tool="web_search", input_template="q", output_name="a")
    s1.status, s1.result = StepStatus.SUCCESS, "### Web Search Results ..."
    s2 = WorkflowStep(tool="write_file", input_template="f", output_name="b")
    s2.status, s2.result = StepStatus.SUCCESS, "File written successfully to /home/user/x.html\n" + "x" * 500
    out = summarize_run_steps([s1, s2])
    lines = out.splitlines()
    assert len(lines) == 2
    assert "web_search" in lines[0] and "success" in lines[0]
    assert "File written successfully" in lines[1]
    assert len(lines[1]) < 200  # bounded head, newlines flattened


def test_summarize_run_steps_never_raises_on_garbage():
    assert summarize_run_steps(None) == ""
    assert summarize_run_steps([object()]) != None  # noqa: E711 - just must not raise


def test_execute_workflow_result_contains_step_results(monkeypatch):
    """The tool result carries the per-step lines even when the final output
    itself is garbage - the write step's real path stays visible."""
    import vaf.core.subagent_ipc as ipc_mod
    import vaf.workflows.templates as templates_mod
    import vaf.workflows.engine as engine_mod
    import vaf.workflows.tool_overlay as overlay_mod
    from vaf.tools.workflow_executor import ExecuteWorkflowTool

    class _FakeIpc:
        def has_live_task(self, *a, **k): return False
        def create_task(self, **k): return "t1"
        def mark_task_running(self, t): pass
        def cancel_task(self, t): return True
        def consume_result(self, t): return None
        def update_heartbeat(self, t): pass
        def claim_task_slot(self, *a, **k): return True

    class _Step:
        def __init__(self, tool, result):
            self.tool, self.result = tool, result
            self.status = StepStatus.SUCCESS
            self.description = tool
            self.input_template = ""
            self.output_name = tool

    made_steps = [_Step("write_file", "File written successfully to /home/user/wetter.html"),
                  _Step("librarian_agent", "No files found matching '*was*'")]

    class _FakeEngine:
        def __init__(self, tools=None, callback=None, **kw): pass
        def execute(self, steps, variables=None, check_stop=None, **kw):
            return types.SimpleNamespace(success=True, paused=False, error=None,
                                         final_output="No files found matching '*was*'")

    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: _FakeIpc())
    monkeypatch.setattr(templates_mod, "get_template",
                        lambda wid: {"name": "Research & Code", "variables": {}, "defaults": {}})
    monkeypatch.setattr(templates_mod, "list_templates", lambda: [])
    monkeypatch.setattr(engine_mod, "create_workflow", lambda template: made_steps)
    monkeypatch.setattr(engine_mod, "WorkflowEngine", _FakeEngine)
    monkeypatch.setattr(overlay_mod, "workflow_primitives", lambda: {"write_file": object()})

    agent = types.SimpleNamespace(current_session_id="sess-x", tools={})
    result = ExecuteWorkflowTool().run(workflow_id="research_and_code", _agent=agent)
    assert "Step results:" in result
    assert "File written successfully to /home/user/wetter.html" in result
