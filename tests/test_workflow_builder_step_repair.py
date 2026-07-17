# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Per-step weak-model repair for create_agent_workflow.

Live incident: the model did everything right at the top level
(action=run_temp, name, sensible plan) but authored each STEP as
{"action": "web_search", "description": "Wetter suchen (Berlin)",
"name": "step_1_wetter"} - tool named in 'action', instruction in
'description', no 'input'. The nested schema requirement rejected the whole
call pre-dispatch ("'input' is a required property"); the model could not act
on that and regressed into planning spin until the loop guards ended the turn
(21 wasted steps, nothing produced). The generic input-repair layer only
remaps TOP-LEVEL aliases, so nested step objects get their own repair here.
"""
import json

from vaf.tools.agent_workflow_builder import AgentWorkflowBuilderTool, _repair_raw_step

INCIDENT_STEPS = [
    {"action": "web_search", "description": "Wetter suchen (Berlin)", "name": "step_1_wetter"},
    {"action": "web_search", "description": "News suchen", "name": "step_2_news"},
]


def test_incident_steps_are_repaired():
    s = _repair_raw_step(INCIDENT_STEPS[0])
    assert s["tool"] == "web_search"          # tool <- step-level 'action'
    assert s["input"] == "Wetter suchen (Berlin)"  # input <- description


def test_canonical_steps_pass_through_unchanged():
    s = _repair_raw_step({"input": "Search for {topic}", "tool": "web_search", "output": "news"})
    assert s["input"] == "Search for {topic}" and s["tool"] == "web_search"


def test_args_only_step_gets_a_synthesized_input():
    s = _repair_raw_step({"tool": "write_file",
                          "args": {"path": "out.md", "content": "{summary}"}})
    assert s["input"] == "Run write_file"
    assert s["args"] == {"path": "out.md", "content": "{summary}"}


def test_truly_empty_step_stays_empty():
    assert not str(_repair_raw_step({"output": "x"}).get("input") or "").strip()


def test_incident_call_passes_schema_validation_now():
    """The exact rejected call must clear the central schema/repair layer."""
    from vaf.core.tool_input_repair import repair_tool_input

    tool = AgentWorkflowBuilderTool()
    args = {
        "action": "run_temp",
        "description": "TEMP Workflow: Websuche Wetter + News und HTML-Erstellung",
        "name": "websuche_wetter_news_html",
        "steps": INCIDENT_STEPS,
    }
    _repaired, _applied, errors = repair_tool_input(
        tool.parameters, json.loads(json.dumps(args)), getattr(tool, "input_aliases", None))
    assert not errors, errors


def test_run_temp_auto_enables_validation_instead_of_bouncing(monkeypatch):
    """Live incident: the [VALIDATION CHECK] bounce ("call run_temp again with
    validate flags") cost a weak model the run twice in one chat - it retried
    without the flags, got bounced again, and did every step manually. A
    content/agent step without validate flags now gets validation enabled
    automatically and the workflow RUNS; skip_validation stays the opt-out."""
    import types

    import vaf.workflows.engine as engine_mod

    captured = {}

    class _FakeEngine:
        def __init__(self, tools=None, callback=None, **kw):
            pass

        def execute(self, steps, variables=None, check_stop=None, **kw):
            captured["steps"] = steps
            return types.SimpleNamespace(success=True, final_output="ok", error=None,
                                         paused=False, outputs={})

    monkeypatch.setattr(engine_mod, "WorkflowEngine", _FakeEngine)
    tool = AgentWorkflowBuilderTool()
    agent = types.SimpleNamespace(tools={"web_search": object(), "coding_agent": object()},
                                  current_session_id=None)
    result = tool.run(
        action="run_temp", name="wetter_html",
        steps=[
            {"input": "suche wetter berlin", "tool": "web_search", "output": "w"},
            {"input": "Erstelle ein HTML aus {w}", "tool": "coding_agent", "output": "html"},
        ],
        _agent=agent,
    )
    assert "VALIDATION CHECK" not in str(result)   # no bounce
    assert "steps" in captured                      # it RAN
    coder_steps = [s for s in captured["steps"] if s.tool == "coding_agent"]
    assert coder_steps and all(s.validate for s in coder_steps)  # auto-enabled

    # Explicit opt-out still works and does not auto-enable.
    captured.clear()
    tool.run(
        action="run_temp", name="wetter_html2",
        steps=[
            {"input": "suche wetter berlin", "tool": "web_search", "output": "w"},
            {"input": "Erstelle ein HTML aus {w}", "tool": "coding_agent", "output": "html"},
        ],
        skip_validation=True, _agent=agent,
    )
    coder_steps = [s for s in captured["steps"] if s.tool == "coding_agent"]
    assert coder_steps and not any(s.validate for s in coder_steps)


def test_run_temp_accepts_the_incident_steps(monkeypatch, tmp_path):
    """End to end through _run_temp with a fake engine: the incident steps
    normalise into two runnable web_search steps (no single-step rejection,
    no missing-input error)."""
    import types

    import vaf.workflows.engine as engine_mod

    captured = {}

    class _FakeEngine:
        def __init__(self, tools=None, callback=None, **kw):
            pass

        def execute(self, steps, variables=None, check_stop=None, **kw):
            captured["steps"] = steps
            return types.SimpleNamespace(success=True, final_output="ok", error=None,
                                         paused=False, outputs={})

    monkeypatch.setattr(engine_mod, "WorkflowEngine", _FakeEngine)

    tool = AgentWorkflowBuilderTool()
    agent = types.SimpleNamespace(tools={"web_search": object()}, current_session_id=None)
    result = tool.run(action="run_temp", name="websuche_wetter_news_html",
                      steps=[dict(s) for s in INCIDENT_STEPS],
                      skip_validation=True, _agent=agent)
    assert "steps" in captured, result
    got = captured["steps"]
    assert len(got) == 2
    assert got[0].tool == "web_search"
    assert "Wetter suchen" in got[0].input_template
