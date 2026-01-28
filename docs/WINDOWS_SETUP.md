# 🪟 VAF on Windows: Setup & Usage

VAF is fully optimized for Windows, providing a seamless background service experience while allowing powerful CLI interactions.

## 📥 Automated Installation

The easiest way to set up VAF on Windows is using the provided PowerShell script. This script creates a virtual environment, installs all dependencies (including Windows-specific drivers for GPU and Speech), and creates shortcuts for you.

1. Open PowerShell in the project directory.
2. Run the setup script:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\setup_win.ps1
   ```

### What this script does:
- Creates a `venv` (Virtual Environment).
- Installs `pywin32` and patches it for COM/Background stability.
- Installs all requirements and the VAF package in editable mode.
- **Creates Shortcuts**: Adds "VAF Agent" to your **Desktop** and **Start Menu**.
- **Icon Generation**: Auto-generates high-quality icons from the VAF logo.

---

## 🚀 Running VAF

There are two primary ways to interact with the framework:

### 1. Desktop Mode (Recommended for Daily Use)
Start VAF by double-clicking the **VAF Agent** shortcut on your Desktop.
- **Behavior**: VAF starts as a silent background process (no console window).
- **Tray Icon**: A VAF icon appears in your System Tray (near the clock).
- **Web UI**: Your default browser will automatically open the Dashboard.
- **Singleton**: If VAF is already running, clicking the shortcut will simply bring the existing browser tab to the foreground.

### 2. Terminal Mode (For Developers & Power Users)
Run the agent directly in your command line:
```powershell
vaf run
```
- **Behavior**: Starts the interactive Terminal UI (TUI). 
- **Note**: This mode **does not** start the Web UI by default to save resources. Use `vaf run --web` if you want both.

---

## 🎨 Tray Icon States

The system tray icon uses the VAF logo with a small status indicator:
- 🟢 **Green**: Agent is active (processing a request).
- 🟡 **Yellow**: Idle (ready, model may be unloaded to save VRAM).
- 🔴 **Red**: Persistent Mode (model is kept in VRAM for instant response).

---

## 🛠️ Troubleshooting

If you encounter issues, especially with the background service, check the following:

### 1. Startup Logs
VAF writes a detailed trace of the background startup process to:
`logs/startup_trace.txt`
Check this file if the Web UI shows "Disconnected" or if the tray icon fails to appear.

### 2. Cleaning up "Zombies"
If you suspect old processes are blocking ports (like Port 3000 or 8001), run these commands in PowerShell:
```powershell
# Kill only VAF-related Node/Next.js processes (Safe for your environment)
netstat -ano | findstr :3000
# Look for the PID at the end of the line, then:
taskkill /F /PID <PID> /T
```

### 3. Speech/COM Errors
VAF uses Windows COM for speech. If the server hangs during startup, ensure `pywin32` is correctly initialized by running:
```powershell
.\venv\Scripts\python.exe scripts\fix_venv.py
```
