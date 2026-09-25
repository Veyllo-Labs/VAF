# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A workflow runs in a process of its own; the chat turn that starts it ends at once.

A workflow used to run INSIDE the chat turn for as long as it took, and one chat worker serves
every chat by default. The router lane already ran whole workflows as their own process; the
two tools did not. vaf/workflows/background.py is the one launcher now, and these tests pin:

- WHEN: the switch, not inside a child, a chat to come back to, and only when the child has
  every tool the plan names - otherwise inline, as before;
- WHAT the child gets: a saved template by id and its variables, or a temporary plan exactly
  as the chat normalised it (validation flags included) through the IPC payload, never argv;
- the child runs that plan with the same checks and the same cleanup and reports once;
- the router lane lost its hand-built copy of the spawn.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import vaf.workflows.background as bg
from vaf.core.subagent_spawn import SpawnRefused, spawn_subagent

ROOT = Path(__file__).resolve().parents[1]


class _FakeIpc:
    def __init__(self, win=True):
        self.win = win
        self.created, self.payloads, self.running, self.cancelled = [], {}, [], []
        self.completed, self.failed = [], []

    def create_task(self, agent_type, task_description, session_id=None):
        self.created.append((agent_type, task_description, session_id))
        return f"tid{len(self.created)}"

    def claim_task_slot(self, task_id, agent_type, session_id):
        return self.win

    def store_task_payload(self, task_id, payload):
        self.payloads[task_id] = payload

    def get_task_payload(self, task_id):
        return self.payloads.get(task_id)

    def mark_task_running(self, task_id):
        self.running.append(task_id)

    def cancel_task(self, task_id):
        self.cancelled.append(task_id)
        return True

    def complete_task(self, task_id, result):
        self.completed.append((task_id, result))

    def fail_task(self, task_id, error):
        self.failed.append((task_id, error))

    def update_heartbeat(self, task_id, **kw):
        pass


@pytest.fixture
def rig(monkeypatch):
    ipc = _FakeIpc()
    opened = {"ok": True, "calls": []}

    def _open(cmd, title=None, extra_env=None):
        opened["calls"].append({"cmd": cmd, "title": title, "env": dict(extra_env or {})})
        return opened["ok"]

    monkeypatch.setattr("vaf.core.subagent_ipc.get_ipc", lambda: ipc)
    monkeypatch.setattr("vaf.core.subagent_ipc.get_current_session_id", lambda: "sess-ctx")
    monkeypatch.setattr("vaf.core.config.subagent_provider_override", lambda: "")
    monkeypatch.setattr("vaf.core.platform.Platform.open_new_terminal", staticmethod(_open))
    monkeypatch.setattr("vaf.core.platform.Platform.is_windows", staticmethod(lambda: False))
    for key in bg._CHILD_MARKERS:
        monkeypatch.delenv(key, raising=False)
    return ipc, opened


# ---- when ------------------------------------------------------------------------

def test_the_switch_and_a_child_decide(monkeypatch):
    """MUTATION: drop the child-marker check (a child would nest terminals) - red."""
    for key in bg._CHILD_MARKERS:
        monkeypatch.delenv(key, raising=False)
    assert bg.enabled(lambda k, d: True) is True
    assert bg.enabled(lambda k, d: False) is False
    monkeypatch.setenv("VAF_IN_WORKFLOW_TERMINAL", "1")
    assert bg.enabled(lambda k, d: True) is False, "a workflow child starts nothing of its own"


def test_an_application_authorizer_keeps_the_run_inline(monkeypatch):
    """An embedder's authorizer is a callable, and a callable cannot cross into the child: a
    run it has to see stays where the engine is handed it. MUTATION: drop the authorizer check
    in enabled() - red."""
    for key in bg._CHILD_MARKERS:
        monkeypatch.delenv(key, raising=False)
    assert bg.enabled(lambda k, d: True, authorizer=lambda req: None) is False
    assert bg.enabled(lambda k, d: True, authorizer=None) is True


def test_run_temp_with_an_authorizer_runs_in_the_chat(rig, monkeypatch):
    """The lane hands its agent's authorizer to the decision. MUTATION: call enabled()
    without it in agent_workflow_builder - the run leaves for the child, unseen - red."""
    from vaf.tools.agent_workflow_builder import AgentWorkflowBuilderTool
    monkeypatch.setattr(bg, "enabled", lambda config_get=None, authorizer=None: authorizer is None)
    agent = SimpleNamespace(current_session_id="green123456", tools={},
                            _current_user_scope_id="s", _current_username="alice",
                            _tool_authorizer=lambda req: None)
    tool = AgentWorkflowBuilderTool()
    tool._agent = agent
    monkeypatch.setattr(tool, "_collect_tools", lambda: {"web_search": object(), "write_file": object()})
    import vaf.workflows.engine as engine_mod
    seen = []
    monkeypatch.setattr(engine_mod.WorkflowEngine, "execute",
                        lambda self, steps, **k: seen.append(self._authorize) or SimpleNamespace(
                            success=True, paused=False, error=None, final_output="ok",
                            outputs={}, steps=steps))
    out = tool.run(action="run_temp", name="Suche", _agent=agent, steps=[
        {"tool": "web_search", "input": "x"}, {"tool": "write_file", "input": "y"}])
    assert not out.startswith("[SUBAGENT_ASYNC:") and rig[1]["calls"] == []
    assert seen == [agent._tool_authorizer]


def test_execute_workflow_with_an_authorizer_runs_in_the_chat(rig, monkeypatch):
    import vaf.workflows.engine as engine_mod
    import vaf.workflows.templates as templates_mod
    from vaf.tools.workflow_executor import ExecuteWorkflowTool
    template = {"name": "Deep Research", "variables": {"topic": "t"}, "defaults": {},
                "steps": [{"tool": "web_search", "input": "{topic}"}, {"tool": "write_file", "input": "x"}]}
    monkeypatch.setattr(templates_mod, "get_template", lambda wid: template if wid == "deep_research" else None)
    ran = []
    monkeypatch.setattr(engine_mod.WorkflowEngine, "execute", lambda *a, **k: ran.append(1) or SimpleNamespace(
        success=True, paused=False, error=None, final_output="ok", outputs={}, steps=[]))
    monkeypatch.setattr(bg, "enabled", lambda config_get=None, authorizer=None: authorizer is None)
    agent = SimpleNamespace(current_session_id="green123456", tools={},
                            prompt_manager=SimpleNamespace(user_language="de"),
                            _tool_authorizer=lambda req: None)
    out = ExecuteWorkflowTool().run(workflow_id="deep_research", variables={"topic": "Solar"}, _agent=agent)
    assert not out.startswith("[SUBAGENT_ASYNC:") and ran == [1], out


def test_only_the_childs_tools_go_to_the_background():
    """The child has the workflow primitives, not the agent's registry: a plan naming a mail,
    calendar, custom or MCP tool runs inline. MUTATION: return [] always - red."""
    assert bg.missing_tools(["web_search", "coding_agent", "document_agent"]) == []
    assert bg.missing_tools(["web_search", "send_mail", "my_custom", "send_mail"]) == ["send_mail", "my_custom"]


# ---- the launcher ----------------------------------------------------------------

def test_a_saved_workflow_starts_as_its_own_process(rig):
    ipc, opened = rig
    out = bg.start_saved("deep_research", {"topic": "Solar"}, name="Deep Research",
                         session_id="green123456", language="de")
    assert out.startswith("[SUBAGENT_ASYNC:tid1:workflow:deep_research] ")
    assert "do not start it again" in out
    cmd = opened["calls"][0]["cmd"]
    assert " workflow run deep_research " in cmd and "--variables" in cmd and "Solar" in cmd
    assert cmd.rstrip().endswith("--task-id tid1")
    assert " --task " not in cmd, "the description stays off the argv"
    env = opened["calls"][0]["env"]
    assert (env["VAF_SESSION_ID"], env["VAF_USER_LANGUAGE"]) == ("green123456", "de")
    assert ipc.running == ["tid1"]


def test_a_twin_holds_the_slot_and_nothing_starts(rig):
    """Register-then-verify: two launches in the same instant, one wins. The loser starts
    nothing, cancels its task, and is NOT run inline either. MUTATION: drop the claim - red."""
    ipc, opened = rig
    ipc.win = False
    out = bg.start_saved("deep_research", {}, name="Deep Research", session_id="green123456")
    assert "ALREADY RUNNING" in out
    assert opened["calls"] == [] and ipc.cancelled == ["tid1"]


def test_a_process_that_cannot_start_falls_back_inline(rig):
    ipc, opened = rig
    opened["ok"] = False
    assert bg.start_saved("deep_research", {}, name="Deep Research", session_id="green123456") is None
    assert ipc.cancelled == ["tid1"], "no task waits for a run that never started"


def test_a_temporary_plan_travels_as_the_payload(rig):
    """MUTATION: put the plan (or the model's label) on the argv - red. A label that starts
    with "-" would be read as an option, and a plan does not fit a command line."""
    ipc, opened = rig
    steps = [{"input": "search {topic}", "tool": "web_search", "output": "hits"},
             {"input": "write it", "tool": "document_agent", "output": "doc", "validate": True}]
    out = bg.start_temp("-rf report", steps, {"topic": "x"}, session_id="green123456",
                        keep_files=["/tmp/keep.docx"], user_intent="Bericht über x")
    assert out.startswith("[SUBAGENT_ASYNC:tid1:workflow:temp:-rf report] ")
    cmd = opened["calls"][0]["cmd"]
    assert " workflow run temp --plan-from-task --task-id tid1" in cmd
    assert "-rf" not in cmd and "search" not in cmd
    plan = bg.plan_from_payload(ipc.payloads["tid1"])
    assert plan["name"] == "-rf report" and plan["steps"] == steps
    assert plan["keep_files"] == ["/tmp/keep.docx"] and plan["user_intent"] == "Bericht über x"
    assert bg.plan_from_payload("not json") == {} and bg.plan_from_payload('{"steps": []}') == {}


def test_spawn_takes_a_command_and_refuses_a_lost_claim(rig):
    ipc, opened = rig
    out = spawn_subagent("workflow:x", "t", command=("workflow", "run", "x"), include_task_arg=False)
    assert " -m vaf.main workflow run x --task-id tid1" in opened["calls"][0]["cmd"]
    assert out.marker.startswith("[SUBAGENT_ASYNC:tid1:workflow:x]")
    ipc.win = False
    with pytest.raises(SpawnRefused):
        spawn_subagent("workflow:x", "t", command=("workflow", "run", "x"), exclusive=True)
    assert ipc.cancelled == ["tid2"]


def test_a_spawn_that_raises_is_a_failed_spawn(rig, monkeypatch):
    ipc, _ = rig

    def _boom(*a, **k):
        raise OSError("no terminal")
    monkeypatch.setattr("vaf.core.platform.Platform.open_new_terminal", staticmethod(_boom))
    assert spawn_subagent("coding_agent", "t") is None
    assert ipc.cancelled == ["tid1"], "MUTATION: let the exception escape - the task stays pending"


# ---- the two tools -----------------------------------------------------------------

def test_execute_workflow_goes_to_the_background(rig, monkeypatch):
    """MUTATION: drop the background branch from execute_workflow and the engine runs here."""
    import vaf.workflows.engine as engine_mod
    import vaf.workflows.templates as templates_mod
    from vaf.tools.workflow_executor import ExecuteWorkflowTool

    ran = []
    template = {"name": "Deep Research", "variables": {"topic": "t"}, "defaults": {},
                "steps": [{"tool": "web_search", "input": "{topic}"}, {"tool": "document_agent", "input": "x"}]}
    monkeypatch.setattr(templates_mod, "get_template", lambda wid: template if wid == "deep_research" else None)
    monkeypatch.setattr(engine_mod.WorkflowEngine, "execute", lambda *a, **k: ran.append(1))
    monkeypatch.setattr(bg, "enabled", lambda config_get=None, authorizer=None: authorizer is None)
    agent = SimpleNamespace(current_session_id="green123456", tools={},
                            prompt_manager=SimpleNamespace(user_language="de"))
    out = ExecuteWorkflowTool().run(workflow_id="deep_research", variables={"topic": "Solar"}, _agent=agent)
    assert out.startswith("[SUBAGENT_ASYNC:"), out
    assert ran == []


def test_run_temp_goes_to_the_background_with_its_checks(rig, monkeypatch):
    """The plan the child gets is the plan the chat built: repaired, and with the validation
    the chat would have switched on for its content steps. MUTATION: send the raw steps - the
    child would run a document step unchecked."""
    from vaf.tools.agent_workflow_builder import AgentWorkflowBuilderTool
    monkeypatch.setattr(bg, "enabled", lambda config_get=None, authorizer=None: authorizer is None)
    agent = SimpleNamespace(current_session_id="green123456", tools={"web_search": object()},
                            _current_user_scope_id="s", _current_username="alice",
                            _resolve_user_intent=lambda: "Bericht",
                            prompt_manager=SimpleNamespace(user_language=None))
    tool = AgentWorkflowBuilderTool()
    tool._agent = agent
    monkeypatch.setattr(tool, "_collect_tools", lambda: {"web_search": object(), "document_agent": object()})
    out = tool.run(action="run_temp", name="Bericht", _agent=agent, steps=[
        {"action": "web_search", "description": "Suche"},
        {"tool": "document_agent", "input": "Schreib den Bericht aus {step_1_output}"}])
    assert out.startswith("[SUBAGENT_ASYNC:"), out
    ipc = rig[0]
    plan = bg.plan_from_payload(ipc.payloads["tid1"])
    assert [s["tool"] for s in plan["steps"]] == ["web_search", "document_agent"]
    assert plan["steps"][1].get("validate") is True and not plan["steps"][0].get("validate")
    assert plan["user_intent"] == "Bericht"


def test_a_plan_the_child_cannot_run_stays_in_the_chat(rig, monkeypatch):
    from vaf.tools.agent_workflow_builder import AgentWorkflowBuilderTool
    monkeypatch.setattr(bg, "enabled", lambda config_get=None, authorizer=None: authorizer is None)
    agent = SimpleNamespace(current_session_id="green123456", tools={},
                            _current_user_scope_id="s", _current_username="alice")
    tool = AgentWorkflowBuilderTool()
    tool._agent = agent
    monkeypatch.setattr(tool, "_collect_tools", lambda: {"web_search": object(), "send_mail": object()})
    import vaf.workflows.engine as engine_mod
    monkeypatch.setattr(engine_mod.WorkflowEngine, "execute",
                        lambda self, steps, **k: SimpleNamespace(success=True, paused=False, error=None,
                                                                 final_output="ok", outputs={}, steps=steps))
    out = tool.run(action="run_temp", name="Mail", _agent=agent, steps=[
        {"tool": "web_search", "input": "x"}, {"tool": "send_mail", "input": "y"}])
    assert not out.startswith("[SUBAGENT_ASYNC:") and rig[1]["calls"] == []


# ---- the child ------------------------------------------------------------------------

def test_the_child_runs_the_plan_with_its_checks_and_reports_once(rig, monkeypatch, tmp_path):
    """MUTATION: skip the validator wiring or the cleanup in the plan branch - red."""
    from typer.testing import CliRunner

    import vaf.cli.cmd.workflow as wf_cmd
    import vaf.workflows.engine as engine_mod
    ipc, _ = rig
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "scratch.py").write_text("x")
    (proj / "Bericht.docx").write_text("x")
    seen = {}

    class _Engine:
        def __init__(self, tools, callback=None, **kw):
            self._validate_step = None

        def execute(self, steps, variables=None, **kw):
            seen["steps"] = [(s.tool, bool(getattr(s, "validate", False))) for s in steps]
            seen["validator"] = self._validate_step is not None
            seen["intent"] = getattr(self, "_workflow_user_intent", "")
            seen["vars"] = variables
            return SimpleNamespace(success=True, paused=False, error=None, final_output="Fertig: Bericht.docx",
                                   outputs={"workflow_project_path": str(proj)}, steps=steps)

    monkeypatch.setattr(engine_mod, "WorkflowEngine", _Engine)
    monkeypatch.setattr("vaf.workflows.tool_overlay.workflow_primitives", lambda: {"web_search": object()})
    monkeypatch.setattr(wf_cmd, "finish_terminal", lambda **kw: None)
    monkeypatch.delenv("VAF_SESSION_ID", raising=False)
    ipc.payloads["tidX"] = json.dumps({
        "name": "Bericht", "variables": {"topic": "x"}, "user_intent": "Bericht über x", "keep_files": [],
        "steps": [{"input": "search", "tool": "web_search", "output": "a"},
                  {"input": "write", "tool": "document_agent", "output": "b", "validate": True}]})
    res = CliRunner().invoke(wf_cmd.app, ["run", "temp", "--plan-from-task", "--task-id", "tidX"])
    assert res.exit_code == 0, res.output
    assert seen["steps"] == [("web_search", False), ("document_agent", True)]
    assert seen["validator"] is True and seen["intent"] == "Bericht über x" and seen["vars"] == {"topic": "x"}
    assert not (proj / "scratch.py").exists() and (proj / "Bericht.docx").exists()
    assert len(ipc.completed) == 1 and ipc.completed[0][0] == "tidX"
    assert "Temporary workflow 'Bericht' completed." in ipc.completed[0][1]
    assert "THE WORK IS DONE" in ipc.completed[0][1]


# ---- one implementation -----------------------------------------------------------------

def test_the_router_lane_lost_its_copy_of_the_spawn():
    """MUTATION: build `workflow run` by hand in agent.py again - red."""
    agent = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    region = agent.split("def _try_workflow", 1)[1].split("\n    def ", 1)[0]
    assert "_wf_bg.spawn_saved(" in region
    assert '_wf_bg.enabled(self.config.get, authorizer=getattr(self, "_tool_authorizer", None))' in region
    assert "open_new_terminal" not in region and "workflow run" not in region


def test_the_step_validator_is_one_function():
    from vaf.workflows.step_validation import validate_step_output

    calls = []

    def ask(messages, max_tokens):
        calls.append(messages[0]["content"][:20])
        return "</false>\nRETRY: add the numbers"
    assert validate_step_output("list the numbers", "no data", "document_agent", ask=ask) == (False, "add the numbers")
    assert validate_step_output("g", "r", "x", ask=lambda m, t: "</true>") == (True, None)
    assert validate_step_output("g", "r", "x", ask=lambda m, t: "maybe") == (True, None), "indecision accepts"

    def boom(m, t):
        raise RuntimeError("backend down")
    assert validate_step_output("g", "r", "x", ask=boom) == (True, None), "a broken backend never blocks"
    assert validate_step_output("", "r", "x", ask=boom) == (True, None)
    agent = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    body = agent.split("    def _validate_step_output(", 1)[1].split("\n    def ", 1)[0]
    assert "validate_step_output(" in body and "You are a strict validator" not in body


def test_the_temp_cleanup_keeps_every_deliverable(tmp_path):
    from vaf.workflows.engine import remove_temp_intermediates
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "keep.sh").write_text("x")
    (tmp_path / "Report.pdf").write_text("x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.tmp").write_text("x")
    assert remove_temp_intermediates(str(tmp_path), keep_files=[str(tmp_path / "keep.sh")]) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Report.pdf", "keep.sh"]
    assert remove_temp_intermediates("", keep_files=[]) == 0
