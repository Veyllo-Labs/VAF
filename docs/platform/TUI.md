# The Terminal App (`vaf run`)

`vaf run` opens a full-screen terminal app (built on Textual): a scrolling
transcript with streamed answers and the model's reasoning as a separate muted
think block, tool cards, event narration, the sub-agent status line, the
context-usage bar, the agent's avatar, and keyboard-complete overlays for
settings, model, history, sessions and help.

The transcript is strictly chronological - newest at the bottom, always: a
reply streamed after a tool call appears below that tool card (anything
mounted below the live reply seals it; the next chunk opens a new one at the
bottom). The avatar is the agent's bracket body with the animated eye,
`[ ● ]`, drawn in the head row of the NEWEST reply (`[ ● ] VAF · HH:MM`);
older replies drop it, and there is no fixed avatar chrome anywhere else. The
eye animates with the agent's state (blink when idle, pulse when thinking,
orbiting satellite when a tool runs, ring on success) and stays white in every
theme. The previous lanes remain available:

| Lane | How to get it | What it is |
|------|---------------|------------|
| `app` (default) | `vaf run` | The full-screen terminal app described here. |
| `modern` | `tui_mode: modern` in the config, or `vaf run --web` (unless the `--classic` flag is given) | The prompt-toolkit lane with the bottom toolbar. |
| `classic` | `vaf run --classic` | The plain text prompt. |

The `tui_mode` config key picks the default lane; the `--classic` flag
overrides it per invocation (see
[CONFIG_SCHEMA.md](../setup/CONFIG_SCHEMA.md)).

## Architecture: a client over the framework seams

The app is an in-process client of the same seams the web UI uses - there is no
second turn implementation:

- **Module map** (`vaf/cli/tui_app/`): `theme_bridge.py` converts the vaf theme
  catalog (`vaf.cli.themes.THEMES`, single source of truth) into Textual
  themes; `widgets.py` holds transcript and chrome widgets; `screens.py` the
  overlays; `agent_bridge.py` the single agent lane; `app.py` the assembly and
  `run_tui` entry point. Textual is imported lazily inside the `vaf run`
  dispatch, so every other command keeps the slim import graph.
- **Turns** run through the imported classic function `_process_agent_message`
  (streaming, think-state machine, web mirroring, session persistence),
  rendered through a small duck surface that maps the classic `dim` style onto
  the think channel. One daemon worker lane serializes turns and drains
  (`chat_step` is not reentrant) - a daemon deliberately, so quitting mid-turn
  never blocks process exit; shutdown cancels a waiting gate through the same
  resolve contract first.
- **Sub-agent results** are drained by the imported `_check_subagent_results`
  (the exactly-once claim lives there, unchanged), on a timer instead of the
  classic notifier thread; the summary turn runs with the classic flags
  (`disable_workflows=True`, `disable_tools=False`).
- **Engine events** arrive over `agent.set_event_sink` (tool_start/tool_end/
  gate_required/gate_decision) and become tool cards and the gate overlay.
- **Narration** (`UI.event`: Router, Context, Memory lines) arrives over the
  console-sink hook (`UI.add_console_sink` + `UI.set_app_mode`) and renders as
  event lines in the transcript; with app mode on, the raw console print is
  suppressed while the Web UI log bridge keeps running.
- **The confirmation gate** finally has a terminal responder: the agent's
  `_ask_user_about_gate` (the decide hook its tool caller is built with) waits
  on `web_interface.register_gate` whenever a session id is bound, which
  previously timed out (300 s, then cancel) unless a browser was watching. The
  app answers through the same `web_interface.resolve_gate` contract the web
  UI uses.

## Keys and commands

Enter sends, Ctrl+J inserts a newline. The classic run-loop words still work
typed alone into the prompt: `s` settings, `c` model, `t` theme, `h` history,
`l` voice, `?` help, `exit` quits. Slash commands (`/settings`, `/model`,
`/theme`, `/history`, `/sessions`, `/voice`, `/help`, `/exit`) and Ctrl+P
(palette), Ctrl+S (sessions), F1 (help), Ctrl+Q (quit) route to the same
places. `@path/to/file` inlines a file into the message, as in the classic
lane. Every overlay walks with arrow keys, activates with enter or space, and
closes with esc.

The settings overlay is the `vaf settings` main menu as a stacked arrow menu:
boolean rows flip their real config keys immediately; rows whose flows need an
agent rebuild or a real backend (provider and model switches, context limit,
model download, microphone) show live values and point to `vaf settings` until
their flows land.

## Named boundaries (this round)

- The app lane does not start the web dashboard; `vaf run --web` therefore
  keeps the modern lane, which owns the server startup wiring (heartbeat,
  web-input watcher, result notifier).
- Voice capture, session switching, provider and model switching, and the
  completion/history machinery of the prompt land in the next round; the
  overlays that represent them say so instead of pretending. The classic
  lane's speech preloads (TTS engine warmup, the STT microphone check, the
  langid preload) are likewise not run at boot; until the voice round, the
  first spoken reply pays the engine spin-up lazily.
- Boot (model load, warmup) runs in the plain terminal BEFORE the app takes
  the screen: llama.cpp writes C-level stderr that would corrupt app mode.
