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
  test_command_needs_network; option values no longer skipped -> its option rows;
- no de-duplication, or the "Retry the SAME call" sentence kept -> the missing-field test;
- `create` refusing an unknown tool -> test_create_saves_a_plan_with_a_tool_of_another_lane;
- the execute_workflow check removed -> the saved-workflow test; the `@workflow_id` check or
  the `vaf workflow run` check removed -> the two lane tests after it.

The network refusal (one line, one instruction, the tools it names handed to the turn), run
through a real ToolCaller whose registry and identity decide what the caller may use:
- the per-step instruction restored -> test_network_refusal_fits_the_cut;
- python_sandbox and host_bash swapped in what is advised -> test_network_refusal_variants;
- the "Fix the other steps" line dropped -> test_mixed_plan_closes_with_both;
- the preflight asking `agent.visible_tools()` again instead of `advise_tools` ->
  test_no_host_bash_advice_for_a_regular_account, test_no_host_bash_advice_on_a_channel;
- `_HOST_BASH_INSTEAD` changed -> test_saved_refusal_outside_a_call_is_unchanged;
- the sandbox code or the host_bash list showing only the first command ->
  test_every_command_is_spelled_out; no fitting -> the sixty-command test;
- the sandbox example without its timeout, or with the sandbox's own 30 s ->
  test_the_sandbox_example_has_room;
- `check=True` back in the sandbox code (stop at the first failure) ->
  test_the_sandbox_code_runs_every_command;
- no step-numbers-only fallback, or no cap on the step numbers ->
  test_a_huge_command_and_a_thousand_steps_fit;
- host_bash rendered with the old quote-only escaping -> test_host_bash_examples_are_json.
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

    def _call(steps, *, action="run_temp", agent_tools=None, **extra):
        agent = SimpleNamespace(
            tools=dict(agent_tools or {}),
            current_session_id=None, _current_user_role="admin",
            _current_user_scope_id=None, _current_username="alice")
        tool = AgentWorkflowBuilderTool()
        return tool.run(action=action, steps=steps, _agent=agent, **extra)
    return _call


REGULAR = "ab12cd34-0000-0000-0000-000000000000"   # synthetic scope of a regular account


@pytest.fixture
def run_as(run, monkeypatch):
    """Call the builder through a real ToolCaller, as the chat lane does; returns the
    ToolCallOutcome. `registry` names the tools besides the builder (real instances), and
    the caller's identity and source decide which of them the caller may use."""
    from vaf.core.tool_dispatch import ToolCaller
    from vaf.tools.host_bash import HostBashTool
    from vaf.workflows.tool_overlay import workflow_primitives

    def _call(steps, *, registry=("python_sandbox", "host_bash"), role="admin", scope=None,
              source="", session_id=None, action="run_temp", **extra):
        tools = {"create_agent_workflow": AgentWorkflowBuilderTool()}
        if "host_bash" in registry:
            tools["host_bash"] = HostBashTool()
        if "python_sandbox" in registry:
            tools["python_sandbox"] = workflow_primitives()["python_sandbox"]
        agent = SimpleNamespace(tools={}, current_session_id=session_id,
                                _current_user_role=role, _current_user_scope_id=scope,
                                _current_username="alice")
        caller = ToolCaller(tools, user_scope_id=scope, user_role=role, source=source,
                            session_id=session_id)
        return caller.call("create_agent_workflow",
                           {"action": action, "steps": steps, "_agent": agent, **extra})
    return _call


def _net_plan(pips=10, clones=10):
    return ([{"tool": "bash", "args": {"command": f"pip install pkg{i}"}} for i in range(pips)]
            + [{"tool": "bash", "args": {"command": f"git clone https://example.org/r{i}.git"}}
               for i in range(clones)])


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
    # an option's value is not the subcommand
    ("yarn --cwd app install", True), ("yarn --cwd app dev", False),
    ("yarn --cwd=app add x", True), ("yarn --network-timeout 60000 install", True),
    ("npm --prefix web install", True), ("npm --prefix web run build", False),
    ("pnpm --dir web add x", True), ("pip --cache-dir /tmp/c install x", True),
    ("git -C repo status", False), ("git --git-dir .git fetch", True),
])
def test_command_needs_network(command, downloads):
    from vaf.tools.bash import command_needs_network
    assert command_needs_network(command) is downloads


def test_network_bash_step_refused(run, run_as):
    plan = [{"tool": "bash", "args": {"command": "pip install vaf"}},
            {"tool": "bash", "args": {"command": "git clone https://x/y"}}]
    out = run_as(plan).result
    assert out.startswith("Error: nothing ran") and "steps 1, 2 (bash)" in out, out
    assert 'host_bash(command="pip install vaf")' in out
    assert 'host_bash(command="git clone https://x/y")' in out
    assert "run('pip install vaf')\\nrun('git clone https://x/y')\\n" in out
    out = run_as(plan, role="user", scope=REGULAR).result
    assert "host_bash(" not in out and "python_sandbox(code=" in out, out
    assert not _FakeEngine.runs
    out = run([{"tool": "bash", "args": {"command": "ls -la"}},
               {"tool": "bash", "input": "python -m pytest"}])
    assert "completed" in out, out


def test_network_refusal_fits_the_cut(run_as):
    """The measured plan: 10 installs and 10 clones. Its refusal was 5369 characters, and the
    chat funnel cuts at 2000 from the middle, so the model saw neither all the steps nor its
    instruction whole."""
    out = run_as(_net_plan()).result
    assert len(out) < 2000, len(out)
    assert "[Output Truncated" not in out
    lines = [line for line in out.splitlines() if line.startswith("- ")]
    assert lines == ["- steps " + ", ".join(str(i) for i in range(1, 21)) + " (bash): these "
                     "commands need the network, and the workflow shell has none."], lines
    # Every command in the sandbox code, once; host_bash points at that order, because both
    # lists together would not fit the cut.
    for step in _net_plan():
        assert out.count(f"run({step['args']['command']!r})") == 1, step


def _commands(plan):
    return [step["args"]["command"] for step in plan]


@pytest.mark.parametrize("case, caller", [
    ("both", {}),
    ("sandbox_only", {"role": "user", "scope": REGULAR}),
    ("host_bash_only", {"registry": ("host_bash",)}),
])
@pytest.mark.parametrize("size", [(2, 2), (10, 10)])
def test_every_command_is_spelled_out(run_as, case, caller, size):
    """A weak model copies the example it is shown: shown only the first command, it runs
    that one and reports all of them done. Each command is in the sandbox code as its own
    run(...), and each is its own host_bash call where host_bash is the only way; a small
    plan gets both lists in full."""
    plan = _net_plan(*size)
    out = run_as(plan, **caller).result
    assert len(out) < 2000, len(out)
    for command in _commands(plan):
        if case != "host_bash_only":
            assert out.count(f"run({command!r})") == 1, (case, command)
        if case == "host_bash_only" or (case == "both" and size == (2, 2)):
            assert out.count(f'`host_bash(command="{command}")`') == 1, (case, command)


def test_a_plan_too_long_to_spell_out_names_the_rest(run_as):
    """Sixty commands do not fit the cut: as many as fit are spelled out, and the rest are
    named by step number instead of being cut out of the middle by the funnel."""
    plan = [{"tool": "bash", "args": {"command": f"git clone https://example.org/team/{i}.git"}}
            for i in range(60)]
    out = run_as(plan).result
    assert len(out) <= 1900, len(out)
    assert "run('git clone https://example.org/team/0.git')" in out
    assert "Then the commands of steps" in out and "the same way, one per command" in out, \
        out[-300:]


def test_the_sandbox_example_has_room(run_as):
    """The sandbox stops a run after 30 s unless asked for more (600 at most): an install or a
    clone takes longer, so the example asks for room per command."""
    import re
    from vaf.tools.python_sandbox import PythonSandboxTool
    for size, plan in ((2, _net_plan(1, 1)), (20, _net_plan())):
        out = run_as(plan).result
        timeouts = [int(t) for t in re.findall(r"timeout=(\d+)\)`", out)]
        assert timeouts == [min(600, 60 + 45 * size)], (size, timeouts)
        assert 30 < timeouts[0] <= PythonSandboxTool.MAX_TIMEOUT_SECONDS


def test_the_sandbox_code_runs_every_command(tmp_path):
    """The example's code itself, run: a failed command does not stop the others, and each
    command gets its own OK or FAILED line with its exit code."""
    import subprocess
    import sys
    from vaf.tools.agent_workflow_builder import _sandbox_code
    code = _sandbox_code(["exit 3", "echo second", "exit 4"])
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=tmp_path, timeout=60)
    lines = done.stdout.splitlines()
    assert done.returncode == 0, done.stderr
    assert [line.split()[:2] for line in lines] == [["FAILED", "3"], ["OK", "0"], ["FAILED", "4"]]


def test_a_huge_command_and_a_thousand_steps_fit(run_as):
    """No plan can push the refusal past the cut: a command longer than the budget is named
    by its step number only, and a thousand step numbers are capped."""
    huge = "pip install " + "a" * 3000
    out = run_as([{"tool": "bash", "args": {"command": huge}},
                  {"tool": "web_search", "input": "x"}]).result
    assert len(out) <= 1900, len(out)
    assert huge not in out and "a" * 200 not in out
    assert "python_sandbox has network" in out and "host_bash" in out and "timeout=" in out
    many = [{"tool": "bash", "args": {"command": f"git clone https://example.org/r{i}.git"}}
            for i in range(1000)]
    out = run_as(many).result
    assert len(out) <= 1900, len(out)
    assert "and 970 more (bash)" in out, out[:400]
    assert "python_sandbox has network" in out and "host_bash" in out


def test_host_bash_examples_are_json():
    """A quote, a trailing backslash or a newline in a command stays one valid argument."""
    import json
    import re
    from vaf.tools.agent_workflow_builder import _network_instruction
    commands = ['git clone "https://x/y" dir\\', "pip install a\npip install b"]
    text = _network_instruction(commands, ["1", "2"], ("host_bash",))
    rendered = re.findall(r"`host_bash\(command=(.*?)\)`", text)
    assert [json.loads(arg) for arg in rendered] == commands, text


_SANDBOX_TAIL = ("Only if the user needs the result on their own computer, say that you cannot "
                 "put it there from here.")
_HOST_ONLY = ("Run them outside the workflow with host_bash, one call per command and in this "
              "order; the user confirms each: " + ", ".join(
                  f'`host_bash(command="{c}")`' for c in _commands(_net_plan())) + ".")
_NEITHER = ("Neither python_sandbox nor host_bash is available to you here, so tell the user "
            "to run these commands themselves.")


@pytest.mark.parametrize("case, caller, advised", [
    ("both", {}, ("python_sandbox", "host_bash")),
    ("sandbox_only", {"role": "user", "scope": REGULAR}, ("python_sandbox",)),
    ("host_bash_only", {"registry": ("host_bash",)}, ("host_bash",)),
    ("neither", {"registry": ("host_bash",), "source": "telegram",
                 "session_id": "telegram_123456"}, ()),
])
def test_network_refusal_variants(run_as, case, caller, advised):
    """The instruction names exactly what this caller may use, and exactly that is advised;
    python_sandbox first, because it asks nothing."""
    outcome = run_as(_net_plan(), **caller)
    out = outcome.result
    assert outcome.advised_tools == advised, (case, outcome.advised_tools)
    last = out.splitlines()[-1]
    sandbox_call = ('`python_sandbox(code="import subprocess\\ndef run(c):\\n r = '
                    'subprocess.run(c, shell=True, capture_output=True, text=True)\\n')
    if case == "both":
        assert last.startswith("Run them outside the workflow. python_sandbox has network")
        assert sandbox_call in last
        assert last.endswith("one call per command in the order of the run(...) calls above, "
                             'starting with `host_bash(command="pip install pkg0")`; the user '
                             "confirms each."), last
        assert last.index("python_sandbox(") < last.index("host_bash(")
    elif case == "sandbox_only":
        assert sandbox_call in last and last.endswith(_SANDBOX_TAIL), last
        assert "host_bash" not in out
    elif case == "host_bash_only":
        assert last == _HOST_ONLY, last
        assert "python_sandbox" not in out
    else:
        assert last == _NEITHER, last
    assert "call create_agent_workflow again" not in out, "sends the model back into the refusal"


def test_mixed_plan_closes_with_both(run_as):
    """A network step and a missing field: the network instruction, then the plan's own."""
    out = run_as([{"tool": "bash", "args": {"command": "pip install vaf"}},
                  {"tool": "move_file", "args": {"path": "a"}}]).result
    lines = out.splitlines()
    assert lines[1] == ("- step 1 (bash): this command needs the network, and the workflow "
                        "shell has none."), lines
    assert lines[2].startswith("- step 2 (move_file):")
    assert lines[-2].startswith("Run it outside the workflow. python_sandbox"), lines[-2]
    assert lines[-1] == "Fix the other steps and call create_agent_workflow again."


# ── A6: no host_bash step ────────────────────────────────────────────────────

@pytest.mark.parametrize("action", ["run_temp", "create"])
def test_host_bash_step_refused(run, tmp_path, action):
    from vaf.tools.host_bash import HostBashTool
    extra = {"workflow_id": "host_plan", "name": "Host plan"} if action == "create" else {}
    out = run([{"tool": "host_bash", "args": {"command": "ls"}},
               {"tool": "web_search", "input": "y"}],
              action=action, agent_tools={"host_bash": HostBashTool()}, **extra)
    assert out.startswith("Error:") and "step 1 (host_bash)" in out, out
    assert "directly in the chat" in out
    assert not _FakeEngine.runs
    assert not (tmp_path / ".vaf" / "workflows" / "host_plan.py").exists()


def _host_bash_wording(out):
    return ("directly in the chat" in out, "host_bash is not available to you here" in out)


def test_no_host_bash_advice_for_a_regular_account(run_as, tmp_path):
    """host_bash is opt-in per account: a regular account whose list does not name it gets
    the "not available" wording from the preflight (A5 and A6) and from the saved-workflow
    refusal, and nothing about host_bash is advised."""
    plan = [{"tool": "host_bash", "args": {"command": "ls"}},
            {"tool": "bash", "args": {"command": "pip install vaf"}}]
    outcome = run_as(plan, role="user", scope=REGULAR)
    assert _host_bash_wording(outcome.result) == (False, True), outcome.result
    assert "host_bash(" not in outcome.result
    assert "host_bash" not in outcome.advised_tools
    admin = run_as(plan)
    assert _host_bash_wording(admin.result) == (True, False), admin.result
    assert "host_bash" in admin.advised_tools
    _saved_refusal_through_caller(tmp_path, role="user", scope=REGULAR, expect_host_bash=False)
    _saved_refusal_through_caller(tmp_path, role="admin", scope=None, expect_host_bash=True)


def test_no_host_bash_advice_on_a_channel(run_as, tmp_path):
    """No messaging channel can show host_bash's confirmation, so it is never advised there."""
    channel = {"source": "telegram", "session_id": "telegram_123456"}
    plan = [{"tool": "host_bash", "args": {"command": "ls"}},
            {"tool": "bash", "args": {"command": "pip install vaf"}}]
    outcome = run_as(plan, **channel)
    assert _host_bash_wording(outcome.result) == (False, True), outcome.result
    assert "host_bash(" not in outcome.result
    assert outcome.advised_tools == ("python_sandbox",)
    _saved_refusal_through_caller(tmp_path, expect_host_bash=False, **channel)


def _saved_refusal_through_caller(home, *, expect_host_bash, role="admin", scope=None,
                                  source="", session_id=None):
    """`agent_workflow_refusal` asked inside a chat call (execute_workflow's lane)."""
    from vaf.core.tool_dispatch import ToolCaller
    from vaf.tools.agent_workflow_builder import agent_workflow_refusal
    from vaf.tools.base import BaseTool
    from vaf.tools.host_bash import HostBashTool
    from vaf.workflows.templates import get_template

    _saved_workflow(home, "old_plan", marker=True)

    class _Start(BaseTool):
        name = "start_saved"
        description = "starts the saved workflow"
        parameters = {"type": "object", "properties": {}}

        def run(self, **kw):
            return agent_workflow_refusal("old_plan", get_template("old_plan"))

    outcome = ToolCaller({"start_saved": _Start(), "host_bash": HostBashTool()},
                         user_scope_id=scope, user_role=role, source=source,
                         session_id=session_id).call("start_saved", {})
    assert outcome.result.startswith("Error: nothing ran"), outcome.result
    assert _host_bash_wording(outcome.result) == (expect_host_bash, not expect_host_bash), \
        outcome.result
    assert outcome.advised_tools == (("host_bash",) if expect_host_bash else ())


def test_saved_refusal_outside_a_call_is_unchanged(tmp_path, monkeypatch):
    """The `@workflow` lane and `vaf workflow run` ask outside any tool call: nothing can be
    handed over there, and the text is exactly what it was."""
    from vaf.core.tool_dispatch import advise_tools
    from vaf.tools.agent_workflow_builder import agent_workflow_refusal
    from vaf.workflows.templates import get_template
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _saved_workflow(tmp_path, "old_plan", marker=True)
    assert advise_tools("host_bash") is None
    assert agent_workflow_refusal("old_plan", get_template("old_plan")) == (
        "Error: nothing ran, the saved workflow 'old_plan' has steps that cannot run:\n"
        "- step 1 (host_bash): a workflow runs its steps without asking the user, and "
        "host_bash needs the user's OK for every command.\n"
        "Call host_bash directly in the chat instead, one call per command (the user "
        "confirms each).")


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

    assert refusal.count("Call host_bash directly") == 1, "one closing instruction"
    started = []
    monkeypatch.setattr(engine_module, "create_workflow",
                        lambda template: started.append(template) or [])
    out = ExecuteWorkflowTool().run(workflow_id="old_plan")
    assert out == refusal and not started, out


def test_the_at_workflow_lane_refuses_it_too(run, tmp_path, monkeypatch):
    """`@old_plan` in the chat starts the saved workflow without execute_workflow."""
    from vaf.core.agent import Agent
    from vaf.core.platform import Platform
    _saved_workflow(tmp_path, "old_plan", marker=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    agent = Agent(register_signals=False, run_kind="chat",
                  config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
    monkeypatch.delenv("VAF_IN_AUTOMATION", raising=False)
    built = []
    monkeypatch.setattr(engine_module, "create_workflow",
                        lambda template: built.append(template) or [])
    streamed = []
    out = agent._try_workflow("@old_plan", streamed.append, route_input="@old_plan")
    assert out and out.startswith("Error: nothing ran") and "step 1 (host_bash)" in out, out
    assert not built, "the workflow's steps were built"
    assert "".join(streamed) == out


def test_the_workflow_command_refuses_it_too(run, tmp_path, monkeypatch):
    """`vaf workflow run old_plan`: the person's command and the background child of both
    chat lanes."""
    from typer.testing import CliRunner

    import vaf.cli.cmd.workflow as wf_cmd
    _saved_workflow(tmp_path, "old_plan", marker=True)

    class _Ended(BaseException):
        """What finish_terminal does for real: sys.exit, past every `except Exception`."""

    def _finish(**kw):
        raise _Ended(kw)
    monkeypatch.setattr(wf_cmd, "finish_terminal", _finish)
    monkeypatch.delenv("VAF_SESSION_ID", raising=False)
    with pytest.raises(_Ended) as ended:
        CliRunner().invoke(wf_cmd.app, ["run", "old_plan"])
    assert ended.value.args[0].get("success") is False
    assert not _FakeEngine.runs, "an engine was built for a refused workflow"


def test_delete_refuses_a_path_id(run, tmp_path, monkeypatch):
    """The agent's delete joins the id into a file name; a path escapes nothing."""
    from types import SimpleNamespace as _NS
    from vaf.tools.agent_workflow_builder import AgentWorkflowBuilderTool
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    victim = tmp_path / "secret.py"
    victim.write_text("keep me")
    agent = _NS(tools={}, visible_tools=lambda: {}, current_session_id=None,
                _current_user_role="admin", _current_user_scope_id=None, _current_username="a")
    out = AgentWorkflowBuilderTool().run(action="delete", _agent=agent,
                                         workflow_id="../secret")
    assert out.startswith("Error:") and "valid workflow id" in out, out
    assert victim.exists(), "delete escaped the workflows directory"


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
