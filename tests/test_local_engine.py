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
- drop the two-field retry for an old driver -> test_an_old_driver_still_finds_the_gpu;
- remove the profile check in `vllm_server.ensure_model` (or `install`) ->
  test_api_profile_refuses_install_model_and_start;
- map Qwen to llama3_json, or default an unknown family to hermes ->
  test_parsers_for_families / test_unknown_family_refuses;
- read only `tokens` in `local_token_count` -> test_local_token_count_shapes;
- read only `reasoning_content` in `reasoning_text` -> test_reasoning_text_both_fields;
- swap the two default models -> test_default_vllm_model_by_vram;
- remove `vllm_` from the admin-only prefixes -> test_engine_keys_admin_only.
"""
import subprocess
import sys
from types import SimpleNamespace

import pytest

from vaf.core import local_engine as le
from vaf.core.gpu_detection import GPUInfo


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


def _nv(cc="8.6", vram=10240, model="NVIDIA GeForce RTX 3080"):
    return GPUInfo("nvidia", model, vram, True, True, compute_capability=cc)


@pytest.mark.parametrize("gpu, system, machine, level, words", [
    (_nv(), "Linux", "x86_64", le.SUPPORTED, ["vLLM runs here", "RTX 3080", "10 GB", "llama.cpp"]),
    (_nv(cc="7.5"), "Linux", "aarch64", le.SUPPORTED, ["vLLM runs here"]),
    (_nv(cc="9.0"), "Linux", "x86_64", le.SUPPORTED, ["vLLM runs here"]),
    (_nv(cc="7.0"), "Linux", "x86_64", le.UNSUPPORTED, ["7.5", "7.0", "llama.cpp runs here"]),
    (_nv(cc="6.1"), "Linux", "x86_64", le.UNSUPPORTED, ["6.1", "llama.cpp runs here"]),
    (_nv(cc=""), "Linux", "x86_64", le.UNSUPPORTED, ["compute capability", "llama.cpp runs here"]),
    (GPUInfo("amd", "Radeon RX 7900", 24576), "Linux", "x86_64", le.LATER,
     ["ROCm", "Python 3.12", "llama.cpp (Vulkan) runs here"]),
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
    verdict = le.support(gpu=gpu, system=system, machine=machine, python=(3, 13))
    assert verdict.level == level, verdict
    for word in words:
        assert word in verdict.reason, (word, verdict.reason)
    assert verdict.supported is (level == le.SUPPORTED)


def test_support_names_a_python_outside_vllms_range():
    verdict = le.support(gpu=_nv(), system="Linux", machine="x86_64", python=(3, 10))
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
        "--query-gpu=name,memory.total,compute_cap": (0, "NVIDIA GeForce RTX 3080, 10240, 8.6\n")})
    gpu = detect_nvidia_gpu()
    assert gpu is not None and gpu.vram_mb == 10240
    assert gpu.compute_capability == "8.6"
    assert calls == ["--query-gpu=name,memory.total,compute_cap"]


def test_an_old_driver_still_finds_the_gpu(monkeypatch):
    """A driver that does not know compute_cap refuses the whole query; the GPU must not be
    lost for llama.cpp over the extra column."""
    from vaf.core.gpu_detection import detect_nvidia_gpu
    _fake_smi(monkeypatch, {
        "--query-gpu=name,memory.total,compute_cap": (2, ""),
        "--query-gpu=name,memory.total": (0, "Tesla P4, 7680\n")})
    gpu = detect_nvidia_gpu()
    assert gpu is not None and gpu.vram_mb == 7680 and gpu.compute_capability == ""
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
    "nvidia_unknown": "VAF could not read this GPU's compute capability from nvidia-smi (the "
                      "driver may be too old for vLLM's CUDA build). llama.cpp runs here.",
    "amd": "vLLM supports some AMD GPUs through ROCm 6.3+, with Python 3.12 builds only; VAF "
           "does not manage that yet. llama.cpp (Vulkan) runs here.",
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
    (GPUInfo("amd", "Radeon RX 7900", 24576), "Linux", "x86_64", (3, 13), le.LATER, "amd", {}),
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
    gains a code with its parameters. MUTATION: swap the codes amd/intel, or change one
    sentence - red."""
    verdict = le.support(gpu=gpu, system=system, machine=machine, python=python)
    assert (verdict.level, verdict.code, dict(verdict.params)) == (level, code, params)
    assert verdict.reason == _REASONS[code]
    assert code in le.SUPPORT_CODES


@pytest.fixture
def vllm_box(monkeypatch, tmp_path, config):
    from vaf.core import backend
    from vaf.core import vllm_server as vs
    monkeypatch.setattr(vs, "venv_dir", lambda: tmp_path / "bin" / "vllm" / "venv")
    box = {"supported": True, "version": "0.32.0", "pid": None, "health": None, "loaded": None}
    monkeypatch.setattr(le, "support", lambda **kw: le.EngineSupport(
        le.SUPPORTED, "vLLM runs here.", "supported") if box["supported"] else le.EngineSupport(
        le.LATER, "AMD here.", "amd", {}))
    monkeypatch.setattr(vs, "installed_version", lambda: box["version"])
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
    assert [code for code, _ in items] == list(le.CONFLICT_CODES)
    assert le.conflicts() == [text for _, text in items]
    assert items[0][1] == 'local_engine is "vllm", but: AMD here.'


def test_without_engine_keys():
    payload = {"local_engine": "llama", "vllm_model": "a/b", "vllm_gpu_memory_utilization": 0.9,
               "model": "x.gguf", "provider": "local"}
    assert le.without_engine_keys(payload) == {"model": "x.gguf", "provider": "local"}
    same = {"model": "x.gguf"}
    assert le.without_engine_keys(same) is same
