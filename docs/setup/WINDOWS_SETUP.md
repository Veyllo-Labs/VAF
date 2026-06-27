# VAF on Windows: Setup & Usage Guide

On Windows, VAF runs as a background service and provides a command-line interface.

## Installation

The recommended method is the automated installer. From the project root in PowerShell, run:

```powershell
.\install.bat
```

(or `powershell -ExecutionPolicy Bypass -File .\install.ps1`). It provisions a bare machine
without admin rights.

### Installation actions:
- **Python**: not required up front — if no suitable Python is found, the installer installs
  [uv](https://docs.astral.sh/uv/) and provisions Python itself.
- **Node.js**: not required up front — if missing, a portable Node is downloaded into
  `%LOCALAPPDATA%\Veyllo\node` for the web UI.
- **Virtual environment**: creates an isolated `venv` and installs the Python dependencies (editable mode).
- **System integration**: installs and patches `pywin32` for reliable background operation and COM interaction.
- **Docker** *(optional)*: **detected, not installed** — used for the memory/RAG system and the code
  sandbox if a runtime is present (Docker Desktop, or Docker Engine in WSL2). The app runs without it.
- **Shortcuts & icons**: "VAF Agent" shortcuts on the **Desktop** and in the **Start Menu**, with generated app icons.

---

## Operation Modes

VAF offers two primary modes of operation:

### 1. Desktop Mode (Recommended)
Launch VAF using the **VAF Agent** shortcut.
- **Background Service**: Runs silently in the background without obstructing your workspace.
- **System Tray**: A status icon appears in the notification area (system tray).
- **Dashboard**: The Web UI automatically opens in your default browser.
- **Smart Launch**: If VAF is already active, using the shortcut focuses the existing dashboard.

### 2. Terminal Mode (Advanced)
For developers requiring direct output access or debugging:
```powershell
vaf run
```
- **Interface**: Launches the interactive Terminal User Interface (TUI).
- **Web UI**: By default, this mode does not launch the Web UI. Append `--web` to enable it.

---

## System Tray Status

The tray icon provides immediate visual feedback on the agent's state:
- 🟢 **Green (Active)**: The agent is currently processing a request or task.
- 🟡 **Yellow (Idle)**: The agent is standing by. Resources (VRAM) may be released depending on configuration.
- 🔵 **Blue (Persistent)**: The model remains loaded in memory for instant response times.

---

## Local Network Hosting

VAF includes secure capabilities to share the agent within your local network (LAN).

### enabling Network Access
1. Open **Settings** via the Web UI.
2. Navigate to the **Local Network** tab.
3. Toggle **Enable Local Network Hosting**.
4. Review the security warning and confirm.
5. Click **Save Changes**. The server will restart automatically to apply the new network bindings.

### Security Features
- **Firewall Integration**: Automatically configures Windows Firewall to allow access only from private IP ranges (RFC 1918). Public internet access remains blocked.
- **Authentication**: Non-localhost connections require a username/password or 2FA login.
- **Connection Tracking**: Real-time monitoring of connected devices via the **Network Topology** map.

### Configuration
- **Port**: Customizable frontend port (Default: 3000).
- **Host IP**: Displays your machine's LAN IP address for easy sharing.

---

## Troubleshooting

### 1. Startup Issues
If the application fails to launch, consult these logs (in order of usefulness):

- **`logs/tray_startup_YYYY-MM-DD.txt`** – Always written when the tray is started (shortcut or `vaf tray`). Shows whether the shortcut launched, singleton status, and any crashes. One file per day; old files are removed by the garbage collector after gc_max_age_hours.
- **`logs/startup_trace.txt`** – Detailed trace (only when Debug Logs is enabled — on by default; disable via `debug_logs_enabled: false` in `~/.vaf/config.json`).

**Tray icon hidden?** On Windows 10/11, the VAF icon may be in the overflow area. Click the `^` arrow in the system tray to see all icons.

### 2. Network Port Conflicts
If VAF cannot bind to the required ports (3000/8001):
1. The system attempts to automatically kill stale processes.
2. If issues persist, verify no other applications are using these ports.
3. **Manual Cleanup**:
   ```powershell
   netstat -ano | findstr :3000
   taskkill /F /PID <PID> /T
   ```

### 3. Localization Issues
VAF is designed to handle non-English system locales (e.g., German Windows) correctly. If you experience process management issues, ensure your VAF version is up to date, as robust locale handling has been implemented.
