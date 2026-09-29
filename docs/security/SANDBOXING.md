# VAF Sandboxing

Security is paramount when allowing an AI to execute code. VAF uses **Docker Containers** to isolate generated code execution from your host operating system.

---

## Security Model

**Docker is REQUIRED for code execution.** There is NO fallback to host execution.

| Tool | Isolation | Use Case |
|------|-----------|----------|
| `python_sandbox` | Docker Container | Safe code execution (default) |
| `python_exec` | Host System | Only with explicit user trust |

File tools (e.g. `librarian_agent`, `read_file`) block access to the VAF installation directory; the agent is instructed not to request operations on that path.

### Isolation model: Docker-level, not Python-level

VAF's sandbox isolation is enforced entirely at the **Docker container level**. There is no Python-level module blocklist - standard-library modules like `subprocess`, `socket`, and `os` are importable inside the container. What prevents abuse is:

- **Process namespace isolation** - processes cannot escape the container
- **Filesystem isolation** - host filesystem is not mounted (code runs in `/tmp/vaf_*` per execution)
- **Resource limits** - 512 MB memory, 0.5 CPU cores (hard limits via Docker)
- **No host privilege escalation** - default Docker unprivileged mode

**What is NOT blocked at Python level:**
- `import subprocess` - works, spawns processes inside the container only
- `import socket` - works; sandbox has outbound network access (needed for pip and Tool Bridge)
- `import os` - works; filesystem access is limited to what Docker mounts, not by Python

See [`SANDBOX_MODULES.md`](SANDBOX_MODULES.md) for the full module reference and security details.

The browser container is a separate sandbox with its own design doc and is NOT covered here: see
[`BROWSER_AGENT.md`](../agents/BROWSER_AGENT.md) for its image, ports and health probe, and section 5 of
[`USER_ISOLATION.md`](USER_ISOLATION.md) for the per-user browser pool.

---

## Smart Auto-Start

VAF automates sandbox startup:
1. **Detection:** Before running code, VAF checks if the Docker Daemon is running.
2. **Auto-Start:** If Docker is installed but stopped, VAF attempts to launch it automatically:
   - **macOS:** Launches `Docker.app`.
   - **Windows:** Launches `Docker Desktop.exe`.
   - **Linux:** Triggers `docker.socket` (requires systemd socket activation).
3. **Polling:** VAF waits up to 30 seconds for the daemon to become ready.

---

## No Fallback (By Design)

If Docker is **not installed** or **cannot be started**:
- Code execution is **BLOCKED** (not degraded to host)
- You will see: `[SECURITY] Sandbox requires Docker: ...`
- This is intentional - we do not compromise on security

To execute code, you must:
1. Install a Docker runtime - Docker Desktop (<https://docker.com>), or Docker Engine / Colima / Podman
2. Start the runtime so the daemon is reachable (e.g. `colima start`, or open Docker Desktop)
3. Re-run your code request

---

## Container Configuration

VAF uses a **persistent sandbox container** for fast code execution:

```bash
# The sandbox is part of the VAF Docker stack
docker compose -f docker-compose.memory.yml up -d
```

| Resource | Detail |
|----------|--------|
| **Image** | python:3.11-slim |
| **Container** | vaf-sandbox (persistent), `sleep infinity` behind docker-init (`init: true`): the init forwards SIGTERM, so a stop takes a moment instead of the 10 s grace and a SIGKILL, and it reaps the orphans `docker exec` runs leave behind |
| **Memory** | 512MB |
| **CPU** | 0.5 Cores |
| **Network** | `vaf-sandbox-network` (isolated bridge). Cannot reach postgres/redis/gotenberg/tts/stt by hostname. Outbound internet (pip install) and Tool Bridge back-channel (`host.docker.internal`) still work. |
| **Filesystem** | Isolated (no host access). Packages installed via the `packages` parameter are TEMPORARY: pip runs with `--target` into the run's private `_pkgs` dir (plus `--no-cache-dir` so the shared pip cache does not grow), `PYTHONPATH`/`PIP_TARGET` point there for the run - `PIP_TARGET` also redirects code that shells out to pip itself - and the whole directory is deleted with the per-run workspace. Nothing accumulates in the shared container across runs or users. |
| **Workspace** | Per-execution temp dir under `/tmp/vaf_*` (unique UUID per run, auto-deleted after). Container `working_dir` is `/workspace` (persistent volume), but code always executes in the per-run `/tmp/vaf_*` dir. |
| **Capabilities** | `cap_drop: ALL`, `no-new-privileges: true` - container has no Linux capabilities beyond default isolation. |
| **Module blocking** | None at Python level - `subprocess`, `socket`, `os` are importable. Constraints are enforced by Docker process/filesystem isolation, network isolation, and resource limits, not by a Python import blocklist. |
| **Timeout kill** | A timed-out or user-stopped execution is killed INSIDE the container, scoped to that run only: a pure-sh procfs scan terminates every process whose cwd or cmdline carries the run's unique workspace path (`kill_run_processes_cmd` in `vaf/tools/sandbox.py`). Slim images ship no procps, so the previous `pkill -9 -f python` silently no-opped (a timed-out pip finished a 229MB install into an already-cleaned workspace) - and would have hit every other user's run in the shared container. Guarded by `tests/test_sandbox_hardening.py`. |
| **Ephemeral fallback** | When the persistent container is unavailable, executions fall back to a per-instance ephemeral container that carries the SAME hardening: `--cap-drop ALL`, `no-new-privileges`, and its own isolated bridge network `vaf-sandbox-ephemeral` (auto-created; a separate name because docker compose refuses to adopt a same-name network it did not create) plus the `host.docker.internal` alias for the Tool Bridge. If the network cannot be provided, the container starts degraded (capabilities still dropped, default bridge) with a loud warning. Never `--network none`: outbound pip and the Tool Bridge are designed features. |

### Performance

Using a persistent container provides:
- **~800ms** execution time (vs 5–10s for ephemeral containers)
- Pre-installed packages persist across executions
- Instant startup (no container creation overhead)

---

## Standard Usage

### Basic code execution

```python
python_sandbox(code="print(2 ** 32)")
```

### Installing packages

```python
python_sandbox(
    code="import numpy as np; print(np.array([1,2,3]).mean())",
    packages=["numpy"]
)
```

### Custom timeout

```python
python_sandbox(code="import time; time.sleep(5); print('done')", timeout=60)
```

---

## Programmatic Tool Calling (`with_vaf_tools=True`)

The sandbox supports **Programmatic Tool Calling** - code inside the sandbox can call any VAF tool via an injected `vaf_tools` module. Only the final `print()` output of the script returns to the model context; intermediate tool results are consumed entirely inside the running script and never become chat messages.

This is provider-agnostic and works with every backend (OpenAI, Anthropic, Google, local).

### Usage

```python
python_sandbox(
    code="""
import vaf_tools

# Call multiple VAF tools inside the script
weather = vaf_tools.call("web_search", {"query": "Berlin weather today"})
contact = vaf_tools.call("get_contact", {"name": "Max"})

# Only this output reaches the model
print(f"Weather: {weather[:300]}")
print(f"Contact: {contact}")
""",
    with_vaf_tools=True,
)
```

### List available tools from inside the sandbox

```python
import vaf_tools
print(vaf_tools.available())
```

### How it works

```
Host (VAF process)                         Docker sandbox
─────────────────────────────────────────  ─────────────────────────────
ToolBridgeServer (random port, daemon) ←── vaf_tools.call("web_search", …)
  token check (per-execution secret)        HTTP POST /call  (JSON body)
  → agent.execute_tool("web_search", …)     ← JSON {"result": "..."}
  → return result string                    script continues with result
                                            …
                                            print("final answer") → model
```

**Files:**
- `vaf/core/tool_bridge.py` - `ToolBridgeServer`, `_BridgeHandler`, stub source
- `vaf/tools/python_sandbox.py` - `_build_call_tool_fn()`, `_run_with_bridge()`

### Security properties

| Property | Detail |
|---|---|
| Token | `secrets.token_hex(16)` per execution - mismatches rejected (HTTP 403) |
| Binding | `0.0.0.0` on host, random ephemeral port. Accessible from any interface on the host; relies on the per-execution token for authentication. |
| Trust gates | All calls go through `agent.execute_tool()` - full VAF gate pipeline applies |
| Cleanup | `bridge.stop()` in `finally` block - no port leak on crash |

### Host gateway by OS

The sandbox container connects back to the host via `host.docker.internal` on all platforms:

| OS | How it resolves |
|---|---|
| Windows | Docker Desktop DNS alias - automatic |
| macOS | Docker Desktop DNS alias - automatic |
| Linux | `extra_hosts: ["host.docker.internal:host-gateway"]` in `docker-compose.memory.yml` injects the host IP (Docker 20.10+) |

---

## Troubleshooting

**Error: "Docker is not installed"**
- Install a Docker runtime: Docker Desktop (<https://docker.com>), or Docker Engine / Colima / Podman
  (the VAF installer detects an existing runtime but does not install one)
- Then re-run the request (no reinstall needed)

**Error: "Docker Daemon is not running"**
- Start the runtime (open Docker Desktop, or `colima start`)
- Docker Desktop first-run: accept the Terms of Service
- Linux: `sudo systemctl start docker`

**Error: "Image not found"**
- The sandbox pulls `python:3.11-slim` automatically on first use
- Ensure you have internet access for the first run
- Manual pull: `docker pull python:3.11-slim`

**Error: "vaf_tools: bridge unreachable"** (when using `with_vaf_tools=True`)
- The sandbox container cannot reach the host via `host.docker.internal`
- Verify the compose stack is running so `extra_hosts` is applied: `docker compose -f docker-compose.memory.yml up -d`
- From inside the container, check resolution: `docker exec vaf-sandbox getent hosts host.docker.internal`
- Check that no firewall rule blocks the ephemeral port range: `sudo ufw allow 32768:65535/tcp` (temporary test)
- Requires Docker 20.10+ for the `host-gateway` special value in `extra_hosts`

---

## python_exec (Unsafe Alternative)

The `python_exec` tool runs code directly on your host system. It is:
- **Disabled by default**
- Only available with explicit trust configuration
- Shows clear warnings when used
- Never run from a messaging channel in the chat, also with `channel_tools_unrestricted` on
  (it needs a person, like `host_bash`, see "Shell execution surfaces" below)

Use this only when you need host filesystem/network access and trust the code source.

---

## Shell execution surfaces

Beyond the Python sandbox there are three shell-execution surfaces, each with a distinct
confinement model. The guiding rule: **the coder's own shell is jailed; the host is reached
only through `host_bash`, which an account has or does not have.**

### Coder `bash` - kernel-jailed workspace shell (`vaf/tools/workspace_exec.py`)

The coding agent's `bash` (`coder_only`) needs a real shell for its project - run scripts,
`npm`/`pip install`, run the app, but must never be able to touch VAF's own source, secrets,
or itself and break the running system. String-filtering a shell is not real security, so the
command is confined by the **kernel**:

- **Linux + bubblewrap (`bwrap`):** the command runs on the real host inside a bwrap jail.
  The project workspace is bind-mounted **read-write** (edits persist to the host); system dirs
  (`/usr`, `/bin`, `/etc`, ...) are **read-only**; and the VAF repo, `~/.vaf`, secrets and the
  docker socket are simply **not mounted** - they do not exist for the command. The environment
  is `--clearenv`'d and only non-secret basics (`PATH`, `LANG`, ...) are re-injected, so tray
  API keys never leak. The network is `--unshare-net`'d, so host-loopback services (the memory
  DB on `5432`, the VAF API) are unreachable from the jail.
- **Fallback (no bwrap):** a fresh container with **only** the workspace mounted (`-v ws:/workspace`)
  and `--network none`. Same confinement, minus host access.
- **No sandbox at all:** the tool **refuses**. A raw, unconfined host shell is never run, and
  `bash` also refuses if no project workspace is bound (it would otherwise root the jail at `$HOME`).
- **No network, also not for a dependency repository** (named boundary, in the module docstring
  of `vaf/tools/workspace_exec.py`). A Maven, Gradle or npm build that downloads dependencies
  runs through `host_bash`, which the coder uses without asking where the account has it. An
  allowlisted network for the jail (only Maven Central, only the Paper repository) would need a
  user-space network stack behind a filtering proxy; it is earned by the first measured request
  from an account without `host_bash`. So that the coder does not find this out by failing:
  `bash` describes itself as offline and sends downloads to `host_bash`, and a failed command
  whose output shows a missing network (an unresolved host, a failed artifact transfer) says so
  in its result.

**Docker is refused in the coder shell.** The host docker socket is host-root-equivalent
(a container can `--privileged` / `-v /:/host` / `--pid=host` its way to the whole host
filesystem, outside this jail's mount namespace) and cannot be safely policed by inspecting the
command string. So the coder's `bash` refuses any `docker` invocation up front and points the
user at the main agent instead. Confinement is verified by real escape attempts in
`tests/test_workspace_exec.py` (VAF-core write blocked, source invisible, host DB unreachable,
env secrets not leaked, docker always refused).

### `run_tests` (`vaf/tools/sandbox_test_runner.py`)

Gives the coder a sanctioned way to actually run its project's tests and get the **real**
pass/fail, instead of guessing. It copies the project (tar-pipe) into a fresh
`/workspace/testrun_...` directory in the `vaf-sandbox` container, runs `python3 -m pytest -q`
under an in-container `timeout -s KILL`, returns the summary, and removes the run directory in a
`finally`. It is `read`-level (no host side effects).

### `host_bash` - host shell (`vaf/tools/host_bash.py`)

Some tasks genuinely need the real host - "check my running docker container", inspect host
services, run a host CLI, a local build. `host_bash` runs **unsandboxed on the host and
outside the per-user file jail, on purpose**. Who may use it is an **account permission**
(the account allowlist in user management; the standard preset includes it, so a new
account has it unless the admin takes it away). How each use is controlled:

1. **`permission_level = "dangerous"`** → the framework's confirmation gate fires, and the
   person sees the command, not just the tool name - in the Web UI, the TUI modal and the
   terminal prompt alike. They answer **only this time** (this one call, nothing is
   remembered), **for this chat** (the tool keeps running unasked for this person in this
   chat, in memory until the next restart; every such run is announced as a
   `gate_bypassed` event with `why="chat_grant"`), **always** (persisted per person: the
   tool everywhere plus trust for the current folder) or **cancel**. The rendered arguments
   are hardened (`vaf/core/arg_preview.py`): hidden and direction-changing characters become
   visible markers, credential material is redacted, and a truncation says so, so the
   approved text cannot differ from the executed one.
2. **The coding agent and workflow steps run it without asking** (`vaf/tools/coder.py`,
   `vaf/workflows/engine.py` with `gate_enabled=False`). Deliberate: both run unattended, a
   dialog would stall them, and a coder that needs a local build or a host CLI must not stop
   for it. What still applies there: the account allowlist (so an account without
   `host_bash` does not get it through the coder either), the policy block (`admin_only`;
   `channel_restrictions` while the admin keeps `channel_tools_unrestricted` off), and for
   workflow steps an application's authorizer. The coder enforces its own tool allow-list
   at dispatch as well, so a tool it was never given does not run because the model named it.
3. **The main agent's direct call is blocked on remote channels.** There is no safe way to
   show the confirmation on Telegram/WhatsApp/Discord. `channel_restrictions = ("channel",)`
   is the policy-layer block (`evaluate_tool_policy`), and because the tool is ALSO
   `dangerous` it needs a person, so the policy refuses it on a channel *even when the admin
   enables `channel_tools_unrestricted`* (default ON on a fresh install), which otherwise
   lifts the block and the confirmation for the convenience tools (section 1a of
   `evaluate_tool_policy`). The answer comes from the chat lane's own source and session, the
   same single resolution every channel decision uses. Only in the lane that would ask a
   person (`ToolCaller` with its gate on), deliberately: it protects the turn where somebody
   would have been asked, not the unattended lanes in 2 - a coder started from a Telegram
   message may still build. `python_exec` declares the same two and is refused the same way;
   before the rule it ran from Telegram for anyone with a stored "always" (measured).

   **The main agent: local Web UI / CLI only.**
4. **Background mode** (`background=true`) starts the command detached and returns its id;
   the approval above is given once, at the start, and `host_process` then reads its log,
   writes to its input or stops it without asking again. The command's log is private
   (0600, owner-only folder, outside the project folder), the process is visible only to the
   chat and person that started it, and it ends with VAF. It is refused in a messaging-channel
   chat and inside a sub-agent's process, where nobody could be told when it ends. Details:
   [TOOL_SUPERVISION.md](../agents/TOOL_SUPERVISION.md), "Background host commands".

An offline classifier (`vaf/core/command_policy.py`, shared with `bash` but run with the
strict `host` profile) refuses the catastrophic set even after confirmation: code fetched
from the network and piped into a shell, writes to a block device, a fork bomb, a recursive
delete of a system or home root, and a command whose executable is built by a substitution
(there the approved text is not the text that would run). It tokenizes quote-aware and
descends into substitutions rather than matching substrings, so `rm -rf /tmp/scratch` is
ordinary work while `rm  -rf  /` is not. It also classifies the command a command carries:
`bash -c '...'`, `su -c '...'`, `eval ...` with the lane's own profile, and `ssh host '...'`
with the `remote` profile (the catastrophic core, while an installer piped into a shell is
named rather than refused, because that is how a server is commonly set up). A wrapper's
option values are stepped over (`sudo -u root rm -rf /` is judged as `rm -rf /`), and a
command nested more than `MAX_NESTING` deep is refused. Before this, ten of ten measured
wrapped forms passed. The verdict carries its categories, which the confirmation dialog
shows. For the main agent's own call, the real safety is still the
person's approval plus the local-only rule in 3. The unattended lanes (the coder and
workflow steps, point 2) have no approval: there it is the account allowlist, the policy
block and, for workflow steps, the application's authorizer that decide. The controls are
pinned in `tests/test_host_bash.py`, `tests/test_command_policy.py` and
`tests/test_coder_dispatch_gate.py`.

### `ssh` - a shell on another machine (`vaf/tools/ssh.py`)

Runs a command on a server, or copies a file to or from it, over the system's own OpenSSH
client (`vaf/core/ssh.py`). It never touches this computer beyond the account's own files:
the local side of a copy is under the account's write jail (`file_access = "write"`), and the
command line is argv with the remote command after `--`, so a server name cannot smuggle an
OpenSSH option (measured: `ssh -G -F none localhost -oProxyCommand=echo` takes one even
after the host). The controls:

1. **Confirmed in the chat** (`dangerous`), and a trusted folder does not silence it
   (`trusted_dir_grants = False`: a server is in no folder).
2. **The first connection to a server is always asked** (`ask_reason`), also under "always"
   and the admin's hands-off switch, with only "this time" and "cancel" on offer. Where
   nobody is asked (a workflow step) it is refused (`accepts_call_confirmation`), so an
   unattended run reaches only servers a person confirmed. A server that later shows a
   different key is refused (`StrictHostKeyChecking=accept-new` on the account's own list).
3. **Never over a messaging channel** (dangerous and restricted with `"channel"`, see 3 in
   the `host_bash` section), and **a regular account only when its allowlist names it**
   (`account_opt_in`).
4. **The remote command is classified** with the `remote` profile: the catastrophic core is
   refused, an installer piped into a shell is named in the dialog.
5. **Each account's own identity**: key, passphrase and servers are the account's, never the
   machine owner's `~/.ssh`, config or ssh-agent ([USER_ISOLATION.md](USER_ISOLATION.md)).
   For the same reason `host_bash` refuses `ssh`, `scp` and `sftp` and points here.

Pinned in `tests/test_ssh.py` (a fake `ssh` on PATH and the real `ssh-keygen`).

> **Note on `channel_tools_unrestricted`:** this admin setting (default ON) lets channel sessions
> use the same tools as the main agent and lifts `channel_restrictions` for tools that rely on it
> (e.g. `browser_agent`). A tool that needs a person - `dangerous` and restricted on channels:
> `host_bash`, `python_exec` - is deliberately exempt, because code on a computer with no
> confirmation path must never be reachable from a messaging channel. This used to be a guard
> inside `host_bash` alone, and `python_exec` was lifted with the rest.
