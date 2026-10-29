# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Which engine runs the local model: llama.cpp (GGUF) or a VAF-managed vLLM.

``local_engine`` is the engine of provider ``local``, chosen with `vaf engine`:

- ``llama`` (the default, and what every installation ran before the key existed): the
  llama.cpp ``llama-server`` with a GGUF from ``models/`` (vaf/core/backend.py).
- ``vllm``: vLLM, installed on request into its own environment under ``bin/vllm/``, with
  a Hugging Face model in safetensors form (vaf/core/vllm_server.py).

vLLM is an ENGINE of ``local``, not a provider: every rule that keeps one local inference
server on the machine keys on ``provider == "local"``, and as an engine vLLM inherits all of
them unchanged. It listens on the local inference port (127.0.0.1:8080) and speaks the same
OpenAI endpoints, so every consumer of the local server reaches it without a change; the
two differences in wire format live here, once each: the ``/tokenize`` contract
(``local_token_count``) and the reasoning field of a reply (``reasoning_text``).

The choke points dispatch on ``current()``: ``ServerManager.start_server``, ``stop_server``,
``ensure_model_present``, ``ensure_local_model``, ``stop_local_server`` and
``Agent.load_model``. A start hands the port over: the engine that is not chosen is stopped,
and its process has exited, before the chosen one is spawned.

Platform verdicts (``support()``), from the vLLM documentation:

- Linux on x86_64 or aarch64 with an NVIDIA GPU of compute capability 7.5 or newer:
  supported. vLLM's prebuilt wheel carries CUDA 12.9 binaries and installs into a fresh
  environment of VAF's own Python (vLLM 0.32.0 accepts 3.11 to 3.14).
  https://docs.vllm.ai/en/latest/getting_started/installation/gpu/
- NVIDIA below 7.5 (Pascal, Volta): unsupported by vLLM itself.
- AMD (ROCm 6.3+, selected Instinct and Radeon cards) and Intel GPUs (XPU, "basic model
  inference and serving"): vLLM supports them with builds for Python 3.12 only, which is
  not the Python VAF runs on; VAF does not manage them yet (later).
- Apple Silicon: the GPU path is vLLM-Metal, "a community-maintained hardware plugin" on
  MLX; the CPU build on macOS is experimental and built from source (later).
  https://docs.vllm.ai/en/latest/getting_started/installation/cpu/
- Windows: "vLLM does not support Windows natively" (WSL only); later.
- No GPU: vLLM's CPU backend is basic and has no 4-bit GGUF-style quantization; llama.cpp
  is the better engine for a CPU (unsupported).
https://docs.vllm.ai/en/latest/getting_started/installation/ lists the platforms.

Named boundaries, each deliberate:

- **Not on the facade, and not per agent.** The engine is the installation's, like
  ``inference_profile``: it is read from the installation's config (``vaf engine`` writes
  it), because the one local server on 127.0.0.1:8080 is the machine's and two agents
  cannot run it on two engines at once. An embedder's ``Agent(config=...)`` override of
  ``local_engine`` therefore does not switch it (the server start reads the installation's
  config); ``vaf engine`` does. No new ``vaf.`` symbol is needed for that; the only readers
  outside the core are ``vaf engine``, ``vaf health`` and the product.
- **A foreign server for the main loop** (an Ollama or vLLM that VAF did not start) is not
  supported: the address is written out instead of read from
  ``Config.get_llama_server_url`` in 42 lines of 13 files (``git grep -E
  "(127\.0\.0\.1|localhost):8080" -- 'vaf/*.py'`` at the commit before this engine,
  comments included), so the main loop always talks to the server VAF manages.
  ``local_api_url`` stays the lane for the API-backend consumers.
- **Local vision under vLLM**: the projector lane is llama.cpp's; under vLLM images reach
  the model only through an API ``vision_provider``.
- **Concurrency**: vLLM batches concurrent requests, but the local worker clamp
  (``max_parallel_local_workers``) stays as it is until that is measured.
- **The install is pinned, not hash-locked**: ``vllm==<PIN>`` from PyPI, its dependency
  tree resolved by pip over TLS (llama.cpp's asset is SHA-256 pinned).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

LLAMA = "llama"
VLLM = "vllm"
ENGINES = (LLAMA, VLLM)
DEFAULT = LLAMA

SUPPORTED = "supported"
LATER = "later"
UNSUPPORTED = "unsupported"

# The oldest NVIDIA architecture vLLM's CUDA build runs on (Turing).
MIN_COMPUTE_CAPABILITY = (7, 5)
# The Python range of the pinned vLLM release (its requires_python: >=3.11,<3.15).
PYTHON_RANGE = ((3, 11), (3, 15))

# The default model of ``vllm_model = "auto"``: total VRAM below the threshold gets the
# 4B (2.67 GB of weights), from it up the 8B (6.10 GB). Both AWQ 4-bit, Apache-2.0, not
# gated, text-only. 11.5 rather than 12: a "12 GB" card reports 12282 MiB as well as 12288.
DEFAULT_MODEL_SMALL = "Qwen/Qwen3-4B-AWQ"
DEFAULT_MODEL_LARGE = "Qwen/Qwen3-8B-AWQ"
LARGE_MODEL_FROM_GB = 11.5

# Keys whose change restarts the running engine (the tray's config hook). Every key that
# starts with ``vllm_`` is a vLLM launch argument.
LLAMA_RESTART_KEYS = ("model", "n_ctx", "gpu_layers")
ENGINE_KEYS = ("local_engine", "vllm_model", "vllm_tool_call_parser", "vllm_reasoning_parser",
               "vllm_gpu_memory_utilization")
RESTART_KEYS = LLAMA_RESTART_KEYS + ENGINE_KEYS

_REPO_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")
_PARSER_NAME = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class EngineSupport:
    level: str
    reason: str

    @property
    def supported(self) -> bool:
        return self.level == SUPPORTED


def current(config: Optional[Dict] = None) -> str:
    """The engine of provider ``local``: ``vllm`` when the value, trimmed and ignoring case,
    is ``vllm``, else ``llama``. A mistyped or unreadable value must never take the GGUF
    engine away from an installation that runs on it."""
    try:
        if config is None:
            from vaf.core.config import Config
            value = Config.get("local_engine", DEFAULT)
        else:
            value = config.get("local_engine", DEFAULT)
        return VLLM if str(value or "").strip().lower() == VLLM else LLAMA
    except Exception:
        return DEFAULT


def is_vllm(config: Optional[Dict] = None) -> bool:
    return current(config) == VLLM


def restarts_running_engine(key: str, config: Optional[Dict] = None) -> bool:
    """True when a change of ``key`` must restart the local engine that runs now. A GGUF
    choice means nothing to vLLM and a vLLM setting nothing to llama.cpp; restarting for
    them would only cost a model load (minutes for vLLM)."""
    if key in ("local_engine", "n_ctx"):
        return True
    if key in LLAMA_RESTART_KEYS:
        return current(config) == LLAMA
    if key.startswith("vllm_"):
        return current(config) == VLLM
    return False


def _parse_capability(text: str) -> Optional[Tuple[int, int]]:
    m = re.fullmatch(r"\s*(\d+)\.(\d+)\s*", str(text or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


_DETECT = object()


def support(gpu: Any = _DETECT, system: Optional[str] = None, machine: Optional[str] = None,
            python: Optional[Tuple[int, int]] = None) -> EngineSupport:
    """Whether VAF can run vLLM on this machine, with the sentence that says why (the
    platform table in the module docstring). ``gpu`` is a ``gpu_detection.GPUInfo`` or None
    for no GPU; left out, the primary GPU is detected."""
    import platform as _platform
    system = system if system is not None else _platform.system()
    machine = (machine if machine is not None else _platform.machine()).lower()
    python = python if python is not None else (sys.version_info[0], sys.version_info[1])
    if gpu is _DETECT:
        try:
            from vaf.core.gpu_detection import get_primary_gpu
            gpu = get_primary_gpu()
        except Exception:
            gpu = None

    if system == "Windows":
        return EngineSupport(LATER, "vLLM has no native Windows support (WSL only); VAF does "
                                    "not manage it yet. llama.cpp runs here.")
    if system == "Darwin":
        if machine in ("arm64", "aarch64"):
            return EngineSupport(LATER, "vLLM on Apple GPUs is the community plugin vLLM-Metal "
                                        "(MLX models); VAF does not manage it yet. llama.cpp "
                                        "(Metal) runs here.")
        return EngineSupport(UNSUPPORTED, "vLLM has no build for this machine. llama.cpp runs "
                                          "here.")
    if system != "Linux" or machine not in ("x86_64", "amd64", "aarch64", "arm64"):
        return EngineSupport(UNSUPPORTED, "vLLM has no build for this machine. llama.cpp runs "
                                          "here.")
    vendor = str(getattr(gpu, "vendor", "") or "").lower() if gpu is not None else ""
    if not vendor:
        return EngineSupport(UNSUPPORTED, "vLLM's CPU backend is basic (no 4-bit GGUF-style "
                                          "quantization); llama.cpp is the better engine for "
                                          "a CPU.")
    if vendor == "amd":
        return EngineSupport(LATER, "vLLM supports some AMD GPUs through ROCm 6.3+, with "
                                    "Python 3.12 builds only; VAF does not manage that yet. "
                                    "llama.cpp (Vulkan) runs here.")
    if vendor == "intel":
        return EngineSupport(LATER, "vLLM's Intel GPU support is basic and needs Python 3.12; "
                                    "VAF does not manage it yet. llama.cpp runs here.")
    if vendor != "nvidia":
        return EngineSupport(UNSUPPORTED, "vLLM has no build for this GPU. llama.cpp runs here.")
    raw = str(getattr(gpu, "compute_capability", "") or "")
    capability = _parse_capability(raw)
    if capability is None:
        # nvidia-smi learned the field long before the driver a CUDA 12.9 build needs, so a
        # driver that cannot report it is too old for vLLM as well.
        return EngineSupport(UNSUPPORTED, "VAF could not read this GPU's compute capability "
                                          "from nvidia-smi (the driver may be too old for "
                                          "vLLM's CUDA build). llama.cpp runs here.")
    if capability < MIN_COMPUTE_CAPABILITY:
        return EngineSupport(UNSUPPORTED, f"vLLM needs compute capability 7.5 or newer; this "
                                          f"GPU has {raw.strip()}. llama.cpp runs here.")
    low, high = PYTHON_RANGE
    if not (low <= tuple(python) < high):
        return EngineSupport(UNSUPPORTED, f"vLLM needs Python 3.11 to 3.14; VAF runs on "
                                          f"{python[0]}.{python[1]} here. llama.cpp runs here.")
    vram_gb = (int(getattr(gpu, "vram_mb", 0) or 0)) / 1024
    model = str(getattr(gpu, "model", "") or "").strip() or "GPU"
    return EngineSupport(SUPPORTED, f"vLLM runs here (NVIDIA {model.replace('NVIDIA ', '')}, "
                                    f"{vram_gb:.0f} GB). llama.cpp stays the lighter choice "
                                    "for small GPUs and quick starts.")


def default_vllm_model(vram_gb: Optional[float] = None) -> str:
    """The model ``vllm_model = "auto"`` stands for, by the GPU's total memory."""
    if vram_gb is None:
        try:
            from vaf.core.gpu_detection import get_primary_gpu
            gpu = get_primary_gpu()
            vram_gb = (gpu.vram_mb / 1024) if gpu and gpu.vram_mb else 0.0
        except Exception:
            vram_gb = 0.0
    return DEFAULT_MODEL_LARGE if float(vram_gb) >= LARGE_MODEL_FROM_GB else DEFAULT_MODEL_SMALL


def valid_repo_id(value: str) -> bool:
    """A Hugging Face repo id ("owner/name"): what ``vllm_model`` accepts besides ``auto``."""
    return bool(_REPO_ID.fullmatch(str(value or "").strip()))


def resolve_vllm_model(config: Optional[Dict] = None) -> str:
    """The repo id the vLLM engine runs: ``vllm_model``, with ``auto`` (or nothing) resolved
    to the VRAM default."""
    try:
        if config is None:
            from vaf.core.config import Config
            value = Config.get("vllm_model", "auto")
        else:
            value = config.get("vllm_model", "auto")
    except Exception:
        value = "auto"
    value = str(value or "").strip()
    if not value or value.lower() == "auto":
        return default_vllm_model()
    return value


# The tool-call and reasoning parser per model family (vLLM's --tool-call-parser and
# --reasoning-parser): https://docs.vllm.ai/en/latest/features/tool_calling/ and
# https://docs.vllm.ai/en/latest/features/reasoning_outputs/. Matched on the repo's name
# (after the owner), lowercase. Only families whose parser needs no extra chat template file
# are listed; Qwen2.5 has no thinking mode, so no reasoning parser. Coder, vision and
# Qwen3.5 variants are left out on purpose: their parsers differ and are not verified here.
_FAMILIES = (
    (re.compile(r"qwq(-|$)"), "hermes", "deepseek_r1"),
    (re.compile(r"qwen3-\d"), "hermes", "qwen3"),
    (re.compile(r"qwen2\.5-\d"), "hermes", ""),
    (re.compile(r"hermes-(2-pro|3)"), "hermes", ""),
    (re.compile(r"(mistral|ministral|mixtral)"), "mistral", ""),
    (re.compile(r"gpt-oss"), "openai", ""),
)


def parsers_for(model_id: str, config: Optional[Dict] = None) -> Tuple[Optional[str], str]:
    """(tool_call_parser, reasoning_parser) for the model. The tool parser is None when the
    family is unknown and ``vllm_tool_call_parser`` is empty: the start refuses then, since
    an agent without tool calls cannot work. ``vllm_reasoning_parser = "none"`` turns the
    reasoning parser off; the reasoning parser is "" when there is none."""
    try:
        if config is None:
            from vaf.core.config import Config
            tool_cfg = Config.get("vllm_tool_call_parser", "")
            reasoning_cfg = Config.get("vllm_reasoning_parser", "")
        else:
            tool_cfg = config.get("vllm_tool_call_parser", "")
            reasoning_cfg = config.get("vllm_reasoning_parser", "")
    except Exception:
        tool_cfg = reasoning_cfg = ""
    name = str(model_id or "").strip().rsplit("/", 1)[-1].lower()
    tool: Optional[str] = None
    reasoning = ""
    for pattern, family_tool, family_reasoning in _FAMILIES:
        if pattern.match(name):
            tool, reasoning = family_tool, family_reasoning
            break
    tool_cfg = str(tool_cfg or "").strip()
    reasoning_cfg = str(reasoning_cfg or "").strip()
    if tool_cfg:
        tool = tool_cfg
    if reasoning_cfg:
        reasoning = "" if reasoning_cfg.lower() == "none" else reasoning_cfg
    return tool, reasoning


def parser_refusal(model_id: str, tool: Optional[str], reasoning: str) -> str:
    """Why a start with these parsers is refused, or "". A parser name becomes a launch
    argument, so anything but a plain name is refused rather than passed on."""
    if not tool:
        return (f"VAF does not know the tool-call parser for {model_id}: set "
                "vllm_tool_call_parser to the one vLLM names for this model family "
                "(https://docs.vllm.ai/en/latest/features/tool_calling/). VAF needs tool calls.")
    for key, value in (("vllm_tool_call_parser", tool), ("vllm_reasoning_parser", reasoning)):
        if value and not _PARSER_NAME.fullmatch(value):
            return f"{key} must be a parser name (lowercase letters, digits, _), not {value!r}."
    return ""


def reasoning_text(obj: Any, *, local: bool = True) -> str:
    """The reasoning of a reply message or stream delta, a dict or an SDK object.

    llama.cpp (and DeepSeek) put it in ``reasoning_content``; vLLM renamed that field to
    ``reasoning``, and a reader of the old name alone gets an empty string from it. The new
    name is read only for the local server (``local``): other providers (OpenRouter) carry a
    ``reasoning`` field of their own that VAF does not show."""
    if obj is None:
        return ""
    if isinstance(obj, dict):
        value = obj.get("reasoning_content") or (obj.get("reasoning") if local else None)
    else:
        value = getattr(obj, "reasoning_content", None) or (
            getattr(obj, "reasoning", None) if local else None)
    return value if isinstance(value, str) else ""


_LOCAL_URL = "http://127.0.0.1:8080"


def local_token_count(text: str, base_url: Optional[str] = None, *, timeout: float = 5,
                      engine: Optional[str] = None) -> Optional[int]:
    """Tokens of ``text`` by the running local server's own tokenizer, or None when it cannot
    say (callers estimate then). The request has the running engine's shape: llama.cpp takes
    ``{"content": ...}`` and answers ``{"tokens": [...]}``, vLLM takes ``{"prompt": ...}`` and
    answers ``{"count": N, "tokens": [...]}``."""
    import requests
    engine = engine or current()
    body = {"prompt": text} if engine == VLLM else {"content": text}
    try:
        r = requests.post(f"{(base_url or _LOCAL_URL).rstrip('/')}/tokenize", json=body,
                          timeout=timeout)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    count = data.get("count")
    if isinstance(count, int) and not isinstance(count, bool):
        return count
    tokens = data.get("tokens")
    if isinstance(tokens, list):
        return len(tokens)
    return None


_window_cache: Dict[str, Optional[int]] = {}


def local_context_window(base_url: Optional[str] = None) -> Optional[int]:
    """The context window the running vLLM serves (``max_model_len`` on ``/v1/models``), or
    None: under llama.cpp (no request is made), or when vLLM does not answer. Remembered per
    server process, since the window is a launch argument."""
    if current() != VLLM:
        return None
    from vaf.core import vllm_server
    key = f"{base_url or _LOCAL_URL}#{vllm_server.running_pid()}"
    if key in _window_cache and _window_cache[key]:
        return _window_cache[key]
    import requests
    window: Optional[int] = None
    try:
        r = requests.get(f"{(base_url or _LOCAL_URL).rstrip('/')}/v1/models", timeout=2)
        if r.status_code == 200:
            for entry in (r.json().get("data") or []):
                if isinstance(entry, dict) and entry.get("owned_by") == "vllm":
                    value = entry.get("max_model_len")
                    if isinstance(value, int) and value > 0:
                        window = value
                    break
    except Exception:
        window = None
    _window_cache[key] = window
    return window


def conflicts(config: Optional[Dict] = None) -> List[str]:
    """Settings that do not work with the chosen engine, one sentence each, for `vaf engine`
    and `vaf health`. Empty under llama.cpp; nothing is refused here."""
    try:
        if config is None:
            from vaf.core.config import Config
            config = Config.load()
    except Exception:
        return []
    if current(config) != VLLM:
        return []

    def _v(key: str, default: str = "") -> str:
        return str(config.get(key, default) or default).strip().lower()

    out: List[str] = []
    verdict = support()
    if not verdict.supported:
        out.append(f"local_engine is \"vllm\", but: {verdict.reason}")
    try:
        from vaf.core import vllm_server
        if not vllm_server.is_installed():
            out.append("vLLM is the local engine but is not installed: run `vaf engine vllm`.")
    except Exception:
        pass
    if _v("voice_agent_provider") == "local" and _v("provider", "local") != "local":
        out.append("voice_agent_provider is \"local\" while the chat runs on an API provider: "
                   "a voice call on vLLM needs provider local in this version.")
    if _v("vision_provider") == "local":
        out.append("vision_provider is \"local\": local image understanding runs on llama.cpp "
                   "only; under vLLM choose an API provider for images.")
    if _v("voice_agent_provider") == "local" and _v("voice_agent_model"):
        # With voice_agent_provider "local" the key names a dedicated voice GGUF; with an API
        # voice provider it is that API's model and still used.
        out.append("voice_agent_model names a voice GGUF, but under vLLM a voice call rides "
                   "the running vLLM model; the setting is not used.")
    return out
