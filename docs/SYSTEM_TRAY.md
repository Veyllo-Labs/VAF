# System Tray & Persistent Server

VAF includes a persistent background service managed by a system tray application. This allows for instant agent availability, dynamic resource management, and a native desktop experience.

## Features

- **Persistent Icon**: A system tray (Windows/Linux) or menu bar (macOS) icon indicates the server state.
    - 🟢 **Green / Active**: Server is running, model is loaded into RAM.
    - 🟡 **Yellow / Idle**: Server is standing by, model is unloaded (saves RAM).
    - 🔵 **Blue / Persistent**: Model is pinned in RAM (Persistent Mode).
- **macOS Dock Integration**: On macOS, clicking the Dock icon (when VAF is already running) focuses the app and opens/re-activates the Web UI.
- **Smart Tab Reuse**: If a VAF tab is already open in Safari or Google Chrome, clicking the Dock icon will re-focus that existing tab instead of opening a new one.
- **Dynamic Resource Management**: Automatically unloads the LLM from RAM after 10 seconds (default) of inactivity to free up system resources.
- **Instant Wake-on-Demand**: The server wakes up instantly when you run a CLI command (`vaf run`) or open the Web UI.
- **Graceful Shutdown**: Checks for active CLI sessions before quitting to prevent data loss.

## Usage

### Starting the Tray App

**macOS (Recommended):**
1. Open **Spotlight** (Cmd + Space).
2. Type `VAF` and press Enter.
3. VAF will appear in your **Dock** and **Menu Bar**.

**CLI (All Platforms):**
```bash
vaf tray
```

### Menu Options

- **Open WebUI**: Opens the VAF dashboard in your default browser.
- **Persistent Server**: Toggle this to keep the model loaded in RAM even when idle (useful for frequent interaction).
- **Quit**: Stops the server and exits the application.

### CLI Integration

When the tray app is running, `vaf run` automatically detects it and uses the shared server instance instead of spinning up a separate one. This drastically reduces startup time for new sessions.

## Configuration

Settings are managed in `~/.vaf/config.json`:

| Setting | Default | Description |
| :--- | :--- | :--- |
| `server_idle_timeout` | `10` | Seconds to wait before unloading model |
| `persist_server` | `false` | If true, model stays loaded (same as checkbox) |

## Architecture

The system uses a shared `TrayContext` to manage state between the Uvicorn web server and the UI loop.
- **macOS**: Uses `rumps` for a native Cocoa menu bar experience.
- **Windows/Linux**: Uses `pystray` for cross-platform system tray support.
