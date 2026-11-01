# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf engine` (vaf/cli/cmd/engine.py): show, switch to vLLM, back to llama.cpp, uninstall.

Typer's CliRunner on the command alone; the install, the model download, the fit and the
GPU are faked, so nothing is installed or downloaded.

MUTATIONS, each turns the named test red:
- write local_engine before the support check -> test_unsupported_exit_2;
- return exit 0 on a refusal -> test_unsupported_exit_2 / test_engine_vllm_refused_under_api;
- switch although the model does not fit -> test_a_model_that_does_not_fit_keeps_llama;
- let --uninstall run while vLLM is the engine -> test_uninstall_refused_while_active.
"""
import pytest
import typer
from typer.testing import CliRunner

from vaf.cli.cmd import engine as engine_mod
from vaf.core import local_engine as le
from vaf.core import vllm_server as vs

REPO = "Qwen/Qwen3-4B-AWQ"
REAL_STOP_LLAMA = le._stop_llama


@pytest.fixture(autouse=True)
def no_display(monkeypatch):
    """The display state is pinned: this machine drives a display and a CI runner does not,
    and the desktop reserve follows it (vllm_server.desktop_reserve_mb). The GPU memory
    table of state() reads no GPU either."""
    from vaf.core import vllm_server
    monkeypatch.setattr(vllm_server, "_display_active", lambda: False)
    # state() builds the GPU memory table; no test asks nvidia-smi for it.
    monkeypatch.setattr(vllm_server, "_gpu_memory", lambda: (0, 0))


@pytest.fixture
def config(monkeypatch, tmp_path):
    from vaf.core.config import Config
    values = {"vllm_model": REPO}
    real = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: values[key] if key in values else real(key, default)))
    monkeypatch.setattr(Config, "set", staticmethod(lambda key, value: values.__setitem__(key, value)))
    monkeypatch.setattr(Config, "load", staticmethod(lambda: dict(values)))
    monkeypatch.setattr(vs, "pid_file", lambda: tmp_path / "vllm.pid")
    monkeypatch.setattr(vs, "venv_dir", lambda: tmp_path / "bin" / "vllm" / "venv")
    monkeypatch.setattr(le, "_stop_llama", lambda say: (False, ""))
    # The NVIDIA build unless a test says otherwise: the build follows this machine's GPU.
    monkeypatch.setattr(vs, "backend", lambda: vs.CUDA)
    return values


def _app():
    app = typer.Typer()
    app.command()(engine_mod.engine_cmd)
    return app


def _invoke(*args, input=None):
    return CliRunner().invoke(_app(), list(args), input=input)


def _supported(monkeypatch, ok=True):
    level = le.SUPPORTED if ok else le.UNSUPPORTED
    reason = "vLLM runs here (NVIDIA RTX 3080, 10 GB)." if ok else \
        "vLLM needs compute capability 7.5 or newer; this GPU has 6.1. llama.cpp runs here."
    monkeypatch.setattr(le, "support", lambda **kw: le.EngineSupport(level, reason))


def _fake_install(monkeypatch, calls, fits=True):
    state = {"installed": False}
    monkeypatch.setattr(vs, "is_installed", lambda: state["installed"])
    monkeypatch.setattr(vs, "installed_version", lambda: vs.PIN if state["installed"] else None)
    monkeypatch.setattr(vs, "recipe_current", lambda: state["installed"])

    def install(log=None, run=None, **kw):
        calls.append("install")
        state["installed"] = True
        return ""
    monkeypatch.setattr(vs, "install", install)
    monkeypatch.setattr(vs, "ensure_model", lambda repo, outcome=None: calls.append(("model", repo)) or "/hf/snap")
    monkeypatch.setattr(vs, "plan_fit", lambda d, n, kv_dtype=None, model="", utilization=None: vs.FitResult(
        fits, n, 0.78, "fp8", 6076, 7987, "Fits: about 6076 of 7987 MiB." if fits else
        "The model needs about 9000 MiB, but vLLM gets 7987 MiB."))
    monkeypatch.setattr(vs, "first_start", lambda repo, n_ctx, port=8080, utilization=None: vs.FirstStart(True))
    return state


def test_show(config, monkeypatch):
    _supported(monkeypatch)
    monkeypatch.setattr(vs, "installed_version", lambda: None)
    out = _invoke().output
    for words in ("Engine: llama", "supported", "vLLM runs here", "installed: no", REPO, "hermes", "qwen3"):
        assert words in out, (words, out)
    monkeypatch.setattr(vs, "installed_version", lambda: "0.32.0")
    monkeypatch.setattr(vs, "is_installed", lambda: True)
    monkeypatch.setattr(vs, "running_pid", lambda: None)
    monkeypatch.setattr(vs, "is_current", lambda: True)
    config["local_engine"] = "vllm"
    out = _invoke().output
    assert "Engine: vllm" in out and "0.32.0" in out and "not running" in out
    assert "earlier VAF" not in out
    # Installed by an earlier VAF: never shown as simply installed.
    monkeypatch.setattr(vs, "is_current", lambda: False)
    out = _invoke().output
    assert "vLLM installed: 0.32.0, set up by an earlier VAF" in out and "vaf engine vllm" in out


def test_show_prints_start_failure(config, monkeypatch):
    """`vaf engine` says a remembered start failure and how to retry, instead of "it starts
    with the next use". MUTATION: drop the branch - red."""
    _supported(monkeypatch)
    monkeypatch.setattr(vs, "installed_version", lambda: "0.32.0")
    monkeypatch.setattr(vs, "is_installed", lambda: True)
    monkeypatch.setattr(vs, "running_pid", lambda: None)
    monkeypatch.setattr(vs, "is_current", lambda: True)
    monkeypatch.setattr(vs, "recipe_current", lambda: True)
    monkeypatch.setattr(vs, "compute_capability", lambda: (8, 6))
    monkeypatch.setattr(vs, "flashinfer_buildable", lambda env=None: False)
    config["local_engine"] = "vllm"
    config["n_ctx"] = 32768
    vs._remember(vs.start_signature(REPO, 32768), "memory", vs.cause_text("memory"))
    out = " ".join(_invoke().output.split())
    assert "could not start" in out and "enough free memory" in out, out
    assert "vaf engine vllm" in out and "vaf engine llama" in out
    assert "it starts with the next use" not in out


def test_unsupported_exit_2(config, monkeypatch):
    _supported(monkeypatch, ok=False)
    calls = []
    _fake_install(monkeypatch, calls)
    result = _invoke("vllm", "--yes")
    assert result.exit_code == 2
    assert "compute capability 7.5" in result.output
    assert calls == [] and config.get("local_engine") is None


def test_engine_vllm_refused_under_api(config, monkeypatch):
    _supported(monkeypatch)
    config["inference_profile"] = "api"
    calls = []
    _fake_install(monkeypatch, calls)
    result = _invoke("vllm", "--yes")
    assert result.exit_code == 2 and "profile" in result.output
    assert calls == [] and config.get("local_engine") is None


def test_switch_with_yes(config, monkeypatch):
    _supported(monkeypatch)
    calls = []
    _fake_install(monkeypatch, calls)
    result = _invoke("vllm", "--yes")
    assert result.exit_code == 0, result.output
    assert calls == ["install", ("model", REPO)]
    assert config["local_engine"] == "vllm"
    assert "6076" in result.output, "the fit numbers are printed"
    # Installed now: a second run installs nothing.
    calls.clear()
    assert _invoke("vllm", "--model", "Qwen/Qwen3-8B-AWQ").exit_code == 0
    assert calls == [("model", "Qwen/Qwen3-8B-AWQ")] and config["vllm_model"] == "Qwen/Qwen3-8B-AWQ"


def test_switch_asks_before_installing(config, monkeypatch):
    _supported(monkeypatch)
    calls = []
    _fake_install(monkeypatch, calls)
    result = _invoke("vllm", input="n\n")
    assert result.exit_code == 1 and calls == [] and config.get("local_engine") is None
    assert f"{vs.DOWNLOAD_GB_AT_LEAST:.1f} GB" in result.output
    result = _invoke("vllm", input="y\n")
    assert result.exit_code == 0 and calls[0] == "install"


def test_a_model_that_does_not_fit_keeps_llama(config, monkeypatch):
    _supported(monkeypatch)
    _fake_install(monkeypatch, [], fits=False)
    result = _invoke("vllm", "--yes")
    assert result.exit_code == 1 and "9000 MiB" in result.output
    assert config.get("local_engine") is None


@pytest.mark.parametrize("args", [["vllm", "--model", "no-slash"], ["vllm", "--model", "a/b; rm"],
                                  ["bogus"], ["llama", "--yes"], ["vllm", "--uninstall", "--model", "a/b"]])
def test_wrong_arguments_exit_2(config, monkeypatch, args):
    _supported(monkeypatch)
    calls = []
    _fake_install(monkeypatch, calls)
    assert _invoke(*args).exit_code == 2
    assert calls == []


def test_an_unknown_model_family_is_refused_before_the_install(config, monkeypatch):
    _supported(monkeypatch)
    calls = []
    _fake_install(monkeypatch, calls)
    result = _invoke("vllm", "--yes", "--model", "someone/unknown-7b")
    assert result.exit_code == 2 and "vllm_tool_call_parser" in result.output
    assert calls == []


def test_switch_back_to_llama(config, monkeypatch):
    config["local_engine"] = "vllm"
    stops = []
    monkeypatch.setattr(vs, "running_pid", lambda: 4242)
    monkeypatch.setattr(vs, "stop", lambda wait=15.0: stops.append(1) or "")
    result = _invoke("llama")
    assert result.exit_code == 0 and config["local_engine"] == "llama" and stops == [1]


def test_uninstall_refused_while_active(config, monkeypatch):
    config["local_engine"] = "vllm"
    removed = []
    monkeypatch.setattr(vs, "uninstall", lambda: removed.append(1) or "")
    result = _invoke("vllm", "--uninstall")
    assert result.exit_code == 2 and removed == []
    config["local_engine"] = "llama"
    monkeypatch.setattr(vs, "installed_version", lambda: vs.PIN)
    monkeypatch.setattr(vs, "local_snapshot", lambda repo: "")
    result = _invoke("vllm", "--uninstall")
    assert result.exit_code == 0 and removed == [1]
    assert "Hugging Face cache" in result.output


def test_the_command_is_registered():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "vaf" / "main.py").read_bytes().decode("utf-8")
    assert 'app.command(name="engine"' in source and "engine_cmd.engine_cmd" in source


def test_a_failed_fit_says_the_llama_server_was_stopped(config, monkeypatch):
    """The llama server was stopped for the measurement; the user learns it comes back."""
    _supported(monkeypatch)
    _fake_install(monkeypatch, [], fits=False)
    monkeypatch.setattr(le, "_stop_llama", lambda say: (True, ""))
    result = _invoke("vllm", "--yes")
    assert result.exit_code == 1
    assert "stopped" in result.output and "next use" in result.output


def test_the_fit_check_waits_for_the_released_gpu_memory(config, monkeypatch):
    """`vaf engine vllm` measures after the same handover the vLLM start runs (stopped, gone,
    port free, memory settled), not right after the kill. The handover moved into the core
    with the switch (local_engine._stop_llama); a handover that fails exits 1."""
    from vaf.core.backend import ServerManager
    calls = []
    monkeypatch.setattr(ServerManager, "_llama_pid", lambda self: 4242)
    monkeypatch.setattr(ServerManager, "hand_over_from_llama",
                        lambda self, port=8080, wait=15.0: calls.append(port) or "")
    monkeypatch.setattr(le, "_stop_llama", REAL_STOP_LLAMA)
    assert le._stop_llama(lambda text, kind="info": None) == (True, "") and calls == [8080]
    monkeypatch.setattr(ServerManager, "hand_over_from_llama",
                        lambda self, port=8080, wait=15.0: "llama-server (PID 4242) did not stop.")
    _supported(monkeypatch)
    _fake_install(monkeypatch, [])
    stopped = _invoke("vllm", "--yes")
    assert stopped.exit_code == 1 and "did not stop" in stopped.output
    assert config.get("local_engine") is None
    monkeypatch.setattr(ServerManager, "_llama_pid", lambda self: None)
    assert le._stop_llama(lambda text, kind="info": None) == (False, "")


def test_models_set_warns_under_vllm(config):
    """A GGUF set under vLLM is stored but not run: the command says so and names the vLLM
    model command. MUTATION: remove the warning - red."""
    from vaf.cli.cmd import models as models_mod
    runner = CliRunner()
    config["local_engine"] = "vllm"
    result = runner.invoke(models_mod.app, ["set", "qwen.gguf"])
    assert result.exit_code == 0 and config["model"] == "qwen.gguf"
    assert "vaf engine vllm --model" in result.output
    config["local_engine"] = "llama"
    result = runner.invoke(models_mod.app, ["set", "qwen.gguf"])
    assert "vaf engine vllm --model" not in result.output


@pytest.mark.parametrize("key", ["local_engine", "vllm_model", " Local_Engine "])
def test_models_config_refuses_the_switch_keys(config, key):
    """`vaf models config` writes any key; the engine and its model change only through the
    switch (install, download, fit). The parser keys stay writable there."""
    from vaf.cli.cmd import models as models_mod
    result = CliRunner().invoke(models_mod.app, ["config", key, "vllm"])
    assert result.exit_code == 2 and "vaf engine" in result.output
    assert config.get("local_engine") is None and config["vllm_model"] == REPO
    result = CliRunner().invoke(models_mod.app, ["config", "vllm_tool_call_parser", "hermes"])
    assert result.exit_code == 0 and config["vllm_tool_call_parser"] == "hermes"


def test_engine_on_an_amd_gpu(config, monkeypatch):
    """On a supported AMD GPU `vaf engine` prints the ROCm verdict, and the install question
    names the ROCm build's size; both come from the core, the CLI adds nothing of its own.
    MUTATION: the CLI or the core printing the CUDA size - red."""
    from vaf.core.gpu_detection import GPUInfo
    verdict = le.support(gpu=GPUInfo("amd", "AMD Radeon RX 7900 XTX", 24560, True, True,
                                     gfx_arch="gfx1100"),
                         system="Linux", machine="x86_64", glibc="2.39", rocm=(True, "7.2.3"),
                         python312="/usr/bin/python3.12", kfd_access=True)
    monkeypatch.setattr(le, "support", lambda **kw: verdict)
    monkeypatch.setattr(vs, "backend", lambda: vs.ROCM)
    monkeypatch.setattr(vs, "installed_version", lambda: None)
    monkeypatch.setattr(le, "default_vllm_model", lambda vram_gb=None: REPO)
    out = " ".join(_invoke().output.split())      # the console wraps long lines
    assert "vLLM runs here (AMD Radeon RX 7900 XTX, 24 GB)" in out, out
    calls = []
    monkeypatch.setattr(vs, "install", lambda **kw: calls.append("install") or "")
    result = _invoke("vllm", input="n\n")
    assert result.exit_code == 1 and calls == []
    out = " ".join(result.output.split())
    assert "about 3.3 GB or more to download" in out, out
    assert "2.8 GB" not in out


def _table(configured=0.0, display=True, auto=0.67, total=10240, reserve=3328):
    return {"total_mb": total, "free_mb": 8500, "display": display, "reserve_mb": reserve,
            "configured": configured, "auto_share": auto, "auto_reserved": True,
            "kv_dtype": "int8_per_token_head", "range": [0.30, 0.95], "min_share": 0.67,
            "steps": []}


@pytest.mark.parametrize("value, passed", [("0.68", 0.68), ("auto", 0.0), ("AUTO", 0.0),
                                           (None, None)])
def test_the_gpu_share_is_passed_to_the_switch(config, monkeypatch, value, passed):
    """`--gpu-share 0.68` and `auto` reach ``apply``; no flag keeps the stored share.
    MUTATION: not passing the flag through - red."""
    seen = []
    monkeypatch.setattr(le, "apply", lambda engine, **kw: seen.append((engine, kw.get("gpu_share")))
                        or le.EngineSwitch("done", "unchanged_vllm", "The local engine is vLLM.",
                                           "vllm"))
    args = ["vllm"] + (["--gpu-share", value] if value is not None else [])
    assert _invoke(*args).exit_code == 0
    assert seen == [("vllm", passed)]


def test_a_bad_gpu_share_exits_2(config, monkeypatch):
    """The flag with llama, with --uninstall, or a value the switch refuses: exit 2, and
    nothing runs."""
    monkeypatch.setattr(le, "support", lambda **kw: pytest.fail("the verdict ran"))
    result = _invoke("llama", "--gpu-share", "0.7")
    assert result.exit_code == 2 and "--gpu-share goes with vllm" in result.output
    result = _invoke("vllm", "--uninstall", "--gpu-share", "0.7")
    assert result.exit_code == 2 and "--gpu-share" in result.output
    for value in ("0.2", "0.97", "x", "nan"):
        result = _invoke("vllm", "--gpu-share", value)
        assert result.exit_code == 2, (value, result.output)
        assert "0.30 to 0.95" in " ".join(result.output.split()), result.output
    assert config.get("local_engine") is None and "vllm_gpu_memory_utilization" not in config


def test_show_prints_the_gpu_memory_line(config, monkeypatch):
    """`vaf engine` says vLLM's share, automatic or by hand, its size and what stays free for
    the screen. MUTATION: drop the line - red."""
    _supported(monkeypatch)
    monkeypatch.setattr(vs, "installed_version", lambda: None)
    monkeypatch.setattr(le, "_memory_table", lambda repo, server: _table())
    out = " ".join(_invoke().output.split())
    assert ("vLLM GPU memory: automatic, 0.67 now (6.7 of 10.0 GB); 3.3 GB stay free for the "
            "screen (this GPU drives a display)") in out, out
    monkeypatch.setattr(le, "_memory_table", lambda repo, server: _table(configured=0.78))
    out = " ".join(_invoke().output.split())
    assert ("vLLM GPU memory: 0.78, set by hand (7.8 of 10.0 GB); leaves 2.2 GB, less than the "
            "3.3 GB a screen on this GPU needs") in out, out
    monkeypatch.setattr(le, "_memory_table", lambda repo, server: _table(auto=None))
    assert "automatic, measured at the next start" in " ".join(_invoke().output.split())
    monkeypatch.setattr(le, "_memory_table", lambda repo, server: _table(display=False, reserve=0,
                                                                         configured=0.78))
    out = " ".join(_invoke().output.split())
    assert "leaves 2.2 GB" in out and "screen on this GPU needs" not in out
    monkeypatch.setattr(le, "_memory_table", lambda repo, server: _table(display=None))
    assert "VAF cannot tell whether this GPU drives a display" in " ".join(_invoke().output.split())


def test_a_switch_below_the_reserve_warns(config, monkeypatch):
    """A share by hand that leaves less than the reserve on a display GPU is applied, with a
    warning that the screen can freeze."""
    fit = vs.FitResult(True, 40960, 0.78, "int8_per_token_head", 7407, 7987, "Fits.", code="fits",
                       leaves_mb=2253, below_reserve=True)
    monkeypatch.setattr(le, "apply", lambda engine, **kw: le.EngineSwitch(
        "done", "unchanged_vllm", "The local engine is vLLM.", "vllm", fit=fit))
    out = " ".join(_invoke("vllm", "--gpu-share", "0.78").output.split())
    assert "leaves 2.2 GB" in out and "screen can freeze" in out and "3.3 GB" in out, out
    monkeypatch.setattr(le, "apply", lambda engine, **kw: le.EngineSwitch(
        "done", "unchanged_vllm", "The local engine is vLLM.", "vllm",
        fit=vs.FitResult(True, 40960, 0.6, "int8_per_token_head", 6000, 6144, "Fits.")))
    assert "screen can freeze" not in _invoke("vllm", "--gpu-share", "0.6").output


def test_no_below_reserve_warning_for_a_switch_that_failed(config, monkeypatch):
    """A first start that failed carries its passing fit, and the stored share is unchanged:
    no warning about a share that was not applied. MUTATION: drop the status check - red."""
    fit = vs.FitResult(True, 40960, 0.78, "int8_per_token_head", 7407, 7987, "Fits.", code="fits",
                       leaves_mb=2253, below_reserve=True)
    monkeypatch.setattr(le, "apply", lambda engine, **kw: le.EngineSwitch(
        "failed", "start_failed", "vLLM did not start: it stopped during its start.", "vllm",
        fit=fit, detail="The local engine stays llama."))
    result = _invoke("vllm", "--gpu-share", "0.78")
    assert result.exit_code == 1
    assert "screen can freeze" not in result.output and "did not start" in result.output
