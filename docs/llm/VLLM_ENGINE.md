# The vLLM Engine

VAF runs a local model on one of two engines, chosen per installation with `vaf engine`:

- **llama.cpp** (`local_engine = llama`, the default): `llama-server` with a GGUF from
  `models/`. Light, starts in seconds, runs on every platform VAF supports, CPU included.
- **vLLM** (`local_engine = vllm`): a VAF-managed vLLM server with a Hugging Face model in
  safetensors form. Heavier to install and slower to start, built for NVIDIA GPUs.

The code: `vaf/core/local_engine.py` (the choice, the platform verdicts, the parsers, the
two wire-format differences; its module docstring holds the named boundaries),
`vaf/core/vllm_server.py` (install, model, fit, start, stop), the engine branch in
`ServerManager.start_server` / `stop_server` and `Agent.load_model`, `vaf/cli/cmd/engine.py`
and the `engine` row of `vaf health`.

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

Only on request, never on a start: `vaf engine vllm` shows the size and asks (or `--yes`).
A start that finds `local_engine = vllm` and nothing installed refuses with a message that
names the command.

- **Where:** `bin/vllm/venv/`, next to the llama-server binary (`bin/` is gitignored). A
  fresh environment made with VAF's own Python (vLLM 0.32.0 accepts 3.11 to 3.14).
- **What:** `pip install vllm==0.32.0 --extra-index-url https://download.pytorch.org/whl/cu129`,
  the command of vLLM's installation guide, then `vllm download-kernels` (without the
  prebuilt kernels FlashInfer compiles them at the first start, several minutes). vLLM is
  never imported into VAF's process; its version is read from the package metadata.
- **Check:** `python -c "import torch; ..."` in that environment must see a CUDA GPU; when it
  does not, the NVIDIA driver is the likely cause (the build needs a driver for CUDA 12.9),
  and the local engine stays as it was.
- **Size:** measured lower bound of the download about 1.8 GB (the vllm wheel 327 MB, torch
  527 MB, torch's NVIDIA wheels 932 MB), before the rest of the dependency tree. The
  installed size is recorded with the first live install.
- **Pinned, not hash-locked** (named boundary): pip resolves the tree over TLS; the
  llama.cpp asset is SHA-256 pinned, the vLLM tree is not. Bumping `PIN` is a release step
  ([RELEASING.md](../setup/RELEASING.md)).
- **Uninstall:** `vaf engine vllm --uninstall` (refused while vLLM is the engine) removes
  only `bin/vllm/venv`; the models stay in the Hugging Face cache and the command says
  where and how large.
- A failed install leaves no half environment behind and the engine unchanged.

## Model

`vllm_model` is a Hugging Face repo id (`owner/name`), free entry. `auto` (the default) is
chosen by the GPU's total memory:

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
  switch when it fails; it stops a running llama server first, whose memory vLLM needs.

Measured case (RTX 3080, 10240 MiB, 8500 MiB free): `Qwen3-4B-AWQ` at 32768 tokens needs
about 6079 MiB with fp8 KV and fits in the 7988 MiB share; with 16-bit KV it needs about
8383 MiB and does not.

## Privacy and execution

The server's environment is VAF's environment plus `VLLM_NO_USAGE_STATS=1` and
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
it, and only `supported` installs. From vLLM's installation guide
(https://docs.vllm.ai/en/latest/getting_started/installation/, the
[GPU](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/) and
[CPU](https://docs.vllm.ai/en/latest/getting_started/installation/cpu/) pages):

| Machine | Verdict | Why |
|---|---|---|
| Linux x86_64 or aarch64, NVIDIA, compute capability 7.5 or newer | supported | the prebuilt wheel carries CUDA 12.9 binaries and installs into VAF's Python |
| Linux, NVIDIA below 7.5 (Pascal, Volta) | unsupported | vLLM needs 7.5 or newer |
| Linux, NVIDIA whose compute capability nvidia-smi does not report | unsupported | a driver too old to report it is too old for the CUDA 12.9 build |
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
  another one, also a vLLM, and **never stopped** by VAF. The reuse is parity with the
  llama engine, which reuses any healthy server on the port that holds the wanted model:
  the loopback port is trusted as the local server, the same trust the 42 call sites place
  in it. `local_api_url` stays the lane for the API-backend consumers.
- **The prompt cache is shared across accounts:** vLLM's automatic prefix cache, like
  llama.cpp's prompt cache and slots, reuses the KV cache of a prefix whoever sent it. No
  text of another account is ever returned from it, but the time to the first token can
  tell whether a prefix was sent before (a timing side channel). VAF sends no per-account
  `cache_salt`; the local server serves one installation, which is reached over the local
  or VPN network only ([USER_ISOLATION.md](../security/USER_ISOLATION.md), section 3).
- **Concurrency:** vLLM batches concurrent requests, but `max_parallel_local_workers` and
  every `provider != "local"` gate stay until that is measured.
- **The pickers:** three model pickers do not know the engine yet and still list GGUFs: the
  web Settings (AI & Model), the first-run wizard and the full-screen TUI. Under vLLM a GGUF
  picked there writes `model`, which only the llama engine reads, and the tray does not
  restart vLLM for it. One picker knows it: the terminal's classic settings menu
  (`vaf/cli/cmd/settings.py`) shows the vLLM model and the `vaf engine vllm --model` command
  instead of a GGUF list.
- **Start time and idle unload:** a vLLM start takes minutes, not seconds. The desktop tray
  unloads the local model when nobody uses it, so the first message after idle waits for a
  full start; `server_persistence_enabled` keeps it loaded.
- **Not on the facade, and not per agent:** the engine is the installation's, like
  `inference_profile`: the server start reads it from the installation's config, which
  `vaf engine` writes, because the one local server on the port is the machine's and two
  agents cannot run it on two engines at once. An `Agent(config={"local_engine": ...})`
  override does not switch it.

## Switching

```bash
vaf engine                       # the engine, this machine's verdict, the install, model, parsers
vaf engine vllm                  # hardware check, install (asks), model, fit, switch
vaf engine vllm --model Qwen/Qwen3-8B-AWQ
vaf engine llama                 # back to llama.cpp; a running vLLM is stopped
vaf engine vllm --uninstall      # removes bin/vllm/venv (models stay in the HF cache)
```

Exit codes like `vaf profile`: 0 done, 1 not complete, 2 refused. The switch writes the
key; the server starts with the next use of the local model. A running desktop tray picks a
switch made in a terminal up within 25 seconds (its config poll watches `local_engine` and
the `vllm_` keys) and restarts the local engine; it restarts only for a key the running
engine reads. Under the `api` inference profile neither engine installs or runs.

## Debugging

- `logs/vllm_server.log`: the server's output of the last start (the start before it in
  `vllm_server.prev.log`); "Application startup complete" marks the end of a start.
- `~/.vaf/vllm.pid`: the pid of the managed server (the leader of its process group).
- `vaf engine`, `vaf health` (the `engine` row): installed version, model, server running
  or idle, and what contradicts the choice.
