# The Terminal App (`vaf run`)

`vaf run` opens a full-screen terminal app (built on Textual). A session opens
with a start block in the shape of `neofetch`, centred rather than pinned to a
corner: the Veyllo mark as terminal art, then version, the agent's name
from its identity, the session name and id, the active model, the local date
and time, and the one line that says how to reach older sessions. Below
that: a scrolling
transcript with streamed answers and the model's reasoning as a separate muted
think block, tool cards, event narration, the sub-agent status line, the
context-usage bar, the agent's avatar, and keyboard-complete overlays for
settings, model, history, sessions and help.

The transcript is strictly chronological - newest at the bottom, always: a
reply streamed after a tool call appears below that tool card (anything
mounted below the live reply seals it; the next chunk opens a new one at the
bottom). Answers render as markdown - headings, lists, emphasis and fenced
code with syntax highlighting - not as raw `**stars**`. Streaming chunks are
coalesced at 100 ms rather than re-rendered per token: the parser reparses only
the tail, so the cost stays flat as the answer grows. The view follows a
growing answer, but only while the reader is already at the newest line -
scroll up to read something and the transcript leaves you there until you come
back down. Following is the transcript's own anchored state, evaluated inside
the layout pass, not a scroll position recomputed after each growth: widget
heights are set after the mount, so a measurement taken between the two reads a
transcript that grew on its own as a reader who scrolled away. The reasoning
block is
deliberately NOT markdown; it is plain muted text, because reasoning is not
authored as markup. The avatar is the agent's bracket body with the animated eye,
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

The bottom row carries the key hints on the left and the live context usage on
the right, and the two do not fit together below roughly 120 columns. Rather
than let the row clip mid-label, the strip does the arithmetic itself: the
context bar drops its token counts first, then its caption and half its cells,
and only when that is not enough do whole hint pairs drop from the right. What
is shown is always shown whole.

## Architecture: a client over the framework seams

The app is an in-process client of the same seams the web UI uses - there is no
second turn implementation:

- **Module map** (`vaf/cli/tui_app/`): `theme_bridge.py` converts the vaf theme
  catalog (`vaf.cli.themes.THEMES`, single source of truth) into Textual
  themes - the default theme is monochrome, a black-to-white ramp so the chrome
  never competes with the content, with only the three semantic colours
  (success, warning, error) kept as desaturated hues because telling a gate
  warning from an error is the one job colour still has. Both terminal lanes
  read that catalog, so the look is the same in either. The active theme is the
  `theme` config key - and only the Settings > Theme row writes it. `t` and
  `theme <name>` change the look for THIS SESSION, exactly as the classic
  lane's `theme` command always did: browsing must not choose. (It used to
  persist on every press, and walking the list once therefore ended with the
  LAST catalog entry as the startup default - matrix, which reads as a plain
  green terminal; the next start then looked like the VAF theme was gone. The
  same person hit that twice before this line existed.) A stored theme that no
  longer exists falls back to the default rather than stranding the user.
  That key is also
  what answers *which* theme is current, everywhere: `ThemeManager.current()`
  resolves it on first read and then holds it for the process, so the settings
  marker, the `vaf-settings` menu and the colours on screen cannot disagree.
  `ThemeManager.set_theme()` overrides that cache for one session without
  writing to disk, which is exactly what the classic lane's `theme <name>`
  command does. Every theme in the
  catalog is dark by contract (pinned by a test): the agent's mark and several
  accents are white, so a light background would make them invisible. `widgets.py` holds transcript and chrome widgets; `screens.py` the
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

Enter sends, Ctrl+J inserts a newline. Commands come from one shared registry
(`vaf/cli/commands.py`) that every terminal lane reads, so the completer, the
palette, the help screen and the dispatcher can no longer disagree: `help`,
`settings`, `model`, `theme <name>`, `history`, `sessions`, `session <id>`
(plus `session list` and `session current` - the latter prints the FULL id,
which `vaf run --session <id>` needs and the panel can only truncate),
`tools`, `context`, `clear`, `undo`, `restore`, `export <file>` (markdown, or
json by extension; no argument is a usage line, never a surprise file),
`listen`, `halt`, `restart`, `exit` - each with its classic aliases (`s`,
`c`, `t`, `h`, `l`, `?`, `q`, `stop`/`quiet`/`stfu`, `reload`/`r`). They work
typed alone or with a slash, and arguments keep their case, so
`session AbC123` and `theme dark` do what they say.

A word from that list is never sent to the model, and an unknown `/command` is
reported inline with the closest match rather than becoming a message. A
sentence that merely starts with a command word (`clear the table for dinner`)
stays a message: only commands that declare arguments match with any.

Destructive commands (`clear`, `undo`, `restart`) ask first, in a modal that
does not block. `clear` mid-turn also DISCARDS the reply still being written:
its remaining chunks would otherwise mount a fresh bubble into the emptied
transcript, with no visible question above it and a history
`clear_conversation` deletes moments later. The bubble is simply never
mounted (the streaming path is untouched: `feed()` buffers, the flush is a
no-op while unmounted, no interval is armed), and a note says the reply was
discarded. `halt` runs on its own thread rather than the agent lane -
the lane is exactly what is busy producing the speech being stopped. Beyond
the explicit command, EVERY submitted input silences running speech before it
is parsed - the classic loop's unconditional barge-in: TTS is asynchronous
and routinely outlives the turn that produced it. That stop is quiet; only
`halt` narrates. `restart` execs only after the app has released the screen.

Ctrl+P (palette), Ctrl+S (sessions), F1 (help) and Ctrl+Q (quit) reach the same
places. Every overlay walks with arrow keys, activates with enter or space, and
closes with esc.

The prompt carries the affordances the classic one had, from the same code:

- **History** on the arrow keys, from `~/.vaf/history` - the SAME file the
  classic lane writes, read through the same parser, so the history does not
  split in two depending on how VAF was started. Up and down only walk it at
  the first and last line, so a multi-line draft still moves its cursor; the
  half-typed draft is put back when you walk past the newest entry.
- **An inline suggestion** from the learned corpus, accepted with Tab or Right.
- **A completion menu** for `/commands` (from the command registry, so it can
  only offer words that route) and `@paths` (folders first, with sizes). While
  it is open, Enter ACCEPTS rather than sends - press it again to send. Esc
  closes the menu and keeps the draft. A candidate that would add nothing is
  not offered, or the menu would swallow the Enter meant to submit.

`@path/to/file` still inlines a file into the message, as in the classic lane.

Ctrl+S opens the sessions panel: arrows walk it, enter loads the highlighted
session (through the complete context swap, so tool calls and images survive),
`n` creates a fresh one and switches to it, `r` renames the highlighted row
(a small input modal on top of the panel), `d` deletes it after a confirm -
the session you are IN is refused honestly, switch away first. `/session new`
and `/session rename <name>` do the same from the prompt. The panel shows the
engine's ONE surface list (`SessionManager.list_ui`) - the same filter the
web sidebar uses, so channel and thinking sessions stay in their dashboards
on both surfaces. Renames go through the engine primitive, which writes the
file and nothing else; the file is the name's source of truth, and the app
adopts the on-disk name before its exit save, so a rename made in the web
while the app was open survives the app closing. Esc closes the panel. A
session nobody actually wrote in is discarded rather than kept - on leaving
it and on quitting - using the same criterion `SessionManager.cleanup_empty`
applies (no message with role `user`), so starting the app and closing it
again does not grow the list. Reading the list happens off the UI thread. On
the way out, the plain terminal gets the session id and both ways back into
it - the classic lane printed the same thing, minus its blocking "save?"
question, since the app saves unconditionally.

The settings overlay is the `vaf settings` main menu as a stacked arrow menu.
What it can change is decided by where a key is READ, not by how it looks:

- **Written straight to the config** - boolean toggles, the TTS engine, the
  input language, the sub-agent timeout duration, the auto-open tab cap,
  auto-start of the local server. Every one of these is read live at its
  consumption site, so writing it IS the whole job.
- **Written as a PAIR** - the sub-agent provider. It used to be in the list
  above, and that sentence was the defect: `subagent_provider` names the choice
  while `subagent_use_separate_provider` gates it, so writing the name alone is
  silently inert. Both halves go through `config.set_subagent_provider()`, the
  row's marker follows `config.subagent_provider_override()` rather than the
  stored name, and a provider with no API key is refused here exactly as the
  inquirer menu refuses it.
- **Applied to the running agent** - the provider and the API model, through
  the engine's own `reload_all_api_backends`. That is the supported way to move
  a live agent: it re-reads the config, rebuilds the backend under a lock and
  re-attaches the event sink. The app never calls `init_chat()` afterwards -
  that would reset the history to the system message and wipe the conversation
  behind the transcript. A switch is refused while a turn is running, and if
  the running agent kept its backend anyway the app says so instead of claiming
  success. It is also refused when the target provider has no API key, and the
  refusal lives on the bridge rather than in the overlay because three routes
  reach the same method (the model overlay, the settings row that dismisses
  into it, and `/model`). The overlay asks for the key instead of only
  refusing: picking a provider with no key opens a masked field, and `k` on a
  provider row opens it for one that already has a key, which is the only way a
  wrong or expired key can be replaced. The field's three answers are distinct
  on purpose - a typed key, empty for "keep the stored one", escape for "change
  nothing". A typed key is stored and then verified with one real request
  before the provider moves; a key that fails to verify is kept (the request
  can fail on the network as easily as on the key) while the provider stays
  put. The verification runs on the agent lane, never on the UI thread, and the
  key value never reaches a note, an event or the transcript.
- **Still pointing at `vaf settings`** - the model download (a long
  cancellable network operation mid-app needs a background-work concept the
  TUI does not have yet). The local model LEFT this list: the "Select Active
  Model" submenu lists `models/*.gguf` with the active file marked, and a pick
  swaps the running agent live through the engine's `reload_local_model` -
  model-aware server swap, parser identity recomputed, conversation kept. The
  swap runs on the agent lane (the new weights block while they load) and
  refuses during a running turn; with a cloud provider only the config moves,
  as in the classic menu. (The context limit and the microphone left earlier:
  writing `n_ctx` is what the classic menu does and the row says when it
  applies; the microphone submenu enumerates real devices once per entry
  behind an fd-2 guard. The Automations submenu carries the classic table -
  enabled mark, schedule, next run - one activation flips enabled, and the
  storage folder opens from its own row; reads are cached per entry like the
  microphone list. "Show All Tools" pushes the existing tool catalog on top
  of the settings modal.)

## Named boundaries (this round)

- The app lane does not start the web dashboard; `vaf run --web` therefore
  keeps the modern lane, which owns the web-input watcher and the result
  notifier. That lane hosts backend and frontend ITSELF and no longer
  auto-starts the tray as a detached background service - the dashboard ends
  with the session, which is what README promises for this flag; the
  persistent service is `vaf tray`. The boundary is now SAID at startup rather than only in `--help`:
  the switch changes the whole interface, and a user who did not ask for that
  cannot otherwise tell a boundary from a broken app. The line appears only
  when the lane actually changes, so `tui_mode: modern` stays quiet. (The
  heartbeat is NOT part of that wiring and runs on every interactive lane: it
  is the only signal the tray has that a CLI session is alive, and without it
  the tray unloads the local model mid-session.)
  The transcript-replay half of that boundary is closed: a session switch
  clears the transcript and repaints the loaded conversation (newest
  `REPLAY_CAP` messages, an honest note names what was trimmed and points at
  `/export`), and a resumed session boots with its conversation under the
  start banner instead of an empty screen. Replayed bubbles carry the
  persisted timestamp's HH:MM, not "now", and replayed agent messages are
  STATIC - complete content at construction, no flush ticker, because
  feed/done against a just-scheduled mount races on_mount and would leave
  the 100 ms interval running forever (the avatar-leak class). What still
  blocks the `__CMD__` session commands is only their producer wiring.
- The in-process TaskQueue IS consumed now, once a second, and a fired timer
  arrives as an amber wake card followed by a real turn. There is exactly one
  producer in this process: the timer scheduler. The `__CMD__` session commands
  are deliberately not handled - all four of their producers live in the web
  server, and an explicit `vaf run --web` routes to the modern lane, so no such
  task can reach this process. That branch lands when a producer for it does.
  A task belonging to a DIFFERENT session is dropped with a note rather than
  swapping the transcript underneath the reader, and it is gone rather than
  deferred: the scheduler removed it from its in-memory store before firing.
- Two things a timer turn can do that the app cannot yet answer, both
  pre-existing and both worth knowing: a gated tool pushes the confirmation
  screen over whatever you are doing, and this lane has no way to stop a turn
  in flight. Quitting while an unattended turn runs gives the finalizer a five
  second budget to save the session, which was previously only reachable right
  after you had submitted something yourself.
- Typing while a timer turn is running is allowed and your draft survives; the
  message is queued and a note says so. Pressing enter mid-turn seals the live
  reply and the remainder opens a new bubble below your message - the split is
  explained by that note rather than removed, because removing it would mean
  buffering a live stream around every user mount in this lane.
- The classic lane's speech preloads (Piper check + voice model, the STT
  microphone check with an honest "pyaudio is not installed", the langid
  warmup) run in the BOOT phase now, where the terminal is still plain and
  their output is readable.
- Voice input works: `l` opens the recording overlay, whose meter renders the
  REAL capture state - the framework's `listen()` grew an `on_state` callback
  (data instead of painting; its raw-stdout meter would shred the alternate
  screen) and a cooperative `should_stop`, which is how escape cancels the
  CAPTURE rather than closing a view over a live microphone. The capture runs
  on its own thread ("listening works any time"), and the transcript takes the
  SAME send path a typed message does, so the turn shows your words before the
  answer. With `ux_voice_review` on (Settings, Voice, "review before send")
  the transcript lands in the input box instead: read what the transcription
  heard, fix it, enter sends. The classic painted meter stays for the plain
  lanes - minus the markup tags it had been printing literally to a raw fd
  since it was written.
- Boot (model load, warmup) runs in the plain terminal BEFORE the app takes
  the screen: llama.cpp writes C-level stderr that would corrupt app mode.
  The git preflight runs there too, for the same reason - an install prompt
  only works while the terminal is still plain. Boot also starts the Docker
  service stack in the background (same engine primitive as the tray,
  `vaf/core/service_stack.py`) - the tray stops the stack when it quits, so
  a terminal-only session used to run against a dead memory database.
  Without Docker or the compose file the start is skipped honestly.
- A turn that fails writes its traceback to the dated crash log
  (`crash_YYYY-MM-DD.log`, see [DEBUGGING.md](../DEBUGGING.md)); only the path
  appears in the transcript, because a printed traceback would land under the
  alternate screen.
