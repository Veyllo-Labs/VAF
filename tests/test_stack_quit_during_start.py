# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Quitting while the service stack is still starting leaves nothing running.

The start runs in a background thread and takes minutes on a first run. A quit in that
window raced it: `compose stop` enumerates the containers when it starts, the start's
still-running `up --build` then created tts and vaf-browser behind it (measured live: "Up
51 seconds" beside "Exited 53 seconds ago"), and nothing stopped them again. The stop now
cancels the start first, ends the `up`/`build` it is running (found by parentage), and
makes a second pass when it had to cut one short. Plus the two stop-side defects found on
the way: tts and the sandbox ignored SIGTERM (exit 137 after 10 s), and the per-user browser
containers, created with `docker run`, were never stopped at all.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

import vaf.core.service_stack as stack

REPO = Path(__file__).resolve().parents[1]


class _Result:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


@pytest.fixture
def hermetic(monkeypatch):
    monkeypatch.chdir(REPO)
    monkeypatch.setattr(stack, "_ensure_macos_brew_path", lambda: None)
    monkeypatch.setattr(stack, "is_docker_daemon_running", lambda: True)
    monkeypatch.setattr(stack, "resolve_docker_exe", lambda: "docker")
    monkeypatch.setattr(stack, "_maybe_rebuild_stale_browser_image", lambda *a, **k: None)
    yield
    stack._start_cancelled.clear()
    stack._start_interrupted.clear()


def test_a_quit_during_the_core_up_starts_no_further_phase(hermetic, monkeypatch):
    """MUTATION: drop the check after the core `up` and the optional `up --build` still
    runs after the quit - the very call that brought tts and vaf-browser up."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if "postgres" in cmd:
            stack.cancel_start()        # the quit arrives while the core up runs
        return _Result(0)

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    assert stack.ensure_service_stack() is False
    assert not [c for c in calls if "tts" in c], calls


def test_a_quit_during_the_browser_rebuild_skips_the_optional_up(hermetic, monkeypatch):
    calls = []
    monkeypatch.setattr(stack.subprocess, "run", lambda cmd, **kw: calls.append(list(cmd)) or _Result(0))
    monkeypatch.setattr(stack, "_maybe_rebuild_stale_browser_image",
                        lambda *a, **k: stack.cancel_start())
    stack.ensure_service_stack()
    assert not [c for c in calls if "tts" in c], calls


def test_a_new_start_is_not_cancelled_by_an_old_stop(hermetic, monkeypatch):
    stack.cancel_start()
    calls = []
    monkeypatch.setattr(stack.subprocess, "run", lambda cmd, **kw: calls.append(list(cmd)) or _Result(0))
    assert stack.ensure_service_stack() is True
    assert [c for c in calls if "tts" in c]


def test_cancel_ends_our_own_up_and_nothing_else(hermetic):
    """By parentage: our child running this compose file with `up` is ended; our child
    running it with `stop` is not. MUTATION: match on the file name alone and the stop
    dies too."""
    sleeper = "import time, sys; time.sleep(30)"
    up = subprocess.Popen([sys.executable, "-c", sleeper, stack.COMPOSE_FILENAME, "up", "-d"])
    stop = subprocess.Popen([sys.executable, "-c", sleeper, stack.COMPOSE_FILENAME, "stop"])
    try:
        time.sleep(0.3)
        assert stack.cancel_start() == 1
        up.wait(timeout=10)
        assert stop.poll() is None, "the stop command was ended as well"
        assert stack._start_interrupted.is_set()
    finally:
        for p in (up, stop):
            try:
                p.kill()
            except Exception:
                pass


@pytest.mark.parametrize("interrupted,still_ours,passes", [
    (False, True, 1),    # nothing was cut short: one stop
    (True, True, 2),     # a start was cut short: stop again
    (True, False, 1),    # ...unless another instance serves by then
])
def test_the_second_pass_only_after_a_cut_short_start(hermetic, monkeypatch, interrupted,
                                                      still_ours, passes):
    """MUTATION: drop the second pass and a container the daemon started just after the
    first stop stays up; drop the referee and a new instance's stack is pulled down."""
    stops = []
    monkeypatch.setattr(stack, "cancel_start",
                        lambda: stack._start_interrupted.set() if interrupted else None)
    monkeypatch.setattr(stack, "_compose_stop", lambda root, log: stops.append(1) or True)
    monkeypatch.setattr(stack.time, "sleep", lambda s: None)
    stack.stop_service_stack(still_ours=lambda: still_ours)
    assert len(stops) == passes


def test_the_stop_uses_the_start_s_docker_and_env_file(hermetic, monkeypatch, tmp_path):
    env = tmp_path / "compose.env"
    env.write_text("X=1\n")
    monkeypatch.setattr(stack, "resolve_docker_exe", lambda: "/opt/rancher/docker")
    monkeypatch.setattr(stack, "compose_env_file", lambda: env)
    seen = []
    monkeypatch.setattr(stack.subprocess, "run", lambda cmd, **kw: seen.append(list(cmd)) or _Result(0))
    stack._compose_stop(REPO, None)
    assert seen[0][:4] == ["/opt/rancher/docker", "compose", "--env-file", str(env)]


# -- the per-user browser containers --------------------------------------------------------


def test_the_quit_stops_the_pool_s_own_browsers_only(monkeypatch):
    """MUTATION: skip stop_known_instances and a running per-user browser outlives VAF
    (measured on macOS: 12 days). Only instances this pool knows - never by name."""
    import vaf.core.browser_pool as bp
    pool = bp.BrowserPool()
    for scope, name in (("a", "vaf-browser-u-aaa"), ("b", "vaf-browser-u-bbb")):
        pool._instances[scope] = type("I", (), {"container_name": name})()
    stopped = []
    monkeypatch.setattr(bp, "_pool", pool)
    monkeypatch.setattr(bp, "_docker", lambda args, timeout=60: stopped.append(list(args)))
    assert bp.stop_known_instances() == 2
    assert sorted(a[-1] for a in stopped) == ["vaf-browser-u-aaa", "vaf-browser-u-bbb"]
    assert all(a[:3] == ["stop", "-t", "5"] for a in stopped), "stopped, never removed"
    assert pool._instances == {}


def test_without_a_pool_nothing_is_touched(monkeypatch):
    import vaf.core.browser_pool as bp
    calls = []
    monkeypatch.setattr(bp, "_pool", None)
    monkeypatch.setattr(bp, "_docker", lambda args, timeout=60: calls.append(args))
    assert bp.stop_known_instances() == 0 and calls == []


# -- SIGTERM reaches the process in tts and the sandbox ---------------------------------------


def _service_block(name):
    text = (REPO / "docker-compose.memory.yml").read_text(encoding="utf-8")
    start = text.index(f"\n  {name}:\n")
    nxt = text.find("\n  ", start + 3)
    while nxt != -1 and text[nxt + 3] == " ":
        nxt = text.find("\n  ", nxt + 3)
    return text[start:nxt if nxt != -1 else len(text)]


@pytest.mark.parametrize("service", ["sandbox", "tts", "vaf-browser"])
def test_these_services_run_behind_an_init(service):
    """`sleep` and the Flask server as PID 1 ignore SIGTERM (no handler, and the kernel
    applies no default action to PID 1): every stop waited 10 s and ended in SIGKILL, exit
    137. MUTATION: remove `init: true` from either."""
    assert "\n    init: true" in _service_block(service), service


# -- the quit's order ---------------------------------------------------------------------------


def test_the_quit_cancels_the_start_before_it_looks_at_the_stack():
    """A start the quit has not cancelled can bring vaf-memory-db up after the quit began,
    and the StartedAt referee reads that as a new instance and skips the whole stop."""
    src = (REPO / "vaf" / "tray.py").read_text(encoding="utf-8")
    quit_fn = src[src.index("def quit_app("):]
    assert quit_fn.index("cancel_start()") < quit_fn.index("_shutdown_began_utc =")
    assert "docker_stop.join(" in quit_fn, "the quit no longer waits for the Docker stop"
    assert "stop_known_instances()" in quit_fn, "the quit no longer stops the pool's browsers"


@pytest.mark.parametrize("found,expected", [(None, True), ("self", True), (4242, False)])
def test_the_second_pass_referee_asks_for_another_instance(monkeypatch, found, expected):
    import vaf.core.instance as instance
    from vaf import tray
    pid = os.getpid() if found == "self" else found
    monkeypatch.setattr(instance, "find_service", lambda: (
        None if pid is None else instance.Instance(pid=pid, mode="tray")))
    assert tray._no_other_instance_serves() is expected
