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


@pytest.fixture(autouse=True)
def _the_real_start(monkeypatch):
    """This file is about the stack start itself, with docker faked: the suite-wide stub
    (conftest `_no_service_stack`) steps aside here."""
    monkeypatch.setattr(stack, "ensure_service_stack", stack._real_ensure_service_stack)


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
    monkeypatch.setattr("vaf.core.containers.docker", lambda args, timeout=60: stopped.append(list(args)))
    monkeypatch.setattr(bp, "_in_use_by_another_process", lambda name: False)
    assert bp.stop_known_instances() == 2
    assert sorted(a[-1] for a in stopped) == ["vaf-browser-u-aaa", "vaf-browser-u-bbb"]
    assert all(a[:3] == ["stop", "-t", "5"] for a in stopped), "stopped, never removed"
    assert pool._instances == {}


def test_without_a_pool_nothing_is_touched(monkeypatch):
    import vaf.core.browser_pool as bp
    calls = []
    monkeypatch.setattr(bp, "_pool", None)
    monkeypatch.setattr("vaf.core.containers.docker", lambda args, timeout=60: calls.append(args))
    assert bp.stop_known_instances() == 0 and calls == []


# -- SIGTERM reaches the process in tts and the sandbox ---------------------------------------


def _service_block(name):
    text = (REPO / "docker-compose.memory.yml").read_text(encoding="utf-8")
    start = text.index(f"\n  {name}:\n")
    nxt = text.find("\n  ", start + 3)
    while nxt != -1 and text[nxt + 3] == " ":
        nxt = text.find("\n  ", nxt + 3)
    return text[start:nxt if nxt != -1 else len(text)]


# Services whose own PID 1 handles SIGTERM, each with the reason. Everything else runs
# behind docker-init.
_OWN_SIGNAL_HANDLING = {
    "postgres": "docker-entrypoint.sh execs postgres, which handles SIGTERM as PID 1",
    "redis": "docker-entrypoint.sh execs redis-server, which handles SIGTERM as PID 1",
    "gotenberg": "the image's entrypoint is tini",
}


def _compose_services():
    """The keys of the top-level `services:` block, which ends at the next top-level key."""
    import re
    text = (REPO / "docker-compose.memory.yml").read_text(encoding="utf-8")
    body = text[text.index("\nservices:\n") + len("\nservices:\n"):]
    nxt = re.search(r"^[A-Za-z]", body, flags=re.M)
    body = body[:nxt.start()] if nxt else body
    return re.findall(r"^  ([a-z][a-z0-9-]*):\s*$", body, flags=re.M)


def test_the_service_list_is_read():
    assert {"postgres", "tts", "stt", "vaf-browser"} <= set(_compose_services())


@pytest.mark.parametrize("service", _compose_services())
def test_every_service_runs_behind_an_init_or_says_why_not(service):
    """A PID-1 process without a SIGTERM handler ignores the stop (the kernel applies no
    default action to PID 1): `sleep`, the Flask server and the Whisper service while it
    loads its model all waited out the grace and ended in SIGKILL, exit 137. The rule is
    per service, so the next one added without an init fails here. MUTATION: remove
    `init: true` from any of them."""
    if service in _OWN_SIGNAL_HANDLING:
        return
    assert "\n    init: true" in _service_block(service), (
        f"{service} runs without docker-init; add `init: true` or list it in "
        "_OWN_SIGNAL_HANDLING with the reason its PID 1 handles SIGTERM")


# -- the quit's order ---------------------------------------------------------------------------


def test_the_quit_cancels_the_start_before_it_looks_at_the_stack():
    """A start the quit has not cancelled can bring vaf-memory-db up after the quit began,
    and the StartedAt referee reads that as a new instance and skips the whole stop."""
    src = (REPO / "vaf" / "tray.py").read_text(encoding="utf-8")
    quit_fn = src[src.index("def quit_app("):]
    assert quit_fn.index("cancel_start()") < quit_fn.index("_shutdown_began_utc =")
    assert "docker_stop.join(" in quit_fn, "the quit no longer waits for the Docker stop"
    assert "stop_known_instances()" in quit_fn, "the quit no longer stops the pool's browsers"


def test_the_quit_stops_environments_beside_the_browsers_not_behind_them():
    """Both are `docker run` containers compose stop never sees. MUTATION: call
    stop_all_at_quit at the end of _stop_browsers again - red: the environments started
    only after the browsers used up their budget, and os._exit cut them off."""
    src = (REPO / "vaf" / "tray.py").read_text(encoding="utf-8")
    quit_fn = src[src.index("def quit_app("):]
    browsers_fn = quit_fn[quit_fn.index("def _stop_browsers("):quit_fn.index("def _stop_environments(")]
    assert "stop_all_at_quit" not in browsers_fn
    env_fn = quit_fn[quit_fn.index("def _stop_environments("):quit_fn.index("browsers = threading.Thread(")]
    assert "stop_all_at_quit()" in env_fn
    start = quit_fn.index("environments.start()")
    assert start < quit_fn.index("stop_memory_stack(still_ours=")
    assert "for worker in (browsers, environments):" in quit_fn
    assert "worker.join(timeout=max(0.0, 18.0 - (time.monotonic() - quit_began)))" in quit_fn


@pytest.mark.parametrize("found,expected", [(None, True), ("self", True), (4242, False)])
def test_the_second_pass_referee_asks_for_another_instance(monkeypatch, found, expected):
    import vaf.core.instance as instance
    from vaf import tray
    pid = os.getpid() if found == "self" else found
    monkeypatch.setattr(instance, "find_service", lambda: (
        None if pid is None else instance.Instance(pid=pid, mode="tray")))
    assert tray._no_other_instance_serves() is expected


def test_a_container_that_never_became_healthy_is_stopped_too(monkeypatch):
    """MUTATION: stop only `_instances` again and a started-but-unhealthy container
    (never handed out, never cached) runs on after the quit."""
    import vaf.core.browser_pool as bp
    pool = bp.BrowserPool()
    pool._owned["c"] = "vaf-browser-u-ccc"          # started, readiness failed
    stopped = []
    monkeypatch.setattr(bp, "_pool", pool)
    monkeypatch.setattr("vaf.core.containers.docker", lambda args, timeout=60: stopped.append(list(args)))
    monkeypatch.setattr(bp, "_in_use_by_another_process", lambda name: False)
    assert bp.stop_known_instances() == 1
    assert stopped and stopped[0][-1] == "vaf-browser-u-ccc"


def test_an_allocation_under_way_at_quit_stops_its_own_container(monkeypatch):
    """The quit's snapshot is taken; a `docker run` finishing after it must not register
    a container nobody will stop. MUTATION: register without the closing check."""
    import vaf.core.browser_pool as bp
    pool = bp.BrowserPool()
    stopped = []
    monkeypatch.setattr("vaf.core.containers.docker", lambda args, timeout=60: stopped.append(list(args)))
    monkeypatch.setattr(bp, "_in_use_by_another_process", lambda name: False)
    pool._closing = True
    assert pool._take_ownership("d", "vaf-browser-u-ddd") is False
    assert stopped == [["stop", "-t", "5", "vaf-browser-u-ddd"]]
    assert pool._owned == {}
    assert pool._resolve_inner("e") is None, "a closing pool must not allocate"


def test_an_adoption_finishing_at_quit_spares_a_browser_in_use_elsewhere(monkeypatch):
    """The allocation adopted a running container a `vaf run` session is using, and the
    pool closed meanwhile. MUTATION: stop without the quit's in-use rule and that
    session's browser is cut mid-use."""
    import vaf.core.browser_pool as bp
    pool = bp.BrowserPool()
    stopped = []
    monkeypatch.setattr("vaf.core.containers.docker", lambda args, timeout=60: stopped.append(list(args)))
    monkeypatch.setattr(bp, "_in_use_by_another_process", lambda name: True)
    pool._closing = True
    assert pool._take_ownership("f", "vaf-browser-u-fff") is False
    assert stopped == [], "an adopted browser another VAF process uses was stopped"
    assert pool._owned == {}


_HOLDER = ("import socket, sys, time; "
           "s = socket.create_connection(('127.0.0.1', int(sys.argv[1]))); time.sleep(30)")


def _published_port(monkeypatch, bp, port):
    monkeypatch.setattr("vaf.core.containers.docker", lambda args, timeout=20: type(
        "R", (), {"returncode": 0, "stdout": f"9222/tcp -> 127.0.0.1:{port}\n"})())


def test_a_browser_another_vaf_process_is_connected_to_is_left_running(monkeypatch):
    """Real sockets: a process that is NOT ours (its launcher exited, as a `vaf run` in
    another terminal) with a `vaf run` command line holds a connection to the container's
    published port. MUTATION: stop without asking and that session's browser is cut
    mid-use."""
    import socket
    import vaf.core.browser_pool as bp
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    launcher = subprocess.run(
        [sys.executable, "-c",
         "import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', sys.argv[1], "
         "sys.argv[2], 'vaf.main', 'run'], stdout=subprocess.DEVNULL, "
         "stderr=subprocess.DEVNULL); print(p.pid)", _HOLDER, str(port)],
        capture_output=True, text=True, timeout=30)
    holder = psutil.Process(int(launcher.stdout.strip()))
    try:
        conn, _ = srv.accept()
        _published_port(monkeypatch, bp, port)
        assert bp._in_use_by_another_process("vaf-browser-u-shared") is True
        holder.kill()
        holder.wait(timeout=10)
        conn.close()
        time.sleep(0.3)
        assert bp._in_use_by_another_process("vaf-browser-u-shared") is False
    finally:
        try:
            holder.kill()
        except psutil.NoSuchProcess:
            pass
        srv.close()


def test_a_connection_from_our_own_descendant_does_not_keep_the_browser(monkeypatch):
    """A sub-agent this instance started is ended by the same quit, so its connection is
    no reason to leave the container running. MUTATION: count only this process as ours
    and the browser outlives the quit that killed its only user."""
    import socket
    import vaf.core.browser_pool as bp
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    child = subprocess.Popen([sys.executable, "-c", _HOLDER, str(port), "vaf.main", "subagent"])
    try:
        conn, _ = srv.accept()
        _published_port(monkeypatch, bp, port)
        assert bp._in_use_by_another_process("vaf-browser-u-shared") is False
        conn.close()
    finally:
        child.kill()
        child.wait(timeout=10)
        srv.close()


def test_the_stop_drains_a_step_launched_after_its_first_scan(hermetic, monkeypatch):
    """The start checked the flag, then the cancel scanned (nothing yet), then the start
    launched its `up`. MUTATION: drop the drain loop and that `up` runs on after the stop."""
    import threading
    stack._start_active.set()
    late = []

    def launch_late():
        time.sleep(0.3)
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                              stack.COMPOSE_FILENAME, "up", "-d"])
        late.append(p)
        p.wait()                      # returns once the drain ends it
        stack._start_active.clear()   # the start sees the flag and returns

    t = threading.Thread(target=launch_late, daemon=True)
    t.start()
    monkeypatch.setattr(stack, "_compose_stop", lambda root, log: True)
    try:
        stack.stop_service_stack(still_ours=lambda: True)
        assert late and late[0].poll() is not None, "the late `up` survived the stop"
        assert stack._start_interrupted.is_set()
    finally:
        for p in late:
            try:
                p.kill()
            except Exception:
                pass
        stack._start_active.clear()


def test_the_quit_leaves_a_browser_in_use_elsewhere_running(monkeypatch):
    import vaf.core.browser_pool as bp
    pool = bp.BrowserPool()
    pool._owned["s"] = "vaf-browser-u-sss"
    stopped = []
    monkeypatch.setattr(bp, "_pool", pool)
    monkeypatch.setattr("vaf.core.containers.docker", lambda args, timeout=60: stopped.append(list(args)))
    monkeypatch.setattr(bp, "_in_use_by_another_process", lambda name: True)
    bp.stop_known_instances()
    assert stopped == [], "a browser another VAF process uses was stopped"


def test_the_browsers_do_not_hold_up_the_stack_stop():
    """The browsers may take their whole budget; the stack stop must not wait behind them
    past the quit's bound (os._exit would then cut it off entirely)."""
    src = (REPO / "vaf" / "tray.py").read_text(encoding="utf-8")
    fn = src[src.index("def _stop_docker():"):]
    assert fn.index("browsers.start()") < fn.index("stop_memory_stack(still_ours=")
