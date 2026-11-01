# The vLLM Engine

VAF runs a local model on one of two engines, chosen per installation in Settings > AI & Model,
in the full-screen TUI's settings or with `vaf engine`:

- **llama.cpp** (`local_engine = llama`, the default): `llama-server` with a GGUF from
  `models/`. Light, starts in seconds, runs on every platform VAF supports, CPU included.
- **vLLM** (`local_engine = vllm`): a VAF-managed vLLM server with a Hugging Face model in
  safetensors form. Heavier to install and slower to start, built for NVIDIA GPUs and,
  through vLLM's ROCm build, for selected AMD GPUs on Linux.

The code: `vaf/core/local_engine.py` (the choice, the platform verdicts, the parsers, the
two wire-format differences, and the switch itself: `validate`, `apply`, `uninstall_vllm`,
`state`, `status_row`; its module docstring holds the named boundaries),
`vaf/core/vllm_server.py` (install, model, fit, start, stop), the engine branch in
`ServerManager.start_server` / `stop_server` and `Agent.load_model`, and the three callers of
the switch: `vaf/cli/cmd/engine.py`, the routes `/api/system/local-engine`
(`vaf/api/system_routes.py`) behind `web/components/settings/LocalEngineSection.tsx`, and the
full-screen TUI (`vaf/cli/tui_app/agent_bridge.py` `apply_engine`, the Engine row in
`screens.py`). The `engine` row of `vaf health` and the Local engine node of Update and Repair
read `local_engine.status_row`.

## Why an engine of `local`, not a provider

Every rule that keeps ONE local inference server on the machine keys on
`provider == "local"`: the single-server start lock, the local worker clamp, the voice
lane's time sharing, the failover's local link, the gates that allow concurrent inference
only for API providers. A provider id of its own would have put vLLM on the API side of
all of them. As an engine of `local` it inherits every one unchanged, and the provider
registries (`PROVIDER_MODELS`, `PROVIDER_SPECS`, the coder's endpoint map, the web provider
list) are untouched.

## The port and the handover

vLLM listens where llama-server does, on `127.0.0.1:8080`, and speaks the same OpenAI
endpoints (`/v1/chat/completions`, `/v1/models`, `/health`), so every consumer of the local
server (the main loop, the shared `complete()`, the voice lane, the coder, the research
agent, the librarian, the browser agent and the failover link) reaches it without a change.
It is launched with `VLLM_SKIP_MODEL_NAME_VALIDATION=1`: those consumers send their own
model name or none, and vLLM would answer an unknown name with a 404.

Two wire-format differences live in `local_engine`, once each:

| | llama.cpp | vLLM | Reader |
|---|---|---|---|
| `/tokenize` | `{"content": text}` -> `{"tokens": [...]}` | `{"prompt": text}` -> `{"count": N, "tokens": [...]}` | `local_token_count` (None = estimate) |
| reasoning field | `reasoning_content` | `reasoning` | `reasoning_text` |

`reasoning_text` reads `reasoning` only for the local server: OpenRouter carries a
`reasoning` field of its own, and `OpenAIProvider` and the coder read it only for provider
`local`. A token count the server cannot give falls back to the estimate instead of a sum
that silently left a message out. The context window the agent reports under vLLM is the
`max_model_len` the server was launched with (from `/v1/models`).

The main loop's payload carries `repetition_penalty` under vLLM (vLLM's name for the knob
llama.cpp calls `repeat_penalty`; vLLM ignores the unknown field). The agent's model identity
(its display name, and the Gemma message handling and tool-call parsing that
`Agent._apply_local_model_identity` turns on) follows the vLLM model under vLLM, not the GGUF
setting, which names a model that is not running.

**One server at a time.** Starting one engine stops the other first and waits until its
process is gone: a llama server (its pid file, its identity verified) is stopped and the
port polled free, and 1.5 s given to CUDA to release the old model's memory, before vLLM is
spawned (`ServerManager.hand_over_from_llama`, which `vaf engine vllm` runs before its fit
check too); a managed vLLM is stopped (its whole process group) before llama-server is
spawned. Two VAF processes cannot both spawn one: the check and the spawn run under a file
lock (`bin/vllm/start.lock`), and a launch that dies because the port is taken is reported
as that, without the KV-cache retry. vLLM binds the port before it sets up the engine and
its GPU memory, so a second server on the same port fails at bind, before it loads weights.
A start that finds the managed vLLM alive but not yet answering `/health` (it loads for
minutes) WAITS for it within `server_ready_timeout` instead of spawning a second one; one
that never comes up within that budget is stopped and replaced.

## Install

Only on request, never on a start: `vaf engine vllm` shows the size and asks (or `--yes`),
Settings asks in a confirmation that names the size, the TUI in its confirm screen. A start
that finds `local_engine = vllm` and nothing installed refuses with a message that names the
command.

- **Where:** `bin/vllm/venv/`, next to the llama-server binary (`bin/` is gitignored). A
  fresh environment made with VAF's own Python (vLLM 0.32.0 accepts 3.11 to 3.14); on an
  AMD GPU with a Python 3.12 (see "The ROCm build" below).
- **Which build:** `vllm_server.backend()` is `rocm` when the primary GPU is an AMD one and
  `cuda` otherwise (NVIDIA first, the order of `gpu_detection.get_primary_gpu`). Each build
  has its own lock, probe and recipe tag; everything else (the model, the fit, the start,
  the stop) is shared.
- **What (NVIDIA):** vLLM 0.32.0 from PyPI only, the build the release names (the table "Release
  Artifacts" of vLLM v0.32.0 lists `PyPI (CUDA 13.0) | pip install vllm`), installed from
  its hash-locked tree: `pip install --require-hashes --no-deps --only-binary=:all: -r
  vaf/core/vllm_requirements.lock` (see "Hash-locked" below). That wheel
  is the CUDA 13.0 build (its METADATA requires `torch==2.13.0` and
  `nvidia-cutlass-dsl[cu13]`, its `_C_stable_libtorch` extension needs `libcudart.so.13`), and
  PyPI's `torch 2.13.0` is the CUDA 13.0 PyTorch, so the tree is consistent. Not the cu129
  extra index that vLLM's installation page at that tag still shows: with it pip prefers
  `torch 2.13.0+cu129` (a local version label from the extra index) over PyPI's CUDA 13.0
  torch, vLLM's extension then cannot load `libcudart.so.13`, and vLLM cannot start at all
  (measured; `vllm download-kernels` already died on that ImportError). vLLM is never
  imported into VAF's process; its version is read from the package metadata.
- **Check (NVIDIA):** a probe in that environment must pass two things: PyTorch sees a CUDA GPU
  (exit 3 otherwise: `cuda_probe_failed`, the NVIDIA driver for CUDA 13.0 or a hidden GPU is
  the cause), AND `import vllm._C_stable_libtorch` works, the CUDA extension vLLM's platform
  module imports at its first start (exit 4 or any other failure: `vllm_libs_failed`, the
  CUDA libraries of the environment do not load). Either failure leaves the local engine as
  it was.
- **Recipe record:** only a passing probe writes `bin/vllm/venv/vaf_recipe`, holding the
  recipe's format tag (`vllm_server.RECIPE_FORMAT`, on the ROCm build
  `vllm_server.RECIPE_FORMAT_ROCM`). An environment of the other build (a machine whose GPU
  changed vendor) is not current either: its tag and its version (`0.32.0` against
  `0.32.0+rocm723`) are not this build's, so the next install rebuilds it. An environment
  without it (built by an earlier VAF with the cu129 index, or one whose probe failed) counts
  as not installed:
  `vaf engine vllm` and Settings offer the install again, and the install removes it and
  builds it fresh; a start refuses it before any launch with "installed by an earlier VAF and
  cannot start" and the command to rebuild it.
- **The GPU for the engines:** the desktop tray and the headless runner hide the GPU from
  PyTorch in their own process (`CUDA_VISIBLE_DEVICES=""`, because in-process PyTorch users
  such as embeddings and speech pre-allocate GPU memory even for a CPU model;
  `gpu_detection.hide_gpu_from_inprocess_torch`). The hide records the variables it set, and
  every GPU engine VAF starts as a child (each install step and the probe, the vLLM server,
  the llama server) gets an environment without them (`gpu_detection.gpu_child_env`). A
  `CUDA_VISIBLE_DEVICES` you set yourself is passed on unchanged. Before this, the probe ran
  under the tray with the hidden GPU and reported "no CUDA GPU" on a machine whose GPU was
  fine.
- **Size (NVIDIA):** measured lower bound of the download about 2.8 GB, from PyPI's file sizes (the
  vllm wheel 327 MB, torch 527 MB, the CUDA 13 NVIDIA wheels about 1990 MB: the cuda-toolkit
  13.0.3 components 1189 MB, cudnn-cu13 366, nccl-cu13 206, cusparselt-cu13 170, nvshmem-cu13
  60), before the rest of the dependency tree. The installed size is recorded with the first
  live install.
- **Hash-locked:** the whole tree, 198 packages for 0.32.0, is pinned in
  `vaf/core/vllm_requirements.lock` with the SHA-256 of every file PyPI publishes for each
  pinned release, generated by pip-compile `--generate-hashes` from
  `vaf/core/vllm_requirements.in`, the toolchain of VAF's own `requirements.lock`. pip
  refuses any file whose hash the lock does not name (`--require-hashes`), any package the
  lock does not pin (`--no-deps`) and any build from source (`--only-binary=:all:`, every
  pinned release has wheels for every supported target). Without the lock the install
  resolved about 200 packages freely at install time, one of them a build uploaded three days
  before. One lock serves Linux x86_64 and aarch64 with Python 3.11 to 3.14:
  `scripts/check_vllm_lock.py` reads the metadata of the wheel pip would pick on each of the
  eight targets and checks every requirement against the lock. A resolution on Python 3.13
  alone was measured untrue for 3.11 and 3.12 (`mistral-common` requires `numpy<2.4` there),
  so the input caps numpy. On some targets the lock installs a package that target does not
  need (`six` and `setuptools` on 3.11, `torch-c-dlpack-ext` on 3.14); nothing is missing.
  What the lock does not do (named boundary): judge the releases themselves. A file that was
  already malicious on PyPI when the lock was generated is locked in with it; the lock stops
  anything newer or different. Bumping `PIN` regenerates it as a release step
  ([RELEASING.md](../setup/RELEASING.md)).
- **No prebuilt kernels:** the install deliberately does not run `vllm download-kernels`. That
  command runs FlashInfer's own `python -m flashinfer download-kernels`, which pip-installs
  `flashinfer-cubin` from `https://flashinfer.ai/whl` and `flashinfer-jit-cache` from
  `https://flashinfer.ai/whl/cu130`, version-pinned but not hashed and not from PyPI: the
  only compiled code that would bypass the lock (read in the source of vLLM 0.32.0,
  `vllm/entrypoints/cli/download_kernels.py`, and FlashInfer 0.7.0.post1,
  `flashinfer/__main__.py`). Without them FlashInfer can only JIT-compile its kernels, which
  needs a CUDA toolkit (`nvcc`) and `ninja`. Measured on an RTX 3080 (compute capability
  8.6) without a toolkit: vLLM 0.32.0 logged "FlashInfer kernels are disabled" and still
  picked FlashInfer as the attention backend for the fp8 KV cache, and its top-p/top-k
  sampler is FlashInfer's by default; both died on "Could not find nvcc". So the launch reads
  whether FlashInfer can build in the server's environment (`vllm_server.flashinfer_buildable`,
  vLLM's own check without importing anything: `flashinfer_cubin` installed, or nvcc through
  `CUDA_HOME`, `CUDA_PATH`, PATH or `/usr/local/cuda` and `ninja` on PATH). Where it can, the
  command is as before. Where it cannot, VAF names an attention backend and KV cache that need
  no build (`vllm_server.attention_plan`, CUDA build only) and starts the server with
  `VLLM_USE_FLASHINFER_SAMPLER=0`:

  | Compute capability | Quantized attempt | Fallback (`auto` KV) |
  |---|---|---|
  | 7.5 | `int8_per_token_head`, `TRITON_ATTN` | `TRITON_ATTN` |
  | 8.0, 8.6, 8.7 | `int8_per_token_head`, `TRITON_ATTN` (measured on 8.6) | `FLASH_ATTN` |
  | 8.9, 10.x, 12.x | `fp8`, `TRITON_ATTN` | `FLASH_ATTN` |
  | 9.x | `fp8`, `FLASH_ATTN` (FA3) | `FLASH_ATTN` |

  Below 8.9 there is no fp8 KV cache without FlashInfer: Triton's fp8 needs 8.9 (measured
  refusal on 8.6: "native FP8 (fp8e4nv) requires SM89+", for `fp8_e5m2` too) and
  FlashAttention takes fp8 only as FA3 on 9.x. `int8_per_token_head` is vLLM's quantized
  KV cache for the Triton backend: one byte per element plus a float32 scale per token and
  head. Measured on the RTX 3080: it starts, answers with top-p/top-k, parses a tool call and
  serves a 23,827-token prompt in 15.4 s; without the sampler switch the server died in its
  warmup. Only the 8.6 row is measured; the others are read from vLLM 0.32.0's sources
  (`platforms/cuda.py`, `triton_attn.py`, `flash_attn.py`), and the switch's first start
  refuses a row that does not start before the engine changes. A measured speed gap of the
  Triton backend would earn hash-locking `flashinfer-cubin` and `flashinfer-jit-cache`.
  On compute capability 9.0 and newer, the locked FlashInfer itself downloads some prebuilt
  kernels at runtime (DeepGEMM, the TensorRT-LLM generated kernels, the CuTe DSL attention
  kernels) from NVIDIA's artifact server when they are not in its cache
  (`flashinfer/jit/cubin_loader.py`, `get_artifact`). That download is hash-pinned by the
  locked wheel: every file is checked against a `checksums.txt` whose SHA-256 is fixed in
  `flashinfer/artifacts.py` (`CheckSumHash`), and a mismatch is not loaded. It stays allowed,
  because refusing it can break those GPUs; `FLASHINFER_NO_DOWNLOAD=1` in VAF's environment
  refuses it (the server inherits it), and `FLASHINFER_CUBIN_CHECKSUM_DISABLED` must never be
  set, since it turns the check off.
- **The ROCm build (AMD GPUs):** vLLM publishes no ROCm wheel on PyPI. The pinned release's
  ROCm build is on vLLM's versioned wheel index,
  `https://wheels.vllm.ai/rocm/0.32.0/rocm723` (`vllm_server.ROCM_INDEX`; the only variant
  there, "ROCm commit 5df63a7...", `rocm700` is 404). Its wheels are cp312, x86_64 and
  `manylinux_2_39` only (not the glibc 2.35 vLLM's documentation states), and vllm's
  METADATA pins the whole ROCm tree to builds of the same index: `torch==2.13.0+git733fca1`,
  `triton==3.8.0+git669b31ac`, `torchvision`, `torchaudio`, `amdsmi`, `flash-attn` and
  `amd-aiter`. So the environment is made with a Python 3.12 (`vllm_server.rocm_python`:
  VAF's own when it runs on 3.12, else `python3.12` on PATH; VAF's own Python range does not
  apply, because vLLM is never imported into VAF's process), and installed with
  `pip install --require-hashes --no-deps --only-binary=:all: --extra-index-url
  https://wheels.vllm.ai/rocm/0.32.0/rocm723 -r vaf/core/vllm_requirements_rocm.lock`. The
  index is an EXTRA one on the command line, never in the lock (pip honours index options
  inside a requirements file; `tests/test_vllm_lock.py` refuses any in either lock); the
  primary index stays pip's own, as for CUDA, and `--require-hashes` makes the second index
  safe, since a file of the same name from another index has another hash. What it does not
  give is independence from PyPI: a release of the same name and version published there
  cannot replace a locked file (its hash differs, pip refuses it), but it can make the
  install fail with a hash error. `amd-aiter` exists on PyPI under another publisher (one
  release, 0.1.7.post2.dev18, not the pinned 0.1.24.post1); the packages pinned with a local
  version label (torch, triton, vllm, torchvision, torchaudio, amdsmi) cannot be published
  there at all.
  The wheels do NOT bundle the ROCm runtime: `torch/lib/libc10_hip.so` and
  `vllm/_rocm_C.abi3.so` NEED `libamdhip64.so.7` (RUNPATH `/opt/rocm/lib` and
  `/opt/rocm-7.2.3/lib`, read with readelf on the wheels), and the build image is
  `rocm/dev-ubuntu-22.04:7.2.3-complete`. The host needs ROCm 7.2 or newer installed; a
  distribution's packages in `/usr/lib64` or `/usr/lib/x86_64-linux-gnu` count, the linker
  finds them without a RUNPATH. A ROCm under `$ROCM_PATH` or another `/opt/rocm-<version>`
  (no `/opt/rocm` link) is on neither, so every install step, the probe and the server get
  its library directory first on `LD_LIBRARY_PATH` (`gpu_detection.rocm_library_dir`,
  `vllm_server.with_rocm_library`; a relative `$ROCM_PATH` is named as an absolute path);
  the CUDA build's environment is untouched. The ROCm lock (219 pinned releases) is
  generated with uv (`uv==0.13.6` in a throwaway venv, the command is in
  `vllm_requirements_rocm.in`):
  pip-compile resolves only for the interpreter it runs on, and this lock is for cp312 x86_64
  glibc 2.39. uv is a release-time tool only, never a runtime dependency. The ROCm index
  publishes no digests, so each of its eight packages carries exactly one hash, the one file
  it serves (pip verifies it at install time); `scripts/check_vllm_lock.py
  vaf/core/vllm_requirements_rocm.lock` checks the lock at its one target, reading those
  packages' metadata from the index by HTTP range reads. The probe checks the same two
  things as on CUDA: PyTorch sees a GPU (HIP answers through `torch.cuda`; exit 3:
  `rocm_probe_failed`, the cause is ROCm missing or too old, the account outside the render
  and video groups, or `HIP_VISIBLE_DEVICES`, `ROCR_VISIBLE_DEVICES` or
  `CUDA_VISIBLE_DEVICES` hiding the GPU), AND `import vllm._C_stable_libtorch, vllm._rocm_C`
  works, the extensions vLLM's ROCm platform module imports at its first start (exit 4:
  `vllm_libs_failed`). Download lower bound about 3.3 GB: the ROCm-index files sum to 2.73 GB
  (torch 1349 MB, amd-aiter 532, triton 425, vllm 301, flash-attn 125), the PyPI part of the
  tree at least 0.62 GB.
- **Uninstall:** `vaf engine vllm --uninstall` or "Remove vLLM" in Settings (refused while vLLM
  is the engine) removes
  only `bin/vllm/venv`; the models stay in the Hugging Face cache and the command says
  where and how large.
- A failed install leaves no half environment behind and the engine unchanged.

## Model

`vllm_model` is a Hugging Face repo id (`owner/name`), free entry. `auto` (the default) is
chosen by the GPU's total memory; the pickers (Settings, the TUI) offer Automatic, the two
models of this table with their weights (`local_engine.CURATED_MODELS`), a stored repo of
its own and a free field, and `vaf engine vllm --model auto` goes back to the automatic
choice:

| Total VRAM | Model | Weights |
|---|---|---|
| below 11.5 GB | `Qwen/Qwen3-4B-AWQ` | 2.67 GB |
| 11.5 GB and more | `Qwen/Qwen3-8B-AWQ` | 6.10 GB |

Both are AWQ 4-bit, Apache-2.0, not gated, text-only; the quantization needs no setting
(vLLM reads it from the checkpoint). The threshold is 11.5 rather than 12, because a 12 GB
card reports 12282 MiB as well as 12288.

The model is downloaded with `huggingface_hub.snapshot_download` into the standard Hugging
Face cache, which vLLM reads itself; it lies outside the checkout, so a multi-GB
safetensors set can never be staged by git. Only `*.json`, `*.safetensors`, `tokenizer*`,
`*.model`, `*.txt` and `*.jinja` are fetched, and code files (`*.py`, `*.pyc`, shared
libraries, scripts) are excluded even where a pattern would match them, so no Python file of
the repo is fetched. A start reads the cache with `local_files_only` and never reaches
Hugging Face (`HF_HUB_OFFLINE=1` for the server). A sharded model counts as downloaded only
when every shard its `model.safetensors.index.json` names is on disk, so an interrupted
download is resumed instead of sized and launched from part of the model. Gated models (a
Hugging Face token) are not supported.

## Parsers

VAF is an agent: without tool calls it cannot work. Every launch carries
`--enable-auto-tool-choice` and a `--tool-call-parser`, derived from the model family by a
small table (`local_engine.parsers_for`), overridable with `vllm_tool_call_parser` and
`vllm_reasoning_parser` (`none` turns reasoning off):

| Family (repo name) | Tool-call parser | Reasoning parser |
|---|---|---|
| `Qwen3-<size>` | `hermes` | `qwen3` |
| `QwQ` | `hermes` | `deepseek_r1` |
| `Qwen2.5-<size>` | `hermes` | none (no thinking mode) |
| `Hermes-2-Pro`, `Hermes-3` | `hermes` | none |
| Mistral, Ministral, Mixtral | `mistral` | none |
| `gpt-oss` | `openai` | none |

A family the table does not know, with no `vllm_tool_call_parser`, is refused before the
launch with a message naming the setting and vLLM's tool-calling page. Coder, vision and
Qwen3.5 variants are not in the table on purpose: their parsers differ and are not verified;
families that need an extra chat template file (Llama 3.x `llama3_json`, DeepSeek) neither.
A parser name must be a plain name (lowercase letters, digits, `_`); it becomes a launch
argument.

## Context, KV cache and memory

- **Context:** `n_ctx` is the engine's `--max-model-len`, with the same 32768 floor VAF's own
  request needs, clamped to the model's `max_position_embeddings` (with a message). When it
  does not fit, the fit takes the largest context that does, in steps of 1024 and never
  below the floor, and says so ("The context is reduced to N tokens so the model fits.");
  only a model that does not fit at 32768 is refused. The context can then differ between
  starts as free memory changes; the agent follows it, because it reads `max_model_len` from
  `/v1/models`.
- **KV cache:** the quantized attempt of the attention plan first (fp8 where FlashInfer can
  build or the GPU has native fp8, `int8_per_token_head` otherwise, see Install), the
  counterpart of llama.cpp's quantized KV cache. A launch that dies with it is retried once
  with `auto` (the model's own 16-bit dtype), and the process remembers which attempt came
  up, so later starts of the same signature begin there.
- **GPU share:** vLLM preallocates its whole share at launch (the weights, then KV blocks up
  to the share, whatever `--max-model-len` is). `--gpu-memory-utilization` = (free - 512 MiB)
  / total from `nvidia-smi`, within 0.50..0.92, then at most (total - 3328 MiB) / total when
  the GPU drives a display or that cannot be read (the desktop reserve, see below), floored
  to 0.01 (`vllm_server.auto_share`, `floor_share`), unless `vllm_gpu_memory_utilization`
  sets it. The floor keeps the launch argument equal to the share the fit checked: a
  computed 0.675 is fitted and launched as 0.67. Without it the fit checked 0.675 while the
  two-decimal launch argument rounded it to 0.68, 51 MiB more on 10 GB.
  A share set by hand (the slider, `--gpu-share`, the TUI: 0.30 to 0.95,
  `vllm_server.SHARE_RANGE`) is taken as it is and never capped by the reserve; one that
  leaves less than the reserve on a display GPU is marked `below_reserve`, and `vaf engine
  vllm` warns. Whether the GPU drives a display is read without root:
  `gpu_detection.nvidia_display_active` (nvidia-smi `display_active`: `Enabled`, `Disabled`,
  anything else unknown; `display_mode` answers "deprecated" on a current driver), and on AMD
  `gpu_detection.amd_display_active` (the DRM connectors of the KFD node's render device,
  `/sys/class/drm/renderD<n>/device/drm/card*/card*-*/status` and `enabled`).
  On an AMD GPU free and
  total come from the amdgpu driver (`/sys/class/drm/renderD<n>/device/mem_info_vram_total`
  and `mem_info_vram_used`, `gpu_detection.amd_vram_mb`), never from nvidia-smi. The fp8 KV
  cache first and the one fallback to `auto` are the same on both builds (the attention plan
  is the CUDA build's only); whether fp8 KV starts on an RDNA3 card is not measured yet (the
  switch's first start refuses it before the engine changes).
- **Fit check before the launch:** weights (the safetensors size) + KV cache (layers x KV
  heads x 2 x (head dim x bytes, plus 4 bytes of scale for `int8_per_token_head`) x context,
  from the model's `config.json`) + 1.85 GiB overhead, against that share; a share larger
  than the free memory counts as not fitting too (vLLM refuses to start then). The overhead
  is the measured cold start (RTX 3080, Qwen3-4B-AWQ, share 0.62: the first torch.compile;
  about 1.2 GiB once the compile cache is warm), because the first start of a switch is the
  one that must not fail. The share is computed again at every launch: a desktop's free
  memory moved by about 1 GB within minutes. A model that does not fit is refused with the
  numbers instead of a crash a minute later, and the refusal says what helps: the free
  memory and what other programs hold, to close programs that use the GPU and try again,
  that the local engine stays as it was (llama.cpp can move part of the model to the
  CPU), and a smaller model only when the chosen one is not the smallest default (never a smaller
  context: the floor is VAF's own need, and `n_ctx` is shared with llama.cpp). A model that
  fits only without the desktop reserve gets its own refusal, `too_big_reserve`: the reserve
  and the display, the smallest share that fits (`min_share`, in 0.01 steps), `vaf engine vllm
  --gpu-share <min>` and Settings > AI & Model, the smaller model where it applies, that the
  screen can freeze below the reserve, and that the engine stays as it was. `vaf engine
  vllm` prints the same check and does not switch when it fails; it stops a running llama server first, whose memory vLLM needs, and
  a running managed vLLM serving another model too: measured against its own share of the
  GPU, a model change was refused although it fits (820 of 10240 MiB free with the old
  server up). A vLLM that already serves the wanted model is neither stopped nor measured.

Measured cases (RTX 3080, 10240 MiB): with 8500 MiB free, `Qwen3-4B-AWQ` at 32768 tokens
needs about 6744 MiB with fp8 KV (2304 MiB of it the KV cache) and fits in the 7988 MiB
share; with 16-bit KV it needs about 9048 MiB and does not. With `int8_per_token_head` the KV
cache at 32768 tokens is 2376 MiB. With desktop programs holding 4018 MiB (6222 MiB free) the
4B does not fit at the floor with any KV cache, and the refusal names that memory.

With a display (the desktop reserve), `Qwen3-4B-AWQ` (2542 MiB of weights in the cached
config) with `int8_per_token_head`: on the 10240 MiB card with 8500 MiB free the automatic
share is 0.67 (6860 MiB) and the context 32768 (6813 MiB needed); without a display it stays
0.78 with 40960. On an 8192 MiB card with 7500 MiB free the share is capped at 0.59, the 4B
needs 6813 MiB at the floor and is refused with `too_big_reserve` and a share by hand of at
least 0.84. On a 12288 MiB card the automatic `Qwen3-8B-AWQ` needs 10087 MiB and gets 8847
(0.72): `too_big_reserve`, naming the 4B and the share by hand. The automatic model choice
stays by total memory: a display-aware threshold would change the model with a docked
monitor.

**Desktop reserve.** The free figure at launch covers only what the desktop holds at that
moment, and vLLM keeps its whole share for as long as it runs. Measured on an RTX 3080
(10240 MiB, KDE Plasma on Wayland): desktop programs hold 3.1 to 3.6 GB of GPU memory. A start
at share 0.78 (about 7987 MiB for vLLM, 2253 MiB left) froze the screen during a long coder
turn: the desktop window's GPU context was lost (Skia out of memory), the compositor could
not allocate a buffer (`DRM_IOCTL_MODE_CREATE_DUMB failed: Cannot allocate memory`) and the
shell aborted, while system memory was fine. Starts at 0.66 to 0.68 (3482 to 3277 MiB left)
stayed alive. So an automatic share leaves `vllm_server.DESKTOP_RESERVE_MB` = 3328 MiB free
on a GPU that drives a display, and on one whose display state cannot be read: a reserve too
many costs context, which the slider gives back, one too few freezes the desktop. A GPU known
to drive no display (a server, a second card) keeps the share computed from the free memory.

| share (10240 MiB, 4B, int8 KV) | fits | context | vLLM gets (MiB) | left for the rest (MiB) |
|---|---|---|---|---|
| 0.66 | no | - | 6758 | 3482 |
| 0.67 | yes | 32768 | 6860 | 3380 |
| 0.68 | yes | 34816 | 6963 | 3277 |
| 0.70 | yes | 36864 | 7168 | 3072 |
| 0.78 | yes | 40960 | 7987 | 2253 (froze) |

**GPU memory slider.** `vllm_server.share_table` (in `local_engine.state()["vllm"]["memory"]`)
carries the card, the display and its reserve, the stored share, the automatic share now (none
while a local server holds memory: measured at the next start) and one fit per share from 0.30
to 0.95 in 0.01 steps, without the free-memory refusal, which is the start's. Settings, the
TUI and `vaf engine` only read it; there is no second fit formula. A share reaches
`vllm_gpu_memory_utilization` only through the switch (`local_engine.apply(...,
gpu_share=...)`): vLLM cannot change its share while it runs, so the switch stops a running
vLLM of the same model for a changed share, fits and test-starts the new share
(`plan_fit(..., utilization=)`, `first_start(..., utilization=)`), and writes the key only
after both passed; a failure leaves it as it was. A running desktop tray then restarts vLLM
with it (the key is in `local_engine.ENGINE_KEYS`).

## Privacy and execution

The server's environment is VAF's environment (without VAF's own GPU hide, see Install) plus `VLLM_NO_USAGE_STATS=1` and
`DO_NOT_TRACK=1` (vLLM sends usage statistics by default), `HF_HUB_OFFLINE=1` and
`VLLM_SKIP_MODEL_NAME_VALIDATION=1`. Every other `VLLM_` variable of VAF's environment is left
out (development endpoints, runtime LoRA loading, plugins imported at start are launch
decisions like flags), and `VLLM_SERVER_DEV_MODE` and `VLLM_ALLOW_RUNTIME_LORA_UPDATING` are
pinned to `0`. Where FlashInfer cannot build its kernels, `VLLM_USE_FLASHINFER_SAMPLER=0` is
set too (see "No prebuilt kernels" under Install). A stop signals a process only when its
command line carries the managed `vllm` executable followed by `serve`. `--trust-remote-code` is never passed: it would execute
code from the model repo. There is no free-form extra-arguments setting: a launch argument
is an execution decision. Every `vllm_` key and `local_engine` are admin-only.

## Platforms

`local_engine.support()` gives every machine a verdict and a sentence; `vaf engine` prints
it, and only `supported` installs. The driver version comes from the same nvidia-smi query as
the compute capability; one it cannot read is not refused (the install's probe still catches
a driver that is too old). From vLLM's installation guide
(https://docs.vllm.ai/en/latest/getting_started/installation/, the
[GPU](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/) and
[CPU](https://docs.vllm.ai/en/latest/getting_started/installation/cpu/) pages):

| Machine | Verdict | Why |
|---|---|---|
| Linux x86_64 or aarch64, NVIDIA, compute capability 7.5 or newer, driver 580 or newer, glibc 2.31 or newer (x86_64) or 2.34 or newer (aarch64) | supported | the PyPI wheel is the CUDA 13.0 build and installs into VAF's Python |
| Linux with an older glibc, or without glibc (musl) | unsupported | pip cannot install the locked tree there (`glibc_old`): vllm and torch are `manylinux_2_28` wheels, but `llguidance` 1.7.6 ships `manylinux_2_31` only, and on aarch64 `z3-solver`, `tilelang` and `pynvvideocodec` ship `manylinux_2_34` only; there are no musl wheels. `local_engine.MIN_GLIBC` holds the floors, and `scripts/check_vllm_lock.py` checks the lock at exactly them |
| Linux, NVIDIA below 7.5 (Pascal, Volta) | unsupported | vLLM needs 7.5 or newer |
| Linux, NVIDIA, driver below 580 | unsupported | the CUDA 13.0 build needs driver 580 or newer ("CUDA 13.x >= 580", NVIDIA's CUDA compatibility guide); VAF installs one recipe, not the CUDA 12.9 wheel that exists only as a release asset |
| Linux, NVIDIA whose compute capability nvidia-smi does not report | unsupported | a driver too old to report it is too old for the CUDA 13.0 build |
| Linux x86_64, AMD GPU with gfx90a, gfx942, gfx950 (Instinct MI200, MI300, MI350), gfx1100, gfx1101 (Radeon RX 7900), gfx1200, gfx1201 (RX 9000), gfx1150, gfx1151 (Ryzen AI 300, MAX), ROCm 7.2 or newer, /dev/kfd open to the account, glibc 2.39 or newer, a Python 3.12 | supported | vLLM's ROCm build of the pin installs into an environment of that Python 3.12 (see Install) |
| Linux, AMD GPU on aarch64 | unsupported | the ROCm build is x86_64 only (`no_build_gpu`) |
| Linux, AMD GPU whose architecture KFD does not report | unsupported | `amd_unknown`: the architecture comes from `/sys/class/kfd/kfd/topology/nodes/*/properties` (`gfx_target_version`) |
| Linux, AMD GPU of another architecture (for example gfx1030, RX 6800) | unsupported | `amd_gfx`: the build has no kernels for it (`PYTORCH_ROCM_ARCH` in `docker/Dockerfile.rocm_base` at v0.32.0, `local_engine.ROCM_GFX`) |
| Linux, AMD, no `libamdhip64.so.7` | unsupported | `amd_rocm_missing`: the wheels link the host's ROCm 7 runtime |
| Linux, AMD, ROCm below 7.2 | unsupported | `amd_rocm_old`: the build image is ROCm 7.2.3; a version VAF cannot read (`<rocm>/.info/version`, missing in a distribution install) is not refused, the probe judges it |
| Linux, AMD, /dev/kfd not open to the account | unsupported | `amd_no_access`: ROCm's prerequisite is membership in the render and video groups |
| Linux, AMD, glibc below 2.39, unknown, or musl | unsupported | `amd_glibc_old`: vllm, torch, amd-aiter and flash-attn of the ROCm index are `manylinux_2_39` wheels (Ubuntu 24.04, Fedora 40, RHEL 10 and later); `local_engine.MIN_GLIBC_ROCM` |
| Linux, AMD, no Python 3.12 | unsupported | `amd_python`: the ROCm wheels are cp312 only; Ubuntu 24.04 and Fedora 40 ship it as their default Python; Debian 13 meets the glibc floor but ships no Python 3.12, so the sentence names pyenv for such systems (VAF finds `python3.12` on PATH, `vllm_server.rocm_python`) |
| Linux, Intel GPU | later | "basic model inference and serving", needs Python 3.12 |
| Linux, no GPU | unsupported | the CPU backend is basic, no 4-bit GGUF-style quantization; llama.cpp is the better CPU engine |
| macOS, Apple Silicon | later | the GPU path is vLLM-Metal, "a community-maintained hardware plugin" on MLX; the CPU build is experimental, from source |
| Windows | later | "vLLM does not support Windows natively" (WSL only) |
| Python outside 3.11 to 3.14, Intel Macs, other machines | unsupported | no build |

vLLM's quantization table marks AWQ unsupported on AMD, but the code of 0.32.0 accepts it:
`vllm/platforms/rocm.py` lists `awq`, `auto_awq` and `awq_marlin` in its supported
quantizations and turns on `VLLM_USE_TRITON_AWQ=1` itself for AWQ on ROCm, and the
mixed-precision kernel table has Triton and RDNA3 W4A16 kernels for ROCm. So the AWQ default
models stay on AMD. FP8 weights would need CDNA or RDNA4 (`supports_fp8`), which would
exclude the RX 7900 and Ryzen AI. Named boundary: no AWQ start on AMD hardware has been
measured yet; if the live run fails, the unquantized `Qwen/Qwen3-4B` (about 8 GB of weights)
is the fallback for AMD.

The differences in performance and install effort are large: on a supported NVIDIA card
vLLM batches concurrent requests and runs AWQ/FP8 checkpoints efficiently, but it installs
several GB and starts in minutes; llama.cpp stays the lighter choice for small GPUs, CPUs
and quick starts.

## Voice and vision

- **Voice:** `voice_agent_provider = "local"` rides the running vLLM model; the dedicated
  voice GGUF (`voice_agent_model`) is not loaded (a swap would be a multi-minute vLLM
  restart). A local voice lane with an API main provider is not supported under vLLM in
  this version: nothing would start vLLM for the call, so the lane reports no model
  (`voice_agent._resolve_backend` answers none) and `vaf engine` names the setting.
- **Vision (named boundary):** the projector lane (`vision_provider = local`) is
  llama.cpp's. Under vLLM `/v1/models` carries no image capability, so images degrade to the
  existing "analysis unavailable" path; an API `vision_provider` keeps working.

## Named boundaries

- **A foreign server for the main loop** (an Ollama or vLLM that VAF did not start): the
  address is written out instead of read from `Config.get_llama_server_url` in 42 lines of
  13 files (`git grep -E "(127\.0\.0\.1|localhost):8080" -- 'vaf/*.py'` at the commit
  before this engine, comments included), so every consumer talks to whatever answers on
  the local port. What the start does with a server VAF did not start: it is **reused**
  when it already serves the configured model, **refused** (no launch) when it serves
  another one, also a vLLM, and **never stopped** by VAF. The loopback port is trusted
  as the local server, the same trust the 42 call sites place in it. The llama engine
  trusts it more: its `start_server` reuses a healthy server that holds the wanted model,
  and `Agent.load_model` reuses any healthy server on the port without asking which model
  it holds. Under vLLM `load_model` always goes through the start, so the model check
  above applies. `local_api_url` stays the lane for the API-backend consumers.
- **The prompt cache is shared across accounts:** vLLM's automatic prefix cache, like
  llama.cpp's prompt cache and slots, reuses the KV cache of a prefix whoever sent it. No
  text of another account is ever returned from it, but the time to the first token can
  tell whether a prefix was sent before (a timing side channel). VAF sends no per-account
  `cache_salt`; the local server serves one installation, which is reached over the local
  or VPN network only ([USER_ISOLATION.md](../security/USER_ISOLATION.md), section 3).
- **Concurrency:** vLLM batches concurrent requests, but `max_parallel_local_workers` and
  every `provider != "local"` gate stay until that is measured.
- **Start time and idle unload:** a vLLM start takes minutes, not seconds. The desktop tray
  unloads the local model when nobody uses it, so the first message after idle waits for a
  full start; `server_persistence_enabled` keeps it loaded.
- **AMD detection needs ROCm's tools:** `gpu_detection.detect_amd_gpu` finds an AMD GPU on
  Linux only while `rocm-smi` or `amd-smi` is on PATH; a runtime-only ROCm install without
  either reads as no GPU (`no_gpu`). Widening the gate to the KFD topology alone would make
  every AMD APU laptop without ROCm count as an AMD GPU for the llama.cpp paths too
  (`backend.py`, `agent.py`, `vaf info`), which is a change of its own. The product name
  still comes from rocm-smi; architecture and VRAM come from KFD.
- **No `HSA_OVERRIDE_GFX_VERSION`:** the community trick that makes gfx1030 or gfx1102 cards
  load gfx1100 kernels is not honoured. VAF reads the real architecture and refuses a card
  the build has no kernels for.
- **One AMD GPU:** the fit reads the KFD node with the most VRAM (a discrete GPU before an
  APU's carve-out); whether vLLM uses that same device on a machine with an APU and a
  discrete GPU (HIP's device order) is not measured. On a Ryzen AI MAX with shared memory,
  which figure KFD reports (the BIOS carve-out or more) is not measured either.
- **The attention plan is measured on compute capability 8.6 only** (an RTX 3080): the rows
  for 7.5, 8.0, 8.9, 9.x, 10.x and 12.x are read from vLLM 0.32.0's sources, and the
  switch's first start turns a wrong row into a refused switch, not a broken chat. Every
  `PIN` bump re-reads the Triton backend's fp8 check and the attention selector
  ([RELEASING.md](../setup/RELEASING.md)). `int8_per_token_head` is checked for coherent
  answers and tool calls, not benchmarked against fp8; Triton's prefill is slower than
  FlashAttention's, and machines where FlashInfer can build keep the earlier command.
- **The desktop reserve is a constant, not a key:** `DESKTOP_RESERVE_MB` is measured on one
  desktop (KDE Plasma on Wayland, an RTX 3080); the slider is the override, and a second key
  would be a second way to say the same. Not measured on another desktop or driver.
- **The display reading on AMD is not measured on an AMD card:** the DRM connector layout is
  the same on nvidia-drm, where it was read; an AMD GPU whose connectors cannot be read keeps
  the reserve (unknown).
- **The first NVIDIA GPU:** the display state, like the free memory, is nvidia-smi's first
  GPU; on a machine with several, CUDA's device order can differ.
- **llama.cpp keeps its own headroom:** its VRAM-adaptive context cap
  (`vaf/core/backend.py`) sizes the context to free - weights - 400 MiB and takes no share of
  the card, so it has no slider and no desktop reserve; no freeze under llama.cpp has been
  measured. A measured one would earn the reserve there too.
- **The wizard has no slider:** it runs before an account exists, and the automatic share
  with the reserve is the safe default; the slider is in Settings.
- **Not on the facade, and not per agent:** the engine is the installation's, like
  `inference_profile`: the server start reads it from the installation's config, which
  `vaf engine` writes, because the one local server on the port is the machine's and two
  agents cannot run it on two engines at once. An `Agent(config={"local_engine": ...})`
  override does not switch it.

## In Settings, the wizard and the TUI

**Settings > AI & Model.** With provider "Local (this machine)" the "Local model" section
starts with the Engine choice, two cards built the same way: whom the engine is for, then
what it runs on, how it takes memory and how long it starts, then a note about this machine.
llama.cpp: "For everyday tasks on personal PCs, laptops and edge devices", then "Runs GGUF
models on almost any GPU or on the CPU alone, takes only the memory it needs and starts in
seconds", and "llama.cpp runs on this machine." vLLM: "For servers and high-end PCs that
answer many requests at once", then "Runs Hugging Face models on a supported NVIDIA or AMD
GPU, reserves its share of GPU memory up front and starts in one to two minutes" (the
measured 50 s warm and 90 s cold, see Switching), and this machine's verdict. In every
language the two cards carry about the same text (a test keeps the ratio within 0.8 to 1.25).
The wizard shows the same cards, and the TUI's engine rows the same English
lines (`screens.ENGINE_LINES`, kept equal to en.json by a test). Under the vLLM model picker
is the GPU memory slider (see "GPU memory slider" above; [WEB_UI.md](../web-ui/WEB_UI.md)
describes the screen); the wizard has none and starts on the automatic share. A verdict other
than `supported` disables the vLLM card and keeps its reason visible. Below the choice is the
model picker of the engine: the GGUF picker and the GGUF download box under llama.cpp, the
vLLM model picker (with the parsers of the chosen repo, or the refusal of an unknown family,
before anything installs) under vLLM. Under the `api` profile both cards are disabled with the
profile's sentence and no picker shows. The other local pickers (the Thinker, the sub-agents'
Tool / Workflow model, the voice call and the failover's local link) show one read-only line
"Runs on the vLLM model (...)" under vLLM instead of a GGUF list, since every local lane rides
the one vLLM model.

**Never by another write.** `vaf models config` refuses `local_engine` and `vllm_model`
(`local_engine.SWITCH_KEYS`) with the `vaf engine` command to use; the parser and GPU-share
keys stay writable there, which is where the refusal of an unknown model family points. The
GPU share's own lane is the switch (the slider, `--gpu-share`, the TUI), which fits and
test-starts a new share before it writes it; a value written with `vaf models config` is
checked by the next start's fit.

**Applies right away, never with Save changes.** A switch installs, downloads, stops a server
and checks the fit, and a save sends the whole page: a page opened before a switch would write
the old engine back. Both save lanes (the WebSocket `save_config` and `PATCH /api/config`) drop
`local_engine` and every `vllm_` key (`local_engine.without_engine_keys`); the engine changes
only through `local_engine.apply`.

**The routes** (admin-only): `GET /api/system/local-engine[?model=owner/name]` is
`local_engine.state()` plus the job; `POST /api/system/local-engine {engine, model?, install?,
gpu_share?}` (`gpu_share` 0 = automatic, 0.30 to 0.95 by hand, vLLM only; any other value is
`bad_share`) refuses a wrong request with 400 and its code (`validate`), then runs `apply` as a job (202,
GET follows `job`), because pip plus a model download outlasts any proxy; `DELETE
/api/system/local-engine/vllm` is `uninstall_vllm`. The switch claims the container job slot
(`container_jobs.ENGINE_SWITCH`): not beside a repair, a profile switch (Lean halfway would
refuse the install mid-way), a backup or an update. Across processes (`vaf engine` in a
terminal while the web switch runs) `apply` holds `bin/vllm/switch.lock` without waiting and
refuses with `busy`.

**Progress.** The steps go to the machine's maintenance banner (`MAINTENANCE`, kind
`vllm_engine`, done/total = finished steps of 7 with the install, 4 without, the last being
the first start; no "please wait" line, llama.cpp keeps answering until the switch is done)
and to the section's own step bar;
the model download to the download banner (`web_interface.model_download_broadcast`, its
closing frame without a model list, so the GGUF list stays). pip has no progress figure: the
install step shows its name for minutes. pip's output goes to `logs/vllm_install.log` in the
web server and the TUI (`install(output=...)`); the terminal keeps printing it. The banner's
Cancel, for an admin, ends every model download running at that moment
(`MODEL_DOWNLOAD.request_cancel`): the vLLM model of a switch, which then ends with
`download_cancelled` while the engine stays, and a GGUF download running beside it, since a
GGUF and a vLLM model can download at once (each on its own file lock) and share the one
download slot and its one cancel flag. Each download reads its own exception
(`model_download_state.was_cancelled`), so one that failed for another reason is not taken
for a cancel and the GGUF download keeps its fallback to the default model then. The pip step
has no cancel, as in the terminal.

Named boundary, one Cancel for every download running: there is no cancel token per download.
The banner shows one slot and one Cancel, and an overlap needs a GGUF download a chat started
while an admin's engine switch downloads the vLLM model; the `start` of one download also
clears a request still pending for the other. A measured overlap in practice is what would
earn per-download tokens.

**Every outcome has a code** (the verdicts, the fit, the conflicts, each result of `apply` and
`uninstall_vllm`, the phases, the status row): the UI shows the catalogue text in the reader's
language; the English sentence stays for `vaf engine`, `vaf health` and the logs. After a
switch the live agents of the process recompute their model identity
(`agent.refresh_local_identity_all`), so the tool-call parser follows the new engine's model.

**The first-run wizard.** On the Veyllo step, above "Skip, I'll run locally", for the `local`
profile only: the same Engine choice, llama.cpp preselected, vLLM enabled only for a
`supported` verdict. `needs-setup` sends the verdict's level and code only (no GPU name: it
needs no session), from `local_engine.support_class`: the route needs no session and the
wizard polls it, and the detection starts nvidia-smi and lspci, so a conclusive verdict is
kept for the process and the probes run at most once a minute otherwise. An inconclusive
detection (it raised, or "no GPU" while nvidia-smi is installed, which is how a probe timeout
or a driver still loading reports) is not kept, so it cannot leave the wizard saying vLLM is
unavailable until a restart; meanwhile the field is left out, never the setup answer.
Choosing vLLM says that VAF installs it and its model after the setup; once
the account exists, the wizard starts the switch with the install, and its progress shows in
the main page's banners while llama.cpp answers.

**The full-screen TUI.** An "Engine: llama.cpp" or "Engine: vLLM" row above "Select Active
Model" opens both engines with their lines and the verdict (vLLM a note with its reason where
it cannot run, both notes under `api`). An install asks first with its size; under vLLM the
model submenu lists Automatic, the curated repos, a stored repo and "Other Hugging Face
model..." instead of GGUFs, and a GGUF pick writes nothing. Under vLLM the engine menu has a
"GPU memory: Automatic" (or "GPU memory: 68%") row: Automatic with the share now, 30% to 95%
in steps of 5 and a stored value off that grid, each with its size and "fits, context N" or
"too small" from the share table, under the note that vLLM restarts to apply it; a pick is the
switch with that share. `vaf models set` under vLLM warns
that the GGUF applies to llama.cpp only.

**Update and Repair** shows a "Local engine" node: llama.cpp running or idle, vLLM with its
version and model running, loading or idle, amber when vLLM is chosen but not installed, set up
by an earlier VAF so that a start refuses it (`outdated`), or not supported here ("Change it
under AI & Model"), grey under `api`. Never a repair target. `vaf health`, `vaf engine` and the
Settings section name the `outdated` state the same way: such an environment is never shown as
installed and idle, and the install that replaces it says so first.

A switch that stopped shows its reason under the banner's title (`localEngine.banner.<code>`,
one line per `local_engine.FAILURE_CODES`) and, for an admin, a Details button that opens
Settings on AI & Model, where the whole result is. A page that connects after the end gets no
banner (`web_interface.maintenance_snapshot_frame` answers "no job" for a finished one). The
full-screen TUI announces the end as "vLLM setup finished" or "vLLM setup stopped: <code>",
not as a database migration.

**Measured limits.** A message sent while the switch runs restarts llama.cpp on its next use,
which can take the memory the fit check measures; the fit then refuses with its numbers and
the engine stays. A chat answer in flight on llama.cpp is cut when the switch stops it for the
fit check (no streamed text is erased). The same message during the first start stops the
vLLM that is loading (the llama start hands the port back): the switch then ends with
`start_failed`, cause `interrupted`, and nothing is remembered, so switching again is the
retry. A stop from another VAF process (a terminal) is not told apart and reads as cause
`other`.

## Switching

```bash
vaf engine                       # the engine, this machine's verdict, the install, model, parsers
vaf engine vllm                  # hardware check, install (asks), model, fit, switch
vaf engine vllm --model Qwen/Qwen3-8B-AWQ
vaf engine vllm --model auto     # back to the automatic choice by GPU memory
vaf engine vllm --gpu-share 0.70 # vLLM's share of the GPU memory (0.30 to 0.95); restarts it
vaf engine vllm --gpu-share auto # back to the automatic share (with the desktop reserve)
vaf engine llama                 # back to llama.cpp; a running vLLM is stopped
vaf engine vllm --uninstall      # removes bin/vllm/venv (models stay in the HF cache)
```

Exit codes like `vaf profile`: 0 done, 1 not complete, 2 refused. Settings > AI & Model and
the full-screen TUI run the same switch (`local_engine.apply`). Before it writes the key, the
switch starts vLLM once on the local port, sends it one chat completion with `top_p` and
`top_k` (the sampler path that killed a server on its first chat) and stops it again
(`vllm_server.first_start`, the step "first start", one to two minutes; skipped when vLLM
already serves the wanted model). A start that dies, does not come up or does not answer ends
the switch with `start_failed` and the engine unchanged, before any chat could reach it. The
server then starts with the next use of the local model (a warm start, the compile cache
filled: measured 50 s on an RTX 3080, against 90 s cold). A running desktop tray picks a
switch made in a terminal up within 25 seconds (its config poll watches `local_engine` and
the `vllm_` keys) and restarts the local engine; it restarts only for a key the running
engine reads. Under the `api` inference profile neither engine installs or runs.

## Debugging

- `logs/vllm_server.log`: the server's output of the last start (the start before it in
  `vllm_server.prev.log`); "Application startup complete" marks the end of a start.
- `~/.vaf/vllm.pid`: the pid of the managed server (the leader of its process group).
- `logs/vllm_install.log`: pip's output of an install started from Settings or the TUI, and
  the probe's error (the ImportError of vLLM's CUDA or ROCm extension) when it fails.
- `bin/vllm/venv/vaf_recipe`: the recipe record (`vllmenv-1-18dfb7` for the CUDA build,
  `vllmrocm-1-a69536` for the ROCm build); missing, or the other build's, means the next
  install rebuilds the environment.
- `bin/vllm/switch.lock`: held while a switch runs; a second switch (another process) is
  refused with `busy` instead of waiting.
- `vaf engine`, `vaf health` (the `engine` row): installed version, model, server running
  or idle, a start that failed (`vllm_failed`, with its cause), and what contradicts the
  choice.
- `start_failed` (a switch's result): the first start did not pass; its cause (`kernels`,
  `memory`, `timeout`, `fit`, `interrupted`, `other`) is read from `logs/vllm_server.log`.

## When a start fails

A start that died, timed out or was refused by the fit is remembered in the process
(`vllm_server.remembered_failure`), for its signature: the model, the requested context, the
GPU share (the share a switch is testing, else the configured one, so a switch that failed
with a new share does not block the configured start), the parsers, the pin, the recipe and
the attention plan. The same start
again returns at once without a launch, and `ServerManager._start_vllm_locked` does not stop
the llama server for it. Before this, the desktop tray's activity loop and the headless
runner's lazy load started a failing vLLM again on every tick or turn (measured: four starts
in three minutes, each about 40 s of load and compile).

- **What clears it:** every switch (`local_engine.apply`, also choosing vLLM again while it is
  the engine: Settings' **Try vLLM again**, `vaf engine vllm`), a change of the signature (a
  config change), and a restart of VAF. A fit refusal is measured again after 60 seconds, since
  the memory other programs hold changes when they close. A port held by another program and
  a start interrupted by another start are not remembered.
- **The chat:** a local turn under vLLM gets "vLLM could not start: <cause>. Details in
  logs/vllm_server.log. Switch to llama.cpp in Settings > AI & Model or with `vaf engine
  llama`, or switch to vLLM again to retry." through the runner's turn refusal
  (`headless_runner.turn_refusal_reason`), shown and stored with the message; a messenger
  turn is only logged.
- **The tray** starts nothing while the failure is remembered and logs it once.
- **The status:** `local_engine.status_row` gives `degraded` with code `vllm_failed` and the
  cause (`vaf health`, Update and Repair), `state()["vllm"]["start_failure"]` carries it to
  Settings, and `vaf engine` prints it.
- **Named boundary:** the memory lives in the process, not in a file (a file would be a
  persisted format of its own); a `vaf chat` in another terminal tries once more by design.
