# VAF Architecture - Framework and Harness

This document explains *what VAF is* at the conceptual level and draws a clean
line between the two things it actually is: a **framework** (a foundation you
build on) and a **harness** (the product Veyllo builds on that foundation).

If you want the hands-on guide to embedding VAF as a library, see
[EMBEDDING.md](EMBEDDING.md). If you want the end-user product, see the
[README](../README.md). This page is the map that ties them together.

---

## In one sentence

VAF is a reusable agent **framework** (the engine plus a stable public
interface), and VAF the **product** is the reference **harness** built on that
framework - the same way anyone else could build their own.

We built the framework first; our desktop/server/web product is a harness on
top of it. They are intentionally separate, and this document keeps them
separate.

---

## The three layers

```
  ┌─────────────────────────────────────────────────────────────┐
  │  HARNESS / PRODUCT        Desktop app · Web UI · Server ·   │
  │  (the product we ship)    TUI · tray · integrations ·       │
  │                           automations · memory dashboard    │
  ├─────────────────────────────────────────────────────────────┤
  │  FRAMEWORK SURFACE        from vaf import Agent · BaseTool ·│
  │  (the stable contract)    vaf.tools entry points · config · │
  │                           VAF_NONINTERACTIVE + trust        │
  ├─────────────────────────────────────────────────────────────┤
  │  ENGINE / CORE            agent loop (chat_step) · tool     │
  │  (does the work)          dispatch · context management ·   │
  │                           provider abstraction · sub-agents │
  └─────────────────────────────────────────────────────────────┘
```

The **framework** is the bottom two layers together: the engine *and* the stable
interface that lets you use it without forking it. The **harness** is the top
layer: our product.

### Layer 1 - Engine / Core

The agent runtime. It turns a raw LLM into a working agent: the loop, tool
dispatch, context-window management, the multi-provider abstraction, sub-agents,
and the permission gate. This is where the real work happens, and it is large
and fast-changing.

Lives in `vaf/core/`: [agent.py](../vaf/core/agent.py) (`chat_step`),
[context.py](../vaf/core/context.py), [backend.py](../vaf/core/backend.py) +
[api_backend.py](../vaf/core/api_backend.py),
[system_prompt.py](../vaf/core/system_prompt.py),
[subagent_ipc.py](../vaf/core/subagent_ipc.py), [trust.py](../vaf/core/trust.py).
See [vaf/core/README.md](../vaf/core/README.md) for the module list.

> In AI terminology this engine is itself often called the "agent harness" - the
> orchestration loop around the model. Note the clash with how we use "harness"
> below.

### Layer 2 - Framework surface

The small, **stable, documented contract** through which everything else uses
the engine - without reaching into its internals. This is the part that makes
VAF a *framework* rather than just an engine. It is deliberately thin so it can
stay stable while the engine underneath evolves.

| Surface | Where | Purpose |
|---|---|---|
| `from vaf import Agent` | [vaf/framework.py](../vaf/framework.py), [vaf/__init__.py](../vaf/__init__.py) | Embed the agent: `Agent(config=...).run(prompt)`; one-shots via `.complete(prompt)` |
| `vaf.CoreAgent` | re-export of `vaf.core.agent.Agent` | Advanced/full engine access |
| `BaseTool` | [vaf/tools/base.py](../vaf/tools/base.py) | The tool contract; `parameters` is validated and weak-model shape mistakes are repaired at dispatch (see [agents/TOOL_INPUT_REPAIR.md](agents/TOOL_INPUT_REPAIR.md)) |
| `vaf.tools` entry points | discovered in `_load_tools` | Ship tools as pip packages |
| config dict / schema | [vaf/core/config.py](../vaf/core/config.py) | Provider, model, keys, n_ctx |
| `VAF_NONINTERACTIVE` + trust | [vaf/core/trust.py](../vaf/core/trust.py) | Headless-safe tool gating |

How-to: [EMBEDDING.md](EMBEDDING.md).

### Layer 3 - Harness / Product

Veyllo's own application built on top of the framework: the desktop app, the
headless server, the web UI, the terminal interface, the system tray, the
messenger integrations, automations, and the memory dashboard. It is one
harness - a reference build - and a third party could build a different one on
the same framework.

Lives in `vaf/cli/` (TUI), [vaf/core/web_server.py](../vaf/core/web_server.py) +
`web/` (web UI), [vaf/tray.py](../vaf/tray.py) +
[desktop_window.py](../vaf/core/desktop_window.py) (desktop), `vaf/api/`
(routes), and the integration modules.

---

## Terminology (read this before arguing about words)

- **Framework** - the foundation you build on: Layers 1 + 2 (engine + stable
  interface). The name "Veyllo Agentic Framework" refers to this.
- **Harness** - *in this project* - the product built on the framework: Layer 3.
  Our desktop/server/web app is the reference harness.
- **Heads up:** in the wider AI/agent field, "harness" usually means the
  orchestration loop around the model - i.e. our **engine** (Layer 1), not the
  product. When talking to that audience, say "engine/runtime" for Layer 1 and
  "product" for Layer 3 to avoid confusion.

---

## The public boundary - what you may rely on

This is the contract. Treat it as a stable API.

**Stable (safe to build on; changes are versioned and announced):**

- `from vaf import Agent` - `Agent(config=...)`, `.run(prompt, on_token=...)`, `.run_async(...)`, `.complete(prompt, ...)`, `.add_tool(tool)`, `.on_event(cb)`, `.save_session()`, `.core`
- `vaf.CoreAgent`
- `BaseTool` and its declared attributes (`name`, `description`, `parameters`,
  `permission_level`, `side_effect_class`, `admin_only`, `channel_restrictions`,
  `coder_only`, `identity_kwargs`, `run`)
- `vaf.markers` - the special-return-value constants
- `vaf.user_jail` - turning a declared identity into a file boundary
- `vaf.ToolCaller` and `vaf.ToolRequest`, plus `set_tool_authorizer` on both
  `Agent` and `CoreAgent` - running and vetoing a tool call. Their documented
  arguments are in [EMBEDDING.md](EMBEDDING.md); `ToolCaller`'s remaining
  constructor parameters are not part of the promise.
- `vaf.set_account_allowlist_resolver` - the per-account tool allowlist hook,
  and `vaf.account_allows_tool` - its read side, for a surface that LISTS tools
  and must not offer what the account cannot run (contract in
  [EMBEDDING.md](EMBEDDING.md))
- `vaf.contained_path`, `vaf.safe_entry_name` and `vaf.PathEscape` - keeping a
  path that arrived from outside inside the directory it may touch, decided on
  RESOLVED paths so a symlink cannot lead out of the root
  (contract in [EMBEDDING.md](EMBEDDING.md))
- the `vaf.tools` entry-point group
- documented config keys

The authoritative list is `vaf.__all__` plus that page; this one is a summary.

**Internal (may change between releases; do not depend on it from outside):**

- everything else under `vaf.core.*` (including `chat_step` internals,
  `_clean_reasoning`, context/compaction internals, sub-agent IPC, web server).
  The names above that live in `vaf.core` but are re-exported on the façade
  (`CoreAgent`, `ToolCaller`, `ToolRequest`, `VoiceTurnEngine`, `TurnOutcome`,
  `extract_pdf_markdown`, `set_account_allowlist_resolver`,
  `account_allows_tool`, `contained_path`, `safe_entry_name`,
  `PathEscape`) are the deliberate
  exceptions - import them from `vaf`, not from `vaf.core`.
- private methods and attributes (leading underscore)

**What "stable" means during the alpha.** VAF is currently a `0.1.0aN`
prerelease, so breaking changes to this surface are still possible. "Stable"
here means this is the part of the API we commit to keeping compatible: it is
not broken without a MAJOR bump and a deprecation note, per the
backward-compatibility rules in [setup/RELEASING.md](setup/RELEASING.md), and
any change to it is announced in `CHANGELOG.md`. Everything listed as internal
carries no such commitment and may change between releases without notice. The
surface itself is guarded by a CI test (`tests/test_public_facade.py`) that
fails when the facade, the `Agent` signatures, or the `BaseTool` contract drift.
Note which way that guard points: it pins `vaf.__all__` and cites
[EMBEDDING.md](EMBEDDING.md), so it stays green while THIS summary is out of
date. If the two disagree, `vaf.__all__` is right.

When the engine needs to change, change it freely *behind* Layer 2. When Layer 2
needs to change, that is a deliberate, versioned event.

---

## Best practices by what you are doing

**Using VAF as a library (most common).**
Install the slim base with `pip install --pre vaf` (or from source: `pip install -e .`), add only the extras you need, and use
`from vaf import Agent`. Do not import from `vaf.core.*` directly unless you
truly need the full engine - that couples you to internals. See
[EMBEDDING.md](EMBEDDING.md).

**Adding a tool.**
Subclass `BaseTool`. For your own app, ship it as a pip package via a `vaf.tools`
entry point so you never touch VAF's source. Declare `permission_level` and
`side_effect_class` honestly - the engine enforces them.

**Building your own harness on the framework.**
Drive the agent through `vaf.Agent` / `vaf.CoreAgent`; build your UI, transport,
and storage as a layer *above* the framework surface, the way our product does.
Keep `VAF_NONINTERACTIVE` set and grant tools via the trust mechanisms rather
than disabling the gate. Do not fork the engine to add product features - that
is what Layer 2 is for.

**Working on the engine itself (core contributors).**
Prefer additive changes; keep the framework surface (Layer 2) stable. If a
change would alter the public boundary above, it is an API decision, not just a
refactor. The deeper hardening of the engine seams (decoupling product concerns
from the core, registries for providers/sub-agents, multi-agent isolation) is
deliberately deferred until real consumers show which seam needs it, so that
risky surgery on the engine is driven by need, not speculation.

---

## See also

- [EMBEDDING.md](EMBEDDING.md) - how to install and build on the framework.
- [README](../README.md) - the product (the reference harness) for end users.
- [vaf/core/README.md](../vaf/core/README.md) - the engine's internal modules.
- [llm/PROVIDER_MODES.md](llm/PROVIDER_MODES.md) - provider-specific behavior in the engine.
