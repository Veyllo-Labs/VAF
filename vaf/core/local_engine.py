# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
r"""Which engine runs the local model: llama.cpp (GGUF) or a VAF-managed vLLM.

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

The switch itself is ``apply`` (with ``validate``, ``state``, ``uninstall_vllm`` and
``status_row`` around it): one implementation behind `vaf engine`, the web route of Settings >
AI & Model (vaf/api/system_routes.py) and the full-screen TUI (agent_bridge). The engine and
its model never change through a Settings save (``without_engine_keys``): a save sends the
whole page, and a switch installs, downloads, stops a server and checks the fit.

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
  ``inference_profile``: it is read from the installation's config (``apply`` writes it,
  for `vaf engine`, Settings and the TUI), because the one local server on 127.0.0.1:8080 is the machine's and two agents
  cannot run it on two engines at once. An embedder's ``Agent(config=...)`` override of
  ``local_engine`` therefore does not switch it (the server start reads the installation's
  config); ``apply`` does. No new ``vaf.`` symbol is needed for that: the callers of
  ``apply`` are `vaf engine`, the web route and the TUI bridge, all product code of this
  installation, the position of ``inference_profile.apply``.
- **A foreign server for the main loop** (an Ollama or vLLM that VAF did not start) is not
  a supported setup: the address is written out instead of read from
  ``Config.get_llama_server_url`` in 42 lines of 13 files (``git grep -E
  "(127\.0\.0\.1|localhost):8080" -- 'vaf/*.py'`` at the commit before this engine,
  comments included), so every consumer talks to whatever answers on the local port.
  What the start does with such a server: it is REUSED when it already serves the
  configured model, REFUSED (no launch) when it serves another one, and never stopped by
  VAF. That is the vLLM start; the loopback port is trusted as the local server, the same
  trust those 42 call sites already place in it. The llama engine trusts it more:
  ``ServerManager.start_server`` reuses a healthy server that holds the wanted model, and
  ``Agent.load_model`` reuses any healthy server it finds on the port without asking which
  model it holds (unchanged here; under vLLM ``load_model`` always goes through the start). ``local_api_url`` stays the lane for
  the API-backend consumers.
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
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

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
# The models a picker offers by name, with their measured weights in GB (the "Model" table of
# docs/llm/VLLM_ENGINE.md); any other repo id is free entry.
CURATED_MODELS = ((DEFAULT_MODEL_SMALL, 2.67), (DEFAULT_MODEL_LARGE, 6.10))

# Keys whose change restarts the running engine (the tray's config hook). Every key that
# starts with ``vllm_`` is a vLLM launch argument.
LLAMA_RESTART_KEYS = ("model", "n_ctx", "gpu_layers")
ENGINE_KEYS = ("local_engine", "vllm_model", "vllm_tool_call_parser", "vllm_reasoning_parser",
               "vllm_gpu_memory_utilization")
RESTART_KEYS = LLAMA_RESTART_KEYS + ENGINE_KEYS
# The keys only ``apply`` writes: a direct write would switch the engine or its model without
# the install, the download and the fit check. The parser and GPU-share keys stay writable
# (config-file and `vaf models config` settings).
SWITCH_KEYS = ("local_engine", "vllm_model")

_REPO_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")
_PARSER_NAME = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class EngineSupport:
    """The verdict, the English sentence (`vaf engine`, `vaf health`, logs) and a code with
    its parameters, from which a surface in another language builds its own sentence."""
    level: str
    reason: str
    code: str = ""
    params: Mapping[str, str] = field(default_factory=dict, compare=False)

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
                                    "not manage it yet. llama.cpp runs here.", "windows")
    if system == "Darwin":
        if machine in ("arm64", "aarch64"):
            return EngineSupport(LATER, "vLLM on Apple GPUs is the community plugin vLLM-Metal "
                                        "(MLX models); VAF does not manage it yet. llama.cpp "
                                        "(Metal) runs here.", "apple")
        return EngineSupport(UNSUPPORTED, "vLLM has no build for this machine. llama.cpp runs "
                                          "here.", "no_build")
    if system != "Linux" or machine not in ("x86_64", "amd64", "aarch64", "arm64"):
        return EngineSupport(UNSUPPORTED, "vLLM has no build for this machine. llama.cpp runs "
                                          "here.", "no_build")
    vendor = str(getattr(gpu, "vendor", "") or "").lower() if gpu is not None else ""
    if not vendor:
        return EngineSupport(UNSUPPORTED, "vLLM's CPU backend is basic (no 4-bit GGUF-style "
                                          "quantization); llama.cpp is the better engine for "
                                          "a CPU.", "no_gpu")
    if vendor == "amd":
        return EngineSupport(LATER, "vLLM supports some AMD GPUs through ROCm 6.3+, with "
                                    "Python 3.12 builds only; VAF does not manage that yet. "
                                    "llama.cpp (Vulkan) runs here.", "amd")
    if vendor == "intel":
        return EngineSupport(LATER, "vLLM's Intel GPU support is basic and needs Python 3.12; "
                                    "VAF does not manage it yet. llama.cpp runs here.", "intel")
    if vendor != "nvidia":
        return EngineSupport(UNSUPPORTED, "vLLM has no build for this GPU. llama.cpp runs here.",
                             "no_build_gpu")
    raw = str(getattr(gpu, "compute_capability", "") or "")
    capability = _parse_capability(raw)
    if capability is None:
        # nvidia-smi learned the field long before the driver a CUDA 12.9 build needs, so a
        # driver that cannot report it is too old for vLLM as well.
        return EngineSupport(UNSUPPORTED, "VAF could not read this GPU's compute capability "
                                          "from nvidia-smi (the driver may be too old for "
                                          "vLLM's CUDA build). llama.cpp runs here.",
                             "nvidia_unknown")
    if capability < MIN_COMPUTE_CAPABILITY:
        return EngineSupport(UNSUPPORTED, f"vLLM needs compute capability 7.5 or newer; this "
                                          f"GPU has {raw.strip()}. llama.cpp runs here.",
                             "nvidia_too_old", {"capability": raw.strip()})
    low, high = PYTHON_RANGE
    if not (low <= tuple(python) < high):
        version = f"{python[0]}.{python[1]}"
        return EngineSupport(UNSUPPORTED, f"vLLM needs Python 3.11 to 3.14; VAF runs on "
                                          f"{version} here. llama.cpp runs here.",
                             "python", {"python": version})
    vram_gb = (int(getattr(gpu, "vram_mb", 0) or 0)) / 1024
    model = str(getattr(gpu, "model", "") or "").strip() or "GPU"
    name = model.replace('NVIDIA ', '')
    return EngineSupport(SUPPORTED, f"vLLM runs here (NVIDIA {name}, "
                                    f"{vram_gb:.0f} GB). llama.cpp stays the lighter choice "
                                    "for small GPUs and quick starts.",
                         "supported", {"gpu": name, "vram_gb": f"{vram_gb:.0f}"})


_SUPPORT_CLASS: Optional[Tuple[str, str]] = None
_SUPPORT_RETRY_AT = 0.0
_SUPPORT_LOCK = threading.Lock()
# How long an inconclusive detection is not repeated (see support_class).
SUPPORT_RETRY_SECONDS = 60.0


def support_class() -> Optional[Tuple[str, str]]:
    """(level, code) of this machine's verdict for the unauthenticated first-run question
    (``needs-setup``), or None while it is not known. Never raises.

    A conclusive verdict is kept for the process: the hardware does not change while VAF runs,
    and ``support`` starts external programs (nvidia-smi, lspci) that an unauthenticated
    caller must not be able to start on every request. An inconclusive one is not kept: a
    detection that raised, or "no GPU" while nvidia-smi is installed (its probe swallows a
    timeout or a driver still loading and reports no GPU, gpu_detection.detect_nvidia_gpu).
    Kept, that would leave the wizard saying vLLM cannot run until a restart. It is tried
    again after ``SUPPORT_RETRY_SECONDS``, and None comes back meanwhile, so the probes still
    run at most once a minute.

    A caller that finds a detection already running gets None at once instead of waiting for
    it: the route answers without a session, and a probe can run into its timeouts (5 s each),
    which would hold every waiting caller's thread for that long."""
    global _SUPPORT_CLASS, _SUPPORT_RETRY_AT
    if not _SUPPORT_LOCK.acquire(blocking=False):
        return _SUPPORT_CLASS
    try:
        if _SUPPORT_CLASS is not None:
            return _SUPPORT_CLASS
        now = time.monotonic()
        if now < _SUPPORT_RETRY_AT:
            return None
        _SUPPORT_RETRY_AT = now + SUPPORT_RETRY_SECONDS
        try:
            from vaf.core.gpu_detection import get_primary_gpu
            verdict = support(gpu=get_primary_gpu())
        except Exception:
            return None
        import shutil
        if verdict.code == "no_gpu" and shutil.which("nvidia-smi"):
            return None
        _SUPPORT_CLASS = (str(verdict.level), str(verdict.code))
        return _SUPPORT_CLASS
    finally:
        _SUPPORT_LOCK.release()


# Every code ``support`` gives, for the catalogues (a guard test reads this tuple).
SUPPORT_CODES = ("supported", "windows", "apple", "no_build", "no_build_gpu", "no_gpu", "amd",
                 "intel", "nvidia_unknown", "nvidia_too_old", "python")


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


def conflict_items(config: Optional[Dict] = None) -> List[Tuple[str, str]]:
    """Settings that do not work with the chosen engine, as (code, sentence) pairs: the code
    for a surface that translates, the sentence for `vaf engine` and `vaf health`. Empty
    under llama.cpp; nothing is refused here."""
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

    out: List[Tuple[str, str]] = []
    verdict = support()
    if not verdict.supported:
        out.append(("unsupported", f"local_engine is \"vllm\", but: {verdict.reason}"))
    try:
        from vaf.core import vllm_server
        if not vllm_server.is_installed():
            out.append(("not_installed", "vLLM is the local engine but is not installed: run "
                                         "`vaf engine vllm`."))
    except Exception:
        pass
    if _v("voice_agent_provider") == "local" and _v("provider", "local") != "local":
        out.append(("voice_api_main", "voice_agent_provider is \"local\" while the chat runs on "
                                      "an API provider: a voice call on vLLM needs provider "
                                      "local in this version."))
    if _v("vision_provider") == "local":
        out.append(("vision_local", "vision_provider is \"local\": local image understanding "
                                    "runs on llama.cpp only; under vLLM choose an API provider "
                                    "for images."))
    if _v("voice_agent_provider") == "local" and _v("voice_agent_model"):
        # With voice_agent_provider "local" the key names a dedicated voice GGUF; with an API
        # voice provider it is that API's model and still used.
        out.append(("voice_model_unused", "voice_agent_model names a voice GGUF, but under vLLM "
                                          "a voice call rides the running vLLM model; the "
                                          "setting is not used."))
    return out


CONFLICT_CODES = ("unsupported", "not_installed", "voice_api_main", "vision_local",
                  "voice_model_unused")


def conflicts(config: Optional[Dict] = None) -> List[str]:
    """The sentences of ``conflict_items``, for `vaf engine` and `vaf health`."""
    return [text for _code, text in conflict_items(config)]


# ── the switch: one implementation behind `vaf engine`, Settings and the TUI ──────────


class EngineRefusal(ValueError):
    """A switch refused before anything ran: ``code`` for a surface that translates, the
    message (``str()``) in English for the terminal and the logs."""

    def __init__(self, code: str, text: str, params: Optional[Mapping[str, str]] = None):
        super().__init__(text)
        self.code = code
        self.params = dict(params or {})


@dataclass(frozen=True)
class EngineSwitch:
    """What ``apply`` did. ``status``: ``done``, ``needs_install`` (nothing ran: the install
    was not allowed), ``refused`` (nothing ran) or ``failed`` (a step failed; the engine is
    unchanged, except ``vllm_stop_failed``, where llama.cpp is chosen and vLLM still runs).
    ``text`` is the English sentence of the outcome, ``detail`` a second one, ``note`` the
    success sentence of a switch whose last step failed."""
    status: str
    code: str = ""
    text: str = ""
    engine: str = ""
    model: str = ""
    fit: Any = None
    stopped_llama: bool = False
    stopped_vllm: bool = False
    note: str = ""
    detail: str = ""
    previous: str = ""
    params: Mapping[str, str] = field(default_factory=dict, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "code": self.code, "text": self.text,
                "detail": self.detail, "engine": self.engine, "model": self.model,
                "fit": fit_dict(self.fit), "stopped_llama": self.stopped_llama,
                "stopped_vllm": self.stopped_vllm,
                "note": self.note, "previous": self.previous, "params": dict(self.params)}


# Every code a refusal or a result of ``apply`` and ``uninstall_vllm`` can carry, besides the
# support codes (a refusal on the verdict carries the verdict's own code), for the catalogues.
SWITCH_CODES = ("unknown_engine", "bad_repo", "profile_api", "parser_unknown", "parser_bad",
                "busy", "needs_install", "switched_vllm", "unchanged_vllm", "switched_llama",
                "unchanged_llama", "install_failed", "cuda_probe_failed", "download_failed",
                "download_cancelled", "llama_stop_failed", "no_fit", "vllm_stop_failed",
                "engine_active", "not_installed", "uninstall_failed", "removed")
# The phases of a switch, in order (MAINTENANCE kind ``vllm_engine``): the four install steps
# of ``vllm_server.install``, then the model, the handover and the fit; ``stop_vllm`` is the
# one step back to llama.cpp.
PHASES = ("install_env", "install_vllm", "install_kernels", "install_probe", "download_model",
          "handover", "fit_check", "stop_vllm")
MAINTENANCE_KIND = "vllm_engine"


def fit_dict(result: Any) -> Optional[Dict[str, Any]]:
    """A ``vllm_server.FitResult`` as plain data (the numbers a translated sentence needs)."""
    if result is None:
        return None
    keys = ("ok", "code", "max_model_len", "utilization", "kv_dtype", "need_mb", "budget_mb",
            "weights_mb", "kv_mb", "overhead_mb", "free_mb", "total_mb", "reason", "note")
    return {key: getattr(result, key, None) for key in keys}


def _cfg() -> Dict:
    try:
        from vaf.core.config import Config
        return Config.load()
    except Exception:
        return {}


def _model_setting(model: Optional[str]) -> Tuple[Optional[str], str]:
    """(what to store in ``vllm_model``, the repo id that runs). ``auto`` stands for the
    VRAM default; None leaves the stored setting as it is."""
    if model is None:
        return None, resolve_vllm_model()
    value = str(model).strip()
    if value.lower() == "auto":
        return "auto", default_vllm_model()
    return value, value


def _validated(engine: str, model: Optional[str]) -> Tuple[str, Optional[str], str]:
    wanted = str(engine or "").strip().lower()
    if wanted not in ENGINES:
        raise EngineRefusal("unknown_engine", f"Unknown engine {engine!r}: use llama or vllm.")
    if wanted == LLAMA:
        return LLAMA, None, ""
    from vaf.core import inference_profile
    if not inference_profile.local_inference_allowed():
        raise EngineRefusal("profile_api", inference_profile.refusal("vLLM"))
    if model is not None and str(model).strip().lower() != "auto" and not valid_repo_id(model):
        raise EngineRefusal("bad_repo", f"--model takes a Hugging Face repo id (owner/name), "
                                        f"not {model!r}.", {"model": str(model)})
    verdict = support()
    if not verdict.supported:
        raise EngineRefusal(verdict.code or UNSUPPORTED, verdict.reason, verdict.params)
    setting, repo = _model_setting(model)
    tool, reasoning = parsers_for(repo)
    refusal = parser_refusal(repo, tool, reasoning)
    if refusal:
        raise EngineRefusal("parser_unknown" if not tool else "parser_bad", refusal,
                            {"model": repo})
    return VLLM, setting, repo


def validate(engine: str, model: Optional[str] = None) -> str:
    """The engine ``apply`` would switch to, or ``EngineRefusal`` (a ValueError): an unknown
    engine, the ``api`` profile, a value that is no repo id, a machine whose verdict is not
    ``supported`` (the verdict's code), a model family without a known parser. Nothing is
    written: a caller that runs ``apply`` in the background (the web switch) refuses a wrong
    request here, before it starts."""
    return _validated(engine, model)[0]


def _n_ctx() -> int:
    from vaf.core.config import Config
    from vaf.core.vllm_server import MIN_CONTEXT
    try:
        return max(int(Config.get("n_ctx", MIN_CONTEXT) or MIN_CONTEXT), MIN_CONTEXT)
    except (TypeError, ValueError):
        return MIN_CONTEXT


def needs_install() -> bool:
    """True when vLLM is not installed, or installed at another version than the pin."""
    from vaf.core import vllm_server
    return not vllm_server.is_installed() or vllm_server.installed_version() != vllm_server.PIN


def _switch_lock():
    """One switch at a time across processes (the web switch and `vaf engine` in a terminal):
    a second one is refused (``busy``), not queued, because it would install, download or
    stop a server under the first."""
    from filelock import FileLock

    from vaf.core import vllm_server
    root = vllm_server.venv_dir().parent
    root.mkdir(parents=True, exist_ok=True)
    return FileLock(str(root / "switch.lock"))


def _stop_llama(say: Callable[..., None]) -> Tuple[bool, str]:
    """Stop the llama server, which holds the GPU memory vLLM is measured against and needs,
    and wait until that memory is released (the handover the vLLM start runs). Returns
    (one was running, why it is still running or "")."""
    from vaf.core.backend import ServerManager
    mgr = ServerManager(skip_cleanup=True)
    if mgr._llama_pid() is None:
        return False, ""
    say("Stopping the llama server (vLLM needs its GPU memory)...", "info")
    return True, mgr.hand_over_from_llama()


class _Steps:
    """The switch's progress: MAINTENANCE (the banner) and the caller's ``progress``."""

    def __init__(self, total: int, progress: Optional[Callable[[Dict[str, Any]], None]]):
        self.total = total
        self.finished = 0
        self.phase = ""
        self._progress = progress

    def _push(self, active: bool, error: str = "") -> None:
        try:
            from vaf.core.maintenance_state import MAINTENANCE
            MAINTENANCE.update(kind=MAINTENANCE_KIND, active=active, done=self.finished,
                               total=self.total, phase=self.phase, error=error)
        except Exception:
            pass
        if self._progress is not None:
            try:
                self._progress({"phase": self.phase, "step": self.finished,
                                "steps": self.total, "error": error})
            except Exception:
                pass

    def begin(self, phase: str) -> None:
        if self.phase:
            self.finished += 1
        self.phase = phase
        self._push(True)

    def done(self) -> None:
        self.finished = self.total
        self._push(False)
        try:
            from vaf.core.maintenance_state import MAINTENANCE
            MAINTENANCE.clear()
        except Exception:
            pass

    def fail(self, code: str) -> None:
        self._push(False, error=code)


def _refresh_agents() -> None:
    """The live agents of this process follow the new engine's model (their tool-call parser
    and message handling); only where agents exist, so `vaf engine` imports no engine."""
    module = sys.modules.get("vaf.core.agent")
    if module is not None:
        try:
            module.refresh_local_identity_all()
        except Exception:
            pass


def apply(engine: str, *, model: Optional[str] = None, install: bool = False,
          progress: Optional[Callable[[Dict[str, Any]], None]] = None,
          output: Optional[str] = None,
          log: Optional[Callable[..., None]] = None) -> EngineSwitch:
    """Switch the local engine: the one implementation behind `vaf engine`, Settings > AI &
    Model and the full-screen TUI.

    To vLLM, in this order, each step only after the one before succeeded: refuse what
    ``validate`` refuses; install the pinned vLLM when it is missing (only with ``install``,
    else ``needs_install`` and nothing runs; ``output`` is a file for pip's output); download
    the model; stop a running llama server (vLLM is measured against the memory it holds);
    check the fit; write ``vllm_model`` (when ``model`` was given) and then ``local_engine``.
    A failed step leaves ``local_engine`` as it was. To llama.cpp: write the key, stop a
    running managed vLLM. Neither starts a server: the next use of the local model does.

    ``log(text, kind)`` receives the English lines of the way (kind info, success or warning).
    The steps go to MAINTENANCE (kind ``vllm_engine``) and to ``progress``. One switch at a
    time across processes (``busy``)."""
    say = log or (lambda text, kind="info": None)
    try:
        wanted, setting, repo = _validated(engine, model)
    except EngineRefusal as e:
        return EngineSwitch("refused", e.code, str(e), params=e.params)
    lock = _switch_lock()
    from filelock import Timeout
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return EngineSwitch("refused", "busy", "Another switch of the local engine is running; "
                                               "wait for it to finish first.")
    try:
        if wanted == LLAMA:
            result = _apply_llama(progress)
        else:
            result = _apply_vllm(setting, repo, install, progress, output, say)
    finally:
        lock.release()
    if result.status == "done" or result.code == "vllm_stop_failed":
        _refresh_agents()
    return result


def _apply_llama(progress) -> EngineSwitch:
    from vaf.core import vllm_server
    from vaf.core.config import Config
    steps = _Steps(1, progress)
    steps.begin("stop_vllm")
    previous = current()
    try:
        Config.set("local_engine", LLAMA)
        reason = vllm_server.stop() if vllm_server.running_pid() is not None else ""
    except BaseException:
        steps.fail("vllm_stop_failed")
        raise
    unchanged = previous == LLAMA
    text = "The local engine is llama.cpp." + (" (unchanged)" if unchanged else "")
    if reason:
        steps.fail("vllm_stop_failed")
        return EngineSwitch("failed", "vllm_stop_failed", reason, LLAMA, note=text,
                            previous=previous)
    steps.done()
    return EngineSwitch("done", "unchanged_llama" if unchanged else "switched_llama", text,
                        LLAMA, previous=previous)


def _apply_vllm(setting, repo, install, progress, output, say) -> EngineSwitch:
    from vaf.core import vllm_server
    from vaf.core.config import Config
    missing = needs_install()
    if missing and not install:
        gb = f"{vllm_server.DOWNLOAD_GB_AT_LEAST:.1f}"
        return EngineSwitch(
            "needs_install", "needs_install",
            f"This installs vLLM {vllm_server.PIN} (about {gb} GB or more to download, more on "
            f"disk) into {vllm_server.venv_dir()}.", VLLM, repo,
            params={"pin": vllm_server.PIN, "download_gb": gb})
    steps = _Steps(7 if missing else 3, progress)
    previous = current()

    def failed(code: str, text: str, detail: str = "", **extra) -> EngineSwitch:
        steps.fail(code)
        return EngineSwitch("failed", code, text, VLLM, repo, detail=detail, previous=previous,
                            **extra)

    try:
        if missing:
            last = [""]

            def on_step(code: str) -> None:
                last[0] = code
                steps.begin(code)

            reason = vllm_server.install(log=lambda text: say(text, "info"), output=output,
                                         on_step=on_step)
            if reason:
                code = "cuda_probe_failed" if last[0] == "install_probe" else "install_failed"
                return failed(code, reason, "The local engine is unchanged.")
            say(f"vLLM {vllm_server.installed_version() or vllm_server.PIN} is installed.",
                "success")

        steps.begin("download_model")
        download: Dict[str, bool] = {}
        model_dir = vllm_server.ensure_model(repo, outcome=download)
        if not model_dir:
            if download.get("cancelled"):
                return failed("download_cancelled", f"The download of {repo} was cancelled; "
                                                    "the local engine is unchanged.")
            return failed("download_failed", f"The model {repo} could not be downloaded; the "
                                             "local engine is unchanged.")

        steps.begin("handover")
        stopped, reason = _stop_llama(say)
        if reason:
            return failed("llama_stop_failed", reason + " The local engine is unchanged.",
                          stopped_llama=stopped)
        stopped_vllm = False
        result = None
        if vllm_server.running_pid() is not None:
            if previous == VLLM and vllm_server.served_model() == repo:
                # It already serves this model, so it fits: nothing to stop or measure.
                say(f"vLLM already serves {repo}.", "info")
            else:
                # Its own share of the GPU would be measured as taken, and the fit refused a
                # model that fits once it is gone (measured: 820 of 10240 MiB free).
                say("Stopping vLLM (the new model is measured against the memory it holds)...",
                    "info")
                reason = vllm_server.stop()
                if reason:
                    return failed("vllm_stop_failed", reason + " The local engine is unchanged.",
                                  stopped_llama=stopped)
                stopped_vllm = True

        if not (previous == VLLM and vllm_server.running_pid() is not None and not stopped_vllm):
            steps.begin("fit_check")
            result = vllm_server.plan_fit(model_dir, _n_ctx())
            if result.note:
                say(result.note, "warning")
            if not result.ok:
                return failed("no_fit", result.reason,
                              f"The local engine stays {previous}; a smaller model (--model) or "
                              "a smaller n_ctx may fit."
                              + (" The llama server was stopped for the measurement; it starts "
                                 "again with the next use of the local model." if stopped else "")
                              + (" vLLM was stopped for the measurement; it starts again with "
                                 "the next use of the local model." if stopped_vllm else ""),
                              fit=result, stopped_llama=stopped, stopped_vllm=stopped_vllm)
            say(f"{repo}: {result.reason}", "info")
        if setting is not None and str(Config.get("vllm_model", "auto") or "") != setting:
            # Only a change: the tray restarts a running vLLM for every vllm_ key it sees change.
            Config.set("vllm_model", setting)
        Config.set("local_engine", VLLM)
    except BaseException:
        steps.fail("failed")
        raise
    steps.done()
    unchanged = previous == VLLM
    return EngineSwitch(
        "done", "unchanged_vllm" if unchanged else "switched_vllm",
        "The local engine is vLLM." + (" (unchanged)" if unchanged else "")
        + " It starts with the next use of the local model; the first start takes minutes.",
        VLLM, repo, fit=result, stopped_llama=stopped, stopped_vllm=stopped_vllm,
        previous=previous)


def uninstall_vllm() -> Dict[str, Any]:
    """Remove the vLLM environment: refused while vLLM is the engine (``engine_active``) or a
    switch runs (``busy``). The models stay in the Hugging Face cache; the answer names the
    one of the current setting and its size. Returns {ok, code, text, kept_model,
    kept_bytes, kept_path}."""
    from vaf.core import vllm_server
    out: Dict[str, Any] = {"ok": False, "code": "", "text": "", "kept_model": "",
                           "kept_bytes": 0, "kept_path": ""}
    if is_vllm():
        out.update(code="engine_active", text="vLLM is the local engine: switch back first "
                                              "(`vaf engine llama`), then uninstall.")
        return out
    lock = _switch_lock()
    from filelock import Timeout
    try:
        lock.acquire(timeout=0)
    except Timeout:
        out.update(code="busy", text="Another switch of the local engine is running; wait for "
                                     "it to finish first.")
        return out
    try:
        # Again under the lock: a switch to vLLM can finish between the check above and the
        # lock, and removing its environment then would leave the engine without one.
        if is_vllm():
            out.update(code="engine_active", text="vLLM is the local engine: switch back first "
                                                  "(`vaf engine llama`), then uninstall.")
            return out
        if vllm_server.installed_version() is None and not vllm_server.venv_dir().exists():
            out.update(ok=True, code="not_installed", text="vLLM is not installed.")
            return out
        reason = vllm_server.uninstall()
        if reason:
            out.update(code="uninstall_failed", text=reason)
            return out
    finally:
        lock.release()
    repo = resolve_vllm_model()
    snapshot = vllm_server.local_snapshot(repo)
    out.update(ok=True, code="removed", text=f"Removed {vllm_server.venv_dir()}.")
    if snapshot:
        out.update(kept_model=repo, kept_path=snapshot,
                   kept_bytes=vllm_server.model_size_bytes(snapshot))
    return out


def _server_state(probe_health: bool = True) -> str:
    """``running``, ``loading`` (the process is there, /health does not answer 200 yet) or
    ``idle`` for the managed vLLM."""
    from vaf.core import vllm_server
    if vllm_server.running_pid() is None:
        return "idle"
    if not probe_health:
        return "running"
    return "running" if vllm_server.health_status() == 200 else "loading"


def state(model: Optional[str] = None) -> Dict[str, Any]:
    """Everything a picker shows: the engine, the profile, this machine's verdict, the vLLM
    install, its model (the setting, what it resolves to, the automatic choice, the curated
    ones, whether it is downloaded, its parsers or why they are refused), the server, the
    GGUF of llama.cpp and the conflicts. ``model`` (a repo id or ``auto``) previews another
    model's parsers and download state. No network beyond the local server's /health."""
    from vaf.core import inference_profile, vllm_server
    cfg = _cfg()
    verdict = support()
    setting = str(cfg.get("vllm_model", "auto") or "auto").strip() or "auto"
    auto_model = default_vllm_model()
    resolved = resolve_vllm_model(cfg)
    preview = resolved
    if model is not None:
        value = str(model).strip()
        if value.lower() == "auto":
            preview = auto_model
        elif valid_repo_id(value):
            preview = value
    tool, reasoning = parsers_for(preview, cfg)
    refusal_text = parser_refusal(preview, tool, reasoning)
    refusal = ({"code": "parser_unknown" if not tool else "parser_bad", "text": refusal_text}
               if refusal_text else None)
    installed = vllm_server.installed_version()
    return {
        "engine": current(cfg),
        "profile": inference_profile.current(),
        "support": {"level": verdict.level, "code": verdict.code, "params": dict(verdict.params),
                    "reason": verdict.reason},
        "vllm": {
            "pin": vllm_server.PIN,
            "installed_version": installed,
            "needs_install": needs_install(),
            "download_gb_at_least": vllm_server.DOWNLOAD_GB_AT_LEAST,
            "model_setting": setting,
            "model": resolved,
            "auto_model": auto_model,
            "curated": [{"repo": repo, "weights_gb": gb} for repo, gb in CURATED_MODELS],
            "preview": preview,
            "model_downloaded": bool(vllm_server.local_snapshot(preview)),
            "parsers": {"tool": tool, "reasoning": reasoning},
            "parser_refusal": refusal,
            "server": _server_state(),
        },
        "llama": {"model": str(cfg.get("model", "") or "")},
        "conflicts": [{"code": code, "text": text} for code, text in conflict_items(cfg)],
    }


# The states of ``status_row``, for the catalogues.
STATUS_CODES = ("profile_api", "llama_running", "llama_idle", "unsupported", "not_installed",
                "vllm_running", "vllm_loading", "vllm_idle")


def _vllm_row(cfg: Dict, probe_health: bool = True) -> Dict[str, Any]:
    from vaf.core import vllm_server
    row: Dict[str, Any] = {"engine": VLLM, "version": "", "model": "", "params": {}}
    verdict = support()
    if not verdict.supported:
        return {**row, "state": "degraded", "code": "unsupported",
                "params": {"support": verdict.code, **dict(verdict.params)},
                "text": f"vLLM is the local engine, but: {verdict.reason}"}
    version = vllm_server.installed_version()
    if version is None:
        return {**row, "state": "degraded", "code": "not_installed",
                "text": "vLLM is the local engine but is not installed: run `vaf engine vllm`."}
    repo = resolve_vllm_model(cfg)
    server = _server_state(probe_health)
    shown = "server idle" if server == "idle" else "server running"
    return {**row, "state": {"running": "ok", "loading": "loading"}.get(server, "idle"),
            "code": f"vllm_{server}", "version": version, "model": repo,
            "params": {"version": version, "model": repo},
            "text": f"vLLM {version} installed, model {repo}, {shown}."}


def status_row(config: Optional[Dict] = None) -> Dict[str, Any]:
    """The local engine as one row (Update and Repair, ``vaf health``): {engine, state, code,
    params, text, version, model}. ``state``: ``ok``, ``loading``, ``idle``, ``degraded``
    (vLLM chosen but not installed or not supported here) or ``off`` (the ``api`` profile).
    Never a repair target: the repair cannot install an engine."""
    from vaf.core import inference_profile
    cfg = config if config is not None else _cfg()
    if not inference_profile.local_inference_allowed(cfg):
        return {"engine": current(cfg), "state": "off", "code": "profile_api", "params": {},
                "text": "Not used (no local models).", "version": "", "model": ""}
    if current(cfg) == VLLM:
        return _vllm_row(cfg)
    try:
        # What the server on the port holds (llama-server's /v1/models): a status read that
        # polls must not construct a ServerManager (on Windows that creates a job object).
        from vaf.core.backend import get_loaded_model_id
        loaded = get_loaded_model_id(timeout=1.0)
    except Exception:
        loaded = None
    running = bool(loaded)
    model = loaded or str(cfg.get("model", "") or "")
    return {"engine": LLAMA, "state": "ok" if running else "idle",
            "code": "llama_running" if running else "llama_idle", "params": {"model": model},
            "text": "llama.cpp, server running." if running else "llama.cpp, server idle.",
            "version": "", "model": model}


def without_engine_keys(payload: Dict) -> Dict:
    """A Settings save without ``local_engine`` and the ``vllm_`` keys. They change only
    through ``apply``, which installs, downloads, stops a server and checks the fit; a save
    sends the whole page, and a page loaded before a switch (another tab, the terminal, the
    switch still running) would write the old engine back without any of that. Both save
    lanes pass it: the WebSocket ``save_config`` and ``PATCH /api/config``."""
    if isinstance(payload, dict) and any(k == "local_engine" or str(k).startswith("vllm_")
                                         for k in payload):
        return {k: v for k, v in payload.items()
                if k != "local_engine" and not str(k).startswith("vllm_")}
    return payload
