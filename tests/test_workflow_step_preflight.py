# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A model-built workflow is checked before it runs, and a shell step stays a shell step.

Live incident: a model planned a temporary workflow with three steps
`{"tool": "bash", "args": {"command": "pip install vaf"}}`. The builder's alias table turned
`bash` into `python_sandbox` and kept the args, the run started in the background, and step 1
died there with "missing required field: 'code'". The model learned of it a turn later and
invented the results. Three more things stood behind it, each measured: the inline lane's
registry had no `bash` at all; a `bash` workflow step had no workspace, so it could never
succeed, and a model-written `base_dir` would have chosen the jail root; and nothing checked a
step's arguments before the run.

MUTATIONS, each turns the named tests red:
- re-add `"bash": "python_sandbox"` -> test_bash_stays_bash, test_aliases_take_the_source_args;
- `_collect_tools` back to `dict(agent.tools)` -> test_inline_run_temp_has_the_primitives;
- `setdefault` without the pop, or the pop after the project-path return ->
  test_bash_workspace_is_assigned;
- drop the primary merge in `build_step_args` -> test_build_step_args_matches_the_engine;
- skip the preflight call -> the refusal tests; let type errors refuse too ->
  test_placeholders_pass;
- `command_needs_network` returns False -> test_command_needs_network,
  test_network_bash_step_refused;
- remove the host_bash check -> test_host_bash_step_refused;
- yarn back to "every subcommand but a short list downloads" -> the yarn rows of
  test_command_needs_network;
- no de-duplication, or the "Retry the SAME call" sentence kept -> the missing-field test;
- `create` refusing an unknown tool -> test_create_saves_a_plan_with_a_tool_of_another_lane;
- the execute_workflow check removed -> the saved-workflow test.
"""
import ast
import importlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import vaf.tools.agent_workflow_builder as builder
import vaf.workflows.engine as engine_module
from vaf.tools.agent_workflow_builder import (
    AgentWorkflowBuilderTool,
    _STEP_TOOL_ALIASES,
    _resolve_step_tool,
)
from vaf.workflows.engine import _inject_workflow_paths, build_step_args

_REPO = Path(__file__).resolve().parents[1]


class _FakeEngine:
    """Stands in for WorkflowEngine: records what a run would have been given."""
    runs: list = []

    def __init__(self, tools=None, **kw):
        self.tools = tools
        _FakeEngine.runs.append(self)
        self.steps = None

    def execute(self, steps, **kw):
        self.steps = steps
        return SimpleNamespace(success=True, paused=False, error="", outputs={},
                               final_output="done", steps=steps)


@pytest.fixture
def run(monkeypatch, tmp_path):
    """Call the builder like the agent does, on a fake engine, in a scratch home."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(engine_module, "WorkflowEngine", _FakeEngine)
    _FakeEngine.runs = []

    def _call(steps, *, action="run_temp", visible=(), agent_tools=None, **extra):
        agent = SimpleNamespace(
            tools=dict(agent_tools or {}),
            visible_tools=lambda: {name: object() for name in visible},
            current_session_id=None, _current_user_role="admin",
            _current_user_scope_id=None, _current_username="alice")
        tool = AgentWorkflowBuilderTool()
        return tool.run(action=action, steps=steps, _agent=agent, **extra)
    return _call


# ── A1: the alias table ──────────────────────────────────────────────────────

def test_bash_stays_bash(run):
    assert _resolve_step_tool("bash") == "bash"
    assert _resolve_step_tool("python_exec") == "python_sandbox"
    out = run([{"tool": "bash", "args": {"command": "ls"}},
               {"tool": "bash", "args": {"command": "python -m pytest"}}])
    assert "completed" in out, out
    assert [s.tool for s in _FakeEngine.runs[-1].steps] == ["bash", "bash"]


def _tool_class(name):
    """The class of the built-in tool registered as `name`, or None when none is."""
    pattern = re.compile(r'^\s+name\s*=\s*["\']' + re.escape(name) + r'["\']', re.M)
    for path in sorted((_REPO / "vaf" / "tools").glob("*.py")):
        if not pattern.search(path.read_text(encoding="utf-8")):
            continue
        module = importlib.import_module(f"vaf.tools.{path.stem}")
        for value in vars(module).values():
            if isinstance(value, type) and getattr(value, "name", None) == name \
                    and getattr(value, "parameters", None):
                return value
    return None


def test_aliases_take_the_source_args():
    """An alias keeps the step's args, so the target must take every argument the source
    tool requires. A source that is no tool (`python`, `search`) has no args to keep."""
    checked = 0
    for src, dst in _STEP_TOOL_ALIASES.items():
        src_cls = _tool_class(src)
        if src_cls is None:
            continue
        dst_cls = _tool_class(dst)
        assert dst_cls is not None, f"alias target {dst!r} is not a tool"
        required = set(src_cls.parameters.get("required") or [])
        props = set((dst_cls.parameters.get("properties") or {}))
        assert required <= props, f"{src} -> {dst} drops {sorted(required - props)}"
        checked += 1
    assert _tool_class("bash") is not None
    assert checked >= 1, "no alias source is a real tool any more: the guard checks nothing"
    # bash itself would be checked if it came back: it is a real tool.
    assert "bash" not in _STEP_TOOL_ALIASES


# ── A2: the registry ─────────────────────────────────────────────────────────

def test_inline_run_temp_has_the_primitives(run):
    run([{"tool": "list_files", "args": {"path": "."}},
         {"tool": "web_search", "input": "x"}],
        agent_tools={"web_search": SimpleNamespace(parameters={})})
    tools = _FakeEngine.runs[-1].tools
    assert {"bash", "move_file", "python_sandbox"} <= set(tools)


def test_the_builder_has_no_hand_written_tool_list():
    source = (_REPO / "vaf" / "tools" / "agent_workflow_builder.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_collect_tools")
    body = ast.unparse(fn)
    assert "workflow_primitives()" in body
    assert "importlib" not in body and "WebSearchTool" not in body


# ── A3: the bash workspace ───────────────────────────────────────────────────

def test_bash_workspace_is_assigned():
    args = {"command": "ls", "base_dir": "/somewhere"}
    _inject_workflow_paths("bash", args, "/proj")
    assert args == {"command": "ls", "base_dir": "/proj"}
    args = {"command": "ls", "base_dir": "/somewhere"}
    _inject_workflow_paths("bash", args, None)
    assert args == {"command": "ls"}, "a model-written base_dir survived without a project"


# ── A4: one place builds a step's args ───────────────────────────────────────

def test_build_step_args_matches_the_engine():
    same = lambda text: text  # noqa: E731
    assert build_step_args("web_search", "Wetter Berlin", None, same) == \
        {"query": "Wetter Berlin", "deep": False}
    assert build_step_args("bash", "", {"command": "ls"}, same) == {"command": "ls"}
    assert build_step_args("web_search", "Wetter", {"max_results": 3}, same) == \
        {"max_results": 3, "query": "Wetter", "deep": False}
    assert build_step_args("web_search", "label", {"query": "real"}, same) == {"query": "real"}
    assert build_step_args("python_sandbox", '{"code": "print(1)"}', None, same) == \
        {"code": "print(1)"}
    assert build_step_args("bash", "{not json}", None, same) == {"command": "{not json}"}
    seen = []
    build_step_args("coding_agent", "", {"task": "do {x}", "n": 2}, lambda t: t.replace("{x}", "it"),
                    trace=lambda k, before, after: seen.append((k, before, after)))
    assert seen == [("task", "do {x}", "do it")]


def test_the_engine_builds_args_only_through_the_helper():
    source = (_REPO / "vaf" / "workflows" / "engine.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    execute = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "execute")
    body = ast.unparse(execute)
    assert body.count("build_step_args(") == 1
    assert "_PRIMARY_ARG_BY_TOOL" not in body and "json.loads(resolved_input)" not in body
    assert "_infer_args" not in source


# ── A4: the preflight ────────────────────────────────────────────────────────

def test_preflight_refuses_missing_code(run):
    for step in ({"tool": "python_sandbox", "args": {"command": "pip install vaf"}},
                 {"tool": "python_sandbox", "input": "pip install vaf"}):
        out = run([step, {"tool": "web_search", "input": "x"}])
        assert out.startswith("Error:") and "nothing ran" in out, out
        assert "step 1 (python_sandbox)" in out and "missing required field: 'code'" in out
        assert not _FakeEngine.runs, "a refused plan reached the engine"


def test_a_missing_field_is_named_once_with_one_closing_instruction(run):
    """jsonschema reports a missing field once per required entry; the refusal names each
    field once, and only the plan's own instruction closes it."""
    out = run([{"tool": "move_file", "args": {"path": "a"}},
               {"tool": "web_search", "input": "y"}])
    assert out.count("'src'") == 1 and out.count("'dst'") == 1, out
    assert "Retry the SAME call" not in out
    assert out.count("call create_agent_workflow again") == 1


def test_preflight_collects_all_steps(run):
    out = run([{"tool": "python_sandbox", "args": {"command": "a"}},
               {"tool": "python_sandbox", "args": {"command": "b"}}])
    assert "step 1 (python_sandbox)" in out and "step 2 (python_sandbox)" in out
    assert out.count("missing required field: 'code'") == 2
    assert out.rstrip().endswith("call create_agent_workflow again.")


def test_placeholders_pass(run):
    # `max_results` is an integer field: "{n}" fails the type check until it is resolved.
    out = run([{"tool": "web_search", "input": "Wetter", "output": "w"},
               {"tool": "python_sandbox", "args": {"code": "{w}"}},
               {"tool": "web_search", "args": {"query": "{w}", "max_results": "{n}"}}])
    assert "completed" in out, out
    assert len(_FakeEngine.runs) == 1


def test_unknown_tool_hint(run):
    out = run([{"tool": "web_serch", "input": "x"}, {"tool": "web_search", "input": "y"}])
    assert "Unknown tool 'web_serch'" in out and "web_search" in out.split("web_serch'", 1)[1]
    assert not _FakeEngine.runs


def test_create_refuses_without_writing(run, tmp_path):
    out = run([{"tool": "python_sandbox", "args": {"command": "x"}},
               {"tool": "web_search", "input": "y"}],
              action="create", workflow_id="daily_brief", name="Daily brief")
    assert out.startswith("Error:") and "missing required field: 'code'" in out, out
    assert not (tmp_path / ".vaf" / "workflows" / "daily_brief.py").exists()


# ── A5: a download in the offline shell ──────────────────────────────────────

@pytest.mark.parametrize("command, downloads", [
    ("pip install vaf", True), ("python3 -m pip install x", True), ("uv pip install y", True),
    ("npm install", True), ("yarn", True), ("git clone https://x/y", True),
    ("cd repo && git -C sub pull", True), ("sudo apt-get install -y jq", True),
    ("curl -O https://x/y", True),
    ("ls -la", False), ("python -m pytest", False), ("git status", False),
    ("npm run build", False), ("yarn test", False), ("echo pip install", False),
    ("pip list", False),
    # yarn: only the bare command and the fetching subcommands download; a package script runs.
    ("yarn install", True), ("yarn add left-pad", True), ("yarn dlx create-vite", True),
    ("yarn upgrade", True), ("yarn start", False), ("yarn dev", False), ("yarn tsc", False),
    ("yarn format", False), ("yarn --version", False),
])
def test_command_needs_network(command, downloads):
    from vaf.tools.bash import command_needs_network
    assert command_needs_network(command) is downloads


def test_network_bash_step_refused(run):
    plan = [{"tool": "bash", "args": {"command": "pip install vaf"}},
            {"tool": "bash", "args": {"command": "git clone https://x/y"}}]
    out = run(plan, visible=("host_bash",))
    assert 'host_bash(command="pip install vaf")' in out
    assert 'host_bash(command="git clone https://x/y")' in out
    out = run(plan)
    assert "host_bash(" not in out and "args.packages" in out
    assert not _FakeEngine.runs
    out = run([{"tool": "bash", "args": {"command": "ls -la"}},
               {"tool": "bash", "input": "python -m pytest"}])
    assert "completed" in out, out


# ── A6: no host_bash step ────────────────────────────────────────────────────

@pytest.mark.parametrize("action", ["run_temp", "create"])
def test_host_bash_step_refused(run, tmp_path, action):
    from vaf.tools.host_bash import HostBashTool
    extra = {"workflow_id": "host_plan", "name": "Host plan"} if action == "create" else {}
    out = run([{"tool": "host_bash", "args": {"command": "ls"}},
               {"tool": "web_search", "input": "y"}],
              action=action, visible=("host_bash",), agent_tools={"host_bash": HostBashTool()},
              **extra)
    assert out.startswith("Error:") and "step 1 (host_bash)" in out, out
    assert "directly in the chat" in out
    assert not _FakeEngine.runs
    assert not (tmp_path / ".vaf" / "workflows" / "host_plan.py").exists()


def test_create_saves_a_plan_with_a_tool_of_another_lane(run, tmp_path):
    """A saved workflow may run where the tool exists (the automation lane's channel tools):
    `create` saves it and says so; run_temp still refuses (test_unknown_tool_hint)."""
    out = run([{"tool": "web_search", "input": "x", "output": "w"},
               {"tool": "send_signal_message", "input": "{w}"}],
              action="create", workflow_id="other_lane", name="Other lane")
    assert "saved successfully" in out, out
    assert "step 2 (send_signal_message)" in out and "does not have" in out
    assert (tmp_path / ".vaf" / "workflows" / "other_lane.py").exists()


def _saved_workflow(home, workflow_id, *, marker):
    folder = home / ".vaf" / "workflows"
    folder.mkdir(parents=True, exist_ok=True)
    body = ('WORKFLOW = {"name": "Saved", "description": "d", "triggers": [], "steps": ['
            '{"input": "ls", "tool": "host_bash", "output": "a"}, '
            '{"input": "x", "tool": "web_search", "output": "b"}]}\n')
    (folder / f"{workflow_id}.py").write_text(("# created_by: agent\n" if marker else "") + body,
                                             encoding="utf-8")


def test_a_saved_model_workflow_with_host_bash_is_refused_at_start(run, tmp_path, monkeypatch):
    """A model-written workflow saved before `create` refused host_bash: execute_workflow
    refuses it with the same message. A person's own workflow file is not touched."""
    from vaf.tools.agent_workflow_builder import agent_workflow_refusal, is_agent_created
    from vaf.tools.workflow_executor import ExecuteWorkflowTool
    from vaf.workflows.templates import get_template

    _saved_workflow(tmp_path, "old_plan", marker=True)
    _saved_workflow(tmp_path, "own_plan", marker=False)
    assert is_agent_created("old_plan") and not is_agent_created("own_plan")
    refusal = agent_workflow_refusal("old_plan", get_template("old_plan"))
    assert refusal and refusal.startswith("Error:") and "step 1 (host_bash)" in refusal
    assert "directly in the chat" in refusal
    assert agent_workflow_refusal("own_plan", get_template("own_plan")) is None

    started = []
    monkeypatch.setattr(engine_module, "create_workflow",
                        lambda template: started.append(template) or [])
    out = ExecuteWorkflowTool().run(workflow_id="old_plan")
    assert out == refusal and not started, out


def test_create_saves_a_plan_that_can_run(run, tmp_path):
    out = run([{"tool": "web_search", "input": "x", "output": "w"},
               {"tool": "write_file", "args": {"path": "a.md", "content": "{w}"}}],
              action="create", workflow_id="good_plan", name="Good plan")
    assert "saved successfully" in out, out
    assert (tmp_path / ".vaf" / "workflows" / "good_plan.py").exists()


def test_the_builder_uses_the_one_alias_helper():
    source = (_REPO / "vaf" / "tools" / "agent_workflow_builder.py").read_text(encoding="utf-8")
    assert source.count("_resolve_step_tool(") == 3, "run_temp and create resolve the same way"
    assert "_tool_aliases" not in source
    assert builder._STEP_TOOL_ALIASES is _STEP_TOOL_ALIASES
