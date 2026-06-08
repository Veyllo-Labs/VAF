# VAF Sandboxing

Security is paramount when allowing an AI to execute code. VAF uses **Docker Containers** to isolate generated code execution from your host operating system.

---

## 🔒 Security Model

**Docker is REQUIRED for code execution.** There is NO fallback to host execution.

| Tool | Isolation | Use Case |
|------|-----------|----------|
| `python_sandbox` | ✅ Docker Container | Safe code execution (default) |
| `python_exec` | ❌ Host System | Only with explicit user trust |

File tools (e.g. `librarian_agent`, `read_file`) block access to the VAF installation directory; the agent is instructed not to request operations on that path.

### Isolation model: Docker-level, not Python-level

VAF's sandbox isolation is enforced entirely at the **Docker container level**. There is no Python-level module blocklist — standard-library modules like `subprocess`, `socket`, and `os` are importable inside the container. What prevents abuse is:

- **Process namespace isolation** — processes cannot escape the container
- **Filesystem isolation** — host filesystem is not mounted (code runs in `/tmp/vaf_*` per execution)
- **Resource limits** — 512 MB memory, 0.5 CPU cores (hard limits via Docker)
- **No host privilege escalation** — default Docker unprivileged mode

**What is NOT blocked at Python level:**
- `import subprocess` — works, spawns processes inside the container only
- `import socket` — works; sandbox has outbound network access (needed for pip and Tool Bridge)
- `import os` — works; filesystem access is limited to what Docker mounts, not by Python

See [`SANDBOX_MODULES.md`](SANDBOX_MODULES.md) for the full module reference and security details.

---

## 🚀 Smart Auto-Start

VAF tries to make sandboxing seamless:
1. **Detection:** Before running code, VAF checks if the Docker Daemon is running.
2. **Auto-Start:** If Docker is installed but stopped, VAF attempts to launch it automatically:
   - **macOS:** Launches `Docker.app`.
   - **Windows:** Launches `Docker Desktop.exe`.
   - **Linux:** Triggers `docker.socket` (requires systemd socket activation).
3. **Polling:** VAF waits up to 30 seconds for the daemon to become ready.

---

## ⚠️ No Fallback (By Design)

If Docker is **not installed** or **cannot be started**:
- Code execution is **BLOCKED** (not degraded to host)
- You will see: `[SECURITY] Sandbox requires Docker: ...`
- This is intentional — we do not compromise on security

To execute code, you must:
1. Install Docker Desktop from https://docker.com
2. Start Docker Desktop
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
| **Container** | vaf-sandbox (persistent) |
| **Memory** | 512MB |
| **CPU** | 0.5 Cores |
| **Network** | `vaf-sandbox-network` (isolated bridge). Cannot reach postgres/redis/gotenberg/tts/stt by hostname. Outbound internet (pip install) and Tool Bridge back-channel (`host.docker.internal`) still work. |
| **Filesystem** | Isolated (no host access). Packages installed via `pip` persist in the container between executions (by design, for performance). |
| **Workspace** | Per-execution temp dir under `/tmp/vaf_*` (unique UUID per run, auto-deleted after). Container `working_dir` is `/workspace` (persistent volume), but code always executes in the per-run `/tmp/vaf_*` dir. |
| **Capabilities** | `cap_drop: ALL`, `no-new-privileges: true` — container has no Linux capabilities beyond default isolation. |
| **Module blocking** | None at Python level — `subprocess`, `socket`, `os` are importable. Constraints are enforced by Docker process/filesystem isolation, network isolation, and resource limits, not by a Python import blocklist. |

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

The sandbox supports **Programmatic Tool Calling** — code inside the sandbox can call any VAF tool via an injected `vaf_tools` module. Only the final `print()` output of the script returns to the model context; intermediate tool results are consumed entirely inside the running script and never become chat messages.

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
- `vaf/core/tool_bridge.py` — `ToolBridgeServer`, `_BridgeHandler`, stub source
- `vaf/tools/python_sandbox.py` — `_build_call_tool_fn()`, `_run_with_bridge()`

### Security properties

| Property | Detail |
|---|---|
| Token | `secrets.token_hex(16)` per execution — mismatches rejected (HTTP 403) |
| Binding | `0.0.0.0` on host, random ephemeral port. Accessible from any interface on the host; relies on the per-execution token for authentication. |
| Trust gates | All calls go through `agent.execute_tool()` — full VAF gate pipeline applies |
| Cleanup | `bridge.stop()` in `finally` block — no port leak on crash |

### Host gateway by OS

The sandbox container connects back to the host via `host.docker.internal` on all platforms:

| OS | How it resolves |
|---|---|
| Windows | Docker Desktop DNS alias — automatic |
| macOS | Docker Desktop DNS alias — automatic |
| Linux | `extra_hosts: ["host.docker.internal:host-gateway"]` in `docker-compose.memory.yml` injects the host IP (Docker 20.10+) |

---

## Troubleshooting

**Error: "Docker is not installed"**
- Install Docker Desktop from https://docker.com
- Re-run the VAF installer: `.\install.bat` (Windows) or `./install.sh` (Linux/macOS)

**Error: "Docker Daemon is not running"**
- Open Docker Desktop manually
- Ensure you have accepted the Docker Desktop Terms of Service (common first-run issue)
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

Use this only when you need host filesystem/network access and trust the code source.
