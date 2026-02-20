# VAF Sandboxing

Security is paramount when allowing an AI to execute code. VAF uses **Docker Containers** to isolate generated code execution from your host operating system.

## 🔒 Security Model

**Docker is REQUIRED for code execution.** There is NO fallback to host execution.

| Tool | Isolation | Use Case |
|------|-----------|----------|
| `python_sandbox` | ✅ Docker Container | Safe code execution (default) |
| `python_exec` | ❌ Host System | Only with explicit user trust |

File tools (e.g. `librarian_agent`, `read_file`) block access to the VAF installation directory; the agent is instructed not to request operations on that path.

## 🚀 Smart Auto-Start

VAF tries to make sandboxing seamless:
1.  **Detection:** Before running code, VAF checks if the Docker Daemon is running.
2.  **Auto-Start:** If Docker is installed but stopped, VAF attempts to launch it automatically:
    *   **macOS:** Launches `Docker.app`.
    *   **Windows:** Launches `Docker Desktop.exe`.
    *   **Linux:** Triggers `docker.socket` (requires systemd socket activation).
3.  **Polling:** VAF waits up to 30 seconds for the daemon to become ready.

## ⚠️ No Fallback (By Design)

If Docker is **not installed** or **cannot be started**:
*   Code execution is **BLOCKED** (not degraded to host)
*   You will see: `[SECURITY] Sandbox requires Docker: ...`
*   This is intentional - we do not compromise on security

To execute code, you must:
1. Install Docker Desktop from https://docker.com
2. Start Docker Desktop
3. Re-run your code request

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
| **Network** | Enabled (for pip install) |
| **Filesystem** | Isolated (no host access) |
| **Workspace** | /workspace (persistent volume) |

### Performance

Using a persistent container provides:
- **~800ms** execution time (vs 5-10s for ephemeral containers)
- Pre-installed packages persist across executions
- Instant startup (no container creation overhead)

## Installing Packages

The `python_sandbox` tool supports installing pip packages:

```python
# Via agent: "Install numpy and calculate something"
# Tool call:
python_sandbox(
    code="import numpy as np; print(np.array([1,2,3]).mean())",
    packages=["numpy"]
)
```

## Troubleshooting

**Error: "Docker is not installed"**
*   Install Docker Desktop from https://docker.com
*   Run the VAF installer again: `.\install.bat` (Windows) or `./install.sh` (Linux/macOS)

**Error: "Docker Daemon is not running"**
*   Open Docker Desktop manually
*   Ensure you have accepted the Docker Desktop Terms of Service (common issue on first run)
*   On Linux: `sudo systemctl start docker`

**Error: "Image not found"**
*   The sandbox pulls `python:3.11-slim` automatically on first use
*   Ensure you have internet access for the first run
*   Manual pull: `docker pull python:3.11-slim`

## python_exec (Unsafe Alternative)

The `python_exec` tool runs code directly on your host system. It is:
- **Disabled by default**
- Only available with explicit trust configuration
- Shows clear warnings when used

Use this only when you need host filesystem/network access and trust the code source.