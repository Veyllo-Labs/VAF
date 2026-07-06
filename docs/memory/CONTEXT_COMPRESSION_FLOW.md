# Context Compression – Step-by-Step Flow

This document describes **exactly** what happens during context compression in VAF: where it is triggered, what the `ContextManager` does, and how this ties in with the dynamic system prompt.

---

## 1. Where is compression triggered?

**File:** `vaf/core/agent.py`

Compression is checked **once per user turn** in `chat_step()`, **before** the new user message is appended to the history.

Order within `chat_step()`:

1. `context_manager.decay_state()` – the TTL of state entries (files, etc.) is decremented.
2. **Dynamic Context:** when `user_input` and `prompt_manager` are present:
   - Detect the language, call `analyze_context(user_input)`, and build `new_prompt = build_prompt(...)`.
   - `new_prompt` is set **only within this block**; it is **not** written into the history.
3. **Context Compression:**
   - **Condition:** `context_manager.should_compress(self.history)` must be `True`.
   - If **yes:** call `compress()`, then append the context glue to `new_prompt` and overwrite the system prompt in `history[0]` with this `new_prompt` (including the glue and, if present, PROJECT CONTEXT).
   - If **no:** nothing happens to the history; a `new_prompt` built earlier is **not** written into `history[0]` (it is applied only when compression runs this turn).

---

## 2. When does "should be compressed" apply?

**File:** `vaf/core/context.py`

```python
def should_compress(self, history: List[Dict]) -> bool:
    usage = self.get_usage_percent(history)
    return usage >= self.trigger_threshold
```

- **`trigger_threshold`:** Dynamic, based on `max_tokens` (e.g. 0.70 for small windows, 0.85 for large windows up to 128k, 0.90 for very large windows).
- **`get_usage_percent(history)`:**  
  `estimate_tokens(history) / max_tokens`  
  – i.e. the estimated tokens of the current history divided by the configured context limit (e.g. 8192 or 128000).

**In short:** compression is triggered as soon as the estimated usage of the history reaches the dynamic threshold of the current context window.

---

## 3. Token estimation (`estimate_tokens`)

**File:** `vaf/core/context.py`

- Per message:
  - The estimate uses dynamic ratios depending on `max_tokens`.
  - Small contexts (`<= 16384`) use more conservative ratios (more estimated tokens per character) than large contexts.
  - Role fields are also counted using the active text ratio.
- Then: a **+10 %** safety margin is added to the total (special tokens, formatting).

No real tokenization (e.g. tiktoken) is used, only a character-based estimate.

---

## 4. Flow of `compress(history)` – step by step

**File:** `vaf/core/context.py`, method `compress()`.

### 4.1 Precondition

- If `len(history) < 3`: **no** compression; `history` is returned unchanged.

### 4.2 Step 1: Archive

- `_archive_history(history)` is called.
- **In-memory:** a `ContextSnapshot` (timestamp, history copy, intent, state, token count) is appended to `self.archive`; at most 3 snapshots are kept, the oldest is removed.
- **On disk:** a JSON file is written under `~/.vaf/context_archive/` (`context_YYYYMMDD_HHMMSS_<hash>.json`) containing the history, intent, state, and token count. Optional; errors are silently ignored.

### 4.3 Step 2: Update intent and state from **all** of the history

- **All** messages in `history` are iterated over:
  - For `role == "user"`: `update_intent(msg["content"])` – extracts goals, keywords, and constraints (regex patterns).
  - For **every** message: `update_state(msg)` – extracts:
    - files (created/read/modified),
    - errors (error/failed/fehler),
    - tools (from `role=="tool"`),
    - "Key decisions" from assistant text (without `<think>`),
    - code snippets from code blocks.

This keeps intent and state up to date **before** the old messages are discarded.

### 4.4 Step 3: Critical tool results from the "middle part"

- **Middle part:** `history[1 : -recent_memory_size]` (everything except the first entry and the last `recent_memory_size` messages).
- Within it, messages with `role == "tool"` and a `name` in `preserve_tools` are searched for (including core tools such as `set_todos`, `write_file`, `read_file`, plus further safety-relevant tools depending on the current implementation).
- Per match: the content is truncated to 300 characters and collected in `critical_tools` as a message with `role`, `name`, `content`, and `tool_call_id`.
- Later, at most the **last 5** of these critical tool messages are carried over into the new history.

### 4.5 Step 4: Building blocks of the new history

- **System prompt:** `system_prompt = history[0]` (**always** carried over; its content can still be replaced by `new_prompt` in the agent afterwards).
- **Recent:** `recent_messages = history[-recent_memory_size:]` (dynamic, depending on `max_tokens`) – kept **unchanged** ("raw").

### 4.6 Step 5: Build the context summary ("glue")

- `_build_context_summary()` produces a text block consisting of:
  - **Narrative summary** (if set by the state),
  - **Project state:** created/modified/read files,
  - **Errors,** key decisions,
  - **Primary goal** from the intent.
- Format: Markdown with headings such as `RECENT SUMMARY`, `PROJECT STATE`, etc.

### 4.7 Step 6: Assemble the new history

- `new_history = [system_prompt]`
- If `context_summary` is not empty: a **second system message** with content `context_summary` is appended.
- Then: up to 5 critical tool messages (see 4.4).
- Then: `recent_messages` (the last 10 messages).

Result: significantly fewer messages and a sharply reduced token count, while preserving "stability" (intent, state, last N messages).

### 4.8 Logging

- UI messages such as "Compressing (X/Y tokens, Z%)…", "Compressed: N → M msgs, X → Y tokens", "Preserved K critical tool results", "Full history archived. Use /restore to recover."

---

## 5. What happens in the agent **after** `compress()`?

**File:** `vaf/core/agent.py` (directly after `self.history = self.context_manager.compress(self.history)`):

1. **Add the context glue:**
   `context_glue = self.context_manager._build_context_summary()` is built **again** and appended to **`new_prompt`** (`new_prompt += ...`).
   Note: `new_prompt` only exists if the "Dynamic Context" block ran during **this** turn (i.e. `user_input` and `prompt_manager` were present). Otherwise `new_prompt` is not defined in this branch.

2. **Preserve PROJECT CONTEXT:**
   If `self.history[0]["content"]` contains the `## PROJECT CONTEXT` section, that part is extracted and appended to `new_prompt`.

3. **Replace the system prompt:**
   `self.history[0]["content"] = new_prompt`
   – so the dynamic system prompt (including the glue and PROJECT CONTEXT) ends up in the history **only on compression**.

---

## 6. Quick overview: when is what done?

| Step                    | Where / When |
|---------------------------|-----------|
| Check "should it be compressed?" | Every turn in `chat_step()`, when `usage >= trigger_threshold` (dynamic) |
| `compress(history)`       | Only when `should_compress(history)` is True |
| Archive (memory + disk)    | Always at the start of `compress()` |
| Intent/state from the history   | In `compress()`, across all messages |
| Keep: system + last N + glue + critical tools | In `compress()` |
| Glue + PROJECT CONTEXT in the system prompt | In the agent only **if** compression happened this turn **and** `new_prompt` was set |

---

## 7. Configuration (ContextManager)

- **`max_tokens`:** Set when the `ContextManager` is created (e.g. from the agent config / `n_ctx`); default 8192 (can be raised to e.g. 128000).
- **`trigger_threshold`:** Dynamic, depending on `max_tokens` (small/medium/large windows).
- **`recent_memory_size`:** Dynamic, depending on `max_tokens` (from small windows up to 200 for very large windows).
- **`preserve_tools`:** The tool list is extended; core tools are retained, and additional tool types are taken into account as well.

---

## 8. Edge cases and safeguards

An earlier `new_prompt` `NameError` on compression without user input has been resolved and is no longer a live failure path. When investigating behavior in this area, verify against the current `agent.py` rather than relying on older descriptions.

### Dangling `tool_calls` after compression

`compress()` keeps the last N messages as `recent_messages` and discards the middle section. If this middle section contains a `{role: "tool", tool_call_id: "X"}` response while the corresponding `{role: "assistant", tool_calls: [{id: "X"}]}` message ends up in `recent_messages`, a "dangling tool_call" is created — the API (e.g. DeepSeek) rejects this state with HTTP 400.

**Fix (since 2026-05-14):** `_prepare_messages()` in `agent.py` runs a cleanup pass before every API call: all `tool_call_id`s in `{role: "tool"}` messages are collected; `tool_calls` entries in `assistant` messages without a matching response are removed. If all calls in a message are dangling, `tool_calls` is removed entirely.

---

## 9. Where user-related info appears (system prompt vs. separate message)

The model receives user-related information in two ways:

### 9.1 In the system prompt: "User identity (current user)" block

**Location**: `vaf/core/system_prompt.py`, `build_prompt()` → block **"## User identity (current user)"**.

- **When `username` is set**: Name and `preferred_language` (and preferences, do's, don'ts) are read from **`user_identity.json`** in that user's workspace (`~/.vaf/users/<username>/user_identity.json`). See [USER_IDENTITY.md](USER_IDENTITY.md).
- **When `user_scope_id` is set**: In addition, **"Known facts from memory"** is read from a cache file:
  - Path: `Config.APP_DIR / "user_profile_cache" / f"{user_scope_id}.txt"`
  - Content: Result of a RAG search with the fixed query `"user profile facts preferences about this user"` (k=8).
  - **When the cache is written**: In `vaf/memory/rag.py`, `refresh_user_profile_summary(user_scope_id)` runs after **session compaction** (every N user turns). So the cache is updated periodically, not every turn.

The main system prompt therefore includes the current user profile (from `user_identity.json`) and, if present, the cached RAG summary. This is in `history[0]` once `build_prompt()` has run and the result is written into history (every turn when the dynamic prompt is applied).

### 9.2 Second system message: "Memory context (relevant to this query)"

**Location**: `vaf/core/agent.py`, immediately before the LLM call (`api_backend.chat_completion` or server payload).

- `memory_context` is passed into `chat_step(..., memory_context=None)` by the caller (headless runner, gateway, or automation), which runs a RAG search with the **current user message** as the query.
- The messages sent to the LLM are built as: first the main system prompt (`history[0]`), then a second system message with content `"## Memory context (relevant to this query)\n\n" + (memory_context or a "No memories found" placeholder)`. So the model sees: `[system prompt, memory context message, user, assistant, ...]`.

The RAG results for this specific query are therefore in a separate system message, not inside the main system prompt string. The model sees both; user-related info comes partly from the prompt (user_identity.json + optional cache) and partly from that second message (query-specific RAG results).
