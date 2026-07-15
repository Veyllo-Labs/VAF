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
  Saving the config triggers a `RELOAD_CONFIG` path in the headless runner (the agent switches to API operation and the local agent LLM instance is reset). In addition, the tray's activity loop (`check_activity_loop`) reads the provider live every tick and, on a cloud/API provider, **unloads the local model immediately** (`server_mgr.stop_server` + `set_model_loaded(False)`) to free VRAM/RAM - it no longer waits for the idle window. The unload is deferred while a thinking run is active, and it is skipped while `voice_agent_provider = "local"` with websockets connected (the dedicated voice model legitimately serves the live call next to a cloud main provider; the normal ws-idle unload still frees it once the UI is gone).

- **API to Local:**
  On the switch back the activity loop detects the local provider and **(re)loads the local model** right away (`start_model_async`). After `RELOAD_CONFIG` the local path is active again.

This live load/unload is driven by the **tray activity loop** (which reads `provider` each tick — desktop only), NOT by `on_config_changed`. The config save itself still queues `__CMD__:RELOAD_CONFIG` for the headless runner.

## Technical notes

- **Config/WebSocket:** On save the API marks the provider change (`requires_refresh: true`) and queues `__CMD__:RELOAD_CONFIG`.
- **Headless runner:** `RELOAD_CONFIG` updates the agent context (provider/backend, LLM reset, `use_server` path).
- **Tray:** `on_config_changed` handles model-, context-, gpu- and network-related keys. The **provider** switch is handled separately by the activity loop (`check_activity_loop`), which reads `provider` each tick: cloud/API + model still loaded → unload immediately; switch back to local → load. (Desktop tray only.)
- **WebSocket:** On config save the `config_saved` response may include `requires_refresh: true` (e.g. on a provider change). In that case the Web UI shows the same overlay and reloads the page after 5 seconds.

## Related documentation

- **WEB_UI.md** — Web UI overview and status indicators
- **WEBUI_WEBSOCKET_FLOW.md** — message types (including `config_saved`, `model_state`)
- **SYSTEM_TRAY.md** — tray, idle timeout, and persistent mode
