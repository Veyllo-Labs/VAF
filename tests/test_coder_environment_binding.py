# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The coder working in a sandbox environment.

A coder run is bound to the caller's project environment whose /workspace IS the run's
project folder: explicitly (`environment`, which must match) or on its own when one exists.
Its bash and run_tests then run in that container. The binding is per instance and per
run, never class state: the tool objects are shared across turns and people."""
import os
import types

import pytest

from vaf.core import environments as envmod
from vaf.tools.bash import BashTool
from vaf.tools.sandbox_test_runner import RunTestsTool


def _env(env_id="0a1b2c3d", kind="project", project_path="/p", network="registries"):
    return envmod.Environment(id=env_id, kind=kind, network=network, owner="h", container="c",
                              volume="", net="n", project_path=project_path, state="running")


@pytest.fixture
def mgr(monkeypatch, tmp_path):
    m = envmod.EnvironmentManager(state_dir=tmp_path / "state")
    monkeypatch.setattr(envmod, "get_environment_manager", lambda: m)
    return m


# -- which environment ---------------------------------------------------------------

def test_an_explicit_environment_must_be_a_project_environment_for_exactly_this_folder(mgr, monkeypatch, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    env = _env(project_path=str(proj))
    monkeypatch.setattr(mgr, "get", lambda owner, env_id, admin=False: env)
    assert mgr.bind_for_project("s", str(proj), env.id) is env
    with pytest.raises(envmod.EnvironmentRefused, match="works in"):
        mgr.bind_for_project("s", str(other), env.id)
    env.kind, env.project_path = "temporary", ""
    with pytest.raises(envmod.EnvironmentRefused, match="no project folder"):
        mgr.bind_for_project("s", str(proj), env.id)


def test_without_an_id_the_callers_environment_for_the_folder_is_found(mgr, monkeypatch, tmp_path):
    """Resolved paths: a project reached through a symlink is the same project.
    MUTATION: compare the raw strings - red."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    env = _env(project_path=str(real))
    monkeypatch.setattr(mgr, "list", lambda owner, everyone=False: [env, _env("ffff0000", kind="temporary", project_path="")])
    assert mgr.bind_for_project("s", str(link)) is env
    assert mgr.bind_for_project("s", str(tmp_path)) is None


# -- bash and run_tests in the environment ----------------------------------------------

class _Recorder:
    def __init__(self):
        self.calls = []

    def exec_in(self, env, argv, **kw):
        self.calls.append(("exec_in", argv, kw))
        return envmod.ExecResult(0, "built\n", "")

    def start_process(self, owner, env_id, command, **kw):
        self.calls.append(("start_process", owner, env_id, command, kw))
        return f"e-{env_id}-p{len(self.calls):08x}"

    def stop_process(self, owner, handle):
        self.calls.append(("stop_process", owner, handle))
        return "stopped"


def test_bash_runs_in_the_environment_and_starts_dev_servers_there(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(envmod, "get_environment_manager", lambda: rec)
    tool = BashTool("/p", environment=_env(), owner_scope="scope-alice")
    out = tool.run(command="npm run build")
    assert "environment 0a1b2c3d" in out and "built" in out and "exit code: 0" in out
    assert rec.calls[0][:2] == ("exec_in", ["sh", "-c", "npm run build"])
    tool.run(command="npm run dev", background=True)
    tool.run(command="npm run api", background=True, keep_running=True)
    kept = tool.stop_unkept()
    stopped = [c for c in rec.calls if c[0] == "stop_process"]
    assert len(stopped) == 1 and stopped[0][1] == "scope-alice"
    assert len(kept) == 1 and kept[0] not in stopped[0][2]
    # Asked again (the run's wrapper on its way out): nothing more is stopped.
    assert tool.stop_unkept() == kept
    assert len([c for c in rec.calls if c[0] == "stop_process"]) == 1


def test_a_download_without_network_says_why(monkeypatch):
    """MUTATION: drop the hint in _run_in_environment - red: the coder guessed at its build
    instead of learning the environment has no network."""
    class _NoNet(_Recorder):
        def exec_in(self, env, argv, **kw):
            return envmod.ExecResult(1, "", "Could not resolve host: registry.npmjs.org")

    monkeypatch.setattr(envmod, "get_environment_manager", lambda: _NoNet())
    out = BashTool("/p", environment=_env(network="none"), owner_scope="s").run(command="npm i")
    assert "has no network (network none)" in out and "Failed (exit code: 1)" in out
    out = BashTool("/p", environment=_env(network="open"), owner_scope="s").run(command="npm i")
    assert "has no network" not in out


def test_background_processes_end_on_every_exit_of_the_run():
    """run() has many early returns and can raise; the summary at its end was the only
    place that stopped a run's dev servers. MUTATION: drop the decorator from
    CodingAgentTool.run - red."""
    from vaf.tools import coder
    wrapper_code = coder._ends_background_with_the_run(lambda self, **kw: None).__code__
    assert coder.CodingAgentTool.run.__code__ is wrapper_code

    asked = []

    class _Bash:
        def stop_unkept(self):
            asked.append(self)
            return []

    class _Run:
        def __init__(self, bind):
            self.bind = bind
            self.local_tools = {"bash": _Bash()}         # an earlier run's tool

        @coder._ends_background_with_the_run
        def run(self, **kwargs):
            if self.bind:
                self.local_tools = {"bash": _Bash()}
            raise RuntimeError("the provider kept failing")

    for bind in (True, False):
        with pytest.raises(RuntimeError):
            _Run(bind).run(task="x")
    assert len(asked) == 1        # only the tool the raising run bound; never a leftover


def test_as_root_is_the_environments_and_only_runs_to_its_end(monkeypatch):
    """MUTATION: drop as_root from the exec_in call - red; let the jailed shell accept it -
    red: the host has no root lane."""
    rec = _Recorder()
    monkeypatch.setattr(envmod, "get_environment_manager", lambda: rec)
    tool = BashTool("/p", environment=_env(), owner_scope="s")
    out = tool.run(command="apt-get install -y tree", as_root=True)
    assert rec.calls[-1][2]["as_root"] is True and "as root" in out
    tool.run(command="ls")
    assert rec.calls[-1][2]["as_root"] is False
    assert "start a background process without it" in tool.run(command="x", as_root=True, background=True)
    assert "only in a sandbox environment" in BashTool("/p").run(command="apt-get install tree", as_root=True)


def test_bash_is_offered_in_every_context_and_as_root_only_when_bound():
    """A task run without a shell faked an npm package by hand. MUTATION: put the bash
    schema back inside the main-context block - red."""
    import inspect
    from vaf.tools import coder
    src = inspect.getsource(coder.CodingAgentTool.run)
    bash_at = src.index('"name": "bash"')
    main_block = src.rindex("if is_main_context:", 0, bash_at)
    assert src.index("tools_schema.extend(plug_and_play_tools)", main_block) < bash_at
    assert src.index("THE chokepoint", bash_at) > bash_at
    schema = src[bash_at:src.index("THE chokepoint", bash_at)]
    assert schema.index('"as_root"') < schema.index("if _env_binding is not None else\n                            {")
    # MUTATION: drop timeout from either schema - red: an install that needs more than the
    # default 120 s could not ask for it, though BashTool takes up to 300.
    bound, unbound = schema.split("if _env_binding is not None else\n                            {")
    assert '"timeout": _bash_timeout' in bound and '"timeout": _bash_timeout' in unbound
    assert "default 120, at most 300" in src[src.rindex("_bash_timeout = ", 0, bash_at):bash_at]


def test_bash_without_an_environment_keeps_the_jail(monkeypatch):
    import vaf.tools.workspace_exec as wx
    seen = []
    monkeypatch.setattr(wx, "run_in_workspace", lambda ws, cmd, timeout: (seen.append(ws) or (0, "", "", "bwrap")))
    BashTool("/p").run(command="ls")
    assert seen == ["/p"]


def test_run_tests_runs_in_the_project_itself_and_says_it_can_write(monkeypatch):
    """No copy: the environment's /workspace is the project. Per instance - the class
    stays read-only for every other run. MUTATION: set the permission on the class - red."""
    rec = _Recorder()
    monkeypatch.setattr(envmod, "get_environment_manager", lambda: rec)
    bound = RunTestsTool("/p", environment=_env())
    out = bound.run()
    assert out.startswith("TESTS PASSED")
    assert any(c[0] == "exec_in" and c[2].get("cwd") == "/workspace" and c[1][:2] == ["sh", "-c"]
               for c in rec.calls)
    assert bound.permission_level == "write"
    assert RunTestsTool("/p").permission_level == "read" and RunTestsTool.permission_level == "read"


# -- the id crosses the process boundary as an argument ---------------------------------

def test_the_spawn_passes_the_environment_as_an_argument_never_as_a_variable(monkeypatch):
    """Rule 4.5: a variable set for a spawn can outlive it in the parent; an argument
    cannot. MUTATION: hand it over in extra_env - red."""
    import vaf.core.config as cfg
    import vaf.core.subagent_spawn as spawn
    from vaf.tools.coder import CodingAgentTool
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    real_get = cfg.Config.get
    monkeypatch.setattr(cfg.Config, "get", classmethod(
        lambda cls, k, d=None: True if k == "sub_agents_in_separate_terminals" else real_get(k, d)))
    seen = {}

    def _spawn(kind, task, args=(), extra_env=None, **kw):
        seen.update(kind=kind, args=args, env=extra_env or {})
        return types.SimpleNamespace(marker="[SUBAGENT_ASYNC:t:coding_agent]")

    monkeypatch.setattr(spawn, "spawn_subagent", _spawn)
    out = CodingAgentTool().run(task="make the tests pass", project_path="/tmp/proj-x",
                                environment="0a1b2c3d")
    assert out == "[SUBAGENT_ASYNC:t:coding_agent]"
    assert seen["args"] == ("--project-path", "/tmp/proj-x", "--environment", "0a1b2c3d")
    assert not any("0a1b2c3d" in str(v) for v in seen["env"].values())


def test_the_child_cli_hands_the_environment_to_the_coder():
    """Read from the source, not invoked: `vaf subagent run` sets process-wide state on
    purpose (VAF_IN_SUBAGENT_TERMINAL, VAF_TASK_ID, the adopted cwd), because it IS the
    child; invoked inside the suite it leaked that state into every later test.
    MUTATION: drop the kwargs line - red."""
    import ast
    from pathlib import Path
    import vaf.cli.cmd.subagent as sub
    tree = ast.parse(Path(sub.__file__).read_text(encoding="utf-8"))
    run = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_subagent")
    assert "environment" in [a.arg for a in run.args.args]
    src = ast.unparse(run)
    assert "kwargs['environment'] = environment" in src
    assert "'--environment'" in src


def test_browser_agent_is_refused_for_the_environments_own_pages():
    """The personal browser never joins an environment's network. MUTATION: return None from
    _environment_browser_refusal - red: a live run spent 5.5 minutes on file:///workspace."""
    from vaf.tools import coder
    env = _env()
    for task in ("Open file:///workspace/index.html and click +",
                 "open http://localhost:5173/ and fill the form",
                 "check http://127.0.0.1:8000/"):
        msg = coder._environment_browser_refusal("browser_agent", {"task": task}, env)
        assert msg and "render_check" in msg and env.id in msg, task
    assert coder._environment_browser_refusal("browser_agent", {"task": "read https://docs.python.org/3/"}, env) is None
    assert coder._environment_browser_refusal("browser_agent", {"task": "file:///workspace/x"}, None) is None
    assert coder._environment_browser_refusal("render_check", {"task": "file:///workspace/x"}, env) is None


def test_bound_runs_offer_no_browser_agent_and_say_where_render_check_looks():
    """MUTATION: advertise browser_agent while bound, or drop the dispatch check - red."""
    import inspect
    from vaf.tools import coder
    src = inspect.getsource(coder.CodingAgentTool.run)
    head = src.index('"name": "browser_agent"')
    assert "*([] if _env_binding is not None else [{" in src[head - 400:head]
    assert "Render a page INSIDE sandbox environment {_env_binding.id}" in src
    assert "_refusal = (_environment_browser_refusal(fn_name, fn_args, _env_binding)" in src
