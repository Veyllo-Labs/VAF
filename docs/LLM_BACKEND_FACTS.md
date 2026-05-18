# LLM Backend – Facts from the Code

## Which backend is used?

In `chat_step()` (agent.py) there are exactly **three** paths:

1. **`if self.api_backend`** → API (OpenAI, Anthropic, DeepSeek, Google, OpenRouter). No local model.
2. **`elif self.use_server`** → HTTP to **127.0.0.1:8080** (native llama-server). Model runs in the **server process**.
3. **`else`** → **Library** (llama-cpp-python, `self.llm`). Model runs **inside the VAF Python process**.

Exactly one of these three paths is always active. Which one is logged in **`logs/backend.log`** (e.g. `chat_step backend=library(llama-cpp-python)` or `backend=server(8080)`).

---

## When is Server (8080) vs. Library used?

In `load_model()` (see `vaf/core/agent.py`):

- **Windows default:** If `force_server` is not explicitly set, Windows defaults to **`True`**. Goal: avoid loading the model twice into the Python process.
- **Server path:** The agent uses the HTTP server path (`127.0.0.1:8080`) when `self.use_server` is active.
- **Library path:** Only when `self.use_server` is not active does the model run in-process via `llama-cpp-python`.

**Practical note:** The old rule "Windows + no `force_server` = always Library" is outdated. Current builds already default to server-friendly behavior on Windows.

**To force VQ1 exclusively through the server (8080):** `force_server: true` remains the explicit switch (in addition to the Windows default logic).

---

## Why was no Thinking (<think>) received?

- **Server path (8080):** Reads `delta.get('reasoning_content')` **and** `delta.get('content')` and streams both (thinking + answer). Works correctly for VQ1 on the server.
- **Library path (llama-cpp-python):** Previously only read `delta.get('content')`. A separate `reasoning_content` field was **not** evaluated and **not** streamed.

**Change:** The library path now reads `reasoning_content` the same way as the server path and streams it as thinking (including `<think>` / `</think>`). If VQ1 returns `reasoning_content` via the library, it now appears in the UI.

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

**Root cause:** After context compression (`ContextManager.compress()`), the middle section of history is summarized. Critical **tool results** are preserved but their corresponding **assistant+tool_calls** messages may be discarded. After compression the `clean_history` in `coder.py` sees an assistant message with `tool_calls` whose matching `role: tool` response is gone → 400 on the next API request.

**Fix (`clean_history` in `coder.py`):** Before building `clean_history`, two index sets are computed from raw history:

- `_valid_tool_call_ids`: IDs present in any `assistant.tool_calls` → used to drop orphaned `role: tool` responses
- `_responded_ids`: IDs that have a matching `role: tool` response → used to **strip dangling tool_calls from assistant messages**

For each assistant message, any `tool_calls` entry whose ID is not in `_responded_ids` is removed. If all tool_calls are dangling, the `tool_calls` key is removed entirely. This is the same logic `_prepare_messages()` in `agent.py` applies.

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

- **Minimum: 32768** — hardcoded in VAF regardless of config. With 100+ tools, the overhead alone is ~11K tokens (system prompt ~5.5K + tool schemas ~6K), leaving ~20K for conversation. Values below 32K cause the router safety net to trigger on every turn.
- **Configuration:** Set `n_ctx` in `config.json` (or via Settings → Advanced). Values below 32768 are silently raised to 32768.
- **KV Cache:** VAF uses `q8_0` for keys and `q4_0` for values — ~62.5% less VRAM than f16 with negligible quality loss.
- **VRAM estimate (RTX 3080, 10GB):** VQ-1 q4_k_m (~4GB) + 32K KV cache (~0.8GB) = ~4.8GB total, leaving ~5GB free.

## Native Chat Template and Tool Calling

VQ-1's embedded Jinja template uses `<tool_call>` XML format for function calls:
```
<tool_call>
{"name": "web_search", "arguments": {"query": "..."}}
</tool_call>
```

llama-server (with `--jinja`) parses these tags and converts them to standard OpenAI `tool_calls` objects in the API response. VAF's streaming parser then picks them up and executes the tools.

**Do not override with `--chat-template chatml`** — the generic chatml template does not include `<tool_call>` formatting, which causes the model to describe tool usage in text instead of emitting actual tool calls (hallucination).
