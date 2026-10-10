# VAF Sandboxing

Security is paramount when allowing an AI to execute code. VAF uses **Docker Containers** to isolate generated code execution from your host operating system.

---

## Security Model

**Docker is REQUIRED for code execution.** There is NO fallback to host execution.

| Tool | Isolation | Use Case |
|------|-----------|----------|
| `python_sandbox` | The caller's own scratch environment (a Docker container of their own) | Safe code execution (default) |
| `python_exec` | Host System | Only with explicit user trust |

File tools (e.g. `librarian_agent`, `read_file`) block access to the VAF installation directory; the agent is instructed not to request operations on that path.

### Isolation model: Docker-level, not Python-level

VAF's sandbox isolation is enforced entirely at the **Docker container level**. There is no Python-level module blocklist - standard-library modules like `subprocess`, `socket`, and `os` are importable inside the container. What prevents abuse is:

- **One container per person** - `python_sandbox` and `run_tests` run in the caller's scratch environment (see "Sandbox environments" below). Another account's code never runs in it, so one person's run cannot read another person's files or processes.
- **Process namespace isolation** - processes cannot escape the container
- **Filesystem isolation** - no host path is mounted (code runs in `/tmp/vaf_run_*` per execution, inside the person's container)
- **Unprivileged** - a non-root user, every Linux capability dropped, `no-new-privileges`, a process limit
- **Resource limits** - 512 MB memory, 0.5 CPU cores (hard limits via Docker)

**What is NOT blocked at Python level:**
- `import subprocess` - works, spawns processes inside the container only
- `import socket` - works; the scratch environment has outbound network access (needed for pip and the Tool Bridge)
- `import os` - works; filesystem access is limited to what Docker mounts, not by Python

See [`SANDBOX_MODULES.md`](SANDBOX_MODULES.md) for the full module reference and security details.

The browser container is a separate sandbox with its own design doc and is NOT covered here: see
[`BROWSER_AGENT.md`](../agents/BROWSER_AGENT.md) for its image, ports and health probe, and section 5 of
[`USER_ISOLATION.md`](USER_ISOLATION.md) for the per-user browser pool.

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

The service stack starts the daemon where it can (`vaf/core/service_stack.py`); the sandbox
lanes themselves only ask whether it answers.

---

## The scratch environment (`python_sandbox`, `run_tests`)

Each person has one scratch environment, created on first use and kept running between calls
so a run starts in well under a second (measured: `docker exec` of `python3` in about 80 ms).

| Resource | Detail |
|----------|--------|
| **Container** | `vaf-env-<scope hash>-scratch`: one per person, fixed name, so the web server, the CLI and a coder child converge on the same one |
| **Image** | The sandbox environment image (`vaf-sandbox-env`, Python 3.12 with pytest, Node.js, git, a compiler). Until it is built, `python:3.12-slim-bookworm`, so execution never waits for a build |
| **User** | Non-root: the caller's uid on Linux, the image's uid 10001 elsewhere. `--cap-drop ALL`, `no-new-privileges`, `--pids-limit` |
| **Memory / CPU** | 512 MB / 0.5 cores; exempt from the per-person count and the memory floor of other environments, so this lane keeps answering |
| **Network** | Its own bridge network with outbound internet (pip) and `host.docker.internal` for the Tool Bridge. No other environment shares it |
| **Workspace** | A directory per run, `/tmp/vaf_run_<id>`, removed after the run. Packages installed via the `packages` parameter go into that run's `_pkgs` (pip `--target`, `--no-cache-dir`, `PIP_TARGET`/`PYTHONPATH` set), so nothing accumulates. `export_files` copies only files inside the run's own directory, and a link or folder the copy produced is removed instead of delivered |
| **Timeout and Stop** | Every command of a run carries a marker (`VAF_RUN_ID`) in its environment. A timeout (`timeout -s KILL` inside the container) or a Stop kills exactly that run's processes, found through `/proc/*/environ` (`vaf/core/containers.py`, `exec_bounded`); killing the host's `docker exec` client alone would leave them running |
| **Lifetime** | Removed whole 24 h after its last use (`sandbox_env_temp_ttl_hours`), stopped at VAF's quit |

`run_tests` copies the project (without `.git`, `node_modules`, virtualenvs and build output; at
most 50 MB) into a fresh `/tmp/vaf_tests_<id>` in the same scratch environment, runs the command
there, and removes the copy in a `finally`. The host project is never written. pytest is in the
image; on the fallback image it is installed on demand into the unprivileged user's site.

Before the scratch environment existed, every account shared one container (`vaf-sandbox`,
`python:3.11-slim`, running as root), and the runs were kept apart by directory names under `/tmp`:
a concurrent run could list and read another account's working directory and, through
`export_files`, copy files out of it.

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
| Token | `secrets.token_hex(16)` per execution - mismatches rejected (HTTP 403). It reaches the run as its environment: `docker exec -e VAF_BRIDGE_TOKEN` carries only the name, the value travels in the docker client's environment. Written into the command, it sat in the cmdline of the host client and of the shell in the container, readable from `/proc` by other runs there |
| Binding | `0.0.0.0` on host, random ephemeral port. Accessible from any interface on the host; relies on the per-execution token for authentication. |
| Trust gates | All calls go through `agent.execute_tool()` - full VAF gate pipeline applies |
| Cleanup | `bridge.stop()` in `finally` block - no port leak on crash |

### Host gateway by OS

The scratch environment connects back to the host via `host.docker.internal` on all platforms:

| OS | How it resolves |
|---|---|
| Windows | Docker Desktop DNS alias - automatic |
| macOS | Docker Desktop DNS alias - automatic |
| Linux | `--add-host host.docker.internal:host-gateway` when the scratch environment is created injects the host IP (Docker 20.10+) |

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
- Until the sandbox environment image is built, the scratch environment pulls `python:3.12-slim-bookworm` on first use
- Ensure you have internet access for the first run
- Manual pull: `docker pull python:3.12-slim-bookworm`

**Error: "vaf_tools: bridge unreachable"** (when using `with_vaf_tools=True`)
- The scratch environment cannot reach the host via `host.docker.internal`
- From inside it, check resolution: `docker exec vaf-env-<hash>-scratch getent hosts host.docker.internal` (`docker ps --filter label=org.veyllo.vaf.env.kind=scratch` lists the names)
- Check that no firewall rule blocks the ephemeral port range: `sudo ufw allow 32768:65535/tcp` (temporary test)
- Requires Docker 20.10+ for the `host-gateway` special value

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

## Fetching from the network: the destination guard (`vaf/network/egress.py`)

The sandbox keeps CODE away from the host. The agent's FETCHES are the other way in: a
URL a model, a web page, a search result or a mail hands the agent is fetched by the VAF
process itself. On this machine a request from `127.0.0.1` without a token is the owner
(the "Localhost Bypass" in [NETWORK_FEATURES.md](../setup/NETWORK_FEATURES.md)), so an
unguarded fetch of `http://127.0.0.1:8005/api/contacts` returned the owner's contacts -
measured, together with the account list and the configuration, before this guard
existed. The same request reaches the cloud metadata service at `169.254.169.254` and
every device on the LAN.

Every fetch whose URL VAF does not choose itself goes through `egress_session()`:

| Destination (`binding.classify_address`) | Fetched? | Logged |
|---|---|---|
| Internet (public) | yes | no |
| LAN / overlay (RFC 1918, `100.64.0.0/10`, `198.18.0.0/15`, `fc00::/7`) | yes while `egress_allow_private_hosts` is on (default) | each fetch in the `egress` log |
| This machine (loopback, in every spelling: `::ffff:127.0.0.1`, NAT64 `64:ff9b::7f00:1`, 6to4, Teredo, `::7f00:1`) | never | security event `egress_blocked` |
| Link-local incl. the metadata service, multicast, documentation, reserved, unspecified | never | security event `egress_blocked` |

How it holds:
- The host is resolved ONCE and every answer is judged; one refused address refuses the
  fetch. The connection goes to the checked address and TLS still verifies the
  certificate against the NAME, so a name that answers differently a moment later (DNS
  rebinding) cannot move the connection.
- Every redirect hop is judged like the first request; at most five hops.
- Only `http` and `https`. The site egress proxy comes from `system_proxy_for`, never from
  the request library's own environment merge (the uppercase `HTTP_PROXY` is not trusted).
- `EgressPolicy(trusted_host=...)` lets one host an administrator registered (an MCP
  server on this machine) be local; a redirect elsewhere is judged normally.
- `tests/test_egress_static_guard.py` refuses a raw `requests`/`httpx`/`urllib` call
  with an outside URL in the agent's tools, the WebDAV client and the MCP lane.

NAMED BOUNDARIES:
- Behind a site proxy the proxy resolves the name, so nothing can be pinned. A name
  that resolves LOCALLY to a refused address is still refused; a name the local resolver
  does not know (split horizon) is the proxy's to judge.
- A shell command (`host_bash`, `curl`) is the person's grant, not an HTTP client this
  guard wraps.
- VAF's own fixed internal calls (sub-agent and workflow IPC to its backend) and the A2A
  rooms (their own `wss` client with a pinned CA) are not fetches of an outside URL.
- The receiving side still treats a tokenless loopback request as the owner; replacing
  that with a per-start IPC token is a separate change.

## Sandbox environments

### The environment image (`vaf/core/environment_image.py`)

One image serves every sandbox environment and the registries proxy:

- Python 3.12, Node.js LTS, git, a C/C++ compiler for packages without a wheel, ripgrep and jq;
- `tinyproxy`, which the proxy container runs from the same image;
- `chromium-headless-shell`, which takes screenshots of what an environment serves.

| Property | Detail |
|---|---|
| **Source** | `vaf/assets/sandbox/Dockerfile`, inside the package. It goes to `docker build -` on stdin with no build context: a wheel install has no `docker/` directory, and an embedder building on the facade gets the same image. |
| **Tag** | `vaf-sandbox-env:<first 12 hex of the Dockerfile's sha256>`. A changed Dockerfile is a new image. After a successful build, the images of earlier Dockerfiles are removed; one still used by a container stays until that environment is deleted. |
| **Pins** | Node.js comes from the official release, version-pinned with a sha256 per architecture. The architecture is read from `dpkg`, not from BuildKit's `TARGETARCH`. pytest is pinned. |
| **User** | `sandbox` (uid 10001) with a HOME every uid can write: on Linux an environment runs as the caller's uid, which has no passwd entry. `git safe.directory '*'`, because a container holds only the caller's own project. |
| **When it is built** | The stack start begins the build in the background, and so does the scratch environment while the image is missing; after a background attempt that ended without an image (offline, a mirror down) the next one waits ten minutes (`BACKGROUND_RETRY_S`). `ensure_image()` builds when a caller needs the image now and never waits. A lock is shared across processes (`filelock`), so the web server, the CLI and a coder child never build it twice. |
| **While it is missing** | The scratch environment that `python_sandbox` uses runs on `python:3.12-slim-bookworm`, so code execution never waits for a build or an offline machine. That fallback has no Node.js and no browser. |
| **Freshness** | Past `sandbox_env_image_max_age_days` (default 14, `0` = off), the next build pulls the base image and skips the cache, so the Debian packages inside receive their security updates. |
| **Size (measured)** | Built in 105 s on a warm docker cache; 1.64 GB on disk, 1.44 GB of it beyond the Python base image it shares. |

**Why the headless shell and not the full Chromium.** The full browser's headless mode crashes in a container without a crash database. Measured with Chromium 151: `chrome_crashpad_handler: --database is required`, and with crashpad switched off an "FD ownership violation". Debian's `chromium-headless-shell` is the separate binary built for exactly this use. It took a 1280x800 screenshot of a page the environment served in 215 ms, as a non-root user with every capability dropped and no network beyond the environment's own.

**Licences.** The image is built locally only and never published. tinyproxy is GPL-2.0, and `chromium-headless-shell` carries the Chromium licences. Each runs as a separate program next to the other Debian packages, an aggregate with no code linkage. See [THIRD_PARTY.md](../legal/THIRD_PARTY.md).

### Environments (`vaf/core/environments.py`)

An environment is a container, a volume and a network of its own, per person. The main
agent and the coder reach the same environment by its id; `vaf env` and the web UI list
and remove environments. How many exist and run, machine-wide, is shown next to the Docker
services (`vaf repair --check`, `vaf top`, the Update and Repair dialog; see
[DOCKER_SERVICES.md](../setup/DOCKER_SERVICES.md#sandbox-environments)). Applications use the same object (`vaf.get_environment_manager()`, see
[EMBEDDING.md](../EMBEDDING.md#sandbox-environments-vafenvironmentmanager)).

| Property | Detail |
|---|---|
| **Kinds** | `temporary`: removed whole (container, volume, network, record) when it expires, by default 24 h after its last use. `project`: kept across restarts with its installed packages; stopped after `sandbox_env_idle_stop_minutes` without a running process, never removed unasked; may mount a host project at `/workspace`. **Scratch**: one per person under a fixed name, used by `python_sandbox` and `run_tests`; it extends itself on use and is exempt from the count and memory limits, because those lanes must keep answering. |
| **Names** | `vaf-env-<scope hash>-<id>`, `vaf-env-vol-<id>`, `vaf-env-net-<id>`. The scope hash (12 hex of the scope's sha256) keeps a container listing from saying who uses the machine. |
| **Identity** | Docker labels `org.veyllo.vaf.env.*` (id, owner, kind, network, created) on all three objects, so a crash between the creates leaves nothing a label listing cannot find. Labels cannot change afterwards; the name, expiry, last use and the project path live in a record per environment under `<vaf dir>/environments/`, written atomically under a lock shared across processes. |
| **Ownership** | The owner label, checked on every operation. Another person's environment answers like a missing one, so ids cannot be probed. An admin may list and delete it, never run anything in it. A missing scope means the machine owner (the rule of `config.resolve_caller_username`); with no owner configured the call is refused. The shared "no scope" bucket of the old sandbox does not come back. |
| **Process** | Non-root: the caller's uid:gid on Linux, so files in a mounted project stay theirs, and the image's uid 10001 elsewhere. `--init`, `--cap-drop ALL`, `no-new-privileges`, `--pids-limit`, `--memory`, `--cpus`. No docker socket, no host path besides the project, no host secret. |
| **Root lane** | For the coder only: `bash(as_root=true)` runs ONE command as root inside a temporary or project environment, so it can install system packages for what it builds. Such an environment is created with six capabilities added back (`ROOT_LANE_CAPS`: CHOWN, DAC_OVERRIDE, FOWNER, FSETID, SETUID, SETGID), which only a root process can use: the environment's own user has no ambient capabilities and `no-new-privileges` keeps setuid from granting any. Measured: with every capability dropped a root `apt-get` fails; with these six it updates and installs, while the environment's user still cannot write a system folder. What a root command leaves in `/workspace` is handed back to the environment's user at once, because a mounted project is the person's own folder and a root-owned file there could not be removed without sudo. First the setuid and setgid bits come off every file there, whoever owns it: a mounted project is a folder on the host, and a setuid file left in it would be a way to root on this machine (measured: a root-copied `sh` with mode 4755 ends up the person's, without the bit). A give-back that fails is reported in the command's result, never swallowed. The root command and its give-back carry a run marker like every other run, and the reaper asks for markers as root as well as as the environment's user, because each reads only its own processes' environment (no `CAP_SYS_PTRACE`): asked as the user alone, it stopped an environment in the middle of a root `apt-get`. The `registries` network lets the image's own apt sources through (`deb.debian.org`, `security.debian.org`). **Named boundaries:** the main agent's `sandbox_exec` and `vaf env exec` have no root lane, the scratch environment has none, an environment created before the lane existed refuses (create a new one), and nothing ever runs as root on the host. |
| **Project mount** | `-v <project>:/workspace:z`. `:z` relabels the directory for SELinux, which is enforcing on Fedora-family hosts. The path must pass `is_unsafe_project_dir`, `assert_safe_workspace` (VAF's code, the home directory and `/` are refused, which is what keeps `:z` away from them) and the person's file jail. |
| **Limits** | `sandbox_env_*` in [CONFIG_SCHEMA.md](../setup/CONFIG_SCHEMA.md), all admin-only. A refusal names its reason. |

**Network profiles, measured on Docker 29.7.** Every environment has its own network.

| Profile | Network | Reaches |
|---|---|---|
| `none` | `--internal` with `com.docker.network.bridge.gateway_mode_ipv4=isolated` | Nothing outside the environment's network. A plain `--internal` network still reached the host through its own gateway (VAF's port 8443 answered from one); in isolated gateway mode the host was unreachable on every address. |
| `registries` | The same, plus the shared proxy `vaf-env-proxy` | Only the hosts in `sandbox_env_registry_hosts`, through tinyproxy (`FilterDefaultDeny`, anchored host patterns, CONNECT to 443 and 80). Measured: pip and npm installed through it; `example.com`, an IP literal and a look-alike host got 403, and the direct route was closed. The reaper stops the proxy while no registries environment runs; every use of one that runs starts it again if it is gone (measured: stopped by hand, the next command brought it back and pip downloaded). |
| `open` | An ordinary bridge | The internet. |
| scratch | An ordinary bridge plus `host.docker.internal` | The internet and the Tool Bridge, as `python_sandbox` always had. |

The isolated gateway mode needs Docker 28. On an older engine, the network is created
without it and the environment's `degraded` field says that the host is reachable on the
network's gateway.

**The agent's tools** (`vaf/tools/environments.py`), each acting as the caller and refused
on messaging channels:
- `sandbox_manage`: create, list, stop, delete. A project path runs through `is_safe_path` and
  the write jail before the manager's own checks. A project environment asked for without a
  path gets a new, empty folder in the chat's own project area (`VAF_Projects/<account>/<chat>/`,
  where the coder makes its projects too; removed again if the create is refused); without a
  chat, the path is required. The answer names the call that puts the coder to work in it:
  `coding_agent(project_path=..., environment=...)`. The manager itself and `vaf env create
  --project` keep a pathless project environment on a volume of its own: a terminal has no chat
  area to put a folder in.
- `sandbox_exec`: a command in `/workspace` (or `cwd`), bounded and stop-aware.
  `background=true` starts a process that keeps running (a dev server). The command travels
  in the environment of the docker client, not on a command line; its output goes to
  `/tmp/vaf-env-proc/` inside the container; it carries the marker `VAF_PROC_ID`.
- `sandbox_files`: read, write, list inside the environment.
- `sandbox_transfer`: `copy_in` and `copy_out` between the environment and the caller's own
  folders; the host side runs through `is_safe_path` and the write jail, and only regular
  files and folders arrive.

- `sandbox_preview`: one look at a page the environment serves or holds. `localhost` is
  the environment itself, so a dev server bound to `127.0.0.1` works. You get a screenshot
  (saved into the chat workspace and shown in the chat), the console output, the page errors
  and the rendered text. `chromium-headless-shell` takes it inside the environment's own
  container: no browser container joins the environment's network (a person's browser
  serves CDP without authentication on its network), and no port is published. Failed
  requests are not measured this way, and the report says so. A coder bound to an
  environment gets the same lane through `render_check`, and is not offered `browser_agent`,
  which cannot see the environment; a `browser_agent` call aimed at `/workspace` or a
  `localhost` address is refused with a pointer to `render_check`.

From the terminal, `vaf env` does the same as the machine owner, behind the terminal door:
`list [--all]`, `create --temp | --project NAME [--path DIR] [--network ...] [--memory MB]`
(waits for the image the first time), `exec ID -- CMD`, `shell ID`, `ps`, `logs`, `kill`,
`preview ID TARGET`, `stop`, `delete` and `prune`. `exec` keeps each word of `CMD` whole
(`-- python3 -c "print('a b')"`); a single quoted word is taken as a shell line, so pipes
and `&&` work (`-- "pip install -r requirements.txt && pytest"`).

Background processes are listed, read and stopped with `host_process` (ids
`e-<environment>-<process>`), next to the chat's host commands. The record of who started
one, from which chat, lives on the host beside the environment's record, so code in the
environment cannot rewrite it; whether it runs and its exit code are read from the
container. The reaper wakes the chat when one ends on its own, and ends one after
`sandbox_env_process_max_hours`. None of these tools is offered to thinking runs or on a
4k context.

**Housekeeping.**
- **Reaper.** The web server runs one: it removes expired temporary environments, stops idle project environments, and clears records and labelled volumes or networks a crash left behind.
- **Busy.** It is asked of docker, not of the reaper's memory: a process inside that carries a VAF run marker (`VAF_RUN_ID`, `VAF_PROC_ID`) keeps an environment alive. When the answer cannot be had, the environment counts as busy.
- **Repeatable removal.** Removing an environment twice does no harm, because several reapers may race.
- **Quit.** VAF's quit stops every environment that is not busy. They are started with `docker run`, so `compose stop` never sees them.
- **Revoked access.** An account whose access is taken away has its environments stopped.

**Named boundaries.**
- `open` is not "internet only". Through the gateway it reaches whatever listens on the host's `0.0.0.0` (the Tool Bridge, token-protected; VAF in LAN mode, login-protected), and the LAN is reachable.
- `registries` does not stop data leaving through the allowed hosts themselves (`npm publish`, a push with a token the code brought along).
- No disk quota. overlay2 has none without xfs project quotas, so the number of environments per person is limited instead, and their size is shown.

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
pass/fail, instead of guessing. It copies the project into a fresh `/tmp/vaf_tests_<id>` in the
CALLER's scratch environment (see "The scratch environment" above), runs `python3 -m pytest -q`
under an in-container `timeout -s KILL`, returns the summary, and removes the copy in a
`finally`. It is `read`-level (no host side effects). Every refusal starts with
"Cannot run tests:", which the coder counts as a failed run.

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

### `ftp` - files on a web space (`vaf/tools/ftp.py`)

Lists, uploads (a file or a folder), downloads and deletes over FTPS, or plain FTP when
written `ftp://` (`vaf/core/ftp.py`, Python's ftplib). The controls are the `ssh` tool's:
confirmed in the chat, a trusted folder does not silence it, the first connection to a server
is always asked and refused where nobody is asked, never over a messaging channel, a regular
account only when its allowlist names it, the local side under the account's write jail. Its
own: the certificate is checked (an authority's, or the fingerprint remembered at the first
connection), the address a server names for passive transfers is not followed, cloud metadata
addresses are refused, and `host_bash` refuses `ftp`, `lftp`, `ncftp` and `curl`/`wget` with
an `ftp://` address (`ftp_transfer` in `vaf/core/command_policy.py`) and points here. Pinned
in `tests/test_ftp_core.py` and `tests/test_ftp_tool.py`, against a small FTPS server from the
standard library (`tests/ftp_stub.py`).

> **Note on `channel_tools_unrestricted`:** this admin setting (default ON) lets channel sessions
> use the same tools as the main agent and lifts `channel_restrictions` for tools that rely on it
> (e.g. `browser_agent`). A tool that needs a person - `dangerous` and restricted on channels:
> `host_bash`, `python_exec` - is deliberately exempt, because code on a computer with no
> confirmation path must never be reachable from a messaging channel. This used to be a guard
> inside `host_bash` alone, and `python_exec` was lifted with the rest.
