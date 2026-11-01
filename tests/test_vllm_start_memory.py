# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The start memory of the managed vLLM (vaf/core/vllm_server.py): a start that failed is not
run again by the tray, the headless runner or ``load_model`` until a switch clears it.

Measured before it existed: after the fp8 start died and the fallback did not fit, the tray's
activity loop and the runner's lazy load started vLLM again four times in three minutes, each
about 40 s of load and compile. Nothing here starts vLLM: the launch is a counting fake Popen.

MUTATIONS, each turns the named test red:
- drop the remembered-failure check in `start` -> test_died_start_is_not_relaunched;
- drop `clear_start_failure` in `local_engine.apply` -> test_apply_clears_memory;
- compare only the repo in `remembered_failure` (not the signature) ->
  test_other_signature_launches;
- give a fit refusal no retry time -> test_fit_refusal_retried_after_60s;
- drop the check in `_start_vllm_locked` -> test_backend_skips_handover_when_remembered;
- remember an interrupted start -> test_interrupted_start_is_not_remembered;
- return "" for every local turn in `turn_refusal_reason` ->
  test_turn_refusal_reason_names_vllm_failure;
- drop the guard in the tray's `start_model_async` ->
  test_tray_start_model_async_skips_remembered_failure;
- remember a fallback's fit refusal over the crash before it ->
  test_crash_then_fit_refusal_is_not_relaunched;
- drop the BaseException stop in `start` or in `first_start` ->
  test_an_interrupt_in_the_ready_wait_stops_the_server /
  test_an_interrupt_in_the_test_request_stops_the_server.
"""
import time
from types import SimpleNamespace

import pytest

from vaf.core import local_engine as le
from vaf.core import vllm_server as vs

REPO = "Qwen/Qwen3-4B-AWQ"
SHAPE_4B = vs.ModelShape(weights_bytes=int(2.67e9), layers=36, kv_heads=8, head_dim=128,
                         max_position=40960)


@pytest.fixture(autouse=True)
def no_display(monkeypatch):
    """The display state is pinned: this machine drives a display and a CI runner does not,
    and the desktop reserve follows it (vllm_server.desktop_reserve_mb)."""
    from vaf.core import vllm_server
    monkeypatch.setattr(vllm_server, "_display_active", lambda: False)


@pytest.fixture
def home(monkeypatch, tmp_path):
    from vaf.core.config import Config
    values = {"local_engine": "vllm", "vllm_model": REPO, "n_ctx": 32768}
    real = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: values[key] if key in values else real(key, default)))
    monkeypatch.setattr(Config, "set", staticmethod(lambda key, value: values.__setitem__(key, value)))
    monkeypatch.setattr(Config, "load", staticmethod(lambda: dict(values)))
    monkeypatch.setattr(vs, "backend", lambda: vs.CUDA)
    monkeypatch.setattr(vs, "compute_capability", lambda: (8, 6))
    monkeypatch.setattr(vs, "flashinfer_buildable", lambda env=None: False)
    monkeypatch.setattr(vs, "venv_dir", lambda: tmp_path / "bin" / "vllm" / "venv")
    monkeypatch.setattr(vs, "pid_file", lambda: tmp_path / ".vaf" / "vllm.pid")
    monkeypatch.setattr(vs, "log_path", lambda: tmp_path / "logs" / "vllm_server.log")
    monkeypatch.setattr(vs, "_child", None)
    monkeypatch.setattr(vs, "_start_failure", None)
    monkeypatch.setattr(vs, "_came_up", None)
    monkeypatch.setattr(vs, "_logged_failure", None)
    monkeypatch.setattr(vs, "_stopped_while_loading", set())
    monkeypatch.setattr(vs, "is_installed", lambda: True)
    monkeypatch.setattr(vs, "is_current", lambda: True)
    monkeypatch.setattr(vs, "recipe_current", lambda: True)
    monkeypatch.setattr(vs, "local_snapshot", lambda repo: str(tmp_path / "hf" / repo))
    monkeypatch.setattr(vs.ModelShape, "from_dir", classmethod(lambda cls, d: SHAPE_4B))
    monkeypatch.setattr(vs, "_gpu_memory", lambda: (20000, 24576))
    monkeypatch.setattr(vs, "health_status", lambda port=8080, timeout=1.0: None)
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "terminate_process_group", staticmethod(lambda pgid, grace=1.0: None))
    return values


class _Proc:
    _next = 910001

    def __init__(self, dies=True):
        self.pid = _Proc._next
        _Proc._next += 1
        self.dies = dies

    def poll(self):
        return 1 if self.dies else None

    def wait(self, timeout=None):
        return 0


def _counting_popen(calls, dies=True):
    def popen(argv, stdout=None, **kw):
        calls.append(argv)
        if stdout is not None and hasattr(stdout, "write"):
            stdout.write("RuntimeError: Could not find nvcc and default cuda_home\n")
            stdout.flush()
        return _Proc(dies=dies)
    return popen


def test_died_start_is_not_relaunched(home):
    calls = []
    assert vs.start(REPO, 32768, popen=_counting_popen(calls)) is False
    assert len(calls) == 2, "the plan's two attempts ran once"
    failure = vs.start_failure()
    assert failure is not None and failure.cause == "kernels" and failure.retry_at == 0.0
    began = time.monotonic()
    assert vs.start(REPO, 32768, popen=_counting_popen(calls)) is False
    assert time.monotonic() - began < 1.0
    assert len(calls) == 2, "a remembered failure was launched again"
    assert "vaf engine llama" in vs.failure_sentence(failure)
    assert vs.SERVER_LOG in vs.failure_sentence(failure)


def test_apply_clears_memory(home, monkeypatch):
    calls = []
    vs.start(REPO, 32768, popen=_counting_popen(calls))
    assert vs.remembered_failure(REPO, 32768) is not None
    monkeypatch.setattr(le, "support", lambda **kw: le.EngineSupport(le.SUPPORTED, "ok", "supported"))
    monkeypatch.setattr(le, "_switch_lock", lambda: SimpleNamespace(
        acquire=lambda timeout=0: None, release=lambda: None))
    monkeypatch.setattr(le, "_apply_llama", lambda progress: le.EngineSwitch("done", "switched_llama"))
    le.apply("llama")
    assert vs.start_failure() is None, "a switch is a fresh try"


def test_other_signature_launches(home):
    calls = []
    vs.start(REPO, 32768, popen=_counting_popen(calls))
    assert len(calls) == 2
    vs.start(REPO, 40960, popen=_counting_popen(calls))
    assert len(calls) == 4, "another context is another start"
    vs.start("Qwen/Qwen3-8B-AWQ", 40960, popen=_counting_popen(calls))
    assert len(calls) == 6, "another model is another start"


def test_fit_refusal_retried_after_60s(home, monkeypatch):
    monkeypatch.setattr(vs, "_gpu_memory", lambda: (3000, 10240))
    now = [1000.0]
    monkeypatch.setattr(vs.time, "monotonic", lambda: now[0])
    calls = []
    assert vs.start(REPO, 32768, popen=_counting_popen(calls)) is False
    failure = vs.start_failure()
    assert failure is not None and failure.cause == "fit" and calls == []
    now[0] += 30
    assert vs.remembered_failure(REPO, 32768) is failure
    now[0] += vs.FIT_RETRY_SECONDS
    assert vs.remembered_failure(REPO, 32768) is None, "measured again after 60 s"
    monkeypatch.setattr(vs, "_gpu_memory", lambda: (20000, 24576))
    vs.start(REPO, 32768, popen=_counting_popen(calls))
    assert calls, "the memory grew back: it launches again"


def test_interrupted_start_is_not_remembered(home, monkeypatch):
    """A llama.cpp start in this process stops a vLLM that is still loading: that death is no
    failure of the model, so nothing is remembered and nothing is retried."""
    calls = []

    def popen(argv, **kw):
        calls.append(argv)
        proc = _Proc(dies=False)
        return proc

    def wait_ready(proc, port):
        with vs._lock:
            vs._stopped_while_loading.add(proc.pid)
        proc.dies = True
        return "died"
    monkeypatch.setattr(vs, "_wait_ready", wait_ready)
    assert vs.start(REPO, 32768, popen=popen) is False
    assert len(calls) == 1, "an interrupted start is not retried with the fallback"
    assert vs.start_failure() is None and vs._last_interrupted is True
    result = vs.first_start(REPO, 32768, popen=popen)
    assert not result.ok and result.cause == "interrupted"


def test_backend_skips_handover_when_remembered(home, monkeypatch):
    from vaf.core.backend import ServerManager
    calls = []
    vs.start(REPO, 32768, popen=_counting_popen(calls))
    mgr = ServerManager(skip_cleanup=True)
    handed = []
    monkeypatch.setattr(mgr, "hand_over_from_llama", lambda port=8080, wait=15.0: handed.append(1) or "")
    monkeypatch.setattr(vs, "start", lambda repo, n_ctx, port=8080, utilization=None: calls.append("start") or False)
    assert mgr._start_vllm_locked(n_ctx=32768) is False
    assert handed == [], "the llama server was stopped for a start that does not happen"
    vs.clear_start_failure()
    mgr._start_vllm_locked(n_ctx=32768)
    assert handed == [1], "without a remembered failure the handover runs"


def test_turn_refusal_reason_names_vllm_failure(home, monkeypatch):
    from vaf.core import headless_runner
    agent = SimpleNamespace(api_backend=None, provider="local",
                            _no_backend_reason=lambda: "no backend")
    monkeypatch.setattr(vs, "running_pid", lambda: None)
    assert headless_runner.turn_refusal_reason(agent, None) == ""
    vs.start(REPO, 32768, popen=_counting_popen([]))
    why = headless_runner.turn_refusal_reason(agent, None)
    assert why.startswith("vLLM could not start:") and "vaf engine llama" in why
    home["local_engine"] = "llama"
    assert headless_runner.turn_refusal_reason(agent, None) == "", "only under vLLM"


def test_refused_turn_is_told_and_stored_without_a_load(home, monkeypatch):
    """The refusal goes through the existing lane: emitted and stored for a web chat, only
    logged for a messenger turn."""
    from vaf.core import headless_runner
    emitted, saved, logged = [], [], []
    wi = SimpleNamespace(emit_agent_message=lambda role, text, session_id=None: emitted.append(text),
                         emit_message_complete=lambda content="", session_id=None: None)
    monkeypatch.setattr(headless_runner, "get_web_interface", lambda: wi)
    monkeypatch.setattr(headless_runner, "append_domain_log_always", lambda lane, text: logged.append(text))
    session = SimpleNamespace(messages=[], add_message=lambda role, content: session.messages.append((role, content)))
    mgr = SimpleNamespace(load=lambda sid: session, save=lambda s, sync_state=False: saved.append(s))
    vs.start(REPO, 32768, popen=_counting_popen([]))
    why = vs.failure_sentence(vs.start_failure())
    task = SimpleNamespace(source="web", session_id="s1", input_text="hello", metadata={})
    headless_runner.refuse_turn_without_backend(task, why, mgr)
    assert emitted == [why] and session.messages == [("user", "hello"), ("assistant", why)]
    task = SimpleNamespace(source="telegram", session_id="telegram_1", input_text="hi", metadata={})
    headless_runner.refuse_turn_without_backend(task, why, mgr)
    assert len(emitted) == 1 and logged, "a messenger turn is only logged"


def test_tray_start_model_async_skips_remembered_failure(home, monkeypatch):
    """The tray asks before every start; while a failure is remembered under vLLM it starts
    nothing and says so once."""
    import inspect

    from vaf import tray
    monkeypatch.setattr(tray, "_logged_engine_failure", None)
    lines = []
    monkeypatch.setattr(tray, "log", lambda lane, text: lines.append(text))
    assert tray._engine_start_refusal() == ""
    vs.start(REPO, 32768, popen=_counting_popen([]))
    assert tray._engine_start_refusal() and tray._engine_start_refusal()
    assert len(lines) == 1, "said once, not on every tick"
    home["local_engine"] = "llama"
    assert tray._engine_start_refusal() == ""
    # The guard comes before the start in the activity loop's start_model_async.
    body = inspect.getsource(tray.check_activity_loop)
    starter = body[body.index("def start_model_async"):body.index("def emit_model_state")]
    assert starter.index("_engine_start_refusal()") < starter.index("server_mgr.start_server(")



def test_crash_then_fit_refusal_is_not_relaunched(home, monkeypatch):
    """The quantized attempt dies, its 16-bit fallback does not fit: the crash stays
    remembered without a retry time. Remembered as a fit refusal, the dying attempt was
    launched again 61 s later (the loop this memory exists against)."""
    monkeypatch.setattr(vs, "_gpu_memory", lambda: (8000, 10240))
    now = [1000.0]
    monkeypatch.setattr(vs.time, "monotonic", lambda: now[0])
    calls = []
    assert vs.start(REPO, 32768, popen=_counting_popen(calls)) is False
    assert len(calls) == 1, "int8 launched, auto refused by the fit"
    failure = vs.start_failure()
    assert failure.cause == "kernels" and failure.retry_at == 0.0, failure
    now[0] += vs.FIT_RETRY_SECONDS + 1
    assert vs.start(REPO, 32768, popen=_counting_popen(calls)) is False
    assert len(calls) == 1, "the crashing attempt was launched again"


def test_an_interrupt_in_the_ready_wait_stops_the_server(home, monkeypatch):
    """Ctrl+C while vLLM loads: the server leads its own session, so start() stops it before
    the interrupt goes on."""
    stopped = []
    monkeypatch.setattr(vs, "stop", lambda wait=15.0: stopped.append(1) or "")

    def wait_ready(proc, port):
        raise KeyboardInterrupt
    monkeypatch.setattr(vs, "_wait_ready", wait_ready)
    with pytest.raises(KeyboardInterrupt):
        vs.start(REPO, 32768, popen=_counting_popen([], dies=False))
    assert stopped == [1]
    assert vs._loading_pid is None


def test_an_interrupt_in_the_test_request_stops_the_server(home, monkeypatch):
    stopped = []
    monkeypatch.setattr(vs, "stop", lambda wait=15.0: stopped.append(1) or "")
    monkeypatch.setattr(vs, "start", lambda repo, n_ctx, port=8080, popen=None, utilization=None: True)

    def post(url, json=None, timeout=None):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        vs.first_start(REPO, 32768, post=post)
    assert stopped == [1], "the server of the switch was left running"


def test_a_share_override_has_its_own_signature(home, monkeypatch):
    """The first start of a switch runs with the share it is about to write: a failure is
    remembered for THAT share, so it does not block the configured start, and the configured
    one launches with the configured share. MUTATION: ignore the override in start_signature
    - red."""
    assert vs.start_signature(REPO, 32768, utilization=0.7) != vs.start_signature(REPO, 32768)
    assert vs.start_signature(REPO, 32768, utilization=None) == vs.start_signature(REPO, 32768)
    calls = []
    assert vs.start(REPO, 32768, popen=_counting_popen(calls), utilization=0.7) is False
    assert vs.remembered_failure(REPO, 32768, 0.7) is not None
    assert vs.remembered_failure(REPO, 32768) is None, "the override's failure blocked the configured start"
    shares = [argv[argv.index("--gpu-memory-utilization") + 1] for argv in calls]
    assert shares == ["0.70", "0.70"], "the launch did not carry the override"
    vs.start(REPO, 32768, popen=_counting_popen(calls))
    assert len(calls) == 4, "the configured start was not launched"
    assert calls[-1][calls[-1].index("--gpu-memory-utilization") + 1] != "0.70"


def test_a_failed_first_start_is_remembered_for_its_share(home, monkeypatch):
    """A first start whose test request fails is remembered under the override's signature."""
    monkeypatch.setattr(vs, "start", lambda repo, n_ctx, port=8080, popen=None, utilization=None: True)
    monkeypatch.setattr(vs, "stop", lambda wait=15.0: "")
    monkeypatch.setattr(vs, "running_pid", lambda: 4242)
    out = vs.first_start(REPO, 32768, post=lambda url, json=None, timeout=None:
                         SimpleNamespace(status_code=500), utilization=0.68)
    assert out.ok is False
    assert vs.start_failure().signature == vs.start_signature(REPO, 32768, utilization=0.68)
