# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Ephemeral sandbox fallback hardening + temporary per-run pip installs.

The persistent compose sandbox was hardened long ago (own network, cap_drop
ALL, no-new-privileges) but the ephemeral fallback in DockerSandbox was
skipped: it landed on the DEFAULT bridge with full caps. And pip installs
went into the shared container's global site-packages forever. Both fixed;
these tests pin the contracts.
"""
from vaf.tools.sandbox import EPHEMERAL_NETWORK, ephemeral_hardening_flags
from vaf.tools.python_sandbox import PythonSandboxTool


# -- ephemeral container flags -------------------------------------------------

def test_hardening_flags_mirror_the_persistent_container():
    flags = ephemeral_hardening_flags(EPHEMERAL_NETWORK)
    joined = " ".join(flags)
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges:true" in joined
    assert f"--network {EPHEMERAL_NETWORK}" in joined
    assert "--add-host host.docker.internal:host-gateway" in joined  # Tool Bridge parity


def test_degraded_retry_keeps_caps_drops_only_network():
    flags = ephemeral_hardening_flags(None)
    joined = " ".join(flags)
    assert "--cap-drop ALL" in joined and "no-new-privileges:true" in joined
    assert "--network" not in joined and "--add-host" not in joined


def test_ephemeral_network_is_not_the_compose_name():
    """docker compose refuses to adopt a same-name network it did not create,
    so the ephemeral lane must use its own network name."""
    assert EPHEMERAL_NETWORK != "vaf-sandbox-network"


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


# -- targeted timeout kill (the pkill-on-slim gap) ------------------------------

def test_run_marker_extracted_from_every_command_shape():
    from vaf.tools.sandbox import extract_run_marker
    wd = "/tmp/vaf_ab12cd34ef56_deadbeef"
    for cmd in (f"mkdir -p {wd}",
                f"pip install --target {wd}/_pkgs numpy",
                f"cd {wd} && PIP_TARGET={wd}/_pkgs python3",
                f"rm -rf {wd}"):
        assert extract_run_marker(cmd) == wd
    assert extract_run_marker("docker ps") is None
    assert extract_run_marker("") is None


def test_kill_script_is_scoped_and_self_excluding():
    from vaf.tools.sandbox import kill_run_processes_cmd
    s = kill_run_processes_cmd("/tmp/vaf_x_1")
    assert '[ "$p" = "$$" ] && continue' in s          # the scanner never kills itself
    assert 'readlink "$d/cwd"' in s                     # catches children (pip) via inherited cwd
    assert 'grep -qa -- "/tmp/vaf_x_1" "$d/cmdline"' in s  # catches the payload shell
    assert 'kill -9' in s
    assert "pkill" not in s                             # slim images have no procps


def test_timeout_kill_uses_marker_and_skips_without_one(monkeypatch):
    from vaf.tools.python_sandbox import PythonSandboxTool

    calls = []
    import vaf.tools.python_sandbox as ps
    monkeypatch.setattr(ps.subprocess, "run", lambda *a, **k: calls.append(a[0]))

    class _P:
        def kill(self): pass

    tool = PythonSandboxTool()
    tool._kill_sandbox_exec(_P(), "cd /tmp/vaf_scope1_run9 && python3 x.py")
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:3] == ["docker", "exec", ps.SANDBOX_CONTAINER]
    assert "/tmp/vaf_scope1_run9" in argv[-1] and "kill -9" in argv[-1]

    calls.clear()
    tool._kill_sandbox_exec(_P(), "echo no marker here")
    assert calls == []                                  # never falls back to a broad kill
