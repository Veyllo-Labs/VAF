# Embedding VAF as a Library

VAF can be used as a headless agent **framework** — a foundation you build your
own application on, instead of writing the agent loop, tool dispatch, context
management and multi-provider LLM plumbing yourself. This page is the developer
contract for that use.

For the desktop/server product, see the main [README](../README.md).

---

## Install

The base install is intentionally slim - only what a headless agent needs:

```bash
pip install --pre vaf
```

(`--pre` because VAF is currently an alpha prerelease; the flag becomes
unnecessary with the first stable release. To work from source instead - this
is also what `install.sh` uses:
`git clone https://github.com/Veyllo-Labs/VAF.git && cd VAF && pip install -e .`)

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
pip install --pre "vaf[memory,server]"    # mix and match
pip install --pre "vaf[all]"              # everything (parity with the full product)
```

(From a source checkout the same works as `pip install -e ".[memory,server]"`.)

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

### Persistent conversations

```python
agent = Agent(config={"provider": "deepseek"})
agent.run("My name is Lisa.")
sid = agent.save_session()          # persist; returns the session id

# ... process restarts ...
agent = Agent(config={"provider": "deepseek"}, session=sid)
agent.run("What's my name?")        # remembers
```

`save_session()` writes into VAF's standard session store (`~/.vaf/sessions/`)
and is idempotent: later calls update the same session, so saving after every
turn is fine. An unknown `session=` id raises `ValueError` at construction; with
`user_scope` set, resuming another tenant's session is refused (legacy
sessions without an owner are resumable and get stamped with your scope on
the next save). Runnable version:
[examples/05_chatbot_with_memory.py](../examples/05_chatbot_with_memory.py).

### Async applications

`await agent.run_async(prompt, on_token=...)` runs the turn in a worker
thread so your event loop stays responsive - an honest thread-executor
wrapper, not native async. `on_token` fires on the worker thread; hop back to
the loop before touching loop-bound state. One instance still means one
conversation: never run two turns on the same Agent concurrently.

### Special return values: `vaf.markers`

Handled failures and control-flow outcomes come back as strings inside the
normal return channel. Compare against the constants in `vaf.markers`
(`SYSTEM_LOG_ONLY`, `GENERATION_STOPPED`, `LOOP_PROTECTION`, `ASYNC_ACK`,
`TOOL_CONFIRMATION_REQUIRED`) instead of copying string literals from the
docs - a CI guard keeps the constants in sync with the engine.

---

## Setting the persona (system prompt)

Pass `system_prompt=` to give the agent its identity and instructions:

```python
from vaf import Agent

agent = Agent(
    config={"provider": "openai", "api_key_openai": "sk-..."},
    system_prompt=(
        "You are Aria, a terse code-review assistant. "
        "Answer in short, direct bullet points. Never apologise."
    ),
)
print(agent.run("Review this: def add(a, b): return a - b"))
```

`system_prompt` replaces VAF's built-in on-disk persona (the desktop product's
"Soul") for THIS agent only; nothing is written to disk. The engine's own
technical instructions (the `<think>` format, action verification, working-
memory hygiene) are always kept, so tools and streaming behave normally - you
are setting the personality and task framing, not rewiring the engine.

When you omit `system_prompt`, the agent uses the machine's Soul file
(`~/.vaf/users/admin/soul.md`); if none exists yet, VAF materializes its
default persona there on first use (a neutral built-in identity is only the
last resort when that load fails). For an embedded app you almost always want
to set `system_prompt` explicitly, so your agent's voice does not depend on
machine-local state.

---

## Choosing a backend: local vs API

Every `Agent` needs a model backend; the `provider` key picks it. Both lanes
drive the exact same agent, tools and API surface - switching is a config
change, not a code change.

**Local (`provider="local"`, the default)** - VAF manages its own llama.cpp
server (exactly one per machine, on `127.0.0.1:8080`) and auto-picks a GGUF
model for your hardware (`model="auto"`; override with a filename or repo).

- No API key, no per-token cost, prompts never leave the machine.
- Needs hardware: a multi-GB model download on first run and roughly 3-18 GB
  of RAM/VRAM depending on the model tier; tool use needs the 32k-token
  context floor.
- One server per machine, never two concurrent inferences - many *parallel*
  agents on one box want an API provider instead.
- Quality: small local models handle chat and light tool use well; long
  agentic runs and the coder sub-agent are noticeably stronger on API models.

**API (`provider="veyllo" | "openai" | "anthropic" | "google" | "deepseek" |
"openrouter"`)** - per-request HTTPS to the provider, no local model at all.

- Zero VRAM, fast first start, and parallel `Agent` instances scale freely
  (each request is independent).
- Needs a key: pass `api_key_<provider>` (raw, e.g. `"sk-..."`) and
  optionally `api_model_<provider>` per instance via `config=`; your prompts
  are sent to that provider.
- `veyllo` is Veyllo's own hosted OpenAI-compatible endpoint (multimodal
  `veyllo-chat`); `openrouter` fans out to many third-party models behind
  one key.

| You want | Pick |
|---|---|
| Privacy / offline / no per-token cost | `local` |
| Strongest quality, parallel load, or weak hardware | an API provider |
| Mixed: private by default, strong when needed | both - each `Agent` chooses independently |

```python
private_agent = Agent(config={"provider": "local"})
power_agent = Agent(config={
    "provider": "anthropic",
    "api_key_anthropic": "sk-ant-...",
})
```

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

VAF gates its dangerous tools behind a confirmation prompt: anything declaring
`permission_level = "dangerous"`, plus the by-name legacy set `bash`,
`run_command`, `move_file` and `python_exec`. Ordinary file writes are **not**
among them - `write_file` and `edit_file` are `"write"`, and what confines them
is the per-user jail, not the gate. An embedded library must never block waiting for a human,
so the façade sets `VAF_NONINTERACTIVE=1` by default: gated tools return an
error instead of hanging. To opt out, set `VAF_NONINTERACTIVE=0` before
constructing the agent.

To decide per call - which tool, which user, which arguments - use
`set_tool_authorizer` (below): `req.allow()` lets one call past the question
without granting anything durable, and `req.ask()` insists on the question even
where a standing grant would have skipped it.

To let specific dangerous tools run unattended for good, use the trust mechanisms
instead of disabling the gate:

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

## Sub-agents as a library

VAF's heavy sub-agents (`coding_agent`, `research_agent`, `document_agent`,
`librarian_agent`, `browser_agent`) are tools the model can call, but they are
built for the full product and only partially apply in a bare library process.
What to expect:

- **Inline execution works.** In a headless library there is no terminal to
  spawn and no web server to stream to, so a sub-agent call runs INLINE, in the
  same process, and returns its result to your `run()` turn. This is the
  supported library path and needs nothing extra.
- **The product's async/windowed modes do not apply.** In the desktop/server
  product a sub-agent can open its own terminal window or a child process that
  streams back to the web server; those paths need that infrastructure (a
  display plus `vaf` on PATH, or the running web server). On a headless host the
  spawn simply fails and falls back to inline, so leave
  `sub_agents_in_separate_terminals` at its default.
- **The coder needs a sandbox.** The coding agent's shell needs bubblewrap or
  Docker; its test runner and `python_sandbox` need Docker specifically (see
  [Security posture](#security-posture)). Without them, those steps return an
  error string and the run continues.
- **Memory/research depth needs services.** `research_agent` and RAG-backed
  work reach for the memory stack (PostgreSQL + Redis); without it they fail
  soft. Install `vaf[memory]` and run that stack if you need it.

Rule of thumb for a library embedding: prefer your own tools (`add_tool`) on
the main agent; treat sub-agents as a bonus that works inline and gets richer
once you add the product's services.

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
- **A tool that touches per-user data must DECLARE what it needs.** The dispatcher
  used to hand identity out from a hardcoded list of its own tool names, so a tool
  you registered could never receive one. It now reads a declaration, and yours is
  read exactly like a built-in one:

  ```python
  from vaf import BaseTool, user_jail

  class TenantNotes(BaseTool):
      name = "tenant_notes"
      description = "Read the calling user's notes."
      parameters = {"type": "object", "properties": {"path": {"type": "string"}}}

      # Ask for exactly what you consume. Valid keys: "user_scope_id",
      # "username", "user_role". Declaring nothing means receiving nothing.
      identity_kwargs = ("user_scope_id", "user_role")

      def run(self, **kwargs):
          scope = kwargs.get("user_scope_id")     # assigned by the dispatcher,
          role = kwargs.get("user_role")          # never taken from the model
          # Declaring tells you WHO is calling. It does not confine anything by
          # itself - for file access, turn the answer into a boundary. Enter the
          # jail INSIDE run(): your tool can also be called directly, with no
          # dispatcher to have set anything up for it.
          with user_jail(scope, role, mode="read"):
              return read_notes(kwargs.get("path"))
  ```

  Two properties worth relying on. The values are **assigned, never defaulted**: the
  arguments reaching `run()` start out as whatever the model produced, so a
  prompt-injected `user_role="admin"` is overwritten with the session's real role
  rather than honored. And a tool that declares nothing gets nothing - the safe
  direction, so forgetting the declaration cannot hand a tool an identity by accident.

  One caveat, stated because you will see it if you read the built-ins. A handful of
  them are also handed the live agent object as an `_agent` kwarg, and anything holding
  that object can read the caller's scope, name and role straight off it - the timer
  tools do exactly that. This is chat-lane plumbing for tools that need the running
  session, **not** a second supported way to learn who is calling: the dispatcher hands
  `_agent` to a fixed set of built-in NAMES, there is no declaration for it, and a tool
  you register never receives it. Do not reach for it; it may disappear without a
  major version. `identity_kwargs` is the surface that is kept.
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

## Running a tool yourself: `ToolCaller`

Sometimes you want a tool run, not a conversation: a job queue, a scheduled
task, your own agent loop with a different planner. Reaching into the engine for
that would mean rebuilding the parts that make a tool call safe, and rebuilding
them is how they come to disagree.

`ToolCaller` is that path, and it is **the same object the agent's own dispatch
uses** - there is no separate implementation for embedders:

```python
from vaf import BaseTool, ToolCaller

caller = ToolCaller(
    {"tenant_notes": TenantNotes()},        # your registry: {name: instance}
    user_scope_id="6f9619ff-8b86-d011-b42d-00c04fc964ff",
    username="alice",
    user_role="user",
)
print(caller.execute("tenant_notes", {"path": "todo.md"}))
```

What one `execute()` does, in order: evaluate policy (`admin_only`,
`channel_restrictions`), consult the confirmation gate, emit `tool_start`,
validate and repair the arguments against the tool's schema, assign the declared
identity, run the tool under a timeout with stop polling, emit `tool_end`, and
truncate. Same order, same rules, same event schema as a chat turn - the
ordering is pinned by a measurement, not by convention.

It never raises for a tool failure and never blocks on a human. Everything comes
back as a string: `Security Error: ...` for a policy block, `Tool Error: ...`
for a schema failure or an exception inside the tool, and the
`vaf.markers.TOOL_CONFIRMATION_REQUIRED` marker when a gated tool had nobody to
ask. A hard block emits **no events at all**, so an observer never sees a
blocked tool reported as having run.

The supported arguments:

| Argument | What it is for |
|---|---|
| `tools` | Your registry, `{name: BaseTool instance}`. Positional. |
| `user_scope_id`, `username`, `user_role` | Who is calling. Assigned into whatever the tool declares in `identity_kwargs`, overwriting anything a model put there. **Pass `username` if you serve more than one tenant**: it falls back to `"admin"`, the machine owner, because the tokenless desktop and the CLI have no username and the stores keyed on it treat that as the owner. Scope and role without a username therefore run every tenant against the owner's username-keyed data. |
| `source`, `session_id` | Where the call comes from. Feeds `channel_restrictions`; leave them out if you have no messaging channels. |
| `interactive`, `decide` | Set `interactive=True` and pass `decide(tool_name, reason) -> "allow_once" \| "allow_always" \| "cancel"` to plug your own confirmation UI into the gate. Left out, gated tools are refused rather than run. |
| `trust_dir` | Which directory a standing grant applies to. Defaults to the process's current one. |
| `timeout_for` | `f(tool_name) -> seconds`, for your own timeout policy. Defaults to the configured agent timeout. |
| `stop_check` | `f() -> bool`, polled during the run so you can cancel from outside. |
| `max_result_chars` | Result cut, `2000` like a chat turn. Pass `None` to switch it off - do that when you chain a result into something else, because a cut result can lose a trailing marker. |
| `authorize` | Your per-call decision hook, exactly as in the next section. `ToolCaller(..., authorize=fn)` is the same thing `Agent.set_tool_authorizer(fn)` installs. |
| `on_event` | `f(dict)` for `tool_start` / `tool_end` / gate events. Same schema as `Agent.on_event` (`CoreAgent.set_event_sink`), documented in [OBSERVABILITY.md](OBSERVABILITY.md). A raising sink is swallowed: a broken observer must not fail a run. |

Two limits, stated plainly:

- **It is a dispatcher, not an agent.** No model, no history, no planning, and
  none of the chat-turn machinery (plan gate, session workspace, router
  bookkeeping). If you want those, you want `Agent`.
- **The constructor takes further arguments that are not part of this
  contract.** They exist for VAF's own lanes and may change without a major
  version; the table above is what is kept.

---

## Deciding about a tool call: `set_tool_authorizer`

VAF answers three questions before a tool runs: may this caller use it at all
(`admin_only`, `channel_restrictions`), does a person have to confirm it
(`permission_level`), and who is calling (`identity_kwargs`). All three are
answered from what the *tool* declares. Your application knows things the tool
cannot: which tenant is on which plan, which paths this customer owns, that this
account is thirty seconds from its quota.

`set_tool_authorizer` is where you say so. It runs on every call that got past
policy, on both the agent and a bare `ToolCaller`:

```python
def authorize(req):
    if req.tool_name == "bash" and req.user_role != "admin":
        req.deny("shell access is not part of this plan")
    elif req.tool_name == "write_file" and not owns(req.user_scope_id, req.args.get("path")):
        req.deny("that file belongs to another tenant")
    elif req.side_effect_class == "irreversible":
        req.ask("this cannot be undone")

agent = Agent(config={"provider": "deepseek"})
agent.set_tool_authorizer(authorize)
```

**You answer with a method, not a return value.** A callback that returns
nothing would force `None` to mean something, and the tempting meaning ("no
objection") turns every forgotten `return` into an approval. Here, saying nothing
means having no opinion and the call proceeds exactly as it would without you.

| Method | What happens |
|---|---|
| `req.deny(reason)` | The call is refused with `Security Error: <reason>`. Nothing runs, and nothing is emitted, so an observer never sees a refused call reported as one that ran. |
| `req.ask(reason)` | The confirmation gate is put to a person, **even where a standing grant would normally skip it** - a trusted directory or a policy of `allow` does not silence you. With nobody to ask, the call is refused rather than run. |
| `req.allow()` | The confirmation gate is skipped for **this call only**. Nothing is written to `trust.json`, so it never widens into a standing grant. |

Say two of them and the more restrictive wins (deny over ask over allow), so the
order you call them in cannot change the outcome.

**What the request tells you** splits cleanly in two, and the split is the point:

- **Trustworthy**, from the caller's context: `user_scope_id`, `username`,
  `user_role`, `source`, `session_id`, plus what the tool declares -
  `tool_name`, `permission_level`, `side_effect_class`, `admin_only`,
  `channel_restrictions`.
- **Not trustworthy**: `args`, which is whatever the model produced. Read it to
  decide - that is the whole reason for per-call authorization - but never read
  an identity out of it. The identity is on the request precisely so you do not
  have to, and an `args["user_role"]` is the attacker's own answer.

`args` is a snapshot, so writing to it changes nothing: deciding is not editing.

Four limits worth knowing before you rely on it:

- **It cannot escalate.** `allow()` skips a confirmation question, never a policy
  block. An `admin_only` tool is refused before you are asked, so there is
  nothing for `allow()` to override. It is a second lock, not a master key.
- **A raising callback is a refusal.** This is the opposite of the event sink,
  which swallows failures on purpose: a broken observer must not fail a run it
  only watches, while a broken guard must not quietly become no guard.
- **The coder does not consult it.** Tool calls made *inside* the coder sub-agent
  are not put to your authorizer - not because of the process boundary (embedded,
  the coder runs inline in yours) but because its own loop calls `tool.run()`
  directly instead of going through the dispatcher. A callable also cannot cross
  into the terminal-spawned coder, which is why a per-user tool allowlist has to
  travel as data rather than as a callback.
- **The workflow engine does not consult it yet.** A saved workflow's steps run
  through the shared execution path but not yet through the full pipeline, so
  they are not authorized. If that matters to your deployment, do not enable
  workflows for those users.

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
fine). `run()` blocks; `await run_async()` wraps it in a thread executor (see
[Async applications](#async-applications)) - there is no natively async engine.

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

- `from vaf import Agent` - the façade: `Agent(config=..., system_prompt=..., user_scope=..., session=...)`, `.run(prompt, on_token=...)`, `.run_async(...)`, `.add_tool(tool)`, `.on_event(cb)`, `.save_session()`, `.core`.
- `vaf.markers` - the special-return-value constants.
- `vaf.CoreAgent` - the engine, for advanced embedding.
- `BaseTool` - the tool contract, including the `identity_kwargs` declaration.
- `vaf.user_jail` - turning a declared identity into a file boundary.
- `vaf.ToolCaller` - running a tool with the agent's own policy, gate, identity
  and bounds, without an agent. Its **documented arguments** (the table under
  "Running a tool yourself") and `execute(name, args) -> str` are the promise;
  the constructor's remaining parameters exist for VAF's internal lanes and are
  not.
- `set_tool_authorizer(fn)` on both `Agent` and `CoreAgent`, and `vaf.ToolRequest`
  with `deny()` / `ask()` / `allow()` and its context fields.
- The `vaf.tools` entry-point group.

Everything else under `vaf.core.*` is internal and may change between releases.
`vaf.ToolCaller` and `vaf.ToolRequest` are the deliberate exceptions: both live in
`vaf.core` but are re-exported on the façade, and the façade names are the ones to
import.
