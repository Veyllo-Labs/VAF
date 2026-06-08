# Switching between the local model and an API

When you change the provider in Settings (from "Local" to an API such as OpenAI, or the other way round), VAF shows a brief notice overlay and handles the local model's memory correctly.

## Flow in the Web UI

1. In Settings you change the **AI Provider** (e.g. from "Local" to "OpenAI" or back) and save.
2. A centered overlay appears with the text **"Changing model"** and a short note.
3. The overlay stays visible for about 5 seconds, then the page reloads.
4. After the reload the new provider is active; for Local the local model is loaded, or it loads on the next request.

The overlay mirrors the behavior when toggling the network settings (Local Network on/off): same look, no close button, automatic reload after a few seconds.

## Backend behavior (memory / VRAM)

- **Local to API:**
  Saving the config triggers a `RELOAD_CONFIG` path in the headless runner. The agent is switched to API operation and the local agent LLM instance is reset. The server process is not stopped by a dedicated `provider` observer in `tray.py`; it follows the regular runtime/idle management.

- **API to Local:**
  After `RELOAD_CONFIG` the local path becomes active again. The local model/server is then provided through the normal activity and load routine.

The switch runs through a config save plus a queue command (`__CMD__:RELOAD_CONFIG`), not through an explicit `provider` branch in `tray.py`.

## Technical notes

- **Config/WebSocket:** On save the API marks the provider change (`requires_refresh: true`) and queues `__CMD__:RELOAD_CONFIG`.
- **Headless runner:** `RELOAD_CONFIG` updates the agent context (provider/backend, LLM reset, `use_server` path).
- **Tray:** `on_config_changed` handles model-, context-, gpu- and network-related keys; no separate `provider` branch.
- **WebSocket:** On config save the `config_saved` response may include `requires_refresh: true` (e.g. on a provider change). In that case the Web UI shows the same overlay and reloads the page after 5 seconds.

## Related documentation

- **WEB_UI.md** — Web UI overview and status indicators
- **WEBUI_WEBSOCKET_FLOW.md** — message types (including `config_saved`, `model_state`)
- **SYSTEM_TRAY.md** — tray, idle timeout, and persistent mode
