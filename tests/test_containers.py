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
