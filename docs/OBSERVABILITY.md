# Observability: Streaming, Events, and Machine-Readable Output

How to watch what an embedded or scripted VAF agent is doing. VAF exposes two
independent channels per run, plus an NDJSON CLI mode that combines both for
non-Python integrations.

| Channel | Carries | How to attach |
|---|---|---|
| Text stream | the model's output as it is generated | `Agent.run(prompt, on_token=...)` or `chat_step(..., stream_callback=...)` |
| Event sink | structured tool/gate events (dicts) | `CoreAgent.set_event_sink(callable)` |

Audience: developers embedding VAF ([EMBEDDING.md](EMBEDDING.md)) or driving it
as a subprocess. The Web UI uses its own WebSocket channel (see
[WEBUI_WEBSOCKET_FLOW.md](web-ui/WEBUI_WEBSOCKET_FLOW.md)); the event sink is
`None` in web sessions.

---

## The text stream

`on_token` (facade) / `stream_callback` (engine) is called with raw text
deltas on the thread that runs the turn. Expect more than prose:

- Reasoning models: explicit reasoning is bracketed with synthetic
  `<think>` / `</think>` markers; inline `<think>` tags from the model are
  passed through as-is.
- Status strings are injected into the stream: error texts such as
  `[Error] API backend failure: ...`, and `\n\n[Generation stopped by user]`
  when a run is stopped.
- A trailing `\n` follows each generation.

Treat streamed text as non-final until the call returns: the facade's `run()`
returns the cleaned final answer (reasoning stripped) regardless of what was
streamed. If you drive `chat_step` directly, read
[CORE_AGENT.md](CORE_AGENT.md) first - its return value is a status/placeholder
in several normal cases and the real answer must be taken from the stream or
from `agent.history[-1]`.

## The event sink

```python
agent = Agent(config={"provider": "deepseek"})
agent.on_event(lambda evt: print("EVENT", evt))   # facade shortcut
agent.run("List the files in this folder.")
```

(`agent.on_event(cb)` is the facade shortcut for `agent.core.set_event_sink(cb)`;
it can be called before the first run and survives backend swaps.)

Contract (all verified against `vaf/core/agent.py`):

- The sink is a callable taking one plain `dict`. Return value is ignored.
- Events fire synchronously on the thread executing the tool call, in strict
  order per call: `[gate_required -> gate_decision] -> tool_start -> tool_end`.
- A raising sink is swallowed (`try/except`): a broken consumer can never
  break the run.
- Tool execution and (on API providers) LLM calls emit sink events. There are
  currently **no** events for context compression (Web UI pushes / log files
  only), and `tool_end` carries no result payload.

### Event types

| type | Fields | Notes |
|---|---|---|
| `tool_start` | `tool`, `args` | `args` is sanitized best-effort: heavy fields such as `content`/`code` (and `command` for the `bash` tool) are replaced by `<field>_len`, `<field>_sha256`, `<field>_preview`. Not exhaustive: the `multi_tool_use.parallel` wrapper's own `tool_start` carries raw args, and other field names pass through - treat args as potentially sensitive |
| `tool_end` | `tool`, `duration_ms`, `ok` | normally closes every `tool_start`, including on tool error (the error is returned as the result string); rare early-return branches can leave a `tool_start` unclosed, so do not block forever waiting for the pair. `ok` is DISPATCH-level (False = the tool raised or the name was unknown), not the semantic success of the tool's output |
| `gate_required` | `tool`, `cwd`, `reason`, `args_preview` (max 300 chars) | a confirmation-gated tool was hit; in non-interactive mode the tool then returns an `[ERROR] ... requires confirmation ...` string and **no** `gate_decision` follows |
| `gate_decision` | `tool`, `decision`: `allow_once` \| `allow_always` \| `cancel` | interactive runs only |
| `llm_start` | `provider`, `model` | one per chat-completion call on API providers (`model` may be None before default resolution). Note: INTERNAL engine calls (context compression, routers, title generation) also emit llm pairs - do not assume one pair per visible turn |
| `llm_end` | `provider`, `model`, `duration_ms`, `ok`, `usage` | closes every `llm_start`; `ok` False = errored OR abandoned before completion (e.g. user stop); `usage` is a best-effort snapshot of the serving provider's last request (may lag one call behind when failover served the response). Local-server and in-process lanes do not emit llm events yet |

Two shapes to handle defensively:

- `multi_tool_use.parallel` emits its own `tool_start`/`tool_end` pair, and
  every inner tool emits its own full sequence in between - so pairs nest.
- Consume events with a stack (or by correlation), not by assuming a flat
  strictly-alternating sequence.

Cancelled gates emit no `tool_start`/`tool_end` for the cancelled call, and
hard policy blocks (`Security Error: ...`) emit nothing at all.

---

## `vaf prompt`: scripting and subprocess integration

One-shot, non-interactive turns from the CLI. This is the integration surface
for non-Python applications: spawn the process, parse stdout.

```bash
vaf prompt -p "Summarize the README" --output-format stream-json
```

Flags:

| Flag | Default | Meaning |
|---|---|---|
| `--prompt` / `-p` | required | the user message |
| `--output-format` | `text` | `text` \| `json` \| `stream-json` |
| `--session` / `-s` | none | load an existing session's history first (a missing id silently starts fresh) |
| `--save-session` | off | save prompt + answer as a **new** session (new id; ignored in `text` mode) |

`vaf prompt` force-sets `VAF_NONINTERACTIVE=1`: gated tools return error
strings, nothing blocks on stdin. It uses the same `~/.vaf/config.json` as the
rest of VAF (env overrides `VAF_PROVIDER` / `VAF_MODEL_OVERRIDE` apply), and it
is cwd-sensitive: a `VAF.md` found from the current directory upward (nearest
parent wins; `.vaf/VAF.md` works too) is loaded as project context. The alias `vaf run prompt` is equivalent but additionally binds the
local-admin identity (memory/RAG then read and write the same scope the Web UI
uses) and silences HTTP client logs.

### Output formats

**`text`**: the final answer on stdout. Interactive status lines are NOT
silenced in this mode (they are in the machine formats), so other output may
surround the answer. Exit 0.

**`json`**: one object on stdout:

```json
{"ok": true, "output": "<final answer>"}
```

Honest caveat: `ok` is currently always `true` and the exit code is 0 even
when the turn failed internally - handled failures come back as text like
`[Error] API backend failure: ...` inside `output`. Inspect the text; do not
rely on `ok` for error detection.

**`stream-json`**: NDJSON on stdout - one JSON object per line, flushed per
event. Event order:

1. `{"type": "start"}`
2. interleaved, during the turn:
   - `{"type": "text_delta", "text": "..."}` for every stream chunk (including
     `<think>` markers and injected status strings, see above)
   - the four sink events (`tool_start`, `tool_end`, `gate_required`,
     `gate_decision`) exactly as specified in the table above
3. `{"type": "session_saved", "id": "<sessionid>"}` (only with `--save-session`)
4. `{"type": "end"}`

There is no final aggregated-result event: concatenate the `text_delta` texts
(and strip `<think>...</think>` blocks) to reconstruct the answer.

Constructed example (a run that lists a directory):

```json
{"type": "start"}
{"type": "text_delta", "text": "I'll check the folder."}
{"type": "tool_start", "tool": "list_files", "args": {"path": "."}}
{"type": "tool_end", "tool": "list_files"}
{"type": "text_delta", "text": "It contains three files: ..."}
{"type": "text_delta", "text": "\n"}
{"type": "end"}
```

### stdout, stderr, exit codes

- NDJSON lines, the `json` payload, and the `text` answer go to **stdout**.
  Usage errors (missing `--prompt`, bad `--output-format`) go to stderr with
  exit code 2. Unhandled exceptions print a traceback to stderr and exit
  non-zero.
- In `json`/`stream-json` mode the common UI printers are silenced, but not
  every residual line is guaranteed suppressed; a strict consumer should
  ignore stdout lines that do not parse as JSON.

---

## What this surface does not cover (today)

Known limitations, so you do not go looking for events that do not exist:

- No run/span ids; llm events cover API providers only (the local llama-server
  and in-process lanes emit none yet).
- No cost-in-currency events; `llm_end.usage` carries token counts, and
  `get_token_usage()` in [CORE_AGENT.md](CORE_AGENT.md) remains the polling
  accessor for context fill.
- `tool_end` has no result payload - correlate results from the final answer
  or the [debug logs](DEBUGGING.md) (`tool_use_*.log`, sub-agent
  `events.jsonl`).

Changes to the event schema are announced in [CHANGELOG.md](../CHANGELOG.md)
per the backward-compatibility rules in [RELEASING.md](setup/RELEASING.md).
