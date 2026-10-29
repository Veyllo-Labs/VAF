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
    monkeypatch.setattr(engine_mod, "_stop_llama", lambda: None)
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

    def install(log=None, run=None):
        calls.append("install")
        state["installed"] = True
        return ""
    monkeypatch.setattr(vs, "install", install)
    monkeypatch.setattr(vs, "ensure_model", lambda repo: calls.append(("model", repo)) or "/hf/snap")
    monkeypatch.setattr(vs, "plan_fit", lambda d, n, kv_dtype=None: vs.FitResult(
        fits, n, 0.78, "fp8", 6076, 7987, "Fits: about 6076 of 7987 MiB." if fits else
        "The model needs about 9000 MiB, but vLLM gets 7987 MiB."))
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
    config["local_engine"] = "vllm"
    out = _invoke().output
    assert "Engine: vllm" in out and "0.32.0" in out and "not running" in out


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
    assert "1.8 GB" in result.output
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
    monkeypatch.setattr(engine_mod, "_stop_llama", lambda: True)
    result = _invoke("vllm", "--yes")
    assert result.exit_code == 1
    assert "stopped" in result.output and "next use" in result.output


def test_the_fit_check_waits_for_the_released_gpu_memory(monkeypatch):
    """`vaf engine vllm` measures after the same handover the vLLM start runs (stopped, gone,
    port free, memory settled), not right after the kill."""
    from vaf.core.backend import ServerManager
    calls = []
    monkeypatch.setattr(ServerManager, "_llama_pid", lambda self: 4242)
    monkeypatch.setattr(ServerManager, "hand_over_from_llama",
                        lambda self, port=8080, wait=15.0: calls.append(port) or "")
    assert engine_mod._stop_llama() is True and calls == [8080]
    monkeypatch.setattr(ServerManager, "hand_over_from_llama",
                        lambda self, port=8080, wait=15.0: "llama-server (PID 4242) did not stop.")
    with pytest.raises(typer.Exit) as stopped:
        engine_mod._stop_llama()
    assert stopped.value.exit_code == 1
    monkeypatch.setattr(ServerManager, "_llama_pid", lambda self: None)
    assert engine_mod._stop_llama() is False
