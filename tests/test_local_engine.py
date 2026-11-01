# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The engine of provider local: the choice, the platform verdicts, the model, the parsers and
the two wire-format differences (vaf/core/local_engine.py).

Nothing here installs vLLM, downloads a model or touches a GPU: nvidia-smi, HTTP and the
config are faked.

MUTATIONS, each turns the named test red:
- drop the `.strip().lower()` in `current` -> test_current_engine_values;
- change the 7.5 floor to 7.0, or drop compute_cap from the nvidia-smi query ->
  test_support_matrix / test_detect_nvidia_reads_compute_cap;
- lower MIN_DRIVER to (575, 0, 0), or drop driver_version from the nvidia-smi query ->
  test_support_matrix[driver-575] / test_detect_nvidia_reads_compute_cap;
- lower an arch's MIN_GLIBC, use the x86_64 floor for aarch64, or let "" (musl) pass ->
  test_glibc_floor_per_arch; read glibc through platform.libc_ver only, or answer "" on a
  host that is not Linux -> test_host_glibc_reads_where_pip_reads;
- drop the two-field retry for an old driver -> test_an_old_driver_still_finds_the_gpu;
- remove the profile check in `vllm_server.ensure_model` (or `install`) ->
  test_api_profile_refuses_install_model_and_start;
- map Qwen to llama3_json, or default an unknown family to hermes ->
  test_parsers_for_families / test_unknown_family_refuses;
- read only `tokens` in `local_token_count` -> test_local_token_count_shapes;
- read only `reasoning_content` in `reasoning_text` -> test_reasoning_text_both_fields;
- swap the two default models -> test_default_vllm_model_by_vram;
- remove `vllm_` from the admin-only prefixes -> test_engine_keys_admin_only;
- drop the "AMD " strip, or route AMD past its branch -> test_amd_supported_verdict;
- check the ROCm runtime before the architecture, refuse an unreadable ROCm version, or
  remove gfx1151 from ROCM_GFX -> test_amd_verdict_rows;
- keep `amd` in SUPPORT_CODES -> test_amd_is_no_longer_a_later_code;
- drop rocm-smi/amd-smi from support_class's inconclusive check ->
  test_support_class_amd_no_gpu_is_inconclusive;
- state() or the install question reading the CUDA size -> test_download_size_follows_the_backend.
"""
import subprocess
import sys
from types import SimpleNamespace

import pytest

from vaf.core import local_engine as le
from vaf.core.gpu_detection import GPUInfo


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
def config(monkeypatch):
    from vaf.core.config import Config
    values = {}
    real = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: values[key] if key in values else real(key, default)))
    monkeypatch.setattr(Config, "set", staticmethod(lambda key, value: values.__setitem__(key, value)))
    monkeypatch.setattr(Config, "load", staticmethod(lambda: dict(values)))
    return values


@pytest.mark.parametrize("value, expected", [
    (None, "llama"), ("", "llama"), ("llama", "llama"), ("vllm", "vllm"), ("VLLM", "vllm"),
    (" vLLM ", "vllm"), ("vlm", "llama"), ("v llm", "llama"), (3, "llama"),
])
def test_current_engine_values(value, expected):
    """A typo never takes the GGUF engine away: only the word vllm selects vLLM."""
    assert le.current({"local_engine": value}) == expected
    assert le.current({}) == "llama"


def _nv(cc="8.6", vram=10240, model="NVIDIA GeForce RTX 3080", driver=""):
    return GPUInfo("nvidia", model, vram, True, True, compute_capability=cc, driver_version=driver)


@pytest.mark.parametrize("gpu, system, machine, level, words", [
    (_nv(), "Linux", "x86_64", le.SUPPORTED, ["vLLM runs here", "RTX 3080", "10 GB", "llama.cpp"]),
    (_nv(cc="7.5"), "Linux", "aarch64", le.SUPPORTED, ["vLLM runs here"]),
    (_nv(cc="9.0"), "Linux", "x86_64", le.SUPPORTED, ["vLLM runs here"]),
    (_nv(cc="7.0"), "Linux", "x86_64", le.UNSUPPORTED, ["7.5", "7.0", "llama.cpp runs here"]),
    (_nv(cc="6.1"), "Linux", "x86_64", le.UNSUPPORTED, ["6.1", "llama.cpp runs here"]),
    (_nv(cc=""), "Linux", "x86_64", le.UNSUPPORTED, ["compute capability", "llama.cpp runs here"]),
    pytest.param(_nv(driver="575.57.08"), "Linux", "x86_64", le.UNSUPPORTED,
                 ["CUDA 13.0", "driver 580 or newer", "575.57.08", "llama.cpp runs here"],
                 id="driver-575"),
    pytest.param(_nv(driver="579.99"), "Linux", "x86_64", le.UNSUPPORTED, ["579.99"],
                 id="driver-579"),
    pytest.param(_nv(driver="580.65.06"), "Linux", "x86_64", le.SUPPORTED, ["vLLM runs here"],
                 id="driver-580"),
    pytest.param(_nv(driver="590.44.01"), "Linux", "x86_64", le.SUPPORTED, ["vLLM runs here"],
                 id="driver-590"),
    pytest.param(_nv(driver="[N/A]"), "Linux", "x86_64", le.SUPPORTED, ["vLLM runs here"],
                 id="driver-unreadable"),
    (GPUInfo("intel", "Arc A770", 16384), "Linux", "x86_64", le.LATER,
     ["Intel", "Python 3.12", "llama.cpp runs here"]),
    (None, "Linux", "x86_64", le.UNSUPPORTED, ["CPU", "llama.cpp"]),
    (GPUInfo("apple", "M2", 10240), "Darwin", "arm64", le.LATER,
     ["vLLM-Metal", "llama.cpp (Metal) runs here"]),
    (None, "Darwin", "x86_64", le.UNSUPPORTED, ["no build", "llama.cpp runs here"]),
    (_nv(), "Windows", "AMD64", le.LATER, ["Windows", "WSL", "llama.cpp runs here"]),
    (_nv(), "Linux", "riscv64", le.UNSUPPORTED, ["no build", "llama.cpp runs here"]),
    (_nv(), "FreeBSD", "amd64", le.UNSUPPORTED, ["no build"]),
])
def test_support_matrix(gpu, system, machine, level, words):
    """Exactly Linux x86_64/aarch64 with NVIDIA >= 7.5 is supported; every other row gets its
    level and a sentence that names the reason and what runs instead."""
    verdict = le.support(gpu=gpu, system=system, machine=machine, python=(3, 13), glibc="2.39")
    assert verdict.level == level, verdict
    for word in words:
        assert word in verdict.reason, (word, verdict.reason)
    assert verdict.supported is (level == le.SUPPORTED)


@pytest.mark.parametrize("machine, glibc, code", [
    ("x86_64", "2.30", "glibc_old"), ("x86_64", "2.31", "supported"),
    ("x86_64", "2.39", "supported"), ("aarch64", "2.33", "glibc_old"),
    ("aarch64", "2.34", "supported"), ("arm64", "2.31", "glibc_old"),
    ("x86_64", "", "glibc_old"), ("aarch64", None, "supported"),
])
def test_glibc_floor_per_arch(machine, glibc, code):
    """pip can install the locked tree only at or above the arch's glibc floor (measured from
    the wheels on PyPI); no glibc at all (musl) is refused, an unknown one is not."""
    verdict = le.support(gpu=_nv(), system="Linux", machine=machine, python=(3, 13), glibc=glibc)
    assert verdict.code == code, verdict
    if code == "glibc_old":
        floor = "2.34" if machine in ("aarch64", "arm64") else "2.31"
        assert verdict.params == {"glibc": glibc, "min_glibc": floor}
        assert f"glibc {floor} or newer" in verdict.reason and "llama.cpp runs here" in verdict.reason
        assert ("musl" in verdict.reason) is (glibc == "")


def test_host_glibc_reads_where_pip_reads(monkeypatch):
    import os
    monkeypatch.setattr(le.sys, "platform", "linux")
    monkeypatch.setattr(os, "confstr", lambda name: "glibc 2.27", raising=False)
    assert le.host_glibc() == "2.27"
    assert le.support(gpu=_nv(), system="Linux", machine="x86_64", python=(3, 13)).code == "glibc_old"

    def musl(name):
        raise ValueError("unrecognized configuration name")
    monkeypatch.setattr(os, "confstr", musl, raising=False)
    assert le.host_glibc() == ""
    monkeypatch.setattr(le.sys, "platform", "win32")
    assert le.host_glibc() is None, "a host that is not Linux has nothing to say about glibc"


def test_support_names_a_python_outside_vllms_range():
    verdict = le.support(gpu=_nv(), system="Linux", machine="x86_64", python=(3, 10), glibc="2.39")
    assert verdict.level == le.UNSUPPORTED and "3.10" in verdict.reason


def _fake_smi(monkeypatch, answers):
    """nvidia-smi on PATH; each query answered from `answers` by its --query-gpu value."""
    import shutil

    from vaf.core import gpu_detection
    calls = []
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None)
    monkeypatch.setattr(gpu_detection, "_check_cuda_available", lambda: True)

    def run(argv, **kw):
        query = next(a for a in argv if a.startswith("--query-gpu="))
        calls.append(query)
        code, out = answers.get(query, (6, ""))
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")
    monkeypatch.setattr(gpu_detection.subprocess, "run", run)
    return calls


def test_detect_nvidia_reads_compute_cap(monkeypatch):
    from vaf.core.gpu_detection import detect_nvidia_gpu
    calls = _fake_smi(monkeypatch, {
        "--query-gpu=name,memory.total,compute_cap,driver_version":
            (0, "NVIDIA GeForce RTX 3080, 10240, 8.6, 580.196.07\n")})
    gpu = detect_nvidia_gpu()
    assert gpu is not None and gpu.vram_mb == 10240
    assert gpu.compute_capability == "8.6" and gpu.driver_version == "580.196.07"
    assert calls == ["--query-gpu=name,memory.total,compute_cap,driver_version"]
    _fake_smi(monkeypatch, {
        "--query-gpu=name,memory.total,compute_cap,driver_version":
            (0, "NVIDIA GeForce RTX 3080, 10240, 8.6, [N/A]\n")})
    assert detect_nvidia_gpu().driver_version == ""


def test_an_old_driver_still_finds_the_gpu(monkeypatch):
    """A driver that does not know compute_cap refuses the whole query; the GPU must not be
    lost for llama.cpp over the extra column."""
    from vaf.core.gpu_detection import detect_nvidia_gpu
    _fake_smi(monkeypatch, {
        "--query-gpu=name,memory.total,compute_cap,driver_version": (2, ""),
        "--query-gpu=name,memory.total": (0, "Tesla P4, 7680\n")})
    gpu = detect_nvidia_gpu()
    assert gpu is not None and gpu.vram_mb == 7680 and gpu.compute_capability == ""
    assert gpu.driver_version == ""
    assert le.support(gpu=gpu, system="Linux", machine="x86_64").level == le.UNSUPPORTED


def test_the_free_memory_reader(monkeypatch):
    from vaf.core import gpu_detection
    _fake_smi(monkeypatch, {"--query-gpu=memory.free": (0, "8500\n")})
    assert gpu_detection.nvidia_free_vram_mb() == 8500
    _fake_smi(monkeypatch, {})
    assert gpu_detection.nvidia_free_vram_mb() == 0


def test_api_profile_refuses_install_model_and_start(config, monkeypatch, tmp_path):
    """Under the api profile neither the install, nor the model download, nor the start runs
    anything."""
    from vaf.core import vllm_server as vs
    config["inference_profile"] = "api"
    monkeypatch.setattr(vs, "venv_dir", lambda: tmp_path / "bin" / "vllm" / "venv")
    monkeypatch.setattr(le, "support", lambda **kw: le.EngineSupport(le.SUPPORTED, "ok"))

    ran = []

    def boom(*a, **kw):
        # Recorded, not only raised: the code under test swallows exceptions of a download.
        ran.append(a)
        raise RuntimeError("something ran under the api profile")
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "snapshot_download", boom)
    monkeypatch.setattr(vs, "is_installed", lambda: True)
    assert "profile" in vs.install(run=boom)
    assert vs.ensure_model("Qwen/Qwen3-4B-AWQ") == ""
    assert vs.start("Qwen/Qwen3-4B-AWQ", 32768, popen=boom) is False
    assert ran == [], "the api profile let something run"
    assert not (tmp_path / "bin" / "vllm").exists()


@pytest.mark.parametrize("repo, expected", [
    ("Qwen/Qwen3-4B-AWQ", ("hermes", "qwen3")),
    ("Qwen/Qwen3-8B-AWQ", ("hermes", "qwen3")),
    ("Qwen/Qwen3-30B-A3B", ("hermes", "qwen3")),
    ("Qwen/Qwen2.5-7B-Instruct", ("hermes", "")),
    ("Qwen/QwQ-32B", ("hermes", "deepseek_r1")),
    ("NousResearch/Hermes-3-Llama-3.1-8B", ("hermes", "")),
    ("mistralai/Mistral-7B-Instruct-v0.3", ("mistral", "")),
    ("openai/gpt-oss-20b", ("openai", "")),
    ("Qwen/Qwen3-Coder-30B-A3B-Instruct", (None, "")),
    ("Qwen/Qwen3.5-4B", (None, "")),
    ("meta-llama/Llama-3.1-8B-Instruct", (None, "")),
])
def test_parsers_for_families(repo, expected):
    assert le.parsers_for(repo, {}) == expected


def test_parser_overrides():
    cfg = {"vllm_tool_call_parser": "llama3_json", "vllm_reasoning_parser": "none"}
    assert le.parsers_for("Qwen/Qwen3-4B-AWQ", cfg) == ("llama3_json", "")
    assert le.parsers_for("meta-llama/Llama-3.1-8B-Instruct",
                          {"vllm_tool_call_parser": "llama3_json"}) == ("llama3_json", "")
    assert le.parsers_for("Qwen/Qwen2.5-7B", {"vllm_reasoning_parser": "qwen3"}) == ("hermes", "qwen3")


def test_unknown_family_refuses(config, monkeypatch, tmp_path):
    """An agent without tool calls cannot work: an unknown family without an override is
    refused before a launch, with the setting named; a parser that is no plain name too."""
    from vaf.core import vllm_server as vs
    assert "vllm_tool_call_parser" in le.parser_refusal("x/unknown-7b", None, "")
    assert le.parser_refusal("Qwen/Qwen3-4B-AWQ", "hermes", "qwen3") == ""
    assert "vllm_tool_call_parser" in le.parser_refusal("x/y", "--trust-remote-code", "")
    assert "vllm_reasoning_parser" in le.parser_refusal("x/y", "hermes", "a b")

    errors = []
    monkeypatch.setattr(vs, "is_installed", lambda: True)
    monkeypatch.setattr(vs, "is_current", lambda: True)
    monkeypatch.setattr(vs, "local_snapshot", lambda repo: str(tmp_path))
    monkeypatch.setattr("vaf.cli.ui.UI.error", staticmethod(lambda text, *a, **kw: errors.append(text)))

    def no_launch(*a, **kw):
        raise AssertionError("launched without a tool-call parser")
    assert vs.start("someone/unknown-model-7b", 32768, popen=no_launch) is False
    assert any("vllm_tool_call_parser" in e for e in errors), errors


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


def test_local_token_count_shapes(monkeypatch):
    """The request in the running engine's shape, both answers read: vLLM's count, llama.cpp's
    token list; None when the server cannot say."""
    import requests
    sent = []

    def post(url, json=None, timeout=None):
        sent.append((url, json))
        if "prompt" in json:
            return _Resp(200, {"count": 7, "max_model_len": 32768, "tokens": [1, 2]})
        return _Resp(200, {"tokens": [1, 2, 3]})
    monkeypatch.setattr(requests, "post", post)
    assert le.local_token_count("hallo welt", engine="vllm") == 7
    assert sent[-1] == ("http://127.0.0.1:8080/tokenize", {"prompt": "hallo welt"})
    assert le.local_token_count("hallo welt", engine="llama") == 3
    assert sent[-1] == ("http://127.0.0.1:8080/tokenize", {"content": "hallo welt"})

    monkeypatch.setattr(requests, "post", lambda *a, **kw: _Resp(404, {}))
    assert le.local_token_count("x", engine="vllm") is None
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _Resp(200, {"other": 1}))
    assert le.local_token_count("x", engine="llama") is None

    def down(*a, **kw):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(requests, "post", down)
    assert le.local_token_count("x", engine="llama") is None


def test_local_context_window_only_under_vllm(config, monkeypatch):
    import requests
    from vaf.core import vllm_server
    asked = []

    def get(url, timeout=None):
        asked.append(url)
        return _Resp(200, {"data": [{"id": "Qwen/Qwen3-4B-AWQ", "owned_by": "vllm",
                                     "max_model_len": 24576}]})
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(vllm_server, "running_pid", lambda: 4242)
    le._window_cache.clear()
    assert le.local_context_window() is None and asked == [], "llama.cpp: no request at all"
    config["local_engine"] = "vllm"
    assert le.local_context_window() == 24576
    assert le.local_context_window() == 24576 and len(asked) == 1, "remembered per server"
    le._window_cache.clear()


def test_reasoning_text_both_fields():
    assert le.reasoning_text({"reasoning_content": "a"}) == "a"
    assert le.reasoning_text({"reasoning": "b"}) == "b"
    assert le.reasoning_text({"reasoning_content": None, "reasoning": "b"}) == "b"
    assert le.reasoning_text({"content": "x"}) == ""
    assert le.reasoning_text(SimpleNamespace(reasoning_content=None, reasoning="c")) == "c"
    assert le.reasoning_text(SimpleNamespace(reasoning_content="d")) == "d"
    assert le.reasoning_text(None) == ""
    # Another provider's own `reasoning` field is not read.
    assert le.reasoning_text({"reasoning": "b"}, local=False) == ""
    assert le.reasoning_text(SimpleNamespace(reasoning="c"), local=False) == ""
    assert le.reasoning_text({"reasoning_content": "a"}, local=False) == "a"


@pytest.mark.parametrize("vram_gb, expected", [
    (4, "Qwen/Qwen3-4B-AWQ"), (10, "Qwen/Qwen3-4B-AWQ"), (11, "Qwen/Qwen3-4B-AWQ"),
    (12282 / 1024, "Qwen/Qwen3-8B-AWQ"), (12, "Qwen/Qwen3-8B-AWQ"), (24, "Qwen/Qwen3-8B-AWQ"),
])
def test_default_vllm_model_by_vram(vram_gb, expected):
    assert le.default_vllm_model(vram_gb) == expected


def test_auto_resolves_through_the_default(monkeypatch):
    monkeypatch.setattr(le, "default_vllm_model", lambda vram_gb=None: "Qwen/Qwen3-8B-AWQ")
    assert le.resolve_vllm_model({"vllm_model": "auto"}) == "Qwen/Qwen3-8B-AWQ"
    assert le.resolve_vllm_model({"vllm_model": " "}) == "Qwen/Qwen3-8B-AWQ"
    assert le.resolve_vllm_model({}) == "Qwen/Qwen3-8B-AWQ"
    assert le.resolve_vllm_model({"vllm_model": "Qwen/Qwen3-4B-AWQ"}) == "Qwen/Qwen3-4B-AWQ"


def test_engine_keys_admin_only():
    """Every engine key is a server launch decision for the whole instance."""
    from vaf.core.config import Config
    keys = ["local_engine"] + [k for k in Config.DEFAULTS if k.startswith("vllm_")]
    assert len(keys) == 5
    for key in keys:
        assert Config.is_global_config_key(key), key
    assert Config.DEFAULTS["local_engine"] == "llama"
    assert Config.DEFAULTS["vllm_model"] == "auto"
    assert Config.DEFAULTS["vllm_tool_call_parser"] == ""
    assert Config.DEFAULTS["vllm_reasoning_parser"] == ""
    assert Config.DEFAULTS["vllm_gpu_memory_utilization"] == 0


@pytest.mark.parametrize("key, engine, restarts", [
    ("model", "llama", True), ("gpu_layers", "llama", True), ("n_ctx", "llama", True),
    ("model", "vllm", False), ("gpu_layers", "vllm", False), ("n_ctx", "vllm", True),
    ("vllm_model", "vllm", True), ("vllm_model", "llama", False),
    ("local_engine", "llama", True), ("local_engine", "vllm", True), ("temperature", "llama", False),
])
def test_only_keys_the_running_engine_reads_restart_it(key, engine, restarts):
    """A GGUF pick under vLLM must not cost a multi-minute vLLM reload."""
    assert le.restarts_running_engine(key, {"local_engine": engine}) is restarts


def test_conflicts_under_vllm(monkeypatch):
    from vaf.core import vllm_server
    monkeypatch.setattr(le, "support", lambda **kw: le.EngineSupport(le.LATER, "Windows here."))
    monkeypatch.setattr(vllm_server, "is_installed", lambda: False)
    assert le.conflicts({"local_engine": "llama", "vision_provider": "local"}) == []
    out = " ".join(le.conflicts({"local_engine": "vllm", "provider": "openai",
                                 "voice_agent_provider": "local", "voice_agent_model": "x/y.gguf",
                                 "vision_provider": "local"}))
    for word in ("Windows here.", "vaf engine vllm", "voice_agent_provider", "vision_provider",
                 "voice_agent_model"):
        assert word in out, word
    # An API voice provider uses its own model; that is no conflict.
    assert not any("voice_agent_model" in c for c in le.conflicts(
        {"local_engine": "vllm", "voice_agent_provider": "openai", "voice_agent_model": "gpt-x"}))


def test_import_stays_light():
    """local_engine is read on hot paths (every token count); importing it pulls in no HTTP
    client or GPU probe."""
    code = ("import sys; import vaf.core.local_engine; "
            "print(int('requests' in sys.modules), int('vaf.core.gpu_detection' in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split()[-1] == "0", out.stdout


# ── codes for the translated surfaces, the status row, the health row ─────────────────

# The sentences main printed, recorded literally: a reason must never change while it gains
# its code (the terminal, `vaf health` and the logs read the sentence).
_REASONS = {
    "supported": "vLLM runs here (NVIDIA GeForce RTX 3080, 10 GB). llama.cpp stays the lighter "
                 "choice for small GPUs and quick starts.",
    "nvidia_too_old": "vLLM needs compute capability 7.5 or newer; this GPU has 6.1. llama.cpp "
                      "runs here.",
    "nvidia_driver_old": "vLLM 0.32.0 is built for CUDA 13.0 and needs NVIDIA driver 580 or "
                         "newer; this one is 575.57.08. Update the NVIDIA driver and restart "
                         "VAF. llama.cpp runs here.",
    "glibc_old": "vLLM 0.32.0 needs glibc 2.31 or newer on this processor; this system has glibc "
                 "2.28. A newer Linux runs it (Ubuntu 20.04, Debian 11 or RHEL 9 and later). "
                 "llama.cpp runs here.",
    "nvidia_unknown": "VAF could not read this GPU's compute capability from nvidia-smi (the "
                      "driver may be too old for vLLM's CUDA build). llama.cpp runs here.",
    "intel": "vLLM's Intel GPU support is basic and needs Python 3.12; VAF does not manage it "
             "yet. llama.cpp runs here.",
    "no_gpu": "vLLM's CPU backend is basic (no 4-bit GGUF-style quantization); llama.cpp is the "
              "better engine for a CPU.",
    "apple": "vLLM on Apple GPUs is the community plugin vLLM-Metal (MLX models); VAF does not "
             "manage it yet. llama.cpp (Metal) runs here.",
    "no_build": "vLLM has no build for this machine. llama.cpp runs here.",
    "windows": "vLLM has no native Windows support (WSL only); VAF does not manage it yet. "
               "llama.cpp runs here.",
    "no_build_gpu": "vLLM has no build for this GPU. llama.cpp runs here.",
    "python": "vLLM needs Python 3.11 to 3.14; VAF runs on 3.10 here. llama.cpp runs here.",
}


@pytest.mark.parametrize("gpu, system, machine, python, level, code, params", [
    (_nv(), "Linux", "x86_64", (3, 13), le.SUPPORTED, "supported",
     {"gpu": "GeForce RTX 3080", "vram_gb": "10"}),
    (_nv(cc="6.1"), "Linux", "x86_64", (3, 13), le.UNSUPPORTED, "nvidia_too_old", {"capability": "6.1"}),
    (_nv(cc=""), "Linux", "x86_64", (3, 13), le.UNSUPPORTED, "nvidia_unknown", {}),
    (_nv(driver="575.57.08"), "Linux", "x86_64", (3, 13), le.UNSUPPORTED, "nvidia_driver_old",
     {"driver": "575.57.08"}),
    (_nv(), "Linux", "x86_64", (3, 13), le.UNSUPPORTED, "glibc_old",
     {"glibc": "2.28", "min_glibc": "2.31"}),
    (GPUInfo("intel", "Arc A770", 16384), "Linux", "x86_64", (3, 13), le.LATER, "intel", {}),
    (None, "Linux", "x86_64", (3, 13), le.UNSUPPORTED, "no_gpu", {}),
    (GPUInfo("apple", "M2", 10240), "Darwin", "arm64", (3, 13), le.LATER, "apple", {}),
    (None, "Darwin", "x86_64", (3, 13), le.UNSUPPORTED, "no_build", {}),
    (_nv(), "Windows", "AMD64", (3, 13), le.LATER, "windows", {}),
    (_nv(), "Linux", "riscv64", (3, 13), le.UNSUPPORTED, "no_build", {}),
    (GPUInfo("qualcomm", "X", 1024), "Linux", "x86_64", (3, 13), le.UNSUPPORTED, "no_build_gpu", {}),
    (_nv(), "Linux", "x86_64", (3, 10), le.UNSUPPORTED, "python", {"python": "3.10"}),
])
def test_support_codes_and_unchanged_reasons(gpu, system, machine, python, level, code, params):
    """Every row of the platform table keeps its level and its sentence byte for byte and
    gains a code with its parameters. MUTATION: swap the codes intel/apple, or change one
    sentence - red."""
    glibc = "2.28" if code == "glibc_old" else "2.39"
    verdict = le.support(gpu=gpu, system=system, machine=machine, python=python, glibc=glibc)
    assert (verdict.level, verdict.code, dict(verdict.params)) == (level, code, params)
    assert verdict.reason == _REASONS[code]
    assert code in le.SUPPORT_CODES


@pytest.fixture
def vllm_box(monkeypatch, tmp_path, config):
    from vaf.core import backend
    from vaf.core import vllm_server as vs
    monkeypatch.setattr(vs, "venv_dir", lambda: tmp_path / "bin" / "vllm" / "venv")
    monkeypatch.setattr(vs, "backend", lambda: vs.CUDA)
    box = {"supported": True, "version": "0.32.0", "pid": None, "health": None, "loaded": None,
           "current": True}
    monkeypatch.setattr(le, "support", lambda **kw: le.EngineSupport(
        le.SUPPORTED, "vLLM runs here.", "supported") if box["supported"] else le.EngineSupport(
        le.LATER, "AMD here.", "amd", {}))
    monkeypatch.setattr(vs, "installed_version", lambda: box["version"])
    monkeypatch.setattr(vs, "is_installed", lambda: box["version"] is not None)
    monkeypatch.setattr(vs, "is_current", lambda: box["version"] is not None and box["current"])
    monkeypatch.setattr(vs, "running_pid", lambda: box["pid"])
    monkeypatch.setattr(vs, "health_status", lambda port=8080, timeout=1.0: box["health"])
    monkeypatch.setattr(backend, "get_loaded_model_id", lambda port=8080, timeout=2.0: box["loaded"])
    config["vllm_model"] = "Qwen/Qwen3-4B-AWQ"
    return box


def test_status_row_states(config, vllm_box):
    """Update and Repair's row: llama running/idle, vLLM ok/loading/idle/degraded, off under
    the api profile. MUTATION: report `ok` while vLLM is not installed - red."""
    row = le.status_row(dict(config))
    assert (row["engine"], row["state"], row["code"]) == ("llama", "idle", "llama_idle")
    vllm_box["loaded"] = "qwen3-4b.gguf"
    row = le.status_row(dict(config))
    assert (row["state"], row["code"], row["model"]) == ("ok", "llama_running", "qwen3-4b.gguf")
    config["local_engine"] = "vllm"
    vllm_box["pid"], vllm_box["health"] = 4242, 200
    assert le.status_row(dict(config))["state"] == "ok"
    vllm_box["health"] = None
    row = le.status_row(dict(config))
    assert (row["state"], row["code"]) == ("loading", "vllm_loading")
    vllm_box["pid"] = None
    assert le.status_row(dict(config))["code"] == "vllm_idle"
    # Installed, but a start refuses it (an earlier VAF set it up): never "idle".
    vllm_box["current"] = False
    row = le.status_row(dict(config))
    assert (row["state"], row["code"], row["params"]) == ("degraded", "outdated",
                                                          {"version": "0.32.0"})
    assert "outdated" in le.STATUS_CODES
    vllm_box["current"] = True
    vllm_box["version"] = None
    row = le.status_row(dict(config))
    assert (row["state"], row["code"]) == ("degraded", "not_installed")
    vllm_box["supported"] = False
    row = le.status_row(dict(config))
    assert (row["state"], row["code"], row["params"]["support"]) == ("degraded", "unsupported", "amd")
    config["inference_profile"] = "api"
    assert le.status_row(dict(config))["state"] == "off"
    for row_code in ("profile_api", "llama_running", "vllm_loading", "unsupported"):
        assert row_code in le.STATUS_CODES


def _remember_a_failed_start(monkeypatch, cause="kernels"):
    from vaf.core import vllm_server as vs
    monkeypatch.setattr(vs, "compute_capability", lambda: (8, 6))
    monkeypatch.setattr(vs, "flashinfer_buildable", lambda env=None: False)
    monkeypatch.setattr(vs, "recipe_current", lambda: True)
    signature = vs.start_signature("Qwen/Qwen3-4B-AWQ", 32768)
    vs._remember(signature, cause, vs.cause_text(cause))
    return vs


def test_status_row_vllm_failed(config, vllm_box, monkeypatch):
    """A start that failed and is not run again is not "idle": degraded, vllm_failed, with
    its cause, in the row and in state(). MUTATION: drop the branch - red."""
    config["local_engine"] = "vllm"
    config["n_ctx"] = 32768
    vs = _remember_a_failed_start(monkeypatch)
    row = le.status_row(dict(config))
    assert (row["state"], row["code"]) == ("degraded", "vllm_failed"), row
    assert row["params"]["reason_code"] == "kernels" and vs.SERVER_LOG in row["text"]
    assert "vllm_failed" in le.STATUS_CODES
    monkeypatch.setattr(le, "_cfg", lambda: dict(config))
    monkeypatch.setattr(vs, "local_snapshot", lambda repo: "")
    failure = le.state()["vllm"]["start_failure"]
    assert failure == {"code": "kernels", "text": vs.cause_text("kernels")}
    vs.clear_start_failure()
    assert le.status_row(dict(config))["code"] == "vllm_idle"
    assert le.state()["vllm"]["start_failure"] is None


def test_engine_check_text_unchanged(config, vllm_box):
    """`vaf health` says what it said before it read the shared row. MUTATION: change one
    sentence - red."""
    from vaf.core.health import engine_check
    assert engine_check() is None
    config["local_engine"] = "vllm"
    assert engine_check() == {"name": "engine", "status": "ok", "reason":
                              "vLLM 0.32.0 installed, model Qwen/Qwen3-4B-AWQ, server idle."}
    vllm_box["pid"] = 4242
    assert engine_check()["reason"] == "vLLM 0.32.0 installed, model Qwen/Qwen3-4B-AWQ, server running."
    vllm_box["current"] = False
    assert engine_check() == {"name": "engine", "status": "degraded", "reason":
                              "vLLM is the local engine, but its environment was set up by an "
                              "earlier VAF and cannot start: run `vaf engine vllm` to rebuild it."}
    vllm_box["current"] = True
    vllm_box["version"] = None
    assert engine_check() == {"name": "engine", "status": "degraded", "reason":
                              "vLLM is the local engine but is not installed: run `vaf engine vllm`."}
    vllm_box["supported"] = False
    assert engine_check()["reason"] == "vLLM is the local engine, but: AMD here."
    config["inference_profile"] = "api"
    assert engine_check()["reason"] == "vLLM is the local engine, but: AMD here.", \
        "the api profile does not hide the row health always showed"


def test_conflict_codes_keep_their_sentences(config, monkeypatch):
    from vaf.core import vllm_server as vs
    monkeypatch.setattr(le, "support", lambda **kw: le.EngineSupport(le.LATER, "AMD here.", "amd"))
    monkeypatch.setattr(vs, "is_installed", lambda: False)
    config.update(local_engine="vllm", provider="openai", voice_agent_provider="local",
                  vision_provider="local", voice_agent_model="voice.gguf")
    items = le.conflict_items()
    # not_installed and outdated exclude each other: one environment is either missing or not.
    assert [code for code, _ in items] == [c for c in le.CONFLICT_CODES if c != "outdated"]
    assert le.conflicts() == [text for _, text in items]
    assert items[0][1] == 'local_engine is "vllm", but: AMD here.'
    monkeypatch.setattr(vs, "is_installed", lambda: True)
    monkeypatch.setattr(vs, "is_current", lambda: False)
    items = le.conflict_items()
    assert [code for code, _ in items] == [c for c in le.CONFLICT_CODES if c != "not_installed"]
    assert dict(items)["outdated"] == le.OUTDATED_TEXT and "vaf engine vllm" in le.OUTDATED_TEXT
    monkeypatch.setattr(vs, "is_current", lambda: True)
    assert "outdated" not in dict(le.conflict_items()), "a current environment is no conflict"


def test_without_engine_keys():
    payload = {"local_engine": "llama", "vllm_model": "a/b", "vllm_gpu_memory_utilization": 0.9,
               "model": "x.gguf", "provider": "local"}
    assert le.without_engine_keys(payload) == {"model": "x.gguf", "provider": "local"}
    same = {"model": "x.gguf"}
    assert le.without_engine_keys(same) is same


# ── AMD: vLLM's ROCm build ─────────────────────────────────────────────────────────

def _amd(gfx="gfx1100", vram=24560, model="AMD Radeon RX 7900 XTX"):
    return GPUInfo("amd", model, vram, True, True, gfx_arch=gfx)


# Every fact of a machine the ROCm build runs on; a row of the table below changes one.
_AMD_OK = {"gpu": _amd(), "system": "Linux", "machine": "x86_64", "python": (3, 13),
           "glibc": "2.39", "rocm": (True, "7.2.3"), "python312": "/usr/bin/python3.12",
           "kfd_access": True}


def test_amd_supported_verdict():
    """A Radeon RX 7900 XTX on Ubuntu 24.04 with ROCm 7.2.3: supported, named once as AMD.
    MUTATION: drop the "AMD " strip, or route AMD to the NVIDIA branch - red."""
    verdict = le.support(**_AMD_OK)
    assert (verdict.level, verdict.code) == (le.SUPPORTED, "supported")
    assert dict(verdict.params) == {"gpu": "Radeon RX 7900 XTX", "vram_gb": "24"}
    assert verdict.reason == ("vLLM runs here (AMD Radeon RX 7900 XTX, 24 GB). llama.cpp stays "
                              "the lighter choice for small GPUs and quick starts.")
    # VAF's own Python range is not the ROCm build's: vLLM runs in its 3.12 environment.
    assert le.support(**{**_AMD_OK, "python": (3, 10)}).code == "supported"


@pytest.mark.parametrize("change, code, params, words", [
    pytest.param({"machine": "aarch64"}, "no_build_gpu", {}, ["no build for this GPU"],
                 id="aarch64"),
    pytest.param({"gpu": _amd(gfx="")}, "amd_unknown", {}, ["architecture", "/sys", "amdgpu driver is loaded"],
                 id="no-gfx"),
    pytest.param({"gpu": _amd(gfx="gfx1030")}, "amd_gfx", {"gfx": "gfx1030"},
                 ["gfx1030", "RX 7900", "MI300"], id="gfx1030"),
    pytest.param({"gpu": _amd(gfx="gfx1030"), "rocm": (False, "")}, "amd_gfx",
                 {"gfx": "gfx1030"}, ["gfx1030"], id="gfx-before-rocm"),
    pytest.param({"rocm": (False, "")}, "amd_rocm_missing", {},
                 ["ROCm 7.2 or newer", "libamdhip64.so.7"], id="no-rocm"),
    pytest.param({"rocm": (True, "7.1.1")}, "amd_rocm_old", {"rocm": "7.1.1"},
                 ["ROCm 7.2 or newer", "7.1.1", "Update ROCm"], id="rocm-7.1"),
    pytest.param({"rocm": (True, "6.4.0"), "kfd_access": False}, "amd_rocm_old",
                 {"rocm": "6.4.0"}, ["6.4.0"], id="rocm-before-kfd"),
    pytest.param({"rocm": (True, "")}, "supported", {"gpu": "Radeon RX 7900 XTX", "vram_gb": "24"},
                 ["vLLM runs here"], id="rocm-version-unreadable"),
    pytest.param({"rocm": (True, "7.10.0")}, "supported",
                 {"gpu": "Radeon RX 7900 XTX", "vram_gb": "24"}, ["vLLM runs here"],
                 id="rocm-7.10"),
    pytest.param({"kfd_access": False}, "amd_no_access", {}, ["/dev/kfd", "render and video"],
                 id="no-kfd"),
    pytest.param({"kfd_access": False, "glibc": "2.35"}, "amd_no_access", {}, ["/dev/kfd"],
                 id="kfd-before-glibc"),
    pytest.param({"glibc": "2.38"}, "amd_glibc_old", {"glibc": "2.38", "min_glibc": "2.39"},
                 ["glibc 2.39 or newer", "glibc 2.38", "Ubuntu 24.04"], id="glibc-2.38"),
    pytest.param({"glibc": ""}, "amd_glibc_old", {"glibc": "", "min_glibc": "2.39"},
                 ["musl"], id="musl"),
    pytest.param({"glibc": "2.38", "python312": ""}, "amd_glibc_old",
                 {"glibc": "2.38", "min_glibc": "2.39"}, ["2.38"], id="glibc-before-python"),
    pytest.param({"python312": ""}, "amd_python", {}, ["Python 3.12", "python3.12-venv", "pyenv"],
                 id="no-python312"),
    pytest.param({"gpu": _amd(gfx="gfx1151", vram=98304, model="AMD Radeon 8060S")},
                 "supported", {"gpu": "Radeon 8060S", "vram_gb": "96"}, ["AMD Radeon 8060S"],
                 id="ryzen-ai-max"),
    pytest.param({"gpu": _amd(gfx="gfx90a", model="AMD Instinct MI210")}, "supported",
                 {"gpu": "Instinct MI210", "vram_gb": "24"}, ["MI210"], id="mi210"),
])
def test_amd_verdict_rows(change, code, params, words):
    """One row per check of the ROCm verdict, in its order of precedence (the first failing
    check wins); every refusal ends with what runs instead. MUTATION: swap the order of
    amd_rocm_missing and amd_gfx; refuse an unreadable ROCm version; remove gfx1151 from
    ROCM_GFX - red."""
    verdict = le.support(**{**_AMD_OK, **change})
    assert (verdict.code, dict(verdict.params)) == (code, params), verdict
    assert verdict.level == (le.SUPPORTED if code == "supported" else le.UNSUPPORTED)
    for word in words:
        assert word in verdict.reason, (word, verdict.reason)
    if code != "supported":
        assert verdict.reason.endswith("llama.cpp runs here."), verdict.reason
    assert code in le.SUPPORT_CODES


def test_amd_is_no_longer_a_later_code():
    """The `amd` later verdict is gone from the codes and from every catalogue."""
    import json
    from pathlib import Path
    assert "amd" not in le.SUPPORT_CODES
    messages = Path(__file__).resolve().parents[1] / "web" / "messages"
    for path in sorted(messages.glob("*.json")):
        support = json.loads(path.read_bytes())["localEngine"]["support"]
        assert "amd" not in support, path.name


def test_amd_facts_are_detected_when_left_out(monkeypatch):
    """Left out, the ROCm runtime, the Python 3.12 and the /dev/kfd access are read from this
    host (gpu_detection.rocm_runtime, vllm_server.rocm_python, os.access)."""
    import os

    from vaf.core import gpu_detection, vllm_server
    seen = []
    monkeypatch.setattr(gpu_detection, "rocm_runtime", lambda: seen.append("rocm") or (True, "7.2.3"))
    monkeypatch.setattr(vllm_server, "rocm_python", lambda: seen.append("py") or "/x/python3.12")
    monkeypatch.setattr(os, "access", lambda path, mode: seen.append(path) or True)
    verdict = le.support(gpu=_amd(), system="Linux", machine="x86_64", glibc="2.39")
    assert verdict.code == "supported" and seen == ["rocm", "/dev/kfd", "py"]


def test_support_class_amd_no_gpu_is_inconclusive(monkeypatch):
    """"No GPU" while rocm-smi or amd-smi is installed is not kept: rocm-smi's probe reports a
    timeout as no GPU, like nvidia-smi's. MUTATION: drop the rocm-smi/amd-smi clause - red."""
    import shutil

    import vaf.core.gpu_detection as gd
    monkeypatch.setattr(gd, "get_primary_gpu", lambda: None)
    for tool in ("rocm-smi", "amd-smi"):
        monkeypatch.setattr(le, "_SUPPORT_CLASS", None)
        monkeypatch.setattr(le, "_SUPPORT_RETRY_AT", 0.0)
        monkeypatch.setattr(shutil, "which", lambda name, tool=tool: f"/usr/bin/{tool}"
                            if name == tool else None)
        assert le.support_class() is None, tool
    monkeypatch.setattr(le, "_SUPPORT_CLASS", None)
    monkeypatch.setattr(le, "_SUPPORT_RETRY_AT", 0.0)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert le.support_class() == (le.UNSUPPORTED, "no_gpu")


@pytest.mark.parametrize("build, gb", [("cuda", "2.8"), ("rocm", "3.3")])
def test_download_size_follows_the_backend(vllm_box, monkeypatch, build, gb):
    """The picker's size and the install question are the build's own lower bound.
    MUTATION: state() reading the CUDA constant - red."""
    from vaf.core import vllm_server as vs
    monkeypatch.setattr(vs, "backend", lambda: build)
    monkeypatch.setattr(vs, "local_snapshot", lambda repo: "")
    monkeypatch.setattr(le, "default_vllm_model", lambda vram_gb=None: "Qwen/Qwen3-4B-AWQ")
    vllm_box["version"] = None
    assert f"{le.state()['vllm']['download_gb_at_least']:.1f}" == gb
    result = le.apply("vllm")
    assert (result.status, result.params["download_gb"]) == ("needs_install", gb)
    assert f"about {gb} GB" in result.text
