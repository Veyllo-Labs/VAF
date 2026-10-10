# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""python_sandbox in the caller's own scratch environment: temporary per-run pip
installs, bridge values that never touch a command line, and a run that belongs to
one person.

The sandbox used to be one container for every account (python:3.11-slim, root, the
runs separated by directory names under /tmp) with an ephemeral fallback; each person
has a scratch environment of their own now (vaf/core/environments.py), and the bounded,
marker-killed exec lives in vaf/core/containers.py (tests/test_containers.py)."""
import types

from vaf.tools.python_sandbox import PythonSandboxTool


# -- temporary pip installs ----------------------------------------------------

def test_pip_installs_target_the_per_run_dir_and_skip_cache():
    cmd = PythonSandboxTool._pip_install_cmd(["numpy", "pandas==2.2.0"], "/tmp/vaf_x_1")
    assert "--target /tmp/vaf_x_1/_pkgs" in cmd     # inside the workdir -> removed with it
    assert "--no-cache-dir" in cmd                   # shared container's pip cache must not grow
    assert cmd.endswith("numpy pandas==2.2.0")


def test_exec_env_exposes_and_redirects_packages():
    prefix = PythonSandboxTool._run_env_prefix("/tmp/vaf_x_1")
    assert "PYTHONPATH=/tmp/vaf_x_1/_pkgs" in prefix          # installed pkgs importable
    assert "PIP_TARGET=/tmp/vaf_x_1/_pkgs" in prefix          # in-code pip installs land there too
    bridged = PythonSandboxTool._run_env_prefix("/tmp/vaf_x_1", extra_pythonpath="/tmp/vaf_x_1")
    assert "PYTHONPATH=/tmp/vaf_x_1:/tmp/vaf_x_1/_pkgs" in bridged  # vaf_tools stub stays importable


def test_package_specs_reject_shell_metacharacters():
    ok = PythonSandboxTool._validate_packages(["numpy", "pandas==2.2.0", "uvicorn[standard]", "torch>=2.0,<3"])
    assert ok is None
    for evil in (["numpy; rm -rf /"], ["$(curl evil)"], ["a && b"], ["pkg`x`"], ["-r/etc/passwd"]):
        assert PythonSandboxTool._validate_packages(evil) is not None


# -- one person's container, values as environment ---------------------------

def test_each_run_goes_to_the_callers_scratch_environment(monkeypatch):
    """MUTATION: hand every run the same scratch environment regardless of scope - red."""
    import vaf.core.environments as envmod
    asked = []

    class _Mgr:
        def scratch_for(self, scope):
            asked.append(scope)
            raise envmod.EnvironmentRefused("stop here")

    monkeypatch.setattr(envmod, "get_environment_manager", lambda: _Mgr())
    monkeypatch.setattr(PythonSandboxTool, "_ensure_docker_available", staticmethod(lambda: (True, "")))
    out = PythonSandboxTool().run(code="print(1)", user_scope_id="scope-alice")
    assert asked == ["scope-alice"] and "stop here" in out


def test_the_executor_hands_values_over_as_environment_with_one_marker(monkeypatch):
    """The run's commands share one marker (so a Stop ends exactly this run) and the
    bridge values travel as env_values, never in the command text. MUTATION: build the
    command with NAME=value again - red."""
    import vaf.core.environments as envmod
    calls = []

    class _Mgr:
        def exec_in(self, env, argv, **kw):
            calls.append((argv, kw))
            return envmod.ExecResult(0, "out", "")

    monkeypatch.setattr(envmod, "get_environment_manager", lambda: _Mgr())
    execute = PythonSandboxTool()._executor(types.SimpleNamespace(container="c"), "run-1")
    execute("mkdir -p /tmp/vaf_run_1", 30)
    execute("python3 x.py", 30, env={"VAF_BRIDGE_TOKEN": "s3cr3t"})
    assert {kw["run_id"] for _, kw in calls} == {"run-1"}
    argv, kw = calls[-1]
    assert kw["env_values"] == {"VAF_BRIDGE_TOKEN": "s3cr3t"}
    assert not any("s3cr3t" in a for a in argv)


def test_a_timeout_and_a_stop_read_as_such(monkeypatch):
    import vaf.core.environments as envmod

    class _Mgr:
        def __init__(self, result):
            self.result = result

        def exec_in(self, env, argv, **kw):
            return self.result

    monkeypatch.setattr(envmod, "get_environment_manager",
                        lambda: _Mgr(envmod.ExecResult(-1, "", "", timed_out=True)))
    rc, _, err = PythonSandboxTool()._executor(None, "r")("sleep 9", 3)
    assert rc == -1 and "timed out after 3s" in err
    monkeypatch.setattr(envmod, "get_environment_manager",
                        lambda: _Mgr(envmod.ExecResult(-1, "", "", cancelled=True)))
    rc, _, err = PythonSandboxTool()._executor(None, "r")("sleep 9", 3)
    assert rc == -1 and "cancelled by stop request" in err


def test_the_bridge_run_hands_its_values_over_as_env():
    """MUTATION: build the command with `VAF_BRIDGE_TOKEN="..."` again - red."""
    calls = []

    def _exec(cmd, timeout, env=None):
        calls.append((cmd, env))
        return 0, "", ""

    PythonSandboxTool()._run_with_bridge(
        "print(1)", _exec, "/tmp/vaf_abc_1", 10,
        {"VAF_BRIDGE_URL": "http://host.docker.internal:4242", "VAF_BRIDGE_TOKEN": "tok-123"},
        "# stub")
    run_cmd, run_env = calls[-1]
    assert "tok-123" not in run_cmd and "VAF_BRIDGE_TOKEN" not in run_cmd
    assert run_env == {"VAF_BRIDGE_URL": "http://host.docker.internal:4242",
                       "VAF_BRIDGE_TOKEN": "tok-123"}
