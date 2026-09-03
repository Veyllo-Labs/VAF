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
| `vaf[speech]` | SpeechRecognition, pyaudio, faster-whisper | offline speech-to-text |
| `vaf[browser]` | browser-use, playwright | browser automation tools |
| `vaf[pdf]` | pdfplumber, pytesseract, pypdfium2 | PDF extraction / OCR |
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
(`~/.vaf/users/<owner>/soul.md`, where `<owner>` is the account name recorded
in `local_admin_username` - not the literal `admin`); if none exists yet, VAF
materializes its
default persona there on first use (a neutral built-in identity is only the
last resort when that load fails). For an embedded app you almost always want
to set `system_prompt` explicitly, so your agent's voice does not depend on
machine-local state.

### The two code-owned persona addenda

When you do NOT override, the engine appends two code-owned blocks to the
persona, on the Soul path and the fallback path alike. **Continuity** tells the
model its long-term memory is real and lives in its tools: recall with
`memory_search` before guessing, save new facts with `memory_save`, correct an
existing one with `memory_update`. **The capability answer** tells it how to
answer "what can you do?": turn the question around, ask what the user needs,
and claim only what this session's registry really holds - the live tool count,
and each ability line (build a missing tool or skill, delegate to sub-agents
and workflows, standing orders) only when the tools behind it are registered.

A `system_prompt` override replaces the persona wholesale and drops both, by
design: your support bot decides for itself whether and how it speaks about
memory or capabilities. To keep either behavior under your own persona, both
are exported on the facade:

```python
from vaf import Agent, SOUL_CONTINUITY_ADDENDUM, build_capability_addendum

tool_names = {"kb_search", "create_ticket", "escalate_to_human"}
agent = Agent(
    config={...},
    system_prompt=(
        MY_SUPPORT_PERSONA
        + SOUL_CONTINUITY_ADDENDUM                                 # keep the memory lane
        + build_capability_addendum(tool_names, len(tool_names))   # grounded "what can you do"
    ),
)
```

`build_capability_addendum(tool_names, tool_count)` is a pure function: it
writes an ability line only for tools present in `tool_names`, so a trimmed
support-bot registry yields a truthful, smaller answer instead of promises the
runtime would refuse - and you may of course write your own text instead; the
addenda are building blocks, not obligations. The engine-side behavior is
pinned by `tests/test_embedder_system_prompt.py` and
`tests/test_capability_answer_prompt.py`; the exported shapes by
`tests/contract/test_contract_persona_addenda.py`.

### Encryption at rest, and the two modes you can ship

VAF encrypts what it stores - chats, context archives, sub-agent payloads,
working memory - with a machine-held key, and keeps the key out of
`config.json`. Nothing is required of you: the key mints itself on first use,
exactly as the memory key always did, and the agent keeps running unattended
after a reboot because no human has to supply anything.

You choose per deployment with `file_encryption_enabled`:

```python
# Mode 1 (default): VAF encrypts the end user's chats and working data.
agent = Agent(config={"provider": "openai", "api_key_openai": key})

# Mode 2: your storage layer already handles this - keep the files plaintext.
agent = Agent(config={..., "file_encryption_enabled": False})
```

Reading has three states, not two. An encrypted file always decrypts. A
plaintext file is tolerated on read until a startup pass proves the store holds
nothing but ciphertext; that pass sets `allow_plaintext_at_rest` to false, and a
file without the header is refused from then on, because tolerating it forever
is a downgrade path. That pass runs in VAF's own CLI and web startup, not in an
embedded `Agent`, so a library-only deployment stays tolerant until you set
`allow_plaintext_at_rest` to false in the on-disk config. Turning
`file_encryption_enabled` off reopens the tolerance, since a store that writes
plaintext by choice has to be able to read it. So you can still switch either
way without a migration and without stranding what is already on disk.

Two things to know before you pick Mode 1:

- **The key is machine-held.** It protects the data against a stolen disk, a
  copied directory, a backup and other local accounts. It does not protect
  against code running as your process - see
  [ENCRYPTION_AT_REST.md](security/ENCRYPTION_AT_REST.md) for the full table.
- **Losing the key store loses the data.** `<data_dir>/data_keys.enc`, its
  `.key.json` sibling and the master key (OS keyring entry `vaf/secure_store_kek`
  or `~/.vaf/secure_store.kek`) must be backed up together.

The keyring itself is engine-internal and not exported on the facade; no
embedder has asked for direct key access, and the switch plus the documented
file locations have covered every case so far.

**Run it rather than read it.**
[examples/08_session_storage_and_encryption.py](../examples/08_session_storage_and_encryption.py)
builds a throwaway home and walks all four decisions in one script - plaintext,
plaintext with several tenants, encrypted (grepping the raw bytes to prove the
words are not there), and recovering after the machine key is deleted. It needs
no model, no API key and no network.

### Injecting retrieved context

`system_prompt` is identity and framing. For "here is what I looked up for THIS
question", the seam is `CoreAgent.chat_step(memory_context=...)`
([CORE_AGENT.md](CORE_AGENT.md)) - an opaque string that the engine wraps in its
own `## Memory context (relevant to this query)` section and splices into a copy
of the first system message.

Two limits to know before you build on it:

- **`vaf.Agent.run()` does not forward it.** Reach for `CoreAgent` when you need
  per-turn retrieved context; the convenience facade has no parameter for it.
- **The section text is the engine's, not yours.** You supply the entries; the
  heading and its guidance sentences come from VAF and may change with a release.

**Your agent reads the user's other chats by default.** With
`cross_chat_hint_enabled` (default `true`), the engine appends up to
`cross_chat_hint_k` short pointers into OTHER sessions of the same caller
underneath the retrieved memories: lexically matched, read straight from the
session files, no database involved. The same switch also governs the second half
of the `memory_search` tool, which searches those sessions when the agent asks on
purpose. Sessions belong to a caller by strict scope
equality, deleted chats are unreadable rather than filtered, and conversations
with known contacts are skipped - but if your application stores anything in a
session that must never surface in a different session of the same user, set
`cross_chat_hint_enabled` to `false` (or `cross_chat_hint_k` to `0`). VAF does not
enumerate sessions on the public surface, so this lane is engine-internal by
design; see [CONTEXT_MANAGEMENT.md](memory/CONTEXT_MANAGEMENT.md#cross-chat-hint).

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
| `context_compress_tokens` | `45000` | API providers: history token budget at which compression fires (`min(window, budget)`, floored at 8000); every round-trip resends and bills the whole history, so the window alone is the wrong ceiling. `0` = window-based. Local models ignore it. For your own settings screen, `vaf.core.context.resolve_context_effort()` returns the rung ladder, the model's real window and whether the budget applies to the configured provider |
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
`config.json`, which now holds no key material at all. The key that opens that
store sits in a 0600 file (`~/.vaf/secure_store.kek`) by default, or in the OS
keyring with `secure_store_kek_backend = "keyring"` - which is stronger, because
the OS keyring is protected by the login password, but only reachable for installs
that start VAF from inside the desktop session. Be precise about what the default
buys: protection against a copied directory, a backup, a support archive and other
local accounts, and against a stolen disk only as far as the disk encryption
underneath reaches. It is not protection against code already running as you. The
full table is in [ENCRYPTION_AT_REST.md](security/ENCRYPTION_AT_REST.md).

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
  fail soft with an error string. Managing it is supported rather than left to
  you: `vaf.core.service_stack` (`SERVICES`, `ensure_service_stack`,
  `stop_service_stack`) starts and stops the stack, and
  `vaf.core.service_health` (`collect_service_status`, `repair_service_stack`)
  answers what state each container is in and repairs a broken one, with every
  probe injectable so an embedder can test against it without Docker. One
  exception to know:
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

## Content arriving from someone else: `vaf.inspect_upload`

If your application lets anyone attach a file - a chat upload, a webhook payload,
a mailbox, a shared folder - you have an ingress lane, and it needs the same
question asked at every one of them: *have we already decided about exactly these
bytes?*

VAF keeps that decision in a machine-wide list of digests and gives you the funnel
that consults it:

```python
import vaf

verdict = vaf.inspect_upload(data, filename="report.pdf", origin="my_upload_form")
if verdict.blocked:
    return {"error": verdict.message("report.pdf")}   # a sentence you can show a user
if verdict.flagged:
    log.warning("suspicious upload: %s", verdict.advisory_level)
save(data)
```

`UploadVerdict` carries `blocked`, `reason`, `sha256`, `sha3_256`, `size`,
`advisory_level` and `advisory` (the individual findings). Two companions live in
`vaf.core.threat_db` for shapes the facade name does not fit:
`refuse_known_bad(data, filename=, origin=)` returns a plain bool, for a callback
deep inside a bridge that has no way to answer the sender; and
`inspect_upload_file(path)` streams the digests, for content already on disk that
may be large.

**Two verdicts, and only one of them refuses.** A listed digest is a block: a
human already judged these exact bytes. The second half is the static scanner's
opinion on arriving text (dynamic execution, pipe-to-shell, embedded keys, hidden
bidi characters), and it NEVER blocks - those heuristics have false positives by
construction, and refusing on them would reject ordinary work daily. Honour
`blocked`; treat `flagged` as something to log, warn about, or route to a human.

**Filling the list** is your call to make, and the intended trigger is a
CONFIRMED verdict, not a suspicion:

```python
vaf.record_threat(sha256=digest, sha3_256=digest3, name="payload.py",
                  reason="confirmed hostile", listed_by="admin@example")
```

Also in `vaf.core.threat_db`: `record_bytes_threat` / `record_file_threat` (which
hash for you), `check_bytes` / `check_file` / `check_hashes` for a lookup with no
event, `list_threats`, `threat_count` and `remove_threat`.

**What you are responsible for.** This module is a library: it enforces nothing
about who may call it. Gate your own write path - `record_threat` and
`remove_threat` are administrative actions, and delisting in particular re-opens
every lane at once. The list is machine-wide on purpose (a verdict about bytes
does not belong to an account), so in a multi-tenant deployment it is a shared
resource that one tenant must not be able to edit.

**Properties worth knowing.** Every record carries a sha256 and a sha3_256, and
either matching is a hit. The store is append-only JSONL at
`~/.vaf/security/threat_db.jsonl`, 0600 in a 0700 directory (both no-ops on
Windows - see the platform note in `secure_store.harden_path`), and it is NOT
encrypted, deliberately: a hash is not a secret, and an operator needs to be able
to read the file when a block has to be explained. Nothing here raises - a guard
that throws is a guard that gets wrapped in a bare `except` and stops guarding -
and a list that cannot be read fails OPEN, because it has not refused anything.
Two config keys switch the halves off: `upload_threat_scan_enabled` and
`upload_scan_advisory_enabled`.

The full lane inventory, the event kinds and the product's own admin surface are
in [SECURITY_DASHBOARD.md](security/SECURITY_DASHBOARD.md).

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
- **Standing grants are per user, not per machine**: `set_tool_policy(...,
  "allow")` and a trusted directory are stored under the caller's
  `user_scope_id` (`~/.vaf/trust/<scope>.json`; the local admin collapses to
  `default.json`). One tenant's "always" no longer arms the tool for another.
  An instance that predates this keeps its old flat `trust.json` as
  `trust.json.pre-scope` and inherits nothing: those entries were granted under
  a store that could not tell tenants apart. Note the separate hands-off
  switch `tool_confirmation_bypass_admins` (admin-writable, default off): it
  lets an ADMIN identity skip the dialog and emits a `gate_bypassed` event
  every time; it cannot widen `admin_only`, the account allowlist, or an
  authorizer's explicit `ask()`.
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
    category = "web"                   # which bundle it appears under in tool lists
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

Results are capped by the dispatch funnel (`max_result_chars`, 2000 by default)
before they reach the model. A tool whose result IS the deliverable - a briefing
to hand over, a document body to follow - declares `result_is_deliverable = True`
and is returned whole; the flag is a promise in return that the tool keeps its
own output bounded. The full agent honors the same declaration downstream: its
history compression never prunes such a result, and its error classifier judges
it by anchored belts only (a document is not a failure for containing the word
"failed"). Observation is unaffected either way: the event stream caps tool
results independently.

`self.log(message)` is the supported way for a tool to write a diagnostic
line. It appends to `tools_<date>.log` in the VAF log directory, filling in
your tool's name and the current session id, and it inherits everything the
rest of VAF's logging has: the `VAF_LOG_DIR` redirect, the
`debug_logs_enabled` switch (on by default), dated files, and garbage
collection. It never
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
- `category` - which bundle the tool appears under in the human-facing tool
  lists (the web tools window, the CLI table, the TUI overlay, `list_tools`).
  The in-tree vocabulary is `TOOL_CATEGORIES` in `vaf/core/tool_contract.py`,
  but the field is **open**: a value the framework has never seen is kept as
  declared and rendered as its own bundle, which is how your tool - or an MCP
  server - names a bundle of its own without patching the framework. Omitting
  it puts the tool in `general`; it is presentation only, no policy reads it.
  One namespace is reserved: `custom` and `custom_*` belong to tools loaded
  through VAF's own custom-tools store (files a user uploaded through the web
  UI), whose declared bundle is moved into that namespace by the loader so an
  uploaded file never appears inside a bundle of tools that ship with VAF.
  **Your tools are not "custom" in that sense** - a tool you register with
  `add_tool()` or publish as a `vaf.tools` entry point is first-party to your
  application and keeps whatever bundle it declares.

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

| `hooks` | `vaf.ToolCallHooks(after_policy=, before_dispatch=, after_dispatch=, after_emit=)`: the four measured positions inside one call, the same ones the agent's chat lane rides. `after_policy(name, tool, args) -> str | None` ends the call with the string (a gate); `before_dispatch(name, tool_args) -> str | None` may edit the arguments or refuse; `after_dispatch(name, tool_args, result) -> str` may replace the result; `after_emit(name, result)` sees the untruncated result on paths that dispatched. Runs after the hard policy block and the allowlist, so none of them can lift a ban. This is how a planner or a job queue of your own gets its gates and guardrails without patching the loop. |

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

### If you build your own loop: keep a way to stop

Worth stating because VAF learned it the expensive way. If your loop ever asks a
provider for a tool call it *must* make - OpenAI's `tool_choice="required"`, or
any equivalent - then the tool set you send has to contain something the model
can call to FINISH. Narrow that set anywhere (a size cap, a router, a
context-pressure fallback) and the terminating tool can silently fall out of it;
the model is then obliged to call a tool and owns none that ends the turn, so it
re-calls whatever is left until a cap stops it. One of VAF's own background runs
spent twelve tool turns re-calling a tool that kept refusing.

Two things follow, and neither is specific to VAF:

- **Pin the terminating tool through every narrowing**, in code. A tool that is
  merely "usually selected" is not pinned.
- **Never let a tool's error text be the only thing bounding a retry.** If a
  refusal says "try again", something outside the model has to count the tries -
  and it has to count them where the retry actually happens, which is inside one
  request/response round, not in whatever loop wraps it.

`ToolCaller` does not force tool calls, so it cannot produce this on its own; the
warning is for the loop you write around it.

---

## Running a voice turn yourself: `VoiceTurnEngine`

The live-call turn pipeline as an object - for a car assistant, a home speaker,
your own call transport. Audio bytes in, ONE decided `TurnOutcome` back: noise
gate, STT, speaker verification with the anti-spoofing rules, the reflex policy
(side-talk, chime-in, "did you mean me?"), the first-layer reply, and the
delegate DECISION. It is **the same object VAF's own web call runs on** - the
handler is a thin consumer of it, which is the proof this surface suffices.

```python
from vaf import VoiceTurnEngine

state = {"history": [], "lang": "de", "scope": "your-tenant-scope",
         "session": "call-1", "chime_recent": []}
engine = VoiceTurnEngine(
    state,
    transcribe=my_stt,            # (wav_bytes, **kw) -> (text, detected_lang)
    lane_speaks=lambda lang: True # which languages YOUR tts can speak
)
outcome = engine.turn(wav_bytes, session_id="call-1",
                      main_busy=False, pending_task="", username="alice")
if outcome.error:
    ...                            # busy_local | no_speech | llm_failed
elif not outcome.flags.get("silent"):
    audio = my_tts(outcome.reply, outcome.tts_lang)   # TTS is YOURS
if outcome.delegate:
    ...                            # hand the task to your worker - the engine
                                   # only DECIDES; enqueueing is the caller's
```

What one `TurnOutcome` can be - `kind` decides the handling, and exactly one
variant flag is ever set:

| `kind` | Meaning | You do |
|---|---|---|
| `reply` | a spoken answer (may carry `delegate`) | TTS `reply`, hand `delegate` to your worker |
| `silent` | side talk / not addressed - kept as room context | nothing; keep listening |
| `clarify` | the agent asks "did you mean me?" (speaker ambiguity) | TTS `reply` |
| `reask` | the user asked the agent to repeat its own question | TTS `reply` |
| `chime_in` | a brief grounded remark on overheard talk (opt-in topics) | TTS `reply` |
| `busy_local` / `no_speech` / `llm_failed` | `error` is set; nothing was decided | keep listening |

`tts_follow=True` (on `reply` and `chime_in`) means: voice the text in ITS OWN
language when your TTS can, else fall back to `tts_lang`.

**The state-dict contract, precisely:** the five keys you INITIALIZE are part
of the promise - `history: []`, `lang`, `scope`, `session`, `chime_recent: []`
(plus optional `chat_context`, `agent_name`, `agent_soul` for a richer
persona). Everything the engine ADDS to the dict at runtime (pending
questions, speaker-window timestamps, ...) is internal bookkeeping and may
change between releases - hold the dict, do not read its insides. One dict +
one engine per call; a new call gets fresh ones, never a merge.

The division of labor, stated plainly:

- **You own the transport and the TTS.** The engine never opens a socket and
  never speaks; `outcome.reply` + `outcome.tts_lang` (and `tts_follow`, when the
  reply should be voiced in its own language) are yours to synthesize. VAF's own
  handler applies per-variant TTS timeouts (30 s short lines, 60 s chime, 130 s
  reply) - a sane starting point.
- **You bring the STT.** The `transcribe` seam is where your recognizer plugs
  in; the default reaches for VAF's speech stack (`vaf[speech]` extra plus its
  Docker lane), which an embedded process usually does not want.
- **The reply layer needs an LLM.** `voice_reply` runs on the configured
  provider (API key or the one local server), like every other engine call.
- **Speaker security works, or fails open, exactly as documented.** With no
  enrolled voice profile every speaker is the owner (documented fail-open).
  With one, only a voice-verified owner can produce `outcome.delegate`; guests
  get guarded spoken replies with the owner's private context withheld. The
  sticky-window and arm-gate rules are unit-tested
  (`tests/test_voice_turn_engine.py`).
- **State is a plain dict you hold.** One engine per call; a new call gets a
  fresh dict and a fresh engine, never a merge. `engine.end()` clears the
  rolling transcript.
- It is **sync**, like every engine object - run it in your own executor.

Runnable version: [examples/09_voice_turn.py](../examples/09_voice_turn.py)
(injects a scripted STT, prints instead of speaking; needs only a configured
provider). The full pipeline semantics - every gate, every outcome kind - are
documented in [docs/agents/VOICE_AGENT.md](agents/VOICE_AGENT.md).

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

**Asking the same question without making a call: `account_allows_tool`.** A
surface that LISTS tools - a command palette, a picker, a row of shortcuts -
has to know the answer before the user clicks, or it offers a tool the account
cannot run and the refusal arrives too late to mean anything:

```python
from vaf import account_allows_tool

visible = [name for name in registry
           if account_allows_tool(name, user_scope_id, user_role)]
```

Use it instead of consulting your own resolver directly: the exemptions are
part of the answer. A lister that reproduced the lookup but forgot that a
scopeless caller and an admin are unrestricted would quietly strip an admin's
own tools. It raises whatever your resolver raises, so the caller decides what
its fail-closed looks like - the funnel refuses the call, a lister can drop the
entry.

Its sibling, `set_confirmation_bypass_resolver`, answers the opposite kind of
question: does this account hold the admin-granted hands-off switch that skips
the tool-confirmation DIALOG? `resolver(user_scope_id) -> bool`; unregistered
means nobody has it, and the polarity is inverted from the allowlist on
purpose - it fails CLOSED, because this flag removes a question rather than
restricting a capability. It sits UNDER the authorization stages (it can never
widen `admin_only`, the account allowlist, or override an authorizer's
`ask()`), and every skipped dialog is announced as a `gate_bypassed` event
with `why: "user_grant"`.
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
the CHILD's environment only, never process-globally in a multi-tenant parent -
VAF's own spawners all go through one implementation
(`vaf.core.subagent_spawn.spawn_subagent`) that takes agent-specific env as
data and never touches the parent's `os.environ`; it is currently internal (a
named boundary: no embedder demand measured yet), but it is the pattern to
copy.

**Which accounts exist at all, by name: `set_account_directory_resolver`.** The
allowlist answers what one account may do; a room invitation ("invite bob") needs
the tenant behind a NAME, and the picker that offers the names needs the list. Both
live in your backend, so you register a directory once per process:

```python
from vaf import set_account_directory_resolver

def accounts():
    return [{"username": u.name, "user_scope_id": u.scope, "active": u.enabled}
            for u in users.all()]

set_account_directory_resolver(accounts)
```

A LOOKUP, not a guard, so the polarity differs from the allowlist: a raising
resolver is read as an empty directory rather than a refusal, because the only thing
built on it is finding somebody to invite, and "nobody found" is already the safe
outcome. Names are compared case-insensitively; an inactive account is listed but
never resolved for an invitation; nothing is cached here. Unregistered means no
directory: `Room.invite_account` still works with a scope you resolved yourself, and
the name-based lanes (`vaf a2a invite --account`, the agent's `room_invite` with
`account`) answer "no such account" for every name but the owner's.

Runnable demonstration: part 6 of
[examples/07_tool_caller_and_authorizer.py](../examples/07_tool_caller_and_authorizer.py).

---

## Rooms: several agents in one conversation

A room is a group chat between agents. Some of them may not be VAF, and some may not be
on this machine. The transcript is kept on the machine that hosts the room, encrypted
the way conversations are, and it is readable in canonical order by anybody who joined.

The full wire contract is [A2A_PROTOCOL.md](agents/A2A_PROTOCOL.md); this section is
what the facade gives you.

```python
import vaf

room = vaf.Room.create(kind="round", owner_scope="tenant-a", topic="Deploy talk")

# Join with the DERIVED handle, not a minted one: it is a function of who you are and
# which room it is, so it survives a restart with no index to keep in sync, a re-join
# lands on the same seat, and `joined_rooms` below can find this room at all.
key = vaf.participant_key("agent", "tenant-a")
me = room.join(display="MyApp", scope_id="tenant-a",
               peer_id=vaf.derive_peer_id(key, room.room_id))

room.say(me, "anyone looked at the logs?")
text = "@Bob the logs, please"                 # a leading @Name wakes ONE member and the
room.say(me, text, to=room.addressee(text))   # others read along; to_peer=<handle> when known
room.react(me, frame.id, "+1")                # an emoji on one message; wakes nobody
for entry in room.transcript():
    print(entry["label"], vaf.describe_room_entry(entry))
```

Three things the room does for you, so an embedder does not rebuild them:

```python
packet = room.welcome(me)          # who is here, what this role may send, the shared
                                   # folder, open work, and whether the room is still
                                   # waiting to hear what this peer can do
room.report(me, "on it", status="working", reply_to=task_id,
            progress={"done": 3, "total": 5, "step": "writing tests"})
for peer in room.idle_peers(quiet_for_s=3600):
    room.ping(host, peer)          # HOST only, addressed to that ONE peer, and the
                                   # body is shaped by that peer's role
```

`welcome` is what a newcomer would otherwise discover one call at a time; `progress`
turns an unchanging `working` into something a reader can watch; and the check-in is
the room asking a member that has neither read nor written for a while whether it is
still with it - an invitation, never an order, because a room is input and not
authority. `Room.tasks()` and the exported `vaf.fold_room_tasks(frames, labels=...)` answer the same
board from a store or from frames alone, which is what a peer on the wire has;
`Room.votes()` and `vaf.fold_room_votes(frames, labels=..., members=...)` do the same for
the questions a room decides together. When you render or scan a transcript yourself,
`vaf.BOOKKEEPING_KINDS` names the frames that are the room talking about itself (joins,
acks, role changes) - filter on it rather than hand-writing the set, or the next
bookkeeping kind silently lands in your output as if somebody had said it.
`vaf.NON_CONVERSATION_KINDS` is the wider set a surface built for PEOPLE asks about: the bookkeeping plus the room's check-ins (`ping`) and reactions, meaning "nothing was said" for a badge, a learning transcript or a corpus. The event triggers ask it too, which is what put it here: three in-tree surfaces asking one question through a private import.

Runnable end to end in [examples/11_a2a_room.py](../examples/11_a2a_room.py), which needs
no provider, no key and no network.

**Inviting, and being invited.** A foreign agent gets a bearer ticket and a briefing
(`vaf.room_invitation`); an ACCOUNT on the same machine is invited by name and joins
only when it says yes. Both are tickets in one store, so one list answers "who did I
invite and who arrived":

```python
room = vaf.Room.create(kind="round", owner_scope="tenant-a", multi_scope=True)
# or, on a room that already exists and holds one account:
room.open_to_accounts(host)                 # host only; newcomers then read from their join

row = room.invite_account(host, "tenant-b")  # host or leader; returns the pending record
vaf.invited_rooms("tenant-b")                # -> [(room, record)]: the invitee's sidebar
me  = room.accept_invitation("tenant-b", display="Bob")   # admitted, then joined as itself
room.decline_invitation("tenant-b")          # spent, and the answer is kept
room.revoke_invitation(host, row["id"])      # whoever minted it, or host or leader
room.invitations(host)                       # both kinds, with status: pending, accepted
                                             # (and by whom), declined, revoked, expired
```

An account invitation is not a bearer credential: `redeem_ticket` refuses it without
consuming it, so an invitation id shown in a panel opens the room for the one account
it names and for nobody else. `invited_rooms` is the moment before `joined_rooms` - a
surface that lists a person's rooms asks both.

`kind` is `"round"` (peers, nobody commands) or `"chain"` (one leader, workers who
report). What a role may EMIT is enforced by the room at ingest, in one place, so you do
not check it yourself and cannot check it differently.

**Ask what will be stored before you store it.** `room.compose(payload)` returns the
content a submission will actually be written as - `kind`, `to`, `body`, `reply_to`,
`must_understand`, `ext` - with everything the room settles already settled (a ballot's
choice resolved against its vote, a vote's options trimmed, an absent `to` filled in). It
takes no identity and writes nothing.

Its contract is that composing twice changes nothing, and it is worth relying on rather
than working around: `room.compose(room.compose(x)) == room.compose(x)`, and what
`ingest` stores is exactly what `compose` returned. That is what lets a caller commit to
its own words - build the content, be told what it will become, and hand that back - and
it is why a wrapper of your own should ask the room rather than re-implement the same
trimming. A submission whose `to`, `body` or `ext` is not an object raises `RoomError`
here, named field first, instead of failing later inside a constructor.

**Who wrote what, provably.** A room ASSIGNS authorship - `ingest` overwrites `from`
with the admitted peer - which is sound while the machine holding the room is the one
that admitted the connection, and says nothing to anybody reading the transcript
elsewhere. A peer may therefore SIGN what it says, and a reader holding only the
transcript can check it.

```python
room.signing_keys()          # {peer_id: public key}, folded from the join frames
room.verdict_for(frame)      # what a reader may conclude about one frame
room.verify_frames()         # [(frame, verdict)] for the whole room, keys folded once
```

Five verdicts, and the distinctions are the point: `unsigned` (nothing was claimed,
the ordinary case and not a complaint), `valid`, `foreign_key` (a real signature by a
key that peer NEVER published here in a form a reader can check - a frame written into
the wrong lane, or a peer whose client announced a key without signing the
announcement), `invalid` (the only one that accuses anybody) and `unreadable` (a claim
this version cannot parse, which is what a newer scheme looks like to an older
reader). Nothing here raises, and a verdict never removes a frame: a failed signature
downgrades what may be concluded and nothing more.

Five things an embedder should know before building on it:

- **Signing is optional in both directions.** A room where nobody signs behaves
  exactly as it always has, and a peer that has never heard of the field relays and
  renders a signed frame unchanged.
- **VAF signs only for its OWN actors**, the `agent` and `cli` lanes, whose keys come
  out of this machine's keyring. A peer arriving over the wire signs by PRESENTING a
  signature or stays unsigned, and publishes its key by sending a `join` of its own -
  a member may emit one, and the fold takes the last per peer, so rotation needs no
  handshake field. Do not "helpfully" sign for a remote party: a proof
  produced by the machine it is meant to hold to account proves nothing, and it makes
  `valid` mean less than it says.
- **SIGN THE ANNOUNCEMENT, with the key it announces.** This is the one thing a peer
  you write can get wrong in a way no verdict will tell you about. An unattested key
  is not folded, so every frame that peer ever signs reads `foreign_key` and nothing
  says why. `examples/12_a2a_wire_peer.py` has it as `join_announcement()`, in one
  function, for exactly that reason.
- **The key belongs to a peer by a FOLD over the log**, never by a peer record, and
  the fold asks the `join` to prove it. A record is mutable and lives on the host's
  disk; a join frame does not. But position alone was never enough: a public key is
  public, so a host that writes the log could otherwise copy one peer's key into
  another peer's `join` and file the first peer's signed frames under the second
  handle. If you build your own membership view, read the keys the same way -
  attested, last one wins, an unattested key changing nothing either way - or do not
  read them at all.
- **What it buys is bounded, and the bound is worth knowing.** A signature binds
  CONTENT, the ROOM and the peer's own HANDLE to a key. `seq`, `lamport`, `ts`, `id`
  and `role` are not covered, because the sender controls none of them - it cannot
  sign a sequence number it learns only after speaking. A handle it can: it is given
  once at admission and kept, which is why it is covered and they are not. So the
  content of a conversation is tamper-evident and its ORDER is not: rewriting a stored
  frame's `lamport` or `role` leaves the verdict at `valid`.
  If you render a verdict beside a role, do not let the pairing suggest the role was
  signed; the authority on a role is the fold over `join`, `role` and `leave`.

The canonical byte form is specified in
[docs/agents/A2A_PROTOCOL.md](agents/A2A_PROTOCOL.md), precisely enough to reproduce -
`examples/10_a2a_reference_peer.py` and `examples/12_a2a_wire_peer.py` both implement
it from that text and import nothing from VAF, and a test pins all three to the same
bytes. `vaf.core.a2a.signing` is deliberately NOT on the public facade yet: everything
an embedder has needed so far is reachable through `Room`, and nothing has yet
measured a need to mint or check a signature outside one. Say so if you hit that
wall - a named boundary is easier to move than a guess.

**Deciding together, with a clock.** `room.open_vote(...)` puts a question, `room.cast(...)`
answers it, and `vaf.fold_room_votes(frames, labels=..., members=...)` folds the tally the
same way from a store or from frames alone. A vote ENDS by itself: the room reminds a
member that has not answered after a minute, and a minute later plus one more stops
waiting - or ends at once when everybody has answered. Two calls drive it, and an
embedder that wants the lifecycle must run them on a timer of its own:

```python
room.conclude_votes(host)                     # writes ONE `tally` frame per ended vote
for peer, vote in room.vote_reminders():      # who still owes a ballot, once each
    room.remind_vote(host, peer, vote)
```

Both are HOST-only, like `close` and `kick`: the machine holding the room is the one that
may say how a vote ended. Nothing runs them for you - VAF's own product calls them from
its runner loop every fifteen seconds, and a host that never calls them has votes that
stay open, which is a defensible choice as long as your surface does not draw a countdown.

**Who belongs to whom.** A person and their agent are two members of a room by design
(the `cli` and `agent` lanes of one account), and with several accounts in one room the
useful question becomes which two of them are one household. `room.pairs()` answers it by
RECOMPUTING each handle from an account the room admits and accepting the pair only when
it comes out identical:

```python
for peer, entry in room.pairs().items():
    print(peer, entry["kind"], entry["partner"])   # human | agent | unknown
```

Nothing is read from a member's own file, and that is the whole point: a member writes
its own record, so a `speaks_for` field in one would be a peer naming its own partner.
Where no derivation reaches - a guest that redeemed a ticket carries no account at all -
the answer is `unknown` rather than a guess, unless the transcript carries the answer:
an agent's `join` may hold its OWNER's attestation, a signature by the owner's room key
over the agent's handle and key, and `pairs()` reads those too, marked
`proof: "attested"` beside the derivation's `derived`. A person becomes a member only
when they first act in a room, so `partner: ""` on your own agent is the ordinary
starting state.

```python
vaf.fold_room_owners(frames, room_id)   # {agent peer: owner peer}, from frames alone
```

is the same fold for a reader that has frames and no store - a peer on the wire, which
has no accounts to derive from and is the reason the answer had to be in the log. The
rules it applies are the keys' (see Signing above): the agent's `join` must be attested
or the block is not read, the owner's key must be the one the owner's own attested `join`
bound or the claim binds nothing, and the last attested `join` per agent decides. VAF
puts the block on its own agent's `join` at admission, so your reader folds the host's
household without the host doing anything for you; a household on another machine
attests its agent with the guest client's `attest` verb. What it proves is bounded and
worth saying to your users: whoever holds the key that signed the owner's own words in
this room vouched for this agent. It grants the agent nothing, and it does not expire.

**A room across accounts** is off by default and takes only the accounts it was told to
take: `Room.create(..., multi_scope=True)` opens one and `room.admit(identity, account)`
lets one in (host or leader only). Knowing a room's id admits nobody - ids travel in
invitations, in prompts and in log lines. Two consequences to plan for: every member
reads everything said in such a room (`Frame.addresses` is a routing hint, not a
boundary), and a new member starts reading at its own join rather than receiving the
history of people it has never met.

**Finding the rooms one participant is in**, which is what a sidebar needs:

```python
key = vaf.participant_key("agent", "tenant-a")     # or "cli" for a person
pending = vaf.unread_counts(key)
for joined, identity in vaf.joined_rooms(key):
    print(joined.room_id, identity.role, pending.get(joined.room_id, 0))
```

`participant_key` matters more than it looks. It separates the HUMAN from the AGENT: the
same account owns both and they are two different actors in a room, so without the lane
"send my agent in" and "I am in myself" collapse into one member and whoever spoke last
appears to be the other. A browser and a terminal in front of the same person are ONE
actor and share the `cli` lane.

That split has a consequence the moment your agent can start a SHELL. `vaf a2a` answers
as the machine owner on the `cli` lane - deliberately, since anyone who can run it can
run `vaf` - so an agent reaching for a shell command instead of its own tool writes under
its user's handle, and a room that names who did what then credits the person. If your
runner gives an agent a shell while it is taking a room turn, hand the lane down for that
one room:

```python
from vaf.core.a2a.room import ROOM_ACTOR_ENV, participant_key, room_actor_value

os.environ[ROOM_ACTOR_ENV] = room_actor_value(room.room_id,
                                              participant_key("agent", tenant))
# ... run the turn ...  and restore it on EVERY exit path: the variable is
# process-wide, and a shell can outlive the turn that started it.
```

The room id travels with the key so a stale hand-down answers nowhere else. These three
names are not on the facade: it carries what surfaces outside the room package were
measured to reach for, and this contract has two consumers so far, both inside the tree
(the agent runner writes it, the CLI reads it). Import them from `vaf.core.a2a.room`
rather than rebuilding the string - the parse on the other side is ours, not yours.

**Inviting somebody else's agent** returns the credential and the instructions together:

```python
row = vaf.room_invitation(room, me, display="Codex")
print(row["briefing"])        # the block a human pastes into the other agent
```

The briefing is generated, including the paragraph naming what that role may send, which
is read off the same table that refuses. Hand it over unchanged.

**What a room does NOT do, and this is the load-bearing sentence:** it hands out no
tool, lifts no restriction, and carries no identity into any tool funnel. A `directive`
arriving in a room is INPUT, never a warrant. If you act on what a room says, you are
making that decision in your own code under your own identity - which is why frames from
foreign agents should be treated as untrusted input, exactly like model output.

Errors worth catching: `vaf.RoomError` for anything about the room's rules (role, kind,
budget, ticket, a closed room), `vaf.StoreError` when a room does not exist, and
`vaf.UnsafeName` when an id came from somewhere you do not control.

### The boundaries, stated plainly

- **The room is not a session.** Do not put a room id through a session loader: sessions
  rewrite their whole message list on save, which with N writers is the lost update the
  room store exists to avoid. Rooms are write-once files precisely to escape it.
- **A room is a finite conversation.** One encrypted file per frame means reading a room
  decrypts N files. Thousands of frames will be felt.
- **A cross-machine join is one command now.** `vaf a2a join <room> --ticket <t>
  --url wss://...` speaks the socket (after `vaf a2a trust` pinned the host's
  authority), and an embedder writing its own peer uses the same client the CLI
  does: `vaf.RemoteRoom.connect(url, credential)` performs the handshake, yields
  the backlog and live frames, and submits payloads for acks. Keep the `seat` the
  first welcome hands over - the ticket is spent on arrival, and the seat is the
  only way back in. `vaf.RemoteRefused` carries the close code and a sentence.
- **Cross-tenant rooms are off by default.** `Room.create(multi_scope=True)` opts in, and
  the reason it is not the default is in the protocol document.

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

**Background-thread crashes:** the framework starts threads on your behalf
(bounded tool runs, sub-agent IPC), and an uncaught exception in a thread
does not reach your call stack - CPython's default `threading.excepthook`
prints it to stderr and returns, so in most deployments the only trace
scrolls away. Call `vaf.install_thread_excepthook()` once at startup to route
every uncaught thread exception into `crash_<date>.log` in the VAF log store
(always written, regardless of `debug_logs_enabled`; see
[DEBUGGING.md](DEBUGGING.md)). It chains: a hook you installed before it
still runs, and stderr printing is preserved. VAF's own entry points install
it on every lane; as a library it is opt-in, because a process-global hook is
yours to decide.

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

If your front end renders one bubble per exchange, declare the TURN as well
(`vaf.core.subagent_ipc.set_current_turn_id`, read back with
`get_current_turn_id`). It is the same per-thread mechanism one level finer,
and it is what lets an event describe WHICH exchange produced it: VAF's own
`notify_file_created` stamps it automatically, so a file written from inside a
tool can be attached to the answer of that turn instead of to whichever answer
happens to be newest when the event arrives. It crosses a process boundary as
`VAF_TURN_ID`, exactly like `VAF_SESSION_ID`, so work that finishes after its
turn (a spawned coder) is still addressed correctly. Skipping it is not an
error - events simply carry `None` and your UI has to fall back to arrival
order, which is the bug this exists to remove.

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

- `from vaf import Agent` - the façade: `Agent(config=..., system_prompt=..., user_scope=..., session=...)`, `.run(prompt, on_token=...)`, `.run_async(...)`, `.complete(prompt, ...)`, `.add_tool(tool)`, `.on_event(cb)`, `.on_compaction(cb)`, `.save_session()`, `.core`.
- `vaf.markers` - the special-return-value constants.
- `vaf.CoreAgent` - the engine, for advanced embedding.
- `CoreAgent.chat_step(user_input, ..., memory_context=...)` - the documented turn
  call, including `memory_context` as the seam for per-turn retrieved context (see
  "Injecting retrieved context"). The parameter NAMES and defaults are the promise;
  the section text the engine wraps `memory_context` in is not, and neither is the
  return value beyond the contract in [CORE_AGENT.md](CORE_AGENT.md). A turn you feed
  in is taken as the user speaking in their own chat - it answers whatever question
  the agent has open on that user (the ask-first latch, a background question that
  is waiting for a reply) - unless the agent was constructed with
  `run_kind="automation"` or `"thinking"` (see [CORE_AGENT.md](CORE_AGENT.md)),
  which is how a scheduler of your own keeps its prompts from being filed as the
  user's answers. A per-call origin parameter does not exist yet; that is a named
  boundary, not an oversight - VAF's own runner marks its synthetic turns with two
  attributes on the agent, and one public value replacing them is a facade decision.
- `Agent.on_compaction(cb)` / `CoreAgent.set_compaction_hook(hook)` - the seam after a
  structural compaction, on both paths that compact. `hook(info)` gets `{"before",
  "after", "tokens", "session_id"}` and may return one string, appended as one system
  note: the place for what a summary loses, such as a task board or a running job. It
  cannot edit the history and never sees a reply. Bounded by
  `CoreAgent.COMPACTION_HOOK_SECONDS` (a timeout is "nothing to add") and forgiving (an
  exception is swallowed and logged), like the event sink and unlike the authorizer.
  What it does NOT offer, said plainly: a hook at the END of a turn. `chat_step` has
  27 exits and no single one, so a turn-end seam would first need a single exit; the
  measurement that earns it is a second consumer wanting the same place.
- `vaf.RoomTriggerWatch` - for a scheduler of your own: the "is it due" decision for an
  automation that runs on a room EVENT. `watch.tick(tasks)` takes objects with `id`,
  `frequency == "on_event"`, `user_scope_id` and a `trigger` dict (`kind`: `room_message`
  or `room_reaction`, `room_id`, optional `match`, `emoji`, `from`, `cursor`) and returns
  the ones that are due with the frames that made them so; persist `hit.newest` back as
  `trigger["cursor"]` before running. The loop guard (the owner's own agent never fires
  a trigger), the membership guard and the start-at-newest cursor are inside it, so a
  scheduler built on it does not have to know them. See
  [AUTOMATIONS.md](platform/AUTOMATIONS.md), "Event triggers".
- `file_encryption_enabled` as the switch between the two at-rest modes, and the
  promise that turning it off leaves the whole store readable: encrypted files
  still decrypt, and plaintext is tolerated on read again. A store that is
  provably all ciphertext stops tolerating plaintext (`allow_plaintext_at_rest`),
  which is why the promise is stated for the OFF switch and not for both values.
  The file format and the key locations are documented but not frozen.
- `cross_chat_hint_enabled` / `cross_chat_hint_k` as the OFF switch for the engine
  reading the caller's other sessions. That the switch exists and turns the
  behaviour off is the promise; the retrieval behind it is not.
- `BaseTool` - the tool contract, including the `identity_kwargs` declaration
  and `self.log(message)`.
- `vaf.VoiceTurnEngine` / `vaf.TurnOutcome` - the voice turn pipeline: the
  constructor's documented seams, `turn(wav, ...) -> TurnOutcome`, the
  outcome's documented fields and the five INITIAL state-dict keys are the
  promise; the keys the engine adds at runtime are not - hold the dict,
  do not read its insides.
- `vaf.user_jail` - turning a declared identity into a file boundary by hand. Prefer the
  `file_access` declaration on your tool, which does it on every lane; this remains
  exported for tools that need the boundary around something other than a whole `run()`.
  Entering the write-mode jail needs no pre-provisioned per-user directories - the
  boundary is computed, not created, so a fresh tenant costs nothing to confine.
- `vaf.contained_path(root, relative="", *, must_exist=False)` /
  `vaf.safe_entry_name(name, *, allow_hidden=False)` / `vaf.PathEscape` - keeping a
  path that came from OUTSIDE inside the directory it may touch. The jail above
  answers which roots a caller owns; this answers whether a fragment or a single
  name stays inside the one root you opened it against. Containment is decided on
  RESOLVED paths, which is the whole point: a prefix comparison accepts a symlink
  that lives inside the root and points anywhere on the host, and the write then
  lands at the link's target while the string test still says "inside". VAF's own
  workspace endpoints shipped that mistake, which is why this is a primitive and
  not an example. `contained_path` answers for a path that does not exist yet, so
  you can decide BEFORE creating; `must_exist=True` adds the existing-directory
  requirement a listing needs. Both raise `PathEscape` (a `ValueError`), and
  refuse rather than trim: silently rewriting `a/b` to `b` hands the caller a
  different target than the one they named. A ROOTED fragment is refused in either
  separator convention, whatever the host runs: `os.path.isabs` answers only for the
  host, and its answer for a driveless rooted path changed in Python 3.13 on Windows,
  so a fragment carrying the SENDER's convention would otherwise be read as plain
  relative text and joined onto your root.
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
- `vaf.extract_pdf_markdown(path, max_pages=None, ocr_fallback=True, *,
  first_page=1, cancel=None)` - PDF to Markdown with honest coverage facts.
  The result dict is the contract: `markdown`, `total_pages`, `pages_read`,
  `first_page`, `truncated`, `used_ocr`, `method` (one of `pdfplumber`,
  `pypdf2`, `ocr`), `ocr_unavailable_reason`
  (and `num_pages` as a backward-compat alias of `total_pages`). Pages are
  streamed - memory stays flat over any document size - and page markers carry
  absolute numbers, so a slice read cites correctly. `max_pages=None` reads
  everything; `cancel` is polled once per page (pass your own check from a
  background job - the default only fires on VAF's in-tool lanes). When a
  scanned document cannot be OCRed, `ocr_unavailable_reason` names why
  (missing binary, missing language data, timeout) instead of the result
  looking like an empty document. Needs the `vaf[pdf]` extra at call time;
  `import vaf` stays cheap.
- `vaf.install_thread_excepthook()` - route uncaught background-thread
  exceptions into `crash_<date>.log` (see "Background-thread crashes" above).
  The promise: chains the previously installed hook, idempotent, never raises,
  writes regardless of `debug_logs_enabled`, does not log `SystemExit`.
- The `vaf.tools` entry-point group.

Everything else under `vaf.core.*` is internal and may change between releases.
Two that are easy to mistake for surface because this page names them:
`vaf.core.cross_chat` (the cross-chat retrieval) and `vaf.core.session.SessionManager`
(including `iter_owned_sessions` / `list_owned`, the strict per-scope session walk).
Both are engine-internal by design: VAF exposes no session enumeration on the
façade, and no embedder has asked for one. If you need it, say so - the export is
one lazy branch away, and it is not being added on speculation.
`vaf.ToolCaller`, `vaf.ToolRequest`, `vaf.set_account_allowlist_resolver`,
`vaf.extract_pdf_markdown` and `vaf.install_thread_excepthook` are the deliberate
exceptions: they live in `vaf.core` but are re-exported on the façade, and the
façade names are the ones to import.

### Breaking-change tests you can run in your own CI

The stability list above is executable: [tests/contract/](../tests/contract/)
pins it as an offline pytest suite - no network, no API keys, no Docker -
designed to be vendored. Copy that directory out of the VAF tag you build
against into your own CI, and run it against every VAF version you consider
upgrading to; a failure means that version breaks the surface this page
promises, before it reaches your integration. Run standalone, the suite
isolates itself from your real home and config directories first. The how-to
(and the maintainers' update policy that keeps the suite current as this
surface grows) lives in [tests/contract/README.md](../tests/contract/README.md).
