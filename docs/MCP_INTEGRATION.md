# MCP Integration — external tools as native VAF tools

VAF speaks the [Model Context Protocol](https://modelcontextprotocol.io). MCP servers are treated as
**tools layered on VAF's native tool system**, not as a replacement for it: the in-process native
tools stay the core, and MCP servers plug in as tools. There are two ways to reach an MCP server.

## Two paths

| | `mcp_call` (raw) | Registered native tools |
|---|---|---|
| How | one generic tool; pass `server_command` + `tool_name` + `arguments` each call | configure a server once in `mcp_servers.json`; its tools appear as `mcp_<server>_<tool>` |
| Use for | ad-hoc / unregistered servers, CI, a fallback | day-to-day use — the agent calls `mcp_filesystem_read(path=…)` directly |
| Discovery | none | the server's `tools/list` is queried at startup; the LLM sees typed tools |

Both coexist (like `write_file` vs `python_sandbox`): `mcp_call` is the low-level raw path, the
registered tools are the high-level convenience path.

## Registering servers — `mcp_servers.json`

A hot-reloadable manifest in the VAF data directory (next to the custom-tools data — **not**
`config.py`, which is reserved for core settings). One file, manifest style:

```json
{
  "servers": {
    "filesystem": {
      "command": "npx -y @modelcontextprotocol/server-filesystem /home/me/projects",
      "transport": "stdio",
      "enabled": true,
      "permission_level": "write"
    }
  }
}
```

| Field | Meaning |
|---|---|
| `command` | command that starts the server (stdio transport) |
| `transport` | `stdio` (default) · `http` · `sse`. Tool **discovery** is full for `stdio`, best-effort for `http`, and not supported for `sse` (an `sse` server registers no tools — use `stdio`/`http`, or the raw `mcp_call` tool). |
| `enabled` | set `false` to keep the entry but not load it |
| `permission_level` | `read` · `write` (default) · `dangerous` — see below |
| `url` | only for `http` / `sse` |
| `env` | optional environment variables for the server process (e.g. `{ "GITHUB_TOKEN": "…" }`); merged onto the VAF process environment |

At startup VAF connects to every enabled server **in parallel** with a per-server timeout
(`mcp_discovery_timeout_seconds`, default 5), lists its tools, and registers each as
`mcp_<server>_<tool>`. A server that is slow, hung, or misconfigured is terminated and **skipped** —
it never blocks startup.

Naming is `mcp_<server>_<tool>` (e.g. `mcp_filesystem_read_file`): unambiguous, no dotted names in
LLM tool schemas, and the `mcp_` prefix marks it as external at a glance.

## Managing servers in the UI

Admins can manage servers without editing JSON by hand: **Settings → Advanced → MCP** lists the
configured servers (with a connection status dot and tool count per server) and lets you add, edit, or
remove a server through a form (name, transport, command/url, enabled, `permission_level`) — or paste
a standard `{ "mcpServers": { … } }` config block (the format used by Claude Desktop / Cursor, with
`command` + `args` + `env`) into the panel to auto-fill the form. The
Advanced-tab row shows "N connected / M configured" at a glance. Saving writes `mcp_servers.json` and
hot-reloads the tools immediately (no restart); the underlying manifest is the same file described
above, so manual edits and the UI are interchangeable. Editing the manifest directly still takes
effect on the next reload. See [WEB_UI.md](WEB_UI.md) for the Settings layout and
[WEBUI_WEBSOCKET_FLOW.md](WEBUI_WEBSOCKET_FLOW.md) for the underlying messages.

## Permissions

MCP tools use VAF's normal tool contract (see [TOOL_ROUTER_ARCHITECTURE.md](TOOL_ROUTER_ARCHITECTURE.md)),
so the same gates apply. The default is **`write`**, which routes the tool through the plan gate (the
agent must write a plan before acting — see [CONTEXT_MANAGEMENT.md](CONTEXT_MANAGEMENT.md)) while staying
usable unattended. Override per server:

- **`write`** (default) — plan-gated, no per-call prompt, works in automations/headless.
- **`dangerous`** — adds a confirmation prompt on every call; in non-interactive contexts
  (automations, headless, CI) a `dangerous` tool returns `[ERROR] requires confirmation`. Reserve it
  for untrusted servers you only run interactively.
- **`read`** — read-only, no gate, no prompt (e.g. a safe search/lookup server).

The level is **per server** by default. For finer control, add a `tool_permissions` map to a server
entry to override individual tools (the rest fall back to the server level) — manifest only, no UI:

```json
"filesystem": {
  "command": "npx -y @modelcontextprotocol/server-filesystem /path",
  "permission_level": "read",
  "tool_permissions": { "write_file": "dangerous", "move_file": "write" }
}
```

## Settings

- `mcp_native_tools_enabled` (default `true`) — kill-switch for the whole registration step;
  `mcp_call` still works when it is off.
- `mcp_discovery_timeout_seconds` (default `5`) — the parallel-discovery deadline.

## How it fits

Once registered, MCP tools live in the agent's tool registry like any native tool: they are offered to
the LLM in the same mixed tool list and appear in `list_tools`. Native tools stay fully in-process
(<1ms); MCP tools reuse a shared, warm server process per server (cached), so repeated calls do not
re-spawn.

## Scope: the `tools/` layer, not the `tasks/` layer

VAF drives MCP tools through a synchronous `tools/call`. MCP also defines an **optional task
augmentation** (`tasks/*` — for long-running operations that stream progress through multiple states);
VAF does **not** implement it. A tool that advertises `execution.taskSupport: "required"` in
`tools/list` can never run over a plain `tools/call`, so it is **skipped at discovery** rather than
offered to the LLM as an always-failing tool (tools that mark it `forbidden`, `optional`, or leave it
unset run normally). This affects only servers that hard-require the task layer — none of the common
real-world servers do; it shows up mainly in the reference "everything" test server's research demo.
