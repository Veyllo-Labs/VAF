# VAF Sandboxing

Security is paramount when allowing an AI to execute code. VAF uses **Docker Containers** to isolate generated code execution from your host operating system.

## 🚀 Smart Auto-Start

VAF tries to make sandboxing seamless:
1.  **Detection:** Before running risky code, VAF checks if the Docker Daemon is running.
2.  **Auto-Start:** If Docker is installed but stopped, VAF attempts to launch it automatically:
    *   **macOS:** Launches `Docker.app`.
    *   **Windows:** Launches `Docker Desktop.exe`.
    *   **Linux:** Triggers `docker.socket` (requires systemd socket activation).
3.  **Polling:** VAF waits up to 30 seconds for the daemon to become ready.

## ⚠️ Fallback Mode (Unsafe)

If Docker is **not installed** or **cannot be started** after 30 seconds:
*   VAF will fall back to executing code directly on your **Host System**.
*   You will see a warning: `⚠️ SANDBOX OFFLINE: Executing on HOST (Less Secure)!`
*   In this mode, the agent has full access to your files and network (limited only by your OS user permissions).

## Configuration

The sandbox currently uses a default image: `python:3.11-slim`.
It is ephemeral (destroyed after use) and has limits applied:
*   **Memory:** 512MB
*   **CPU:** 0.5 Cores
*   **Network:** Enabled (for `pip install`, etc.)

## Troubleshooting

**Error: "Docker Daemon is not running"**
*   If auto-start fails, please open Docker Desktop manually.
*   Ensure you have accepted the Docker Desktop Terms of Service (common issue on first run).

**Error: "Image not found"**
*   The sandbox attempts to pull `python:3.11-slim` automatically. Ensure you have internet access for the first run.