# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""vaf.core.containers: the one copy of the docker calls behind VAF's own containers.

The browser pool used to carry these as private helpers; the sandbox environments
build on the same calls. The tests pin what other state depends on (the scope hash
names persisted volumes) and what each helper decides, without a docker daemon."""
import subprocess
import types
from pathlib import Path

import pytest

from vaf.core import containers


def _done(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_scope_hash_is_pinned_to_literal_values():
    """The browser pool's profile volumes are named by this hash: a change orphans
    every saved browser profile, logins included. Literals on purpose - a test that
    recomputed the hash would follow a change instead of catching it."""
    assert containers.scope_hash("alice") == "2bd806c97f0e"
    assert containers.scope_hash("scope-a") == "f051bb2c68cb"
    assert containers.scope_hash("5b1f2c3d-0000-4000-8000-000000000001") == "2e36860feba2"


def test_docker_resolves_the_binary_and_opens_no_window_on_windows(monkeypatch):
    seen = {}

    def _run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return _done()

    import vaf.core.service_stack as stack
    monkeypatch.setattr(stack, "resolve_docker_exe", lambda: "/opt/rd/bin/docker")
    monkeypatch.setattr(containers.subprocess, "run", _run)
    monkeypatch.setattr(containers.sys, "platform", "win32")
    monkeypatch.setattr(containers.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    containers.docker(["ps"], timeout=7, env={"A": "1"})
    assert seen["argv"] == ["/opt/rd/bin/docker", "ps"]
    assert seen["kw"]["timeout"] == 7 and seen["kw"]["env"] == {"A": "1"}
    assert seen["kw"]["creationflags"] == 0x08000000

    monkeypatch.setattr(containers.sys, "platform", "linux")
    containers.docker(["ps"])
    assert "creationflags" not in seen["kw"]


def test_docker_text_is_utf8_whatever_the_locale(monkeypatch):
    """A Windows client decodes captured text with the locale's code page (cp1252) and
    raises on a byte it does not define; a container speaks UTF-8. MUTATION: back to
    text=True for either call - red."""
    seen = {}

    def _run(argv, **kw):
        seen["run"] = kw
        return _done()

    def _popen(argv, **kw):
        seen["popen"] = kw
        return _Proc(("done", 0, "", ""))

    import vaf.core.service_stack as stack
    monkeypatch.setattr(stack, "resolve_docker_exe", lambda: "docker")
    monkeypatch.setattr(containers.subprocess, "run", _run)
    monkeypatch.setattr(containers, "_popen", _popen)
    containers.docker(["ps"])
    assert (seen["run"]["encoding"], seen["run"]["errors"]) == ("utf-8", "replace")
    assert "text" not in seen["run"]
    containers.docker(["exec", "c", "tar", "-cf", "-", "x"], binary=True)
    assert "encoding" not in seen["run"] and "text" not in seen["run"]   # a tar stream stays bytes
    containers.exec_bounded("c", ["true"], timeout=5, workdir="/tmp", run_id="r1")
    assert (seen["popen"]["encoding"], seen["popen"]["errors"]) == ("utf-8", "replace")


def test_container_state_and_running_count(monkeypatch):
    answers = {
        ("inspect", "a"): _done(0, "running\n"),
        ("inspect", "b"): _done(1, "", "No such object"),
        ("ps",): _done(0, "vaf-x-1\nvaf-x-2\n\n"),
    }

    def _docker(args, timeout=60, **kw):
        key = tuple(args[:2]) if args[0] == "inspect" else (args[0],)
        return answers[key]

    monkeypatch.setattr(containers, "docker", _docker)
    assert containers.container_state("a") == "running"
    assert containers.container_state("b") is None
    assert containers.count_running("vaf-x-") == 2
    answers[("ps",)] = _done(1, "", "daemon down")
    assert containers.count_running("vaf-x-") == 0


@pytest.mark.parametrize("exists, create_rc, reinspect_rc, expected", [
    (True, None, None, True),      # already there: no create at all
    (False, 0, None, True),        # created
    (False, 1, 0, True),           # lost a race, the other creator won
    (False, 1, 1, False),          # neither: say so
])
def test_ensure_network(monkeypatch, exists, create_rc, reinspect_rc, expected):
    calls = []
    inspects = iter([_done(0 if exists else 1, "net-a\n" if exists else ""),
                     _done(reinspect_rc if reinspect_rc is not None else 1, "net-a\n")])

    def _docker(args, timeout=60, **kw):
        calls.append(list(args))
        if args[:2] == ["network", "inspect"]:
            return next(inspects)
        return _done(create_rc)

    monkeypatch.setattr(containers, "docker", _docker)
    assert containers.ensure_network(
        "net-a", internal=True, labels={"vaf.env": "1"},
        options={"com.docker.network.bridge.gateway_mode_ipv4": "isolated"}) is expected
    creates = [c for c in calls if c[:2] == ["network", "create"]]
    if exists:
        assert creates == []
    else:
        assert creates == [["network", "create", "--driver", "bridge", "--internal",
                            "--label", "vaf.env=1",
                            "-o", "com.docker.network.bridge.gateway_mode_ipv4=isolated",
                            "net-a"]]


def test_ensure_network_never_raises(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(containers, "docker", _boom)
    assert containers.ensure_network("n") is False


def test_the_pool_keeps_no_private_copies():
    """The extraction deleted the pool's own docker runner, hash, memory reader and
    network creation. MUTATION: add a private `_docker` back to browser_pool - red."""
    src = (Path(containers.__file__).parent / "browser_pool.py").read_text(encoding="utf-8")
    for gone in ("def _docker(", "def _scope_hash(", "def _mem_available_mb(",
                 "def _container_state(", "import hashlib", "import subprocess",
                 '"network", "create"'):
        assert gone not in src, gone
    interactive = (Path(containers.__file__).parent / "browser_interactive.py").read_text(encoding="utf-8")
    assert "from vaf.core.browser_pool import _docker" not in interactive


def test_docker_raises_what_subprocess_raises(monkeypatch):
    """Callers decide what a missing docker means; the helper does not swallow it."""
    def _run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=1)
    import vaf.core.service_stack as stack
    monkeypatch.setattr(stack, "resolve_docker_exe", lambda: "docker")
    monkeypatch.setattr(containers.subprocess, "run", _run)
    with pytest.raises(subprocess.TimeoutExpired):
        containers.docker(["ps"], timeout=1)


# -- bounded exec inside a container ---------------------------------------------------

def test_kill_marked_cmd_only_takes_a_plain_marker():
    cmd = containers.kill_marked_cmd("VAF_RUN_ID", "ab12cd34ef56")
    assert 'grep -qx "VAF_RUN_ID=ab12cd34ef56"' in cmd and "/environ" in cmd
    for bad in ("x; rm -rf /", "", "a b", "$(id)"):
        with pytest.raises(ValueError):
            containers.kill_marked_cmd("VAF_RUN_ID", bad)
    with pytest.raises(ValueError):
        containers.kill_marked_cmd("PATH", "x")


class _Proc:
    def __init__(self, outcome):
        self.outcome = outcome          # ("done", rc, out, err) or "hang"
        self.killed = False
        self.returncode = None
        self.inputs = []

    def communicate(self, input=None, timeout=None):
        self.inputs.append(input)
        if self.outcome == "hang" and not self.killed:
            raise subprocess.TimeoutExpired("docker", timeout)
        if self.outcome == "hang":
            return "partial", ""
        _, self.returncode, out, err = self.outcome
        return out, err

    def kill(self):
        self.killed = True


def test_exec_bounded_builds_one_marked_bounded_command(monkeypatch):
    seen = {}
    import vaf.core.service_stack as stack
    monkeypatch.setattr(stack, "resolve_docker_exe", lambda: "docker")

    def _popen(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return _Proc(("done", 0, "ok\n", ""))

    monkeypatch.setattr(containers, "_popen", _popen)
    rc, out, err, timed_out, cancelled = containers.exec_bounded(
        "env-c", ["python3", "x.py"], timeout=30, workdir="/workspace", run_id="r1",
        env_values={"VAF_BRIDGE_TOKEN": "s3cr3t"}, input_text="data")
    assert (rc, out, timed_out, cancelled) == (0, "ok\n", False, False)
    argv = seen["argv"]
    assert argv[:3] == ["docker", "exec", "-i"]
    assert argv[argv.index("-w") + 1] == "/workspace"
    assert "VAF_RUN_ID=r1" in argv
    assert argv[argv.index("env-c"):] == ["env-c", "timeout", "-s", "KILL", "30", "python3", "x.py"]
    assert not any("s3cr3t" in a for a in argv) and "VAF_BRIDGE_TOKEN" in argv
    assert seen["kw"]["env"]["VAF_BRIDGE_TOKEN"] == "s3cr3t"


def test_a_stop_kills_the_client_and_the_run_inside(monkeypatch):
    """docker exec does not pass a kill on: the run's own processes are ended by their
    marker. MUTATION: drop the in-container kill - red."""
    import vaf.core.service_stack as stack
    monkeypatch.setattr(stack, "resolve_docker_exe", lambda: "docker")
    proc = _Proc("hang")
    monkeypatch.setattr(containers, "_popen", lambda argv, **kw: proc)
    kills = []
    monkeypatch.setattr(containers, "docker", lambda args, timeout=60, **kw: kills.append(args) or _done())
    rc, out, err, timed_out, cancelled = containers.exec_bounded(
        "env-c", ["sleep", "99"], timeout=30, workdir="/workspace", run_id="r2",
        check_stop=lambda: True)
    assert proc.killed and cancelled and not timed_out and rc == -1
    assert "cancelled by stop request" in err
    assert kills and kills[0][:3] == ["exec", "env-c", "sh"]
    assert 'VAF_RUN_ID=r2' in kills[0][-1]


def test_the_in_container_clock_reads_as_a_timeout(monkeypatch):
    import vaf.core.service_stack as stack
    monkeypatch.setattr(stack, "resolve_docker_exe", lambda: "docker")
    monkeypatch.setattr(containers, "_popen", lambda argv, **kw: _Proc(("done", 137, "", "")))
    assert containers.exec_bounded("c", ["sleep", "9"], timeout=1, workdir="/", run_id="r3")[3] is True
