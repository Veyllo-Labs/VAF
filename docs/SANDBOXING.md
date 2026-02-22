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

| Resource | Limit |
|----------|-------|
| **Image** | python:3.11-slim |
| **Container** | vaf-sandbox (persistent) |
| **Memory** | 512MB |
| **CPU** | 0.5 Cores |
| **Network** | Enabled (for pip install + Tool Bridge) |
| **Filesystem** | Isolated (no host access) |
| **Workspace** | Per-execution temp dir under `/tmp/vaf_*` (auto-cleaned) |

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
| Binding | `0.0.0.0` on host, random free port — not externally exposed |
| Trust gates | All calls go through `agent.execute_tool()` — full VAF gate pipeline applies |
| Cleanup | `bridge.stop()` in `finally` block — no port leak on crash |

### Host gateway by OS

The sandbox container connects back to the host via:

| OS | Address |
|---|---|
| Windows | `host.docker.internal` (Docker Desktop DNS alias) |
| macOS | `host.docker.internal` (Docker Desktop DNS alias) |
| Linux | `172.17.0.1` (Docker bridge gateway — the host LAN IP is NOT reachable from inside the container) |

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
- The sandbox container cannot reach the host bridge address
- On Linux, ensure Docker is using the default bridge network (`172.17.0.1`)
- Check that no firewall rule blocks the host port: `sudo ufw allow 32768:65535/tcp` (temporary test)
- On custom Docker networks (non-default bridge), set the bridge IP manually if needed

---

## python_exec (Unsafe Alternative)

The `python_exec` tool runs code directly on your host system. It is:
- **Disabled by default**
- Only available with explicit trust configuration
- Shows clear warnings when used

Use this only when you need host filesystem/network access and trust the code source.
