# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The managed vLLM server: install, model, fit, start and stop.

The engine choice and its platform verdicts are in vaf/core/local_engine.py; this module
runs vLLM once it is chosen. ``ServerManager.start_server`` (vaf/core/backend.py) decides
WHEN (handover, reuse, waiting for a server that is loading); this module does the work.

- **Install** (``install``): only on request (`vaf engine vllm`, Settings, the TUI, all
  through ``local_engine.apply``), never on a start. A fresh environment under
  ``bin/vllm/venv`` (gitignored, next to the llama-server binary) made with VAF's own
  Python, the pinned ``vllm==PIN`` installed from PyPI, its CUDA 13.0 build and the CUDA
  13.0 PyTorch it requires, from the hash-locked tree ``vllm_requirements.lock``
  (``--require-hashes --no-deps --only-binary=:all:``: pip refuses any file whose hash the
  lock does not name, any package it does not pin, and any build from source), and a probe
  that PyTorch in it sees the GPU AND vLLM's CUDA extension loads. Only a passing probe
  writes the recipe record (``RECIPE_FORMAT`` in ``vaf_recipe``); an environment without it
  is rebuilt by the next install and refused by ``start``. Every step runs without VAF's GPU
  hide for in-process torch (``gpu_detection.gpu_child_env``). vLLM is never imported into
  VAF's process.
  On an AMD GPU (``backend()`` is ``rocm``) the same steps install vLLM's ROCm build: an
  environment of a Python 3.12 (``rocm_python``; the ROCm wheels are cp312 only), the
  hash-locked ``vllm_requirements_rocm.lock`` with ``ROCM_INDEX`` as an extra index on the
  command line, a probe that also loads ``vllm._rocm_C``, and its own record
  (``RECIPE_FORMAT_ROCM``). An environment of the other build is not current, so a GPU that
  changed vendor gets a fresh one. The ROCm runtime itself is the host's (ROCm 7.2 or newer):
  the wheels link against ``libamdhip64.so.7`` without bundling it.
- **Model** (``ensure_model``): a Hugging Face repo in safetensors form, in the standard
  Hugging Face cache, which vLLM reads itself; no code file of the repo is fetched
  (``CODE_PATTERNS`` are ignored), and a sharded download counts only when every shard of
  its index is there.
- **Fit** (``fit``): weights + KV cache for the context + an overhead, against the memory
  share vLLM gets, before the launch, so a model that cannot fit is refused with numbers
  instead of a crash a minute later; a context that does not fit is reduced to the largest
  that does above ``MIN_CONTEXT``. The GPU's memory comes from nvidia-smi, or on ROCm from
  the amdgpu driver in /sys (``gpu_detection.amd_vram_mb``). An automatic share keeps
  ``DESKTOP_RESERVE_MB`` of the card's total outside vLLM when the GPU drives a display or
  that cannot be read (``desktop_reserve_mb``), and is floored to 0.01, so the launch
  argument is the share the fit checked; a share set by hand (``SHARE_RANGE``) is never
  capped. ``share_table`` is the GPU memory slider's table.
- **Attention plan** (``launch_plan``, CUDA build only): where FlashInfer cannot build its
  kernels (no CUDA toolkit, ``flashinfer_buildable``), the launch names an attention backend
  and KV cache that need no build and turns FlashInfer's sampler off (``attention_plan``);
  measured on compute capability 8.6, the other rows read from vLLM's sources.
- **Start** (``start``): on the local inference port with ``--enable-auto-tool-choice`` and
  the family's parsers, in a new session (the API server and its EngineCore children are
  one process group), model-name validation off (every consumer of the local server sends
  its own model name or none), usage statistics and Hugging Face access off.
  ``--trust-remote-code`` is never passed: it would run code from the model repo. A start
  that failed is remembered for its signature and not run again until a switch clears it
  (``remembered_failure``, ``clear_start_failure``).
- **First start** (``first_start``): the switch to vLLM starts it once, sends one chat
  completion with top-p/top-k and stops it again, so the engine changes only for a model
  that runs here.
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
from dataclasses import dataclass, replace
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

# The two builds of PIN: ``cuda`` (the PyPI wheel, NVIDIA) and ``rocm`` (vLLM's ROCm build
# from its versioned wheel index, AMD).
CUDA = "cuda"
ROCM = "rocm"
# The ROCm build of PIN: wheels.vllm.ai/rocm/<PIN>/ lists one variant, rocm723 ("ROCm commit
# 5df63a7..."); rocm700 is 404 there. Its wheels are cp312, x86_64, manylinux_2_39 only, and
# vllm's METADATA pins torch, triton, torchvision, torchaudio, amdsmi, flash-attn and
# amd-aiter to the builds of the same index. They do not bundle the ROCm runtime: the host
# needs ROCm 7.x (local_engine.MIN_ROCM). Re-read on every PIN bump (docs/setup/RELEASING.md).
ROCM_VARIANT = "rocm723"
ROCM_VERSION = "7.2.3"
# Passed as an EXTRA index on pip's command line, never written into the lock: pip honours
# index options inside a requirements file, and tests/test_vllm_lock.py refuses any there.
# --require-hashes makes the second index safe: a file of the same name from another index
# has another hash and is refused.
ROCM_INDEX = f"https://wheels.vllm.ai/rocm/{PIN}/{ROCM_VARIANT}"
# Generated with uv for cp312 x86_64 manylinux_2_39 (pip-compile resolves only for its own
# interpreter); the command is in vllm_requirements_rocm.in.
LOCK_PATH_ROCM = Path(__file__).with_name("vllm_requirements_rocm.lock")
# Measured lower bound: the ROCm-index files sum to 2.73 GB (torch 1349 MB, amd-aiter 532,
# triton 425, vllm 301, flash-attn 125), the PyPI part of the tree at least 0.62 GB.
DOWNLOAD_GB_AT_LEAST_ROCM = 3.3
# The recipe record of the ROCm build: its own tag, so a CUDA environment on a machine that
# now has an AMD GPU (or the reverse) is not current and is rebuilt.
RECIPE_FORMAT_ROCM = "vllmrocm-1-a69536"
# The ROCm probe: HIP answers through torch's cuda API; vLLM's ROCm platform module imports
# vllm._C_stable_libtorch and vllm._rocm_C at its first start (vllm/platforms/rocm.py in
# 0.32.0); a PIN bump re-checks the names (RELEASING.md).
_PROBE_ROCM = (
    "import sys\n"
    "import torch\n"
    "if not torch.cuda.is_available():\n"
    "    sys.exit(3)\n"
    "try:\n"
    "    import vllm._C_stable_libtorch\n"
    "    import vllm._rocm_C\n"
    "except ImportError as e:\n"
    "    print(e, file=sys.stderr)\n"
    "    sys.exit(4)\n"
)

# What the fit check counts besides weights and KV cache: CUDA context, activations, the
# graphs vLLM captures and torch.compile's first run. Measured on an RTX 3080 (Qwen3-4B-AWQ,
# int8 KV, share 0.62): about 1.85 GiB on a cold start (the first torch.compile), about
# 1.2 GiB once the compile cache is warm. The cold figure counts, because the first start of
# a switch is the one that must not fail.
OVERHEAD_BYTES = int(1.85 * 1024 ** 3)
# Headroom left free on the GPU when the memory share is computed from the free figure.
FREE_HEADROOM_MB = 512
# The clamp of a share computed from the free memory.
UTILIZATION_RANGE = (0.50, 0.92)
# The range of a share set by hand (the Settings slider, `vaf engine vllm --gpu-share`, the
# TUI). Not up to 1.0: above 0.95 nothing is left for any other GPU context on the card;
# below 0.30 not even the 4B default fits on a 10 GB card, while a large card can still give
# vLLM a small part.
SHARE_RANGE = (0.30, 0.95)
# GPU memory of the card's total that an automatic share keeps outside vLLM when the GPU drives
# a display (or when that cannot be read): the share is at most (total - reserve) / total. It
# is room for the screen and other programs, not a promise of free memory: what they already
# hold counts against it, and the share from the free figure can be smaller. vLLM
# preallocates its whole share up front, and the free figure at launch covers only what the
# desktop holds at that moment. Measured on an RTX 3080 (10240 MiB, KDE Plasma
# on Wayland): desktop programs hold 3.1 to 3.6 GB of GPU memory; at share 0.78 (2253 MiB
# left) the compositor failed to allocate a buffer during a long turn and the screen froze;
# at 0.66 to 0.68 (3277 to 3482 MiB left) it stayed alive. 3328 MiB puts the automatic share
# of that card at 0.67, inside the band that worked, where the 4B default still fits at the
# 32768 floor. A share set by hand is never capped by it: the slider is the override.
DESKTOP_RESERVE_MB = 3328
# VAF's own request (system prompt and routed tools) needs this much context.
MIN_CONTEXT = 32768
# A context reduced to fit is a multiple of this.
CONTEXT_STEP = 1024

# The files of a model repo the engine needs. No Python file, ever: "tokenizer*" alone
# would also match a tokenizer.py, so code files are excluded explicitly (remote code never
# runs, and it is not even fetched).
MODEL_PATTERNS = ["*.json", "*.safetensors", "tokenizer*", "*.model", "*.txt", "*.jinja"]
CODE_PATTERNS = ["*.py", "*.pyc", "*.pyo", "*.so", "*.dll", "*.dylib", "*.sh", "*.exe"]

# The child of THIS process, so stop() can reap it (a killed child that is never waited for
# stays a zombie for the life of a long-running parent).
_child: Optional[subprocess.Popen] = None
_lock = threading.Lock()
# The build of this machine, once a GPU was found (see backend).
_backend: Optional[str] = None


def backend() -> str:
    """``rocm`` when the primary GPU is an AMD one, else ``cuda`` (also without a GPU: the
    verdict refuses that machine before anything installs). The primary GPU follows
    ``gpu_detection.get_primary_gpu`` (NVIDIA before AMD), without its Intel and Apple probes,
    which cannot change the answer. Kept for the process once a GPU was found: the hardware
    does not change while VAF runs; not kept without one (a driver still loading)."""
    global _backend
    if _backend is not None:
        return _backend
    try:
        from vaf.core.gpu_detection import detect_amd_gpu, detect_nvidia_gpu
        if detect_nvidia_gpu() is not None:
            _backend = CUDA
        elif detect_amd_gpu() is not None:
            _backend = ROCM
    except Exception:
        return CUDA
    return _backend or CUDA


def expected_version(build: Optional[str] = None) -> str:
    """The version an environment of ``build`` reports: PIN, with the local label of the ROCm
    build (``0.32.0+rocm723``)."""
    return f"{PIN}+{ROCM_VARIANT}" if (build or backend()) == ROCM else PIN


def lock_path(build: Optional[str] = None) -> Path:
    return LOCK_PATH_ROCM if (build or backend()) == ROCM else LOCK_PATH


def recipe_format(build: Optional[str] = None) -> str:
    return RECIPE_FORMAT_ROCM if (build or backend()) == ROCM else RECIPE_FORMAT


def download_gb_at_least(build: Optional[str] = None) -> float:
    return DOWNLOAD_GB_AT_LEAST_ROCM if (build or backend()) == ROCM else DOWNLOAD_GB_AT_LEAST


def rocm_python() -> str:
    """The Python 3.12 the ROCm environment is made with (its wheels are cp312 only): VAF's
    own when it runs on 3.12, else ``python3.12`` on PATH; "" when there is none."""
    from vaf.core.local_engine import ROCM_PYTHON
    if tuple(sys.version_info[:2]) == ROCM_PYTHON:
        return sys.executable
    return shutil.which(f"python{ROCM_PYTHON[0]}.{ROCM_PYTHON[1]}") or ""


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
    """True when the environment carries this version's recipe record for this machine's
    build (``RECIPE_FORMAT`` or ``RECIPE_FORMAT_ROCM``)."""
    try:
        text = recipe_path().read_bytes().decode("utf-8", errors="replace").strip()
    except OSError:
        return False
    return text == recipe_format()


def is_current() -> bool:
    """Installed at the pin of this machine's build AND built by its recipe, with a passing
    probe. An environment that is installed but not current cannot be trusted to start (the
    earlier recipe's could not, nor can the other build's) and is rebuilt by ``install``."""
    return is_installed() and installed_version() == expected_version() and recipe_current()


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
    CUDA GPU), ``"no_rocm"`` (the ROCm build's PyTorch sees no ROCm GPU) or ``"libs"`` (vLLM's
    GPU extensions do not load, or the probe did not run).

    The build follows ``backend()``: ``cuda`` installs ``LOCK_PATH`` from PyPI into an
    environment of VAF's own Python, ``rocm`` installs ``LOCK_PATH_ROCM`` with ``ROCM_INDEX``
    as an extra index into an environment of ``rocm_python()``."""
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
    build = backend()
    lock = lock_path(build)
    if not lock.is_file():
        # Checked before the old environment is removed: without the lock nothing installs.
        return (f"The hash-locked vLLM tree {lock} is missing from this VAF installation; "
                "reinstall VAF.")
    from vaf.core.gpu_detection import gpu_child_env
    # Without VAF's GPU hide for in-process torch: the probe must see the GPU vLLM will use.
    env = gpu_child_env()
    env = with_rocm_library(env)
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
                              outcome if outcome is not None else {}, build)
    finally:
        if sink is not None:
            try:
                sink.close()
            except OSError:
                pass


def _install_steps(say, run, step, env, root, streams, where, outcome, build=CUDA) -> str:
    from vaf.core import inference_profile
    if build == ROCM:
        steps = [
            ("install_env", "Creating the environment", [rocm_python(), "-m", "venv", str(root)]),
            ("install_vllm", f"Installing vLLM {PIN} for ROCm {ROCM_VERSION} (at least "
                             f"{DOWNLOAD_GB_AT_LEAST_ROCM:.1f} GB to download)",
             [str(venv_python()), "-m", "pip", "install", "--require-hashes", "--no-deps",
              "--only-binary=:all:", "--extra-index-url", ROCM_INDEX, "-r", str(LOCK_PATH_ROCM)]),
        ]
    else:
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
            hint = ""
            if build == ROCM and code == "install_env":
                # Debian and Ubuntu split ensurepip out of the interpreter package.
                hint = (" Python 3.12 needs its venv module (on Debian and Ubuntu the package "
                        "python3.12-venv).")
            return f"{label} failed (exit {getattr(result, 'returncode', '?')}); {where}.{hint}"
    # Deliberately no `vllm download-kernels`: it lets FlashInfer pip-install flashinfer-cubin and
    # flashinfer-jit-cache from its own index, version-pinned but unhashed, the only compiled
    # package that would bypass the lock. Without them FlashInfer can only JIT-compile its
    # kernels, which needs a CUDA toolkit (nvcc) and ninja; where they are missing the launch
    # avoids FlashInfer (launch_plan). On compute capability 9.0+ the locked FlashInfer also
    # downloads some kernels at runtime, checked against checksums pinned in the locked wheel
    # (docs/llm/VLLM_ENGINE.md).
    step("install_probe")
    say("Checking that vLLM loads on the GPU...")
    try:
        probe = run([str(venv_python()), "-c", _PROBE_ROCM if build == ROCM else _PROBE],
                    env=env, **streams)
        code = getattr(probe, "returncode", 1)
    except Exception:
        code = None
    if code == 0:
        try:
            recipe_path().write_bytes(recipe_format(build).encode("utf-8"))
        except OSError as e:
            return f"vLLM was installed, but its recipe record could not be written: {e}"
        return ""
    # No record: the next install rebuilds this environment, and start() refuses it.
    if build == ROCM:
        if code == 3:
            outcome["probe"] = "no_rocm"
            from vaf.core.local_engine import MIN_ROCM
            return (f"vLLM was installed, but PyTorch in its environment sees no ROCm GPU. vLLM "
                    f"{PIN}'s ROCm build needs ROCm {MIN_ROCM[0]}.{MIN_ROCM[1]} or newer "
                    "installed, this account in the render and video groups, and no "
                    "HIP_VISIBLE_DEVICES, ROCR_VISIBLE_DEVICES or CUDA_VISIBLE_DEVICES that "
                    "hides the GPU: fix that and try again.")
        outcome["probe"] = "libs"
        return ("vLLM was installed, but its ROCm libraries do not load in its environment, so "
                "it cannot start. Run `vaf engine vllm` again: it rebuilds the environment, and "
                f"llama.cpp keeps running meanwhile. If it fails again, {where} for why.")
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
    ``too_big_reserve``, ``share_above_free``, ``no_shape``, ``no_gpu_memory``."""
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
    other_mb: int = 0
    attention_backend: str = ""
    # A refusal for a model larger than the smallest default: a smaller model may fit.
    smaller_model: bool = False
    # The desktop reserve that capped an automatic share (0 when it did not cap it).
    reserve_mb: int = 0
    # Whether the GPU drives a display (None: unknown), as the fit was planned.
    display: Optional[bool] = None
    # What the share leaves on the GPU for everything else (total - budget).
    leaves_mb: int = 0
    # A share set by hand that leaves less than the desktop reserve on a GPU that drives a
    # display (or whose display state is unknown): the screen can freeze.
    below_reserve: bool = False
    # On ``too_big_reserve``: the smallest share (0.01 steps) at which the model fits.
    min_share: float = 0.0


FIT_CODES = ("fits", "too_big", "too_big_reserve", "share_above_free", "no_shape",
             "no_gpu_memory")


def _mb(value: float) -> int:
    return int(round(value / 1024 ** 2))


def floor_share(value: float) -> float:
    """``value`` floored to 0.01. The launch argument carries two decimals, and a rounded one
    could hand vLLM up to half a percent more than the fit checked (0.675 prints as 0.68).
    The epsilon keeps a product like ``0.57 * 100 = 56.99999999999999`` at 0.57."""
    import math
    return math.floor(float(value) * 100 + 1e-6) / 100


def kv_bytes_per_token(shape: ModelShape, kv_dtype: str) -> int:
    """The KV cache of one token, keys and values of every layer. ``fp8*`` stores one byte
    per element, ``*_per_token_head`` (``int8_per_token_head``) one byte plus a float32 scale
    per token, head and side (vllm/v1/attention/backends/triton_attn.py in 0.32.0), anything
    else (``auto``) the model's 16-bit dtype."""
    dtype = str(kv_dtype or "")
    if dtype.endswith("_per_token_head"):
        per_head = shape.head_dim * 1 + 4
    elif dtype.startswith("fp8"):
        per_head = shape.head_dim * 1
    else:
        per_head = shape.head_dim * 2
    return 2 * shape.layers * shape.kv_heads * per_head


def auto_share(free_mb: int, total_mb: int, reserve_mb: int = 0) -> float:
    """The share computed from the free memory: ``(free - 512 MiB) / total`` within
    0.50..0.92, then at most ``(total - reserve) / total``, floored to 0.01."""
    low, high = UTILIZATION_RANGE
    share = min(high, max(low, (free_mb - FREE_HEADROOM_MB) / total_mb)) if total_mb else low
    if reserve_mb and total_mb:
        share = min(share, (total_mb - reserve_mb) / total_mb)
    return floor_share(share)


def fit(shape: ModelShape, n_ctx: int, free_mb: int, total_mb: int, *,
        kv_dtype: Optional[str] = None, utilization: float = 0.0,
        model: str = "", reserve_mb: int = 0, check_free: bool = True) -> FitResult:
    """Does the model fit, and with what? ``n_ctx`` is the context (``--max-model-len``),
    clamped to the model's maximum. The KV cache is fp8 unless ``kv_dtype`` says otherwise
    (``auto`` = the model's own 16-bit dtype). ``utilization`` 0 = computed from the free
    memory (``auto_share``): ``(free - 512 MiB) / total`` within 0.50..0.92, capped so that
    ``reserve_mb`` of the total stays outside the share (the desktop reserve of a GPU that
    drives a display), floored to 0.01. A share set by hand is taken as it is (two decimals)
    and never capped by the reserve; one that leaves less than ``reserve_mb`` is marked
    ``below_reserve``. vLLM refuses to start when its share is more than what is free, so
    that counts as not fitting too, unless ``check_free`` is False (the share table of the
    slider).

    When the context does not fit, the largest one that does (a multiple of
    ``CONTEXT_STEP``) is taken, never below ``MIN_CONTEXT``, with a note; only a model that
    does not fit at ``MIN_CONTEXT`` is refused. A refusal where the reserve capped the share
    and the uncapped share would fit is ``too_big_reserve``, with ``min_share``. ``model``
    (the repo id) decides whether the refusal names a smaller model: not for the smallest
    default."""
    note = ""
    max_len = int(n_ctx)
    if shape.max_position and max_len > shape.max_position:
        note = (f"The context {max_len} is more than {shape.max_position}, the most this "
                f"model takes; it runs with {shape.max_position}.")
        max_len = shape.max_position
    dtype = kv_dtype or "fp8"
    per_token = kv_bytes_per_token(shape, dtype)
    manual = bool(utilization) and 0 < float(utilization) <= 1
    capped_by = 0
    if manual:
        share = round(float(utilization), 2)
    else:
        share = auto_share(free_mb, total_mb, reserve_mb)
        if reserve_mb and share < auto_share(free_mb, total_mb):
            capped_by = int(reserve_mb)
    budget_mb = int(share * total_mb)
    leaves_mb = max(int(total_mb) - budget_mb, 0)
    rest = budget_mb * 1024 ** 2 - shape.weights_bytes - OVERHEAD_BYTES
    if per_token * max_len > rest and max_len > MIN_CONTEXT:
        fits_len = (max(rest, 0) // per_token) // CONTEXT_STEP * CONTEXT_STEP
        if fits_len >= MIN_CONTEXT:
            note = ((note + " ") if note else "") + (
                f"The context is reduced to {fits_len} tokens so the model fits.")
            max_len = fits_len
        else:
            max_len = MIN_CONTEXT
    kv_total = per_token * max_len
    need_mb = _mb(shape.weights_bytes + kv_total + OVERHEAD_BYTES)
    other_mb = max(int(total_mb) - int(free_mb), 0)
    numbers = {"weights_mb": _mb(shape.weights_bytes), "kv_mb": _mb(kv_total),
               "free_mb": int(free_mb), "total_mb": int(total_mb),
               "overhead_mb": _mb(OVERHEAD_BYTES), "other_mb": other_mb,
               "reserve_mb": capped_by, "leaves_mb": leaves_mb,
               "below_reserve": manual and bool(reserve_mb) and leaves_mb < int(reserve_mb)}
    if check_free and budget_mb > free_mb:
        return FitResult(False, max_len, share, dtype, need_mb, budget_mb,
                         f"vLLM would take {budget_mb} MiB of the GPU ({share:.2f} of "
                         f"{total_mb} MiB), but only {free_mb} MiB are free.", note,
                         "share_above_free", **numbers)
    if need_mb > budget_mb:
        from vaf.core.local_engine import DEFAULT_MODEL_SMALL
        smaller_helps = bool(model) and model.lower() != DEFAULT_MODEL_SMALL.lower()
        smaller = " or choose a smaller model" if smaller_helps else ""
        if capped_by and fit(shape, n_ctx, free_mb, total_mb, kv_dtype=dtype, model=model,
                             check_free=check_free).ok:
            # The model fits once the reserve is not kept: say so, with the share that does.
            least = floor_share(need_mb / total_mb) if total_mb else 0.0
            while total_mb and int(least * total_mb) < need_mb:
                least = round(least + 0.01, 2)
            smaller_too = " or choose a smaller model" if smaller_helps else ""
            return FitResult(False, max_len, share, dtype, need_mb, budget_mb,
                             f"The model needs about {need_mb} MiB, but vLLM gets {budget_mb} "
                             f"MiB ({share:.2f} of {total_mb} MiB): the automatic share keeps "
                             f"{capped_by} MiB of the card outside vLLM for the screen, because "
                             "this GPU drives a display (or VAF cannot tell). Set the GPU "
                             f"memory by hand to at least {least:.2f} "
                             f"(`vaf engine vllm --gpu-share {least:.2f}`, or Settings > AI & "
                             f"Model){smaller_too}; below the reserve the screen can freeze "
                             "when its memory runs out. The local engine stays as it was.",
                             note, "too_big_reserve", smaller_model=smaller_helps,
                             min_share=least, **numbers)
        return FitResult(False, max_len, share, dtype, need_mb, budget_mb,
                         f"The model needs about {need_mb} MiB (weights "
                         f"{_mb(shape.weights_bytes)}, KV cache {_mb(kv_total)} for {max_len} "
                         f"tokens in {dtype}, overhead {_mb(OVERHEAD_BYTES)}), but vLLM gets "
                         f"{budget_mb} MiB ({share:.2f} of {total_mb} MiB): {free_mb} MiB are "
                         f"free, other programs hold {other_mb} MiB. Close programs that use "
                         f"the GPU and try again{smaller}. The local engine stays as it was; "
                         "llama.cpp can move part of the model to the CPU.",
                         note, "too_big", smaller_model=smaller_helps, **numbers)
    return FitResult(True, max_len, share, dtype, need_mb, budget_mb,
                     f"Fits: about {need_mb} of {budget_mb} MiB (context {max_len}, KV cache "
                     f"{dtype}, GPU share {share:.2f}).", note, "fits", **numbers)


def _gpu_memory() -> tuple:
    """(free_mb, total_mb) of the GPU of this machine's build: the first NVIDIA GPU from
    nvidia-smi, or the AMD GPU from the amdgpu driver (``amd_vram_mb``); (0, 0) when unknown."""
    if backend() == ROCM:
        from vaf.core.gpu_detection import amd_vram_mb
        return amd_vram_mb()
    from vaf.core.gpu_detection import detect_nvidia_gpu, nvidia_free_vram_mb
    gpu = detect_nvidia_gpu()
    total = int(getattr(gpu, "vram_mb", 0) or 0) if gpu else 0
    return nvidia_free_vram_mb(), total


def _display_active() -> Optional[bool]:
    """Whether the GPU of this machine's build drives a display: nvidia-smi's
    ``display_active`` for the first NVIDIA GPU (the one ``_gpu_memory`` reads), or the DRM
    connectors of the AMD GPU's render device; None when unknown."""
    if backend() == ROCM:
        from vaf.core.gpu_detection import amd_display_active
        return amd_display_active()
    from vaf.core.gpu_detection import nvidia_display_active
    return nvidia_display_active()


def desktop_reserve_mb(display: object = ...) -> int:
    """The GPU memory of the card an automatic share keeps outside vLLM:
    ``DESKTOP_RESERVE_MB`` when the GPU
    drives a display AND when that cannot be read (a reserve too many costs context, which
    the slider gives back; one too few freezes the desktop), 0 only when it is known not
    to. ``display`` is a value already read (``_display_active``)."""
    if display is ...:
        display = _display_active()
    return 0 if display is False else DESKTOP_RESERVE_MB


def _configured_utilization() -> float:
    try:
        from vaf.core.config import Config
        value = float(Config.get("vllm_gpu_memory_utilization", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if 0 < value <= 1 else 0.0


def _share_or_configured(utilization: Optional[float]) -> float:
    """An override (``0`` = automatic) or, with None, the configured share."""
    if utilization is None:
        return _configured_utilization()
    try:
        value = float(utilization)
    except (TypeError, ValueError):
        return 0.0
    return value if 0 < value <= 1 else 0.0


def plan_fit(model_dir: str, n_ctx: int, kv_dtype: Optional[str] = None,
             model: str = "", utilization: Optional[float] = None) -> FitResult:
    """The fit for the model on this GPU now (`vaf engine vllm` prints it, the start obeys).
    Without ``kv_dtype`` it is the first attempt of this machine's ``launch_plan``.
    ``utilization`` overrides the configured share (``0`` = automatic; a switch fits the
    share it is about to write); None reads ``vllm_gpu_memory_utilization``. The desktop
    reserve applies when the GPU drives a display or that is unknown (``desktop_reserve_mb``)."""
    plan = launch_plan()
    dtype = kv_dtype or plan.kv_dtypes[0]
    shape = ModelShape.from_dir(model_dir)
    if shape is None:
        return FitResult(False, int(n_ctx), 0.0, dtype, 0, 0,
                         f"The model's config.json in {model_dir} does not say its shape.",
                         code="no_shape")
    free_mb, total_mb = _gpu_memory()
    if not total_mb or not free_mb:
        source = ("The amdgpu driver does not report the GPU's memory (/sys/class/drm)."
                  if backend() == ROCM else "nvidia-smi does not report the GPU's memory.")
        return FitResult(False, int(n_ctx), 0.0, dtype, 0, 0, source,
                         code="no_gpu_memory")
    display = _display_active()
    result = fit(shape, n_ctx, free_mb, total_mb, kv_dtype=dtype,
                 utilization=_share_or_configured(utilization), model=model,
                 reserve_mb=desktop_reserve_mb(display))
    result = replace(result, display=display)
    backend_name = plan.backend_for(dtype)
    return replace(result, attention_backend=backend_name) if backend_name else result


def share_table(model_dir: Optional[str], n_ctx: int, *, model: str = "",
                local_server_running: bool = False) -> Dict[str, object]:
    """What the GPU memory slider shows (Settings, the TUI, `vaf engine`): the card, the
    display and its reserve, the configured share, the automatic share now, and one fit per
    share of ``SHARE_RANGE`` in 0.01 steps (``steps``: share, budget_mb, ok, max_model_len,
    need_mb; checked without the free-memory refusal, which is the start's). ``steps`` is
    empty when the model is not downloaded or its shape is unknown. While a local server runs
    its memory is not free, so ``free_mb`` and ``auto_share`` are None (measured at the next
    start). The one implementation of the numbers: a client only reads them."""
    free_mb, total_mb = _gpu_memory()
    display = _display_active()
    reserve = desktop_reserve_mb(display)
    try:
        dtype = launch_plan().kv_dtypes[0]
    except Exception:
        dtype = "fp8"
    free_known = bool(total_mb) and bool(free_mb) and not local_server_running
    auto = auto_share(free_mb, total_mb, reserve) if free_known else None
    out: Dict[str, object] = {
        "total_mb": int(total_mb or 0) or None,
        "free_mb": int(free_mb) if free_known else None,
        "display": display,
        "reserve_mb": reserve,
        "configured": _configured_utilization(),
        "auto_share": auto,
        "auto_reserved": bool(free_known and reserve and auto is not None
                              and auto < auto_share(free_mb, total_mb)),
        "kv_dtype": dtype,
        "range": list(SHARE_RANGE),
        "min_share": None,
        "steps": [],
    }
    shape = ModelShape.from_dir(model_dir) if model_dir else None
    if shape is None or not total_mb:
        return out
    low, high = (int(round(v * 100)) for v in SHARE_RANGE)
    steps = []
    for pct in range(low, high + 1):
        share = pct / 100
        result = fit(shape, n_ctx, free_mb or total_mb, total_mb, kv_dtype=dtype,
                     utilization=share, model=model, check_free=False)
        steps.append({"share": share, "budget_mb": result.budget_mb, "ok": result.ok,
                      "max_model_len": result.max_model_len, "need_mb": result.need_mb})
    out["steps"] = steps
    out["min_share"] = next((s["share"] for s in steps if s["ok"]), None)
    return out


# ── attention backend and KV cache ───────────────────────────────────────────────

# vLLM's attention backends VAF names on the command line (vllm/v1/attention/backends in
# 0.32.0): Triton (any NVIDIA GPU vLLM runs on, compiled by Triton at run time, no CUDA
# toolkit) and FlashAttention (FA2 from compute capability 8.0, FA3 on 9.x; prebuilt in the
# vllm wheel).
TRITON_ATTN = "TRITON_ATTN"
FLASH_ATTN = "FLASH_ATTN"
# vLLM's quantized KV cache that needs neither native fp8 nor FlashInfer: one byte per
# element plus a float32 scale per token and head, served by TRITON_ATTN only.
INT8_KV = "int8_per_token_head"


@dataclass(frozen=True)
class KvPlan:
    """The launch attempts of this machine, in order: ``kv_dtypes[i]`` with the attention
    backend ``backends[i]`` ("" = vLLM's own choice). ``sampler_off``: the server runs with
    ``VLLM_USE_FLASHINFER_SAMPLER=0``."""
    kv_dtypes: tuple
    backends: tuple
    sampler_off: bool = False

    def backend_for(self, dtype: str) -> str:
        try:
            return self.backends[self.kv_dtypes.index(dtype)]
        except ValueError:
            return ""


def _site_packages() -> List[Path]:
    root = venv_dir()
    return list(root.glob("lib/python*/site-packages")) + [root / "Lib" / "site-packages"]


def flashinfer_buildable(env: Optional[Dict[str, str]] = None) -> bool:
    """Can FlashInfer give vLLM its kernels in the server's environment ``env``? vLLM's own
    check (``has_flashinfer`` in vllm/utils/flashinfer.py of 0.32.0), read without importing
    anything from the environment: ``flashinfer_cubin`` is installed in it, OR FlashInfer's
    JIT finds nvcc (``CUDA_HOME``, ``CUDA_PATH``, ``nvcc`` on PATH, else /usr/local/cuda) AND
    ``ninja`` is on PATH. Measured: when it is False, vLLM 0.32.0 still picks FlashInfer for
    an fp8 KV cache below compute capability 9.0, and for its top-p/top-k sampler, and the
    server dies on "Could not find nvcc"."""
    if any((site / "flashinfer_cubin").is_dir() for site in _site_packages()):
        return True
    source = os.environ if env is None else env
    path = source.get("PATH")
    cuda_home = source.get("CUDA_HOME") or source.get("CUDA_PATH")
    if not cuda_home:
        nvcc_on_path = shutil.which("nvcc", path=path)
        cuda_home = (os.path.dirname(os.path.dirname(nvcc_on_path)) if nvcc_on_path
                     else os.path.join(os.sep, "usr", "local", "cuda"))
    nvcc = shutil.which(os.path.join(cuda_home, "bin", "nvcc"))
    return bool(nvcc) and bool(shutil.which("ninja", path=path))


def attention_plan(capability: Optional[tuple], buildable: bool) -> KvPlan:
    """The KV caches and attention backends to launch with, on the CUDA build.

    With FlashInfer buildable: fp8 then the model's own dtype, vLLM choosing the backend
    (the command of earlier versions). Without it, per compute capability (read in vLLM
    0.32.0: platforms/cuda.py, triton_attn.py, flash_attn.py):

    - below 8.9: ``int8_per_token_head`` with TRITON_ATTN (Triton's fp8 needs 8.9, FA3's fp8
      needs 9.x, FlashInfer would JIT); fallback ``auto`` with FLASH_ATTN (FA2, 8.0 and up)
      or, on 7.5, TRITON_ATTN. Measured on 8.6 (RTX 3080).
    - 9.x: fp8 with FLASH_ATTN (FA3); fallback ``auto`` with FLASH_ATTN.
    - 8.9, 10.x, 12.x: fp8 with TRITON_ATTN; fallback ``auto`` with FLASH_ATTN.

    The backend is always named then, because vLLM's selector picks FlashInfer although its
    own check disabled it (measured), and the FlashInfer sampler is off (it JIT-compiles on
    the first sampled request, measured). Only the 8.6 row is measured; the first start of
    a switch (``first_start``) refuses a row that does not start, before the engine changes."""
    if buildable:
        return KvPlan(("fp8", "auto"), ("", ""), False)
    cap = tuple(capability) if capability else (8, 0)
    if cap < (8, 9):
        return KvPlan((INT8_KV, "auto"),
                      (TRITON_ATTN, FLASH_ATTN if cap >= (8, 0) else TRITON_ATTN), True)
    if cap[0] == 9:
        return KvPlan(("fp8", "auto"), (FLASH_ATTN, FLASH_ATTN), True)
    return KvPlan(("fp8", "auto"), (TRITON_ATTN, FLASH_ATTN), True)


# The compute capability of the NVIDIA GPU, once read (the hardware does not change while
# VAF runs).
_capability: Optional[tuple] = None


def compute_capability() -> Optional[tuple]:
    """(major, minor) of the first NVIDIA GPU from nvidia-smi, or None."""
    global _capability
    if _capability is not None:
        return _capability
    try:
        from vaf.core.gpu_detection import detect_nvidia_gpu
        from vaf.core.local_engine import _parse_capability
        gpu = detect_nvidia_gpu()
        found = _parse_capability(getattr(gpu, "compute_capability", "")) if gpu else None
    except Exception:
        return None
    if found:
        _capability = found
    return found


def _plan_in(server_env: Dict[str, str]) -> KvPlan:
    if backend() != CUDA:
        return KvPlan(("fp8", "auto"), ("", ""), False)
    return attention_plan(compute_capability(), flashinfer_buildable(server_env))


def launch_plan(base: Optional[Dict[str, str]] = None) -> KvPlan:
    """This machine's attempts, for the server's environment (``child_env`` of ``base``). The
    ROCm build keeps fp8 then ``auto`` with vLLM's own backend choice: the plan reads NVIDIA
    compute capabilities and FlashInfer's CUDA JIT."""
    return _plan_in(child_env(base, plan=False))


# ── start / stop ─────────────────────────────────────────────────────────────────


def build_command(model_dir: str, repo_id: str, port: int, result: FitResult,
                  tool_parser: str, reasoning_parser: str) -> List[str]:
    """The launch argument list (no shell)."""
    argv = [str(vllm_exe()), "serve", str(model_dir),
            "--host", "127.0.0.1", "--port", str(int(port)),
            "--served-model-name", repo_id,
            "--max-model-len", str(result.max_model_len),
            "--gpu-memory-utilization", f"{result.utilization:.2f}",
            "--kv-cache-dtype", result.kv_dtype]
    if result.attention_backend:
        argv += ["--attention-backend", result.attention_backend]
    argv += ["--enable-auto-tool-choice", "--tool-call-parser", tool_parser]
    if reasoning_parser:
        argv += ["--reasoning-parser", reasoning_parser]
    return argv


# Switches that would open the server beyond chat completions; pinned off even though no
# VLLM_ variable is passed on (belt and braces against a later change to that rule).
_PINNED_OFF = ("VLLM_SERVER_DEV_MODE", "VLLM_ALLOW_RUNTIME_LORA_UPDATING")


def with_rocm_library(env: Dict[str, str]) -> Dict[str, str]:
    """Put the found ROCm library directory first on ``LD_LIBRARY_PATH`` of ``env`` on the
    ROCm build (``gpu_detection.rocm_library_dir``: a ROCm under ``$ROCM_PATH`` or another
    ``/opt/rocm-<version>`` is not on the wheels' RUNPATH). ``env`` is changed in place and
    returned. Unchanged on CUDA and when the linker finds the runtime by itself."""
    if backend() != ROCM:
        return env
    from vaf.core.gpu_detection import rocm_library_dir
    lib = rocm_library_dir(env)
    if lib:
        rest = [p for p in str(env.get("LD_LIBRARY_PATH", "") or "").split(os.pathsep) if p and p != lib]
        env["LD_LIBRARY_PATH"] = os.pathsep.join([lib] + rest)
    return env


def child_env(base: Optional[Dict[str, str]] = None, *, plan: bool = True) -> Dict[str, str]:
    """The server's environment: VAF's environment without any VLLM_ variable of its own,
    plus the switches below. A VLLM_ variable is a launch decision like a command-line flag
    (development endpoints, runtime LoRA loading, plugins that are imported at start), and
    there is no free-form way to pass those on either. VAF's own GPU hide for in-process
    torch is taken out (``gpu_detection.gpu_child_env``): the server needs the GPU. Where
    FlashInfer cannot build its kernels (``launch_plan``), its sampler is turned off: it
    JIT-compiles on the first sampled request and kills the server without nvcc."""
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
    env = with_rocm_library(env)
    if plan and _plan_in(env).sampler_off:
        env["VLLM_USE_FLASHINFER_SAMPLER"] = "0"
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
    if pid == _loading_pid:
        with _lock:
            _stopped_while_loading.add(pid)
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


# ── the start memory ─────────────────────────────────────────────────────────────

# The log a start failure points at, as the UI names it (log_path() is the file).
SERVER_LOG = "logs/vllm_server.log"
# A start refused by the fit is measured again after this long: the memory other programs
# hold changes when they close.
FIT_RETRY_SECONDS = 60.0
# The causes a failed start is classified into (the catalogues' ``startCause``).
START_CAUSES = ("kernels", "memory", "timeout", "fit", "interrupted", "other")

_CAUSE_TEXT = {
    "kernels": "it needs GPU kernels that cannot be built on this machine",
    "memory": "the GPU does not have enough free memory for the model and its context",
    "timeout": "it was not ready in time",
    "fit": "the model does not fit in the free GPU memory",
    "interrupted": "its start was interrupted by another start of the local model",
    "other": "it stopped during its start",
}
_MEMORY_LOG = re.compile(r"out of memory|no available memory for the cache blocks|"
                         r"kv cache is needed|estimated maximum model length|"
                         r"less than desired gpu memory utilization", re.IGNORECASE)
_KERNELS_LOG = re.compile(r"could not find nvcc|fp8e4nv|not supported by the \w+ attention "
                          r"backend", re.IGNORECASE)


def classify_start_log(text: str) -> str:
    """``memory``, ``kernels`` or ``other`` for the log of a start that died. Memory first:
    FlashInfer's "kernels are disabled" warning stands at the top of every start without a
    CUDA toolkit, also of one that died for want of memory."""
    if _MEMORY_LOG.search(text or ""):
        return "memory"
    if _KERNELS_LOG.search(text or ""):
        return "kernels"
    return "other"


def cause_text(cause: str) -> str:
    return _CAUSE_TEXT.get(cause, _CAUSE_TEXT["other"])


@dataclass(frozen=True)
class StartFailure:
    """A start that failed for this signature: ``cause`` (``START_CAUSES``), the English
    ``text``, the log, when (``time.monotonic``) and, for a fit refusal, from when it is
    measured again (0 = it stays until cleared)."""
    signature: tuple
    cause: str
    text: str
    log: str
    at: float
    retry_at: float = 0.0


# Process memory: a restart of VAF is a fresh try, and `vaf engine vllm`, Settings and the
# TUI clear it through local_engine.apply.
_start_failure: Optional[StartFailure] = None
# The attempt of the plan that came up last, for its signature: later starts begin there.
_came_up: Optional[tuple] = None
# The pid of a start this process stopped while it loaded (another start of the local
# model): its death is an interruption, not a failure of the model.
_stopped_while_loading: set = set()
# The pid of the server this process is waiting for (``_wait_ready``).
_loading_pid: Optional[int] = None
_logged_failure: Optional[StartFailure] = None
# The last start of this process ended because another start stopped it.
_last_interrupted = False


def start_signature(repo_id: str, n_ctx: int, plan: Optional[KvPlan] = None,
                    utilization: Optional[float] = None) -> tuple:
    """What a start depends on besides the free memory: the model, the requested context,
    the GPU share (``utilization``, the override of a switch, else the configured one), the
    parsers, the pin, the recipe and the plan. A change of any of them is a new start, not
    the failed one again. Not in it: the display state and its desktop reserve. A fit refusal
    is measured again after ``FIT_RETRY_SECONDS`` anyway; a remembered crash or timeout
    survives a monitor plugged in or out until a switch or a restart of VAF."""
    from vaf.core import local_engine
    plan = plan or launch_plan()
    return (str(repo_id), max(int(n_ctx or 0), MIN_CONTEXT), _share_or_configured(utilization),
            tuple(local_engine.parsers_for(repo_id)), PIN, recipe_current(),
            tuple(plan.kv_dtypes), tuple(plan.backends))


def start_failure() -> Optional[StartFailure]:
    """The failure remembered from this process's last start, if any (whatever its
    signature)."""
    with _lock:
        return _start_failure


def clear_start_failure() -> None:
    global _start_failure, _logged_failure
    with _lock:
        _start_failure = None
        _logged_failure = None


def _remember(signature: tuple, cause: str, text: str, retry: bool = False) -> None:
    global _start_failure
    now = time.monotonic()
    with _lock:
        _start_failure = StartFailure(signature, cause, text, SERVER_LOG, now,
                                      now + FIT_RETRY_SECONDS if retry else 0.0)


def remembered_failure(repo_id: Optional[str] = None,
                       n_ctx: Optional[int] = None,
                       utilization: Optional[float] = None) -> Optional[StartFailure]:
    """The remembered failure when the next start would be the failed one again: the same
    signature, and for a fit refusal not yet ``FIT_RETRY_SECONDS`` old. ``repo_id`` and
    ``n_ctx`` default to the configuration's. A failure of another signature is kept (a
    reader asking with other defaults must not wipe it) but does not block: that start
    launches, and its outcome replaces the memory."""
    failure = start_failure()
    if failure is None:
        return None
    from vaf.core import local_engine
    if repo_id is None:
        repo_id = local_engine.resolve_vllm_model()
    if n_ctx is None:
        n_ctx = local_engine._n_ctx()
    if start_signature(repo_id, n_ctx, utilization=utilization) != failure.signature:
        return None
    if failure.retry_at and time.monotonic() >= failure.retry_at:
        return None
    return failure


def failure_sentence(failure: StartFailure) -> str:
    """The sentence a chat gets while a start failure is remembered."""
    return (f"vLLM could not start: {failure.text}. Details in {failure.log}. Switch to "
            "llama.cpp in Settings > AI & Model or with `vaf engine llama`, or switch to vLLM "
            "again to retry.")


def start(repo_id: str, n_ctx: int, port: int = 8080,
          popen: Optional[Callable[..., subprocess.Popen]] = None,
          utilization: Optional[float] = None) -> bool:
    """Launch vLLM for ``repo_id`` and wait until ``/health`` answers 200. False, with the
    reason said, when it is refused (profile, not installed, model missing, unknown parser,
    does not fit) or does not come up. The attempts are this machine's ``launch_plan``: the
    quantized KV cache first, the model's own dtype once when that launch dies. The caller
    holds the start lock and has handed the port over (backend.ServerManager).

    A start that died or timed out, or that the fit refused, is remembered for its signature
    (``start_signature``): the same start again returns False at once without a launch (a fit
    refusal is measured again after ``FIT_RETRY_SECONDS``), until ``clear_start_failure``
    (every switch, ``local_engine.apply``), a changed signature or a restart of VAF.
    ``utilization`` overrides the configured GPU share (the first start of a switch, which
    writes the share only once it passed); None reads ``vllm_gpu_memory_utilization``."""
    global _child, _came_up, _logged_failure, _last_interrupted, _loading_pid
    from vaf.core import inference_profile, local_engine
    UI = _ui()
    popen = popen or subprocess.Popen
    _last_interrupted = False
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
    plan = launch_plan()
    signature = start_signature(repo_id, n_ctx, plan, utilization)
    failure = remembered_failure(repo_id, n_ctx, utilization)
    if failure is not None:
        if _logged_failure is not failure:
            _logged_failure = failure
            UI.error(f"vLLM is not started again: {failure.text}. Details in {failure.log}; "
                     "switching to vLLM again (`vaf engine vllm`, Settings) retries.")
        return False
    first = _came_up[1] if _came_up and _came_up[0] == signature else 0
    attempts = list(zip(plan.kv_dtypes, plan.backends))[first:]
    # The log of an attempt that died, when the next one is tried: a fallback the fit refuses
    # must not turn that crash into a fit refusal measured again in 60 s.
    crashed_tail: Optional[str] = None
    for attempt, (dtype, _backend_name) in enumerate(attempts, start=1):
        result = plan_fit(model_dir, n_ctx, kv_dtype=dtype, model=repo_id,
                          utilization=utilization)
        if result.note:
            UI.event("System", result.note, style="warning")
        if not result.ok:
            UI.error("vLLM is not started: " + result.reason)
            if crashed_tail is not None:
                # The quantized attempt died and its fallback does not fit: the crash is what
                # stays remembered (no retry time), or the dying attempt runs again in 60 s.
                cause = classify_start_log(crashed_tail)
                _remember(signature, cause, cause_text(cause))
            else:
                _remember(signature, "fit", result.reason.rstrip("."), retry=True)
            return False
        argv = build_command(model_dir, repo_id, port, result, tool, reasoning)
        if not inference_profile.local_inference_allowed():
            UI.event("Backend", inference_profile.refuse("the local server"), style="dim")
            return False
        shown = f", attention {result.attention_backend}" if result.attention_backend else ""
        UI.event("Server", f"Starting vLLM on :{port} ({repo_id}, context {result.max_model_len}, "
                           f"KV {dtype}{shown}, GPU share {result.utilization:.2f})...",
                 style="dim")
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
        _loading_pid = proc.pid
        try:
            outcome = _wait_ready(proc, port)
        except BaseException:
            # Ctrl+C in a terminal (or any exit through here): the server leads its own
            # session and would keep the GPU after VAF is gone.
            _loading_pid = None
            if log_handle is not None:
                try:
                    log_handle.close()
                except Exception:
                    pass
            stop()
            raise
        finally:
            _loading_pid = None
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:
                pass
        if outcome == "ready":
            _came_up = (signature, first + attempt - 1)
            clear_start_failure()
            return True
        if outcome in ("switched", "timeout"):
            stop()
            if outcome == "switched":
                UI.event("Backend", inference_profile.refuse("the local server"), style="dim")
            else:
                UI.error(f"vLLM was not ready after {int(_ready_budget())}s and is stopped. For a "
                         "slow first start raise server_ready_timeout in the config.")
                _remember(signature, "timeout",
                          f"{cause_text('timeout')} ({int(_ready_budget())} s)")
            return False
        # The API server died; an EngineCore child it left in its group would keep the GPU.
        try:
            from vaf.core.platform import Platform
            Platform.terminate_process_group(proc.pid)
        except Exception:
            pass
        _remove_pid(only=proc.pid)
        with _lock:
            interrupted = proc.pid in _stopped_while_loading
            _stopped_while_loading.discard(proc.pid)
        if interrupted:
            # Not the model's failure: a start of the other engine took the port and the GPU.
            _last_interrupted = True
            UI.event("Server", "The vLLM start was interrupted by another start of the local "
                               "model.", style="yellow")
            return False
        tail = _read_log_tail(log_file_path, chars=20000)
        if _BIND_FAILURE.search(tail):
            # The port, not the KV cache: a retry would die the same way.
            UI.error(f"vLLM could not bind 127.0.0.1:{port}: another program holds the port. "
                     "vLLM is not started.")
            return False
        if attempt < len(attempts):
            UI.event("Server", f"vLLM died with the {dtype} KV cache - retrying with the model's "
                               "own KV dtype...", style="yellow")
            crashed_tail = tail
            continue
        cause = classify_start_log(tail)
        UI.error(f"vLLM failed to start ({cause_text(cause)}). Check {log_file_path}\n"
                 f"{tail[-500:]}")
        _remember(signature, cause, cause_text(cause))
        return False
    return False


# The test request of the first start: short, and with top_p/top_k so the sampler path that
# killed a server on its first chat request (FlashInfer's sampler JIT) runs before a switch.
TEST_REQUEST = {"messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 8,
                "temperature": 0.7, "top_p": 0.8, "top_k": 20}
TEST_REQUEST_TIMEOUT = 180.0


@dataclass(frozen=True)
class FirstStart:
    """The outcome of ``first_start``: ``ok``, or the ``cause`` (``START_CAUSES``) and the
    English ``text``, and the log of the start."""
    ok: bool
    cause: str = ""
    text: str = ""
    log: str = SERVER_LOG


def _test_completion(repo_id: str, port: int, post: Optional[Callable[..., object]] = None) -> str:
    """"" when the server answers one chat completion with text, else why not."""
    if post is None:
        import requests
        post = requests.post
    try:
        r = post(f"http://127.0.0.1:{int(port)}/v1/chat/completions",
                 json={"model": repo_id, **TEST_REQUEST}, timeout=TEST_REQUEST_TIMEOUT)
        if getattr(r, "status_code", 0) != 200:
            return f"the test request was answered with HTTP {getattr(r, 'status_code', '?')}"
        choices = (r.json() or {}).get("choices") or []
        if not choices:
            return "the test request got no answer"
    except Exception as e:      # noqa: BLE001 - the reason is the outcome
        return f"the test request failed ({type(e).__name__})"
    return ""


def first_start(repo_id: str, n_ctx: int, port: int = 8080,
                popen: Optional[Callable[..., subprocess.Popen]] = None,
                post: Optional[Callable[..., object]] = None,
                utilization: Optional[float] = None) -> FirstStart:
    """The first start of a switch to vLLM (``local_engine.apply``, after the fit check):
    the real start path on the local port, one chat completion with top_p and top_k, then
    the server is stopped again, so the engine changes only for a model that starts AND
    answers here. The tray starts it again for real after the switch (a warm start, the
    compile cache filled: measured 50 s on an RTX 3080). A start that dies, does not come up
    or does not answer is classified from its log (``START_CAUSES``); one interrupted by
    another start of the local model in this process is ``interrupted`` and not
    remembered. ``utilization`` is the GPU share the switch is about to write (None: the
    configured one); a failure is remembered for it, not for the configured share."""
    try:
        return _first_start(repo_id, n_ctx, port, popen, post, utilization)
    except BaseException:
        # Ctrl+C during `vaf engine vllm` (the ready wait, the test request): the server of
        # this switch is stopped before the interrupt goes on; it leads its own session and
        # would hold the GPU after the terminal is gone.
        try:
            stop()
        except Exception:
            pass
        raise


def _first_start(repo_id: str, n_ctx: int, port: int,
                 popen: Optional[Callable[..., subprocess.Popen]],
                 post: Optional[Callable[..., object]],
                 utilization: Optional[float] = None) -> FirstStart:
    if not start(repo_id, n_ctx, port=port, popen=popen, utilization=utilization):
        # start() leaves no server of its own behind on any of its False paths.
        if _last_interrupted:
            return FirstStart(False, "interrupted", cause_text("interrupted"))
        failure = start_failure()
        if failure is not None:
            return FirstStart(False, failure.cause, failure.text, failure.log)
        return FirstStart(False, "other", cause_text("other"))
    why = _test_completion(repo_id, port, post)
    alive = running_pid() is not None or (_child is not None and _child.poll() is None)
    if why or not alive:
        tail = _read_log_tail(log_path(), chars=20000)
        cause = classify_start_log(tail)
        text = cause_text(cause) + (f" ({why})" if why else "")
        signature = start_signature(repo_id, n_ctx, utilization=utilization)
        stop()
        _remember(signature, cause, text)
        return FirstStart(False, cause, text)
    reason = stop()
    if reason:
        return FirstStart(False, "other", reason.rstrip("."))
    return FirstStart(True)


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
