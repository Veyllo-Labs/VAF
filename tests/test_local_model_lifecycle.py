# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The local model must not be pulled out from under work that is running.

Live incident 2026-07-20, reconstructed from the tray log and the backend log:

    16:05:38  IdleCheck  loaded=True  lastHeartbeat=1863s
    16:06:39  IdleCheck  loaded=False              <- unloaded while a task was in flight
    16:09:28  openai._base_client - Retrying request to /chat/completions   (x N)
    16:10:33  headless_runner: swap-back belt ... loaded=-                  <- start A (locked)
    16:10:34  tray: "Activity detected. Loading model..."                   <- start B (unlocked)
              -> llama-server: couldn't bind
    16:10:41  loaded=True

Three separate defects in one sequence:

1. The idle watchdog had no concept of WORK. Its inputs were "is a browser attached"
   (is_active) and "has the user typed lately" (really_away) - both describe the USER. The
   user had been quiet for a while, so really_away won and the model was unloaded although
   the machine was busy on their behalf. Work is the opposite of idle.
2. Two starts raced one second apart, because only one of the four start paths went through
   the lock. The loser died with "couldn't bind".
3. A killed llama-server child that is never wait()ed stays a zombie for the lifetime of the
   tray, and two were left behind.

The obvious fix for (2) - route every caller through ensure_local_model, which already holds
the lock - was rejected: four call sites pass n_ctx / n_gpu_layers / port and
ensure_local_model accepts none of them, so rerouting would silently drop the VRAM-aware
layer count. The lock moved into start_server instead, which is why it had to become
reentrant (ensure_local_model -> start_server on one thread).
"""
import ast
import threading
from pathlib import Path

import pytest

import vaf.core.backend as backend

_REPO = Path(__file__).resolve().parents[1]


def _source(rel: str) -> str:
    return (_REPO / rel).read_bytes().decode("utf-8")


# ── (1) never unload while work is in flight ────────────────────────────────────

def test_work_in_flight_reports_a_running_task(monkeypatch):
    import vaf.tray as tray

    class _Busy:
        def is_busy(self):
            return True

        def get_queue_size(self):
            return 0

    monkeypatch.setattr("vaf.core.task_queue.TaskQueue", lambda: _Busy())
    busy, reason = tray._work_in_flight()
    assert busy is True and "Task" in reason


def test_work_in_flight_reports_a_queued_task(monkeypatch):
    import vaf.tray as tray

    class _Queued:
        def is_busy(self):
            return False

        def get_queue_size(self):
            return 3

    monkeypatch.setattr("vaf.core.task_queue.TaskQueue", lambda: _Queued())
    busy, _ = tray._work_in_flight()
    assert busy is True, "a queued turn is work too: unloading now only delays it"


def test_work_in_flight_reports_a_running_subagent(monkeypatch):
    import vaf.tray as tray

    class _Idle:
        def is_busy(self):
            return False

        def get_queue_size(self):
            return 0

    class _Ipc:
        def get_active_tasks(self):
            return [object()]

    monkeypatch.setattr("vaf.core.task_queue.TaskQueue", lambda: _Idle())
    monkeypatch.setattr("vaf.core.subagent_ipc.get_ipc", lambda: _Ipc())
    busy, reason = tray._work_in_flight()
    assert busy is True and "Sub-agent" in reason


def test_work_in_flight_fails_towards_keeping_the_model(monkeypatch):
    """If we cannot tell, we must not unload. The two outcomes are not symmetric: a
    needlessly warm model costs VRAM until the next check, a wrong unload destroys work."""
    import vaf.tray as tray

    def _boom():
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr("vaf.core.task_queue.TaskQueue", _boom)
    busy, reason = tray._work_in_flight()
    assert busy is True and "probe failed" in reason.lower()


def test_the_watchdog_actually_consults_it_unconditionally():
    """work_busy must NOT be gated on really_away - that is the whole point. The user being
    quiet says nothing about whether the machine is busy for them."""
    src = _source("vaf/tray.py")
    line = next(ln for ln in src.splitlines() if ln.strip().startswith("keep_warm ="))
    assert "work_busy" in line, "the idle watchdog still ignores work in flight"
    assert "not really_away) and (is_active" in line, "the existing user-activity terms must stay"
    # work_busy sits at the top level of the or-chain, not inside the really_away group.
    assert "or work_busy or" in line.replace("  ", " "), (
        "work_busy must be an independent term, not gated on user activity"
    )


# ── (2) two starts may not race ─────────────────────────────────────────────────

def test_the_start_lock_is_reentrant():
    """ensure_local_model holds it and then calls start_server, which takes it again on the
    same thread. A plain Lock would self-deadlock."""
    assert isinstance(backend._ENSURE_LOCAL_LOCK, type(threading.RLock()))
    backend._ENSURE_LOCAL_LOCK.acquire()
    try:
        assert backend._ENSURE_LOCAL_LOCK.acquire(timeout=1), "not reentrant"
        backend._ENSURE_LOCAL_LOCK.release()
    finally:
        backend._ENSURE_LOCAL_LOCK.release()


def test_start_server_serializes_every_caller(monkeypatch):
    """THE regression: a second start must WAIT, not run alongside. This is checked on the
    public entry point, because that is what all four call sites use."""
    mgr = backend.ServerManager.__new__(backend.ServerManager)
    overlap = []
    inside = threading.Event()
    release = threading.Event()

    def _slow(self, *a, **kw):
        overlap.append("in")
        inside.set()
        release.wait(timeout=5)
        overlap.append("out")
        return True

    monkeypatch.setattr(backend.ServerManager, "_start_server_locked", _slow)
    monkeypatch.setattr(backend, "reap_abandoned_children", lambda: 0)

    t1 = threading.Thread(target=lambda: mgr.start_server("/m.gguf"))
    t1.start()
    assert inside.wait(timeout=5)

    second_done = threading.Event()
    t2 = threading.Thread(target=lambda: (mgr.start_server("/m.gguf"), second_done.set()))
    t2.start()
    assert not second_done.wait(timeout=0.5), "the second start ran while the first held the lock"

    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert overlap == ["in", "out", "in", "out"], f"starts overlapped: {overlap}"


def test_start_server_keeps_every_parameter():
    """Rerouting callers through ensure_local_model would have dropped n_ctx and
    n_gpu_layers - including the -1 = AUTO layer count that fits the model to VRAM."""
    import inspect

    outer = inspect.signature(backend.ServerManager.start_server).parameters
    inner = inspect.signature(backend.ServerManager._start_server_locked).parameters
    for name in ("model_path", "n_gpu_layers", "n_ctx", "port", "skip_provider_gate"):
        assert name in outer and name in inner, f"{name} lost in the serialization wrapper"
    # ensure_local_model still cannot carry them, which is exactly why the lock moved.
    assert "n_ctx" not in inspect.signature(backend.ensure_local_model).parameters


def test_no_start_path_bypasses_the_entry_point():
    """A caller that reaches the unlocked inner function would race again."""
    offenders = []
    for path in sorted((_REPO / "vaf").rglob("*.py")):
        rel = path.relative_to(_REPO).as_posix()
        if rel == "vaf/core/backend.py":
            continue
        if "_start_server_locked" in path.read_bytes().decode("utf-8"):
            offenders.append(rel)
    assert not offenders, f"these call the unlocked inner start: {offenders}"


# ── (3) no zombie children ──────────────────────────────────────────────────────

class _FakeProc:
    def __init__(self, exited=True):
        self._exited = exited
        self.polled = 0

    def poll(self):
        self.polled += 1
        return 0 if self._exited else None


def test_reaper_collects_exited_children_and_keeps_the_others():
    dead, alive = _FakeProc(True), _FakeProc(False)
    backend._ABANDONED_CHILDREN[:] = [dead, alive]
    assert backend.reap_abandoned_children() == 1
    assert backend._ABANDONED_CHILDREN == [alive], "a live child must not be dropped"
    backend._ABANDONED_CHILDREN[:] = []


def test_reaper_survives_a_broken_handle():
    class _Broken:
        def poll(self):
            raise OSError("gone")

    backend._ABANDONED_CHILDREN[:] = [_Broken()]
    # Dropped, but NOT counted as reaped: nothing was actually collected, and an inflated
    # number would make the tray log claim work it did not do.
    assert backend.reap_abandoned_children() == 0
    assert backend._ABANDONED_CHILDREN == []


def test_a_child_we_gave_up_on_is_parked_for_reaping():
    """stop_server abandons a wedged child after ~1s, which is right for responsiveness -
    but dropping the handle leaks a zombie for the lifetime of the tray."""
    src = _source("vaf/core/backend.py")
    stop = src[src.index("    def stop_server(self"):]
    nxt = stop.find("\n    def ", 10)          # stop_server may be the last method
    stop = stop[:nxt] if nxt != -1 else stop
    assert "_park_abandoned_child(self.process)" in stop, (
        "the give-up path must park the handle instead of dropping it"
    )


def test_the_reaper_is_actually_called():
    """A reaper nobody calls collects nothing."""
    callers = [
        rel for rel in ("vaf/core/backend.py", "vaf/tray.py")
        if "reap_abandoned_children()" in _source(rel)
    ]
    assert set(callers) == {"vaf/core/backend.py", "vaf/tray.py"}, (
        f"reaper not wired into both the start path and the idle tick: {callers}"
    )


# ── (4) a loading server is not a broken one ───────────────────────────────────

def test_the_pid_path_waits_for_a_loading_server_instead_of_killing_it(tmp_path, monkeypatch):
    """llama-server answers 503 while it maps a multi-GB GGUF. Killing it then and starting
    another one is the shape of the incident.

    Behavioural on purpose. The first version of this guard only counted the string "503" in
    the function body, which was already true before the fix - it went red pre-fix merely
    because the function had a different NAME, i.e. it pinned the rename, not the behaviour.
    """
    mgr = backend.ServerManager.__new__(backend.ServerManager)
    mgr.pid_file = str(tmp_path / "server.pid")
    Path(mgr.pid_file).write_text("4242", encoding="utf-8")
    mgr.process = object()

    monkeypatch.setattr(backend.ServerManager, "_is_process_running", lambda self, pid: True)
    monkeypatch.setattr(backend.ServerManager, "ensure_server_exists", lambda self: True)
    monkeypatch.setattr(backend, "_server_ready_budget", lambda: 2.0)
    monkeypatch.setattr(backend.Config, "get", staticmethod(
        lambda key, default=None: {"provider": "local", "auto_start_local_server": True}.get(key, default)))

    killed = []
    monkeypatch.setattr(backend.ServerManager, "stop_server",
                        lambda self, force_external=False: killed.append(True))

    # Health says "loading", then "ready with the wanted model".
    replies = [503, 503, 200]

    class _Resp:
        def __init__(self, code):
            self.status_code = code

    def _fake_get(url, timeout=None):
        return _Resp(replies.pop(0) if replies else 200)

    monkeypatch.setattr(backend.requests, "get", _fake_get)
    monkeypatch.setattr(backend, "_loaded_model_matches", lambda *a, **kw: True)
    monkeypatch.setattr(backend, "reap_abandoned_children", lambda: 0)
    monkeypatch.setattr(backend.time, "sleep", lambda _s: None)

    assert mgr.start_server("/models/wanted.gguf") is True
    assert not killed, "a server that is merely still loading must not be killed and respawned"


def test_the_guard_reads_a_real_function():
    """Cheap sanity: the names this file pins must exist."""
    tree = ast.parse(_source("vaf/core/backend.py"))
    names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef,))}
    for fn in ("start_server", "_start_server_locked", "reap_abandoned_children",
               "ensure_local_model", "stop_server"):
        assert fn in names, f"{fn} vanished"


@pytest.mark.parametrize("rel", ["vaf/tray.py", "vaf/core/backend.py"])
def test_changed_modules_stay_importable(rel):
    __import__(rel.replace("/", ".")[:-3])


def test_the_reaper_does_not_share_the_model_start_lock():
    """The reaper runs on the tray's ~1s idle tick, while the start lock is held for the
    WHOLE model load (minutes). Sharing it would freeze the tray loop - icon, idle checks and
    the unload decision itself - for the entire load. Found in review before it shipped."""
    assert backend._ABANDONED_LOCK is not backend._ENSURE_LOCAL_LOCK

    src = _source("vaf/core/backend.py")
    body = src[src.index("def reap_abandoned_children"):]
    body = body[:body.index("\ndef ", 10)]
    assert "_ABANDONED_LOCK" in body and "_ENSURE_LOCAL_LOCK" not in body, (
        "the reaper must not touch the model-start lock"
    )

    # Proof, not just inspection: hold the start lock and reap from another thread.
    done = threading.Event()
    backend._ENSURE_LOCAL_LOCK.acquire()
    try:
        t = threading.Thread(target=lambda: (backend.reap_abandoned_children(), done.set()))
        t.start()
        assert done.wait(timeout=3), "the reaper blocked on the model-start lock"
        t.join(timeout=3)
    finally:
        backend._ENSURE_LOCAL_LOCK.release()


def test_a_voice_call_entry_alone_does_not_pin_the_model(monkeypatch):
    """The registry is keyed on id(websocket) and voice_call_end comes from exactly one
    frontend site, so an abrupt teardown could orphan an entry. This term is uniquely
    dangerous: unlike the others it can be permanently true, which would pin the model for
    the life of the process and silently switch idle unloading off completely."""
    import vaf.tray as tray
    import vaf.core.web_server as ws

    class _Idle:
        def is_busy(self):
            return False

        def get_queue_size(self):
            return 0

    class _NoTasks:
        def get_active_tasks(self):
            return []

    monkeypatch.setattr("vaf.core.task_queue.TaskQueue", lambda: _Idle())
    monkeypatch.setattr("vaf.core.subagent_ipc.get_ipc", lambda: _NoTasks())

    dead_socket = object()
    monkeypatch.setattr(ws, "_VOICE_CALLS", {id(dead_socket): {"history": []}})
    monkeypatch.setattr(ws.manager, "active_connections", [])
    assert tray._work_in_flight() == (False, ""), "an orphaned call entry must not pin the model"

    live_socket = object()
    monkeypatch.setattr(ws, "_VOICE_CALLS", {id(live_socket): {"history": []}})
    monkeypatch.setattr(ws.manager, "active_connections", [live_socket])
    busy, reason = tray._work_in_flight()
    assert busy is True and reason == "Voice call", "a real live call must still hold the model"


def test_the_call_state_is_dropped_on_socket_teardown():
    """Both teardown paths, not just the graceful one."""
    src = _source("vaf/core/web_server.py")
    assert src.count("_VOICE_CALLS.pop(id(websocket), None)") == 2, (
        "the disconnect handler and the error handler must both drop the call state"
    )


def test_the_busy_state_is_visible_in_the_idle_log():
    """A permanently true busy term would otherwise leave no trace in the exact log this
    incident was reconstructed from: the reason only prints when it causes a LOAD."""
    src = _source("vaf/tray.py")
    state = src[src.index('f"IdleCheck state: loaded='):]
    state = state[:state.index(")\n", 10)]
    assert "work=" in state
