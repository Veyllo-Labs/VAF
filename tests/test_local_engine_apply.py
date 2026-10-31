# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The switch of the local engine, `local_engine.apply`: the one implementation behind `vaf
engine`, Settings > AI & Model and the full-screen TUI.

The install, the download, the fit, the llama handover and the GPU are faked; nothing is
installed or downloaded. The config is a dict.

MUTATIONS, each turns the named test red:
- install without `install=True` -> test_needs_install_changes_nothing;
- write local_engine before plan_fit -> test_switch_order;
- drop the profile check in `_validated` -> test_refusals_before_install[api];
- make the switch lock blocking (`lock.acquire()`) -> test_busy_lock (hangs into its timeout
  guard and fails);
- write the engine before the download -> test_failed_steps_keep_engine;
- drop `stopped_llama` from the no-fit result -> test_no_fit_reports_stopped_llama;
- skip `vllm_server.stop` in the llama branch -> test_back_to_llama_stops_vllm;
- remove the active check in `uninstall_vllm` -> test_uninstall_refused_while_active;
- report a fixed `total`, or leave MAINTENANCE active -> test_maintenance_steps;
- drop `models` from the GGUF closing frame -> test_gguf_done_frame_unchanged;
- ignore `cancel_requested` in the progress hook -> test_cancelled_download;
- remove the vLLM skip in `reload_local_model` -> test_reload_under_vllm_downloads_no_gguf;
- refresh a pinned or embedded agent -> test_refresh_identity_all.
"""
import threading
import time
from types import SimpleNamespace

import pytest

from vaf.core import local_engine as le
from vaf.core import vllm_server as vs
from vaf.core.maintenance_state import MAINTENANCE
from vaf.core.model_download_state import MODEL_DOWNLOAD

REPO = "Qwen/Qwen3-4B-AWQ"
REAL_ENSURE_MODEL = vs.ensure_model


@pytest.fixture
def config(monkeypatch, tmp_path):
    from vaf.core.config import Config
    values = {"vllm_model": REPO}
    events = []
    real = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: values[key] if key in values else real(key, default)))

    def _set(key, value):
        events.append(("set", key, value))
        values[key] = value
    monkeypatch.setattr(Config, "set", staticmethod(_set))
    monkeypatch.setattr(Config, "load", staticmethod(lambda: dict(values)))
    monkeypatch.setattr(vs, "pid_file", lambda: tmp_path / "vllm.pid")
    monkeypatch.setattr(vs, "venv_dir", lambda: tmp_path / "bin" / "vllm" / "venv")
    monkeypatch.setattr(le, "support", lambda **kw: le.EngineSupport(
        le.SUPPORTED, "vLLM runs here.", "supported", {"gpu": "RTX 3080", "vram_gb": "10"}))
    monkeypatch.setattr(le, "_stop_llama", lambda say: events.append(("handover",)) or (False, ""))
    MAINTENANCE.clear()
    values["_events"] = events
    yield values
    MAINTENANCE.clear()


def _fakes(monkeypatch, config, *, installed=False, install_reason="", fail_step="",
           model_dir="/hf/snap", fits=True):
    events = config["_events"]
    state = {"installed": installed}
    monkeypatch.setattr(vs, "is_installed", lambda: state["installed"])
    monkeypatch.setattr(vs, "installed_version", lambda: vs.PIN if state["installed"] else None)

    def install(log=None, run=None, *, output=None, on_step=None):
        events.append(("install", output))
        for code in vs.INSTALL_STEPS:
            if on_step:
                on_step(code)
            if code == fail_step:
                return install_reason
        state["installed"] = True
        return ""
    monkeypatch.setattr(vs, "install", install)
    monkeypatch.setattr(vs, "ensure_model", lambda repo: events.append(("model", repo)) or model_dir)

    def plan_fit(d, n, kv_dtype=None):
        events.append(("fit",))
        return vs.FitResult(fits, n, 0.78, "fp8", 6076 if fits else 9000, 7987,
                            "Fits." if fits else "Needs 9000 MiB.", code="fits" if fits else "too_big")
    monkeypatch.setattr(vs, "plan_fit", plan_fit)
    return events


def test_needs_install_changes_nothing(config, monkeypatch):
    events = _fakes(monkeypatch, config)
    result = le.apply("vllm")
    assert result.status == "needs_install" and result.code == "needs_install"
    assert "1.8 GB" in result.text and result.params["pin"] == vs.PIN
    assert events == [], "nothing installs, downloads, stops or writes without install=True"
    assert not MAINTENANCE.active


def test_switch_order(config, monkeypatch):
    events = _fakes(monkeypatch, config)
    result = le.apply("vllm", model="Qwen/Qwen3-8B-AWQ", install=True, output="/tmp/x.log")
    assert result.status == "done" and result.code == "switched_vllm", result
    names = [e[0] if e[0] != "set" else f"set:{e[1]}" for e in events]
    assert names == ["install", "model", "handover", "fit", "set:vllm_model", "set:local_engine"]
    assert events[0][1] == "/tmp/x.log", "the pip output goes to the file"
    assert config["local_engine"] == "vllm" and config["vllm_model"] == "Qwen/Qwen3-8B-AWQ"
    assert result.fit.code == "fits" and result.to_dict()["fit"]["need_mb"] == 6076
    # "auto" goes back to the automatic choice: stored as auto, the VRAM default runs.
    monkeypatch.setattr(le, "default_vllm_model", lambda vram_gb=None: REPO)
    events.clear()
    result = le.apply("vllm", model="auto")
    assert result.status == "done" and result.code == "unchanged_vllm"
    assert config["vllm_model"] == "auto" and ("model", REPO) in events


@pytest.mark.parametrize("case", ["api", "unsupported", "bad_repo", "parser_unknown"])
def test_refusals_before_install(config, monkeypatch, case):
    events = _fakes(monkeypatch, config)
    model = None
    if case == "api":
        config["inference_profile"] = "api"
    elif case == "unsupported":
        monkeypatch.setattr(le, "support", lambda **kw: le.EngineSupport(
            le.LATER, "AMD here.", "amd"))
    elif case == "bad_repo":
        model = "no-slash"
    else:
        model = "someone/unknown-7b"
    result = le.apply("vllm", model=model, install=True)
    expected = {"api": "profile_api", "unsupported": "amd", "bad_repo": "bad_repo",
                "parser_unknown": "parser_unknown"}[case]
    assert result.status == "refused" and result.code == expected, result
    assert events == [] and "local_engine" not in config
    with pytest.raises(le.EngineRefusal) as refused:
        le.validate("vllm", model)
    assert refused.value.code == expected


def test_busy_lock(config, monkeypatch):
    events = _fakes(monkeypatch, config, installed=True)
    holder = le._switch_lock()
    holder.acquire()
    outcome = {}

    def run():
        started = time.monotonic()
        outcome["result"] = le.apply("vllm")
        outcome["uninstall"] = le.uninstall_vllm()
        outcome["seconds"] = time.monotonic() - started
    try:
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(5)
        assert not worker.is_alive(), "a second switch waited for the lock instead of refusing"
    finally:
        holder.release()
    assert outcome["seconds"] < 1.0, "the refusal waited for the lock"
    assert outcome["result"].status == "refused" and outcome["result"].code == "busy"
    assert outcome["uninstall"]["code"] == "busy"
    assert events == []
    assert le.apply("vllm").status == "done", "free again once the holder is gone"


@pytest.mark.parametrize("failure, code", [
    ("install", "install_failed"), ("probe", "cuda_probe_failed"),
    ("download", "download_failed"), ("cancelled", "download_cancelled"),
    ("llama_stop", "llama_stop_failed"), ("no_fit", "no_fit")])
def test_failed_steps_keep_engine(config, monkeypatch, failure, code):
    config["local_engine"] = "llama"
    kwargs = {}
    if failure == "install":
        kwargs = {"fail_step": "install_vllm", "install_reason": "pip failed (exit 1)."}
    elif failure == "probe":
        kwargs = {"fail_step": "install_probe", "install_reason": "PyTorch sees no CUDA GPU."}
    elif failure in ("download", "cancelled"):
        kwargs = {"model_dir": ""}
    elif failure == "no_fit":
        kwargs = {"fits": False}
    _fakes(monkeypatch, config, **kwargs)
    if failure == "llama_stop":
        monkeypatch.setattr(le, "_stop_llama", lambda say: (True, "llama-server (PID 1) did not stop."))
    if failure == "cancelled":
        monkeypatch.setattr(type(MODEL_DOWNLOAD), "cancel_requested", property(lambda self: True))
    result = le.apply("vllm", install=True)
    assert result.status == "failed" and result.code == code, result
    assert config["local_engine"] == "llama", "a failed step changed the engine"
    assert MAINTENANCE.snapshot()["error"] == code and not MAINTENANCE.active


def test_no_fit_reports_stopped_llama(config, monkeypatch):
    _fakes(monkeypatch, config, installed=True, fits=False)
    monkeypatch.setattr(le, "_stop_llama", lambda say: (True, ""))
    result = le.apply("vllm")
    assert result.code == "no_fit" and result.stopped_llama is True
    assert "stopped for the measurement" in result.detail
    assert result.to_dict()["stopped_llama"] is True and result.to_dict()["fit"]["code"] == "too_big"


def test_back_to_llama_stops_vllm(config, monkeypatch):
    config["local_engine"] = "vllm"
    stops = []
    monkeypatch.setattr(vs, "running_pid", lambda: 4242)
    monkeypatch.setattr(vs, "stop", lambda wait=15.0: stops.append(1) or "")
    result = le.apply("llama")
    assert result.status == "done" and result.code == "switched_llama"
    assert config["local_engine"] == "llama" and stops == [1]
    monkeypatch.setattr(vs, "stop", lambda wait=15.0: "vLLM (PID 4242) did not stop.")
    result = le.apply("llama")
    assert result.code == "vllm_stop_failed" and "llama.cpp" in result.note


def test_uninstall_refused_while_active(config, monkeypatch):
    config["local_engine"] = "vllm"
    removed = []
    monkeypatch.setattr(vs, "uninstall", lambda: removed.append(1) or "")
    out = le.uninstall_vllm()
    assert out["ok"] is False and out["code"] == "engine_active" and removed == []
    config["local_engine"] = "llama"
    monkeypatch.setattr(vs, "installed_version", lambda: vs.PIN)
    monkeypatch.setattr(vs, "local_snapshot", lambda repo: "/hf/snap")
    monkeypatch.setattr(vs, "model_size_bytes", lambda d: 2_870_000_000)
    out = le.uninstall_vllm()
    assert out["ok"] is True and out["code"] == "removed" and removed == [1]
    assert out["kept_model"] == REPO and out["kept_bytes"] == 2_870_000_000


def test_maintenance_steps(config, monkeypatch):
    _fakes(monkeypatch, config)
    seen = []
    le.apply("vllm", install=True, progress=lambda p: seen.append(
        (p["phase"], p["step"], p["steps"], MAINTENANCE.snapshot()["kind"])))
    assert [s[0] for s in seen[:7]] == list(le.PHASES[:7])
    assert [s[1] for s in seen[:7]] == list(range(7)), "done counts the finished steps"
    assert {s[2] for s in seen} == {7} and {s[3] for s in seen} == {"vllm_engine"}
    assert seen[-1][1] == 7 and not MAINTENANCE.active
    seen.clear()
    le.apply("vllm", progress=lambda p: seen.append((p["phase"], p["step"], p["steps"])))
    assert [s[0] for s in seen[:3]] == ["download_model", "handover", "fit_check"]
    assert {s[2] for s in seen} == {3} and seen[-1][1] == 3
    assert not MAINTENANCE.active and MAINTENANCE.snapshot()["error"] == ""


def test_gguf_done_frame_unchanged(monkeypatch, tmp_path):
    """The GGUF download keeps its closing frame with the model list (the vLLM one has none)."""
    import huggingface_hub

    from vaf.core import backend, web_interface
    pushed = []
    monkeypatch.setattr(web_interface, "start_model_download_broadcast", lambda: (lambda: None))
    monkeypatch.setattr(web_interface, "get_web_interface",
                        lambda: SimpleNamespace(push_update=pushed.append))

    def hf_hub_download(repo_id, filename, local_dir, tqdm_class=None):
        (tmp_path / filename).write_bytes(b"gguf")
        return str(tmp_path / filename)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", hf_hub_download)
    monkeypatch.setattr(backend, "_resolve_model_ref", lambda name: ("org/repo", "m.gguf"))
    assert backend.ensure_model_available("org/repo/m.gguf", tmp_path).endswith("m.gguf")
    assert pushed == [{"type": "model_download_done", "success": True, "models": ["m.gguf"]}]


def test_cancelled_download(config, monkeypatch, tmp_path):
    """A cancel from the banner ends a vLLM download through the progress hook: the switch
    ends with download_cancelled and the engine stays."""
    import huggingface_hub

    from vaf.core import web_interface
    config["local_engine"] = "llama"
    _fakes(monkeypatch, config, installed=True)
    monkeypatch.setattr(vs, "ensure_model", REAL_ENSURE_MODEL)
    monkeypatch.setattr(vs, "local_snapshot", lambda repo: "")
    monkeypatch.setattr(web_interface, "start_model_download_broadcast", lambda: (lambda: None))
    monkeypatch.setattr(web_interface, "get_web_interface",
                        lambda: SimpleNamespace(push_update=lambda frame: None))
    ticks = []

    def snapshot_download(repo_id, tqdm_class=None, **kw):
        bar = tqdm_class(total=100, disable=True)
        bar.update(10)
        ticks.append(1)
        assert MODEL_DOWNLOAD.request_cancel() is True
        bar.update(10)          # the next tick ends it
        ticks.append(2)
        return str(tmp_path)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    result = le.apply("vllm")
    assert result.code == "download_cancelled", result
    assert ticks == [1] and config["local_engine"] == "llama"
    assert MODEL_DOWNLOAD.request_cancel() is False, "no download runs: nothing to remember"


def test_reload_under_vllm_downloads_no_gguf(monkeypatch):
    import vaf.core.agent as agent_mod
    import vaf.core.backend as backend_mod
    from vaf.core.agent import Agent
    monkeypatch.delenv("VAF_MODEL_OVERRIDE", raising=False)
    monkeypatch.setattr(agent_mod.Config, "load", classmethod(lambda cls: {"model": "q.gguf"}))
    monkeypatch.setattr(le, "current", lambda config=None: "vllm")
    monkeypatch.setattr(le, "resolve_vllm_model", lambda config=None: REPO)
    monkeypatch.setattr(backend_mod, "ensure_local_model", lambda path, reason="", **kw: True)
    downloads = []
    d = SimpleNamespace(provider="local", llm=None, _backend_swap_lock=threading.Lock(),
                        use_server=False, _tokenizer_instance="x", filename="g.gguf",
                        model_path="/m/g.gguf", current_session_id=None,
                        history=[{"role": "system", "content": "S"}])
    d.ensure_model_exists = lambda: downloads.append(1)
    d.init_chat = lambda **kw: setattr(d, "history", [{"role": "system", "content": "N"}])
    d._apply_local_model_identity = lambda: Agent._apply_local_model_identity(d)
    assert Agent.reload_local_model(d) is True
    assert downloads == [], "a reload under vLLM resolved (and would download) a GGUF"
    assert d.model_display_name == "Qwen"


def test_refresh_identity_all(monkeypatch):
    import vaf.core.agent as agent_mod
    from vaf.core.agent import Agent
    monkeypatch.delenv("VAF_MODEL_OVERRIDE", raising=False)
    monkeypatch.setattr(le, "current", lambda config=None: "vllm")
    monkeypatch.setattr(le, "resolve_vllm_model", lambda config=None: REPO)

    def agent(**kw):
        a = SimpleNamespace(provider="local", filename="gemma-4-e4b.gguf", _config_overrides=None,
                            _tokenizer_instance="t", model_display_name="Gemma",
                            is_gemma_local=True, model_mode="gemma4", **kw)
        a._apply_local_model_identity = lambda: Agent._apply_local_model_identity(a)
        return a
    plain, embedded, api = agent(), agent(), agent()
    embedded._config_overrides = {"model": "x"}
    api.provider = "openai"
    broken = SimpleNamespace(provider="local", _config_overrides=None)
    broken._apply_local_model_identity = lambda: 1 / 0
    monkeypatch.setattr(agent_mod, "live_agents", lambda: [plain, embedded, api, broken])
    assert agent_mod.refresh_local_identity_all() == 1
    assert plain.model_mode is None and plain.is_gemma_local is False
    assert plain._tokenizer_instance is None
    assert embedded.model_mode == "gemma4" and api.model_mode == "gemma4"
    monkeypatch.setenv("VAF_MODEL_OVERRIDE", "x.gguf")
    plain.model_mode = "gemma4"
    assert agent_mod.refresh_local_identity_all() == 0 and plain.model_mode == "gemma4"


def test_cancelled_gguf_download_has_no_fallback(monkeypatch, tmp_path):
    """A GGUF download cancelled from the banner ends there: the self-heal does not start
    downloading the VRAM default in its place. A failure that was no cancel still does."""
    import huggingface_hub

    from vaf.core import backend, web_interface
    monkeypatch.setattr(web_interface, "start_model_download_broadcast", lambda: (lambda: None))
    monkeypatch.setattr(web_interface, "get_web_interface",
                        lambda: SimpleNamespace(push_update=lambda frame: None))
    fetched = []

    def hf_hub_download(repo_id, filename, local_dir, tqdm_class=None):
        fetched.append(filename)
        if filename == "m.gguf":
            raise InterruptedError("The download was cancelled.")
        (tmp_path / filename).write_bytes(b"gguf")
        return str(tmp_path / filename)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", hf_hub_download)
    monkeypatch.setattr(backend, "_resolve_model_ref",
                        lambda name: ("org/repo", "m.gguf") if "m.gguf" in name else ("org/auto", "auto.gguf"))
    import vaf.core.gpu_detection as gd
    monkeypatch.setattr(gd, "recommended_default_model", lambda: "org/auto/auto.gguf")
    monkeypatch.setattr(type(MODEL_DOWNLOAD), "cancel_requested", property(lambda self: True))
    backend.ensure_model_available("org/repo/m.gguf", tmp_path)
    assert fetched == ["m.gguf"], "a cancelled download fell back to the default model"
    monkeypatch.setattr(type(MODEL_DOWNLOAD), "cancel_requested", property(lambda self: False))
    fetched.clear()
    backend.ensure_model_available("org/repo/m.gguf", tmp_path)
    assert fetched == ["m.gguf", "auto.gguf"], "a plain failure keeps its self-heal"
