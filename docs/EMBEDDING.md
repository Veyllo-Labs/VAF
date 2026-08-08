# Embedding VAF as a Library

VAF can be used as a headless agent **framework** - a foundation you build your
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
Google), but **not** the web server, desktop UI, embeddings stack, or chat
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

The same code works with a local GGUF model - it is provider-agnostic:

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

One `Agent` instance keeps one conversation - repeated `run()` calls continue
the same history. Create a new `Agent` for an independent conversation.

### One-shot completions

For one prompt and one answer with no conversation at all - a classification, a
summary, a generated snippet - use `complete()`:

```python
answer = agent.complete("Classify this ticket: ...", max_tokens=200)
```

The guarantees, each the reason this exists instead of "just call run()":
it does NOT enter the conversation (the history is untouched - a `run()` after a
`complete()` sees nothing of it), no tools run, nothing is written to memory or a
session, `<think>` reasoning blocks are stripped, and the return value is
`Optional[str]`: text, or `None` when no backend answered - never an exception
and never an error message dressed as content, so the result is safe to store
without inspection. The first use pays the same one-time engine build as the
first `run()` (system prompt; in local mode the model load); after that a
completion is a single backend call.

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
    config={"provider": "openai", "api_key_openai": os.environ["OPENAI_API_KEY"]},
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
- Needs a key: pass `api_key_<provider>` and optionally `api_model_<provider>`
  per instance via `config=`; your prompts are sent to that provider. Read the
  key from your own secret source - the snippets here take it from the
  environment on purpose, because a key in source code is a key in your git
  history.
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
    "api_key_anthropic": os.environ["ANTHROPIC_API_KEY"],
})
```

---

## Configuration

`config=` is a dict merged on top of `~/.vaf/config.json` for this instance only -
nothing is written to disk, so each `Agent` can carry its own settings. Common
keys (full reference in [CONFIG_SCHEMA.md](setup/CONFIG_SCHEMA.md)):

| Key | Default | Meaning |
|---|---|---|
| `provider` | `local` | `local`, `veyllo`, `openai`, `anthropic`, `google`, `deepseek`, `openrouter` |
| `model` | `auto` | local GGUF filename / repo, or an API model name |
| `api_key_<provider>` | - | API key, e.g. `api_key_deepseek` |
| `api_model_<provider>` | - | model per provider, e.g. `api_model_openai` |
| `n_ctx` | `32768` | context window (min 32768 for tool use) |
| `temperature` | `0.7` | sampling temperature |

```python
Agent(config={
    "provider": "openai",
    "api_key_openai": os.environ["OPENAI_API_KEY"],
    "api_model_openai": "gpt-4o",
})
```

The `api_key_*` and `api_model_*` you pass here reach **every** consumer for this
`Agent` instance - the primary backend, the failover chain, model discovery, and
the voice and speech lanes. That was not always true: the key used to be pulled out
as a single value and handed to one constructor, so an embedded agent's failover
chain could never find a key while this page said "pass your key". Your dict is now
the highest-precedence source, above anything stored on the machine, and a later
provider switch cannot substitute the machine's value for yours.

Two consequences worth knowing before you rely on it:

- **A provider key arms every capability that uses that provider.** Passing
  `api_key_openai` supplies the language model *and* speech-to-text if you enable
  it, because keys are addressed per provider, not per feature. Voice, speech and
  the learning system each sit behind their own switch, so nothing activates that
  you did not turn on.
- **It does not cross a process boundary.** The coder sub-agent runs in its own
  process and reads the machine's own encrypted store; an in-memory key never
  reaches it. This is the third member of a family worth recognising: the declared
  file boundary does not cover the linter either (it shells out), and the librarian
  has to carry identity through `extra_env` for the same reason. Passing a secret
  through a child's environment is a different decision from passing a scope id,
  and VAF does not make it for you.

**Where a key lives when VAF stores one.** Keys set through the product go into the
same envelope-encrypted store as mail, GitHub and cloud credentials - not into
`config.json`. Be precise about what that buys: without a master passphrase (the
default, and the headless case) the key-encryption key sits in `config.json` itself,
which `secure_store` describes as equivalent to chmod-only protection. The gain is
that the secret is no longer in the same file as everything else, is not readable by
eye, and does not travel in a config backup or a screenshot. It is not protection
against someone who can already read your data directory.

### Changing configuration on an agent that is already running

There is no supported way to do it, and that is a decision rather than an oversight,
so here is where the line sits.

**Your `config=` wins permanently, by design.** An agent you constructed with
overrides is caller-controlled for its whole life: nothing VAF does later - a provider
switch on the machine, a key rotated in the product's settings, a reload triggered by
its tray - can substitute the machine's value for yours. That is the guarantee the
precedence rules above are worth having.

The flip side is the boundary: **if you change your own settings, build a new agent.**
There is no `agent.set_config(...)`, and the process-wide re-apply VAF uses internally
(`vaf.core.agent.reload_all_api_backends`) deliberately skips every agent that carries
overrides - so it would do nothing for you even if you reached into it. Treat anything
under `vaf.core.*` as internal, as the stability section says.

Why it is drawn there rather than built out: the need was measured on VAF's own
product, where five chat workers each held a backend and only one was reachable, so a
replaced API key kept being ignored by four of them until a restart. That is a real,
counted requirement for a harness that owns many agents and no config overrides. No
comparable measurement exists for an embedder yet, and a public name whose shape is
still open is worse than none. If you hit this - if rebuilding an agent is genuinely
not an option for your application - that is the report that would move the line.

### A complete example, with error handling

The engine is built **lazily** on the first `run()` (or `.core`) call, so
configuration and connection problems surface there, not at `Agent(...)`. `run()`
returns the final answer as a string. Most misconfigurations and unreachable-provider
errors **raise**, so wrap the call. Some *handled* failures (e.g. the API returning
empty responses repeatedly) are caught internally and come back as a short status
string instead of raising:

```python
from vaf import Agent

agent = Agent(config={
    "provider": "openai",
    "api_key_openai": os.environ["OPENAI_API_KEY"],   # read it, never inline it
    "api_model_openai": "gpt-4o-mini",
})

try:
    answer = agent.run("In one sentence, what is Python?")
    print(answer)
except ValueError as e:
    # configuration problem - e.g. a missing/empty API key for the chosen
    # provider (when no local fallback applies)
    print("config error:", e)
except Exception as e:
    # runtime/provider failure - unreachable endpoint, network timeout,
    # unknown model, ...
    print("run failed:", e)
```

Notes:

- **Lazy init.** `Agent(config=...)` is cheap; the core engine and the provider
  connection are created on first use. Put your `try/except` around `run()` (or
  around the first `.core` access).
- **Gated tools never hang or raise.** Under the embedded default
  `VAF_NONINTERACTIVE=1`, a tool behind the confirmation gate returns an error
  *string* in its result instead of blocking on a human - the run continues and
  the final answer explains what was refused. Grant specific tools via the trust
  mechanisms (below) to let them run unattended.
- **Handled failures may return a string, not raise.** After exhausting its
  internal retries (e.g. a provider returning empty responses), `run()` returns a
  short status string - currently prefixed `[SYSTEM_LOG_ONLY]` - rather than
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
  (Linux `~/.config/vaf/`, macOS `~/Library/Application Support/vaf/` - an
  explicitly set `XDG_CONFIG_HOME`/`XDG_DATA_HOME`/`XDG_CACHE_HOME` wins on
  macOS too, which is how a test harness isolates the store there,
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
- **No account allowlist applies until you register one.** A bare agent
  consults no per-account tool list, because nothing is registered; a
  multi-tenant application registers one resolver process-wide with
  `set_account_allowlist_resolver` (its own section below).

---

## Sub-agents as a library

VAF's heavy sub-agents (`coding_agent`, `research_agent`, `document_agent`,
`librarian_agent`, `browser_agent`) are tools the model can call, but they are
built for the full product and only partially apply in a bare library process.
What to expect:

- **Inline execution works.** In a headless library there is no terminal to
  spawn and no web server to stream to, so a sub-agent call runs INLINE, in the
  same process, and returns its result to your `run()` turn. This is the
  supported library path and needs nothing extra - as long as nothing else is
  drawing on the terminal.
- **If YOUR application owns the terminal, an inline sub-agent will draw over
  it.** The heavy sub-agents animate a progress panel on stdout while they
  work: the coder builds its own console with `force_terminal=True`, so the
  output arrives even when stdout is not a tty. In a headless service that is
  invisible and harmless. In a full-screen terminal application it shreds the
  display. VAF's own terminal app solves this with an internal switch
  (`vaf.cli.tui.UI.set_app_mode`) that makes those panels no-ops; it is not
  public because VAF's app is its only caller so far, and a name whose shape is
  still open is worse than none. If you hit this - if you are building a
  terminal front end on VAF and need sub-agents to render quietly - that is the
  report that would move the line.
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
  from vaf import BaseTool

  class TenantNotes(BaseTool):
      name = "tenant_notes"
      description = "Read the calling user's notes."
      parameters = {"type": "object", "properties": {"path": {"type": "string"}}}

      # Ask for exactly what you consume. Valid keys: "user_scope_id",
      # "username", "user_role". Declaring nothing means receiving nothing.
      identity_kwargs = ("user_scope_id", "user_role")

      # Declare the MODE your tool needs, and the per-user file boundary is installed
      # around run() for you - on every lane, including a direct `.run()` call with no
      # dispatcher in the picture. "read" reaches further than "write" by the folders
      # of the skills this user may see.
      file_access = "read"

      def run(self, **kwargs):
          # Already confined when this line runs. Nothing to enter, nothing to release.
          return read_notes(kwargs.get("path"))
  ```

  `file_access` without the matching `identity_kwargs` is a **TypeError when your class is
  defined**, not a silent no-op at run time. The boundary is resolved from the caller's
  scope, so a tool that never receives one would run completely unconfined while looking
  confined - the contract refuses to be declared halfway. Nesting only ever NARROWS: if a
  boundary is already installed, yours can shrink what it inherits and never add to it.

  Until this existed, the example here told you to write `with user_jail(scope, role, ...)`
  inside `run()` yourself. That was honest about what the framework offered and it was the
  wrong offer: eleven built-in tools hand-rolled the same four steps, and five of the
  twenty-two that needed them had simply forgotten. An older tool doing it by hand keeps
  working - `user_jail` is still exported - but the declaration is the supported form.

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
- **Which tools an account may use at all is YOUR backend's answer.** Register
  one resolver process-wide with `set_account_allowlist_resolver` (its own
  section below) and every scoped, non-admin call is checked against it in the
  funnel - before your authorizer, so an `allow()` cannot lift an account-level
  ban.
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
        self.log(f"[WEATHER] looking up {kwargs['city']}")
        return f"It is sunny in {city}."
```

`self.log(message)` is the supported way for a tool to write a diagnostic
line. It appends to `tools_<date>.log` in the VAF log directory, filling in
your tool's name and the current session id, and it inherits everything the
rest of VAF's logging has: the `VAF_LOG_DIR` redirect, the
`debug_logs_enabled` switch, dated files, and garbage collection. It never
raises, so a broken log line cannot fail a tool call. Do not import
`vaf.core.log_helper` - that is internal and offers no stability promise.

The caller's identity is deliberately not added to the line: it is not
ambient, and one tool instance is shared by every user of an agent, so
anything cached on `self` would leak across them. If you want the scope in
your own lines, declare it via `identity_kwargs` (below) and write it
yourself.

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
- `side_effect_class` - surfaced to the model so it knows what is reversible.
- `admin_only`, `channel_restrictions`, `coder_only` - visibility/scoping.

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

Runnable version, including the authorizer below:
[examples/07_tool_caller_and_authorizer.py](../examples/07_tool_caller_and_authorizer.py).
It is the one example that needs no provider and no network.

What one `execute()` does, in order: write one audit line to
`tool_use_<date>.log` (see below), evaluate policy (`admin_only`,
`channel_restrictions`), consult the account allowlist (the resolver you
registered, if any - its own section below), consult your authorizer, consult
the confirmation gate, emit `tool_start`, validate and repair the arguments
against the tool's schema, assign the declared identity, run the tool under a
timeout with stop polling, emit `tool_end`, and truncate. Same order, same
rules, same event schema as a chat turn - the ordering is pinned by a
measurement, not by convention. Note the two cuts are independent: `tool_end`
carries its own 800-character copy of the result for observers, while
`max_result_chars` below governs what the caller gets back - setting the latter
to `None` does not put an unbounded blob on your event sink.

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
| `user_scope_id`, `username`, `user_role` | Who is calling. Assigned into whatever the tool declares in `identity_kwargs`, overwriting anything a model put there. **Pass `username` if you serve more than one tenant.** With none, the name is resolved from the SCOPE: no scope or the owner's scope gives the configured owner (`local_admin_username`, whatever registration wrote there - never the literal `"admin"`), and any other scope gives a stable synthetic name for that tenant, so a caller whose name you did not pass never lands on the owner's name-keyed data. That synthetic name is isolated, not their account name - if a tenant must reach data stored under their real username, pass it. |
| `source`, `session_id` | Where the call comes from. Feeds `channel_restrictions`; leave them out if you have no messaging channels. |
| `interactive`, `decide` | Set `interactive=True` and pass `decide(tool_name, reason) -> "allow_once" \| "allow_always" \| "cancel"` to plug your own confirmation UI into the gate. Left out, gated tools are refused rather than run. |
| `trust_dir` | Which directory a standing grant applies to. Defaults to the process's current one. |
| `timeout_for` | `f(tool_name) -> seconds`, for your own timeout policy. Defaults to the configured agent timeout. |
| `stop_check` | `f() -> bool`, polled during the run so you can cancel from outside. |
| `max_result_chars` | Result cut, `2000` like a chat turn. Pass `None` to switch it off - do that when you chain a result into something else, because a cut result can lose a trailing marker. |
| `authorize` | Your per-call decision hook, exactly as in the next section. `ToolCaller(..., authorize=fn)` is the same thing `Agent.set_tool_authorizer(fn)` installs. An account-level ban (the allowlist section below) is checked before it and cannot be lifted by an `allow()`. |
| `on_event` | `f(dict)` for `tool_start` / `tool_end` / gate events. Same schema as `Agent.on_event` (`CoreAgent.set_event_sink`), documented in [OBSERVABILITY.md](OBSERVABILITY.md). A raising sink is swallowed: a broken observer must not fail a run. |

**It writes an audit line per call.** Every `execute()` appends one line to
`tool_use_<date>.log` - timestamp, tool, `session_id`, `user_scope_id`, and a
sanitized 200-character argument preview - before anything else happens, so a
call your authorizer or the allowlist refused is recorded too. That is what
makes the file usable for "which tenant reached for what". Turn it off with
`debug_logs_enabled: false` in the on-disk config, choose where it lands with
`VAF_LOG_DIR`; see [DEBUGGING.md](DEBUGGING.md). Without `VAF_LOG_DIR` a
pip-installed VAF writes to `Platform.data_dir()/logs`, never into your
site-packages.

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
answered from what the *tool* declares - plus a fourth, which tools this
ACCOUNT may use, answered by the resolver your application registers (the next
section). Your application also knows things no declaration can carry: which
paths this customer owns, that this account is thirty seconds from its quota,
that THIS argument is the problem.

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
  into the terminal-spawned coder. The account allowlist is the exception, and
  the reason it is one: the coder DOES enforce it, because its answer is data -
  resolved once from your registered resolver and carried into the child as
  `VAF_ALLOWED_TOOLS` (next section) - while a callback is not.
- **The workflow engine consults it for non-spawn steps - with three limits of its
  own.** A workflow step now runs through the full pipeline: your authorizer, the
  account allowlist and the hard policy blocks all apply. Still outside: `ask()` -
  the workflow lane runs with the confirmation gate off, so an `ask()` degrades to
  no opinion there and only `deny()` binds; spawn-mode sub-agent steps (the heavy
  steps of a temporary workflow run as child processes, which is a spawn plus an
  IPC wait, not a tool run - the sub-agent's inner tools remain constrained by
  `VAF_ALLOWED_TOOLS`); and the workflow CLI subprocess, which no callable can
  reach across a process boundary (the account allowlist still holds there, because
  its answer comes from the resolver that process registers itself).

---

## Which tools an account may use: `set_account_allowlist_resolver`

The authorizer above decides per CALL. One question sits below it: which tools
does this account get at all - the thing a plans table or an admin panel
answers. That answer lives in your backend, so you register a resolver for it,
once per process:

```python
from vaf import set_account_allowlist_resolver

def allowed_tools(user_scope_id):
    plan = plans.get(user_scope_id)          # your own storage
    return None if plan is None else plan.tool_names

set_account_allowlist_resolver(allowed_tools)
```

Every scoped, non-admin call in the funnel - the agent's chat lane and a bare
`ToolCaller` alike - is checked against it, after the hard policy block and
BEFORE your authorizer, so an account-level ban cannot be lifted by an
`allow()`. A blocked call is refused with `Security Error: ...` and emits no
events, exactly like a policy block.

The contract, each choice against its failure mode:

- **`None` means unrestricted** - from the resolver, or because none is
  registered. A bare embedder has not opted into account policy and must not be
  locked out by a default.
- **Any other answer is normalized to a frozenset of tool names; an EMPTY
  answer allows nothing.** If your storage treats an empty list as "no
  restriction", map it to `None` yourself - that is a statement about your data
  model, not about the primitive.
- **Callers with no scope and admin identities are exempt** and never reach the
  resolver: it answers "which list for this scope", never "who is exempt". A
  resolver that crashed on the admin's own scope cannot lock the admin out.
- **A raising resolver is a refusal** - the authorizer's polarity, not the
  event sink's: a broken guard must not quietly become no guard. If you want
  fail-open for an unreachable backend, catch inside your resolver and return
  `None`; VAF's own product resolver does exactly that for its auth database.
- **Consulted per call, not cached.** Revocation latency is your resolver's own
  business - cache inside it if lookups are expensive.
- **Process-wide.** One resolver per process (the same topology as "one tenant
  per process" above); the last registration wins and `None` deregisters.

**The answer crosses process boundaries as data.** A callable cannot follow the
coder into a spawned child, so the resolved allowlist travels in the child's
environment as `VAF_ALLOWED_TOOLS`: comma-separated tool NAMES, never a secret
(the same rule the sub-agent section draws for scope ids), absent meaning
unrestricted. The coder filters blocked tools out of the schema its model sees
and refuses hallucinated names at dispatch, on both sides of the boundary - so
"block the coder entirely" stops being the only expressible opinion about what
runs inside it. If you spawn VAF child processes yourself, set the variable in
the CHILD's environment only, never process-globally in a multi-tenant parent.

Runnable demonstration: part 6 of
[examples/07_tool_caller_and_authorizer.py](../examples/07_tool_caller_and_authorizer.py).

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
(a broken package logs an error and is skipped - it never breaks startup).

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
  lifecycle, the `chat_step`/`complete`/`execute_tool` contracts, and the
  concurrency rules.

**Concurrency contract (short version):** one `Agent` is one conversation and
is effectively single-threaded - drive it from one thread at a time. For
parallelism, create multiple `Agent` instances (each in its own thread is
fine). `run()` blocks; `await run_async()` wraps it in a thread executor (see
[Async applications](#async-applications)) - there is no natively async engine.

One corollary when you use SESSION-scoped features (sub-agents, per-session
working memory): the current session lives in a per-thread `ContextVar`, and a
value set on one thread is invisible to threads you start yourself. The thread
that drives an agent must declare its session
(`vaf.core.subagent_ipc.set_current_session_id`) - once at thread start, again
on a session switch; for a one-off session-scoped call from a borrowed thread
use the `session_context(sid)` context manager, which restores the thread's
context exactly as it was. Skipping this quietly routes session-scoped writes
into the legacy global store - VAF's own terminal app shipped that bug.

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

- `from vaf import Agent` - the façade: `Agent(config=..., system_prompt=..., user_scope=..., session=...)`, `.run(prompt, on_token=...)`, `.run_async(...)`, `.complete(prompt, ...)`, `.add_tool(tool)`, `.on_event(cb)`, `.save_session()`, `.core`.
- `vaf.markers` - the special-return-value constants.
- `vaf.CoreAgent` - the engine, for advanced embedding.
- `BaseTool` - the tool contract, including the `identity_kwargs` declaration
  and `self.log(message)`.
- `vaf.user_jail` - turning a declared identity into a file boundary by hand. Prefer the
  `file_access` declaration on your tool, which does it on every lane; this remains
  exported for tools that need the boundary around something other than a whole `run()`.
- `vaf.ToolCaller` - running a tool with the agent's own policy, gate, identity
  and bounds, without an agent. Its **documented arguments** (the table under
  "Running a tool yourself") and `execute(name, args) -> str` are the promise;
  the constructor's remaining parameters exist for VAF's internal lanes and are
  not.
- `set_tool_authorizer(fn)` on both `Agent` and `CoreAgent`, and `vaf.ToolRequest`
  with `deny()` / `ask()` / `allow()` and its context fields.
- `vaf.set_account_allowlist_resolver(fn)` - the per-account tool allowlist hook,
  with the contract its section documents (None/unregistered = unrestricted,
  empty = nothing, exemptions framework-side, raising = refusal, and the
  `VAF_ALLOWED_TOOLS` child-process transport).
- The `vaf.tools` entry-point group.

Everything else under `vaf.core.*` is internal and may change between releases.
`vaf.ToolCaller`, `vaf.ToolRequest` and `vaf.set_account_allowlist_resolver` are the
deliberate exceptions: they live in `vaf.core` but are re-exported on the façade, and
the façade names are the ones to import.
