# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The engine choice at the choke points: the start (dispatch, handover, waiting), the stops,
the model ensure, the agent's load, token count, payload and stream, the API lane's reasoning
field, the voice lane, the health check and the tray's restart hook.

Driven through the real `ServerManager.start_server`, `Agent.chat_step`, `get_token_usage`,
`OpenAIProvider.chat_completion` and `collect_health`, with processes, HTTP and the config
faked. No vLLM, no model, no GPU.

MUTATIONS, each turns the named test red:
- dispatch on the raw value (`Config.get("local_engine") != "llama"`) instead of
  `local_engine.current()` -> test_llama_engine_launch_unchanged;
- skip the wait for the llama pid to exit -> test_handover_llama_to_vllm_order;
- skip the vLLM stop in the llama branch -> test_handover_vllm_to_llama_order;
- remove the "pid alive and not healthy -> wait" branch -> test_second_start_waits_while_loading;
- call `vllm_server.install()` from the start path -> test_vllm_not_installed_refuses_without_install;
- drop the vLLM stop from `stop_server(force_external=True)` or `stop_local_server` ->
  test_every_stop_releases_vllm;
- revert one of the agent's tokenize sites to the inline `{"content"}` post ->
  test_agent_token_usage_counts_vllm_tokens (two sites) / test_no_hand_copy_of_the_tokenize_call;
- revert the stream site to `delta.get('reasoning_content')` -> test_server_stream_shows_reasoning_field;
- drop the `provider_name == "local"` gate -> test_openai_provider_reasoning_only_for_local;
- drop the engine branch in `voice_agent._resolve_backend` -> test_voice_local_rides_vllm_model;
- return ok when vLLM is not installed -> test_health_engine_check_states;
- drop `vllm_model` from ENGINE_KEYS, or the engine filter in the tray hook ->
  test_tray_restart_keys_cover_engine_keys / test_the_tray_restarts_only_for_the_running_engine;
- poll only the network keys again -> test_the_tray_poll_hands_an_engine_switch_on;
- take the identity from the GGUF setting under vLLM -> test_agent_identity_follows_the_engine;
- let a local voice lane with an API main provider resolve under vLLM ->
  test_voice_local_with_an_api_main_is_refused_under_vllm;
- "restart" an unmanaged vLLM serving another model, or stop reusing one that serves the
  configured model -> test_an_unmanaged_server_is_reused_for_the_configured_model_refused_otherwise;
- skip the VRAM settle in the handover -> test_handover_llama_to_vllm_order.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import vaf.core.backend as backend
from vaf.core import local_engine as le
from vaf.core import vllm_server as vs

ROOT = Path(__file__).resolve().parents[1]
REPO = "Qwen/Qwen3-4B-AWQ"
_ABSENT = object()


@pytest.fixture
def values(monkeypatch, tmp_path):
    """The config, the vLLM pid file and the default model, all inside the test."""
    from vaf.core.config import Config
    vals = {"provider": "local", "auto_start_local_server": True, "n_parallel": 1,
            "llama_cache_ram": 4096, "vllm_model": REPO}
    monkeypatch.setattr(Config, "get", staticmethod(lambda key, default=None: vals.get(key, default)))
    monkeypatch.setattr(Config, "set", staticmethod(lambda key, value: vals.__setitem__(key, value)))
    monkeypatch.setattr(Config, "load", staticmethod(lambda: dict(vals)))
    monkeypatch.setattr(vs, "pid_file", lambda: tmp_path / "vllm.pid")
    monkeypatch.setattr(backend, "reap_abandoned_children", lambda: 0)
    monkeypatch.setattr(backend.time, "sleep", lambda s: None)
    monkeypatch.setattr(backend, "_wait_port_free", lambda port, tries=20: None)
    return vals


class _Resp:
    def __init__(self, status, data=None):
        self.status_code, self._data, self.text = status, data or {}, json.dumps(data or {})

    def json(self):
        return self._data


class _Proc:
    pid = 31337

    def poll(self):
        return None


def _manager(tmp_path):
    mgr = backend.ServerManager(skip_cleanup=True)
    mgr.pid_file = str(tmp_path / "server.pid")
    return mgr


# ── llama.cpp stays as it was ──────────────────────────────────────────────────────

def _llama_launch(monkeypatch, tmp_path, values, engine):
    if engine is not _ABSENT:
        values["local_engine"] = engine
    from vaf.core import gpu_detection
    mgr = _manager(tmp_path)
    model = tmp_path / "m.gguf"
    model.write_bytes(b"\0" * 1024)
    monkeypatch.setattr(backend.ServerManager, "ensure_server_exists", lambda self: True)
    monkeypatch.setattr(backend, "resolve_mmproj_for", lambda path: "")
    monkeypatch.setattr(gpu_detection, "get_primary_gpu", lambda: None)
    monkeypatch.setattr(backend, "is_debug_logging_enabled", lambda: False)
    monkeypatch.setattr(backend, "get_app_log_dir", lambda: tmp_path / "logs")
    spawned = []

    def popen(argv, **kw):
        spawned.append(list(argv))
        return _Proc()

    def get(url, timeout=None):
        if not spawned:
            raise requests.ConnectionError("nothing on the port yet")
        return _Resp(200, {"total_slots": 1})
    monkeypatch.setattr(backend.subprocess, "Popen", popen)
    monkeypatch.setattr(backend.requests, "get", get)
    assert mgr.start_server(str(model)) is True
    assert len(spawned) == 1
    return spawned[0], mgr, str(model)


@pytest.mark.parametrize("engine", [_ABSENT, "llama", "", "LLama ", "vlm", None])
def test_llama_engine_launch_unchanged(monkeypatch, tmp_path, values, engine):
    """Whatever the key says unless it says vllm, llama-server starts with the arguments it
    always had."""
    argv, mgr, model = _llama_launch(monkeypatch, tmp_path, values, engine)
    assert argv == [mgr.server_path, "-m", model, "-ngl", "99", "-c", "32768", "--port", "8080",
                    "--host", "127.0.0.1", "--parallel", "1", "-np", "1", "-kvu", "--no-webui",
                    "-ctk", "q8_0", "-ctv", "q4_0", "--jinja", "--log-verbosity", "1",
                    "--cache-ram", "4096"]


def test_no_engine_row_under_llama(values):
    from vaf.core.health import collect_health
    out = collect_health(status_probe=lambda: {}, worker_probe=lambda: None,
                         usage=lambda p: SimpleNamespace(free=100 * 1024 ** 3),
                         backup_probe=lambda: {"name": "backup", "status": "ok", "reason": ""})
    assert [c["name"] for c in out["checks"]] == ["services", "disk", "backup"]


# ── the vLLM branch of the start ───────────────────────────────────────────────────

def _vllm_branch(monkeypatch, values, events, *, installed=True, llama_pid=None):
    values["local_engine"] = "vllm"
    monkeypatch.setattr(vs, "is_installed", lambda: installed)
    monkeypatch.setattr(backend.ServerManager, "_llama_pid", lambda self: llama_pid)

    def no_llama(self):
        events.append("ensure-llama-binary")
        raise AssertionError("the llama binary was touched under vLLM")
    monkeypatch.setattr(backend.ServerManager, "ensure_server_exists", no_llama)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda *a, **k: events.append("spawn-llama") or _Proc())

    def start(repo, n_ctx, port=8080, popen=None):
        events.append(("spawn-vllm", repo, n_ctx, port))
        return True
    monkeypatch.setattr(vs, "start", start)


def test_vllm_dispatch_never_touches_llama(monkeypatch, tmp_path, values):
    events = []
    _vllm_branch(monkeypatch, values, events)
    monkeypatch.setattr(vs, "running_pid", lambda: None)
    monkeypatch.setattr(vs, "health_status", lambda port=8080, timeout=1.0: None)
    assert _manager(tmp_path).start_server("/models/ignored.gguf", n_ctx=8192) is True
    assert events == [("spawn-vllm", REPO, 32768, 8080)]


def test_handover_llama_to_vllm_order(monkeypatch, tmp_path, values):
    """The llama server is stopped AND gone before vLLM is spawned."""
    events = []
    _vllm_branch(monkeypatch, values, events, llama_pid=4242)
    monkeypatch.setattr(vs, "running_pid", lambda: None)
    monkeypatch.setattr(vs, "health_status", lambda port=8080, timeout=1.0: None)
    state = {"stopped": False, "checks": 0}

    def stop_llama(self, force_external=False):
        events.append("stop-llama")
        state["stopped"] = True
    monkeypatch.setattr(backend.ServerManager, "_stop_llama", stop_llama)

    def looks_like_llama(self, pid):
        if not state["stopped"]:
            return True
        state["checks"] += 1
        if state["checks"] < 3:
            return True              # still exiting
        if "llama-exit" not in events:
            events.append("llama-exit")
        return False
    monkeypatch.setattr(backend.ServerManager, "_looks_like_llama_server", looks_like_llama)
    monkeypatch.setattr(backend.time, "sleep", lambda sec: events.append(("settle", sec))
                        if sec == backend._VRAM_SETTLE_SECONDS else None)
    assert _manager(tmp_path).start_server("/m.gguf") is True
    assert events == ["stop-llama", "llama-exit", ("settle", 1.5),
                      ("spawn-vllm", REPO, 32768, 8080)], "the GPU memory was read before it settled"


def test_handover_vllm_to_llama_order(monkeypatch, tmp_path, values):
    """Back to llama.cpp: the vLLM on the port is stopped first, even when its model id is
    unreadable (which the llama reuse check would count as a match)."""
    values["local_engine"] = "llama"
    from vaf.core import gpu_detection
    events = []
    alive = {"vllm": True}
    monkeypatch.setattr(vs, "running_pid", lambda: 777 if alive["vllm"] else None)

    def stop(wait=15.0):
        events.append("stop-vllm")
        alive["vllm"] = False
        events.append("vllm-exit")
        return ""
    monkeypatch.setattr(vs, "stop", stop)
    monkeypatch.setattr(backend.ServerManager, "ensure_server_exists", lambda self: True)
    monkeypatch.setattr(backend, "resolve_mmproj_for", lambda path: "")
    monkeypatch.setattr(gpu_detection, "get_primary_gpu", lambda: None)
    monkeypatch.setattr(backend, "is_debug_logging_enabled", lambda: False)
    monkeypatch.setattr(backend, "get_app_log_dir", lambda: tmp_path / "logs")
    spawned = []

    def popen(argv, **kw):
        assert not alive["vllm"], "llama-server spawned while vLLM still runs"
        events.append("spawn-llama")
        spawned.append(argv)
        return _Proc()

    def get(url, timeout=None):
        if url.endswith("/v1/models"):
            raise requests.ConnectionError("model id unreadable")
        if alive["vllm"] or spawned:
            return _Resp(200, {"total_slots": 1})
        raise requests.ConnectionError("port free")
    monkeypatch.setattr(backend.subprocess, "Popen", popen)
    monkeypatch.setattr(backend.requests, "get", get)
    model = tmp_path / "m.gguf"
    model.write_bytes(b"\0")
    assert _manager(tmp_path).start_server(str(model)) is True
    assert events == ["stop-vllm", "vllm-exit", "spawn-llama"]


def test_second_start_waits_while_loading(monkeypatch, tmp_path, values):
    """vLLM sets its engine up for minutes before /health answers: a second start waits for
    it instead of spawning a second server."""
    events = []
    _vllm_branch(monkeypatch, values, events)
    monkeypatch.setattr(vs, "running_pid", lambda: 555)
    replies = [None, None, None, 200]
    monkeypatch.setattr(vs, "health_status",
                        lambda port=8080, timeout=1.0: replies.pop(0) if replies else 200)
    monkeypatch.setattr(vs, "served_model", lambda port=8080: REPO)
    monkeypatch.setattr(backend, "_server_ready_budget", lambda: 60.0)
    monkeypatch.setattr(vs, "stop", lambda wait=15.0: events.append("stop-vllm") or "")
    assert _manager(tmp_path).start_server("/m.gguf") is True
    assert events == [], f"a loading vLLM was not waited for: {events}"


def test_a_healthy_vllm_with_another_model_is_restarted(monkeypatch, tmp_path, values):
    events = []
    _vllm_branch(monkeypatch, values, events)
    monkeypatch.setattr(vs, "running_pid", lambda: 555)
    monkeypatch.setattr(vs, "health_status", lambda port=8080, timeout=1.0: 200)
    monkeypatch.setattr(vs, "served_model", lambda port=8080: "Qwen/Qwen3-8B-AWQ")
    monkeypatch.setattr(vs, "stop", lambda wait=15.0: events.append("stop-vllm") or "")
    assert _manager(tmp_path).start_server("/m.gguf") is True
    assert events == ["stop-vllm", ("spawn-vllm", REPO, 32768, 8080)]
    events.clear()
    monkeypatch.setattr(vs, "served_model", lambda port=8080: REPO)
    assert _manager(tmp_path).start_server("/m.gguf") is True
    assert events == [], "the configured model is reused"


def test_vllm_not_installed_refuses_without_install(monkeypatch, tmp_path, values):
    events = []
    _vllm_branch(monkeypatch, values, events, installed=False)
    for name in ("install", "ensure_model"):
        monkeypatch.setattr(vs, name, lambda *a, _n=name, **k: events.append(_n) or "")
    errors = []
    monkeypatch.setattr(backend.UI, "error", staticmethod(lambda text, *a, **k: errors.append(text)))
    assert _manager(tmp_path).start_server("/m.gguf") is False
    assert events == [], f"the start installed or downloaded: {events}"
    assert errors and "vaf engine vllm" in errors[-1]


def test_api_profile_refuses_the_vllm_branch(monkeypatch, tmp_path, values):
    events = []
    _vllm_branch(monkeypatch, values, events)
    values["inference_profile"] = "api"
    assert _manager(tmp_path).start_server("/m.gguf", skip_provider_gate=True) is False
    assert events == []


# ── stops, model ensure, swap ──────────────────────────────────────────────────────

def test_every_stop_releases_vllm(monkeypatch, tmp_path, values):
    stops = []
    monkeypatch.setattr(vs, "running_pid", lambda: 999)
    monkeypatch.setattr(vs, "stop", lambda wait=15.0: stops.append("vllm") or "")
    mgr = _manager(tmp_path)
    mgr.stop_server()
    assert stops == [], "a plain stop leaves a vLLM another process started"
    mgr.stop_server(force_external=True)
    assert stops == ["vllm"]
    mgr._vllm_started = True
    mgr.stop_server()
    assert stops == ["vllm", "vllm"], "what this manager started, it stops"
    monkeypatch.setattr(backend.ServerManager, "stop_server", lambda self, force_external=False: None)
    import os
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)
    assert backend.stop_local_server() == ""
    assert stops == ["vllm", "vllm", "vllm"]


def test_ensure_model_present_follows_the_engine(monkeypatch, tmp_path, values):
    calls = []
    monkeypatch.setattr(backend, "ensure_model_available",
                        lambda name, d: calls.append(("gguf", name)) or "/m.gguf")
    monkeypatch.setattr(vs, "ensure_model", lambda repo: calls.append(("hf", repo)) or "/hf/snap")
    mgr = _manager(tmp_path)
    assert mgr.ensure_model_present("x.gguf") == "/m.gguf"
    values["local_engine"] = "vllm"
    monkeypatch.setattr(vs, "is_installed", lambda: False)
    assert mgr.ensure_model_present("x.gguf") == "", "no model for an engine that cannot run"
    monkeypatch.setattr(vs, "is_installed", lambda: True)
    assert mgr.ensure_model_present("x.gguf") == "/hf/snap"
    assert calls == [("gguf", "x.gguf"), ("hf", REPO)]


def test_swap_and_match_under_vllm(monkeypatch, tmp_path, values):
    values["local_engine"] = "vllm"
    monkeypatch.setattr(vs, "served_model", lambda port=8080: REPO)
    assert backend._loaded_model_matches("/models/main.gguf") is True
    monkeypatch.setattr(vs, "served_model", lambda port=8080: "other/model")
    assert backend._loaded_model_matches("/models/main.gguf") is False
    started = []
    monkeypatch.setattr(backend.ServerManager, "start_server",
                        lambda self, path, **kw: started.append((path, kw)) or True)
    assert backend.ensure_local_model("", reason="main-agent task") is True
    assert started and started[0][1]["n_ctx"] >= 32768


# ── the agent ──────────────────────────────────────────────────────────────────────

def _bare_agent(history, tools=None):
    from vaf.core.agent import Agent
    bare = type("_BareAgent", (Agent,), {"TOOLS": tools or []})   # TOOLS is a property
    a = bare.__new__(bare)
    a.history = history
    a.tools = {}
    a.config = {"n_ctx": 32768}
    a.api_backend = None
    a.use_server = True
    return a


def test_agent_token_usage_counts_vllm_tokens(monkeypatch, values):
    values["local_engine"] = "vllm"
    sent = []

    def post(url, json=None, timeout=None):
        sent.append(json)
        if url.endswith("/tokenize") and "prompt" in json:
            return _Resp(200, {"count": len(json["prompt"]), "tokens": []})
        return _Resp(404, {})
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests, "get", lambda url, timeout=None: _Resp(200, {"data": [
        {"id": REPO, "owned_by": "vllm", "max_model_len": 24576}]}))
    monkeypatch.setattr(vs, "running_pid", lambda: 1)
    le._window_cache.clear()
    tools = [{"type": "function", "function": {"name": "web_search"}}]
    agent = _bare_agent([{"role": "system", "content": "abc"}, {"role": "user", "content": "hallo"}],
                        tools)
    total, window = agent.get_token_usage()
    expected = (3 + len("system")) + (5 + len("user")) + 2 * 5 + len(json.dumps(tools)) + 100
    assert total == expected, "not the sum of the server's counts"
    assert window == 24576, "the window is what vLLM serves"
    assert all("content" not in body for body in sent), "a llama-shaped request reached vLLM"
    le._window_cache.clear()


def test_agent_token_usage_estimates_when_the_server_cannot_count(monkeypatch, values):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(404, {}))
    agent = _bare_agent([{"role": "user", "content": "hallo " * 200}])
    monkeypatch.setattr(agent, "_estimate_token_usage", lambda: (777, 32768), raising=False)
    assert agent.get_token_usage() == (777, 32768), "a failed count is an estimate, not 5 + 100"


def test_no_hand_copy_of_the_tokenize_call():
    """The /tokenize contract differs per engine; it lives in local_engine only."""
    for rel in ("vaf/core/agent.py",):
        text = (ROOT / rel).read_bytes().decode("utf-8")
        assert "8080/tokenize" not in text, rel


def _local_agent(monkeypatch, tmp_path):
    from vaf.core.platform import Platform
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    from vaf.core.agent import Agent
    a = Agent(register_signals=False, run_kind="chat", config_overrides={"provider": "local"})
    a.current_session_id = "green123456"
    a.api_backend = None
    a.use_server = True
    monkeypatch.setattr(a, "_try_workflow", lambda *args, **kw: None)
    monkeypatch.setattr(a, "_route_tools", lambda text: ["web_search"])
    monkeypatch.setattr(a, "get_token_usage", lambda: (1000, 32768))
    monkeypatch.setattr(a, "manage_context", lambda *args, **kw: None)
    monkeypatch.setattr(a, "analyze_intent", lambda *args, **kw: 0.7)
    return a


class _Stream:
    status_code = 200
    text = ""

    def __init__(self, deltas):
        self._lines = [b"data: " + json.dumps({"choices": [{"delta": d}]}).encode() for d in deltas]
        self._lines.append(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}')
        self._lines.append(b"data: [DONE]")

    def iter_lines(self):
        return iter(self._lines)

    def close(self):
        pass


def _turn(monkeypatch, agent, deltas):
    payloads, chunks = [], []

    def post(url, json=None, **kw):
        if isinstance(json, dict) and json.get("stream") and "messages" in json:
            payloads.append(json)
            return _Stream(deltas)
        return _Resp(500, {})
    monkeypatch.setattr(requests, "post", post)
    agent.chat_step(user_input="Hallo", raw_user_input="Hallo", stream_callback=chunks.append)
    return payloads, "".join(c for c in chunks if isinstance(c, str))


@pytest.mark.parametrize("engine", ["llama", "vllm"])
def test_server_stream_shows_reasoning_field(monkeypatch, tmp_path, engine):
    """vLLM streams the thinking as `reasoning`; the chat shows it as thinking, under both
    engines. The payload carries vLLM's name for the repetition penalty only under vLLM."""
    from vaf.core.config import Config
    real = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: engine if key == "local_engine" else real(key, default)))
    agent = _local_agent(monkeypatch, tmp_path)
    payloads, shown = _turn(monkeypatch, agent, [{"reasoning": "Ich denke nach."},
                                                 {"content": "Hallo zurueck."}])
    assert payloads, "the turn never reached the local server"
    assert "<think>" in shown and "Ich denke nach." in shown, shown
    assert "Hallo zurueck." in shown
    if engine == "vllm":
        assert payloads[0]["repetition_penalty"] == payloads[0]["repeat_penalty"]
    else:
        assert "repetition_penalty" not in payloads[0]


def _chunk(**delta):
    fields = {"reasoning_content": None, "reasoning": None, "content": None, "tool_calls": None}
    fields.update(delta)
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(**fields),
                                                    finish_reason=None)], usage=None)


@pytest.mark.parametrize("provider, shown", [("local", True), ("openrouter", False)])
def test_openai_provider_reasoning_only_for_local(monkeypatch, provider, shown):
    from vaf.core.api_backend import OpenAIProvider
    p = OpenAIProvider(provider, "sk-test", base_url="http://127.0.0.1:9/v1")
    monkeypatch.setattr(p, "_create_with_retry",
                        lambda kwargs: iter([_chunk(reasoning="gedacht"), _chunk(content="Antwort")]))
    out = "".join(x for x in p.chat_completion([{"role": "user", "content": "x"}], 0.7, 100, True,
                                               "m", None))
    assert ("<think>" in out) is shown and ("gedacht" in out) is shown
    assert "Antwort" in out
    message = SimpleNamespace(reasoning_content=None, reasoning="gedacht", content="Antwort",
                              tool_calls=None)
    monkeypatch.setattr(p, "_create_with_retry", lambda kwargs: SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None))
    out = "".join(x for x in p.chat_completion([{"role": "user", "content": "x"}], 0.7, 100, False,
                                               "m", None) if isinstance(x, str))
    assert ("gedacht" in out) is shown


def test_load_model_follows_the_engine(monkeypatch, values):
    from vaf.core.agent import Agent

    class _Stop(Exception):
        pass

    def gguf(self):
        raise _Stop()
    monkeypatch.setattr(Agent, "ensure_model_exists", gguf)
    agent = Agent.__new__(Agent)
    agent.provider = "local"
    agent.config = {"n_ctx": 32768, "gpu_layers": 99}
    agent.use_server = False
    with pytest.raises(_Stop):
        agent.load_model()               # llama.cpp: the GGUF is ensured first, as before
    values["local_engine"] = "vllm"
    calls = []
    monkeypatch.setattr(backend.ServerManager, "ensure_model_present",
                        lambda self, name=None: calls.append("model") or "/hf/snap")
    monkeypatch.setattr(backend.ServerManager, "start_server",
                        lambda self, path, **kw: calls.append(("start", path, kw["n_ctx"])) or True)
    agent.load_model()
    assert calls == ["model", ("start", "/hf/snap", 32768)]
    assert agent.use_server is True and getattr(agent, "llm", None) is None


# ── voice, health, tray ────────────────────────────────────────────────────────────

def test_voice_local_rides_vllm_model(values):
    from vaf.core import voice_agent, voice_model
    values["voice_agent_provider"] = "local"
    assert voice_agent._resolve_backend() == ("local", voice_model.voice_model_ref())
    values["local_engine"] = "vllm"
    assert voice_agent._resolve_backend() == ("local", None)
    assert voice_agent.dedicated_local_model() is None, "no GGUF swap under vLLM"


def test_health_engine_check_states(monkeypatch, values):
    from vaf.core import health
    assert health.engine_check() is None
    values["local_engine"] = "vllm"
    monkeypatch.setattr(le, "support", lambda **kw: le.EngineSupport(le.LATER, "Windows here."))
    row = health.engine_check()
    assert row["status"] == health.DEGRADED and "Windows here." in row["reason"]
    monkeypatch.setattr(le, "support", lambda **kw: le.EngineSupport(le.SUPPORTED, "ok"))
    monkeypatch.setattr(vs, "installed_version", lambda: None)
    row = health.engine_check()
    assert row["status"] == health.DEGRADED and "vaf engine vllm" in row["reason"]
    monkeypatch.setattr(vs, "installed_version", lambda: "0.32.0")
    monkeypatch.setattr(vs, "running_pid", lambda: None)
    row = health.engine_check()
    assert row["status"] == health.OK and "idle" in row["reason"] and REPO in row["reason"]
    monkeypatch.setattr(vs, "running_pid", lambda: 12)
    assert "running" in health.engine_check()["reason"]
    out = health.collect_health(status_probe=lambda: {}, worker_probe=lambda: None,
                                usage=lambda p: SimpleNamespace(free=100 * 1024 ** 3),
                                backup_probe=lambda: {"name": "backup", "status": "ok", "reason": ""})
    assert [c["name"] for c in out["checks"]][-1] == "engine"


def test_tray_restart_keys_cover_engine_keys():
    from vaf.core.config import Config
    engine_keys = {"local_engine"} | {k for k in Config.DEFAULTS if k.startswith("vllm_")}
    assert engine_keys <= set(le.ENGINE_KEYS) <= set(le.RESTART_KEYS)
    assert {"model", "n_ctx", "gpu_layers"} <= set(le.RESTART_KEYS)
    source = (ROOT / "vaf" / "tray.py").read_bytes().decode("utf-8")
    assert "key in local_engine.RESTART_KEYS and local_engine.restarts_running_engine(key)" in source
    assert "*local_engine.ENGINE_KEYS)" in source, "the file poll does not watch the engine keys"


def test_the_tray_restarts_only_for_the_running_engine(monkeypatch, values):
    import vaf.tray as tray
    scheduled = []

    class _Thread:
        def __init__(self, target=None, daemon=None, name=None, **kw):
            self.target = target

        def start(self):
            scheduled.append(self.target.__name__)
    monkeypatch.setattr(tray.threading, "Thread", _Thread)
    values["local_engine"] = "vllm"
    tray.on_config_changed("model", "x.gguf")
    tray.on_config_changed("gpu_layers", 10)
    assert scheduled == [], "a GGUF setting restarted vLLM"
    tray.on_config_changed("vllm_model", "Qwen/Qwen3-8B-AWQ")
    tray.on_config_changed("local_engine", "vllm")
    assert scheduled == ["_restart_llama", "_restart_llama"]
    assert tray._last_network_config["vllm_model"] == "Qwen/Qwen3-8B-AWQ", \
        "the file poll would report this change a second time"


def test_the_one_shot_lanes_read_vllms_reasoning_field(monkeypatch, values):
    """The shared completion and the voice call read the reply's reasoning under its vLLM name
    too (an answer cut off mid-think, a voice turn's thinking)."""
    from vaf.core import completion, voice_agent
    reply = {"choices": [{"message": {"content": "", "reasoning": "nur gedacht"},
                          "finish_reason": "length"}]}
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, reply))
    assert completion._local_complete([{"role": "user", "content": "x"}], None, 50, 0.2, 5,
                                      True, "test") == "nur gedacht"
    reply["choices"][0]["message"]["content"] = "Antwort"
    assert voice_agent._local_chat([{"role": "user", "content": "x"}]) == \
        "<think>nur gedacht</think>Antwort"


def test_the_coder_reads_vllms_reasoning_field_only_locally():
    """The coder's run loop cannot be driven in a unit test; its two response reads are pinned
    to the shared reader, gated on the local endpoint (OpenRouter's `reasoning` is its own)."""
    source = (ROOT / "vaf" / "tools" / "coder.py").read_bytes().decode("utf-8")
    assert "reasoning_text(_vr_msg, local=not _is_api_mode)" in source
    assert "reasoning_text(delta, local=not _is_api_mode)" in source
    assert "delta.get('reasoning_content', '')" not in source



# ── round 1 ────────────────────────────────────────────────────────────────────────

def test_agent_identity_follows_the_engine(values):
    """A Gemma GGUF setting must not turn Gemma handling on for the Qwen vLLM serves."""
    from vaf.core.agent import Agent
    agent = Agent.__new__(Agent)
    agent.filename = "gemma-4-E4B-it-Q8_0.gguf"
    agent._apply_local_model_identity()
    assert agent.model_mode == "gemma4" and agent.is_gemma_local is True
    values["local_engine"] = "vllm"
    agent._apply_local_model_identity()
    assert agent.model_mode is None and agent.is_gemma_local is False
    assert agent.model_display_name == "Qwen"


def test_load_model_under_vllm_sets_the_identity(monkeypatch, values):
    from vaf.core.agent import Agent
    values["local_engine"] = "vllm"
    monkeypatch.setattr(backend.ServerManager, "ensure_model_present", lambda self, name=None: "/hf")
    monkeypatch.setattr(backend.ServerManager, "start_server", lambda self, path, **kw: True)
    agent = Agent.__new__(Agent)
    agent.provider = "local"
    agent.config = {"n_ctx": 32768}
    agent.filename = "gemma-4-E4B-it-Q8_0.gguf"
    agent.model_mode, agent.is_gemma_local = "gemma4", True       # as construction left it
    agent.load_model()
    assert agent.model_mode is None and agent.is_gemma_local is False


def test_voice_local_with_an_api_main_is_refused_under_vllm(monkeypatch, values):
    """Nothing starts vLLM for a call while the chat runs on an API: the lane says it has no
    model instead of reporting one that is not there."""
    from vaf.core import voice_agent
    values.update({"local_engine": "vllm", "voice_agent_provider": "local", "provider": "openai"})
    assert voice_agent._resolve_backend() == (None, None)

    def no_probe(*a, **k):
        raise AssertionError("probed a server for an unsupported lane")
    monkeypatch.setattr(requests, "get", no_probe)
    assert voice_agent.available() is False
    values["provider"] = "local"
    assert voice_agent._resolve_backend() == ("local", None)


def test_an_unmanaged_server_is_reused_for_the_configured_model_refused_otherwise(
        monkeypatch, tmp_path, values):
    """A server VAF did not start: serving another model it is refused (not VAF's to stop,
    and a launch would only die at bind); serving the configured model it is reused, as the
    llama engine reuses a healthy server holding the wanted model. Never stopped."""
    events = []
    _vllm_branch(monkeypatch, values, events)
    monkeypatch.setattr(vs, "running_pid", lambda: None)
    monkeypatch.setattr(vs, "health_status", lambda port=8080, timeout=1.0: 200)
    monkeypatch.setattr(vs, "served_model", lambda port=8080: "someone/other-model")
    monkeypatch.setattr(vs, "stop", lambda wait=15.0: events.append("stop-vllm") or "")
    errors = []
    monkeypatch.setattr(backend.UI, "error", staticmethod(lambda text, *a, **k: errors.append(text)))
    assert _manager(tmp_path).start_server("/m.gguf") is False
    assert events == [] and "someone/other-model" in errors[-1]
    monkeypatch.setattr(vs, "served_model", lambda port=8080: REPO)
    assert _manager(tmp_path).start_server("/m.gguf") is True, "the configured model is reused"
    assert events == [], "a server VAF did not start was stopped or replaced"


def test_the_tray_poll_hands_an_engine_switch_on(monkeypatch, values):
    """`vaf engine vllm` in a terminal writes the key; the running tray's poll hands it on."""
    import vaf.tray as tray
    from vaf.core.config import Config
    seen = []

    class _Stop(BaseException):
        pass
    sleeps = []

    def sleep(sec):
        sleeps.append(sec)
        if len(sleeps) > 1:
            raise _Stop()
    monkeypatch.setattr(tray.time, "sleep", sleep)
    monkeypatch.setattr(tray, "on_config_changed", lambda k, new, old: seen.append((k, new, old)))
    values["local_engine"] = "llama"
    tray._seed_network_config()
    values["local_engine"] = "vllm"
    monkeypatch.setattr(Config, "load", staticmethod(lambda: dict(values)))
    with pytest.raises(_Stop):
        tray._config_file_poll_loop()
    assert ("local_engine", "vllm", "llama") in seen


# ── round 2 ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("engine", ["llama", "vllm"])
def test_every_prompt_build_names_the_running_model(monkeypatch, tmp_path, engine):
    """Both prompt builds (the chat's first and the per-turn rebuild) name the model that
    runs: the vLLM repo under vLLM, never the GGUF setting, which names a model that is not
    running there."""
    from vaf.core.config import Config
    from vaf.core.system_prompt import SystemPromptManager
    real = Config.get
    overrides = {"local_engine": engine, "vllm_model": REPO}
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: overrides[key] if key in overrides else real(key, default)))
    agent = _local_agent(monkeypatch, tmp_path)
    named = []
    real_build = SystemPromptManager.build_prompt

    def build_prompt(self, filename=None, *args, **kw):
        named.append(filename)
        return real_build(self, filename, *args, **kw)
    monkeypatch.setattr(SystemPromptManager, "build_prompt", build_prompt)
    agent.init_chat()
    first = len(named)
    _turn(monkeypatch, agent, [{"content": "Hallo zurueck."}])
    assert first >= 1 and len(named) > first, f"a prompt build was not reached: {named}"
    wanted = REPO if engine == "vllm" else agent.filename
    assert named and all(n == wanted for n in named), named
