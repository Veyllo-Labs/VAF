# Dynamic Tool Routing Architecture

This document details the **Dynamic Tool Router** and the three **Tool Calling 2.0** features of the Veyllo Agentic Framework (VAF). All features are **provider-agnostic** — they work with OpenAI, Anthropic, Google, local models, and OpenRouter without any provider-specific API extensions.

---

## 1. The Problem: Context Saturation

Modern agents often have access to dozens of tools (File System, Web Search, Git, Automation, Coding, etc.).

1. **Token Cost:** Defining a single tool in a JSON Schema (required for function calling) takes 150–500 tokens.
2. **Scale:** With 20+ tools, the definitions alone can consume 4,000+ tokens.
3. **Distraction:** Overloading the system prompt with irrelevant tools increases the chance of the model hallucinating tool calls or getting confused.

### The "Phantom Consumption"
Before the Router was implemented, the Agent had to reserve aggressive amounts of space for tools, often triggering "Proactive Compression" even when the conversation was short.

---

## 2. Tool Calling 2.0 — Three Provider-Agnostic Features

VAF implements all three concepts from Anthropic's "Advanced Tool Use" research, but in a **provider-agnostic** way so every backend benefits equally.

| Feature | Anthropic API variant | VAF provider-agnostic variant |
|---|---|---|
| **Tool Search** | `defer_loading: true` + tool search tool (beta) | Hybrid Router + `search_tools` tool |
| **Programmatic Tool Calling** | `code_execution` + `allowed_callers` (beta) | `python_sandbox(with_vaf_tools=True)` + `ToolBridgeServer` |
| **Tool Use Examples** | `input_examples` in tool JSON (beta) | `input_examples` embedded in description text |

---

## 3. Feature 1 — Tool Use Examples (`input_examples`)

### What it does
Every tool can optionally declare 1–3 concrete example calls. These are embedded as plain text into the tool's description so **every provider sees them** via the standard description field — no API change needed.

### How to add examples to a tool

```python
from vaf.tools.base import BaseTool

class MyTool(BaseTool):
    name = "my_tool"
    description = "Does something useful."
    input_examples = [
        {"query": "Berlin weather today"},
        {"query": "population of Tokyo", "language": "de"},
    ]
```

`BaseTool.get_description_with_examples()` renders this as:

```
Does something useful.

Examples:
  my_tool({"query": "Berlin weather today"})
  my_tool({"query": "population of Tokyo", "language": "de"})
```

### What the TOOLS property does with it

`agent.py`'s `TOOLS` property calls `get_description_with_examples()` instead of the raw `.description`. For small context windows (`n_ctx < 32000`) descriptions are truncated to 150 chars; the budget widens to 300 chars when examples are present so at least one example survives. (Note: `Config.load()` clamps `n_ctx` to at least 32768, so the small-context branch is effectively inactive on standard installs.)

### Tools that already have examples

| Tool | Examples |
|---|---|
| `python_sandbox` | basic calc, `with_vaf_tools`, `packages` |
| `webfetch` | Python docs URL, GitHub repo |
| `send_mail` | plain email, email with attachment |
| `get_contact` | `name="Max"`, `name="Anna Müller"` |
| `search_tools` | calendar, whatsapp, read file |

To add examples to any other tool, just add the `input_examples` class attribute — no other changes needed.

---

## 4. Feature 2 — Tool Search (provider-agnostic)

### Hybrid Router (`_route_tools`)

VAF already solves the "load only relevant tools" problem without any Anthropic-specific API. The `_route_tools` method runs before every main model call:

```mermaid
graph TD
    A[User Input] --> B{Heuristic Check}

    subgraph "Stage 1: Hybrid Routing"
    B -- "Contains 'folder size'?" --> C[Force: librarian_agent]
    B -- "Contains 'commit'?" --> D[Force: git_tools]
    B -- "Contains 'weather'?" --> E[Force: web_search]

    A --> F(Router LLM Call)
    F -- "Reasoning" --> G[LLM Selected Tools]

    C & D & E --> H[Forced Tools]
    H & G --> I[Combined Tool List]
    end

    subgraph "Stage 2: Context Assembly"
    I --> J{Filter & Deduplicate}
    K[All Tool Definitions] --> J
    J --> L[Optimized System Prompt]
    end

    L --> M[Main Model Inference]
```

**Heuristic keywords → forced tools:**

| Keywords | Forced tools |
|---|---|
| "folder size", "disk usage", "storage" | `librarian_agent` |
| "Google Drive", "OneDrive", "cloud" | `librarian_agent` |
| "calendar", "termin", "meeting", "event", "reminder" | `list_calendar_events`, `create_calendar_event` |
| "termin ändern", "reschedule" | `update_calendar_event` |
| "termin löschen", "cancel" | `delete_calendar_event` |
| "code", "script", "bug", "fix" | `coding_agent`, `git_status`, `git_add_commit` |
| "git", "commit", "push", "pull" | `git_status`, `git_add_commit`, `git_log` |
| "research", "recherche", "analyse" | `research_agent`, `web_search` |
| "search", "find", "news", "weather" | `web_search` |

### `search_tools` — on-demand discovery tool

In addition to the router, the model can itself call `search_tools` to discover tools it doesn't know about:

```
Model: search_tools(query="calendar appointment")
→ Returns:
    Tools matching 'calendar appointment':
      create_calendar_event: Create a new calendar event or appointment.
          create_calendar_event(title: string, start: string, [duration: integer])
      list_calendar_events:  List upcoming events from the calendar.
      update_calendar_event: Modify an existing event.
```

**Scoring:** +2 per query token matching the tool name, +1 per token matching the description. Results capped at 10. The top 3 matches additionally carry a compact call signature (required parameters first, optional ones bracketed; rendered by `format_tool_signature` in `vaf/tools/base.py`) so a discovered tool is callable without guessing parameters. If no matches, shows first 20 tools alphabetically with a "… and N more" trailer (signature-free).

**Format contract:** the post-hook parser is the shared `extract_discovered_tool_names()` in `vaf/tools/search_tools.py` — match lines stay `name: desc`; signature lines sit on their own indented line and their pre-colon part contains `(`, so they can never be mistaken for a tool name. The query echo in the header is capped and the total output self-caps under `execute_tool`'s 2000-char truncation (signature lines are dropped first). Round-trip guard: `tests/test_search_tools_signatures.py`.

**Post-execution hook (`Agent._chat_post_dispatch`, wired into the pipeline as the chat lane's `after_dispatch`):** After `search_tools` returns, the discovered tool names are immediately added to `_active_tools` so the model can call them in the very **next turn** without another router round-trip.

**Always available:** `search_tools` (and `list_tools`) are injected into every restricted tool set: the discovery-only fallback (router found no tools), CORE_TOOLS (tight context), and the emergency fallback list — so the model always has a discovery path.

**Tool cap (`router_max_tools`):** After the router selects tools (and core/discovery tools are added), the list is capped at `router_max_tools` (default: **12**). `list_tools` and `search_tools` are **always kept** and do not count against the cap. This prevents context pollution when many tools are registered.

```json
// ~/.vaf/config.json
{ "router_max_tools": 12 }
```

Range: 1–100. Raise it if agents report missing tools; lower it to reduce token overhead.

**Reasoning model compatibility:** When the router uses a reasoning model (DeepSeek Reasoner, R1) the tool selection often lands inside `<think>…</think>` blocks rather than in the response content. The parser strips think-tags first, then falls back to scanning the full raw response (including reasoning) for tool name substrings — so routing works correctly regardless of model type.

### `_active_tools` state machine

| Value | Meaning |
|---|---|
| `None` | Use ALL registered tools (router failure / retry / internal step) |
| `[list]` | Use only these tool names (normal operation, post-router) |

**Visibility in the Web UI:** The selected tools (e.g. `LLM-based: list_calendar_events` or `Script-based: web_search`) are shown in the chat as a Router system step so you can see which tools were chosen for each turn. See [WEB_UI.md](../web-ui/WEB_UI.md) → Workflow Steps / System Steps.

---

## 5. Feature 3 — Programmatic Tool Calling (`with_vaf_tools=True`)

### Concept

The model calls one tool (`python_sandbox`) with a code block that internally calls multiple other VAF tools. Only the **final `print()` output** of the script returns to the model context. Intermediate tool results are consumed entirely inside the running script — they never become chat messages.

This matches Anthropic's "Programmatic Tool Calling" semantics and works with **every backend**.

### Usage

```python
python_sandbox(
    code="""
import vaf_tools

# Call any VAF tool — results stay inside the script
weather = vaf_tools.call("web_search", {"query": "Berlin weather"})
contact = vaf_tools.call("get_contact", {"name": "Max"})

# Only this line reaches the model context
print(f"Weather: {weather[:200]}\nContact: {contact}")
""",
    with_vaf_tools=True,
)
```

To see all callable tools from inside the script:
```python
import vaf_tools
print(vaf_tools.available())
```

### Architecture

```
Host (VAF process)                          Docker sandbox
──────────────────────────────────────────  ──────────────────────────────
ToolBridgeServer (random port, daemon)  ←── vaf_tools.call("web_search", …)
  token check (per-execution secret)         HTTP POST /call  (JSON)
  → agent.execute_tool("web_search", …)      ← JSON {"result": "..."}
  → return str result                        script continues with result
                                             …
                                             print("final answer")  → model
```

**Files:**
- `vaf/core/tool_bridge.py` — `ToolBridgeServer` + `_BridgeHandler` + stub source
- `vaf/tools/python_sandbox.py` — `with_vaf_tools` parameter + `_run_with_bridge()`

### Security

| Property | Detail |
|---|---|
| Token | `secrets.token_hex(16)` per execution — rejected on mismatch (HTTP 403) |
| Binding | `0.0.0.0` on host, random free port — not exposed beyond local network |
| Trust gates | All calls go through `agent.execute_tool()` — full VAF gate pipeline applies |
| Cleanup | `bridge.stop()` in `finally` block — no port leak even on crash |

### Host gateway resolution

| OS | Bridge address |
|---|---|
| All | `host.docker.internal` (built in on Docker Desktop / Colima on Mac/Win; `extra_hosts: host-gateway` injection on Linux via `docker-compose.memory.yml`) |

---

## 6. Context Consumption Analysis

### Without Router (legacy)
User: "What is the weather?"
- All 25+ tools in context → **~3,500–6,000 tokens**

### With Hybrid Router (current)
User: "What is the weather?"
1. Heuristic matches "weather" → forces `web_search`
2. Router LLM confirms `web_search`
3. Only `web_search` schema sent → **~200 tokens**

**Result:** >90% reduction in system prompt overhead.

---

## 7. Fallback Mechanisms

| Situation | Behaviour |
|---|---|
| Router LLM fails | `_active_tools = None` → ALL tools loaded (fail-safe) |
| Router returns empty | Context OK: discovery-only (`list_tools`, `search_tools`). Context tight (e.g. >75%): CORE_TOOLS subset. |
| Main model retry | `_active_tools = None` → full tool reload |
| Emergency (internal step) | Context >80%: minimal subset (`web_search`, `memory_search`, `list_tools`, `search_tools`, …) |

**CORE_TOOLS** (used when context is tight and router returns nothing):
`web_search`, `memory_search`, `memory_save`, `list_tools`, `search_tools`,
`update_intent`, `update_working_memory`, `read_file`, `list_files`,
`coding_agent`, `librarian_agent`, `research_agent`

---

## 8. Tool-use debug log (user-scope isolation)

When **Debug Logs** are enabled (on by default; disable via `debug_logs_enabled: false` in `~/.vaf/config.json`), each tool execution is written to `logs/tool_use_YYYY-MM-DD.log` with:

- `tool` — tool name
- `session_id` — current chat session ID
- `user_scope_id` — user-scope UUID used for RAG/memory isolation (may be empty on local/single-user)
- `args_preview` — truncated arguments (first 200 chars)

Use this to verify which user scope UUID is used for each tool call when debugging local vs multi-user or isolation issues. Log files are dated and cleaned by the garbage collector like other app logs.

---

## 9. Declarative Tool Contract

VAF tools declare a centralized contract directly on the class. All fields have safe defaults so they're opt-in, but every tool should set them explicitly for clarity.

### Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `permission_level` | `"read"` \| `"write"` \| `"dangerous"` \| `"system"` | `"read"` | Access level. `dangerous` → confirmation gate. `system` → skips the legacy confirmation gate entirely (for internal/agent tools where prompting would be disruptive). For the **main agent**, `write`/`dangerous` (except `python_sandbox`) also require a plan in working memory before running — see the plan gate in [CONTEXT_MANAGEMENT.md](../memory/CONTEXT_MANAGEMENT.md). |
| `side_effect_class` | `"none"` \| `"reversible"` \| `"irreversible"` | `"none"` | Impact of the tool. Added to the confirmation message when `dangerous`. |
| `admin_only` | `bool` | `False` | When `True`, the tool is **hard-blocked** for non-admin users in the shared dispatch pipeline (`vaf/core/tool_dispatch.py`), so it applies to every caller and not only to chat. The check uses the caller's role and scope; in a chat turn those are `_current_user_role` and `_current_user_scope_id`, set on the agent before each turn. This is a role-based check - distinct from `channel_restrictions` which is source-based. |
| `channel_restrictions` | `tuple[str, ...]` | `()` | Sources where the tool is blocked. Common values: `"telegram"`, `"whatsapp"`, `"discord"`, `"channel"` (generic chat). |
| `identity_kwargs` | `tuple[str, ...]` | `()` | Which parts of the CALLER's identity the dispatcher assigns into `run()` before dispatch. Valid keys: `user_scope_id`, `username`, `user_role`. Declare exactly what the tool consumes - a tool that declares nothing receives nothing, which is the safe direction. This is what makes identity work for a tool registered through `Agent.add_tool()` and not only for built-ins; before it, the dispatcher matched on hardcoded tool NAMES. Values are **assigned, never defaulted** - the arguments start out as whatever the model produced. Declaring says who is calling; confining file access to them is `vaf.tools.filesystem.user_jail` (see [USER_ISOLATION.md](../security/USER_ISOLATION.md)). |

### Evaluation order

Two things happen on a tool call, and they belong to different owners. The **shared
pipeline** (`ToolCaller` in `vaf/core/tool_dispatch.py`) is what the chat turn gets, and what
an embedder holding nothing but a tool registry gets. The **chat lane** adds turn-specific
stages on top, and it adds them as hooks at the points below rather than by keeping a
dispatcher of its own - a second dispatcher is how identity assignment came to exist in five
places and be correct in one.

**The workflow engine is NOT on this pipeline yet, and the difference is security-relevant.**
It builds no `ToolCaller`; it imports two pieces of it - `run_tool_bounded` for execution
bounds and `assign_declared_identity` for identity - and calls the tool itself. A workflow
step therefore gets no `admin_only` block, no `channel_restrictions` check, no account
allowlist, no confirmation gate, no argument repair, no authorizer and no
`tool_start`/`tool_end` pair. Do not read the
order below as describing what a workflow step does. Bringing that lane onto the pipeline is
open work; until it lands, "which door did the caller come through" is still a security
answer for workflows.

**The shared pipeline, in order:**

1. **`admin_only` check** - if `admin_only == True` and the caller is not an admin, the tool is hard-blocked and **nothing is emitted**: no `tool_start`, no `tool_end`. A consumer must never see a blocked tool reported as having run. `is_admin` comes from the caller's role and scope through the shared `is_admin_identity` rule.
2. **Channel check** — if the session source is in `channel_restrictions`, the tool is rejected immediately (regardless of user role) — **unless** `channel_tools_unrestricted` is set (default on), which grants messaging-channel sessions the same tools as the main agent (channel restrictions **and** the per-call confirmation gate are lifted; the `admin_only` check above still applies).
2a. **The account allowlist** - which tools this ACCOUNT may use, answered by the process-wide resolver the application registered with `set_account_allowlist_resolver` (the harness registers its auth-DB resolver, `vaf/auth/permissions.py`, at `vaf/main.py` import). Scopeless callers and admins are exempt and never reach the resolver; nothing registered means unrestricted; a RAISING registered resolver refuses (fail-closed, the authorizer's polarity - the harness resolver catches its own DB errors and stays fail-open internally). A blocked tool ends with `Security Error: ...` and **emits nothing**. Sits after the hard blocks and BEFORE the authorizer, so an account-level ban cannot be lifted by an `allow()`. Guard: `tests/test_tool_account_allowlist.py`, wiring: `tests/test_account_allowlist_wiring.py`.
2b. **The application's authorizer** - if the embedder attached one with `set_tool_authorizer`, it is consulted here: after the hard blocks and the account allowlist, so an `allow()` cannot reach a tool policy already refused, and before the gate, so a `deny()` answers at once instead of parking a refused call on a dialog. `deny()` ends the call with `Security Error: ...` and emits nothing at all. `ask()` and `allow()` both reach into step 3 and are described there. See [EMBEDDING.md](../EMBEDDING.md).
3. **Confirmation gate** - if `permission_level == "dangerous"`, the caller is asked (once / always / cancel). `side_effect_class == "irreversible"` adds a warning line. `permission_level == "system"` bypasses the gate entirely. A standing grant (`policy == "allow"`, or a trusted directory) runs the tool **silently** - no gate event at all. Both halves of that sentence are conditional on there being no authorizer: `req.ask()` gates a tool of ANY permission level and ignores standing grants, while `req.allow()` skips the gate for a `dangerous` tool without writing anything durable. When nobody can answer (headless, or an embedder that passed no asker) the call is refused with a string rather than blocking on a person who is not there.
4. **`tool_start` event.**
5. **Input validation & repair** - the model-supplied arguments are validated against the tool's `parameters` schema and common weak-model shape mistakes are repaired (bare string for an array, stringified array, `null` on an optional field, single-key placeholder). Runs on the raw model arguments, before runtime kwargs are injected. See [TOOL_INPUT_REPAIR.md](TOOL_INPUT_REPAIR.md).
6. **Identity injection** - the keys named in the tool's `identity_kwargs` are ASSIGNED from the caller's context, overwriting anything the model supplied under those names. A tool that declares nothing receives nothing. If arguments still violate the schema after repair, the tool is not run and a localized `Tool Error: invalid arguments for '<tool>': <detail>` is returned; that error outranks any refusal a hook raises about a *different* call already in flight.
7. **Bounded execution** - per-tool timeout and stop polling; see [TOOL_SUPERVISION.md](TOOL_SUPERVISION.md).
8. **`tool_end` event**, then truncation of the result (the chat lane cuts at 2000 chars; the workflow engine does not truncate at all, because step outputs are chained into later steps).

**What the chat lane adds**, each at the position where it belongs:

- After the policy check and **before** the confirmation gate, so a refusal cannot be replaced by a gate prompt: the **plan gate** (`write`/`dangerous` tools except `python_sandbox` need a plan in working memory first, answered with `[PLAN REQUIRED]`; skipped for sub-agents and non-interactive runs - see [CONTEXT_MANAGEMENT.md](../memory/CONTEXT_MANAGEMENT.md)), the **working-memory note firewall**, the **proactive-reply mutation gate** (`[CONFIRM REQUIRED]` for stored-state mutations and destructive delegation while the turn is a pickup of a reply that is not a clear affirmative; kill-switch `proactive_reply_mutation_gate_enabled`) and the **ask-first gate** (`[AWAITING USER]` for new `write`/`dangerous` tools and delegations on synthetic drain turns while a blocking question is pending; kill-switch `ask_first_drain_gate_enabled`).
- Before dispatch: **session plumbing** (`_session_id`, `_session_workspace`, `_agent` and the other per-tool runtime kwargs - these are plumbing, not identity, and are not part of the `identity_kwargs` declaration) and the **anti-re-delegation guard**.
- After dispatch and before truncation: the **`search_tools` post-hook** (discovered names join `_active_tools`) and the **`python_exec` fallback**.
- After the events: **router bookkeeping** (`_record_tool_used`), which deliberately does not fire for a call that was blocked before dispatch.

Whare Wananga training (`_ww_training`) skips the turn gates and the confirmation gate, because a probe has no human to ask and a `[CANCELLED]` would be recorded as the tool's real answer. It does **not** skip the policy check: a training run is not an authorisation. Guard: `tests/test_ww_training_skips_the_gate_only.py`.

`ask_user` is a deliberate exception to identity injection: it injects identity only for background runs, because a non-admin's private question would otherwise be delivered to the ADMIN's messenger.

### Examples

```python
# Read-only, safe everywhere
class GetContactTool(BaseTool):
    permission_level  = "read"
    side_effect_class = "none"
    channel_restrictions = ()
    admin_only = False

# Writes to external service, blocked on chat channels
class SendMailTool(BaseTool):
    permission_level  = "write"
    side_effect_class = "irreversible"
    channel_restrictions = ("telegram", "whatsapp")
    admin_only = False

# Dangerous — user must confirm; cannot be undone
class DeleteFileTool(BaseTool):
    permission_level  = "dangerous"
    side_effect_class = "irreversible"
    channel_restrictions = ("telegram", "whatsapp", "discord")
    admin_only = False

# Admin-only, internal — only available in admin sessions, no confirmation gate
class CreateAgentToolTool(BaseTool):
    permission_level  = "system"    # skips confirmation gate
    side_effect_class = "reversible"
    channel_restrictions = ("telegram", "whatsapp", "discord")
    admin_only = True               # hard-blocked for regular users
```

### `admin_only` vs `channel_restrictions` — key distinction

| | `channel_restrictions` | `admin_only` |
|---|---|---|
| Blocks based on | Chat *source* (telegram, web, …) | User *role* (admin vs user) |
| Admin affected? | Yes — admins on Telegram are also blocked (unless `channel_tools_unrestricted` is enabled) | No — admins always pass |
| Use case | Prevent tool abuse via messaging bots | Restrict to elevated-trust sessions |

---

## 10. `coder_only` — Restricting Tools to the Coder Sub-Agent

Set `coder_only = True` on any tool that should **only** be available to the Coding Sub-Agent (`coder.py`), not to the Main Agent.

```python
class BashTool(BaseTool):
    coder_only = True   # Excluded from Main Agent tool list
```

**Tools currently marked `coder_only`:**

| Tool | Reason |
|---|---|
| `bash` | Raw shell — Main Agent delegates to Coder instead |
| `codesearch` | Code-aware search — Coder-specific |
| `linter` | Linting — Coder-specific |
| `context_tools` | Internal Coder context management |

The Main Agent's `_load_tools()` skips any tool with `coder_only = True`. In addition, a
hardcoded `MAIN_AGENT_EXCLUDED_TOOLS` list inside `_load_tools()` excludes a few tools by
NAME (`move_file`, `folder_size`, `bash`, `codesearch`,
`save_thinking_suggestion`) - `write_file` was removed from that list (tool-friction audit):
the main agent now writes single-file artifacts directly, workspace-anchored and
per-user jailed (see `docs/security/USER_ISOLATION.md`). The Coder loads its tools
separately from `vaf/tools/`.

`_load_tools()` discovers tools from four sources, in order:

1. **In-tree** — a `pkgutil` scan of the `vaf/tools/` package (the built-in tools).
2. **Custom tools** — user-uploaded tools from the data directory, via `custom_tools_registry` (`_load_custom_tools()`).
3. **Entry-point tools** — third-party pip packages that register under the `vaf.tools` entry-point group (`_load_entry_point_tools()`); see [EMBEDDING.md](../EMBEDDING.md).
4. **MCP tools** — servers from `mcp_servers.json`, registered as native tools (`_load_mcp_tools()`); see [MCP_INTEGRATION.md](MCP_INTEGRATION.md).

For how tools fit into the wider framework (the tool contract and the public boundary), see [ARCHITECTURE.md](../ARCHITECTURE.md).

---

## 11. `query_llm()` — Making LLM Calls Inside a Tool

`BaseTool` provides a built-in helper for tools that need to call the LLM internally (e.g. to summarize, classify, or generate content as part of their logic):

```python
def run(self, **kwargs) -> str:
    messages = [{"role": "user", "content": "Summarize: " + kwargs.get("text", "")}]
    return self.query_llm(messages, max_tokens=512, temperature=0.3)
```

### What `query_llm()` does internally

1. **Provider detection** — reads the active provider (`openai`, `anthropic`, `google`, `local`, …).
2. **Model resolution** — selects the correct model ID for that provider (e.g. `gpt-4o` vs `claude-sonnet-4-6`).
3. **API backend** — if `use_api_backend=True`, streams the response and returns the full text.
4. **Self-healing** — on HTTP 400/404, retries with a fallback model automatically.
5. **Local server fallback** — if no API key is configured, hits the local OpenAI-compatible endpoint.

Use `query_llm()` instead of calling provider SDKs directly — it stays provider-agnostic and inherits the agent's current model configuration automatically.

---

## 12. Related: Whare Wananga (tool self-learning) & the Action Tag

Two adjacent systems build on the tool layer described here:

- **Whare Wananga** ([WHARE_WANANGA.md](../memory/WHARE_WANANGA.md)) learns per-tool `tool_knowledge`.
  The Settings tools list (`tools_list`) now carries three extra per-tool fields, attached
  server-side in `_attach_learned_states`:
  - `learned_state` — `unlearned` / `learning` / `learned` / `stale`;
  - `requires_config` + `configured` — whether the tool depends on a connection and whether
    that connection is set up (resolved in `vaf/whare_wananga/preconditions.py` from the
    existing `telegram_config` / `discord_config` / `whatsapp_config` / `email_config`).

  The Declarative Tool Contract's `side_effect_class` (Section 9) is also the basis for
  Whare Wananga's safety gating (probe-safe read-only tools vs side-effecting ones, which
  may only be learned via the error/validation path).

  After the router scopes the turn's tools (`_active_tools`), Whare Wananga's **delivery** appends
  each selected tool's learned pitfalls to its schema description (proactive), and re-feeds a failed
  tool's know-how on error (reactive) — see [WHARE_WANANGA.md](../memory/WHARE_WANANGA.md) "Delivery".
  Because `Agent.TOOLS` sits on the hot path of **every** LLM call, the built schema (with the injected
  pitfalls) is **cached** and only rebuilt when the scoping inputs change (active tools, exclusions,
  context size). It was previously rebuilt — re-running pitfall injection per tool — on every access
  (thousands of times per session), which steadily churned memory.
- **Action Tag** ([ACTION_TAG.md](ACTION_TAG.md)) — the agent declares the tool it is about
  to use; a backend parser matches that intent against the loaded tool list.
