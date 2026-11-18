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

This live load/unload is driven by the **tray activity loop** (which reads `provider` each tick - desktop only), NOT by `on_config_changed`. That is a statement about the LOCAL MODEL's memory only. The BACKEND swap is a separate lane and `on_config_changed` does own it: it has a branch on `provider` and on any `api_key_*`, and applies the change to the running agents. The config save itself also still queues `__CMD__:RELOAD_CONFIG` for the headless runner, so both lanes converge on the same per-agent method.

## Technical notes

- **Config/WebSocket:** On save the API marks the provider change (`requires_refresh: true`) and queues `__CMD__:RELOAD_CONFIG`. Note the asymmetry: `requires_refresh` is computed from the PROVIDER only, so a key-only change reaches the backend but produces no UI refresh signal.
- **Headless runner:** `RELOAD_CONFIG` updates the agent context (provider/backend, LLM reset, `use_server` path) for **every** agent in the process, not only the one whose command loop received it - the pool holds one per `parallel_main_workers`.
- **Tray:** `on_config_changed` handles model-, context-, gpu- and network-related keys, **and the backend swap for `provider` and every `api_key_*`** via `reload_all_api_backends`. What the activity loop (`check_activity_loop`) handles separately is the local model's MEMORY, not the backend: it reads `provider` each tick and, on cloud/API with the model still loaded, unloads immediately; on the switch back to local it loads. (Desktop tray only.)
- **An API key changing is a switch too.** It leaves `provider` equal, so every no-op guard on the path treats it as nothing to do unless the caller forces the reload. This is not theoretical: a replaced key was ignored until a restart because the re-apply reached one agent out of the pool.
- **Lane models:** saving a new provider resets `subagent_model` (the tool / workflow model) and `browser_agent_model` to empty when they are not the new provider's models, so Settings then shows "Same as main chat" and sub-agents stop warning that the model belongs to another provider. A lane with its own provider keeps its model. This holds on every path (web, REST, `vaf settings`, `vaf models config`, the terminal app, setup), because the rule sits in `Config.save`; `Config.load` repairs a configuration saved before it. A pick is not remembered per provider: switching back leaves the lane on the provider's own model.
- **WebSocket:** On config save the `config_saved` response may include `requires_refresh: true` (e.g. on a provider change). In that case the Web UI shows the same overlay and reloads the page after 5 seconds.

## In the terminal

The terminal routes share ONE switch, `vaf.core.provider_switch.switch_provider`: the
full-screen terminal app (its picker, opened with `c` or from Settings > AI Provider / API
Model, through `AgentBridge.apply_provider_change`), `vaf settings` and `vaf models config
provider`. In this order it:

1. checks the name against the provider registry (`local` or an API provider) and refuses
   `local` under the `api` inference profile with the profile's sentence;
2. refuses an API provider without a key, or with a stored key that cannot be read; a key
   typed in that moment is stored first and checked with one real request, and a key that
   fails is kept while the provider stays (the request can fail on the network as easily
   as on the key); a stored key is not re-tested;
3. writes `provider` and `api_model_<provider>` under one hold of the config lock;
4. calls `reload_all_api_backends(force=True)` for the live agents of the process.

A refusal writes no config key (a typed key that failed its check stays in the key store),
and no result text carries the key. The switch never resets a
lane model itself: `Config.save` does (see "Lane models" above), and the result only names
what it reset, which `vaf models config provider` prints.

**API to local in the terminal app.** The backend reload leaves an agent moved to `local`
with nothing to serve, and the terminal app's turn path loads no model, so the app loads it
right after the switch (`reload_local_model`, the conversation stays) and says so; with
llama.cpp and no GGUF set, that load first downloads the default model for this machine,
and the note says that instead, and a load that fails names the server's log. The
picker lists `local · llama.cpp` and `local · vLLM` above the API providers; picking an
engine that is not the one in use switches the engine first (`local_engine.apply`, asking
before a vLLM install) and moves the provider only once that switch is done.

**Not on this switch, by design:** the web Settings save (it saves the whole page,
API keys from the same payload included, and answers `requires_refresh`; see above),
`vaf setup` (it writes the key, `provider` and `vision_provider` exactly as the web wizard
does, before any agent exists), and the rebuild of the prompt-toolkit and classic terminal
lanes after their settings menu (their writes go through the switch, the rebuild is theirs).
`vaf engine` prepares an engine and leaves the provider alone; with an API provider it says
so. `vaf models config subagent_provider <name>` writes the name and its switch together
(`set_subagent_provider`), refusing an unknown name, a provider without a key, and `local`
under the `api` profile.

## Related documentation

- **WEB_UI.md** - Web UI overview and status indicators
- **WEBUI_WEBSOCKET_FLOW.md** - message types (including `config_saved`, `model_state`)
- **SYSTEM_TRAY.md** - tray, idle timeout, and persistent mode
