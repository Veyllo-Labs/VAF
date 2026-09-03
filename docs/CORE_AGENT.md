# CoreAgent Reference

`vaf.CoreAgent` (the engine class `vaf.core.agent.Agent`, ~11k lines) is the
advanced embedding surface behind the `vaf.Agent` facade. This page is the
reference for the parts an embedder can rely on; start with
[EMBEDDING.md](EMBEDDING.md) and use the facade unless you need engine-level
control. Design map of the turn loop: [AGENT_LOOP.md](agents/AGENT_LOOP.md).

Stability: `CoreAgent` is part of the declared stable surface at the level
documented HERE (constructor, lifecycle, `chat_step`, `complete`, `execute_tool`,
`set_tool_authorizer`, `set_event_sink`, `set_compaction_hook`, the accessors below). Underscore-prefixed attributes are
internal; the ones listed at the end are known extension points that may
change with a changelog note.

---

## Constructor

```python
CoreAgent(
    verbose=False,           # extra stdout diagnostics; also un-suppresses llama-cpp stderr
    register_signals=True,   # SIGINT/SIGTERM(/SIGHUP) handlers -> shutdown();
                             # pass False off the main thread (registration
                             # there degrades to a warning no-op)
    config_overrides=None,   # dict merged over ~/.vaf/config.json, in memory only;
                             # api_key_<provider> is passed RAW (not Base64-decoded)
    run_kind=None,           # "chat" | "thinking" | "automation" | None (env-sniffed)
    host_audio=False,        # opt-in host-speaker TTS; interactive CLI only
)
```

Construction is heavy: it loads config, resolves the model, scans and
registers all tools (in-tree, custom dir, pip entry points, MCP), and for API
providers builds the HTTP backend (failures are swallowed; `api_backend`
stays `None`). With `config_overrides` set, `reload_api_backend()` becomes a
no-op - embedded config is caller-controlled.

Unlike the facade, constructing `CoreAgent` directly does **not** set
`VAF_NONINTERACTIVE`. In a TTY context a gated tool will therefore block on a
terminal confirmation prompt; set the env var yourself for headless use.

## Lifecycle

```python
agent = CoreAgent(register_signals=False, config_overrides={...})
agent.init_chat()      # builds the system prompt, RESETS history
agent.load_model()     # local provider only: download + start/reuse the one
                       # llama server on 127.0.0.1:8080; no-op for API providers
...
agent.shutdown()       # idempotent cleanup; safe to call manually
```

- `init_chat()` rebuilds the system prompt from the current tools/config,
  loads `VAF.md` project context from the cwd (capped), and resets
  `agent.history` to just the system message. Call it once before the first
  `chat_step`, and again only when you want a fresh conversation.
- `load_model()` is NOT lazy-called by `chat_step`: in local mode you (or the
  facade, which does this for you) must call it before chatting, else the
  turn aborts with "Agent not initialized". Caveat: it reuses ANY healthy
  already-running server without checking which model it serves - the
  model-aware stop-and-respawn lives in the server manager's start path and
  only runs when no healthy server responded. After changing the configured
  model, call `reload_local_model()` (which routes through the model-aware
  `ensure_local_model`) rather than relying on `load_model()` to swap it.
- `shutdown()` stops background helpers and reference-counts other VAF
  processes before touching the shared llama server, so it will not kill a
  server other sessions still use.

## chat_step

```python
chat_step(
    user_input: str,
    stream_callback=None,      # callable(str) - see OBSERVABILITY.md for delta caveats
    auto_retry=False,          # internal recursion flag; leave default
    skip_input=False,          # skip routing/analysis/prompt rebuild
    disable_workflows=False,   # bypass the workflow router
    disable_tools=False,       # send no tools (tool_choice="none")
    memory_context=None,       # str injected as a "## Memory context" section
    thinking_mode=False,       # background-thinking turn (proactive runs)
    images=None,               # [{data, mime_type, name, ...}] vision input
    force_tool_choice=None,    # thinking_mode only
    allow_memory_search=False, # thinking_mode only
) -> str | None
```

Runs one full turn: routing, system-prompt rebuild, context compression, the
LLM/tool loop (with loop budgets), guardrails, persistence.

**`memory_context` is a string you supply; the section around it is the engine's.**
The engine wraps it in the `## Memory context (relevant to this query)` heading and
its guidance sentences, falls back to a "no memories found" variant when you pass
nothing, and appends its own Cross Chat Hint block underneath when the caller's
scope has matching other chats (`cross_chat_hint_enabled`). The result is spliced
into a copy of the first system message, never into `agent.history`.

Several production sites in this repo hand-roll "retrieve snippets and hand them to a
prompt" (the `memory_rag_k` clamp itself now exists once, in `turn_memory_context` -
four lanes used to repeat it), and the headless
runner splices seven further ad-hoc context blocks inline. That is the measured
case for a general context-contributor seam on the public surface. It has not been
built: no feature so far has needed a second labelled sub-block, and until one
does, a registry would be speculative. The number is recorded here so the decision
starts from a measurement rather than a hunch.

**Return contract - read this before using the value.** `chat_step` streams
the real answer; the return value is a status:

- On a normal completion it returns the placeholder `"..."` or a
  check-mark-prefixed `"Tool '<name>' finished: ..."` summary - **not** the
  answer text. Take
  the answer from `stream_callback` (accumulate the deltas) or from
  `agent.history[-1]["content"]` afterwards. The facade's `run()` does exactly
  this and returns the cleaned text.
- `None` means no backend (call `load_model()` / fix the provider), a local
  server error, or an inference exception.
- Meaningful strings are returned for: workflow results, errors
  (`[Error] API backend failure: ...`, `[Error] Server rejected ...`), user
  stop (`[Generation stopped by user]` - though a mid-stream stop returns the
  partial answer instead when text was already produced), loop protection
  (contains `[LOOP_PROTECTION]`, prefixed by a warning emoji - match by
  substring, not prefix), async sub-agent acks (`[ASYNC_ACK]...`), and
  handled degradations (`[SYSTEM_LOG_ONLY]...`).

There is no public `stop()`; a running turn is stopped via
`TaskQueue().request_stop(session_id)` (polled between chunks and tools).

## complete

```python
complete(prompt, *, max_tokens=512, temperature=0.2, timeout=None,
         strip_think=True) -> Optional[str]
```

One completion with this agent's backend - no tools, no history, no memory. The
opposite return contract from `chat_step`, on purpose: the RETURN VALUE is the
answer (reasoning stripped), or `None` when no backend answered; it never raises
and never returns an error message dressed as content, so the result is safe to
store without inspection. The conversation is untouched - a `chat_step` after a
`complete` sees nothing of it.

Backend dispatch follows the documented selection order with the compound gate
(an API provider whose backend failed to initialize falls through to the local
lanes, exactly as `load_model` does), and the API lane reuses `self.api_backend`,
so an embedder's passed keys and the attached event sink travel with the call.
The shared mechanics live in `vaf/core/completion.py` (metadata-frame filter,
error-sentinel handling, the local `enable_thinking:false` lane that never
starts the server); `BaseTool.query_llm` and several CLI features consume the
same primitive.

## execute_tool

```python
execute_tool(name: str, args: dict) -> str
```

Dispatches one tool through the full pipeline, in this order: policy
evaluation (admin-only and channel blocks return `Security Error: ...`), the
interaction gates and the confirmation gate (see
[EMBEDDING.md](EMBEDDING.md) "Headless safety" and "Security posture"), the
`tool_start` event, then schema validation/repair of `args` (invalid input
returns `Tool Error: invalid arguments ...` without dispatch), runtime kwarg
injection (identity from the tool's own `identity_kwargs` declaration - see
[TOOL_ROUTER_ARCHITECTURE.md](agents/TOOL_ROUTER_ARCHITECTURE.md); session and
workspace per tool), and bounded execution with per-tool
timeouts and stop polling. Event schema: the
[event sink](OBSERVABILITY.md). Always returns a string (tool result or
error text).

That pipeline is not private to the chat turn: it lives in
`vaf/core/tool_dispatch.py` as `ToolCaller`, and an embedder can run a tool
through the very same object (see [EMBEDDING.md](EMBEDDING.md), "Running a tool
yourself"). What `execute_tool` adds on top are the stages that belong to a chat
turn specifically - the plan and reply gates, the session plumbing, the router
bookkeeping - and it adds them as hooks into the shared pipeline rather than as a
dispatcher of its own.

The workflow engine is a **partial** adopter and it is worth being exact about
which parts, because the missing ones are the security ones. It shares bounded
execution (`run_tool_bounded`) and identity assignment
(`assign_declared_identity`, in `declared` mode, and only where its caller
supplied an identity at all). It does **not** build a `ToolCaller`, so a workflow
step is not policy-checked, not gated, not argument-repaired, and emits no
tool events.

## Deciding about a tool call

```python
set_tool_authorizer(authorize: Callable[[ToolRequest], None] | None) -> None
```

Consulted on every dispatch that got past policy, before the confirmation gate.
The callback answers with `request.deny(reason)`, `request.ask(reason)` or
`request.allow()`; answering nothing means having no opinion and the call
proceeds unchanged, so a forgotten answer is the status quo rather than a silent
approval. `allow()` skips the confirmation question for that one call and cannot
reach a policy-blocked tool. An exception inside the callback is treated as a
refusal - the opposite polarity from the event sink, deliberately: a broken
observer must not fail a run, a broken guard must not become no guard. In-process
only; it does not cross into the coder sub-agent. Semantics and the full request
shape: [EMBEDDING.md](EMBEDDING.md).

## Putting state back after a compaction

```python
set_compaction_hook(hook: Callable[[dict], str | None] | None) -> None
```

Fired right after a structural compaction, on both paths that compact (the check at the
top of a turn and the session-load pass), with `info = {"before", "after", "tokens",
"session_id"}`. A returned non-empty string is appended to the history as one system
note: the seam for what a summary loses. The hook cannot edit the history and runs outside
the tool loop. Bounded by `COMPACTION_HOOK_SECONDS` (a timeout is "nothing to add") and
forgiving: an exception is swallowed and logged, the event sink's polarity, because a
broken observer must not fail a run. `visible_tools()` beside it is the one answer to
"may the model see this tool": the registry minus `_excluded_tools`, read by the schema,
`list_tools`, `search_tools`, the router prompt, the system prompt's tool documentation
and the sandbox's tool bridge alike; `execute_tool` still runs a hidden tool.

## Observability and accessors

- `set_event_sink(callable)` - structured `tool_start`/`tool_end`/
  `gate_required`/`gate_decision` events; full schema in
  [OBSERVABILITY.md](OBSERVABILITY.md).
- `get_token_usage() -> (used, max)` - provider-appropriate context usage.
- `history` - the OpenAI-style message list (the system prompt is
  `history[0]`).
- `get_live_session_subagents()` - session-scoped, heartbeat-verified list of
  running sub-agents (use this, never process-global state).
- `load_session_context(session_id, *, force=False)` - swap the agent onto a persisted
  session: rebinds identity from session metadata, re-inits the prompt, and
  replays messages preserving tool-call linkage. It returns immediately when the
  agent is already on that session; `force=True` rebuilds anyway, which is what a
  rewind or any out-of-band edit of the stored transcript needs, because the
  in-memory history is otherwise authoritative and the agent would keep answering
  from a version the store no longer holds.
- Hot reload: `reload_builtin_tools()` (new in-tree files only),
  `reload_custom_tools()`, `reload_mcp_tools()`,
  `reload_api_backend(*, force=False)`.
- `reload_api_backend(*, force=False) -> bool` is the supported way to move **one**
  running agent to a different provider or API key, and it is the whole job: it takes a
  swap lock, refuses to touch a sub-agent pinned via `VAF_PROVIDER` or an embedded agent
  with `config_overrides`, builds the backend through `_build_api_backend` (so an embedded
  `api_key` reaches it), reattaches the structured event sink, drops the local stack on
  `local -> cloud` (`use_server=False`, `llm=None`, and `stop_server()` on **its own**
  `ServerManager`), resets the tokenizer and refreshes `model_display_name`. On a failed
  build it reports `False` and leaves the agent on its previous backend rather than
  stranding it with none. Returns `True` when the active backend actually changed.
  **It can only stop a server it owns.** In the desktop/tray product the `llama-server`
  child belongs to the tray's module-level `ServerManager` (`vaf/tray.py`), not to the
  agent, so `agent.server` is `None` there and the loaded GGUF stays resident across a
  provider switch (verified on a single running instance). A WEIGHT swap is different:
  `reload_local_model()` goes through `ensure_local_model`, which stops a foreign server
  by force (`force_external=True`) - the same guarantee the voice-model lane relies on.
  **Pass `force=True` when the provider may be unchanged
  but the key may have moved** - the default skips that case, which is why a config-reload
  that does not know which key changed must force. Do not reassign `provider`,
  `api_backend`, `use_server` or `llm` by hand; guarded by
  `tests/test_provider_swap_single_implementation.py`.
- **What it does NOT re-derive**, so a caller knows where the line sits. It swaps the
  API backend and nothing else: `prompt_manager` and the model name inside the system
  prompt are built by `init_chat()`; `context_manager` and the `n_ctx` it was sized
  with are built once in `__init__`; and `filename` / `repo_id` / `model_path` for a
  local GGUF are resolved at construction. So a **provider or API-model change is
  fully served** by this method, a **local model change is served by
  `reload_local_model()`** (below), and only **`n_ctx` still needs a new agent**.
  Calling `init_chat()` to "finish" a reload is a trap: it resets `history` to the
  system message, which is fine when the caller already discarded the agent and
  destroys the conversation when it did not. VAF's terminal app uses both reload
  methods and leaves `n_ctx` to a restart.
- `reload_local_model() -> bool` is the weight-swap counterpart: it re-resolves
  `filename`/`model_path` from the live config, makes the ONE llama server hold that
  GGUF (`ensure_local_model` - model-aware, blocking, stops a foreign server by
  force, and passes the configured `n_ctx`/`gpu_layers` so a swap cannot reset
  the user's sizing to the defaults), recomputes the identity the tool-call parser gates on
  (`model_display_name` / `is_gemma_local` / `model_mode`, via
  `_apply_local_model_identity`, the same single source `__init__` uses), rebuilds
  the system prompt through `init_chat()` and RE-ATTACHES the conversation tail, then
  re-points session persistence. It refuses (False, untouched) under a
  `VAF_MODEL_OVERRIDE` env pin, a non-local provider, or library mode (`self.llm`
  holds the weights in-process). A failed server start also returns False but keeps
  the fields at the new resolution: the config is the source of truth and the in-turn
  connection-retry relaunches from these fields. Guarded by
  `tests/test_reload_local_model.py`.
- `reload_all_api_backends(*, force=False) -> int` (module level in `vaf/core/agent.py`)
  is what a CONFIG CHANGE should call. It applies the above to every agent alive in the
  process and returns how many changed. **This sentence used to read "the only supported
  way" about the singular form, and that wording was the defect**: a process routinely
  holds several agents - the headless runner builds one per `parallel_main_workers` -
  so re-applying to the agent a code path happens to hold repaired one of them and left
  the rest on the previous key until a restart. Five call sites had each hand-rolled that
  same mistake. The broadcast needs no policy of its own: the `VAF_PROVIDER` pin and the
  `config_overrides` guard live inside the per-instance method, so it reaches exactly the
  agents meant to follow the file. It never raises - one agent refusing must not stop the
  rest from being repaired. Agents are held weakly; note that an Agent stays reachable for
  the life of the process anyway, because `__init__` registers an `atexit` handler.
  Deliberately NOT on the public facade: it declines for every agent built with config
  overrides, which is every agent an embedder builds the documented way. See
  `tests/test_api_backend_broadcast.py` and the boundary in `docs/EMBEDDING.md`.

## Concurrency contract

One `CoreAgent` is one conversation and is effectively **single-threaded**:
per-turn state lives in instance attributes, `history` and `tools` are
mutated without locks. Do not call `chat_step` on the same instance from two
threads. Multiple instances in one process (each on its own thread) are the
intended parallel pattern. Pass `register_signals=False` off the main thread
(the facade always does). The engine's `chat_step` is synchronous and blocks;
the facade offers `await agent.run_async(...)`, a thread-executor wrapper over
`run()` (not a natively async engine).

## Advanced identity/scoping attributes

The product harness sets these per session; an advanced embedder may set them
too (they are underscore-prefixed: subject to change, announced via
changelog):

| Attribute | Effect |
|---|---|
| `_current_username`, `_current_user_scope_id` | identity injected into tools, memory scoping |
| `_current_user_role` | `"admin"` unlocks `admin_only` tools |
| `_current_chat_source` | e.g. `"telegram"` - activates channel restrictions |
| `_background_run` | marks automation runs (suppresses UI pushes) |

The first three are written through `vaf/core/identity_binding.py`, never by hand:
`bind_identity(agent, identity)` writes all three unconditionally, so a field the new
turn does not carry clears the previous one, and `reassert_identity(agent, identity)`
writes only fields that carry a value, for the lanes whose caller identity outranks the
session's after `load_session_context`. The scope is stored as supplied and never coerced
to `uuid.UUID` - see [platform/UUID.md](platform/UUID.md) for why.

For multi-user servers, every scoped datum must key on the user scope - read
[USER_ISOLATION.md](security/USER_ISOLATION.md) before building on these.
