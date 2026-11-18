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
| **Workspace** | A directory per run, `/tmp/vaf_run_<id>`, removed after the run (a background run's when it is reported or stopped). Packages installed via the `packages` parameter go into that run's `_pkgs` (pip `--target`, `--no-cache-dir`, `PIP_TARGET`/`PYTHONPATH` set), so nothing accumulates. `export_files` copies only files inside the run's own directory, and a link or folder the copy produced is removed instead of delivered |
| **Timeout and Stop** | Every command of a run carries a marker (`VAF_RUN_ID`) in its environment. A timeout (`timeout -s KILL` inside the container) or a Stop kills exactly that run's processes, found through `/proc/*/environ` (`vaf/core/containers.py`, `exec_bounded`); killing the host's `docker exec` client alone would leave them running. Output is unbuffered (`PYTHONUNBUFFERED=1` for every Python the run starts), and a run that is stopped answers with everything it printed until then: measured with the command's own shape, a buffered run killed at its limit had lost all three lines it printed |
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

A run waits 120 s by default and at most 600. When the time is up, the run is stopped and the
answer starts with `[timed out after N s]`, followed by everything the code printed so far and,
as its last line, how to give the work more time.

```python
python_sandbox(code="import time; time.sleep(5); print('done')", timeout=60)
```

### Long work in the background

```python
python_sandbox(code="import subprocess\nfor i in range(10):\n    subprocess.run(['pip', 'install', 'requests'])\n    print('done', i)",
               background=True)
```

The call returns at once with a handle (`[started in the background] e-<environment>-<process>`)
and the chat stays free. The code runs as a background process in the same scratch environment
(below, "Background processes"); when it ends, the chat gets a new message with the end of its
output and its exit code, and the run's folder is removed. `host_process` reads its log or stops
it before then. Refused, with "Nothing ran", together with `with_vaf_tools` (the bridge lives only
as long as the call) or `export_files` (files are delivered only inside the call), in a call
without a chat, in a messaging-channel chat, inside a sub-agent's process, in a terminal lane
that does not run the turns VAF starts by itself (`vaf run --web`) and while a tool is being
trained.

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
  (it needs a person, like `host_bash`, see "Shell execution surfaces" below); from the
  Veyllo App, the account holder's own device, only after a tap on a card there, every call
- Refuses code that moves files over FTP (an import of ftplib, a string that names it as
  `__import__`, `importlib.import_module` (also under an alias) or `-m` takes it, an `ftp://`
  or `ftps://` address, or such
  code as a written-out string handed to exec, eval or compile) and points at the `ftp`
  tool, before its trust check, so a stored "always" does not run it either. The same check
  (`python_code_categories` in `vaf/core/command_policy.py`) judges `python -c CODE` and
  `python -m ftplib` in `host_bash`, also under `pypy`, a free-threaded `python3.13t`, and
  behind a wrapper, a shell or a runner. The command a project runner starts (`uv run`,
  `poetry run`, `conda run` ...) or a package runner starts (`npx`, `bunx`, and after their
  verb `npm exec`, `pnpm dlx`, `pnpm exec`, `yarn dlx`, `bun x`, `bun exec`, `pipx run`,
  `pyenv exec`, `opam exec`, `mamba run`, `micromamba run`, also behind `corepack` or `!`)
  is classified like one `bash -c` carries, for every category, so `uv run env python -c ...`,
  `uv run bash -c "python -c ..."`, `uv run rm -rf /` and `npx rm -rf /` are refused like the
  same commands without the runner. A package runner's other subcommands (`npm install`,
  `npm run build`, `pipx install x`) start nothing here, and a shell string it is handed
  (`npx -c 'rm -rf /'`, `npx --call='...'`, `npm exec -c '...'`, `bun exec '...'`, and
  npm's `--call` also before its verb, `npm --call='...' exec`) is read as a shell command.
  Named boundary: `deno`, `cargo run`, `go run`, `nix run`, `mise exec`, `direnv exec` and
  `yarn workspace NAME exec` are not read as runners, and the package script `npm run X` or
  `yarn X` starts is a file, not read like a shell script's content. A runner's option table
  is not kept, so the word after any runner option is read both as that option's value and as
  the command. A harmless command after such an option is therefore refused only when what
  follows it is refused in its own right (`conda run -n py --no-capture-output echo python -c
  'import ftplib'`; `uv run -q echo hi` passes), and a value that is itself a refused
  program's name is refused as one (`uv run --with ssh pytest`). That is the safe side. `--`
  before the command lifts both when no word stands in a value position before it; after one,
  that word may be the command and `--` its argument (`uv run -q ssh -- host` logs in to
  `host`), so both readings are classified, and a value written with `=` (`uv run --with=ssh
  -- pytest`) leaves no such word. Named boundary: the check reads the code it is given; code
  built at runtime (`"ftp" + "lib"`, a string assembled before exec), a script file, a heredoc
  fed to the interpreter, or an interpreter under another name or in a container is not seen,
  as a shell script's content is not read. The control for host code stays the confirmation
  and the account opt-in.
- Refuses code with a string that names a network share or a Windows device path
  (`open(r"\\server\share\x")`, `pathlib.Path(r"\\?\UNC\...")`, `"file://server/..."`)
  where nobody confirmed the call: a standing or chat grant lets it run in the coding agent
  and in a workflow step, and there `_call_confirmed` is False. On Windows opening such a
  path sends the account's NTLM login attempt to that host. "Only this time" in the chat
  runs it. The same reading (`share_paths` in `vaf/core/command_policy.py`, on each string
  constant) is the `share_path` category of `host_bash` below.

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
- A shell command (`host_bash`, `curl`) is not an HTTP client this guard wraps. In the
  chat it runs on the person's yes; in the coding agent and in workflow steps nobody is
  asked, and the account permission is the only answer (see "What an unattended host
  command reaches" under `host_bash`).
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
| **When it is built** | The stack start begins the build in the background, and so does the scratch environment while the image is missing; after a background attempt that ended without an image (offline, a mirror down) the next one waits ten minutes (`BACKGROUND_RETRY_S`). `ensure_image()` builds when a caller needs the image now (it blocks for the build) and is never held back by that pause. A lock is shared across processes (`filelock`), so the web server, the CLI and a coder child never build it twice. |
| **While it is missing** | The scratch environment that `python_sandbox` uses runs on `python:3.12-slim-bookworm`, so code execution never waits for a build or an offline machine. That fallback has no Node.js and no browser. |
| **Freshness** | Past `sandbox_env_image_max_age_days` (default 14, `0` = off), the next build pulls the base image and skips the cache, so the Debian packages inside receive their security updates. |
| **Size (measured)** | Built in 105 s on a warm docker cache; 1.64 GB on disk, 1.44 GB of it beyond the Python base image it shares. |

**Why the headless shell and not the full Chromium.** The full browser's headless mode crashes in a container without a crash database. Measured with Chromium 151: `chrome_crashpad_handler: --database is required`, and with crashpad switched off an "FD ownership violation". Debian's `chromium-headless-shell` is the separate binary built for exactly this use. It took a 1280x800 screenshot of a page the environment served in 215 ms, as a non-root user with every capability dropped and no network beyond the environment's own.

**Package index dates.** The image's `apt-get update` runs with `Acquire::Check-Date=false`, as the browser and speech images do: a build host whose clock is behind (common in VMs, WSL2 and CI) would otherwise reject freshly signed index files as "not valid yet". The signatures are still verified. NAMED BOUNDARY: the option switches off apt's time checks as a whole, so an index file past its `Valid-Until` is not refused either; a mirror that replays an old, validly signed index during a build would not be noticed. The image's age budget (`sandbox_env_image_max_age_days`) rebuilds it regardless.

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
| **Project mount** | `-v <project>:/workspace:z`. `:z` relabels the directory for SELinux, which is enforcing on Fedora-family hosts. The path must pass `is_unsafe_project_dir`, `assert_safe_workspace` (VAF's code and VAF's own state, each in either direction, the home directory, every folder above it such as `/home`, and `/` are refused, which is what keeps `:z` away from them) and the person's file jail. The record stores the resolved path, and every start checks it again (`vaf.core.folder_mount`): a new one (create, a restore's `readopt`) and a restart after an idle stop, because `docker start` resolves the stored path anew each time (measured: a folder renamed aside and linked elsewhere while the environment was stopped was mounted, and relabelled, at the next start). Before the start the path is opened as a handle with itself as the expected real path, so a link anywhere on the way, or a missing folder, refuses with nothing started. After it, on a daemon that shares this kernel, the host's kernel is asked what is mounted at `/workspace` (through `/proc/<the container's process>/root`, never a tool inside the container: a `stat` planted there by a root command printed the expected numbers with another folder mounted, measured), and it must be the handle's `dev:ino`; otherwise the container is stopped (killed when a stop does not take) and the start refused (`EnvironmentRefused`). Until that check passed the start is marked unverified in a file beside the record, with the identity held for that start and a token of the start: any other request or process that finds the container running checks the mount against that identity before its command, a late check clears only its own start's mark, and a container whose stop or removal failed stays refused. Named boundaries: a daemon in a VM (Docker Desktop) shows other numbers, so there the mount is not checked after the start; for a daemon that runs containers under another user (rootless, userns-remap) it is unmeasured whether the host can read the container's root, and a start it cannot read is refused; docker relabels the source it resolved before the check can refuse, so a folder swapped in during the moment between VAF's open and docker's start is relabelled; and the container's own command runs at the start, before the check, so a program the root lane left in place of `sleep` (an environment keeps its filesystem) can reach such a swapped-in folder, whatever folder the link names, until the check stops the container, about 100 ms later (114 ms measured); it needs both the root lane and a process winning that moment. A `docker start` made outside VAF is not checked. |
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
on messaging channels (from the Veyllo App, the account holder's own device, each call is put
to them on a card there instead):
- `sandbox_manage`: create, list, stop, delete. A project path runs through `is_safe_path` and
  the write jail before the manager's own checks; a path that is VAF's own state or contains it
  is refused (see "File tools and VAF's own state" below). A project environment asked for without a
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
  folders; the host side runs through `is_safe_path` (the read rule for `copy_in`, the write
  rule for `copy_out`) and the write jail, and only regular files and folders arrive.
  `copy_out` checks every entry of the stream before it writes the first: the folder names
  come from inside the environment, so into the parent of the data directory an entry named
  `vaf/custom_tools/...` would land in VAF's own state. One such entry refuses the whole copy.

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
`exec` and `logs` print the program's standard output byte for byte, escape sequences
included, as `docker exec` and `docker logs` do, so its colours and progress bars work. For
`logs` that program may be one the agent started (`sandbox_exec(background=true)`), whose
output the model can steer: read it in a terminal you would let that program write to.
`list`, `ps` and `preview` print without escape sequences (`vaf.terminal_text`), since they
show names, commands and page text the agent or a page chose.

Background processes (`sandbox_exec(background=true)`, `python_sandbox(background=true)`) are
listed, read and stopped with `host_process` (ids `e-<environment>-<process>`), next to the
chat's host commands; lists and the report show a process's label (`python_sandbox: <first code
line>`) where one was given. The record of who started one, from which chat, lives on the host
beside the environment's record, so code in the environment cannot rewrite it; whether it runs
and its exit code are read from the container. At most eight run in one environment at a time
(`processes.MAX_PER_CHAT`, the host lane's number); a ninth is refused with the running ones
named. The count and a placeholder record are written in one step under the state lock, so
starts at the same moment cannot all take the last place (which processes run is read just
before the lock, and a start docker answered after that read counts as running); the
placeholder goes when docker refuses the start, and one older than 60 s that never ran (a crash
in between) stops counting, is listed as "did not start" and is removed on the next look. A
process stopped while docker is still starting it is ended as soon as docker has started it:
the stop marks the record before its kill and reports it only while it is still marked, so in
every ordering the process is killed by the stop or the start, or, when the kills fail, stays
tracked and the limit ends it. A start whose placeholder was removed while docker was still
answering is ended too, and the 60 s count from the reservation itself. A background start from
the agent (`sandbox_exec`, `python_sandbox`, like `host_bash`) is refused where its end could
not be reported (`processes.background_refusal`): no chat, a messaging-channel chat, a
sub-agent's process, or a process where no lane runs the turns VAF starts by itself (the modern
terminal lane `vaf run --web` declines them). The process that started one with a chat to
report to watches it itself every 5 seconds, so the report lands in that process's own queue
(the web server's, the full-screen app's). The reaper is the backstop for a process whose
watcher is gone (a restart, a short `vaf env exec --background`) and for one started without a
chat; it leaves the report of a record to another live process that watches it. Whoever reports
first claims the record under the state lock, so one ended process is reported once by intent
(without `filelock` that lock holds only between threads). A process past
`sandbox_env_process_max_hours` is ended by whoever looks, the reaper included, also when
another process watches it (that watcher may have stopped looking); it counts as ended only
when the kill succeeded and the process is gone, otherwise the next look tries again. The
record then says it was ended at the limit, and the report says so. Exit 137 is reported as
"killed: out of memory or ended from outside". A run folder the process was started with
(`/tmp/vaf_<lane>_<hex>` only) is removed when it is reported or stopped; its log stays as long
as the container, so `host_process(action="log")` still reads it after the report. None of
these tools is offered to thinking runs or on a 4k context.

**Housekeeping.**
- **Reaper.** The web server runs one: it removes expired temporary environments, stops idle project environments, clears records and labelled volumes or networks a crash left behind, and reports background processes that ended with nobody else watching them.
- **Restore.** A restore on another machine brings back the records and volumes, not the containers, and the reaper would clear such a record and its volume as a crash leftover. `vaf backup restore` calls `EnvironmentManager.readopt()` first: each project environment gets its container back from its record (with today's image), a temporary one is dropped, and one whose mounted host folder is missing here is reported. The host folder a restored record names passes the same checks as a new environment's mount (not the home directory, not a system folder) and is stored resolved; a refused one is reported as failed. The rest of the record is held to what `create` allows as well: the id is the record file's, the container, volume and network names are derived from it and the owner (they are what removing an environment deletes, so a record never names them), a network profile that is no profile becomes `none` rather than the open network, and the network and memory are capped by today's limits instead of refused, which would hand the work to the reaper. For the same reason the reaper clears an orphan record with only the volume and network VAF names for that id. Before anything is replaced, a restore whose backup holds project environments makes sure the environment image is available and refuses otherwise. The restored volume is handed to this machine's environment user, which need not be the uid the files had on the old one.
- **Busy.** It is asked of docker, not of the reaper's memory: a process inside that carries a VAF run marker (`VAF_RUN_ID`, `VAF_PROC_ID`) keeps an environment alive. When the answer cannot be had, the environment counts as busy.
- **Repeatable removal.** Removing an environment twice does no harm, because several reapers may race.
- **Quit.** VAF's quit stops every environment that is not busy. They are started with `docker run`, so `compose stop` never sees them.
- **Revoked access.** An account whose access is taken away has its environments stopped.

**Named boundaries.**
- `open` is not "internet only". Through the gateway it reaches whatever listens on the host's `0.0.0.0` (the Tool Bridge, token-protected; VAF in LAN mode, login-protected), and the LAN is reachable.
- `registries` does not stop data leaving through the allowed hosts themselves (`npm publish`, a push with a token the code brought along).
- No disk quota. overlay2 has none without xfs project quotas, so the number of environments per person is limited instead, and their size is shown.

## File tools and VAF's own state

VAF keeps what it executes or obeys later in its state roots (`vaf.core.data_stores.roots()`
without the projects): `~/.vaf` (or `$VAF_CONFIG_DIR` in Docker mode) with the skills and
workflows, the data directory with `custom_tools/*.py` (run at every agent load) and
`mcp_servers.json` (commands VAF starts), and the config directory with the tool grants under
`trust/`. The agent's file tools (`write_file`, `edit_file`, `move_file`, `download_file`, the
librarian's renames, `cloud_storage` save, the local side of `ssh` and `ftp` downloads,
`sandbox_transfer` `copy_out`) refuse to write there for every account, the owner included,
on every lane. The decision is `vaf.is_vaf_state`; `is_safe_path` asks it unless its caller
passes `read_only=True`. The writers of those stores are their own tools, which check what
they save: `create_agent_tool` and the Web UI's custom-tool upload, `create_skill` and
`update_skill`, `create_agent_workflow`, the MCP and trust registries.

Reading is refused as well, for every account and on every lane, with three exceptions:
`is_safe_path(path, read_only=True)` asks `vaf.is_vaf_state(path, mode="read")`. The same roots
hold every account's messages, mail, calendars and contacts, the key files, the MCP server list,
the messaging endpoints and the tool grants, and in Docker mode the configuration folder with
the API keys. A credential read out through a chat prompt, or attached to an outgoing message
by injected text (the senders resolve attachments through the same read screen), outlives
anything this instance can revoke. The exceptions are the store rows marked `agent_readable`
in `vaf/core/data_stores.py`, each a folder a tool hands the model paths into: `skills/`
(`use_skill`), `workflows/` and the data directory's `custom_tools/` (`create_agent_tool`'s
edit replaces a tool's whole source, so the model reads the source it edits). They are
compared case-exact, so another spelling of them is refused. A path is judged in both its
spellings, as given and resolved, and every spelling that lies inside VAF's state must lie in
one of those folders: a link in `custom_tools/` to a key file is refused, while a skill folder
linked to a folder outside VAF stays readable, because its resolved spelling is no state.

A path is checked at one moment and written at the next, and code in a sandbox environment
(`sandbox_exec`, the coder's jailed `bash`) can create links in its mounted folder, which the
host sees as ordinary entries: a link at the name a tool is about to write, or a folder
swapped for a link in between. So every tool that writes a file under a name a model chose
opens it with `vaf.open_for_write`: the folder is opened first, a link as the last component
is refused instead of followed, the folder's real path (the kernel's answer for the open
handle) is asked `is_vaf_state` and `is_host_config` again, and only a regular file is written (a pipe is refused
without blocking). That covers `download_file` and the `.part` files of the `ssh` and `ftp`
downloads, `document_writer`, `document_agent` and `research_agent` (text, Markdown and Word),
`render_check`'s screenshot, and the coder's own writes into its project (the `PARTIAL_` backup,
the default `.gitignore`, the README of the document phase, the template files), the
project notes and the coder's task state in a project's `.vaf` (`vaf/core/persistence.py`:
a `.vaf` that is a link is not used, a missing one is made relative to the checked handle of
the project folder, never by path, and every write is pinned to that folder's real path), and the
cloud: `cloud_storage` `download` and `retrieve` copy into the Downloads folder through it (a
tenant's Downloads folder lies in their own project root, which an environment of theirs can
mount), and every provider writes its downloads through it. A cloud file's name is the remote
owner's choice and may carry `/` and `..`: `download` saves only under a plain file name
(`vaf.safe_entry_name`, held inside Downloads by `vaf.contained_path`), and the sync engine
holds each remote path inside the local sync folder (a rooted path is refused, not stripped).
The sync's download direction therefore does not run today, for two reasons: the providers
name remote files with a leading `/`, which is refused as rooted, and the sync folder lies in
VAF's own state, so `open_for_write` would refuse the write as well. It needs matching keys and
a write lane of its own. A guard test
(`tests/test_writes_do_not_follow_links.py`) refuses a new raw write in `vaf/tools` and
`vaf/cloud` that is not on its reviewed list.

**Named boundaries.** On Windows there is no open relative to a folder handle and no
`O_NOFOLLOW`: a link at the name is refused and the real path is checked before and after the
open, but a link or junction swapped in between is followed for that one open (a file it
creates in VAF's state is removed again only while the link is still in place at the second
check; that check resolves the path, so a link into VAF's state removed before it lets the
write land there, unrefused; and the removal is best effort, so a failed one leaves an empty
file in VAF's state while the write is still refused). `write_file` and `edit_file` write a temporary file
beside the target and rename it into place; the rename resolves the folder again, so a folder
swapped for a link between the check and the rename carries the file with it. That takes a
process racing the write from inside a mounted folder. A Word export is no longer replaced
atomically: it is built and checked in memory, so every failure before the first byte leaves
the old file alone, but a failed write on a full disk can leave a partial file. A hard link is not seen, but a sandbox
environment cannot make one to a file outside its mount, and a mount that holds VAF's state is
refused. When neither `Documents/VAF` nor `Downloads/VAF` can be created,
`Platform.get_vaf_output_dir()` falls back to `~/.vaf/output`, inside VAF's own state. Its one
writer, `write_file`'s reroute of a new folder at the top of the home folder, then puts the
file there without asking again; VAF neither loads nor runs anything from that folder.

**Named boundary: `create_agent_tool`.** It is `admin_only`, refused on messaging channels
(from the Veyllo App only after the admin's tap on a card on their device),
and runs without a confirmation, so in the admin's own web chat it still writes and loads
Python that the agent composed, as it is meant to. That is the one remaining agent lane to
code VAF loads; text injected into the admin's web chat can reach it.

## File tools and the home directory's configuration

Some files in the home directory are run or obeyed by other programs at the next login, shell
or Python start, outside every VAF containment: the shell start files, autostart entries,
systemd user units, `environment.d`, the Python user site (a `.pth` file runs at every Python
start), `~/.local/bin`, `~/.gitconfig`, `~/.npmrc`, launch agents, the PowerShell profiles. The
file tools run unconfirmed on the messenger chats, in workflows, automations and background
runs, where injected text can drive them, so they refuse to write there for every account, the
owner included. The decision is `vaf.is_host_config` (`vaf/core/workspace_guard.py`), in two
parts, so there is no list of file names to drift:

- every entry of the home directory whose name starts with a dot, and everything below it
  (deny-by-default, the polarity of `~/.vaf`);
- explicit roots outside a dot folder: `$XDG_CONFIG_HOME` and `$XDG_DATA_HOME` when set to an
  absolute path, `%APPDATA%`, `%LOCALAPPDATA%`, `~/Library/LaunchAgents`, `~/Library/Python`
  (the Python user site of a macOS framework build), `Documents/PowerShell` and `Documents/WindowsPowerShell`.

Paths are compared like VAF's state, in every spelling (as given and resolved, case-folded,
stream suffixes and trailing dots cut). `is_safe_path`'s write rule asks it, and so do
`is_unsafe_project_dir` and `assert_safe_workspace` (no project, no jail and no sandbox mount
there or in a folder that contains it), `copy_out` (every member of the stream, so
`host_path=~` with a member `.bashrc` is refused as a whole) and `move_file` (a folder that
would become a missing parent of such a root, such as a `Library` folder holding
`LaunchAgents/` moved to a missing `~/Library`). Reading those files is unchanged.
The owner's own way to change them is a terminal, or `host_bash`, which asks first and is kept
off chats.

The temp exemption: a path inside the system temp folder is exempt from an explicit root that
contains the temp folder, and from nothing else, and only while the path stays in temp
resolved (a link in temp to elsewhere in `%LOCALAPPDATA%` is judged by its target). Windows'
temp folder lies inside `%LOCALAPPDATA%`, and every staging lane writes there. The dot rule has no exemption, because a
scratch home (the tests, macOS' `/var/folders`) lies in temp and an exemption would switch the
rule off exactly there.

**Named boundaries.**
- `open_for_write` asks `is_host_config` beside `is_vaf_state`, for the path as given and
  for the real path of the folder it opened, so a folder swapped for a link into the home
  directory's configuration before the open is refused. `write_file` and `edit_file` do not
  open through it: they write a temporary file beside the target and rename it into place,
  and a folder swapped for a link between the check and the rename carries the rename with
  it. That is the same window described for VAF's own state in "File tools and VAF's own
  state"; it needs a process running at the same moment that can change the folder (a
  sandbox environment's code in its mounted project).
- The code-running tools (`host_bash`, `python_exec`) write anything the OS user can, and are
  confirmed.
- The listing tools (`tree`, `find_files`) check the folder they start from and may show entry
  NAMES below it, as for `~/.vaf`.
- A `TMPDIR` inside a home dot folder makes temp writes through the file tools refused.
- An entry named only by dots (`~/...`) is folded away by the shared normaliser and is not a
  dot entry; no program reads configuration from such a name.

## Shell execution surfaces

Beyond the Python sandbox there are several shell-execution surfaces (the coder's `bash`,
`run_tests`, `host_bash`, and the remote lanes `ssh` and `ftp`; `sandbox_exec` is described with
the sandbox environments above), each with a distinct confinement model. The guiding rule: **the coder's own shell is jailed; the host is reached
only through `host_bash`, which an account has or does not have.**

One more tool starts processes on the host: `mcp_call`, for a local MCP server. It starts only
a server an administrator registered in `mcp_servers.json` (`mcp_registry.registered_server`),
the same processes discovery starts at every startup, and none while `mcp_native_tools_enabled`
is off; a command the model writes is refused for every account, the admin's included. In a
coding run it reaches only `read`-level tools. It used to start any `server_command` it was given,
which made it a host shell that nobody granted. Details in
[MCP_INTEGRATION.md](../agents/MCP_INTEGRATION.md).

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
  DB on `5432`, the VAF API) are unreachable from the jail. The workspace is the folder that
  was judged: it is held as a handle, bound by name on the jail's own root
  (`vaf.core.folder_mount`), and checked inside the jail, before the command, to show the
  handle's `dev:ino`; a folder swapped for a link in that moment is refused (rc -2), and a
  start the check refused is repeated at most twice, never once the command ran. Measured
  under a process looping rename-and-link: the plain `--bind <path>` showed the other folder
  in 3 of 1000 starts, with the check 0 of 289. Whether the jail can start at all is one real
  probe start per process (`namespace_available`), so a host whose bwrap cannot make a
  namespace takes the fallback below instead of failing every command.
- **Fallback (no working bwrap):** a fresh container with **only** the workspace mounted
  (`-v ws:/workspace`) and `--network none`. Same confinement, minus host access. On a docker
  daemon that shares this kernel the same check runs in the container before the command;
  named boundary: a daemon in a VM (Docker Desktop) shows other folder numbers, so there the
  mount is by name.
- **No sandbox at all:** the tool **refuses**. A raw, unconfined host shell is never run, and
  `bash` also refuses if no project workspace is bound (it would otherwise root the jail at `$HOME`).
- **No network, also not for a dependency repository** (named boundary, in the module docstring
  of `vaf/tools/workspace_exec.py`). A Maven, Gradle or npm build that downloads dependencies
  runs through `host_bash`, which the coder uses without asking where the account has it, or
  in a project sandbox environment, whose network profile decides what it reaches (the way for
  a regular account, which has no `host_bash` unless the admin names it). An allowlisted
  network for the jail itself would need a user-space network stack behind a filtering proxy;
  the environment's `registries` profile is that filter, one layer out. So that the coder does not find this out by failing:
  `bash` describes itself as offline and sends downloads to `host_bash`, and a failed command
  whose output shows a missing network (an unresolved host, a failed artifact transfer) says so
  in its result.

**Whose folder the workspace is.** In a coder run the workspace is the run's project folder.
For an account that folder is one the account may write: the coder chooses it through
`project_dir_refusal` (`vaf/core/workspace_guard.py`, the file jail's own write rule on top of
the unsafe check), and refuses a `project_path` outside the account's own `VAF_Projects/<uid8>/`
and its rooms' shared folders. Since the jail mounts nothing else of the home directory and no
docker socket (a `docker` command is refused, see "Docker is refused in the coder shell"
below), that also bounds what the shell can read and write on the host. The shell asks that
question again itself: `run_in_workspace` takes the caller's `user_scope_id` and `user_role`
as required keywords (a caller that forgets them gets a `TypeError`, not a shell) and asks
`project_dir_refusal` for the folder before anything runs; `bash` is built with that identity
by the coder (its caller), and `WorkflowEngine` binds a copy of the `bash` in its tool set
to the run's identity, so every workflow lane (the chat lane, the saved-template executor, the
`@workflow` subprocess, the workflow builder, automations, resumed runs) asks for the person
who started it; a run with no identity is the machine owner's. A content-only
coder run works in a throwaway folder from `tempfile.mkdtemp`, outside every account's tree;
it is marked scratch and allowed only inside the system temp folder. What `bash` may run is not screened
by the file jail (see "The coder acts as its caller" in
[USER_ISOLATION.md](USER_ISOLATION.md)).

**In a workflow step** the same jailed shell runs in the run's project folder, which the
engine assigns as the workspace (`_inject_workflow_paths` in `vaf/workflows/engine.py`); a
step's own `base_dir` is dropped. The folder is the run's own: a `workflow_project_path` that
arrives through the workflow's variables (a model writes those for a temporary workflow) is
accepted only when `vaf.contained_path` puts it below the run's
`Documents/VAF_Projects[/<uid8>]` folder, as the resume lane hands it back; anything else gets
a fresh folder, so a plan cannot root the jail in `~/.vaf`, `~/.ssh` or another account's
projects. A step cannot name `workflow_project_path` as its output (refused before the run,
`engine.ENGINE_OUTPUT_NAMES`), the engine reports the folder it decided as
`WorkflowResult.project_path`, and the temporary-file cleanup after a temporary workflow
walks only that folder and only below the caller's own `Documents/VAF_Projects/<uid8>`
(NAMED BOUNDARY: an unscoped local run, the single-user machine owner, is bounded by
`Documents/VAF_Projects` alone). The jailed shell can create links in that folder, so the
cleanup never follows one: it walks with `walk_without_links` (no link, nothing below a
linked folder), removes each entry itself and never its resolved target, and checks again
right before each removal that the entry is not a link and that its resolved folder is still
inside the project folder. It removes only files the run created (a snapshot of the folder
taken when the run starts), and skips the run entirely when that snapshot could not read
every folder.

**Docker is refused in the coder shell.** The host docker socket is host-root-equivalent
(a container can `--privileged` / `-v /:/host` / `--pid=host` its way to the whole host
filesystem, outside this jail's mount namespace) and cannot be safely policed by inspecting the
command string. So the coder's `bash` refuses any `docker` invocation up front and points the
user at the main agent instead. Confinement is verified by real escape attempts in
`tests/test_workspace_exec.py` (VAF-core write blocked, source invisible, host DB unreachable,
env secrets not leaked, docker always refused).

**The project's `.git` is written from inside the jail, and git outside it reads it.** The
workspace mount includes `.git`, so the jailed command can write its config and hooks, and
VAF then runs git there on the host (the final commit, history and rollback, the code audit,
the SubAgent window's diffs, `/undo`). Those calls go through
`vaf.core.git_runner.run_project_git`, which pins with `-c` everything through which that
config names a program: an empty VAF-owned `core.hooksPath`, every hook event off and every
hook the config defines (git 2.54) off by name (those ran on every call before, status
included, and reached the host's loopback, measured), no fsmonitor, pager, editor,
askpass, credential helper or signing, no external diff or textconv, and every filter and merge
driver the repository's config defines switched off by name. It also pins the repository
and the work tree to the project itself (`--git-dir=<top>/.git --work-tree=<top>`), because
`core.worktree` in that config moved the work tree to any host folder (a rollback wrote there,
`add -A` pulled its files into the project, measured), and it refuses a `.git` that is a
file or a link, or leads into another repository through `commondir`, alternates or a link
inside it (a reflog line was appended through a linked `logs/HEAD`, measured). It holds the
folder for the whole call, too: the caller's folder is opened as a handle and compared with
the real path the caller judged (`expect_folder`), the top and its `.git` are opened through
`..` handles without following a link, and on Linux every git process of the call runs in
`/proc/self/fd/<top>` with `--git-dir=/proc/self/fd/<git>` and `GIT_COMMON_DIR` on that handle
(`pass_fds`). A process in a sandbox environment that renamed the project folder aside and
linked another account's repository in its place made `git log` print that history in 21 of
300 calls before; with the pin, 0 of 930. Where the private mount lane runs (Linux with a
bubblewrap that can start a namespace, not for a linked worktree), the git command starts in
a namespace of its own instead: its root is its own tmpfs, the top folder is bound at its
real path and checked inside, before git starts, to be the folder the handle holds (the same
check as the jailed shell's), and git works there with `--work-tree=<top>` and the git
handle. A repository
the model makes inside the project with `git init` would have git start a child git there,
reading that repository's own config; status and diff ignore submodules, `add` excludes every
gitlink and `stash create` is refused while one exists. The full list is in
[CODER_ARCHITECTURE.md](../agents/CODER_ARCHITECTURE.md) ("Git in the project folder"). A
driver the person's own global or system config defines (git-lfs) keeps running; `vaf git`
and `vaf update` stay on plain git, because a person runs those in their own repositories.

Named boundary: the driver names are read in one git call and switched off in the next. A
process writing `.git/config` in between can add a filter or merge driver that then runs;
measured with a writer flipping the config in a loop, 189 of 400 reads saw the driver and a
marker filter ran 138 times. The coder's `bash` cannot be that writer (its jail has its own
process namespace and ends with the command, `--unshare-pid --die-with-parent`; the docker
fallback is `--rm`), but a process in a running sandbox environment with the project mounted
can. Hooks, the fsmonitor, textconv, external diff and signing are pinned without a name and
do not race.

git's own text walk is closed where that lane runs. git turns the work tree into a path and
changes into it for every command that reads or writes the files; under a racer looping
rename-and-link, the handle pin alone still let `git status` leak in 3 of 570 calls (2 of
957 in a second run) and `git diff` in 10 of 868, and `reset --hard` (`project_rollback`)
wrote the project's files into the swapped-in folder in 2 of 247 answered calls. Inside the
namespace the path names only the bound folder: 0 leaks of 963 answered status calls and 0
writes in 300 `reset --hard` calls under the same racer. Two namespace starts per call: the
command and the read of the repository's own drivers, which must see what the command sees (an
include through `/proc/self/task/2/`, git's own task inside, let a filter run when that read
was made outside, measured); the global and system config and the index are read through the
handles, which pin them fully. About 17 against 5 ms for a status, and 215 against 65 ms for a
coding run's state update of the SubAgent window. The namespace has no network
(`--unshare-net`): no git command VAF runs fetches, pushes or clones, and a driver of the
person's own global config, the one kind that still runs there, cannot reach the host's
loopback services. Named boundary: a git-lfs object missing locally cannot be downloaded
there; fetch it with plain git first. git there can write only inside the project folder: a
command that writes elsewhere fails with "Read-only file system". The person's global ignore
and attributes files are bound, so the agent's commits skip what they hide, as before.
With `expect_folder` the checked folder must hold its own `.git`, decided once: git never
runs on a repository above it, not even when `.git` disappears during the call.

Named boundary, what remains: where the lane does not run (macOS, Windows, Linux without a
working bubblewrap, a linked worktree, a project inside a folder the namespace binds
read-only such as /opt or a git install prefix) git's text walk stays as measured above, and
`git init` writes the new repository by path; macOS and Windows run git by path after a
comparison before each process. An entry written inside `.git` right after the runner
scanned it (alternates, a `commondir`) is read for that call. Inside the namespace git reads
only the global config files it is given and the ignore and attributes files they name, so
any other file an include of them points to is not there. Details and numbers: the `vaf/core/git_runner.py` and `vaf/core/folder_mount.py`
docstrings.

**In a sandbox environment** (the coder bound with `coding_agent(environment=...)`, or one
exists for its project folder) none of the above applies as written: `bash` runs in that
container (`exec_in`, `/workspace` is the project), with the environment's network and its
installed packages, can start a dev server with `background=true`, and has the root lane
(`as_root`, see "Sandbox environments"). The container is the confinement there. Because it can
have network, a login to or an upload onto another machine (`remote_login`, `ftp_transfer` of
`vaf/core/command_policy.py`) is refused in this lane as well, so the coder's deploy pin
(`deploy_to`) cannot be stepped around from inside an environment.

### `run_tests` (`vaf/tools/sandbox_test_runner.py`)

Gives the coder a sanctioned way to actually run its project's tests and get the **real**
pass/fail, instead of guessing. It copies the project into a fresh `/tmp/vaf_tests_<id>` in the
CALLER's scratch environment (see "The scratch environment" above), runs `python3 -m pytest -q`
under an in-container `timeout -s KILL`, returns the summary, and removes the copy in a
`finally`. It is `read`-level (no host side effects). Bound to a sandbox environment it runs in
the project itself instead (`/workspace`, with the environment's packages), and that instance is
`write`-level, because what the tests write lands in the project. Every refusal starts with
"Cannot run tests:", which the coder counts as a failed run.

### `host_bash` - host shell (`vaf/tools/host_bash.py`)

Some tasks genuinely need the real host - "check my running docker container", inspect host
services, run a host CLI, a local build. `host_bash` runs **unsandboxed on the host and
outside the per-user file jail, on purpose**. Who may use it is an **account permission the
admin grants by name** (`account_opt_in`): it is this computer, as the user VAF runs as, so
the standard and read-only presets leave it out, an account without a list does not have it,
and a regular account works in its sandbox environments instead. Admins and the machine owner
always have it. `python_exec` (unsandboxed Python on the host) is granted the same way.
Accounts whose stored list already names them keep them until the admin takes them away. How
each use is controlled:

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
   The command check is stricter there: the funnel tells the tool whether a person or the
   application confirmed THIS call (`_call_confirmed`, `accepts_call_confirmation`), and an
   unconfirmed call, a direct `HostBashTool().run(...)` included, is read with the
   `host_unattended` profile, which also refuses a network share or Windows device path
   (`share_path` below). A model that writes `_call_confirmed: true` into its arguments is
   overwritten by the funnel.
3. **The main agent's direct call is blocked on remote channels.** There is no safe way to
   show the confirmation on Telegram/WhatsApp/Discord. `channel_restrictions = ("channel",)`
   is the policy-layer block (`evaluate_tool_policy`), and because the tool is ALSO
   `dangerous` it needs a person, so the policy refuses it on a channel *even when the admin
   enables `channel_tools_unrestricted`* (default ON on a fresh install), which otherwise
   lifts the block and the confirmation for the convenience tools (section 1a of
   `evaluate_tool_policy`). The answer comes from the chat lane's own source and session, the
   same single resolution every channel decision uses. The Veyllo App is the exception: its
   far end is the account holder's own paired device (`holder_device` in the channel
   registry), and there each call is put to the holder on a card on that device (section 1h:
   the whole command, every call, nothing standing; vaf/core/app_link/confirmations.py), in a
   turn the holder started or their app agent asked for. The account allowlist and
   `admin_only` refuse before any card, and the command check below still runs after the tap,
   so a Yes on a refused command runs nothing. Only in the lane that would ask a
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
   chat, inside a sub-agent's process, in a call without a chat and in a terminal lane that does
   not run the turns VAF starts by itself, where nobody could be told when it ends. Details:
   [TOOL_SUPERVISION.md](../agents/TOOL_SUPERVISION.md), "Background host commands".
5. **VAF's own terminal commands refuse it.** Every command started for the agent (this
   tool, `python_exec`, a background command) gets its environment from `agent_env` in
   `vaf/core/processes.py`, which marks it, and the terminal door and `vaf users
   reset-password` refuse a marked caller: `vaf session`, `inbox`, `outbox`, `secrets`,
   `ssh`, `ftp` and `env` act as the machine owner, and the agent has its own tools for each
   that know whose turn it is ([ENCRYPTION_AT_REST.md](ENCRYPTION_AT_REST.md), "The
   doors"). It closes the easy path, not the account permission: a command that clears its
   environment, or reads files directly, is what an account with `host_bash` can do.

**What an unattended host command reaches (NAMED BOUNDARY).** Everything the machine
reaches, apart from share and device paths. Measured on Linux through the funnel as the coder
and the workflow engine build it (`ToolCaller(..., gate_enabled=False)`): `curl` to a listener
on `127.0.0.1` ran with no question and was answered (1 of 1), with only the note "fetches
data from the network". Loopback includes VAF's own tokenless loopback trust (the "Localhost
Bypass" in [NETWORK_FEATURES.md](../setup/NETWORK_FEATURES.md)); the LAN, the internet,
link-local addresses with a cloud instance's metadata service, and multicast are reachable
the same way (not contacted in the measurement, and no layer stands in between). Deliberate:
the coding agent's downloads (`pip`, `npm`, `git clone`) run through `host_bash` by design (see
the coder's `bash` above), and the agent downloads without friction. The two measured cuts:
`bwrap --dev-bind / / --unshare-net` (2.6 ms a start against 1.1 ms, the docker CLI keeps
working over its socket) ends every download; `pasta --config-net --no-map-gw` (about 150 ms a
start, a package most systems lack) keeps the downloads and closes loopback only, while the
same shell still reads the same data from disk. The account permission is the answer here; the
API half of loopback is closed by replacing the tokenless loopback trust with a per-start IPC
token, a change of its own that also covers every process the agent starts. A main-agent turn
nobody watches (an automation) runs `host_bash` only under a standing answer (always, a
trusted folder, the admin's bypass, the user-grant resolver); that counts as confirmed, so the
`host` profile applies there.

An offline classifier (`vaf/core/command_policy.py`, shared with `bash` but run with the
strict `host` profile, or `host_unattended` where nobody confirmed the call) refuses the
catastrophic set even after confirmation: code fetched
from the network and piped into a shell, writes to a block device, a fork bomb, a recursive
delete of a system or home root, and a command whose program or shell text the approved
text does not show. It tokenizes quote-aware and reads commands rather than matching
substrings, so `rm -rf /tmp/scratch` is ordinary work while `rm  -rf  /` is not. The
verdict carries its categories, which the confirmation dialog shows. In detail:

- **What it reads.** The command a command carries: `bash -c '...'`, `su -c '...'`,
  `eval ...`, the command a project or package runner starts (see the `python_exec`
  paragraph above), and every command substitution, subshell and process substitution
  (`$(...)`, a backtick pair, `(...)`, `<(...)`, `>(...)`), each with the lane's own
  profile; `ssh host '...'` with the `remote` profile (the catastrophic core, while an
  installer piped into a shell is named rather than refused, because that is how a server
  is commonly set up). A wrapper's option values are stepped over (`sudo -u root rm -rf /`
  is judged as `rm -rf /`). Before this, ten of ten measured wrapped forms passed. Also the
  commands a carrier runs: `env -S`/`--split-string` (its string read as shell text and as
  env splits it itself, quotes grouping and `\_` a blank, and the program after env's
  assignments, also one whose name is no identifier: `env }x=1 cmd`, `env -S 'x=|1 cmd'`),
  `watch` (its words, as `sh -c` gets them), `find -exec`/`-execdir`/`-ok`/`-okdir` up to
  `;` or `+`, where `{}` stands for the start paths when no test comes before it
  (`find / -exec rm -rf {} +` is `rm -rf /`)
  and stays `{}` after one (`find ~ -name '*.tmp' -exec rm -rf {} +` passes), and
  `find START -delete` with no test before it as `rm -rf START`. A here-string or heredoc
  reaches the command a carrier starts (`find . -exec bash \; <<< '...'`, `eval bash <<EOF`,
  whose redirections are its own and no words of the text it runs), and so does what a
  subshell's or an `exec`'s redirections give its input. A descriptor counts as opened only by
  a redirection BEFORE the one that copies it (`bash <&3- 3<f` copies what an earlier command
  opened, then opens 3), and a copy keeps what that descriptor holds (`cat 3< <(base64 -d f)
  <&3 | bash` hands bash decoded text). A recursive delete whose target puts a variable after a protected
  root is judged by that root (`rm -rf /$X`, `"$HOME/$X"`); one that starts with the variable
  is not (`rm -rf "$BUILD_DIR"` passes).
- **A program it cannot see is refused (`opaque_command`), with the way out in the
  refusal.** A substitution in the program word, wherever that word stands: after a
  wrapper, a `VAR=` prefix or a runner (`env $(echo rm) -rf /`), inside quotes or in part
  of the name (`"$(echo rm)"`, `/usr/bin/$(echo rm)`); the way out is to run the
  substitution on its own and write the program out. A leading `-` changes nothing: zsh
  reads it as a precommand modifier, so `zsh -c '- $(echo rm) -rf /'` runs rm. A runner
  option's value from a substitution (`uv run --with $(cat r)`, also `--with=$(cat r)`,
  whose unquoted value is split into words the runner reads as more arguments): write a
  long option's value with `=` and in double quotes (`--with="$(cat r)"`), or run the
  substitution first and write the value in. After a runner flag known to take no value
  (`uv run --frozen`, `uv run -m`, `npx -y`, `npx --no-install`, `bunx --bun`,
  `poetry run --quiet`, `conda run --no-capture-output`, `pipx run --quiet`; the table
  `_VALUELESS_RUNNER_FLAGS`) the next word is the program, so
  `uv run --frozen pytest $(git ls-files tests)` passes. The npx and npm rows are read from
  npm's own parser (npx-cli's argument loop, then nopt with npm's types: the next word stayed
  the package for every listed flag); the others rest on each tool's documentation, since
  none of those tools was installed where the table was measured.
  After a flag the table does not know,
  the next word may be its value or the program, and a substitution after it is refused
  with the way out: put its result into a variable first
  (`files=$(git ls-files tests); uv run --frozen pytest $files`), or, if the flag takes a
  value, write it as `--flag=value`. Variables from a substitution (`env $(cat .env | xargs) make`):
  `set -a; . ./.env; set +a; make` (`.`, not `source`: host_bash and the sandbox run
  `/bin/sh`, which is dash on Debian and Ubuntu and has no `source`). Shell text another
  command prints: `eval "$(...)"`, `bash -c "$(curl ...)"` (refused on the remote lane too),
  and a shell or interpreter whose script comes from another command's output, as a
  process substitution or a here-string holding a substitution, or a descriptor fed by one
  (`source <(...)`, `bash <<< "$(...)"`, `bash /dev/stdin < <(...)`,
  `. /dev/fd/3 3< <(...)`), also with the here-string written against its words or behind
  a descriptor redirection (`bash <<<"$(...)"`, `bash 2>&1 <<< ...`), after `--`, or after a
  value option given no value; for a tool's shell setup the way out is the tool's own
  direct form (`conda run -n ENV ...`, `pyenv exec ...`, `direnv exec . ...`,
  `opam exec -- ...`, `ssh-agent bash -c '...'`), otherwise the output saved to a file,
  read, then run. A shell reading a descriptor above 2 that its own command does not open
  (`exec 3< <(...); bash /dev/fd/3`, `bash <&3`, `bash < /dev/fd/3`, `bash /dev/fd/0 <&3`,
  every spelling of descriptor 0 redirected from it, `<&3-`, `<> /dev/fd/3` and `0<` too,
  also spelt `//dev/fd/3`, `/dev/fd//3` or `/proc/<pid>/fd/3`) is refused the same way.
  A literal here-string fed to a shell
  (`bash <<< 'rm -rf /'`) is read as shell text. A literal program behind a substitution
  passes (`"$(git rev-parse --show-toplevel)/scripts/build.sh"`), and so does a
  substitution in an argument, an option's value, a `VAR=` prefix or an array assignment
  (`sudo -u $(whoami) ls`, `X=$(date) make`, `arr=($(ls))`).
  A program named by an expansion (shape `expansion`): an unquoted `$` in the program word
  (`$X -rf /`, `${X:-rm}`, `$'\x72\x6d'`, `$"rm"`), a `$` inside double quotes in its last
  path component (`"$X"`, `"$DIR/$TOOL"`), a glob (`/bin/r?`, `/bin/[r]m`) or a brace
  expansion (`{rm,-rf,/}`), wherever the program stands (after a wrapper, inside `bash -c`,
  in a loop or a function). The way out: write the program name out (`python3 -m pytest`,
  not `$PYTHON -m pytest`); a variable may stand in an argument, and inside double quotes it
  may name the folder (`"$VIRTUAL_ENV/bin/python" -m pytest`, `~/.local/bin/uv sync`). Decoded
  text piped into a shell (shape `decoded`: `base64`/`base32`/`basenc -d`, `xxd -r`,
  `openssl ... -d`, `uudecode`, `rev`, also through a filter after them): decode it into a
  file, read the file, then run it. PowerShell's encoded command (shape `encoded`:
  `-EncodedCommand` and its short forms `-enc`, `-e`, `-ec`, also after `/`): write the
  command out with `-Command`. A shell's `-c` under xargs whose script holds xargs' replace
  string, or that has no script, so xargs supplies it (`xargs -I{} sh -c {}`): hand the item
  over as an argument (`xargs -I{} sh -c 'black "$1"' _ {}`) or run the program directly
  (shape `xargs_text`, whose refusal says so).
  These refusals hold on all four lanes and after a yes in the chat, like the substitution
  as the program. The everyday commands they refuse, each with its way out:
  `$EDITOR notes.md`, `$PAGER log.txt`, `$PYTHON -m pytest`, `"$PYTHON" -m pytest`,
  `${CC:-gcc} -o app app.c`, `$SHELL -c '...'` (write the program out);
  `$HOME/.local/bin/uv sync` (`"$HOME/.local/bin/uv" sync` or `~/.local/bin/uv sync`);
  `for t in black ruff; do $t --version; done` (write each command); `exec "$@"`,
  `nohup "$CMD" &` (write the program out); `eval "$cmd"`, `bash -c "$cmd"`, `curl ... |
  while read l; do bash -c "$l"; done`, `echo "$SCRIPT" | bash`, `printf '%s' "$X" | sh`
  (write the command itself out, or save the text to a file, read it, run it);
  `./build/*/tool`, `/opt/*/bin/x` (write the path); a recursive delete whose target puts a
  variable after a protected root (`rm -rf ~/.cache/$X`, `/var/tmp/$USER`, `"$HOME/$X"`;
  write the path out, since an empty variable deletes the root);
  `... | base64 -d | sh`, `xxd -r -p | sh`, `openssl base64 -d | bash`, `... | rev | sh`
  (decode into a file, read, run); `powershell -EncodedCommand ...` (`-Command`); an unquoted
  heredoc whose body holds a backtick span with a variable, such as a JavaScript template
  literal (quote the delimiter, `<<'EOF'`, which the refusal offers first when the heredoc
  goes into a file); and an inline script fed to a shell through a heredoc that names its
  programs by variables (`$INSTALL_CMD`; write it to a file and run the file, which is not
  read). On Windows, where host commands run in cmd.exe, `for %i in (*.txt) do ...` is read
  as bash too and refused (bash takes `(*.txt)` for a subshell running a glob): use
  PowerShell (`powershell -Command "Get-ChildItem *.txt | ForEach-Object { ... }"`).
- **Share and device paths (`share_path`).** A command that names a network share or a
  Windows device path: the backslash UNC shape with a host and a share (`dir \\server\share`,
  `\\%COMPUTERNAME%\share`, `\\h@80\s`, `\\h\(share)`), a device prefix (`\\?\UNC\...`,
  `\\.\`, `\??\`) and a `file:` URL naming another host or a UNC path (`file://h/s`,
  `file:////h/s`, `file://localhost//h/s`, also percent-encoded; `file://C:/x` is a local
  drive), on every host; on a Windows host also the forward-slash form `//host/share`, a host
  alone (`net view \\nas`, `sc \\nas query`, `explorer \\nas`: the login happens at the host,
  before a share is named) and a share after two separators (`\\nas\\share`). On Windows
  opening a share on another computer sends the account's NTLM login attempt to it. Refused
  in `host_unattended`, a note in `host` (the dialog shows "share path" and a yes runs it),
  never in `jailed` or `remote`. The refusal names the piece it read as a share and tells the
  agent to hand the path to the person, who can run the command in the chat or copy the files
  to a local folder for the agent to work on. It is read from the RAW text, once for the whole
  command (every nested `bash -c`, substitution and runner included), never from the tokens:
  a POSIX shell lexer turns `\\h\s` into `\hs`. The text is cut at whitespace, quotes, the
  shell's separators and redirections, `=` and commas, so `X=\\h\s make` and `--path=\\h\s`
  count; each piece is read whole and then cut at parentheses, which a share name may hold
  (`open(\\h\s)`, `\\h\(share)`); and the text is read in four spellings: as written;
  without the quoting cmd.exe and PowerShell take out of a word before the program sees it
  (`dir "\\h"\s`, `dir \\h""\s`, cmd's caret `dir \\h^\s`, PowerShell's backtick); with each
  doubled backslash read as one, for a one-liner's C-style string
  (`python -c "open('\\\\h\\s')"`, `node -e`, `perl -e`); and both; a text that holds one
  of the eleven characters NFKC folds into a backslash, a slash or a colon is read once more
  with those characters as NFKC reads them, so the fullwidth and small backslash and slash
  (`＼＼h＼s`, `﹨﹨h﹨s`, `／／h／s`) are plain ones. Through a fixed table, never the
  whole text's NFKC: NFKC makes one character up to eighteen (U+FDFA; 1 M characters of it in
  `python_exec`'s code took 2.84 s and 349 MB when every non-ASCII text was normalised, now
  0.08 s), and older CPython patch releases sort a run of combining marks in quadratic time
  (not measurable here: 3.13.16 normalises two runs of 30 000 in under 0.01 s). Fullwidth
  letters around them stay as written (`ｆｉｌｅ：／／ｈ／ｓ` is then read as `//ｈ/ｓ`
  behind a colon, a share on Windows). A piece's text after a `:`, up to the next one,
  counts too when it starts with a backslash (`-Path:\\h\s`, `/out:\\h\s\x.exe`), is a
  `file:` URL (`-Uri:file://h/s`; `-Uri:file://C:/x` keeps its drive and is local), or starts with `/` behind no URL scheme and no user
  name (`-Path://h/s`, `FileSystem:://h/s`; `https://h/s` is a URL, `user@host://srv/x` is
  rsync's or git's remote path); not after a drive letter at the start of a word
  (`open('C:\\Users\\x')` is a local path, `-P:\\h\s` an option naming a share). A
  refusal names the share as the code's own string holds it, also one only Python's escapes
  spell (`"\x5c\x5ch\x5cs"` names `\\h\s`). The reading is linear in the command's
  length, and it runs before anyone is asked (host_bash, the dialog's preview, a sandbox
  environment's `bash`): a colon's suffix read to the end of its piece made it quadratic
  (130 K characters of `\\h\s:`: 27.6 s and 2.7 GB in the server process, now 0.3 s and
  26 MB). A shape filter in front of `is_remote_path` keeps `//`, `//tmp`, `// TODO`, `\\n`, `\\d+\\.`,
  `\\w\.`, `C:\x`, `https://...`, `file:///x` and `file://localhost/x` out; a host holds no
  regular-expression operator, and the Windows-only host-alone and two-separator readings
  skip a host that looks like an escape (`\\n`, `\\t`, `\\x41`, `\\012`) or a shell expansion
  (`\\$(cmd)`). `python -c` code is
  also read through `python_code_categories`, so a share only Python's escapes spell
  (`"\x5c\x5ch\x5cs"`) counts in a POSIX command; that reading follows POSIX quoting, and on
  Windows the doubled-backslash spelling above is what covers the double-quoted one-liner.
  Named boundary: text only. On POSIX the host-alone and two-separator spellings are not read
  (a POSIX shell has no SMB client of its own, and they would refuse `printf '\\nDone'` for
  nothing). On Windows a host alone shaped like an escape or an expansion (`\\a`, `\\n`,
  `\\xface`, `\\012`, `\\$evil`) is not refused either, even where a host of that name
  exists. Not seen anywhere: a share built at run time (a variable or an environment
  variable's value such as `%CD:~0,0%`, a string joined in PowerShell, also through an empty
  `$()` as in `\\$()h\s`, `[char]92`), one inside a script file, text decoded before it runs
  (`echo ... | base64 -d | sh`, `powershell -EncodedCommand`, which are refused as a whole
  for that reason, see "A program it cannot see"), escapes a program decodes itself outside a
  doubled backslash (Python's `\x5c` in a one-liner cmd hands over unquoted or in its `""`
  form, node's `\x5c` and `\u005c`, perl's `\134`), backslash lookalikes NFKC keeps as they
  are (`∖` U+2216, `⧵` U+29F5, which an ANSI program may still receive as a backslash),
  Windows name resolution of a bare name, and an HTTP client told to log in as the Windows account
  (`curl --ntlm`, `-UseDefaultCredentials`); the Windows-side control for that class is the
  operating system's outgoing-NTLM restriction, which needs admin rights and which VAF does
  not set. Accepted cost: the scan does not tell data from a command, so a heredoc body, an
  `echo`, or the text `ssh host '...'` runs on another machine (`ssh box 'net use
  \\nas\x'`) is refused unattended when it names a share, and so is a WSL path typed as one
  (`\\wsl$\...`, local on Windows), as is, on Windows, a one-liner's text that starts with a
  doubled backslash and a word (`print('\\nDone')`) and text whose NFKC form reads as a path
  behind a colon (`参照：／／共有／資料`).
- **Arithmetic is read both ways.** Whether bash evaluates `$((...))` or runs it as a
  command substitution holding a subshell depends on more than its parentheses (a `#`
  comment or a backtick holding a `)` moves where it closes), so every `$((`, and an
  arithmetic command `(( ... ))`, is read as arithmetic and as a command, and refused when
  either reading is (`echo $((rm -rf /) )`, `echo $((rm -rf /))`). Real arithmetic names no
  program, so it passes (`make -j$(($(nproc)+1))`, `(( $(wc -l < f) > 5 ))`); where the
  parentheses show bash evaluates it, a substitution in front (`$(nproc)+1`) is an operand
  there, not a program. A backtick inside `$(( ))` is refused (write `$(...)` instead:
  `echo $(( $(date +%s) - 10 ))`). The legacy `$[...]` is only ever arithmetic, and its
  substitutions are read. Arithmetic also runs a substitution held in an array subscript
  once a string becomes an array reference (`x='a[$(...)]'; echo $((x))`, `printf -v
  'a[$(...)]' v`, `let`): a substitution left as text in a quoted `NAME[...]` is read as a
  command, not refused (`x='a[$(date)]'; echo $((x))` passes), also bash 5.3's `${ cmd; }`
  and `${| cmd; }`, and also in the string as bash hands it over from double quotes (an
  escaped backtick, `\$(` with a `\\` NL continuation inside: `let "b[\`cmd\`]=1"`). Such a
  `$(` ends where its parentheses balance and where bash's parser ends it (a case pattern's
  `)` or one in a comment closes nothing); both texts are read. A count that starts an
  arithmetic text (`$#`, `${#NAME}`, `${#NAME[@]}`: `echo $(( $# + 1 ))`) is a number there,
  not a program named by an expansion, in the reading of that text as a command; its
  substitutions are read as everywhere (`${#a[$(cmd)]}` runs cmd).
- **`case`.** A `case` statement inside a substitution (the reserved word where a command
  starts, at the start, after `;`, `&&`, `||`, `|`, a newline, or after `if`, `while`,
  `until`, `then`, `do`, `else`, `{`, `!`, `time`, followed by exactly one shell word and
  `in`) refuses the command: a pattern's `)` closes the substitution early. The word in an
  argument, a commit message, or at the start of a line of prose ("case 2 is covered")
  does not. A recursive delete whose target comes from a substitution is judged by the
  literal part in front of it (`/$(...)`, `"$HOME/$(...)"`, and a quoted `"$(...)"` that
  names one unreadable path, are refused).
- **Nothing refused before is lost.** Every verdict is joined with the FROZEN reading
  (`vaf/core/command_reading_frozen.py`, the check as it read commands before
  substitutions were carried, pinned by a hash): a command is refused whenever either
  reading refuses, with both readings' categories. Every earlier attempt to construct the
  new reading so that it kept all old refusals lost some that bash runs, found by
  generators; the union makes it impossible by construction (300 000 generated commands
  on three lanes, measured against the earlier module: no exception). Its price: what the
  frozen reading over-refused stays refused (`echo 'a; $(x)'`, words after a closed
  substitution such as `echo $(true) mkfs x`, a heredoc line that starts with a
  substitution or a code fence), and the refusal says what it saw (a substitution where
  a command may start), to put the substitution's result into a variable first, or to
  join the lines or write the text to a file. A reading that fails refuses the command
  (`unreadable`, logged), never passes it. The frozen module goes once this reading
  refuses everything it refuses on its own: at least 300 000 generated commands on three
  lanes with none refused there and allowed here.
- **Pipes.** A pipe reaches through a carried command at both ends: `curl ... | uv run bash`
  and `uv run curl ... | bash` are a fetch piped into a shell like the same pipe without the
  runner, and so are `curl ... | (bash)`, `curl ... | echo $(bash)`,
  `curl ... | tee $(mktemp) | bash`, `curl ... | tee >(bash)`, `curl ... > >(bash)`,
  `curl ... |& bash`, `bash <<< "$(curl ...)"` and `bash <(curl ...)`. The `&` of a
  redirection (`2>&1`, `&>f`) is no separator. The refusal names the way out: download to
  a file, read it, then run the file. Text that reaches a shell's input is read as its
  script (`bash`, `sh -s`, `. /dev/stdin`): what `echo`, `printf` and `yes` print into the
  pipe (`echo 'rm -rf /' | bash`; dash's echo decodes escapes, so both spellings are read;
  printf's format is used again while arguments are left, as printf does, `%b` decodes its
  argument's escapes, `\c` there ends all of printf's output, and precision (none for `%c`,
  as bash's printf has it: `printf '%.0c%s' r 'm -rf ~'` prints `rm -rf ~`), `%c` and a `*`
  width are followed, a negative `*` precision being none; output past a bound, checked per
  directive before any padding, and a directive whose output is not modelled (a number,
  `%ls`, `%(...)T`, also `%.0(...)T` and `%-9(...)T`) are refused as unreadable text, and a
  `%` that starts no directive ends
  the output there, as printf stops at it), what a subshell, a
  group, a loop or a command run from text prints, every command's output in it and those
  writes joined (`( echo '...'; echo x ) | sh`, `(printf 'r'; printf 'm ...') | bash`,
  `bash -c "echo '...'" | bash`, a script a shell reads from its input,
  `echo 'echo ...' | bash | bash`, whatever that shell's own redirections do, since its
  script can write to any descriptor: `bash <<< 'echo "$HOME"' >/dev/null | bash` is refused
  although it prints nothing into the pipe, a known over-refusal that fails closed), also
  with redirections after its closer that leave any descriptor on the pipe
  (`( ... ) 2>&1 | bash`, `( ... >&2 ) 2>&1- | bash`, also `>/dev/stdout` in any spelling,
  `>/dev/./stdout`, `>//dev/fd/1`, and copies through another descriptor, `3>&1 >&3`,
  `2>/dev/stdout >&2`, followed in order, a descriptor's number read as a number (`3>&01`), a
  word whose operator is quoted or escaped being no redirection (`1">"f`, `1\>f`), every
  target but `/dev/null` possibly the pipe (after `cd /dev`, `>stdout` is; so is
  `/proc/self/root/dev/stdout`, or a link made earlier), and `/dev/null` only as itself,
  `//` and `/./` collapsed but no `..` in it (Linux resolves `..` after a link made
  earlier: `>/tmp/L/../../dev/null` may be the pipe), the standard error kept too with a
  `|&` in the text, and in a text another command runs, or one that opens a descriptor of 3
  or more (`3>&1`, `{fd}>&1`), the output always kept, since a descriptor the closer does not
  name may be the pipe (`{ ( echo ... >&3 ) >/dev/null 2>&-; } 3>&1 | bash`; the named
  boundaries below keep a substitution inside `[[ ]]` and a function apart);
  `( ... ) >/dev/null | bash` hands on nothing), a heredoc
  or a here-string, also passed on through a filter (`cat <<'EOF' | sort | bash`) or a
  command that runs another one (`eval`, `sh -c`, `env -S`, a runner), and into a carried
  command or a subshell. Text from double quotes is read the way bash hands it over:
  shlex keeps the backslash of `\$` there, bash takes it off (`bash <<< "\$(echo rm) -rf /"`
  runs `rm`), so such a text is read both ways, and the text a command runs (`bash -c`,
  `eval`, `su`, `ssh`, `watch`, `find`, `trap`) also with its words as bash's own quote
  removal leaves them, which reaches scripts nested in quotes several levels deep. A variable
  or a substitution in what echo or printf prints makes it shell text another command prints
  (`echo "$X" | bash`): save it to a file, read it, then run it. A variable bash expands in
  a double-quoted here-string or an unquoted heredoc before the shell reads it is refused as
  `expanded_input` (`bash <<< "echo $P"`, `bash <<EOF` with `echo $P` in the body, also
  passed on through `cat <<EOF | bash`): quote it (`<<'EOF'`, `<<< '...'`), so the shell
  reads the variables itself (a variable that is not exported is empty in that shell: export
  it, or write its value in), or escape it (`\$P`, which an unquoted heredoc hands on as
  `$P`; `\\$P` expands); a quoted heredoc passes. A here-string written `$'...'` hands the
  shell its decoded text and expands nothing (`bash <<< $'set -e\nmake'` passes). Assignment
  words before a program are
  stepped over in every form bash takes (`a+=1 cmd`, `a[0]=1 cmd`, `a[${#a[@]}]=x`), and a
  shell reads its options after `-c` too and runs the first word that is none (`bash -c -e '...'`,
  `sh -c - '...'`); a quoted script that starts like a redirection is read as well
  (`bash -c -e '>x;cmd'`). An unquoted expansion as a shell's script name may vanish, so the
  shell's input is read too (`bash $! <<< '...'`). A quoted one is one word even when empty
  (`bash "$S"`, `bash "$SCRIPT" <<< "$INPUT"`, `printf '%s\n' "$ans" | bash "$S"` pass), unless
  the text can make it `--`, `-`, `-s` or nothing, and then the input is read as well:
  anywhere in the word (empty quotes before it, `''"$x"`, add nothing) a command
  substitution or arithmetic (`"$(echo -)"`, a backtick pair), a `${...}` that is more than a
  name or a number (`"${x:---}"`, `"${x--s}"`, `"${x:=--}"`, `"${a[*]}"`, `"${!x}"`, every
  `"${!PREFIX@}"`), `$@` (no word at all without items: `"$@"`, `"$x$@"`, `"${x:-}$@"`) or
  `$_` (the last word of the command before, also as `"${_}"`); a positional parameter (`"$1"`, `"$*"`) when
  the text runs `set`, `shift`, `source` or `.`, or defines a function; a name the text
  spells outside an expansion (`a=-s`, `read a`, `for a in`, `declare -n r=a`), one a
  `${a:=...}` assigns, one bash assigns itself (`REPLY`, `OPTARG`, `MAPFILE`, `BASH_...`),
  and every name when the text may assign one it does not spell (`eval`, `source`, a name
  given by an expansion to `declare`, `read` or `printf -v`); and every quoted expansion in a
  text another command runs (`bash -c`, `eval`, a heredoc), whose names and arguments come
  from outside it. An option word of a shell with an expansion in it may be `-c`
  (`x=c; bash -$x '...'`): the word after it is read as the code as well. A
  newline right after a `|`
  continues the pipeline, as bash reads it.
- **Bounds.** Every reading, the command itself included, spends one of
  `MAX_NESTED_COMMANDS` (64), so one command carries at most 63 nested readings, each
  reading counting as one (`$(( $(x) ))` is two), and a command nested more than
  `MAX_NESTING` deep is refused; past either it is refused as carrying more nested commands
  than anyone can review. The readings stop once the bound is spent, for the whole command
  and not per segment (12 KB of `uv run -a uv -a uv ...`: 39 s before, 0.08 s after). A
  runner's command is read at every option's value position, each candidate the rest of the
  command joined back into text; those candidates, and both readings of a `$((`, may add up
  to the command's own length plus 64 KB, and past that the command is refused the same
  way. Every reading of the current and the grammar reading (both written and nested) also
  spends its text from the characters they share in one call, four times the command plus
  64 KB, the top-level grammar reading included (the frozen reading has its own budget, see
  below): a nested text
  (`eval`, `bash -c`, a heredoc fed to a shell) is read as written and as its grammar text,
  each of which reads its own nested texts again, so a text nested four deep was read up to
  3 to the 4th times (117 s at the length cap); past the budget the command is refused as
  nested too deep (measured: the corpus and the everyday commands read at most 3.4 times
  their length above 200 characters). Empty parentheses spend no reading. The fork-bomb search is three plain searches.
  The frozen reading gets fewer readings for a long command (as many as fit into 768 K
  characters of text read: all 64 up to 12 K characters, 6 at the cap); a spent budget
  refuses. A command over 128 K characters is refused as unreadable before either reading
  (`too_long`: write the text to a file and run the file): both readings together, with
  shlex's own tokenizer quadratic on one long word, took up to 44 s at 1 MB, and nobody
  approving it can read it either. The frozen copy differs from the earlier module in two
  documented substitutions with the same answers, both for time: the fork-bomb search, and
  its substitution search run over the text with every whitespace run collapsed (a run of
  newlines took 125 s at the cap, now 0.04 s; 300 000 random strings, no different
  answer). Measured, single process, both readings, 24 shapes repeated to the
  128 K-character cap: each took about 1.5 s at most (a heredoc of one-line bodies
  1.03 s idle and 1.44 s with the machine under load, `uv run -a` 0.76 s, `x=` 0.52 s, a
  heredoc file 0.10 s; `npx --x y ...` took 10 s at 100 KB before). The grammar reading
  writes its text in one pass and reads it only when it differs from the command, with a
  call budget of its own at the top level and the characters shared with every other reading
  (see above); all three readings together, 33 shapes at
  131 000 characters and 38 more that once grew faster than the text (heredocs inside
  `$( )`, a group of distinct `echo`s, a long backslash run, wide printf fields and a run of
  zeros in one, cmd.exe `for` options whose value is the word `for`, unclosed `a[` where a
  command starts in six spellings, ten thousand `a[0]=1`, element subscripts, a backtick
  text each, quoted spans inside one `${...}`, distinct substitutions in a heredoc, empty
  lines before a heredoc, a long word beside a substitution text, many `$'...'` parts in one
  word; line continuations in a subscript, a funsub in arithmetic, a heredoc delimiter's
  substitution broken by one), measured on a machine under load (load average 2 to 5): about
  4 s at most (`a[;` repeated after a single-quoted span holding a `}` in `"${...}"`, which is
  read both ways, 3.9 to 4.3 s), the next ones about 2 s (a heredoc `cat <<.` with an empty
  body repeated, 1.85 to 2.07 s; 0.59 s before, measured alongside; a long `echo "\${ x; }..."`
  with redirections into a shell 2.3 s, each spelling of its words read once); the same shapes nested
  in `eval` or `bash -c` up to five deep, with a comment, such a span or a subscript as the
  trigger of a second reading at each level, about 4 s at most (one level, 3.1 to 3.9 s;
  deeper ones are refused once the characters all readings share are spent, 2.1 to 3.4 s;
  117 s for four levels before that budget). Every shape is linear: measured, 4 times the
  length takes 3.9 to 4.3 times the time; the tests (`test_grammar_reading_is_linear`, `test_long_and_distinct_shapes_are_linear`,
  `test_nesting_costs_a_bounded_multiple_of_the_text`) allow 8 times the length at most 20
  times the time plus 0.05 s, and four levels of nesting at most 6 times the flat text plus
  0.5 s. The one exception is shlex's own tokenizer on one long word, as before: one word of
  131 000 characters takes 0.53 s, 0.50 s before. The heredoc hint's search took 120 s before
  on 131 000 empty lines and takes 0.77 s now.
- **Compound commands, comments and heredocs (the grammar reading).** The reading above cuts a
  command at its separators and judges simple commands, so the shell's grammar around them
  hid the command word: `{ rm -rf /; }`, `if true; then rm -rf /; fi`, `for`, `while`,
  `case x in x) rm -rf /;; esac`, `f() { rm -rf /; }; f`, `>f mkfs x`, and every line after
  `echo hi # it's`. So every command is read a third time, written out as bash reads it, and
  refused when that reading refuses, with its categories (a union again: it only adds
  refusals). In that text a comment is gone (a `#` where a word starts, to the end of its
  line; a backslash at its end joins nothing, measured with bash); every heredoc ends where
  bash ends it, whatever its delimiter (`<<.`, `<<1`, `<<$X`, `<<$'EOF'`, `<<$$'x'` (the process id and a
  quoted `x`), `<<"E"OF`, an `<<a$(b)c`; an unquoted body's lines joined at a line continuation first; one written
  inside `$( )` also at the first line that starts with its delimiter, as bash 5.3 does,
  the rest of that line read as commands (`EOF)`); without its delimiter line at the end of
  the text), with its body as data and an unquoted body's substitutions as commands, and a
  renamed delimiter that is no line of the text; inside `$( )` or a subshell a quote a body
  leaves open is closed at its end and its parentheses are balanced by comment lines (a
  case pattern's `)`), since the reading above counts quotes and parentheses across it
  (`echo "$(cat <<'EOF'` NL `it's` NL `EOF` NL `)"; rm -rf ~`); a line continuation is joined, also
  inside a word or an operator; `$'...'` is decoded into one quoted word and `r\m` is `rm`;
  reserved words count only where a command starts: `!`, `time`, `coproc`, `function NAME` and
  `NAME ()` are dropped, a compound command becomes a list of its commands (`for NAME in
  WORDS`, `case WORD in` and each case pattern become `: WORDS`, since bash expands them), and
  a compound in a pipeline or with a redirection after it becomes a subshell, so it reads the
  pipe like one (`curl x | { bash; }`, `{ bash; } <&3`); `[[ ... ]]` stays as written, bash
  5.3's `${ cmd; }` is a command substitution that closes at a `}` where a command starts,
  whatever follows it (`}x`), `${...}` ends at its first `}`, its nested quotes inside double
  quotes are left out (`"${x#"'"}"`) and outside them a `<(...)` in its word runs
  (`${x:=<(cmd)}`), and inside `((...))` a `<<` is a shift and `#` no comment once its `))`
  is found (without one bash reads two subshells, and a `$((` without one ends where its
  parentheses balance, the text between read as a command substitution of its own, where a
  heredoc ends with that text); a heredoc in a subshell keeps a renamed delimiter whose body
  comes after the subshell (`( cat <<. )` NL ...); arithmetic reads its text as if in double
  quotes, so a `'` quotes nothing in `$((...))`, `((...))`, `for ((...))`, `$[...]` and a
  substring's offset and length (`${x:'$(cmd)'}` runs cmd; a `${NAME:` whose name, digits
  (`${000...1:`) or subscript run past 1 024 characters counts as a substring, and so does
  one whose first 1 024 characters end on the `\` of a line continuation), and a `$'...'`
  there, also inside double quotes, in an unquoted heredoc's body and in an indexed array's
  subscript (`"${x:$'...'}"`, `"${a[$'...']}"`), which bash decodes and expands again
  (`(( $'\x24\x28cmd\x29' ))` runs cmd), is refused when the decoded text holds a `$`, a
  backtick, a quote or a backslash (`ansi_arithmetic`: write the arithmetic out; a decoded
  `'` would quote what follows the arithmetic, `(( $'\x27' )); r\` NL `m ...` runs rm), and
  read as the plain text it is otherwise (`(( $'1' + 2 ))` passes);
  a `))` inside such a span ends nothing, and an unclosed `$[` there ends
  at the arithmetic's own `)`; an operator, a redirection or a `(` inside an array
  assignment's parentheses (`x=(a;` NL `cmd`) is a syntax error after which bash drops that
  line and runs the next one, so such a command is refused (quote the element:
  `arr=('a;b' c)`), while a subscript (`m=([a;b]=1)`), elements over several lines
  (`m=(` NL `[a]=1` NL `)`) and an extglob group `@(...)` or `+(...)` in a text that turns
  extglob on pass; where a command starts, `NAME[...]=` reads its subscript up to the `]`,
  blanks, operators, newlines and carriage returns included, its quoted spans, escapes and
  plain expansions as written (`a[x;y]=1 cmd`, `a['x' ;y]=1 cmd`, `a[ $i ]=1`,
  `a[ ${i:-1 + 1} ]=v`), its line continuations joined in the name, the subscript and before
  the `=` (`m\` NL `[;]= cmd`, `a[ $\` NL `{ cmd; } ]=1`), also where it looks like a
  function head (`a[()]=1 cmd` is an assignment), the search for its `]` skipping
  substitutions and the quotes nested in a `"${...}"`, and one holding a command
  substitution (also bash 5.3's `${ cmd; }`), `$'...'` or quotes nested in a `${...}` with a
  blank or an operator outside it (`a[$(x) ;y]=1 cmd`) is refused (put the whole index into a
  variable first: `i="$(x) ;y"; a[$i]=1 cmd`), a single-quoted span counting as a
  substitution too, since an indexed array expands its subscript as if in double quotes
  (`b['$(cmd)']=1` runs cmd) and the text does not show the array's kind (so
  `declare -A a; a[ '${ :; }' ]=v` is refused, a known over-refusal that fails closed); an
  array element's subscript holding `$'...'` or a funsub is refused the same way; inside
  arithmetic (`(( a[ ${ cmd; } ]=1 ))`), where a `'` quotes nothing,
  the subscript is read like any word there; the bracket scans of one command share a budget of four times its length, since
  every unclosed `a[` scans to the end, and past it the command is refused (`open_brackets`:
  close each `[`, or quote it), while an `a[` with no `]` after it costs nothing; a blank or
  an operator character inside `${...}` stays in its word (`${x#'/' }cmd`), and inside
  double quotes a single-quoted span in that word still quotes (`"${x#'"'}"`), one holding a
  `}` read both ways, as bash quotes it (`"${x#'}"'}"`) and as a POSIX sh reads its lone `'`
  as text, ending the `${` at that `}`; a process substitution holds
  across a line continuation; a carriage return inside a word stays in it (`X=` CR `1 cmd`
  runs cmd); a backtick pair is written as `$(...)` when its text
  holds a nested backtick, leaves a quote open (closed at its end) or starts with `(`, its
  line continuations joined first (also in a comment there) and a parenthesis it leaves open
  closed as well, and `$$(` as `${$}(`; an array inside an array is the same syntax error,
  and an empty process substitution expands to nothing (`/<()` is `/`); a shell
  whose script name is an expansion reads its input as well (`bash $! <<< '...'`); a
  heredoc delimiter word may hold a backtick pair; a named descriptor `{fd}>` holds across a
  line continuation; inside `$( )` a
  heredoc body ends early only at a line that starts with its delimiter and holds a `)`
  (with `<<''` every line starts with it), and a heredoc in a subshell or `$( )` has its
  unquoted body's substitutions written once more at its start, and what its body leaves
  open for the readings above (a quote, a parenthesis, a `$(` in double quotes) closed;
  redirections move behind the last word of their simple command (`>f mkfs x`,
  `sudo >f rm -rf /`, `rm -rf ~>/dev/null`, `{fd}>f rm -rf /`). The text `bash -c`, `eval`,
  `ssh`, `trap` or a runner runs is written out the same way, and the program a runner or
  `find -exec` starts is judged in its spelling as written (`uv run "$TOOL" -rf ~`). Measured: a differential that runs generated commands
  (comments with apostrophes, heredocs with every kind of delimiter, `$'...'` with `\'`, line
  continuations, every reserved word and brace form, `!`, `time`, `coproc`, functions,
  leading redirections, pipes into groups, `env -S`, `find -exec`) through bash with a marker
  program in the dangerous one's place (each draw in a fresh empty folder, with a PATH of
  harmless tools only), and checks the twin with `rm -rf` in its place: 300 000 draws,
  165 124 of which ran the marker, and every one of their twins refused on host, jailed and
  remote; two further generators (nested quote levels, carriers, arrays, arithmetic and
  heredocs inside substitutions) ran 60 000 and 15 000 draws, 38 369 and 3 432 of which ran
  the marker, again with no twin let through; one that wraps a carrier in quoting layers ran
  120 000 draws, 106 342 of which ran the marker, with none let through; and one that
  mutates working commands ran 100 000 draws, 24 901 of which ran the marker: 2 twins
  passed, through an empty `${| }` before a `/` and empty backticks glued to the option,
  both named below. Run once more on the final code, with two more generators (one builds
  runners, splices fragments and wraps them in containers, one nests subscripts, descriptor
  copies and printed scripts), and a guard that skips any draw naming `-rf` anywhere but after
  the marker: 268 812, 47 261, 15 000, 85 274, 120 000, 60 000 and 150 000 draws, of which
  140 880, 30 046, 3 393, 22 831, 106 429, 54 404 and 73 338 ran the marker; 3 twins passed,
  each of a class named below (a shell's error message fed into a shell, an alias with
  `expand_aliases`, an empty variable before a `/`). And on the code as it stands: the suite's
  own two generators, 269 169 draws with 148 135 running the marker, 2 twins passed (both a
  shell's error message fed into a shell); two generators of subscripts, line continuations,
  funsubs, descriptors and stdin chains, and of the pinned rows mutated, run through bash in a
  read-only sandbox with a marker `rm`: 60 000 and 40 000 draws, 2 473 and 2 350 of the 7 052
  and 4 392 run in bash ran the marker, with no twin let through; and two more, of
  substring windows, `$'...'` in arithmetic, closer targets and words at a shell's script
  name: 30 000 draws each, 2 542 of 6 125 and 2 305 of 4 415 run in bash ran the marker,
  with no twin let through. No refusal of the reading
  before this change was lost in
  any of them (bash 5.3; the suite runs 1 600 draws). The classes the differential found on the way are each pinned in
  `tests/test_command_grammar_reading.py`, and every probe written while the reading was
  built and checked, every pinned row and every corpus row that ran the marker in bash, 1 456
  of them, is in `tests/fixtures/command_policy/never_again.json` and refused on every lane
  (16 more belong to the named boundaries below and are kept apart under their names).
- **Named boundary: what it does not read.** cmd.exe's and PowerShell's own grammar (`%VAR%`
  as the program, the text `pwsh -Command` runs); dash's differences from bash, read as bash,
  the larger grammar; decompressors and other programs that write new text piped into a
  shell (`gunzip`, `tr`, `awk`); a script file an earlier command wrote and a later one runs;
  a variable's value (the text cannot show it, so a variable is refused only as the program,
  or after a protected root of a removal); extglob patterns; carriers it does not step over
  (`flock`, `strace`, `chroot`, `busybox`, `runuser`), which hide the command they run, also
  behind a substitution (`flock f $(true) cmd`), as their plain forms did before; a
  descriptor named through a variable (`exec {fd}< ...; bash /dev/fd/$fd`); a coprocess's or a
  named pipe's text; the file names `find` or `xargs` hand a command (`find . -exec sh -c 'cat
  {}' \;`); the stdin shell of `sudo -s`, `su` or `at`; shell features that store command
  text and run it later or elsewhere: `alias` with `expand_aliases`, `${x@P}`, `PS4` with
  `set -x`, `PROMPT_COMMAND`, `BASH_ENV`, `mapfile`/`readarray -C`, `complete -C`, `bind -x`
  (`trap`'s command is read); the output of other programs piped into a shell beyond those
  named above, also text `sed` or `cut` changes on its way in; a function used as the shell
  for its input (`f() { bash; }; echo ... | f`) or as what prints into one
  (`f() { echo ...; }; f | bash`); a glob as a shell's script name, which `nullglob` can turn
  into nothing (`bash *.q <<< ...`); extglob groups `!(...)`, `*(...)` and `?(...)` in an
  array, refused as a syntax error or an expansion though bash runs them with extglob on; an
  empty substitution or expansion glued to an option (`rm $()-rf /`, `rm $@-rf /`); a variable
  or an empty substitution
  before a `/` with no folder written in front of it (also `rm -rf ${ }/`): `rm -rf $X/`
  passes, since the text cannot show that X is
  empty, and only a variable after a written protected folder is refused (`~/.cache/$X`, not
  `$HOME/.cache/$X`); and a glob
  a file name turns into an option (`rm *` beside a file named `-rf`), both the state of the
  machine rather than the text; a shell's own error messages fed into a shell (`cmd 2>&1 |
  bash` hands on `command not found` lines that quote the command word), also what a
  substitution writes to the standard error or to a descriptor of 3 or more inside `[[ ]]`
  whose descriptor a redirection of that command sends into the pipe
  (`[[ -n $( ( ... >&2 ) >/dev/null ) ]] 2>&1 | bash`,
  `[[ -n $( ( ... >&3 ) >/dev/null 2>/dev/null ) ]] 3>&1 | bash`); a
  shell's input moved to another descriptor and back (`... | bash 1>&0- 0<&1`), read as
  coming from a descriptor this text does not open. A
  `case` statement inside a substitution stays refused as unreadable (see
  `case` above).
- **Comments and heredocs in the other two readings.** Neither takes a `#` as a comment, so
  for them an apostrophe in a comment or a heredoc body opens a quote that swallows the later
  lines; the grammar reading reads those lines. The frozen reading reads every line of a
  heredoc body as a command line: a body line that STARTS with what reads as a command is
  refused (`rm -rf / was the bug`), while one that only mentions it is not
  (`Fix: rm -rf / no longer runs`); to write such text into a file use the file tool, and for
  a commit or pull request message `git commit -F FILE` or `gh pr create --body-file FILE`,
  which the refusal says. The current reading takes a heredoc body as data where it surely is
  one (`<<WORD`, `<<'WORD'`, `<<-WORD` at the top level, outside quotes, `${...}`, `$[...]` and
  a comment, with its delimiter line present, also in a text with CRLF line ends): no
  command lines and no nested readings, so a file
  written through `cat > f <<'EOF'` passes whenever the frozen reading lets it (300 of 300
  measured repository files did). An unquoted body's substitutions are read as commands, with
  their text as bash parses it afresh, and a body fed to a shell (`bash <<'EOF'`, also
  `cat <<'EOF' | bash`) is that shell's script, read after the body's own escapes
  (`\$(...)` runs there). A `<<` after a `#` that starts a comment for bash
  opens no heredoc (`echo ok # <<X`, also `;#`, `|#`, a tab before it, or after a line
  continuation; a `;` inside the comment does not end it): bash ignores the rest of the
  line and runs the next lines, so they are read as commands. A `#` that starts no comment
  for bash leaves the heredoc open: inside a word (`a#b`, `$#`, `16#ff`, and after the `)`
  of `$(...)`, `$((...))`, `<(...)` or an array, where the word goes on: `$(printf x)#`),
  after an escaped blank (`a\ #b`), inside a `${...}` or `$[...]` bash really opens (a
  quoted or escaped `${` opens none, nor `$${`, where `$$` is the process id; a `$[...]`
  closes at its own `]`, a subscript inside it is counted), inside `$(...)` or a backtick
  pair, or after `\v`. After every other `)`, a subshell's, an arithmetic command's
  `(( ))` and a `case` pattern's, a `#` starts a comment. Named boundary: a `$`, a
  backslash-newline and a `{` are not read as `${`, though bash joins the lines first; that
  only keeps a later `#` a comment, so the check refuses more there, never less.
  For an unquoted heredoc whose body
  is expanded, the refusal also offers quoting the delimiter (`<<'EOF'`) only when the
  heredoc goes into a file and nowhere else: the command that receives it is `cat` or
  `tee` and names a file to write (`cat > f; at now <<EOF` is `at`'s heredoc), its line
  holds no `|` or `&&`, no word of the command's own lines (a heredoc body is text and does
  not count) is a shell or interpreter (the ones the check knows as script readers, `csh`
  and `fish` among them, also spelt `/bin/bash` or `b\ash`), `eval`, `source` or `.`, no
  variable stands where a command starts (`$SHELL`, also `"$SHELL"`, and after a reserved
  word that starts one: `{ $SHELL f; }`, `then $SHELL f`, `do $SHELL f`), and the frozen
  reading lets the command pass. For a writer's program refusal, and for a command in the
  body whose program a variable names (a template literal), it comes first. The rule for any
  later reading: it may only ADD refusals to the existing readings, never remove one
  (classify each, refuse if any refuses).

For the main agent's own call, the real safety is still the person's approval plus the
local-only rule in 3. The unattended lanes (the coder and workflow steps, point 2) have no
approval: there it is the account allowlist, the policy block, for workflow steps the
application's authorizer, and the `host_unattended` profile that decide. The controls are
pinned in `tests/test_host_bash.py`, `tests/test_command_policy.py`,
`tests/test_coder_dispatch_gate.py` and `tests/test_unattended_host_share_paths.py`.

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
   different key is refused (`StrictHostKeyChecking=accept-new` on the account's own list,
   for the confirmed first connection only).
   The call goes only to the server the person was asked about: the funnel hands the tool
   the question it put (`_call_question`), the tool builds it again from the server it is
   about to use, and a server forgotten in between (another tab, `vaf ssh forget`, also
   after a standing grant) is refused (`BaseTool.changed_since_asked`). A call the person
   did not confirm as a first connection runs with `StrictHostKeyChecking=yes`
   (`BaseTool.first_contact_confirmed`), and only the account's own list counts
   (`GlobalKnownHostsFile=/dev/null`), so a server forgotten between that check and the
   connection is refused by OpenSSH itself, and the next call asks again. The tool says
   "nothing ran" only when that is certain: exit 255 with nothing come back, OpenSSH's own
   line names the server as unknown, and it is still missing from the list. A changed key
   is read from OpenSSH's own lines first, under `accept-new` and `yes` alike, and only when
   nothing came back; a remote command that prints such words next to its output, or exits
   with anything but 255, is reported as it ran. Named exception: on a remembered server,
   exit 255 with nothing on stdout and OpenSSH's last line "Host key verification failed."
   reads as "OpenSSH reported a key problem", also when a remote command printed that line
   (a nested `ssh server ssh other-host uptime` whose second hop fails can); nothing on the
   client side tells the two apart, and that report never says "nothing ran" or "call
   again".
3. **Never over a messaging channel** (dangerous and restricted with `"channel"`, see 3 in
   the `host_bash` section; from the Veyllo App each call is put to the account holder on a
   card on their device, which also shows the first-connection question, and only a Yes to
   that question takes a new server), and **a regular account only when its allowlist names
   it** (`account_opt_in`).
4. **The remote command is classified** with the `remote` profile: the catastrophic core is
   refused, an installer piped into a shell is named in the dialog.
5. **Each account's own identity**: key, passphrase and servers are the account's, never the
   machine owner's `~/.ssh`, config or ssh-agent ([USER_ISOLATION.md](USER_ISOLATION.md)).
   For the same reason `host_bash` refuses `ssh`, `scp` and `sftp` and points here.

Pinned in `tests/test_ssh.py` (a fake `ssh` on PATH and the real `ssh-keygen`).

### `ftp` - files on a web space (`vaf/tools/ftp.py`)

Lists, uploads (a file or a folder), downloads, deletes, reads part of a file and stats one
over FTPS, or plain FTP when written `ftp://` (`vaf/core/ftp.py`, Python's ftplib); a stored
host, login or port is written `$NAME` in `server` and filled in by the tool. The controls are the `ssh` tool's:
confirmed in the chat, a trusted folder does not silence it, the first connection to a server
is always asked and refused where nobody is asked, the call goes only to the server the person
was asked about, never over a messaging channel, a regular
account only when its allowlist names it, the local side under the account's write jail. For
`ftp` the server is filled in again at the call, so a stored host changed while the question
was open (a second chat's `store_credential`) or after a standing grant is refused instead of
reached, also when both servers were confirmed before and so neither raised a question (the
tool's `call_subject`, handed as `_call_subject`), and a first contact is remembered only after a first-contact question, so a server
forgotten in the last moment is refused by the connection itself. Its
own: the certificate is checked (an authority's, or the fingerprint remembered at the first
connection), the address a server names for passive transfers is not followed, cloud metadata
addresses are refused, and `host_bash` refuses the FTP clients (`ftp`, `lftp`, `ncftp`,
`ncftpput`, `ncftpget`, `tnftp`, `pftp`) and `curl`/`wget` with an `ftp://` or `ftps://` address
(`ftp_transfer` in `vaf/core/command_policy.py`) and points here; so do `python_exec` and
`python -c` / `python -m ftplib` in `host_bash` for Python's ftplib or an `ftp://` address in
the code; the coder's `bash` in a sandbox environment refuses the same. Pinned
in `tests/test_ftp_core.py` and `tests/test_ftp_tool.py`, against a small FTPS server from the
standard library (`tests/ftp_stub.py`).

> **Note on `channel_tools_unrestricted`:** this admin setting (default ON) lets channel sessions
> use the same tools as the main agent and lifts `channel_restrictions` for tools that rely on it
> (e.g. `browser_agent`). A tool that needs a person - `dangerous` and restricted on channels:
> `host_bash`, `python_exec` - is deliberately exempt, because code on a computer with no
> confirmation path must never be reachable from a messaging channel. This used to be a guard
> inside `host_bash` alone, and `python_exec` was lifted with the rest. In the Veyllo App the
> setting plays no part for the tools kept off chats (the host shell, host Python, SSH, FTP,
> the sandbox environments and the rest): each call there asks on the account holder's own
> device instead ([VEYLLO_APP.md](../integrations/VEYLLO_APP.md#tools-in-the-app-chat)).
