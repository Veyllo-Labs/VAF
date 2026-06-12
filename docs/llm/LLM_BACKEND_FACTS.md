# LLM Backend – Facts from the Code

> For the full catalog of provider- and model-specific behavior (DeepSeek, Gemma local mode, the gates), see [PROVIDER_MODES.md](PROVIDER_MODES.md).

## Which backend is used?

In `chat_step()` (agent.py) there are exactly **three** paths:

1. **`if self.api_backend`** → API (OpenAI, Anthropic, DeepSeek, Google, OpenRouter). No local model.
2. **`elif self.use_server`** → HTTP to **127.0.0.1:8080** (native llama-server). Model runs in the **server process**.
3. **`else`** → **Library** (llama-cpp-python, `self.llm`). Model runs **inside the VAF Python process**.

Exactly one of these three paths is always active. Which one is logged in **`logs/backend.log`** (e.g. `chat_step backend=library(llama-cpp-python)` or `backend=server(8080)`).

---

## Default local model (`model: "auto"`)

The default config value `model` is **`"auto"`** (`vaf/core/config.py`). At model load (`vaf/core/agent.py` / `vaf/core/backend.py`), `"auto"` resolves to a **VRAM-adaptive Qwen3.5** GGUF: a 4B model (`unsloth/Qwen3.5-4B-GGUF`) on cards with ≤ 10 GB VRAM, a 9B model (`unsloth/Qwen3.5-9B-GGUF`) above. The quant is chosen so the weights leave room for the desktop/compositor (~1.5–2 GB) **and** the KV cache, not just for the weights alone — each tier sits a notch below "the weights alone fit":

| VRAM | Model | Quant | File size |
|------|-------|-------|-----------|
| < 8 GB | Qwen3.5-4B | `Q4_K_M` (4-bit) | 2.74 GB |
| 8 GB | Qwen3.5-4B | `Q6_K` (6-bit) | 3.53 GB |
| 9–10 GB | Qwen3.5-4B | `UD-Q8_K_XL` (8-bit) | 5.95 GB |
| 11 GB | Qwen3.5-9B | `Q5_K_M` (5-bit) | 6.58 GB |
| 12–15 GB | Qwen3.5-9B | `Q6_K` (6-bit) | 7.46 GB |
| 16–19 GB | Qwen3.5-9B | `Q8_0` (8-bit) | 9.53 GB |
| 20–23 GB | Qwen3.5-9B | `UD-Q8_K_XL` (8-bit) | 12.97 GB |
| ≥ 24 GB | Qwen3.5-9B | `BF16` (16-bit) | 17.92 GB |

VRAM is read from the primary GPU (`nvidia-smi` / `rocm-smi`, total memory). The picker is `recommended_default_model(vram_gb=None)` in `vaf/core/gpu_detection.py` (pass an explicit `vram_gb` to override detection).

An explicit `"repo/file.gguf"` (a value with ≥ 2 path segments) pins a specific model; a bare name/`repo` is resolved as before. The picker is `recommended_default_model()` in `vaf/core/gpu_detection.py`.

**Auto-download and self-heal.** When the resolved model file is not on disk, VAF downloads it from HuggingFace before any server/library load, through one shared function — `backend.ensure_model_available` — used by every path (the tray/server auto-start via `ensure_model_present`, the agent/CLI via `ensure_model_exists`, and the headless web worker). A bare filename of a built-in default (e.g. `Qwen3.5-4B-UD-Q8_K_XL.gguf`) is mapped back to its repo so it can still be fetched; a full `repo/file.gguf` is used as-is. If the configured model cannot be resolved to a repo, or its download fails, VAF falls back to the VRAM-adaptive default (`recommended_default_model`) and downloads that — so an empty `models/` directory recovers to a model that fits the GPU instead of a dead start. The download is serialized by a `filelock` (`models/.download.lock`), so the tray, the web worker and a `vaf run` never fetch the same file at once or read a half-written one: the first caller downloads, the rest block then find the finished file, and `load_model` waits for an in-progress download instead of starting a server against a missing file. Progress is mirrored into `model_download_state.MODEL_DOWNLOAD` and broadcast over the WebSocket (`model_download_progress`), so the WebUI shows the same download banner for an auto-download as for a WebUI-initiated one.

---

## CUDA auto-install (NVIDIA GPU without CUDA)

When the primary GPU is NVIDIA but CUDA is not available, VAF can **auto-install** CUDA-enabled `llama-cpp-python` — but **only on the in-process library path**, i.e. only when `load_model()` actually falls back to `llama-cpp-python` as the backend. The standalone (Vulkan) llama-server path does **not** trigger it: the server has its own GPU backend and never loads the in-process library, so a server-backed run (including the default on Python 3.13) installs nothing. This avoids a re-download loop where the ~1.6 GB CUDA wheel (`--no-cache-dir --force-reinstall`) was pulled on every start for a backend that was never used, and never succeeded when the system was missing `libcudart`. It is also **skipped when the model file itself is missing** (e.g. the server start raced a still-running first-run download): a missing model is a download problem, not a CUDA one, so `load_model` bails out cleanly — letting the caller show "model unavailable" and retry — instead of reinstalling CUDA for a model that is not there. There is **no terminal `[Y/n]` prompt** — the Web UI / headless worker shares the terminal's stdin, so a prompt there would freeze the chat request. Controlled by **`auto_install_gpu`** (default `true`; set `false` to stay on CPU). The reinstalled package is used on the **next VAF restart**; until then the current process runs on CPU. Manual path: `vaf install-gpu`.

**CUDA version mismatch (`libcudart.so.12: cannot open shared object file`):** the prebuilt CUDA `llama-cpp-python` (`libllama.so`) links against **CUDA 12** runtime libs (`libcudart.so.12`, `libcublas.so.12`, `libnvrtc.so.12`). If the environment only has a *different* CUDA major (e.g. a CUDA 13 toolkit / `libcudart.so.13`), `libllama.so` fails to load and the model never loads — surfacing as `Failed to load shared library '.../libllama.so': libcudart.so.12: cannot open shared object file`. Fix **without** disturbing the newer CUDA: add the CUDA 12 runtime libs and make them discoverable. `libllama.so` is built with `RPATH=$ORIGIN` (it searches its own directory), so:

```
venv/bin/pip install nvidia-cuda-runtime-cu12 nvidia-cublas-cu12 nvidia-cuda-nvrtc-cu12
# symlink the cu12 libs next to libllama.so so $ORIGIN finds them:
for d in cuda_runtime cublas cuda_nvrtc; do
  for so in venv/lib*/python3.*/site-packages/nvidia/$d/lib/*.so.12*; do
    ln -sfn "$(realpath "$so")" "$(dirname "$(realpath venv/lib*/python3.*/site-packages/llama_cpp/lib/libllama.so)")/$(basename "$so")"
  done
done
```

The NVIDIA driver is backward-compatible, so a CUDA 12 runtime runs fine alongside a CUDA 13 toolkit. **Caveat:** reinstalling/upgrading `llama-cpp-python` recreates `llama_cpp/lib/` and drops the symlinks — re-apply them.

> ⚠️ **Only worth doing if the prebuilt wheel's CPU backend matches the host CPU.** Making `libllama.so` loadable lets initialization reach the CPU backend (`ggml_cpu_init`, inside `llama_backend_init`). If the wheel was built with CPU instructions the host CPU lacks, it then **crashes with `SIGILL`** there — a hard core-dump that takes down the whole process (e.g. the tray), which is **worse** than the original catchable load failure. CUDA itself is fine (it detects the GPU first — `ggml_cuda_init: found 1 CUDA devices`); the crash is the CPU path. If you hit this, do **not** force the in-process library: **remove the symlinks again** (so the load fails gracefully) and run the model via the **Vulkan llama-server** — the default on Python 3.13 — which uses its own backend and never loads `libllama.so` in-process.

---

## When is Server (8080) vs. Library used?

In `load_model()` (see `vaf/core/agent.py`):

- **Windows default:** If `force_server` is not explicitly set, Windows defaults to **`True`**. Goal: avoid loading the model twice into the Python process.
- **Server path:** The agent uses the HTTP server path (`127.0.0.1:8080`) when `self.use_server` is active.
- **Library path:** Only when `self.use_server` is not active does the model run in-process via `llama-cpp-python`.

**Practical note:** The old rule "Windows + no `force_server` = always Library" is outdated. Current builds already default to server-friendly behavior on Windows.

**To force the local model exclusively through the server (8080):** `force_server: true` remains the explicit switch (in addition to the Windows default logic).

---

## Why was no Thinking (<think>) received?

- **Server path (8080):** Reads `delta.get('reasoning_content')` **and** `delta.get('content')` and streams both (thinking + answer). Works correctly for a local reasoning model on the server.
- **Library path (llama-cpp-python):** Previously only read `delta.get('content')`. A separate `reasoning_content` field was **not** evaluated and **not** streamed.

**Change:** The library path now reads `reasoning_content` the same way as the server path and streams it as thinking (including `<think>` / `</think>`). If the local model returns `reasoning_content` via the library, it now appears in the UI.

---

## DeepSeek: reasoning_content in Conversation History

When DeepSeek models return a response with `reasoning_content` (thinking mode), VAF stores it inline in the assistant message as `<think>...</think>`. On the **next** API call, DeepSeek's API requires that `reasoning_content` is passed back as a **separate field** in the assistant message — not embedded in the `content` field. If it is missing or only present inside `content`, the API returns:

```
400 - The reasoning_content in the thinking mode must be passed back to the API.
```

**Fix (implemented in `_prepare_messages()` in `agent.py` and `clean_history` in `coder.py`):**  
Before every DeepSeek API call, assistant messages containing `<think>...</think>` are transformed:
- The thinking text is extracted and placed in a separate `reasoning_content` field
- The `content` field is set to the remaining non-thinking text (or `""` if empty)
- Messages with `content=""` and no `tool_calls` are **kept** if they have `reasoning_content` (the empty-message filter was previously dropping them, causing 400 on the next turn)

**Fix — coder streaming (coder.py):** The coder makes direct HTTP requests and receives `reasoning_content` as a separate field from DeepSeek (not wrapped in `<think>` tags by `api_backend.py`). The streaming code now tracks the reasoning phase (`_in_reasoning_phase`) and wraps reasoning chunks in `<think>...</think>` in `collected_content`, so `clean_history` can extract them correctly on the next turn.

This transformation is **DeepSeek-specific** — for all other providers (`openai`, `anthropic`, `google`, `openrouter`) `<think>` tags are simply stripped from `content` without adding `reasoning_content`.

---

## Tool-Calls inside <think>

If the model emits a tool call **inside** `<think>...</think>` (e.g. `<tool_call>{"name": "update_intent", ...}</tool_call>` in the think block), it is still detected:

- **XML fallback** (agent.py): Both `full_response` and `full_reasoning` are searched (`text_to_search = full_response + "\n" + full_reasoning`). This catches tool calls in `<think>` even when thinking was streamed separately.
- **System prompt:** The agent is instructed to place tool calls **in the main response (after `</think>`)**, not inside `<think>`, so they are reliably executed.

---

## Logs for debugging

- **`logs/backend.log`**: One line per chat step with the backend in use, e.g. `chat_step backend=library(llama-cpp-python)`, `chat_step backend=server(8080)`, or `chat_step backend=api(openai)`.
- **`logs/memory.log`**: `[PROFILER]` entries (RAM every 30 s), plus compaction, usage, embedding load, `[WHISPER]` load.
- **`logs/startup_trace.txt`**: Tray and WebServer startup. "Model loaded" means the tray started the server (8080); the agent may still use the library if `load_model()` did not take the server path.

Together, **backend.log** and **startup_trace.txt** show whether the tray started the server and whether the agent is using the API, server (8080), or library.

---

## Dangling tool_calls in assistant messages (400: insufficient tool messages)

**Error:** `"An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'. (insufficient tool messages following tool_calls message)"`

### Cause 1: Context compression (compression-induced orphans)

After `ContextManager.compress()`, the middle section of history is summarized. Critical **tool results** are preserved but their corresponding **assistant+tool_calls** messages may be discarded → dangling `assistant+tool_calls` messages (or out-of-order role:tool messages) cause 400 on the next API request.

**Sub-case 1a — coder.py:** `clean_history` computes two index sets:
- `_valid_tool_call_ids`: IDs present in any `assistant.tool_calls` → used to drop orphaned `role: tool` responses
- `_responded_ids`: IDs that have a matching `role: tool` response → used to **strip dangling tool_calls from assistant messages**

**Sub-case 1b — agent.py `_prepare_messages()` (position-aware):** `ContextManager.compress()` inserts preserved critical `role:tool` results from the middle section **before** `recent_messages`. This means a `role:tool` can appear at an *earlier index* than its `assistant+tool_calls` message. A naive set-membership check would incorrectly consider such a TC "responded to", but the API finds no tool result *following* the TC → 400.

**Fix (`_prepare_messages()` in `agent.py`):** Uses a position-aware dict `_tc_response_idx: {id → index}`. A tool_call is only counted as "responded" if its `role:tool` appears at a **later** index than the `assistant+tool_calls` message. Also removes orphaned `role:tool` messages (whose TC was stripped).

### Cause 2: System messages injected between tool results in the same TC batch

When the agent calls **multiple tools** in a single response, all `role:tool` results for that batch must appear consecutively — no system/user messages between them. An error handler injecting a system message between two consecutive role:tool results causes the API to stop reading tool results early → "insufficient tool messages".

**Instances fixed:**

- **`coder.py` — write_file handler:** Nudge/linter messages were appended to `history` before the tool result. Fix: `_post_tool_messages` list — deferred until after the tool result.
- **`coder.py` — set_todos reminder:** No `and not tool_calls` guard → user message injected between TC and tool result. Fix: added `and not tool_calls`.
- **`agent.py` — `is_tool_error` system message & `document_agent` failure message:** Both were appended to `self.history` inside `for tc in tool_calls_detected:`, after the current tool's result but before results of subsequent tools in the same batch. Fix: `_post_tc_messages` list initialized before the `for tc` loop; both messages deferred to it; flushed to `self.history` after the loop ends.

### Cause 3: reasoning_content stripped from tool-calling messages (DeepSeek-specific)

DeepSeek requires `reasoning_content` to be passed back for **every** assistant message that had reasoning — including tool-calling ones. There is no restriction on sending RC for multiple tool-calling messages.

**Misdiagnosis history:** An early fix stripped RC from ALL tool-calling assistant messages, assuming it caused "insufficient tool messages" errors. This was wrong — that error was caused by Cause 2 (user/system message injection between TC and tool result). Stripping RC from TC messages caused a new "reasoning_content must be passed back" 400 error.

**Fix (`clean_history` in `coder.py`):** `reasoning_content` is extracted and passed back for ALL assistant messages (with or without `tool_calls`) that contain `<think>...</think>` in their content. The `<think>` tags are stripped from `content` in all cases. Other providers (OpenAI, Anthropic, Google) just have the tags stripped without adding `reasoning_content`.

---

## DeepSeek: tool_choice Restrictions

DeepSeek's API (both `deepseek-v4-flash` and `deepseek-v4-pro`) only supports `tool_choice: "auto"` and `tool_choice: "none"`. **`tool_choice: "required"` and specific function-forcing are rejected with a 400 error:**

```
{"message": "deepseek-reasoner does not support this tool_choice"}
```

Note: Despite the error message saying "deepseek-reasoner", this affects **all** current DeepSeek models — `v4-flash` and `v4-pro` are internally reasoning models. The API returns this misleading model name regardless of what model name was sent.

**Fix in `vaf/tools/coder.py`:** When `_provider == "deepseek"` and `tool_choice == "required"`, the coding agent downgrades to `"auto"` and injects an explicit user message instructing the model to call `set_todos` immediately. This preserves the planning-first behavior without relying on the API parameter.

**Fix in `vaf/core/api_backend.py`:** The same guard exists for the main agent's tool router.

---

## DeepSeek: Deprecated Model Names (Auto-Migration)

Old DeepSeek model names (`deepseek-chat`, `deepseek-coder`, `deepseek-reasoner`, `deepseek-r1`) are no longer valid and cause errors. **`vaf/core/config.py` `load()` auto-migrates** any saved config value matching these names to `deepseek-v4-flash` on first load, and writes the fix back to `config.json` permanently.

Current valid model IDs (as of 2026-05): `deepseek-v4-flash`, `deepseek-v4-pro`.

---

## Context Window Configuration

When using the Local Server (llama-server), the context window size (`n_ctx`) is critical for tool calling to work correctly.

- **Minimum: 32768** — enforced regardless of the configured value. With 100+ tools, the overhead alone is ~11K tokens (system prompt ~5.5K + tool schemas ~6K), leaving ~20K for conversation. Values below 32K cause the router safety net to trigger on every turn.
- **Configuration:** Set `n_ctx` in `config.json` (or via Settings → Advanced). A value below 32768 is clamped up to 32768 when the configuration is loaded (`Config.load`), so every reader and the local server see one consistent floor. The default is `32768`.
- **KV Cache:** VAF uses `q8_0` for keys and `q4_0` for values — ~62.5% less VRAM than f16 with negligible quality loss.
- **VRAM estimate (RTX 3080, 10GB):** the default gemma-4 E2B (Q8_0) is a small model; together with the quantized KV cache it leaves ample headroom on a 10 GB card. KV-cache use scales with `n_ctx` and becomes the main driver at large windows (e.g. 128K).

## Native Chat Template and Tool Calling

The model's embedded Jinja template defines the tool-call format. Some GGUFs use a `<tool_call>` XML block:
```
<tool_call>
{"name": "web_search", "arguments": {"query": "..."}}
</tool_call>
```

llama-server (with `--jinja`) parses these tags and converts them to standard OpenAI `tool_calls` objects in the API response. VAF's streaming parser then picks them up and executes the tools.

**Do not override with `--chat-template chatml`** — the generic chatml template does not match the model's native tool-call format, which causes the model to describe tool usage in text instead of emitting actual tool calls (hallucination).
