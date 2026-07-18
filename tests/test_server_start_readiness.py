# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""ServerManager.start_server readiness/retry regressions (local llama-server).

Covers the contributed local-model fix plus the review amendments:
- /health == 200 is the only "started" signal (503 = still loading, keep waiting)
- a live server answering 503 gets the FULL configurable budget - a flat ~60s deadline
  turned legitimately slow cold loads (big GGUF, HDD, CPU-only) into a kill/reload loop
- on final timeout the still-loading process is terminated (no untracked orphan)
- the PID file is written immediately after spawn, not only at readiness
- "requires Flash Attention" death triggers exactly one retry without -ctv, the outcome
  is memoized for later restarts, and the retry appends to the log instead of truncating

Hermetic: requests / subprocess.Popen / time are faked; no llama-server, no network.
"""
import os
from pathlib import Path

import pytest

import vaf.core.backend as backend
from vaf.core.backend import ServerManager, _server_ready_budget


class FakeTime:
    """Deterministic clock: sleep() advances monotonic()/time() instantly."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def time(self):
        return self.t

    def sleep(self, secs):
        self.t += secs


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeProc:
    def __init__(self, pid, poll_fn, marker_writer=None, stdout=None):
        self.pid = pid
        self._poll_fn = poll_fn
        self.terminated = False
        self.killed = False
        if marker_writer and stdout is not None and hasattr(stdout, "write"):
            stdout.write(marker_writer)
            stdout.flush()

    def poll(self):
        return self._poll_fn()

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


def _llama_popen(record, factory):
    """Popen stand-in that only accepts llama-server commands - any other subprocess
    (e.g. a GPU probe that escaped its patch) fails the test loudly instead of being
    silently absorbed into the fake."""
    def popen(cmd, stdout=None, stderr=None, **kw):
        assert str(cmd[0]).endswith(("llama-server", "llama-server.exe")), f"unexpected Popen: {cmd}"
        record.append(list(cmd))
        return factory(cmd, stdout)
    return popen


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    """A ServerManager wired to a hermetic environment."""
    fake_time = FakeTime()
    monkeypatch.setattr(backend, "time", fake_time)
    # start_server re-imports get_primary_gpu locally (backend.py:694), so patch the
    # SOURCE module - patching only the backend namespace leaves the real GPU probe
    # running (and its nvidia-smi/lspci subprocesses polluting the Popen fakes).
    import vaf.core.gpu_detection as gpu_mod
    monkeypatch.setattr(gpu_mod, "get_primary_gpu", lambda: None)  # CPU path
    monkeypatch.setattr(backend, "get_primary_gpu", lambda: None)
    monkeypatch.setattr(backend, "get_app_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(backend, "is_debug_logging_enabled", lambda: False)

    overrides = {"provider": "local", "auto_start_local_server": True}

    class FakeConfig:
        @staticmethod
        def get(key, default=None):
            return overrides.get(key, default)

    monkeypatch.setattr(backend, "Config", FakeConfig)
    # start_server re-imports Config from vaf.core.config - patch that module attribute too
    import vaf.core.config as config_mod
    monkeypatch.setattr(config_mod, "Config", FakeConfig)

    m = ServerManager(skip_cleanup=True)
    m.pid_file = str(tmp_path / "server.pid")
    monkeypatch.setattr(m, "ensure_server_exists", lambda: True)
    monkeypatch.setattr(m, "stop_server", lambda *a, **k: None)

    model = tmp_path / "model.gguf"
    model.write_bytes(b"x" * 1024)  # size only feeds the n_parallel heuristic

    m._test = {"time": fake_time, "overrides": overrides, "model": str(model), "tmp": tmp_path}
    return m


def _wire_http(monkeypatch, health_fn, props_payload=None):
    """Fake the requests module: reuse-check refused, /health scripted, /props optional."""
    calls = {"n": 0}

    class FakeRequests:
        @staticmethod
        def get(url, timeout=None):
            if "/props" in url:
                return FakeResponse(200, props_payload or {"total_slots": 1})
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("connection refused")  # the reuse pre-check: no server yet
            return health_fn(calls["n"])

    monkeypatch.setattr(backend, "requests", FakeRequests)
    return calls


def test_slow_load_beyond_60s_still_succeeds(mgr, monkeypatch):
    """THE blocker regression: a healthy server that needs >60s to load (503 all along)
    must NOT be treated as failed - the old flat 120x0.5s budget did exactly that."""
    ft = mgr._test["time"]
    t0 = ft.t
    spawned = []

    def health(_n):
        # ready only after 180 (fake) seconds of loading
        return FakeResponse(200 if ft.t - t0 > 180 else 503)

    _wire_http(monkeypatch, health)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        _llama_popen(spawned, lambda cmd, out: FakeProc(4242, lambda: None)))

    assert mgr.start_server(mgr._test["model"], n_ctx=8192) is True
    assert len(spawned) == 1
    assert ft.t - t0 > 180  # it genuinely waited through the slow load


def test_pid_file_written_during_load_window(mgr, monkeypatch):
    """Orphan cleanup / idle-unload must find the server WHILE it is still loading."""
    seen = {"pid_during_load": False}

    def health(_n):
        if os.path.exists(mgr.pid_file):
            seen["pid_during_load"] = True
        return FakeResponse(200)

    _wire_http(monkeypatch, health)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        _llama_popen([], lambda cmd, out: FakeProc(4242, lambda: None)))

    assert mgr.start_server(mgr._test["model"], n_ctx=8192) is True
    assert seen["pid_during_load"] is True


def test_timeout_kills_the_stuck_server(mgr, monkeypatch):
    """After the ready budget a live-but-never-ready server is terminated, and no stale
    pid file survives - nothing is left for the next start to blindly kill/respawn."""
    mgr._test["overrides"]["server_ready_timeout"] = 90
    procs = []

    _wire_http(monkeypatch, lambda _n: FakeResponse(503))  # loading forever
    made = []
    monkeypatch.setattr(backend.subprocess, "Popen",
                        _llama_popen(procs, lambda cmd, out: made.append(FakeProc(4242, lambda: None)) or made[-1]))

    assert mgr.start_server(mgr._test["model"], n_ctx=8192) is False
    assert made[0].terminated is True
    assert not os.path.exists(mgr.pid_file)


def test_fa_death_retries_without_vquant_and_memoizes(mgr, monkeypatch):
    """Attempt 1 dies with the Flash-Attention marker -> exactly one retry without -ctv;
    the log keeps attempt 1's output (append, not truncate); the outcome is memoized so
    the NEXT start skips the doomed quantized attempt entirely."""
    spawned = []

    def factory(cmd, stdout):
        if "-ctv" in cmd:
            # attempt 1: write the root cause to the log, then die
            return FakeProc(1111, lambda: 1,
                            marker_writer="llama_init_from_model: quantized V cache was requested, "
                                          "but this requires Flash Attention\n",
                            stdout=stdout)
        return FakeProc(2222, lambda: None)

    _wire_http(monkeypatch, lambda _n: FakeResponse(200))
    monkeypatch.setattr(backend.subprocess, "Popen", _llama_popen(spawned, factory))

    assert mgr.start_server(mgr._test["model"], n_ctx=8192) is True
    assert len(spawned) == 2
    assert "-ctv" in spawned[0] and "-ctv" not in spawned[1]
    assert "-ctk" in spawned[1]  # K-quant stays (needs no FA)
    assert mgr._kv_vquant_unsupported is True

    log_text = (mgr._test["tmp"] / "logs" / "server_last.log").read_text()
    assert "requires Flash Attention" in log_text  # attempt 1 evidence preserved
    assert "retry without -ctv" in log_text

    # Memoization: a later restart must not re-pay the deterministic crash.
    spawned.clear()
    _wire_http(monkeypatch, lambda _n: FakeResponse(200))  # fresh counter: no reusable server
    # Remove the pid file from the first run: its FAKE pid may collide with a real
    # process on the machine, and the pid-reuse path would then skip the spawn
    # entirely (observed on a CI runner where pid 2222 happened to be alive).
    if os.path.exists(mgr.pid_file):
        os.remove(mgr.pid_file)
    assert mgr.start_server(mgr._test["model"], n_ctx=8192) is True
    assert len(spawned) == 1
    assert "-ctv" not in spawned[0]


def test_fa_death_retries_even_with_empty_log(mgr, monkeypatch):
    """Core hardening (live Gemma-on-Metal incident): attempt 1 dies but the log has
    NO Flash-Attention marker - at low --log-verbosity llama-server wrote an EMPTY log,
    or the marker scrolled out of the old tail window. The retry without -ctv must STILL
    fire whenever a fallback exists, so an empty/marker-less log can no longer silently
    disable the f16 fallback. The outcome is NOT memoized (no marker seen -> the flag
    stays False), so the next start still tries the quantized cache first."""
    spawned = []

    def factory(cmd, stdout):
        if "-ctv" in cmd:
            return FakeProc(1111, lambda: 1, stdout=stdout)  # attempt 1: die, EMPTY log
        return FakeProc(2222, lambda: None)                  # attempt 2 (f16): healthy

    _wire_http(monkeypatch, lambda _n: FakeResponse(200))
    monkeypatch.setattr(backend.subprocess, "Popen", _llama_popen(spawned, factory))

    assert mgr.start_server(mgr._test["model"], n_ctx=8192) is True
    assert len(spawned) == 2                            # retry fired despite the empty log
    assert "-ctv" in spawned[0] and "-ctv" not in spawned[1]
    assert mgr._kv_vquant_unsupported is False          # not memoized: the marker was never seen


def test_previous_log_survives_one_generation(mgr, monkeypatch):
    """A crashing server is auto-restarted within seconds; the previous start's output
    must survive as server_last.prev.log instead of being truncated away."""
    _wire_http(monkeypatch, lambda _n: FakeResponse(200))
    monkeypatch.setattr(backend.subprocess, "Popen",
                        _llama_popen([], lambda cmd, out: FakeProc(4242, lambda: None)))

    log_dir = mgr._test["tmp"] / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "server_last.log").write_text("post-mortem of the previous crash")

    assert mgr.start_server(mgr._test["model"], n_ctx=8192) is True
    assert (log_dir / "server_last.prev.log").read_text() == "post-mortem of the previous crash"


def test_ready_budget_clamps():
    import vaf.core.config as config_mod
    orig = config_mod.Config
    try:
        class C:
            @staticmethod
            def get(key, default=None):
                return {"server_ready_timeout": 5}.get(key, default)

        backend.Config = C
        assert _server_ready_budget() == 60.0  # never below 60s

        class C2:
            @staticmethod
            def get(key, default=None):
                return {"server_ready_timeout": "not-a-number"}.get(key, default)

        backend.Config = C2
        assert _server_ready_budget() == 600.0  # unparseable -> default
    finally:
        backend.Config = orig
