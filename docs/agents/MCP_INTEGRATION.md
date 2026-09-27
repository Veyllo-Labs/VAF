# MCP Integration - external tools as native VAF tools

VAF speaks the [Model Context Protocol](https://modelcontextprotocol.io). MCP servers are treated as
**tools layered on VAF's native tool system**, not as a replacement for it: the in-process native
tools stay the core, and MCP servers plug in as tools. There are two ways to reach an MCP server.

MCP is one of VAF's tool-discovery paths; for the full list and how tools fit the framework, see [TOOL_ROUTER_ARCHITECTURE.md](TOOL_ROUTER_ARCHITECTURE.md) and [ARCHITECTURE.md](../ARCHITECTURE.md).

## Two paths

| | `mcp_call` (raw) | Registered native tools |
|---|---|---|
| How | one generic tool; pass `server_command` + `tool_name` + `arguments` each call | configure a server once in `mcp_servers.json`; its tools appear as `mcp_<server>_<tool>` |
| Use for | ad-hoc / unregistered servers, CI, a fallback | day-to-day use - the agent calls `mcp_filesystem_read(path=…)` directly |
| Discovery | none | the server's `tools/list` is queried at startup; the LLM sees typed tools |

Both coexist (like `write_file` vs `python_sandbox`): `mcp_call` is the low-level raw path, the
registered tools are the high-level convenience path.

## Registering servers - `mcp_servers.json`

A hot-reloadable manifest in the VAF data directory (next to the custom-tools data - **not**
`config.py`, which is reserved for core settings). One file, manifest style:

```json
{
  "servers": {
    "filesystem": {
      "command": "npx -y @modelcontextprotocol/server-filesystem /home/me/projects",
      "transport": "stdio",
      "enabled": true,
      "permission_level": "write"
    },
    "tracker": {
      "transport": "http",
      "url": "https://mcp.example.com/mcp",
      "permission_level": "read"
    }
  }
}
```

| Field | Meaning |
|---|---|
| `command` | command that starts the server (stdio transport) |
| `transport` | `stdio` (default): a local process VAF starts. `http`: a remote server over **Streamable HTTP**, the transport the specification defines today. `sse`: a remote server over the older HTTP+SSE transport. |
| `url` | the remote server's endpoint (`http` / `sse`), e.g. `https://mcp.example.com/mcp` |
| `enabled` | set `false` to keep the entry but not load it |
| `permission_level` | `read` · `write` (default) · `dangerous` - see below |
| `env` | environment variables for a local server process (e.g. `{ "GITHUB_TOKEN": "…" }`), merged onto the VAF process environment. The file keeps only the names; the values live in the key ring (below). |
| `token` | a remote server's access token, sent as `Authorization: Bearer <token>`. Accepted here once and moved into the key ring; a `headers` object with an `Authorization: Bearer` entry (the shape other MCP clients write) is taken the same way, other headers stay and are sent as they are. |
| `auth` | `"oauth"`: every account signs in to this remote server on its own (below) instead of one token for everybody. Absent or empty: no sign-in, or the `token`. |
| `oauth_client_id` | optional, with `auth: "oauth"`: a client registered at the service by hand, for a service that does not let VAF register one itself. Its secret (`oauth_client_secret`) is accepted here once and moved into the key ring. |
| `tool_permissions` | optional per-tool permission overrides (below) |

At startup VAF connects to every enabled server **in parallel** with a per-server timeout
(`mcp_discovery_timeout_seconds`, default 5), lists its tools, and registers each as
`mcp_<server>_<tool>`. A server that is slow, hung, or misconfigured is terminated and **skipped** -
it never blocks startup. The server list in Settings shows why a server did not connect (a
refused token reads "the server refused the access (401)").

### Remote servers

A remote server is reached through the official `mcp` SDK (`vaf/core/mcp_remote.py`): JSON-RPC
over Streamable HTTP or SSE, with the `initialize` handshake and the session the server hands out
(`Mcp-Session-Id`). One session per server stays open for the life of the process and serves every
call; a session idle for a minute is pinged before it is used and reopened if it does not answer.
A `tools/call` is never retried: a tool may have acted before its answer was lost, and doing it
twice is worse than an error. A changed URL or token opens a fresh session.

Before this, the HTTP path posted a bare `{"name", "arguments"}` to `<url>/tools/call` - no
JSON-RPC, no handshake, no session, no login - so no server that implements the specification
answered it, the SSE path was not built at all, and discovery skipped every server that had a URL
and no command: no remote server ever registered a tool.

### Sign-in per account

A hosted server that holds each person's own data (a workspace, a mailbox, a tracker) does not take
one token for the whole installation: every person signs in with their own account. A server with
`"auth": "oauth"` works that way (`vaf/core/mcp_oauth.py`), through OAuth as the MCP authorization
specification defines it, implemented by the official SDK (`OAuthClientProvider`): VAF reads the
server's protected-resource and authorization-server metadata, registers itself as a client where
the service allows that (dynamic client registration; otherwise the admin enters a client registered
by hand), signs in with PKCE and refreshes the tokens.

- **Signing in** is every account's own step, admin or not: Settings, Connections, "MCP services"
  lists the servers that sign accounts in, with this account's state and a Sign in / Sign out
  button. Sign in opens the service's page in a new tab (the system browser in the desktop app); the
  service sends that tab back to `/api/mcp/oauth/callback`, which must be reached by the person who
  started the sign-in (the same actor binding as the mail sign-in), finishes the exchange and lands
  on the Connections tab. A sign-in not finished within 10 minutes is dropped.
- **Every call runs as the caller.** The tools of such a server declare
  `identity_kwargs = ("user_scope_id",)`, and each call runs in the caller's own session at the
  server. An account that has not signed in gets "not signed in" before any request is made, and
  never another account's session. A sign-in the service no longer accepts (the refresh refused)
  fails the call with "sign in again" at once: nothing waits for a browser nobody opened.
- **The tool list** is read with one signed-in account (the local admin's when it has one), since
  the tools are the server's and the same for every account. Until the first account signs in the
  server shows "sign-in needed" in the MCP list and registers no tools; the first sign-in loads them.
- **Tokens** live in the key ring, one record per account and server
  (`mcp_server.<name>.oauth.<account>`: the tokens, their expiry and the client the account
  registered). A token that ran out while VAF was not running is refreshed on the next call. The
  SDK does not restore the expiry of stored tokens by itself, and without it the provider would send
  the old token, meet a 401 and start a new interactive sign-in instead of the refresh.
- **Signing out** deletes the record and closes the account's session first, then asks the service
  to invalidate the tokens (RFC 7009) where its metadata offers a revocation endpoint.
- **The sign-ins belong to the server as it was set up.** A server moved to another host, switched
  away from the sign-in or given another client drops every account's sign-in (and the service is
  asked to invalidate them); removing the server does the same. A server with the sign-in takes no
  fixed token: one stored before is removed.
- **The redirect address** a hand-registered client needs is shown in the editor; it is this
  installation's `/api/mcp/oauth/callback` on the same base as the email and cloud sign-ins, and
  `mcp_oauth_callback_base_url` overrides the base behind a reverse proxy.

Named boundaries, also in the module docstring: the raw `mcp_call` has no sign-in (it knows a URL,
not a configured server); scopes are the ones the server asks for in its metadata; the sign-ins of a
deleted account stay in the ring until the server is removed, as no store in VAF has a per-account
deletion hook; the terminal has no sign-in (there is no `vaf mcp` command at all), while the
framework functions take the redirect address from their caller, so one could offer a loopback
address.

For an embedder: `mcp_oauth.start_sign_in(name, user_scope_id=, redirect_uri=)` returns the
authorization address, `finish_sign_in(state, code, error)` completes it from your callback
(`pending_owner(state)` says who started it), `sign_out` and `sign_in_status` do the rest; after a
first sign-in, `Agent.reload_mcp_tools()` loads the server's tools.

### Secrets

A local server's `env` values and a remote server's token live in the encrypted key ring
(`vaf/core/mcp_secrets.py`, entries `mcp_server.<name>.env` and `mcp_server.<name>.token`), never in
`mcp_servers.json` and never in the browser. A value written into the file by hand moves into the
ring the next time the file is loaded (after the ring read it back; a move that fails leaves the
file as it was and is tried again). Removing a server removes its secrets. `vaf secure status` names
a server whose secret is still in the file. Every env value counts as a secret, not only the ones
whose names look like one.

A stored token stays with the server it was stored for (same scheme, host and port): an entry
moved to another host without a new token keeps none, and the editor's test button sends the
stored token only to the saved host, so a typo or a new server under an old name never receives
it. Removing a server removes its secrets only once the file no longer lists it.

A server with a sign-in per account adds two kinds of entry: `mcp_server.<name>.oauth.<account>`
(one account's tokens and registered client, see "Sign-in per account") and
`mcp_server.<name>.oauth_client_secret` (the secret of a client registered by hand).

### One caller at a time on a local server

A local server process is shared by every caller in VAF (the raw `mcp_call`, discovery, the
registered tools), so one request at a time runs per process and every request carries its own
JSON-RPC id. Before, every call used the same id and nothing serialised them: two chats calling
the same tool at once could each read the other's answer. The stdio loop itself stays
hand-written (it works; moving it onto the SDK is a change of its own, with the Windows process
flags and the warm-process cache to carry).

Naming is `mcp_<server>_<tool>` (e.g. `mcp_filesystem_read_file`): unambiguous, no dotted names in
LLM tool schemas, and the `mcp_` prefix marks it as external at a glance.

## Managing servers in the UI

Admins can manage servers without editing JSON by hand: **Settings → Advanced → MCP** lists the
configured servers (with a connection status dot and tool count per server) and lets you add, edit, or
remove a server through a form (name, transport, command or URL, an access token for a remote server,
enabled, `permission_level`), or paste a standard `{ "mcpServers": { … } }` config block (the format
used by Claude Desktop / Cursor: `command` + `args` + `env` for a local server, `url` + `type` +
`headers` for a remote one, whose bearer token fills the token field) into the panel to auto-fill the
form. The token field starts empty and says when a token is stored; leaving it empty keeps it.
For a remote server the form also sets the sign-in: none or one token for everybody, or every
account signing in on its own, with an optional hand-registered client (ID and a write-only
secret) and the redirect address such a client needs. The editor's test of such a server runs as
the tester's own account when it has signed in, never as another's, and reads a 401 from a server
nobody signed in to yet as "reachable, every account has to sign in". The server card says how many
accounts signed in.
Env values come back empty for the same reason, and an empty value keeps the stored one. Saving
keeps the keys the form does not show (`tool_permissions`). The
Advanced-tab row shows "N connected / M configured" at a glance. Saving writes `mcp_servers.json` and
hot-reloads the tools immediately (no restart); the underlying manifest is the same file described
above, so manual edits and the UI are interchangeable. Editing the manifest directly still takes
effect on the next reload. See [WEB_UI.md](../web-ui/WEB_UI.md) for the Settings layout and
[WEBUI_WEBSOCKET_FLOW.md](../web-ui/WEBUI_WEBSOCKET_FLOW.md) for the underlying messages.

## Permissions

MCP tools use VAF's normal tool contract (see [TOOL_ROUTER_ARCHITECTURE.md](TOOL_ROUTER_ARCHITECTURE.md)),
so the same gates apply. The default is **`write`**, which routes the tool through the plan gate (the
agent must write a plan before acting - see [CONTEXT_MANAGEMENT.md](../memory/CONTEXT_MANAGEMENT.md)) while staying
usable unattended. Override per server:

- **`write`** (default) - plan-gated, no per-call prompt, works in automations/headless.
- **`dangerous`** - adds a confirmation prompt on every call; in non-interactive contexts
  (automations, headless, CI) a `dangerous` tool returns `[ERROR] requires confirmation`. Reserve it
  for untrusted servers you only run interactively.
- **`read`** - read-only, no gate, no prompt (e.g. a safe search/lookup server).

The level is **per server** by default. For finer control, add a `tool_permissions` map to a server
entry to override individual tools (the rest fall back to the server level) - manifest only, no UI:

```json
"filesystem": {
  "command": "npx -y @modelcontextprotocol/server-filesystem /path",
  "permission_level": "read",
  "tool_permissions": { "write_file": "dangerous", "move_file": "write" }
}
```

## Settings

- `mcp_native_tools_enabled` (default `true`) - kill-switch for the whole registration step;
  `mcp_call` still works when it is off.
- `mcp_discovery_timeout_seconds` (default `5`) - the parallel-discovery deadline.
- `mcp_oauth_callback_base_url` (default empty) - the base of the sign-in's redirect address behind
  a reverse proxy; empty derives it like the email and cloud sign-ins.

## How it fits

Once registered, MCP tools live in the agent's tool registry like any native tool: they are offered to
the LLM in the same mixed tool list and appear in `list_tools`. Native tools stay fully in-process
(<1ms); MCP tools reuse a shared, warm server process per server (cached), so repeated calls do not
re-spawn.

## Scope: the `tools/` layer, not the `tasks/` layer

VAF drives MCP tools through a synchronous `tools/call`. MCP also defines an **optional task
augmentation** (`tasks/*` - for long-running operations that stream progress through multiple states);
VAF does **not** implement it. A tool that advertises `execution.taskSupport: "required"` in
`tools/list` can never run over a plain `tools/call`, so it is **skipped at discovery** rather than
offered to the LLM as an always-failing tool (tools that mark it `forbidden`, `optional`, or leave it
unset run normally). This affects only servers that hard-require the task layer - none of the common
real-world servers do; it shows up mainly in the reference "everything" test server's research demo.

## VAF also ships one MCP server: the room bridge

Everything above is VAF as an MCP CLIENT. The one MCP server VAF ships travels the
other way: the downloadable A2A guest client (`examples/12_a2a_wire_peer.py`, served
by every room host at `/api/a2a/client.py`) has an `mcp` subcommand that serves the
room verbs to a foreign MCP host over stdio - a hand-rolled loop speaking exactly the
subset the client half of this document uses (`initialize` with protocol revision
`2024-11-05`, `tools/list`, `tools/call`, `ping`), standard library only. A
Claude Desktop, Claude Code or Cursor on a machine without VAF configures
`{"command": "python3", "args": ["a2a_client.py", "mcp"]}` and gets the room as
`a2a_*` tools. Details and the tool list live in
[A2A_PROTOCOL.md](A2A_PROTOCOL.md) under "Joining without VAF". The suite proves
interop by consuming the bridge through VAF's own `mcp_call` client.
