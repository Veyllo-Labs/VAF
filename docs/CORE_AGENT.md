# CoreAgent Reference

`vaf.CoreAgent` (the engine class `vaf.core.agent.Agent`, ~11k lines) is the
advanced embedding surface behind the `vaf.Agent` facade. This page is the
reference for the parts an embedder can rely on; start with
[EMBEDDING.md](EMBEDDING.md) and use the facade unless you need engine-level
control. Design map of the turn loop: [AGENT_LOOP.md](agents/AGENT_LOOP.md).

Stability: `CoreAgent` is part of the declared stable surface at the level
documented HERE (constructor, lifecycle, `chat_step`, `execute_tool`,
`set_event_sink`, the accessors below). Underscore-prefixed attributes are
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
  model, restart the server (or process) rather than relying on
  `load_model()` to swap it.
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
injection (identity, session, workspace), and bounded execution with per-tool
timeouts and stop polling. Event schema: the
[event sink](OBSERVABILITY.md). Always returns a string (tool result or
error text).

## Observability and accessors

- `set_event_sink(callable)` - structured `tool_start`/`tool_end`/
  `gate_required`/`gate_decision` events; full schema in
  [OBSERVABILITY.md](OBSERVABILITY.md).
- `get_token_usage() -> (used, max)` - provider-appropriate context usage.
- `history` - the OpenAI-style message list (the system prompt is
  `history[0]`).
- `get_live_session_subagents()` - session-scoped, heartbeat-verified list of
  running sub-agents (use this, never process-global state).
- `load_session_context(session_id)` - swap the agent onto a persisted
  session: rebinds identity from session metadata, re-inits the prompt, and
  replays messages preserving tool-call linkage.
- Hot reload: `reload_builtin_tools()` (new in-tree files only),
  `reload_custom_tools()`, `reload_mcp_tools()`, `reload_api_backend()`.

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

For multi-user servers, every scoped datum must key on the user scope - read
[USER_ISOLATION.md](security/USER_ISOLATION.md) before building on these.
