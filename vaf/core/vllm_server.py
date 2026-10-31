# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The managed vLLM server: install, model, fit, start and stop.

The engine choice and its platform verdicts are in vaf/core/local_engine.py; this module
runs vLLM once it is chosen. ``ServerManager.start_server`` (vaf/core/backend.py) decides
WHEN (handover, reuse, waiting for a server that is loading); this module does the work.

- **Install** (``install``): only on request (`vaf engine vllm`, Settings, the TUI, all
  through ``local_engine.apply``), never on a start. A fresh
  environment under ``bin/vllm/venv`` (gitignored, next to the llama-server binary) made
  with VAF's own Python, the pinned ``vllm==PIN`` installed from PyPI, its CUDA 13.0 build and
  the CUDA 13.0 PyTorch it requires, from the hash-locked tree ``vllm_requirements.lock``
  (``--require-hashes --no-deps --only-binary=:all:``: pip refuses any file whose hash the
  lock does not name, any package it does not pin, and any build from source), and a probe that PyTorch in it sees the GPU AND vLLM's CUDA extension loads. Only a passing
  probe writes the recipe record (``RECIPE_FORMAT`` in ``vaf_recipe``); an environment
  without it is rebuilt by the next install and refused by ``start``. Every step runs
  without VAF's GPU hide for in-process torch (``gpu_detection.gpu_child_env``). vLLM is
  never imported into VAF's process.
- **Model** (``ensure_model``): a Hugging Face repo in safetensors form, in the standard
  Hugging Face cache, which vLLM reads itself; no code file of the repo is fetched
  (``CODE_PATTERNS`` are ignored), and a sharded download counts only when every shard of
  its index is there.
- **Fit** (``fit``): weights + KV cache for the context + an overhead, against the memory
  share vLLM gets, before the launch, so a model that cannot fit is refused with numbers
  instead of a crash a minute later.
- **Start** (``start``): on the local inference port with ``--enable-auto-tool-choice`` and
  the family's parsers, in a new session (the API server and its EngineCore children are
  one process group), model-name validation off (every consumer of the local server sends
  its own model name or none), usage statistics and Hugging Face access off.
  ``--trust-remote-code`` is never passed: it would run code from the model repo.
- **Stop** (``stop``): the whole process group, SIGTERM then SIGKILL after the grace, and
  only a process whose command line names the managed environment.

Debug artifacts: ``logs/vllm_server.log`` (the previous start in ``vllm_server.prev.log``)
and ``~/.vaf/vllm.pid``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

PIN = "0.32.0"
# The CUDA version of PIN's PyPI wheel: the release table of vLLM v0.32.0 lists
# "PyPI (CUDA 13.0) | pip install vllm", its METADATA requires nvidia-cutlass-dsl[cu13] and
# its _C_stable_libtorch extension NEEDS libcudart.so.13. vLLM's installation page at that tag
# still shows a cu129 PyTorch extra index; that command is wrong for this wheel: pip then
# prefers torch 2.13.0+cu129 over PyPI's CUDA 13.0 torch 2.13.0, and the extension cannot
# load. Re-read on every PIN bump (docs/setup/RELEASING.md).
CUDA_VERSION = "13.0"
# Measured lower bound of the download, from PyPI's file sizes: the vllm wheel 327 MB, torch
# 527 MB, the CUDA 13 NVIDIA wheels about 1990 MB (cuda-toolkit 13.0.3 components 1189 MB,
# cudnn-cu13 366, nccl-cu13 206, cusparselt-cu13 170, nvshmem-cu13 60); the rest of the tree
# and the installed size come on top.
DOWNLOAD_GB_AT_LEAST = 2.8
# The hash-locked tree of the environment: every package of the vllm==PIN tree, pinned, with
# the SHA-256 of every file PyPI publishes for it (pip-compile --generate-hashes from
# vllm_requirements.in; one lock for Linux x86_64 and aarch64, Python 3.11 to 3.14, checked by
# scripts/check_vllm_lock.py). Regenerated with every PIN bump (docs/setup/RELEASING.md).
LOCK_PATH = Path(__file__).with_name("vllm_requirements.lock")
# The recipe record: a file in the environment, written only after a hash-locked install and a
# passing probe, that says
# which install recipe built it. Bump the middle number whenever the install command or the
# probe changes. Deliberate: an environment WITHOUT the tag is not an older format to keep
# reading; it is what the earlier recipe (the cu129 extra index) built, and that recipe was
# measured unable to start vLLM (libcudart.so.13 missing), so it counts as not installed and
# the next install rebuilds it.
RECIPE_FORMAT = "vllmenv-1-18dfb7"
# The probe imports the CUDA extension vLLM's platform module imports at its first start
# (vllm/platforms/cuda.py in 0.32.0); a PIN bump re-checks the name (RELEASING.md).
_PROBE = (
    "import sys\n"
    "import torch\n"
    "if not torch.cuda.is_available():\n"
    "    sys.exit(3)\n"
    "try:\n"
    "    import vllm._C_stable_libtorch\n"
    "except ImportError as e:\n"
    "    print(e, file=sys.stderr)\n"
    "    sys.exit(4)\n"
)

# What the fit check counts besides weights and KV cache: CUDA context, activations and
# the graphs vLLM captures.
OVERHEAD_BYTES = int(1.2 * 1024 ** 3)
# Headroom left free on the GPU when the memory share is computed from the free figure.
FREE_HEADROOM_MB = 512
UTILIZATION_RANGE = (0.50, 0.92)
# VAF's own request (system prompt and routed tools) needs this much context.
MIN_CONTEXT = 32768

# The files of a model repo the engine needs. No Python file, ever: "tokenizer*" alone
# would also match a tokenizer.py, so code files are excluded explicitly (remote code never
# runs, and it is not even fetched).
MODEL_PATTERNS = ["*.json", "*.safetensors", "tokenizer*", "*.model", "*.txt", "*.jinja"]
CODE_PATTERNS = ["*.py", "*.pyc", "*.pyo", "*.so", "*.dll", "*.dylib", "*.sh", "*.exe"]

# The child of THIS process, so stop() can reap it (a killed child that is never waited for
# stays a zombie for the life of a long-running parent).
_child: Optional[subprocess.Popen] = None
# The fp8 KV cache died on this machine and auto came up: later starts skip the doomed
# attempt (the counterpart of the llama server's V-cache memo).
_kv_fp8_failed = False
_lock = threading.Lock()


def _ui():
    from vaf.cli.ui import UI
    return UI


def base_dir() -> Path:
    """The checkout or install root, the same one ``ServerManager.base_dir`` names."""
    return Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def venv_dir() -> Path:
    return base_dir() / "bin" / "vllm" / "venv"


def _venv_bin(name: str) -> Path:
    scripts = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return venv_dir() / scripts / f"{name}{suffix}"


def venv_python() -> Path:
    return _venv_bin("python")


def vllm_exe() -> Path:
    return _venv_bin("vllm")


def pid_file() -> Path:
    # Beside the llama server's server.pid (~/.vaf, data_stores row "vllm.pid").
    return Path.home() / ".vaf" / "vllm.pid"


def log_path() -> Path:
    from vaf.core.log_helper import get_app_log_dir
    return get_app_log_dir() / "vllm_server.log"


def installed_version() -> Optional[str]:
    """The vLLM version in the managed environment, read from its package metadata (vLLM is
    never imported into VAF's process), or None when it is not installed."""
    root = venv_dir()
    candidates = list(root.glob("lib/python*/site-packages")) + [root / "Lib" / "site-packages"]
    for site in candidates:
        for meta_dir in sorted(site.glob("vllm-*.dist-info")):
            try:
                text = (meta_dir / "METADATA").read_bytes().decode("utf-8", errors="replace")
            except OSError:
                continue
            m = re.search(r"^Version:\s*(\S+)", text, re.M)
            if m:
                return m.group(1)
    return None


def is_installed() -> bool:
    return installed_version() is not None and vllm_exe().exists()


def recipe_path() -> Path:
    return venv_dir() / "vaf_recipe"


def recipe_current() -> bool:
    """True when the environment carries this version's recipe record (``RECIPE_FORMAT``)."""
    try:
        return recipe_path().read_bytes().decode("utf-8", errors="replace").strip() == RECIPE_FORMAT
    except OSError:
        return False


def is_current() -> bool:
    """Installed at the pin AND built by this version's recipe, with a passing probe. An
    environment that is installed but not current cannot be trusted to start (the earlier
    recipe's could not) and is rebuilt by ``install``."""
    return is_installed() and installed_version() == PIN and recipe_current()


# ── install ──────────────────────────────────────────────────────────────────────


Runner = Callable[..., "subprocess.CompletedProcess"]


INSTALL_STEPS = ("install_env", "install_vllm", "install_probe")


def install(log: Optional[Callable[[str], None]] = None, run: Optional[Runner] = None, *,
            output: Optional[str] = None,
            on_step: Optional[Callable[[str], None]] = None,
            outcome: Optional[Dict[str, str]] = None) -> str:
    """Install the pinned vLLM into its own environment. Returns "" when it is installed (now
    or already, ``is_current``), else why not. Refused under the ``api`` profile and on a
    machine whose verdict is not ``supported``. A failed install leaves no half environment
    behind; an environment that is installed but not current is removed and rebuilt.

    ``output``: a file that receives the stdout and stderr of every step (the web server and
    the full-screen TUI have no terminal to print pip's output on); without it the steps
    inherit the terminal. ``on_step(code)`` runs before each step (``INSTALL_STEPS``).
    ``outcome``: a failed probe sets ``outcome["probe"]`` to ``"no_cuda"`` (PyTorch sees no
    CUDA GPU) or ``"libs"`` (vLLM's CUDA extension does not load, or the probe did not run)."""
    from vaf.core import inference_profile, local_engine
    say = log or (lambda text: None)
    run = run or subprocess.run
    step = on_step or (lambda code: None)
    if not inference_profile.local_inference_allowed():
        return inference_profile.refuse("vLLM")
    verdict = local_engine.support()
    if not verdict.supported:
        return verdict.reason
    if is_current():
        return ""
    if not LOCK_PATH.is_file():
        # Checked before the old environment is removed: without the lock nothing installs.
        return (f"The hash-locked vLLM tree {LOCK_PATH} is missing from this VAF installation; "
                "reinstall VAF.")
    from vaf.core.gpu_detection import gpu_child_env
    # Without VAF's GPU hide for in-process torch: the probe must see the GPU vLLM will use.
    env = gpu_child_env()
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    root = venv_dir()
    if root.exists():
        # A different pin, an earlier recipe or a broken install: start from a fresh
        # environment, as vLLM's documentation asks.
        reason = uninstall()
        if reason:
            return reason
    root.parent.mkdir(parents=True, exist_ok=True)
    sink = None
    if output:
        try:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            sink = open(output, "a", encoding="utf-8", errors="replace")
        except OSError:
            sink = None
    # Every step's stdout and stderr into the file; without one, the terminal's.
    streams = {"stdout": sink, "stderr": subprocess.STDOUT} if sink is not None else {}
    where = f"see {output}" if sink is not None else "see the output above"
    try:
        return _install_steps(say, run, step, env, root, streams, where,
                              outcome if outcome is not None else {})
    finally:
        if sink is not None:
            try:
                sink.close()
            except OSError:
                pass


def _install_steps(say, run, step, env, root, streams, where, outcome) -> str:
    from vaf.core import inference_profile
    steps = [
        ("install_env", "Creating the environment", [sys.executable, "-m", "venv", str(root)]),
        ("install_vllm", f"Installing vLLM {PIN} (at least {DOWNLOAD_GB_AT_LEAST:.1f} GB to download)",
         [str(venv_python()), "-m", "pip", "install", "--require-hashes", "--no-deps",
          "--only-binary=:all:", "-r", str(LOCK_PATH)]),
    ]
    for code, label, argv in steps:
        if not inference_profile.local_inference_allowed():
            uninstall()
            return inference_profile.refuse("vLLM")
        step(code)
        say(label + "...")
        try:
            result = run(argv, env=env, **streams)
        except Exception as e:      # noqa: BLE001 - reported to the caller
            uninstall()
            return f"{label} failed: {e}"
        if getattr(result, "returncode", 1) != 0:
            uninstall()
            return f"{label} failed (exit {getattr(result, 'returncode', '?')}); {where}."
    # Deliberately no `vllm download-kernels`: it lets FlashInfer pip-install flashinfer-cubin and
    # flashinfer-jit-cache from its own index, version-pinned but unhashed, the only compiled
    # code that would bypass the lock. Without them FlashInfer compiles the kernels it needs at
    # the first start (docs/llm/VLLM_ENGINE.md).
    step("install_probe")
    say("Checking that vLLM loads on the GPU...")
    try:
        probe = run([str(venv_python()), "-c", _PROBE], env=env, **streams)
        code = getattr(probe, "returncode", 1)
    except Exception:
        code = None
    if code == 0:
        try:
            recipe_path().write_bytes(RECIPE_FORMAT.encode("utf-8"))
        except OSError as e:
            return f"vLLM was installed, but its recipe record could not be written: {e}"
        return ""
    # No record: the next install rebuilds this environment, and start() refuses it.
    if code == 3:
        outcome["probe"] = "no_cuda"
        from vaf.core.local_engine import MIN_DRIVER
        return (f"vLLM was installed, but PyTorch in its environment sees no CUDA GPU. vLLM "
                f"{PIN} needs an NVIDIA driver for CUDA {CUDA_VERSION} (version {MIN_DRIVER[0]} "
                "or newer): update the NVIDIA driver and try again, and make sure "
                "CUDA_VISIBLE_DEVICES does not hide the GPU.")
    outcome["probe"] = "libs"
    return ("vLLM was installed, but its CUDA libraries do not load in its environment, so it "
            "cannot start. Run `vaf engine vllm` again: it rebuilds the environment, and "
            f"llama.cpp keeps running meanwhile. If it fails again, {where} for why.")


def uninstall() -> str:
    """Remove the managed environment. Only that directory, inside ``bin/``, is touched; the
    models in the Hugging Face cache stay. Returns "" or why not."""
    root = venv_dir()
    try:
        inside = root.resolve().relative_to((base_dir() / "bin").resolve())
    except (ValueError, OSError):
        return f"{root} is not inside VAF's bin folder; it is left alone."
    if not inside.parts:
        return "Refusing to remove the bin folder itself."
    if running_pid() is not None:
        return "vLLM is running; stop it first (`vaf engine llama`)."
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
        if root.exists():
            return f"{root} could not be removed completely."
    try:
        root.parent.rmdir()         # bin/vllm when nothing else is in it
    except OSError:
        pass
    return ""


# ── model ────────────────────────────────────────────────────────────────────────


def local_snapshot(repo_id: str) -> str:
    """The model's directory in the Hugging Face cache, or "" when it is not downloaded. No
    network: a start must not reach Hugging Face."""
    from vaf.core import local_engine
    if not local_engine.valid_repo_id(repo_id):
        return ""
    try:
        from huggingface_hub import snapshot_download
        path = snapshot_download(repo_id, allow_patterns=MODEL_PATTERNS,
                                 ignore_patterns=CODE_PATTERNS, local_files_only=True)
    except Exception:
        return ""
    path = str(path or "")
    return path if path and weights_complete(path) else ""


def weights_complete(model_dir: str) -> bool:
    """True when the weights in ``model_dir`` are all there. A sharded model names every
    shard in ``model.safetensors.index.json``; an interrupted download leaves the index and
    the first shards, and counting that as downloaded would size the fit check from part of
    the model and let vLLM fail at load. Offline: only the files on disk are read."""
    root = Path(model_dir)
    if not any(root.glob("*.safetensors")):
        return False
    index = root / "model.safetensors.index.json"
    if not index.is_file():
        return True
    try:
        weight_map = json.loads(index.read_bytes().decode("utf-8"))["weight_map"]
        shards = {str(name) for name in weight_map.values()}
    except Exception:
        return False
    return bool(shards) and all((root / name).is_file() for name in shards)


def ensure_model(repo_id: str, outcome: Optional[Dict[str, bool]] = None) -> str:
    """The model's directory, downloaded from Hugging Face when it is not in the cache yet.
    "" under the ``api`` profile (nothing is downloaded), for a value that is no repo id, or
    when the download failed or was cancelled (``MODEL_DOWNLOAD.request_cancel``; a caller
    that passes ``outcome`` finds ``outcome["cancelled"]`` True then, decided from this
    download's own exception).
    The download runs inside ``web_interface.model_download_broadcast``: the download banner
    of every open page shows
    its bytes and closes on its end, with no model list in the closing frame (the GGUF list
    is not affected by a vLLM model)."""
    from vaf.core import inference_profile, local_engine
    if not inference_profile.local_inference_allowed():
        inference_profile.refuse(f"the local model {repo_id}")
        return ""
    if not local_engine.valid_repo_id(repo_id):
        _ui().error(f"vllm_model must be a Hugging Face repo id (owner/name), not {repo_id!r}.")
        return ""
    found = local_snapshot(repo_id)
    if found:
        return found
    from filelock import FileLock

    from vaf.core.model_download_state import make_state_tqdm, was_cancelled
    lock_dir = venv_dir().parent
    lock_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock_dir / "download.lock")):
        if not inference_profile.local_inference_allowed():
            return ""
        found = local_snapshot(repo_id)
        if found:
            return found
        from huggingface_hub import snapshot_download

        from vaf.core.web_interface import model_download_broadcast
        _ui().event("System", f"Downloading {repo_id} for vLLM...", style="warning")
        with model_download_broadcast(repo_id, repo_id.rsplit("/", 1)[-1]) as banner:
            try:
                path = snapshot_download(
                    repo_id, allow_patterns=MODEL_PATTERNS, ignore_patterns=CODE_PATTERNS,
                    tqdm_class=make_state_tqdm(inference_profile.download_guard()))
            except Exception as e:      # noqa: BLE001 - reported, and the caller sees ""
                if was_cancelled(e):
                    if outcome is not None:
                        outcome["cancelled"] = True
                    _ui().event("System", f"Download of {repo_id} cancelled.", style="warning")
                else:
                    _ui().error(f"Download of {repo_id} failed: {e}")
                return ""
            banner["success"] = weights_complete(str(path or ""))
    path = str(path or "")
    if not weights_complete(path):
        _ui().error(f"{repo_id} has no complete set of safetensors weights; the vLLM engine "
                    "cannot run it.")
        return ""
    return path


def model_size_bytes(model_dir: str) -> int:
    total = 0
    for weights in Path(model_dir).glob("*.safetensors"):
        try:
            total += weights.stat().st_size     # follows the cache's symlink to the blob
        except OSError:
            pass
    return total


# ── fit ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelShape:
    weights_bytes: int
    layers: int
    kv_heads: int
    head_dim: int
    max_position: int = 0

    @classmethod
    def from_dir(cls, model_dir: str) -> Optional["ModelShape"]:
        """From the model's config.json and its weights; None when the config does not say."""
        try:
            cfg = json.loads((Path(model_dir) / "config.json").read_bytes().decode("utf-8"))
        except Exception:
            return None
        if not isinstance(cfg, dict):
            return None
        if "num_hidden_layers" not in cfg and isinstance(cfg.get("text_config"), dict):
            cfg = cfg["text_config"]
        try:
            layers = int(cfg["num_hidden_layers"])
            heads = int(cfg.get("num_attention_heads") or 0)
            kv_heads = int(cfg.get("num_key_value_heads") or heads)
            head_dim = int(cfg.get("head_dim") or (int(cfg["hidden_size"]) // heads if heads else 0))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return None
        if layers <= 0 or kv_heads <= 0 or head_dim <= 0:
            return None
        return cls(model_size_bytes(model_dir), layers, kv_heads, head_dim,
                   int(cfg.get("max_position_embeddings") or 0))


@dataclass(frozen=True)
class FitResult:
    """The fit, with the English ``reason`` (`vaf engine`, the start's error) and the numbers
    and ``code`` a translated sentence is built from: ``fits``, ``too_big``,
    ``share_above_free``, ``no_shape``, ``no_gpu_memory``."""
    ok: bool
    max_model_len: int
    utilization: float
    kv_dtype: str
    need_mb: int
    budget_mb: int
    reason: str
    note: str = ""
    code: str = ""
    weights_mb: int = 0
    kv_mb: int = 0
    free_mb: int = 0
    total_mb: int = 0
    overhead_mb: int = 0


FIT_CODES = ("fits", "too_big", "share_above_free", "no_shape", "no_gpu_memory")


def _mb(value: float) -> int:
    return int(round(value / 1024 ** 2))


def fit(shape: ModelShape, n_ctx: int, free_mb: int, total_mb: int, *,
        kv_dtype: Optional[str] = None, utilization: float = 0.0) -> FitResult:
    """Does the model fit, and with what? ``n_ctx`` is the context (``--max-model-len``),
    clamped to the model's maximum. The KV cache is fp8 unless ``kv_dtype`` says otherwise
    (``auto`` = the model's own 16-bit dtype). ``utilization`` 0 = computed from the free
    memory: ``(free - 512 MiB) / total`` within 0.50..0.92. vLLM refuses to start when its
    share is more than what is free, so that counts as not fitting too."""
    note = ""
    max_len = int(n_ctx)
    if shape.max_position and max_len > shape.max_position:
        note = (f"The context {max_len} is more than {shape.max_position}, the most this "
                f"model takes; it runs with {shape.max_position}.")
        max_len = shape.max_position
    dtype = kv_dtype or "fp8"
    kv_bytes = 1 if dtype.startswith("fp8") else 2
    if utilization and 0 < float(utilization) <= 1:
        share = float(utilization)
    else:
        low, high = UTILIZATION_RANGE
        share = min(high, max(low, (free_mb - FREE_HEADROOM_MB) / total_mb)) if total_mb else low
    budget_mb = int(share * total_mb)
    kv_total = 2 * shape.layers * shape.kv_heads * shape.head_dim * kv_bytes * max_len
    need_mb = _mb(shape.weights_bytes + kv_total + OVERHEAD_BYTES)
    numbers = {"weights_mb": _mb(shape.weights_bytes), "kv_mb": _mb(kv_total),
               "free_mb": int(free_mb), "total_mb": int(total_mb),
               "overhead_mb": _mb(OVERHEAD_BYTES)}
    if budget_mb > free_mb:
        return FitResult(False, max_len, share, dtype, need_mb, budget_mb,
                         f"vLLM would take {budget_mb} MiB of the GPU ({share:.2f} of "
                         f"{total_mb} MiB), but only {free_mb} MiB are free.", note,
                         "share_above_free", **numbers)
    if need_mb > budget_mb:
        return FitResult(False, max_len, share, dtype, need_mb, budget_mb,
                         f"The model needs about {need_mb} MiB (weights "
                         f"{_mb(shape.weights_bytes)}, KV cache {_mb(kv_total)} for {max_len} "
                         f"tokens in {dtype}, overhead {_mb(OVERHEAD_BYTES)}), but vLLM gets "
                         f"{budget_mb} MiB ({share:.2f} of {total_mb} MiB, {free_mb} MiB free).",
                         note, "too_big", **numbers)
    return FitResult(True, max_len, share, dtype, need_mb, budget_mb,
                     f"Fits: about {need_mb} of {budget_mb} MiB (context {max_len}, KV cache "
                     f"{dtype}, GPU share {share:.2f}).", note, "fits", **numbers)


def _gpu_memory() -> tuple:
    """(free_mb, total_mb) of the first NVIDIA GPU; (0, 0) when unknown."""
    from vaf.core.gpu_detection import detect_nvidia_gpu, nvidia_free_vram_mb
    gpu = detect_nvidia_gpu()
    total = int(getattr(gpu, "vram_mb", 0) or 0) if gpu else 0
    return nvidia_free_vram_mb(), total


def _configured_utilization() -> float:
    try:
        from vaf.core.config import Config
        value = float(Config.get("vllm_gpu_memory_utilization", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if 0 < value <= 1 else 0.0


def plan_fit(model_dir: str, n_ctx: int, kv_dtype: Optional[str] = None) -> FitResult:
    """The fit for the model on this GPU now (`vaf engine vllm` prints it, the start obeys)."""
    shape = ModelShape.from_dir(model_dir)
    if shape is None:
        return FitResult(False, int(n_ctx), 0.0, kv_dtype or "fp8", 0, 0,
                         f"The model's config.json in {model_dir} does not say its shape.",
                         code="no_shape")
    free_mb, total_mb = _gpu_memory()
    if not total_mb or not free_mb:
        return FitResult(False, int(n_ctx), 0.0, kv_dtype or "fp8", 0, 0,
                         "nvidia-smi does not report the GPU's memory.", code="no_gpu_memory")
    return fit(shape, n_ctx, free_mb, total_mb, kv_dtype=kv_dtype,
               utilization=_configured_utilization())


# ── start / stop ─────────────────────────────────────────────────────────────────


def build_command(model_dir: str, repo_id: str, port: int, result: FitResult,
                  tool_parser: str, reasoning_parser: str) -> List[str]:
    """The launch argument list (no shell)."""
    argv = [str(vllm_exe()), "serve", str(model_dir),
            "--host", "127.0.0.1", "--port", str(int(port)),
            "--served-model-name", repo_id,
            "--max-model-len", str(result.max_model_len),
            "--gpu-memory-utilization", f"{result.utilization:.2f}",
            "--kv-cache-dtype", result.kv_dtype,
            "--enable-auto-tool-choice", "--tool-call-parser", tool_parser]
    if reasoning_parser:
        argv += ["--reasoning-parser", reasoning_parser]
    return argv


# Switches that would open the server beyond chat completions; pinned off even though no
# VLLM_ variable is passed on (belt and braces against a later change to that rule).
_PINNED_OFF = ("VLLM_SERVER_DEV_MODE", "VLLM_ALLOW_RUNTIME_LORA_UPDATING")


def child_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The server's environment: VAF's environment without any VLLM_ variable of its own,
    plus the switches below. A VLLM_ variable is a launch decision like a command-line flag
    (development endpoints, runtime LoRA loading, plugins that are imported at start), and
    there is no free-form way to pass those on either. VAF's own GPU hide for in-process
    torch is taken out (``gpu_detection.gpu_child_env``): the server needs the GPU."""
    from vaf.core.gpu_detection import gpu_child_env
    # Without VAF's GPU hide for in-process torch (a user's own CUDA_VISIBLE_DEVICES stays).
    env = {k: v for k, v in gpu_child_env(base).items() if not k.upper().startswith("VLLM_")}
    for name in _PINNED_OFF:
        env[name] = "0"
    env["VLLM_SKIP_MODEL_NAME_VALIDATION"] = "1"
    env["VLLM_NO_USAGE_STATS"] = "1"
    env["DO_NOT_TRACK"] = "1"
    # The model is in the cache before the launch (ensure_model), so vLLM never needs to
    # reach Hugging Face on a start.
    env["HF_HUB_OFFLINE"] = "1"
    return env


def _looks_like_vllm_server(pid: int) -> bool:
    """True only when ``pid`` is the managed server: its command line carries the managed
    ``vllm`` executable as one argument, followed by ``serve``. Fails closed: a stale pid file
    can name any process after a reboot, and this guards a SIGKILL to a whole process group,
    so a process that merely mentions a path inside the environment does not qualify."""
    try:
        if pid <= 0 or pid == os.getpid():
            return False
        import psutil
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        argv = [str(a) for a in (proc.cmdline() or [])]
    except Exception:
        return False
    exe = str(vllm_exe())
    return any(arg == exe and argv[i + 1:i + 2] == ["serve"] for i, arg in enumerate(argv))


def _read_pid() -> Optional[int]:
    try:
        return int(pid_file().read_bytes().decode("utf-8").strip())
    except (OSError, ValueError):
        return None


def _remove_pid(only: Optional[int] = None) -> None:
    """Remove the pid file; with ``only``, only while it still names that pid (another
    process may have started a server and written its own pid since)."""
    if only is not None and _read_pid() != only:
        return
    try:
        pid_file().unlink()
    except OSError:
        pass


def running_pid() -> Optional[int]:
    """The managed vLLM's pid when one runs (its pid file, its identity verified), else None."""
    pid = _read_pid()
    return pid if pid is not None and _looks_like_vllm_server(pid) else None


def health_status(port: int = 8080, timeout: float = 1.0) -> Optional[int]:
    """The HTTP status of ``/health``, None when nothing answers (also while vLLM loads: its
    socket is bound before the engine is up, but nothing is served yet)."""
    import requests
    try:
        return requests.get(f"http://127.0.0.1:{int(port)}/health", timeout=timeout).status_code
    except Exception:
        return None


def served_model(port: int = 8080) -> Optional[str]:
    """The repo id the vLLM on ``port`` serves, None when it is no vLLM or does not answer."""
    import requests
    try:
        r = requests.get(f"http://127.0.0.1:{int(port)}/v1/models", timeout=2)
        if r.status_code != 200:
            return None
        for entry in (r.json().get("data") or []):
            if isinstance(entry, dict) and entry.get("owned_by") == "vllm":
                return str(entry.get("id") or "") or None
    except Exception:
        return None
    return None


def _ready_budget() -> float:
    from vaf.core.backend import _server_ready_budget
    return _server_ready_budget()


def stop(wait: float = 15.0) -> str:
    """Stop the managed vLLM: its whole process group, SIGTERM then SIGKILL after ``wait``,
    and wait for it to be gone. Only a pid whose command line names the managed environment
    is stopped; a pid file naming anything else is a stale record and only removed. Returns
    "" when it stopped or none ran, else why it is still running."""
    global _child
    pid = _read_pid()
    if pid is not None and not _looks_like_vllm_server(pid):
        _remove_pid(only=pid)       # a stale record, nothing to signal
        pid = None
    if pid is None:
        # Without a usable pid file, the server this process started is still ours to stop.
        child = _child
        if child is None or child.poll() is not None or not _looks_like_vllm_server(child.pid):
            return ""
        pid = child.pid
    from vaf.core.platform import Platform
    _ui().event("Server", f"Stopping vLLM (PID {pid})...", style="dim")
    try:
        # The API server leads its own session (start_new_session), so the group id is
        # its pid; the EngineCore children hold the GPU and are members of it.
        Platform.terminate_process_tree(pid, grace=wait, pgid=pid)
    except Exception as e:      # noqa: BLE001 - checked below
        _ui().event("Server", f"Stopping vLLM: {e}", style="yellow")
    if _child is not None and _child.pid == pid:
        try:
            _child.wait(timeout=5)
        except Exception:
            pass
        _child = None
    deadline = time.monotonic() + 5.0
    while _looks_like_vllm_server(pid):
        if time.monotonic() >= deadline:
            return f"vLLM (PID {pid}) did not stop."
        time.sleep(0.1)
    _remove_pid(only=pid)
    return ""


def _start_lock():
    """Held for the check-spawn-record step of a start, across processes: two VAF processes
    (the tray and a terminal) must not both find no server and both spawn one."""
    from filelock import FileLock
    root = venv_dir().parent
    root.mkdir(parents=True, exist_ok=True)
    return FileLock(str(root / "start.lock"))


_BIND_FAILURE = re.compile(r"address already in use|errno 98|only one usage of each socket",
                           re.IGNORECASE)


def _open_log(previous_kept: bool):
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if previous_kept and path.exists():
            os.replace(path, path.with_name("vllm_server.prev.log"))
        return path, open(path, "a" if not previous_kept else "w", encoding="utf-8",
                          errors="replace")
    except Exception:
        return None, None


def _read_log_tail(path: Optional[Path], chars: int = 800) -> str:
    if path is None:
        return ""
    try:
        return path.read_bytes().decode("utf-8", errors="replace")[-chars:]
    except OSError:
        return ""


def start(repo_id: str, n_ctx: int, port: int = 8080,
          popen: Optional[Callable[..., subprocess.Popen]] = None) -> bool:
    """Launch vLLM for ``repo_id`` and wait until ``/health`` answers 200. False, with the
    reason said, when it is refused (profile, not installed, model missing, unknown parser,
    does not fit) or does not come up. A first launch with the fp8 KV cache that dies is
    retried once with ``auto``. The caller holds the start lock and has handed the port
    over (backend.ServerManager)."""
    global _child, _kv_fp8_failed
    from vaf.core import inference_profile, local_engine
    UI = _ui()
    popen = popen or subprocess.Popen
    if not inference_profile.local_inference_allowed():
        UI.event("Backend", inference_profile.refuse("the local server"), style="dim")
        return False
    if not is_installed():
        UI.error("The local engine is vLLM, but vLLM is not installed. Run `vaf engine vllm` "
                 "to install it, or `vaf engine llama` to go back to llama.cpp.")
        return False
    if not is_current():
        # Refused before any launch: the earlier recipe's environment dies on its CUDA
        # extension a minute into the start.
        UI.error(f"vLLM in {venv_dir()} was installed by an earlier VAF and cannot start; run "
                 "`vaf engine vllm` to rebuild it, or `vaf engine llama`.")
        return False
    model_dir = local_snapshot(repo_id)
    if not model_dir:
        UI.error(f"The vLLM model {repo_id} is not downloaded. Run `vaf engine vllm` to fetch it.")
        return False
    tool, reasoning = local_engine.parsers_for(repo_id)
    refusal = local_engine.parser_refusal(repo_id, tool, reasoning)
    if refusal:
        UI.error(refusal)
        return False
    n_ctx = max(int(n_ctx or 0), MIN_CONTEXT)
    dtypes = ["auto"] if _kv_fp8_failed else ["fp8", "auto"]
    for attempt, dtype in enumerate(dtypes, start=1):
        result = plan_fit(model_dir, n_ctx, kv_dtype=dtype)
        if result.note:
            UI.event("System", result.note, style="warning")
        if not result.ok:
            UI.error("vLLM is not started: " + result.reason
                     + ("" if dtype == "fp8" else " llama.cpp (`vaf engine llama`) runs with a "
                        "quantized KV cache that needs less memory."))
            return False
        argv = build_command(model_dir, repo_id, port, result, tool, reasoning)
        if not inference_profile.local_inference_allowed():
            UI.event("Backend", inference_profile.refuse("the local server"), style="dim")
            return False
        UI.event("Server", f"Starting vLLM on :{port} ({repo_id}, context {result.max_model_len}, "
                           f"KV {dtype}, GPU share {result.utilization:.2f})...", style="dim")
        with _start_lock():
            # Another process may have spawned the server since the caller looked: its pid
            # file must not be overwritten, and a second server would only die at bind.
            other = running_pid()
            if other is not None:
                UI.event("Server", f"vLLM (PID {other}) was started by another VAF process; "
                                   "the next use waits for it.", style="dim")
                return False
            # Opened only now, under the lock and for a launch that happens: opening rotates
            # the log, and a refused start that rotated it would move the running server's
            # log aside and leave its starter an empty one to read a bind failure from.
            log_file_path, log_handle = _open_log(previous_kept=(attempt == 1))
            out = log_handle if log_handle is not None else subprocess.DEVNULL
            try:
                proc = popen(argv, stdout=out, stderr=out, env=child_env(),
                             start_new_session=(os.name != "nt"))
            except Exception as e:      # noqa: BLE001 - reported
                if log_handle is not None:
                    log_handle.close()
                UI.error(f"vLLM could not be launched: {e}")
                return False
            with _lock:
                _child = proc
            try:
                pid_file().parent.mkdir(parents=True, exist_ok=True)
                pid_file().write_bytes(str(proc.pid).encode("utf-8"))
            except OSError:
                pass
        outcome = _wait_ready(proc, port)
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:
                pass
        if outcome == "ready":
            if dtype == "auto" and attempt > 1:
                _kv_fp8_failed = True
            return True
        if outcome in ("switched", "timeout"):
            stop()
            if outcome == "switched":
                UI.event("Backend", inference_profile.refuse("the local server"), style="dim")
            else:
                UI.error(f"vLLM was not ready after {int(_ready_budget())}s and is stopped. For a "
                         "slow first start raise server_ready_timeout in the config.")
            return False
        # The API server died; an EngineCore child it left in its group would keep the GPU.
        try:
            from vaf.core.platform import Platform
            Platform.terminate_process_group(proc.pid)
        except Exception:
            pass
        _remove_pid(only=proc.pid)
        if _BIND_FAILURE.search(_read_log_tail(log_file_path, chars=20000)):
            # The port, not the KV cache: a retry would die the same way.
            UI.error(f"vLLM could not bind 127.0.0.1:{port}: another program holds the port. "
                     "vLLM is not started.")
            return False
        if attempt < len(dtypes):
            UI.event("Server", "vLLM died with the fp8 KV cache - retrying with the model's own "
                               "KV dtype...", style="yellow")
            continue
        UI.error(f"vLLM failed to start. Check {log_file_path}\n"
                 f"{_read_log_tail(log_file_path)[-500:]}")
        return False
    return False


def _wait_ready(proc, port: int) -> str:
    """"ready", "died", "switched" (the profile went to api) or "timeout"."""
    from vaf.core import inference_profile
    deadline = time.monotonic() + _ready_budget()
    while True:
        if proc.poll() is not None:
            return "died"
        if not inference_profile.local_inference_allowed():
            return "switched"
        if time.monotonic() >= deadline:
            return "timeout"
        if health_status(port, timeout=1.0) == 200:
            return "ready"
        time.sleep(1.0)
