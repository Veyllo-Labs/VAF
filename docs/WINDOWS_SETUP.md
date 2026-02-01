# 🪟 VAF on Windows: Setup & Usage Guide

VAF is optimized for the Windows environment, offering a seamless background service integration while maintaining powerful command-line capabilities.

## 📥 Installation

The recommended method for setting up VAF on Windows is via the automated PowerShell script. This ensures all dependencies, including system-specific drivers for GPU acceleration and speech synthesis, are correctly configured.

1. Open PowerShell in the project root directory.
2. Execute the setup script:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\setup_win.ps1
   ```

### Installation Actions:
- **Virtual Environment**: Creates an isolated Python environment (`venv`) to manage dependencies.
- **System Integration**: Installs and patches `pywin32` for reliable background operation and COM interaction.
- **Dependency Management**: Installs all required packages in editable mode.
- **Shortcuts**: Generates "VAF Agent" shortcuts on the **Desktop** and in the **Start Menu**.
- **Visuals**: Auto-generates high-resolution application icons.

---

## 🚀 Operation Modes

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

## 🎨 System Tray Status

The tray icon provides immediate visual feedback on the agent's state:
- 🟢 **Green (Active)**: The agent is currently processing a request or task.
- 🟡 **Yellow (Idle)**: The agent is standing by. Resources (VRAM) may be released depending on configuration.
- 🔴 **Red (Persistent)**: The model remains loaded in memory for instant response times.

---

## 🌐 Local Network Hosting

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

## 🛠️ Troubleshooting

### 1. Startup Issues
If the application fails to launch, consult the startup trace log:
`logs/startup_trace.txt`

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
