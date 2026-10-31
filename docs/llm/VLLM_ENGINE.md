# The vLLM Engine

VAF runs a local model on one of two engines, chosen per installation in Settings > AI & Model,
in the full-screen TUI's settings or with `vaf engine`:

- **llama.cpp** (`local_engine = llama`, the default): `llama-server` with a GGUF from
  `models/`. Light, starts in seconds, runs on every platform VAF supports, CPU included.
- **vLLM** (`local_engine = vllm`): a VAF-managed vLLM server with a Hugging Face model in
  safetensors form. Heavier to install and slower to start, built for NVIDIA GPUs.

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
  fresh environment made with VAF's own Python (vLLM 0.32.0 accepts 3.11 to 3.14).
- **What:** vLLM 0.32.0 from PyPI only, the build the release names (the table "Release
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
- **Check:** a probe in that environment must pass two things: PyTorch sees a CUDA GPU
  (exit 3 otherwise: `cuda_probe_failed`, the NVIDIA driver for CUDA 13.0 or a hidden GPU is
  the cause), AND `import vllm._C_stable_libtorch` works, the CUDA extension vLLM's platform
  module imports at its first start (exit 4 or any other failure: `vllm_libs_failed`, the
  CUDA libraries of the environment do not load). Either failure leaves the local engine as
  it was.
- **Recipe record:** only a passing probe writes `bin/vllm/venv/vaf_recipe`, holding the
  recipe's format tag (`vllm_server.RECIPE_FORMAT`). An environment without it (built by an
  earlier VAF with the cu129 index, or one whose probe failed) counts as not installed:
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
- **Size:** measured lower bound of the download about 2.8 GB, from PyPI's file sizes (the
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
  `flashinfer/__main__.py`). Without them FlashInfer compiles the kernels it needs at the
  first start, so the first start takes longer; vLLM's own warning about missing kernels
  fires only on compute capability 9.0 and newer. A measured first-start compile that is too
  slow would earn hash-locking `flashinfer-cubin` and `flashinfer-jit-cache` in the lock.
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
  request needs, clamped to the model's `max_position_embeddings` (with a message).
- **KV cache:** `--kv-cache-dtype fp8` first (the counterpart of llama.cpp's quantized KV
  cache). A launch that dies with it is retried once with `auto` (the model's own 16-bit
  dtype), and the process remembers it when `auto` came up, so later starts skip the doomed
  attempt.
- **GPU share:** `--gpu-memory-utilization` = (free - 512 MiB) / total from `nvidia-smi`,
  within 0.50..0.92, unless `vllm_gpu_memory_utilization` sets it.
- **Fit check before the launch:** weights (the safetensors size) + KV cache (layers x KV
  heads x head dim x 2 x bytes x context, from the model's `config.json`) + 1.2 GiB
  overhead, against that share; a share larger than the free memory counts as not fitting
  too (vLLM refuses to start then). A model that does not fit is refused with the numbers
  instead of a crash a minute later. `vaf engine vllm` prints the same check and does not
  switch when it fails; it stops a running llama server first, whose memory vLLM needs, and
  a running managed vLLM serving another model too: measured against its own share of the
  GPU, a model change was refused although it fits (820 of 10240 MiB free with the old
  server up). A vLLM that already serves the wanted model is neither stopped nor measured.

Measured case (RTX 3080, 10240 MiB, 8500 MiB free): `Qwen3-4B-AWQ` at 32768 tokens needs
about 6079 MiB with fp8 KV and fits in the 7988 MiB share; with 16-bit KV it needs about
8383 MiB and does not.

## Privacy and execution

The server's environment is VAF's environment (without VAF's own GPU hide, see Install) plus `VLLM_NO_USAGE_STATS=1` and
`DO_NOT_TRACK=1` (vLLM sends usage statistics by default), `HF_HUB_OFFLINE=1` and
`VLLM_SKIP_MODEL_NAME_VALIDATION=1`. Every other `VLLM_` variable of VAF's environment is left
out (development endpoints, runtime LoRA loading, plugins imported at start are launch
decisions like flags), and `VLLM_SERVER_DEV_MODE` and `VLLM_ALLOW_RUNTIME_LORA_UPDATING` are
pinned to `0`. A stop signals a process only when its command line carries the managed
`vllm` executable followed by `serve`. `--trust-remote-code` is never passed: it would execute
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
| Linux, AMD GPU | later | ROCm 6.3+ on selected Instinct and Radeon cards, prebuilt for Python 3.12 only |
| Linux, Intel GPU | later | "basic model inference and serving", needs Python 3.12 |
| Linux, no GPU | unsupported | the CPU backend is basic, no 4-bit GGUF-style quantization; llama.cpp is the better CPU engine |
| macOS, Apple Silicon | later | the GPU path is vLLM-Metal, "a community-maintained hardware plugin" on MLX; the CPU build is experimental, from source |
| Windows | later | "vLLM does not support Windows natively" (WSL only) |
| Python outside 3.11 to 3.14, Intel Macs, other machines | unsupported | no build |

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
- **Not on the facade, and not per agent:** the engine is the installation's, like
  `inference_profile`: the server start reads it from the installation's config, which
  `vaf engine` writes, because the one local server on the port is the machine's and two
  agents cannot run it on two engines at once. An `Agent(config={"local_engine": ...})`
  override does not switch it.

## In Settings, the wizard and the TUI

**Settings > AI & Model.** With provider "Local (this machine)" the "Local model" section
starts with the Engine choice, two cards: llama.cpp ("GGUF models, runs on almost any
hardware, starts in seconds") and vLLM ("Hugging Face models, fast with many requests, needs a
supported GPU, starts in minutes"), with this machine's verdict under vLLM. A verdict other
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
keys stay writable there, which is where the refusal of an unknown model family points.

**Applies right away, never with Save changes.** A switch installs, downloads, stops a server
and checks the fit, and a save sends the whole page: a page opened before a switch would write
the old engine back. Both save lanes (the WebSocket `save_config` and `PATCH /api/config`) drop
`local_engine` and every `vllm_` key (`local_engine.without_engine_keys`); the engine changes
only through `local_engine.apply`.

**The routes** (admin-only): `GET /api/system/local-engine[?model=owner/name]` is
`local_engine.state()` plus the job; `POST /api/system/local-engine {engine, model?, install?}`
refuses a wrong request with 400 and its code (`validate`), then runs `apply` as a job (202,
GET follows `job`), because pip plus a model download outlasts any proxy; `DELETE
/api/system/local-engine/vllm` is `uninstall_vllm`. The switch claims the container job slot
(`container_jobs.ENGINE_SWITCH`): not beside a repair, a profile switch (Lean halfway would
refuse the install mid-way), a backup or an update. Across processes (`vaf engine` in a
terminal while the web switch runs) `apply` holds `bin/vllm/switch.lock` without waiting and
refuses with `busy`.

**Progress.** The steps go to the machine's maintenance banner (`MAINTENANCE`, kind
`vllm_engine`, done/total = finished steps of 6 with the install, 3 without; no "please wait"
line, llama.cpp keeps answering until the switch is done) and to the section's own step bar;
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
model..." instead of GGUFs, and a GGUF pick writes nothing. `vaf models set` under vLLM warns
that the GGUF applies to llama.cpp only.

**Update and Repair** shows a "Local engine" node: llama.cpp running or idle, vLLM with its
version and model running, loading or idle, amber when vLLM is chosen but not installed, set up
by an earlier VAF so that a start refuses it (`outdated`), or not supported here ("Change it
under AI & Model"), grey under `api`. Never a repair target. `vaf health`, `vaf engine` and the
Settings section name the `outdated` state the same way: such an environment is never shown as
installed and idle, and the install that replaces it says so first.

**Measured limits.** A message sent while the switch runs restarts llama.cpp on its next use,
which can take the memory the fit check measures; the fit then refuses with its numbers and
the engine stays. A chat answer in flight on llama.cpp is cut when the switch stops it for the
fit check (no streamed text is erased).

## Switching

```bash
vaf engine                       # the engine, this machine's verdict, the install, model, parsers
vaf engine vllm                  # hardware check, install (asks), model, fit, switch
vaf engine vllm --model Qwen/Qwen3-8B-AWQ
vaf engine vllm --model auto     # back to the automatic choice by GPU memory
vaf engine llama                 # back to llama.cpp; a running vLLM is stopped
vaf engine vllm --uninstall      # removes bin/vllm/venv (models stay in the HF cache)
```

Exit codes like `vaf profile`: 0 done, 1 not complete, 2 refused. Settings > AI & Model and
the full-screen TUI run the same switch (`local_engine.apply`). The switch writes the
key; the server starts with the next use of the local model. A running desktop tray picks a
switch made in a terminal up within 25 seconds (its config poll watches `local_engine` and
the `vllm_` keys) and restarts the local engine; it restarts only for a key the running
engine reads. Under the `api` inference profile neither engine installs or runs.

## Debugging

- `logs/vllm_server.log`: the server's output of the last start (the start before it in
  `vllm_server.prev.log`); "Application startup complete" marks the end of a start.
- `~/.vaf/vllm.pid`: the pid of the managed server (the leader of its process group).
- `logs/vllm_install.log`: pip's output of an install started from Settings or the TUI, and
  the probe's error (the ImportError of vLLM's CUDA extension) when it fails.
- `bin/vllm/venv/vaf_recipe`: the recipe record; missing means the next install rebuilds the
  environment.
- `bin/vllm/switch.lock`: held while a switch runs; a second switch (another process) is
  refused with `busy` instead of waiting.
- `vaf engine`, `vaf health` (the `engine` row): installed version, model, server running
  or idle, and what contradicts the choice.
