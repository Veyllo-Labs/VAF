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
- refresh a pinned or embedded agent -> test_refresh_identity_all;
- `needs_install` without the recipe record -> test_an_environment_of_the_earlier_recipe_needs_install;
- `_apply_vllm` deciding the code by the last step instead of the probe outcome ->
  test_failed_steps_keep_engine[libs] / [probe_last_step].
"""
import threading
import time
from types import SimpleNamespace

import pytest

from vaf.core import local_engine as le
from vaf.core import vllm_server as vs
from vaf.core.maintenance_state import MAINTENANCE
from vaf.core.model_download_state import MODEL_DOWNLOAD, DownloadCancelled, was_cancelled

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
           model_dir="/hf/snap", fits=True, probe=""):
    events = config["_events"]
    state = {"installed": installed}
    monkeypatch.setattr(vs, "is_installed", lambda: state["installed"])
    monkeypatch.setattr(vs, "installed_version", lambda: vs.PIN if state["installed"] else None)
    monkeypatch.setattr(vs, "recipe_current", lambda: state["installed"])

    def install(log=None, run=None, *, output=None, on_step=None, outcome=None):
        events.append(("install", output))
        for code in vs.INSTALL_STEPS:
            if on_step:
                on_step(code)
            if code == fail_step:
                if probe and outcome is not None:
                    outcome["probe"] = probe
                return install_reason
        state["installed"] = True
        return ""
    monkeypatch.setattr(vs, "install", install)
    monkeypatch.setattr(vs, "ensure_model", lambda repo, outcome=None: events.append(("model", repo)) or model_dir)

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
    assert f"{vs.DOWNLOAD_GB_AT_LEAST:.1f} GB" in result.text and result.params["pin"] == vs.PIN
    assert result.params["download_gb"] == "2.8"
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
    ("install", "install_failed"), ("probe", "cuda_probe_failed"), ("libs", "vllm_libs_failed"),
    ("probe_last_step", "install_failed"),
    ("download", "download_failed"), ("cancelled", "download_cancelled"),
    ("llama_stop", "llama_stop_failed"), ("no_fit", "no_fit")])
def test_failed_steps_keep_engine(config, monkeypatch, failure, code):
    config["local_engine"] = "llama"
    kwargs = {}
    if failure == "install":
        kwargs = {"fail_step": "install_vllm", "install_reason": "pip failed (exit 1)."}
    elif failure == "probe":
        kwargs = {"fail_step": "install_probe", "install_reason": "PyTorch sees no CUDA GPU.",
                  "probe": "no_cuda"}
    elif failure == "libs":
        kwargs = {"fail_step": "install_probe", "probe": "libs",
                  "install_reason": "vLLM's CUDA libraries do not load."}
    elif failure == "probe_last_step":
        # A failure at the probe step that is no probe outcome (the record could not be
        # written) is an install failure, not a driver or library verdict.
        kwargs = {"fail_step": "install_probe", "install_reason": "record not written."}
    elif failure in ("download", "cancelled"):
        kwargs = {"model_dir": ""}
    elif failure == "no_fit":
        kwargs = {"fits": False}
    _fakes(monkeypatch, config, **kwargs)
    if failure == "llama_stop":
        monkeypatch.setattr(le, "_stop_llama", lambda say: (True, "llama-server (PID 1) did not stop."))
    if failure == "cancelled":
        # This download's own exception was the cancel (ensure_model says so in outcome).
        monkeypatch.setattr(vs, "ensure_model",
                            lambda repo, outcome=None: outcome.__setitem__("cancelled", True) or "")
    result = le.apply("vllm", install=True)
    assert result.status == "failed" and result.code == code, result
    assert config["local_engine"] == "llama", "a failed step changed the engine"
    assert MAINTENANCE.snapshot()["error"] == code and not MAINTENANCE.active


def test_an_environment_of_the_earlier_recipe_needs_install(config, monkeypatch, tmp_path):
    """The pinned METADATA and the exe but no recipe record (built by the earlier recipe):
    the switch offers the install instead of switching onto it."""
    site = vs.venv_dir() / "lib" / "python3.13" / "site-packages" / f"vllm-{vs.PIN}.dist-info"
    site.mkdir(parents=True)
    (site / "METADATA").write_bytes(f"Name: vllm\nVersion: {vs.PIN}\n".encode("utf-8"))
    vs.vllm_exe().parent.mkdir(parents=True, exist_ok=True)
    vs.vllm_exe().write_bytes(b"#!/bin/sh\n")
    assert vs.is_installed() and vs.installed_version() == vs.PIN
    assert le.needs_install()
    result = le.apply("vllm")
    assert result.code == "needs_install"
    # The question says the old environment is replaced, not that nothing is installed.
    assert result.text.startswith(f"vLLM {vs.PIN} in ") and "earlier VAF" in result.text
    assert "it is replaced" in result.text and result.params["replaces"] == vs.PIN
    vs.recipe_path().write_bytes(vs.RECIPE_FORMAT.encode("utf-8"))
    assert not le.needs_install()


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
    # Six steps with the install: env, vLLM, probe, then the model, the handover and the fit.
    assert le.PHASES[:6] == ("install_env", "install_vllm", "install_probe", "download_model",
                             "handover", "fit_check")
    assert [s[0] for s in seen[:6]] == list(le.PHASES[:6])
    assert [s[1] for s in seen[:6]] == list(range(6)), "done counts the finished steps"
    assert {s[2] for s in seen} == {6} and {s[3] for s in seen} == {"vllm_engine"}
    assert seen[-1][1] == 6 and not MAINTENANCE.active
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

    mode = {"cancel": True}

    def hf_hub_download(repo_id, filename, local_dir, tqdm_class=None):
        fetched.append(filename)
        if filename == "m.gguf":
            if mode["cancel"] == "wrapped":
                try:
                    raise DownloadCancelled("The download was cancelled.")
                except DownloadCancelled as cancel:
                    raise RuntimeError("download failed") from cancel
            if mode["cancel"]:
                raise DownloadCancelled("The download was cancelled.")
            raise InterruptedError("The connection dropped.")
        (tmp_path / filename).write_bytes(b"gguf")
        return str(tmp_path / filename)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", hf_hub_download)
    monkeypatch.setattr(backend, "_resolve_model_ref",
                        lambda name: ("org/repo", "m.gguf") if "m.gguf" in name else ("org/auto", "auto.gguf"))
    import vaf.core.gpu_detection as gd
    monkeypatch.setattr(gd, "recommended_default_model", lambda: "org/auto/auto.gguf")
    backend.ensure_model_available("org/repo/m.gguf", tmp_path)
    assert fetched == ["m.gguf"], "a cancelled download fell back to the default model"
    # A library that re-raises the cancel wrapped still reads as a cancel.
    mode["cancel"] = "wrapped"
    fetched.clear()
    backend.ensure_model_available("org/repo/m.gguf", tmp_path)
    assert fetched == ["m.gguf"], "a wrapped cancel fell back to the default model"
    # A later plain failure: the earlier cancel must not answer for it (no stale flag).
    mode["cancel"] = False
    fetched.clear()
    backend.ensure_model_available("org/repo/m.gguf", tmp_path)
    assert fetched == ["m.gguf", "auto.gguf"], "a plain failure keeps its self-heal"


def test_model_change_stops_running_vllm_before_fit(config, monkeypatch):
    """A running vLLM holds its own share of the GPU: measured against it, a model change was
    refused although it fits once the old server is gone (820 of 10240 MiB free). The switch
    stops it first and says so; the same model already served is neither stopped nor measured."""
    config["local_engine"] = "vllm"
    events = _fakes(monkeypatch, config, installed=True)
    running = {"pid": 4242}
    monkeypatch.setattr(vs, "running_pid", lambda: running["pid"])
    monkeypatch.setattr(vs, "served_model", lambda port=8080: REPO)
    monkeypatch.setattr(vs, "stop", lambda wait=15.0: events.append(("stop_vllm",))
                        or running.__setitem__("pid", None) or "")

    def plan_fit(d, n, kv_dtype=None):
        events.append(("fit",))
        fits = running["pid"] is None
        return vs.FitResult(fits, n, 0.5, "fp8", 6000, 5120, "x",
                            code="fits" if fits else "share_above_free")
    monkeypatch.setattr(vs, "plan_fit", plan_fit)
    result = le.apply("vllm", model="Qwen/Qwen3-8B-AWQ")
    assert result.status == "done", result
    names = [e[0] for e in events if e[0] != "set"]
    assert names.index("stop_vllm") < names.index("fit"), "measured while the old vLLM held the GPU"
    assert result.stopped_vllm is True and result.to_dict()["stopped_vllm"] is True
    assert config["vllm_model"] == "Qwen/Qwen3-8B-AWQ"
    # The model it already serves: no stop, no measurement, and no needless vllm_model write.
    events.clear()
    running["pid"] = 4242
    config["vllm_model"] = REPO
    result = le.apply("vllm", model=REPO)
    assert result.status == "done" and result.fit is None and result.stopped_vllm is False
    assert not [e for e in events if e[0] in ("stop_vllm", "fit")]
    assert ("set", "vllm_model", REPO) not in events


def test_no_fit_after_stopping_vllm_says_so(config, monkeypatch):
    config["local_engine"] = "vllm"
    _fakes(monkeypatch, config, installed=True, fits=False)
    running = {"pid": 4242}
    monkeypatch.setattr(vs, "running_pid", lambda: running["pid"])
    monkeypatch.setattr(vs, "served_model", lambda port=8080: REPO)
    monkeypatch.setattr(vs, "stop", lambda wait=15.0: running.__setitem__("pid", None) or "")
    result = le.apply("vllm", model="Qwen/Qwen3-8B-AWQ")
    assert result.code == "no_fit" and result.stopped_vllm is True
    assert "vLLM was stopped for the measurement" in result.detail


def test_uninstall_rechecks_the_engine_under_the_lock(config, monkeypatch):
    """A switch to vLLM that finishes between the first check and the lock must not lose its
    environment."""
    removed = []
    monkeypatch.setattr(vs, "uninstall", lambda: removed.append(1) or "")
    monkeypatch.setattr(vs, "installed_version", lambda: vs.PIN)
    answers = iter([False, True])
    monkeypatch.setattr(le, "is_vllm", lambda config=None: next(answers))
    out = le.uninstall_vllm()
    assert out["code"] == "engine_active" and removed == []


def test_a_cancel_request_ends_with_its_download():
    """A request belongs to the download it was made for: finish() drops it, so it can never
    end the next download; whether a download was cancelled is the honoured-cancel count."""
    MODEL_DOWNLOAD.start("a/b", "b")
    assert MODEL_DOWNLOAD.request_cancel() is True and MODEL_DOWNLOAD.cancel_requested
    MODEL_DOWNLOAD.finish()
    assert MODEL_DOWNLOAD.cancel_requested is False


def test_a_plain_failure_is_no_cancel_while_another_download_was_cancelled(monkeypatch, tmp_path):
    """A GGUF and a vLLM model download can run at once (each on its own file lock). A vLLM
    download cancelled from the banner while the GGUF one fails for another reason must not
    take the GGUF self-heal away: the decision is the failing download's own exception."""
    import huggingface_hub

    import vaf.core.gpu_detection as gd
    from vaf.core import backend, web_interface
    monkeypatch.setattr(web_interface, "start_model_download_broadcast", lambda: (lambda: None))
    monkeypatch.setattr(web_interface, "get_web_interface",
                        lambda: SimpleNamespace(push_update=lambda frame: None))
    fetched = []

    def hf_hub_download(repo_id, filename, local_dir, tqdm_class=None):
        fetched.append(filename)
        if filename == "m.gguf":
            # Meanwhile the banner's Cancel was pressed for the other download (the vLLM
            # model) in the shared slot; this one then fails for a reason of its own.
            assert MODEL_DOWNLOAD.request_cancel() is True
            raise InterruptedError("The connection dropped.")
        (tmp_path / filename).write_bytes(b"gguf")
        return str(tmp_path / filename)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", hf_hub_download)
    monkeypatch.setattr(backend, "_resolve_model_ref",
                        lambda name: ("org/repo", "m.gguf") if "m.gguf" in name else ("org/auto", "auto.gguf"))
    monkeypatch.setattr(gd, "recommended_default_model", lambda: "org/auto/auto.gguf")
    backend.ensure_model_available("org/repo/m.gguf", tmp_path)
    assert fetched == ["m.gguf", "auto.gguf"], "another download's cancel took the self-heal away"


def test_was_cancelled_reads_the_exception_chain():
    assert was_cancelled(DownloadCancelled("x"))
    try:
        try:
            raise DownloadCancelled("x")
        except DownloadCancelled:
            raise OSError("wrapped")
    except OSError as wrapped:
        assert was_cancelled(wrapped)
    assert not was_cancelled(InterruptedError("profile switched"))
    assert not was_cancelled(RuntimeError("network"))


def test_maintenance_done_frame_reaches_banner(config, monkeypatch):
    """The switch marks MAINTENANCE done and clears it at once; the 1 s broadcaster still
    sends one inactive frame with the kind, which the banner shows as "finished"."""
    import time

    from vaf.core import web_interface
    frames = []
    monkeypatch.setattr(web_interface, "get_web_interface",
                        lambda: SimpleNamespace(push_update=frames.append))
    stop = web_interface.start_maintenance_broadcast(poll_seconds=0.05)
    try:
        _fakes(monkeypatch, config, installed=True)
        original = vs.ensure_model
        monkeypatch.setattr(vs, "ensure_model", lambda repo, outcome=None: time.sleep(0.2) or original(repo))
        assert le.apply("vllm").status == "done"
        time.sleep(0.3)
    finally:
        stop()
    seen = [f for f in frames if f["kind"] == "vllm_engine"]
    assert any(f["active"] for f in seen)
    assert seen[-1]["active"] is False and seen[-1]["error"] == "", seen[-1]


def test_done_frame_sent_when_the_extra_fails(monkeypatch):
    from vaf.core import web_interface
    pushed = []
    monkeypatch.setattr(web_interface, "start_model_download_broadcast", lambda: (lambda: None))
    monkeypatch.setattr(web_interface, "get_web_interface",
                        lambda: SimpleNamespace(push_update=pushed.append))
    with web_interface.model_download_broadcast("a/b", "b", done_extra=lambda: 1 / 0) as out:
        out["success"] = True
    assert pushed == [{"type": "model_download_done", "success": True}]


def test_support_class_is_worked_out_once_and_never_raises(monkeypatch):
    """A conclusive verdict is kept for the process; an inconclusive one (detection raised, or
    "no GPU" while nvidia-smi is installed: its probe reports a timeout as no GPU) is not, and
    is tried again only after the retry floor, so an unauthenticated poll cannot start the
    probes on every request."""
    import shutil

    import vaf.core.gpu_detection as gd
    clock = {"now": 1000.0}
    monkeypatch.setattr(le.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(le, "_SUPPORT_CLASS", None)
    monkeypatch.setattr(le, "_SUPPORT_RETRY_AT", 0.0)
    probes = []
    gpu = {"value": None, "raise": False}

    def get_primary_gpu():
        probes.append(1)
        if gpu["raise"]:
            raise OSError("nvidia-smi timed out")
        return gpu["value"]
    monkeypatch.setattr(gd, "get_primary_gpu", get_primary_gpu)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    # The driver is still loading: the probe sees no GPU although nvidia-smi is there.
    assert le.support_class() is None
    assert le.support_class() is None and len(probes) == 1, "probed again inside the floor"
    gpu["raise"] = True
    clock["now"] += le.SUPPORT_RETRY_SECONDS + 1
    assert le.support_class() is None and len(probes) == 2
    # The next try after the floor finds the GPU; that verdict is kept.
    gpu["raise"] = False
    gpu["value"] = _nv_gpu()
    clock["now"] += le.SUPPORT_RETRY_SECONDS + 1
    monkeypatch.setattr(le, "support", lambda gpu=None, **kw: le.EngineSupport(
        le.SUPPORTED, "x", "supported", {"gpu": "RTX"}))
    assert le.support_class() == ("supported", "supported")
    clock["now"] += 10 * le.SUPPORT_RETRY_SECONDS
    assert le.support_class() == ("supported", "supported") and len(probes) == 3
    # Without nvidia-smi, "no GPU" is a conclusive answer and is kept.
    monkeypatch.setattr(le, "_SUPPORT_CLASS", None)
    monkeypatch.setattr(le, "_SUPPORT_RETRY_AT", 0.0)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(le, "support", lambda gpu=None, **kw: le.EngineSupport(
        le.UNSUPPORTED, "x", "no_gpu"))
    assert le.support_class() == ("unsupported", "no_gpu")
    assert le.support_class() == ("unsupported", "no_gpu") and len(probes) == 4


def _nv_gpu():
    from vaf.core.gpu_detection import GPUInfo
    return GPUInfo("nvidia", "NVIDIA GeForce RTX 3080", 10240, True, True, compute_capability="8.6")


@pytest.mark.parametrize("starter, name", [("start_maintenance_broadcast", "maintenance-broadcast"),
                                           ("start_model_download_broadcast", "model-download-broadcast")])
def test_broadcast_stop_waits_for_its_thread(monkeypatch, starter, name):
    """stop() returns only once the loop's thread has ended: a push in flight (here a slow
    one) kept the thread alive after a stop that only set the event, and the leaked-loop guard
    caught it under the slower hostile environment."""
    import threading
    import time

    from vaf.core import web_interface
    pushing = threading.Event()

    def slow_push(frame):
        pushing.set()
        time.sleep(0.3)
    monkeypatch.setattr(web_interface, "get_web_interface",
                        lambda: SimpleNamespace(push_update=slow_push))
    MAINTENANCE.update(kind="vllm_engine", active=True, done=0, total=3, phase="handover")
    MODEL_DOWNLOAD.start("a/b", "b")
    try:
        stop = getattr(web_interface, starter)(poll_seconds=0.01)
        assert pushing.wait(2), "the loop never pushed"
        stop()
        assert not [t for t in threading.enumerate() if t.name == name and t.is_alive()]
    finally:
        MODEL_DOWNLOAD.finish()
        MAINTENANCE.clear()


def test_support_class_does_not_queue_callers_behind_a_running_probe(monkeypatch):
    """The unauthenticated needs-setup route must not hold one thread per caller while a probe
    runs into its timeouts: a second caller gets the answer known so far (None) at once."""
    import threading
    import time

    import vaf.core.gpu_detection as gd
    monkeypatch.setattr(le, "_SUPPORT_CLASS", None)
    monkeypatch.setattr(le, "_SUPPORT_RETRY_AT", 0.0)
    probing, release = threading.Event(), threading.Event()

    def slow_probe():
        probing.set()
        release.wait(5)
        return None
    monkeypatch.setattr(gd, "get_primary_gpu", slow_probe)
    first = threading.Thread(target=le.support_class, daemon=True)
    first.start()
    try:
        assert probing.wait(2)
        started = time.monotonic()
        assert le.support_class() is None
        assert time.monotonic() - started < 0.5, "a second caller waited for the probe"
    finally:
        release.set()
        first.join(5)
    assert not le._SUPPORT_LOCK.locked(), "the lock was not given back"
