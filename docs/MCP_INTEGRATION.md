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
| `transport` | `stdio` (default) · `http` · `sse` |
| `enabled` | set `false` to keep the entry but not load it |
| `permission_level` | `read` · `write` (default) · `dangerous` — see below |
| `url` | only for `http` / `sse` |

At startup VAF connects to every enabled server **in parallel** with a per-server timeout
(`mcp_discovery_timeout_seconds`, default 5), lists its tools, and registers each as
`mcp_<server>_<tool>`. A server that is slow, hung, or misconfigured is terminated and **skipped** —
it never blocks startup. Edit the manifest and call `agent.reload_mcp_tools()` for a live reload.

Naming is `mcp_<server>_<tool>` (e.g. `mcp_filesystem_read_file`): unambiguous, no dotted names in
LLM tool schemas, and the `mcp_` prefix marks it as external at a glance.

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

The level is **per server** (a server exposing both read and write tools shares one level); per-tool
overrides are a later refinement.

## Settings

- `mcp_native_tools_enabled` (default `true`) — kill-switch for the whole registration step;
  `mcp_call` still works when it is off.
- `mcp_discovery_timeout_seconds` (default `5`) — the parallel-discovery deadline.

## How it fits

Once registered, MCP tools live in the agent's tool registry like any native tool: they are offered to
the LLM in the same mixed tool list and appear in `list_tools`. Native tools stay fully in-process
(<1ms); MCP tools reuse a shared, warm server process per server (cached), so repeated calls do not
re-spawn.
