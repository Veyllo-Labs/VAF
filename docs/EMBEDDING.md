# Embedding VAF as a Library

VAF can be used as a headless agent **framework** — a foundation you build your
own application on, instead of writing the agent loop, tool dispatch, context
management and multi-provider LLM plumbing yourself. This page is the developer
contract for that use.

For the desktop/server product, see the main [README](../README.md).

---

## Install

The base install is intentionally slim — only what a headless agent needs. VAF is not
on PyPI yet, so install from source (this is also what `install.sh` uses):

```bash
git clone https://github.com/Veyllo-Labs/VAF.git && cd VAF
pip install -e .
```

This pulls the core runtime and the LLM provider SDKs (OpenAI, Anthropic,
Google) — but **not** the web server, desktop UI, embeddings stack, or chat
bridges. Add those only if you need them, via extras:

| Extra | Adds | For |
|---|---|---|
| `vaf[server]` | fastapi, uvicorn, websockets | the HTTP/WebSocket API |
| `vaf[desktop]` | pywebview, pystray, PySide6 | the desktop window / tray |
| `vaf[memory]` | sqlalchemy, pgvector, sentence-transformers, redis | long-term RAG memory |
| `vaf[speech]` | SpeechRecognition, pyaudio | offline speech-to-text |
| `vaf[browser]` | browser-use, playwright | browser automation tools |
| `vaf[pdf]` | pdfplumber, pytesseract, pdf2image | PDF extraction / OCR |
| `vaf[docs]` | python-docx, openpyxl, python-pptx | Office document tools |
| `vaf[discord]` / `vaf[telegram]` | chat bridges | messaging integrations |
| `vaf[all]` | everything above | parity with the full product |

```bash
pip install -e ".[memory,server]"     # mix and match
pip install -e ".[all]"               # everything (parity with the full product)
```

(Once VAF is published to PyPI, the same works as `pip install "vaf[memory,server]"`.)

Tools whose extra is not installed are not loaded at startup (they are
unavailable until you install the extra); the agent still runs.

---

## Quickstart

```python
from vaf import Agent

agent = Agent(config={"provider": "deepseek"})
answer = agent.run("In one short sentence, what is Python?")
print(answer)
```

`Agent` here is the stable façade. The full internal engine remains available as
`vaf.CoreAgent` (a.k.a. `vaf.core.agent.Agent`) for advanced use. Runnable
versions of this page's snippets live in [examples/](../examples/), including a
complete pip-installable custom-tool package.

The same code works with a local GGUF model — it is provider-agnostic:

```python
agent = Agent(config={"provider": "local"})   # downloads/starts a local model
print(agent.run("Hello!"))
```

In local mode the first `run()` may take a while: it downloads the model on
first use and readies the local backend - depending on the platform either
**one** llama server on `127.0.0.1:8080` (started or reused; the same
single-server rule as the full product) or an in-process llama-cpp load that
opens no port. Expect a multi-GB download and significant RAM/VRAM use; for a
quick first test, an API provider is the fastest path.

### Streaming

```python
agent = Agent(config={"provider": "anthropic"})
agent.run("Explain async/await.", on_token=lambda s: print(s, end="", flush=True))
```

`on_token` receives text deltas as they arrive. For reasoning models the deltas
may include the model's `<think>...</think>` block; the value returned by `run()`
is always the cleaned final answer.

### Stateful conversations

One `Agent` instance keeps one conversation — repeated `run()` calls continue
the same history. Create a new `Agent` for an independent conversation.

---

## Configuration

`config=` is a dict merged on top of `~/.vaf/config.json` for this instance only
— nothing is written to disk, so each `Agent` can carry its own settings. Common
keys (full reference in [CONFIG_SCHEMA.md](setup/CONFIG_SCHEMA.md)):

| Key | Default | Meaning |
|---|---|---|
| `provider` | `local` | `local`, `veyllo`, `openai`, `anthropic`, `google`, `deepseek`, `openrouter` |
| `model` | `auto` | local GGUF filename / repo, or an API model name |
| `api_key_<provider>` | — | API key, e.g. `api_key_deepseek` |
| `api_model_<provider>` | — | model per provider, e.g. `api_model_openai` |
| `n_ctx` | `32768` | context window (min 32768 for tool use) |
| `temperature` | `0.7` | sampling temperature |

```python
Agent(config={
    "provider": "openai",
    "api_key_openai": "sk-...",
    "api_model_openai": "gpt-4o",
})
```

The `api_key_*` and `api_model_*` you pass here reach the LLM backend directly for
this `Agent` instance. Pass the **raw** key (`"sk-..."`) — unlike the on-disk value
it is not Base64-decoded.

### A complete example, with error handling

The engine is built **lazily** on the first `run()` (or `.core`) call — so
configuration and connection problems surface there, not at `Agent(...)`. `run()`
returns the final answer as a string. Most misconfigurations and unreachable-provider
errors **raise** — so wrap the call. Some *handled* failures (e.g. the API returning
empty responses repeatedly) are caught internally and come back as a short status
string instead of raising:

```python
from vaf import Agent

agent = Agent(config={
    "provider": "openai",
    "api_key_openai": "sk-...",          # raw key
    "api_model_openai": "gpt-4o-mini",
})

try:
    answer = agent.run("In one sentence, what is Python?")
    print(answer)
except ValueError as e:
    # configuration problem — e.g. a missing/empty API key for the chosen
    # provider (when no local fallback applies)
    print("config error:", e)
except Exception as e:
    # runtime/provider failure — unreachable endpoint, network timeout,
    # unknown model, ...
    print("run failed:", e)
```

Notes:

- **Lazy init.** `Agent(config=...)` is cheap; the core engine and the provider
  connection are created on first use. Put your `try/except` around `run()` (or
  around the first `.core` access).
- **Gated tools never hang or raise.** Under the embedded default
  `VAF_NONINTERACTIVE=1`, a tool behind the confirmation gate returns an error
  *string* in its result instead of blocking on a human — the run continues and
  the final answer explains what was refused. Grant specific tools via the trust
  mechanisms (below) to let them run unattended.
- **Handled failures may return a string, not raise.** After exhausting its
  internal retries (e.g. a provider returning empty responses), `run()` returns a
  short status string — currently prefixed `[SYSTEM_LOG_ONLY]` — rather than
  raising. If you must distinguish a real answer from such a handled failure,
  check the returned string for that marker.
- **Streaming + errors.** An exception during a streamed run can arrive after
  some `on_token` deltas were already delivered; treat partial output as
  non-final until `run()` returns.

---

## Headless safety: tool confirmation

VAF gates dangerous tools (shell, file writes, unsandboxed Python) behind a
confirmation prompt. An embedded library must never block waiting for a human,
so the façade sets `VAF_NONINTERACTIVE=1` by default: gated tools return an
error instead of hanging. To opt out, set `VAF_NONINTERACTIVE=0` before
constructing the agent.

To let specific dangerous tools run unattended, use the trust mechanisms instead
of disabling the gate:

- mark a working directory trusted (`mark_trusted_dir`),
- set a per-tool policy to allow (`set_tool_policy`),
- both persist in `trust.json` under the platform config dir
  (Linux `~/.config/vaf/`, macOS `~/Library/Application Support/vaf/`,
  Windows `%APPDATA%/vaf/`) - per OS user across all projects, not per
  project.

Two semantics worth knowing before you grant anything:

- `mark_trusted_dir(path)` trusts that directory **and its entire subtree**
  for **all** gated tools; the check runs against the host process's current
  working directory at tool-call time.
- The interactive "always allow" choice does both at once: it trusts the
  current working directory *and* sets the tool's policy to allow.

---

## Security posture

What an embedded agent can and cannot do on the host - the short version of
[SANDBOXING.md](security/SANDBOXING.md), from the embedder's perspective:

- **Code execution needs Docker.** `python_sandbox` (and the test runner)
  refuse to run without a working Docker daemon - there is deliberately **no
  fallback to host execution**. Without Docker the tool returns a
  `[SECURITY] Sandbox requires Docker: ...` error string and the run
  continues. The coder sub-agent's shell needs bubblewrap or Docker and
  refuses otherwise.
- **Host execution is opt-in.** `python_exec` (unsandboxed Python on the
  host) additionally requires a persisted `set_tool_policy("python_exec",
  "allow")` - a one-off interactive confirmation is not enough. File tools
  write to the host as the gate allows.
- **Network posture.** `import vaf` and `Agent(config=...)` open no ports and
  start no services. An API provider means outbound HTTPS only. Local mode
  either starts the one llama server on `127.0.0.1:8080` or loads the model
  in-process (platform-dependent) - never anything on a public interface.
  The memory/RAG stack
  (PostgreSQL, Redis) is **not** started by the library - it is a Docker
  compose stack the desktop/server product manages; without it, memory tools
  fail soft with an error string. One exception to know:
  `python_sandbox(with_vaf_tools=True)` opens a temporary tool-bridge port on
  `0.0.0.0` (random ephemeral port, per-run token auth) for the duration of
  that call.
- **Admin-only tools stay off - but a bare agent still acts as the machine
  owner.** Without `user_scope`, an embedded agent has no admin identity
  (`admin_only` tools are blocked), yet in local mode its memory tools
  operate on the local admin's memory bucket and file tools get the
  no-scope jail exemption (home-wide access as the gate allows). That is
  the intended single-tenant default: embedding VAF on your own machine
  means acting as yourself. For anything multi-tenant, read the next
  section.

---

## Multi-tenant embedding: `user_scope`

To serve multiple end users from your application, tell each `Agent`
instance whose conversation it is:

```python
agent = Agent(config={"provider": "deepseek"}, user_scope="6f9619ff-8b86-d011-b42d-00c04fc964ff")
```

What it does, and the trust model:

- `user_scope` is an **assertion by you, the embedder** - the library
  performs no authentication. The process boundary is the trust boundary
  (an in-process caller could set engine attributes directly anyway); your
  application must authenticate its users before asserting their scope.
- The value is validated as a UUID at construction and raises `ValueError`
  otherwise - a bad scope fails loudly instead of silently operating on the
  machine owner's data.
- Scope and username travel together: VAF resolves the account username for
  the scope itself (a synthetic per-scope name when unknown) and never
  falls back to the admin identity. The identity is bound before the system
  prompt is built and re-asserted on every `run()`.
- Memory, reminders, per-user files (speaker profiles, browser sessions,
  scope-keyed stores) then key on that scope, with the same fail-closed
  filters the product server uses.

Hard limits you must respect (they are architecture, not fine print):

- **One tenant per process.** Two differently-scoped `Agent` instances in
  one process share process-global state (environment variables written
  during tool calls, singletons, the working-context fallback directory).
  Run one OS process per tenant.
- **trust.json is per OS user, machine-global**: a `set_tool_policy(...,
  "allow")` or trusted directory granted while serving one tenant arms that
  permission for every tenant on the machine.
- **The on-disk config is shared**: `Config`-routed settings (not passed
  via `config=`) are the same for all tenants.
- **Do not rely on database-level isolation**: the memory DB's row-level
  security is not an independent backstop yet ([USER_ISOLATION.md](security/USER_ISOLATION.md));
  the app-side fail-closed filters are the active enforcement.
- **Custom tools receive no scope automatically**: a tool you register via
  `add_tool()` that touches per-user data must accept and honor a
  `user_scope_id` argument passed by your own code; the engine's automatic
  injection covers only its built-in tools.
- Passing the local admin's scope id IS full admin (tools and files) - hand
  it out deliberately or never.

---

## Writing a tool

A tool is a `BaseTool` subclass. Four ways to register one: per Agent
instance via `agent.add_tool(tool)` (below), as a pip package (next section),
via the update-surviving `custom_tools/` folder (see "More extension points"),
or in-tree in `vaf/tools/` for contributions. Full contract and examples in
[vaf/tools/base.py](../vaf/tools/base.py) and [vaf/tools/README.md](../vaf/tools/README.md).

```python
from vaf.tools.base import BaseTool

class WeatherTool(BaseTool):
    name = "get_weather"
    description = "Return the current weather for a city."
    permission_level = "read"          # read | write | dangerous | system
    side_effect_class = "none"         # none | reversible | irreversible
    parameters = {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    }

    def run(self, **kwargs) -> str:
        city = kwargs["city"]
        return f"It is sunny in {city}."
```

To give ONE embedded Agent instance this tool - no package, no file drop-in:

```python
agent = Agent(config={"provider": "deepseek"})
agent.add_tool(WeatherTool())   # before the first run()/.core access
print(agent.run("What's the weather in Berlin?"))
```

`add_tool()` must run before the engine is built (it raises `RuntimeError`
afterwards); a tool with an existing name wins (last write). Runnable version:
[examples/04_inline_tool.py](../examples/04_inline_tool.py).

Key declarative rules the runtime enforces:

- `permission_level` - `dangerous` triggers the confirmation gate; `system`
  marks internal plumbing tools and explicitly **bypasses** it; `read`/`write`
  run without confirmation (except a legacy by-name gate on the risky
  built-ins `move_file`, `bash`, `run_command`, `python_exec`).
- `side_effect_class` — surfaced to the model so it knows what is reversible.
- `admin_only`, `channel_restrictions`, `coder_only` — visibility/scoping.

---

## Shipping tools as a pip package (entry points)

Third-party packages can extend VAF without touching its source, via the
`vaf.tools` entry-point group. In your package's `setup.py`:

```python
setup(
    name="vaf-weather",
    # ...
    entry_points={
        "vaf.tools": [
            "get_weather = vaf_weather.tools:WeatherTool",
        ],
    },
)
```

or in `pyproject.toml`:

```toml
[project.entry-points."vaf.tools"]
get_weather = "vaf_weather.tools:WeatherTool"
```

Each entry point must resolve to a `BaseTool` subclass. After
`pip install vaf-weather`, the tool is discovered automatically at agent startup
(a broken package logs an error and is skipped — it never breaks startup).

---

## Observability, logging, and the engine reference

Three companion pages cover the operational side of embedding:

- [OBSERVABILITY.md](OBSERVABILITY.md) - streaming vs structured events:
  `on_token` caveats, `CoreAgent.set_event_sink()` and its event schema, and
  the `vaf prompt --output-format stream-json` NDJSON interface for
  integrating VAF as a subprocess from any language.
- [DEBUGGING.md](DEBUGGING.md) - where an embedded agent writes log files,
  how to redirect them (`VAF_LOG_DIR`), what `debug_logs_enabled` does (and
  does not) silence, and how to read a session JSON. Note: an embedded agent
  **does write log files by default**; set `VAF_LOG_DIR` if you care where.
- [CORE_AGENT.md](CORE_AGENT.md) - the `vaf.CoreAgent` reference: constructor,
  lifecycle, the `chat_step`/`execute_tool` contracts, and the concurrency
  rules.

**Concurrency contract (short version):** one `Agent` is one conversation and
is effectively single-threaded - drive it from one thread at a time. For
parallelism, create multiple `Agent` instances (each in its own thread is
fine). There is no async API today; `run()` blocks.

### More extension points

Beyond tools, the product loads three other user-extensible artifact kinds -
usable from an embedded engine too, documented in their own pages:

- **Custom workflows** - Python files with a module-level `WORKFLOW` dict,
  dropped in `~/.vaf/workflows/`, see
  [WORKFLOW_SELECTION.md](agents/WORKFLOW_SELECTION.md).
- **Skills** - reusable prompt/procedure packages, see [SKILLS.md](agents/SKILLS.md).
- **MCP servers** - register external MCP tool servers in `mcp_servers.json`;
  their tools appear as native tools (`mcp_<server>_<tool>`), see
  [MCP_INTEGRATION.md](agents/MCP_INTEGRATION.md).
- **Update-surviving local tools** - a `custom_tools/` folder in the platform
  data dir (managed via the Web UI, admin-only), see
  [vaf/tools/README.md](../vaf/tools/README.md).

### A note on custom OpenAI-compatible endpoints

The config key `local_api_url` points VAF's *API-backend consumers* (browser
agent, local vision, cloud-to-local failover) at any OpenAI-compatible server
(Ollama, vLLM, LM Studio). It does **not** redirect the main chat loop: with
`provider="local"` the main agent always manages its own llama server on
`127.0.0.1:8080`. Embedding VAF's main loop on top of a foreign inference
server is not supported today. See
[PROVIDER_MODES.md](llm/PROVIDER_MODES.md) for the details.

---

## What is and isn't stable

Stable public surface (safe to build on):

- `from vaf import Agent` - the façade: `Agent(config=...)`, `.run(prompt, on_token=...)`, `.add_tool(tool)`, `.core`.
- `vaf.CoreAgent` - the engine, for advanced embedding.
- `BaseTool` - the tool contract.
- The `vaf.tools` entry-point group.

Everything else under `vaf.core.*` is internal and may change between releases.
