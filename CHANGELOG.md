# Changelog

All notable changes to VAF are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and VAF aims to follow
[Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`, with PEP 440
prerelease suffixes such as `a0` / `b1` / `rc1`).

Each released version has a matching git tag `v<version>` and a GitHub Release.
To update an installed VAF, run `vaf update` (on Windows, from the install folder:
`run_vaf.bat update`).

## [Unreleased]

### Fixed
- **Host-speaker TTS is now opt-in per agent (fail-closed).** With TTS enabled, every
  background turn (web/Telegram/WhatsApp/Discord queue, automations, proactive thinking
  runs, `vaf run -p`, the gateway) used to synthesize and play the answer, a thinking
  filler, and the answer chime on the server machine's speakers, where nobody is
  listening. Agents now carry a `host_audio` construction flag; only the interactive
  CLI sets it. Browser TTS (Read Aloud, auto-speak) is a separate lane and is
  unchanged.

### Added
- **Cloud voice providers: ElevenLabs and OpenAI for speech output and speech input.**
  Settings > Voice gains an admin-only Voice provider section: the TTS and STT
  providers are selectable independently (Local Docker remains the default), with
  per-provider voice and model fields and a new admin-only, read-redacted
  `api_key_elevenlabs` (the OpenAI lane reuses `api_key_openai`). The provider lane
  never breaks a turn: quota, rate-limit and network errors degrade to the local
  engine. The WebSocket audio contract is unchanged (clients still receive WAV),
  Telegram/WhatsApp voice notes honor the provider selection (ElevenLabs answers
  voice replies natively as OGG/Opus), the CLI microphone uses the selected STT
  provider instead of Google's free Web Speech API when one is configured, and the
  local speech containers are only required for the local lane. All speech HTTP
  now goes through a shared client (`vaf/core/speech_client.py`, CI-guarded), and
  the non-admin write hole on the global `stt_enabled` toggle is closed.
  The ElevenLabs model and voice pickers are populated live via an admin-only
  backend proxy (`/api/voice/elevenlabs/*`; the key stays server-side, responses
  cached, hardcoded fallback when unreachable). OpenAI catalogs are current as of
  2026-07: 13 TTS voices (`ballad`, `verse`, `marin`, `cedar` on `gpt-4o-mini-tts`
  only), input capped at 4096 characters, and `verbose_json` language detection
  restricted to `whisper-1`.
- **New tool: schedule_reminder - persistent one-shot reminders without an agent run.**
  The daily calendar check was designed to create one-off reminder automations, but
  create_automation is deliberately stripped from automation runs (runaway guard) -
  the agent silently fell back to set_timer, which is in-memory only and anchored to
  a session via the process-global fallback: reminders from background runs died on
  restart or landed in the wrong chat. A reminder is now stored DATA: the scheduler
  delivers the stored message verbatim at fire_at on the user's main messenger (Web
  UI notification fallback), with no agent run and no tools - which is why the narrow
  lane is safe where create_automation is not. Per-user scoped, bounded (pending cap,
  14-day horizon, 6-hour delivery grace after downtime with honest missed
  notifications), cancellable, excluded from thinking runs (propose-only). The
  calendar-check prompt (default and the existing stored automation) now teaches
  schedule_reminder; the calendar doc no longer claims create_automation is allowed
  inside the run.
- **New tool: send_to_user - channel-agnostic delivery to the user's main messenger.**
  Workflows, automations and the agent previously had to pick a platform tool
  (send_telegram, send_discord, ...) themselves, which froze the platform into stored
  automation definitions and produced wrong deliveries for non-Telegram users. The new
  tool wraps the one canonical router (send_to_main_messenger): the platform is
  resolved at RUN time from the user's main_messenger, a produced file is attached
  best-effort, and when no messenger is reachable the content falls back to a Web UI
  notification instead of being dropped. Switching main_messenger now retargets every
  existing automation automatically. Per-channel send tools remain for explicit
  requests ("send it via Telegram"). The tool joins every send-tool registry copy
  (thinking-mode strip set, agent/engine scope injection, router pinning, workflow
  project-path resolution for file_path) and stays out of the front-office allow-list
  by design. The channel model (rule vs adapter, extension checklist) is documented in
  docs/integrations/CONNECTIONS.md.
- **New built-in workflow: YouTube Summary.** Summarizes a YouTube video from its own
  captions: yt-dlp runs inside the Docker sandbox (installed per run - no host
  installs, no confirmation cascade) and fetches the caption track via ONE metadata
  call plus the signed caption URL (json3) - per-language subtitle file downloads
  turned out to be far more rate-limited and burned a live run in 429s while the
  captions existed; the robust method was discovered by the coder sub-agent
  improvising after that failure and is now the workflow's own. A validated
  generation step writes the Markdown summary into the chat workspace and is
  explicitly forbidden from fetching content itself (an agentic coder otherwise
  spends minutes re-hunting the transcript on a failure marker). Videos without
  captions (or a momentary rate limit) produce an honest note instead of an invented
  summary. Composed from a live session where the agent built this lane ad-hoc over
  confirmed host commands.
- **analyze_image can inspect images from the chat workspace (`image_path`).** The vision
  tool only accepted user attachments, so an agent that had just produced a chart could
  not quality-check it and spiraled through header-parsing and OCR detours instead
  (observed live). It now also takes a path to an image file inside the chat's own
  workspace - and only there: paths outside the workspace are refused, so the vision
  model can never be used to describe foreign files.
- **python_sandbox can deliver the files it produces (`export_files`).** Binary
  artifacts had no scalable path out of the sandbox: the base64-through-context detour
  truncated anything beyond the model's output budget (a ~400KB chart arrived as 2.5KB
  of corrupt PNG). Code can now write files to relative paths and declare them in
  `export_files`; they are copied out of the container into the chat workspace after a
  successful run (before the scratch dir is removed), show up in the UI file browser,
  and never pass through the model's context. Only sandbox scratch paths can be named;
  the destination is always the chat's own workspace. Works with both the persistent
  and the ephemeral sandbox container.
- **The main agent can now save files directly with `write_file`.** Saving a single
  finished artifact (an SVG, an HTML page, a text file) previously required guessing
  between unrelated tools, and the sandbox's own guidance pointed at `write_file` - a
  tool the main agent did not have (it was sub-agent-only), so following the
  instruction produced "Unknown tool". `write_file` is now registered to the main
  agent: relative paths land in the current chat's workspace, explicit absolute paths
  are honored (VAF's own directory and system locations stay protected), non-admin
  (remote) users are jailed to their own `VAF_Projects` area, and the Web UI file
  notifications are attributed to the calling chat session. Background thinking runs
  (propose-only) deliberately do not get the tool, and the write no longer triggers a
  confirmation prompt (the plan gate still applies, consistent with document_writer).

### Changed
- **The agent can now author full-power workflows itself.** create_agent_workflow's
  engine and save path always supported multi-parameter steps, but the schema the
  model sees never advertised them - an agent-created workflow could not express a
  sandbox step with pip packages or exported artifacts. The step schema now documents
  `args` (with python_sandbox packages/export_files and write_file examples), inline
  `{variable|fallback}` defaults for saved workflows, and the brace-safety rule for
  embedded Python code (a brace block containing a dot is a variable lookup and breaks
  the run). The validation guidance also no longer reads as run_temp-only: the
  validate flag works in saved workflows too, and the builder now tells the agent to
  flag deliverable steps in create mode as well.

### Fixed
- **The last platform-hardwired prompt surfaces are channel-agnostic.** The librarian's
  found-one-file hint said "To send via Telegram" regardless of the user's messenger,
  the channel-capabilities prompt picked its send tool via a hardcoded ternary that
  defaulted to send_telegram (CLI sessions now get send_to_user), the ask-once guidance
  now teaches send_to_user for delivery, and the delegation send-success heuristic no
  longer contains bare platform names - which also fixes a latent false positive
  ("Failed to send Telegram message" counted as a successful send). The front-office
  owner-notification mapping gains its missing slack line.
- **The dead 'email' main_messenger value is gone.** update_user_identity accepted
  main_messenger="email", but the identity store heals it to None on every read and the
  delivery router never dispatches e-mail - the value could be stored yet never worked.
  Removed from the tool enum, both validators and the front-office mapping; the channel
  registry drift guard now rejects any value outside KNOWN_CHANNELS.
- **send_discord can attach documents.** The core Discord sender supported file uploads
  all along; only the tool schema hid them, so agents fell back to other channels for
  files. send_discord now accepts file_path with the same path validation and result
  phrasing as send_telegram.
- **Automations no longer get registered twice in the scheduler.** The create_automation
  tool auto-started the scheduler on its own manager instance (whose running-flag was
  False even while the process scheduler ran); since the schedule registry is
  module-global, every job was registered a second time and a second loop thread
  started - each task then triggered twice on every firing, with only the run lock
  preventing double execution. The tool now goes through the process-wide
  ensure_scheduler_started helper, and start_scheduler itself refuses to run on a
  non-singleton manager instance (defense in depth).
- **Background-run identity no longer leaks between concurrent runs.** ask_user's
  automation-handoff branch, thinking/automation tool registration and dispatch
  injection gated on process-wide env vars (VAF_IN_AUTOMATION / VAF_THINKING_MODE),
  which are shared across threads: while a scheduled automation was running, a
  concurrent thinking run's question was misrouted into an automation handoff
  bundle (three occurrences in the 07:00 window), which later steered a user reply
  into unintended actions. Agents now carry a per-instance run kind stamped at
  construction (thinking / automation / chat); env remains only a fallback for
  embedders. Handoff bundles are additionally data-minimized: text-only capped
  snapshots, and resolved bundles drop their history entirely.
- **Replies to background questions no longer trigger unconditional task continuation.**
  The reply-pickup note asserted "CONTINUE the task now" whenever a handoff bundle was
  linked, without validating the bundle and without a decline or ambiguity lane - a
  mislabeled, finding-less bundle framed a plain "nein bitte nicht" as an automation
  continuation and the agent mutated an automation and attempted file deletion.
  Pickup now validates the bundle (automation source + curated findings), degrades to
  a plain-question note otherwise, and every lane is reply-conditional: clear
  agreement continues, a decline changes nothing, an ambiguous reply gets exactly one
  confirming question before any action. Each pickup writes a [REPLY_CTX] audit line.
- **Two confirmation gates stop unconfirmed actions around background questions.**
  (a) While a turn handles the user's reply to a tracked background question, stored-state
  mutations (automation create/update/delete, workflow/tool builders) and destructive
  sub-agent delegation are blocked with a confirm-style result unless the reply is a
  clear affirmative - the agent acknowledges and asks one confirming question instead
  (live incident: a misread "nein bitte nicht" mutated an automation and delegated a
  file deletion). (c) Once the agent's own reply asked the user a blocking question,
  background drain turns deliver results but cannot launch new write-level tools or
  delegations until the user answers; the drain's retry instruction becomes a status
  report meanwhile (live incident: deletion was re-delegated twice AFTER the agent had
  asked "Soll ich die Datei jetzt direkt loeschen?"). Kill-switches:
  proactive_reply_mutation_gate_enabled, ask_first_drain_gate_enabled.
- **Sub-agent result summaries can no longer leak chain-of-thought to messengers.**
  The result drain hand-copied a shorter sanitizer chain than the normal reply path
  and built its text from the raw stream buffer - 1034 characters of untagged English
  deliberation reached the user on Telegram. All messenger sends (normal headless path
  and drain) now share one sanitizer chain including a conservative, language-agnostic
  guard against untagged chain-of-thought prefixes; the drain summary is based on the
  reasoning-stripped chat_step return value, and an empty-after-sanitize summary falls
  back to a deterministic localized result excerpt instead of a noise placeholder.
- **The librarian refuses deletion tasks honestly instead of answering with folder
  statistics.** The librarian has no delete capability, but its filesystem-map fast
  path keyword-matched 'document' inside a task's PATH ('/home/.../Documents/...')
  and answered four delete/verify tasks with canned Documents statistics - neither
  doing nor refusing anything, which fueled the caller's retries. Destructive tasks
  (destructive verb governing a file/folder/path target, DE+EN, per sentence) are now
  refused before any fast path with an explicit capability statement; the map's quick
  answers match intent words with word boundaries after stripping paths and filenames
  ('mov' no longer matches 'remove', 'doc' no longer matches 'docker'), and the tool
  description tells the delegating agent up front that deletion is impossible.
- **Automation timeouts no longer deliver half results twice.** The prompt-run bound
  (previously 180s - unrealistic for real tasks) ignored the timeout sentinel: the
  half-streamed text became the "result", was wrapped into a junk output file and
  pushed, while the abandoned worker finished minutes later and delivered again
  (observed twice live: double message, double attachment). The default is now 600s,
  the sentinel is evaluated, and on timeout the runner waits a bounded grace window
  for the abandoned worker - both live cases would have recovered into one normal,
  complete delivery. Only past the grace does the user get one honest timeout note:
  no partial result, no file wrap, status error.
- **Generated automations no longer message the user raw tool output.** The automation
  workflow generator wrote send steps like "here is the data: {search_results} - please
  summarize" - but send steps are deterministic and deliver their arguments verbatim,
  so the user received a raw search-result dump with a dangling instruction, and the
  HTML report the same automation produced was never attached. The generator now
  teaches the channel-agnostic delivery step (send_to_user incl. file_path attachment),
  that send/write steps are verbatim (produce the final text in a CONTENT_ONLY step
  first), and that a platform tool must never be hardwired; its canonical example
  summarizes before sending and attaches the produced file. The calendar-check prompt
  teaches the same delivery step instead of enumerating platform tools.
- **Automation results are no longer delivered twice, and the Web UI no longer shows
  tool chatter as a saved file.** When an automation already delivered in-run via a
  send tool, the post-run pipeline additionally pushed the run summary to the
  messenger (two messages per run); it now skips the messenger push on a confirmed
  in-run delivery, in BOTH lanes: workflow-based (send step result) and prompt-based
  (send-tool success in the agent history - live incident: the daily calendar check
  messaged the user twice). The history check also recognizes the end-of-turn squash
  form: chat_step consolidates tool results into one "[Context: tools used this turn]"
  system note, which is the only shape left by the time the post-run delivery decision
  runs (live: a real send was missed and the user got the push on top). Detection is
  conservative: a failed or unclear send keeps the push (a duplicate beats a lost
  message). The "saved file" line in the Web UI
  result showed the raw last-step result string (live: "Gespeichert: Message sent to
  the user via Telegram.") - it now appears only when the last step's output actually
  is a file on disk.
- **Router-delivered messenger messages now leave a trace in the channel session.**
  The per-platform send tools record their own sends, but the canonical router path
  (automation result push, send_to_user) delivered without writing to the channel
  session history or message store - so when the user replied to such a message, the
  channel main agent had never seen it and confabulated (live incident: the agent
  could not know which "Timer" the user meant). Successful router sends are now
  mirrored into the channel session (and, where the bridge does not record outbound
  itself, the channel message store). Thinking-mode deliveries opt out: tracked
  requests are reconstructed scope-keyed at reply time and would appear twice.
- **Workflow runs open the Workflow Runtime panel again in TLS setups.** The @workflow
  subprocess posted its UI events (workflow_start/update/done, terminal lines) to a
  hardcoded plain-HTTP 127.0.0.1:8001 - with local_network_tls_enabled that port speaks
  HTTPS, every event died silently, and the frontend (never learning a workflow was
  running) showed the generic SubAgent window instead of the Workflow Runtime panel.
  Subprocess senders now resolve the backend through a shared TLS-aware helper
  (internal plain-HTTP port 8005 when TLS is on); the vaf-run terminal's
  heartbeat/health probes had the same blindness and use it too.
- **@workflow runs no longer fail with "Tool not found" for sandbox steps.** The
  @workflow CLI subprocess, the in-chat executor and the run_temp overlay each
  hand-maintained their own copy of the workflow tool set, and the copies had drifted -
  the subprocess lacked python_sandbox entirely, so a template using it failed its
  first step. All runners now build from one shared list, and a test enforces that
  every tool named by any built-in template is constructible headless. The workflow
  variable extractor also no longer mistakes URLs for file paths (a YouTube link
  became the output filename "//www.youtube.com").
- **A chat's system prompt can no longer advertise another chat's workspace.** With
  parallel main workers, the "this chat's workspace" line and the document writer's
  output folder were resolved through a process-global session pointer that belongs to
  whichever chat touched it last - a fresh chat was told its workspace is the previous
  chat's folder and dutifully saved its deliverable there. Session-derived paths now
  always key on the chat's own session id. The session-workspace anchor
  (session.project_path) is also written by one shared setter on BOTH notification
  paths - previously only files from subprocess sub-agents anchored it, so chats whose
  files were written in-process never got the workspace context note - and the runner
  derives the workspace deterministically when the anchor is missing but the folder
  exists on disk.
- **Deliverables are steered into the chat workspace instead of scattering across the
  filesystem.** A finished artifact could end up in the VAF_Projects root, where the UI
  file browser (the only file access remote/LAN clients have) never shows it. The
  session-workspace context now states that final outputs belong in the workspace,
  write_file flags successful writes that land outside it in the same turn, the coder
  is taught the binary lane (render in the sandbox, save via content_base64) instead of
  writing script source into image-named files, and the built-in "Research & Code"
  workflow declares that it produces text code and cannot emit binary files.
- **The machine owner is no longer locked out of write_file.** The per-user write jail
  treated only an EMPTY user scope as admin, but a logged-in owner session carries the
  admin's real UUID - the owner got "Access denied: outside your own data" on their own
  VAF_Projects folder (observed live). Admin detection now mirrors the librarian jail:
  no scope OR the configured local_admin_scope_id means full access.
- **write_file can now save binary files.** Rendering an image had no supported path:
  write_file only took text, so a sandbox-rendered PNG had to detour through confirmed
  host shell commands (including a host pip install). write_file now accepts
  content_base64 for binary data - render in python_sandbox, print the file as base64,
  save it with write_file; the sandbox's persistence guard message documents the lane.
- **Tool argument errors no longer misreport enum violations as type errors.** A valid
  string that violated an enum (e.g. a task status) was reported as "expects string,
  got str", which a model cannot act on; non-type failures now surface jsonschema's own
  message ("'x' is not one of [...]"), and the reactive know-how lane also recognizes
  "[ERROR]"/"Access denied" shaped failures.
- **Sub-agent failures now carry the failed tool's learned know-how.** When a delegated
  sub-agent (coder, research, document, browser) failed, the error arrived later via the
  result drain as a bare message - the reactive know-how lane never fired because the
  tool call itself had only returned a "task delegated" marker. Both drains (chat/runner
  and the `vaf run` terminal) now attach the tool's learned pitfalls and procedure to the
  failure message, include the original task for context, and feed novel errors into
  background re-learning. The pitfall matcher also strips filesystem paths before
  matching, so path-heavy errors ("File exists: /long/path/...") can match stored
  pitfalls.
- **Learned tool know-how no longer rots silently when it fails the quality gate.** The
  Whare Wananga delivery gate (confirmed + challenge passed + actually probed) silenced
  18 of 67 learned records completely - including ones whose stored pitfalls held exactly
  the knowledge that would have prevented a live failure. Two changes: on the reactive
  lane (a tool call just failed) gate-failing records are now delivered too, clearly
  tagged "UNVERIFIED" (the proactive schema injection stays strictly gated), and every
  gate reject lands in a persistent re-training queue instead of being dropped - shown
  and drained via `vaf ww queue [--scan]` and `vaf ww retrain --pending` (3 attempts per
  tool, 24h cooldown), or automatically by the opt-in eager training worker.
- **document_writer no longer silently accepts non-document filenames.** The tool
  declared .txt/.md/.docx but wrote ANY extension as a rendered "text" document - a raw
  .svg happened to survive, an .html request came out as a text rendering of the input
  instead of HTML. Filenames outside .txt/.md/.docx are now rejected with a redirect to
  the right tool (write_file for raw files, coding_agent for code projects), a missing
  extension is derived from the format parameter (previously format="word" with a bare
  name, or with report.txt, wrote Word bytes into a .txt file), and failures return a
  "Tool Error:" prefix so workflows score them as failed steps instead of successes.
- **The coding agent no longer treats a target FILE path as its project directory.** A task
  like "save it as /path/chart.html" made the coder use the full file path as the project
  folder: the run crashed with "File exists" when the file was already there, and otherwise
  created a DIRECTORY named `chart.html` with the real file nested inside it. File-shaped
  paths (existing files, or unknown paths with a known file extension) are now split into
  project directory + target filename; the filename is passed to the model as the explicit
  deliverable, the safety guard judges the directory part, and a blocked project directory
  now returns an actionable error instead of a crash. The path extraction also keeps file
  extensions intact (previously truncated after "path:"/"in directory" phrases and in
  Windows paths) and no longer swallows closing quotes around quoted paths.
- **The installer no longer fails on a too-new Python (e.g. 3.14).** Both installers accepted
  any Python at or above 3.10, so a machine whose newest Python was 3.14 built the venv with an
  unsupported interpreter and the dependency install crashed while compiling packages that have
  no prebuilt wheels for it yet. The installers now accept only the CI-tested range (3.10-3.13),
  automatically provision a supported Python via uv when the system one is outside that range,
  and recreate an existing venv that was built with an unsupported Python. The Windows installer
  also reports wheel-build failures honestly (unsupported Python / missing prebuilt wheel)
  instead of blaming a "network hiccup".

### Changed
- **search_tools now returns call signatures for the top matches.** Discovering a tool
  by keyword only returned its name and one description line, so the model had to
  guess parameter names on the first call (observed live: an invented argument name
  producing a schema error). The top three matches now include a compact signature
  (required parameters first, optional ones bracketed); the output stays within the
  tool-result budget and the discovery post-hook keeps working unchanged.
- **Voice input (pyaudio) is now an optional extra instead of a core dependency.** pyaudio ships
  no prebuilt wheels for brand-new Python versions and its source build needs the PortAudio C
  headers, which could break the whole installation. It moved out of `requirements.txt` into the
  existing optional `vaf[speech]` extra; CLI microphone input degrades gracefully without it and
  web/desktop microphone capture is unaffected. Install it with `pip install pyaudio` (or
  `pip install "vaf[speech]"`) if you use CLI voice input.

## [0.1.0a10] - 2026-07-09

### Security
- **RAG snippets no longer leak between users on the local network.** In multi-user mode the
  memory-search snippets shown in the chat "RAG-Snippets" panel were pushed to the browser via a
  global WebSocket broadcast to every connected client, so one user's snippets - including those
  from a background thinking or automation run under another user's scope - could appear in a
  different logged-in user's panel. Retrieval itself was always correctly scoped per user; only the
  UI push was global. The push is now routed to the owning user's connections only and dropped when
  the scope is unknown (fail-closed); the same fix applies to the context X-ray payload
  (`real_context_payload`) and the memory-learning status banner (now scoped to the session), and
  the UI clears the snippet panel on session switch. Requires a restart.

## [0.1.0a9] - 2026-07-08

### Added
- **Choose light or dark mode during first-run setup.** A new step right after the language
  picker lets you pick Light or Dark; the choice applies live (onboarding switches immediately)
  and carries into the app. Light stays the default.

### Changed
- **The WhatsApp connection is temporarily marked "Coming Soon".** In Settings > Connections it
  now shows greyed out with a disabled, non-clickable card, like the other not-yet-available
  integrations. This is a UI gate only (the backend is unchanged) and is easily reverted.

### Fixed
- **The browser tool no longer crashes on startup with Chromium 150+.** With the Debian bookworm
  `chromium 150.0.7871.46` build, the browser container died about a second into launch (SIGTRAP)
  whenever the profile resolved to an EEA region, so every browser task failed with "Chrome
  DevTools at http://localhost:9222 did not respond" (Debian bug #1141618; `149` was fine, `150`
  regressed). The container now launches Chromium without `--no-first-run` (the specific trigger)
  and keeps the first-run search-engine choice quiet with `--disable-search-engine-choice-screen`
  and `--search-engine-choice-country=US`; it also supervises Chromium (relaunches it if it exits,
  reaps orphaned child processes, and serves the CDP proxy only while the browser is live) so a
  one-off crash self-heals in seconds instead of leaving the tool permanently unreachable. Apply
  with a browser image rebuild: `docker compose -f docker-compose.memory.yml up -d --build vaf-browser`.
- **Dark-mode buttons stay readable on hover.** Emphasis buttons (e.g. "Save Changes",
  "Connect") turned dark on hover in dark mode while their text stayed dark, making the label
  unreadable; they now brighten slightly on hover so the text stays readable. Applied
  consistently across the whole UI.

## [0.1.0a8] - 2026-07-06

### Fixed
- **`vaf update` now works from any terminal.** The updater was reachable only through a
  shell alias (Linux/macOS, active only in a freshly-sourced interactive shell) and had
  no `vaf` command at all on Windows — so `vaf update` reported "command not found" and
  users could not self-update. The installer now registers a real `vaf` command:
  `~/.local/bin/vaf` on Linux/macOS (on PATH, works in every shell) and a shipped
  `vaf.bat` added to the user PATH on Windows. Until the installer is re-run, the
  always-available fallback is the shipped run script — `run_vaf.bat update` on Windows,
  `./run_vaf.sh update` on Linux/macOS — and the in-app "update available" hint now shows
  the platform-correct command.
- **`vaf update` self-heals a non-git install.** An install created from a downloaded ZIP
  (no `.git`) previously failed with "not a git checkout; re-install from git" and could
  never update. `vaf update` now offers to convert such a folder into a git checkout of the
  official repo in place (git init + origin remote, then adopt the release with
  `git reset --hard`) and continues the normal update. Your settings (`~/.vaf`) and build
  artifacts (venv, `web/.next`, `node_modules`) are left untouched — only tracked source is
  reset to the release. After that, future updates work normally.
- **`vaf update` finds VAF's own git when git is not on PATH.** The Windows installer downloads
  portable MinGit but did not persist it to PATH, so `vaf update` (and any git operation) failed
  with "Git is not installed." on machines without system git — even though a usable git had just
  been fetched. Git operations now resolve VAF's bundled MinGit as a fallback, and the bootstrap
  installer also persists it on the user PATH, so neither VAF nor the user needs a separate git
  install.
- **A harmless startup error about the `run_tests` tool is gone.** The main agent tried to
  instantiate a coder-only tool that needs a project directory, printing
  `Failed to instantiate tool run_tests` on every start (the agent continued fine); it is now
  correctly marked coder-only and no longer logs the error.


## [0.1.0a7] - 2026-07-06

### Added
- **Dark mode.** A neutral `#181818` dark theme for the whole web UI, toggled under
  Settings → Interface → Appearance (default light; stored per-browser). It uses a
  folding Tailwind palette swap so light mode stays byte-identical, with a consistent
  light-neutral for active/emphasis controls (no blue or amber accent) and status
  colors kept semantic. The exact per-theme colors of every surface, control and the
  agent avatar are documented in `docs/web-ui/LIGHTMODE.md` and
  `docs/web-ui/DARKMODE.md`.
- **The coder window shows what the agent is doing, live.** The VS-Code-style sub-agent window
  renders a red/green diff of the file being edited directly in the code pane — based on a
  run-start snapshot, so a previous run's changes are not shown — auto-scrolls to the change, and
  mirrors files into the editor as the agent reads them, so orientation, review, and documentation
  phases are visibly active instead of looking stuck. A phase indicator (Planning / Building /
  Finalizing) with a live spinner keeps file-less phases clearly ongoing.
- **A multi-tab coder editor.** A persistent "Live" tab always streams what the agent is doing;
  clicking a file in the Explorer opens it in its own closable tab, so browsing a file no longer
  hides the live view.
- **The coding agent can search the codebase while building,** not only while planning, so it can
  locate existing code before changing it.
- **HTML deliverables open as a rendered preview.** Clicking an `.html` file in a sub-agent window
  opens it in the HTML viewer instead of showing raw source.
- **The Windows installer checks hardware virtualization first — before any WSL2/container
  work.** It verifies that a hypervisor is running or Intel VT-x / AMD-V is enabled in the
  firmware (no admin rights needed for the check) and stops with clear BIOS/UEFI instructions
  when virtualization is disabled, instead of failing minutes later with the cryptic WSL error
  0x80370102. Windows Home is fully supported — only the hypervisor platform is required, not
  the Hyper-V role.

### Fixed
- **The coding agent no longer crashes on cloud providers mid-run.** A malformed message history —
  a status nudge inserted between an assistant's tool calls and their results — made strict
  providers (DeepSeek, OpenAI) reject the request with `400 "insufficient tool messages following
  tool_calls"`. The history is now normalized before every request so tool results always
  immediately follow their tool call, for all providers.
- **A plan whose items the model sends as objects no longer crashes the coder.** Task titles are
  coerced to plain text at the data-model boundary (the description is extracted from
  `{"text": ...}` / `{"task": ...}` shapes), covering both a fresh `set_todos` call and
  loading or resuming a previously-persisted plan — and self-healing an already-affected
  `tasks.json`. A raw object title otherwise crashed downstream `title[:N]` or `title.lower()`
  (on Python 3.12+, `object[:50]` raises `KeyError: slice(None, 50, None)`).
- **The coding agent is given time to finish a long edit** instead of being cut off by a fixed
  timeout; it runs until genuinely idle.
- **The coder edits the intended file surgically:** `edit_file` and `write_file` are chosen by
  intent, and an oversized whole-file "edit" is rescued into a full write instead of failing.
- **The coder console follows the tail reliably** — the live output no longer freezes after a pause.
- **A new coder request plans from scratch** instead of resuming a leftover task list from a
  previous request.
- **The workspace viewer stays on the workspace you opened,** not the active chat.
- **A file the agent "saved" no longer silently vanishes.** When the agent used `python_sandbox`
  to write a file to your workspace, the write went to the sandbox's isolated Docker filesystem
  and was discarded — while the code's own `print("Saved: ...")` made it look successful, so the
  file never appeared. `python_sandbox` now blocks writes aimed at a workspace/host path and
  redirects the agent to `write_file` (which actually persists to the chat workspace); its
  description also states the sandbox filesystem is ephemeral.
- **The main agent reacts the moment a sub-agent finishes,** instead of only when you next send a
  message. A finished sub-agent (coder, research, document, …) now pushes an internal
  notification that wakes the main runner immediately — with the previous periodic poll kept as a
  fallback — and the runner drains every session's result, so a completion is never missed because
  the runner's "current" session had moved on.
- **You can keep chatting while a sub-agent works (API mode).** The main agent now knows a
  sub-agent is running for your chat and keeps replies light: it will not start heavy new work,
  will not delegate the same task twice (a duplicate spawn is refused outright), and leaves the
  sub-agent's workspace alone; typing and sending stay unlocked the whole time. Safety fixes that make
  this reliable: a streamed reply is NEVER erased anymore — if it sounds like completion while the
  sub-agent still runs, it stays visible and a note keeps the next turn honest; the result is delivered once, by
  the background runner, with all window/messenger notifications — not mixed into a chat reply;
  a result is never validated against unrelated small talk (no more forced-retry storms);
  chatting can no longer force-expire a long run (the 30-minute hardcoded reaper now honors the
  configured timeout); and pressing Stop while a reply streams stops only the reply — the
  sub-agent keeps working (stopping it is an explicit second press when nothing is streaming).
  On local mode nothing changes (the adapted behavior is API-only; the single local
  llama server should not serve two inferences at once).
- **The coding agent works on the Veyllo API.** The coder resolved providers from its own
  hardcoded list that was missing `veyllo`, so switching the provider to Veyllo made every
  coding task fail with "VAF Server unreachable (Port 8080)" (it wrongly fell back to the
  local-server path) while normal chat worked fine — or, with a leftover local llama-server
  still running, silently generated with the LOCAL model instead of the API. An unknown API
  provider now fails loudly instead of falling back, and a test keeps the coder's provider
  map in sync with the central provider list so this cannot drift again.
- **Chat messages no longer queue for minutes behind a coding run.** A crashed workflow step
  could leak an internal "run sub-agents in-process" flag into the long-running backend; after
  that, every coding task silently ran inside the chat turn itself instead of as a separate
  process — the window showed the coder working, but new messages waited in line until it
  finished. The flag is now restored even when a step fails, and the runner additionally clears
  a stale flag before every chat turn.

## [0.1.0a6] - 2026-07-04

### Added
- **The coding agent edits existing files surgically.** A new `edit_file` tool changes only the
  targeted text (exact search/replace, a unique match required, all-or-nothing) instead of
  rewriting the whole file, so a one-line fix no longer risks a full rewrite that drops the
  framework or unrelated code.

### Fixed
- **A coder task that restores from git history no longer stalls.** The version-history and
  restore tools (`git_log`, `project_history`, `project_rollback`) are now available while the
  agent executes a task, not only while it plans, and they run against the real project repo.
  `run_tests` also rejects a `git` or OS-package-install command sent as its shell command and
  points to the right tool, instead of failing silently inside its isolated test sandbox.
- **Tool calls that a model serializes as XML/text in the message body** are recovered and hidden
  instead of leaking into the visible reply.
- **"Allow always" for a directory persists again** — the trusted-directory list stays
  JSON-serializable.
- **The coding agent's console shows output immediately.** Removed the typewriter animation that
  made the live console lag behind the real timestamps.

## [0.1.0a5] - 2026-07-04

### Added
- **The coding agent can run its own tests.** A new `run_tests` tool runs the project's
  test suite inside the isolated Docker sandbox and returns the real pass/fail, so the coder
  verifies its work instead of asserting that "tests pass".
- **The coding agent's shell is confined to a kernel-jailed workspace.** Coder `bash` now runs
  inside a bubblewrap jail with full access to its project but with VAF's own source, config,
  secrets and the host docker socket structurally out of reach, and with networking unshared —
  a generated build can never reach or overwrite the running system. Host and docker tasks move
  to the main agent's new `host_bash` tool, which runs on the host under an explicit per-command
  confirmation and is blocked on remote messaging channels (Telegram/WhatsApp/Discord) in two
  layers, so it can never run unconfirmed from a chat message.
- **Deterministic ORIENT and DOCUMENT phases for the coder.** Before planning, an orientation
  scan feeds the existing project's file inventory into the planner, so edit tasks on an existing
  project no longer stall without making a change. After the build, a documentation phase creates
  or updates the README to reflect the run's real changes (detected via git) — generated projects
  are now documented, and an existing README is updated in place rather than overwritten.
- **Runnable scaffold templates.** Each coder template now ships a small working example (instead
  of an empty TODO) and a matching test that is green out of the box, giving even a small model a
  concrete pattern to adapt. Server and app templates are importable and testable, and the
  template chrome is English throughout.

### Fixed
- **Created Markdown and text files open in the in-app viewer** with a preview toggle instead of
  dead-ending.
- **The failover ("failsafe") level selector** no longer shows its connecting line through the
  hollow, unselected dots.

## [0.1.0a4] - 2026-07-04

### Fixed
- **Workflow/automation files stay in the run's chat workspace.** A workflow step that
  wrote a file with a bare relative name resolved it against the backend process working
  directory (the user's home root), where the file endpoint then refused to serve it —
  clicking the file chip navigated the whole desktop window to a raw `{"detail":"Access
  denied"}` page with no way back. Relative new-artifact paths in `write_file`/`move_file`
  steps now resolve against the shared per-run project directory; explicit absolute/`~`
  paths, folder aliases, and in-place updates of existing files are left untouched. The
  `WriteFileTool` home-reroute guard (dead for months due to a shadowed import) is
  restored, and the coder's CONTENT_ONLY cleanup only removes its own temp directories,
  never an injected workspace (which had deleted freshly written files).
- **Created-file chips never dead-end the UI.** Extension-less files open in the in-app
  viewer; downloads use the native Save-As bridge in the desktop window and a safe blob
  download in the browser, with a toast on failure instead of a full-window navigation.
  Raw file links are excluded from the desktop same-window link rewrite.
- **In-app update notes now appear for pre-alpha installs** whose stored acknowledgement
  used the old internal version numbering, and long release notes scroll inside the card.
- **Security:** refreshed the WhatsApp bridge and web dependency locks — all critical and
  high advisories resolved (63 of 64 alerts; the last is fixed by a future Next upgrade).

### Added
- VAF records itself as a co-author on commits it creates.

## [0.1.0a3] - 2026-07-03

### Added
- **In-app update notes.** After an update, the Web UI shows a one-time "What's new"
  window with the changes of the new version (same place as the first-run alpha
  notice; acknowledged per user). Alpha releases are now compared at full-version
  granularity so every release can carry notes.

### Fixed
- **Windows: installing without WSL2 no longer fails at the Rancher Desktop step.**
  The installer now checks WSL2 first (locale-independent, no admin needed for the
  check), enables it via a single UAC prompt when missing (no Linux distribution is
  installed; `dism` fallback for older Windows 10 builds), sets version 2 as the
  default, and pauses cleanly with resume instructions when Windows needs the
  restart (exit code 3010 is treated as a planned pause, not an error). An already
  running Linux container engine (e.g. Docker Desktop on Hyper-V) skips the check.

## [0.1.0a2] - 2026-07-03

### Fixed
- **First-run setup no longer races the database (all platforms, worst on Windows).**
  The Docker stack starts in parallel with the web server; when PostgreSQL was not
  ready in time (a first Rancher/WSL2 boot takes minutes), the auth tables were never
  created and a fresh install showed a login form with no account to log in to.
  Startup now gives the database a short head start, the auth-table init retries in
  the background until the database is ready (never giving up), and the login page
  shows "Starting the database..." and switches to the setup wizard on its own.
- **macOS: the memory stack starts even when the docker CLI lacks the compose
  plugin** (Homebrew docker + Colima: `docker compose` failed with
  `unknown shorthand flag: 'f'` while the standalone `docker-compose` binary was
  installed and working). VAF now detects the missing plugin and falls back to the
  legacy binary; real compose errors still surface unchanged.
- **Local model loads reliably (llama-server startup).** Server readiness now
  requires `/health` = 200 — llama-server answers 503 while the model is still
  loading, and accepting any response green-lit servers that died seconds later,
  causing an endless relaunch loop with orphaned processes. Slow cold loads get a
  generous configurable budget (`server_ready_timeout`) instead of being killed
  mid-load. When the backend has no Flash Attention kernel for the model (e.g.
  Qwen3.5 on Apple Metal), the quantized V cache made the server die at context
  init — VAF now retries once with an f16 V cache and remembers the outcome.
  Server output is always captured to `logs/server_last.log` (crashes left zero
  diagnostics before).
- **macOS: `model: "auto"` now scales with the machine.** Apple Silicon reported
  0 GB GPU memory, so every Mac downloaded the smallest 4B/Q4 model. The GPU
  budget is now 65% of unified memory (capped at RAM minus 6 GB for the OS and
  services), so e.g. a 32 GB Mac gets the 9B model while a 16 GB Mac stays on the
  4B tier that actually fits.
- **macOS: microphone/STT works in the desktop window.** The installer adds the
  microphone usage description to the host Python.app (with safe re-signing and
  rollback), and VAF grants WebKit microphone capture — scoped to the local WebUI
  origin and microphone-only, so pages loaded in-window (OAuth, model-card links)
  can never capture audio. Note: a `brew upgrade python@X.Y` reverts the plist
  patch; re-run `scripts/macos_mic_plist.sh` (the startup log warns about it).

### Changed
- Windows quickstart in the README works on stock PowerShell 5.1 (no `&&`,
  `install.bat` instead of calling `install.ps1` directly).

## [0.1.0a1] - 2026-07-01

### Fixed
- **macOS: VAF now starts.** The launcher (`run_vaf.sh`) exec'd the raw Homebrew
  framework Python instead of the venv's Python after activating the venv, so every
  dependency showed up as "missing" and startup failed (worse on a Homebrew Python
  3.14 machine, where it hunted for the 3.14 framework binary). It now runs
  `venv/bin/python` directly — a framework build, so the menu-bar tray still works,
  and it sees the installed packages.
- **macOS: the menu-bar tray icon no longer crashes** (`AssertionError: self.png
  is None`, resulting in no tray icon). The icon PNG was opened lazily and read by
  pystray from its own thread while being rewritten on every call; it is now decoded
  eagerly and written atomically (temp file + rename).
- **macOS: the onboarding step animation no longer "double-plays"** (jump up, snap
  back, then slow slide) in the WebKit/WKWebView desktop window — a framer-motion
  v10 WAAPI commit-timing re-read triggered by a reflow mid-transition. The steps
  now animate on the main thread via an `onUpdate` shim.

## [0.1.0a0] - 2026-06-30

### Changed
- **Thinking-mode proactive questions are now delivered to your configured main messenger**
  (Telegram/WhatsApp/Discord) and tracked as a request there, instead of only the Web UI. If a
  messenger question goes unanswered it is escalated once to the Web UI with a note that it was
  already asked on that channel; with no messenger configured the behaviour is unchanged. The
  background run now contacts you exclusively through `ask_user` (all raw `send_*` tools are removed
  from thinking runs), and `ask_user` carries the running user's real scope so a non-admin's question
  is never delivered to the admin's messenger. `send_whatsapp_reply` now reports real delivery, so a
  down WhatsApp bridge falls back to the Web UI instead of silently dropping the message.
- **License: relicensed from "MIT + Commons Clause v1.0" to a dual license — GNU
  AGPL-3.0-or-later (open source) plus a separate Commercial License.** `LICENSE` now
  carries the verbatim AGPL-3.0 text; see the new `LICENSING.md` (dual-license explanation,
  EN/DE) and `COMMERCIAL.md` (commercial/Enterprise terms). Building Plugins, Tools, and
  Workflows on top of VAF stays permission-free via an AGPL Section 7 additional permission.
  Contributor terms in `CONTRIBUTING.md` updated: contributions are accepted under the AGPL
  inbound plus a separate commercial-relicensing grant to Veyllo GmbH (so the dual-license
  model is enforceable), with a DCO `git commit -s` sign-off certifying origin. Source files
  now carry `SPDX-License-Identifier: AGPL-3.0-or-later` headers pointing to `LICENSING.md`.

### Added
- Vision-as-a-tool for attached images (`vision_mode: "description_tool"`, default):
  the main model is text-only — an attached image is described once via the vision
  backend, that description is injected as text, and the new `analyze_image` tool
  re-inspects the image on demand (exact colours, positions, small text, finding an
  object). Token-efficient, works even with a non-vision main provider, and the image
  description survives reloads / the worker pool. `vision_mode: "inline_multimodal"`
  restores the previous raw-image behaviour. New keys `vision_mode` /
  `vision_description_max_tokens`; see `docs/llm/API_INTEGRATION.md`. Uploaded images are
  now stored as **files** in the user-siloed chat folder
  (`VAF_Projects/<uid8>/<session_id>/attachments/`) with only the path in `session.json`
  (no more inline base64 bloat); the agent can reference them by path and the Web UI
  re-displays them after reload via `/api/file`. Legacy base64 sessions keep working.
- Embeddable library surface: `from vaf import Agent` (`docs/EMBEDDING.md`,
  `docs/ARCHITECTURE.md`); slim base install plus optional extras in `setup.py`.
- Entry-point tool discovery: third-party tools via the `vaf.tools` group.
- Tool input validation & repair before dispatch (`docs/agents/TOOL_INPUT_REPAIR.md`).
- Self-update: `vaf update check` / `vaf update`, an opt-in startup
  update-available hint, and a tag-triggered GitHub release workflow.
- Web search result cache: identical `web_search` queries are served from a
  short-lived file cache (default 15 min; `web_search_cache_enabled` /
  `web_search_cache_ttl_seconds`), skipping the providers and synthesis.
- Email subsystem hardening. **New config key `email_allow_private_hosts` (default
  `false`)**: IMAP/SMTP hosts that resolve to loopback / RFC-1918 private / link-local
  addresses (incl. the `169.254` metadata range) are refused as an SSRF guard unless this
  is enabled. IMAP/SMTP connections now verify TLS certificates against the system trust
  store (connect timeouts; port 465 uses implicit SMTP_SSL). `GET /api/config` redacts
  secret keys (`api_key_*`, `*_secret`, `*_password`, `memory_db_url`, `redis_url`,
  encryption keys, ...) for non-admin users; admins still receive everything.
  `POST /api/email/accounts/test` now requires authentication and is rate-limited (shared
  per-IP login limiter). OAuth PKCE state files (email + cloud) are written atomically with
  `0600` permissions, and token-endpoint errors are no longer logged verbatim.
- `send_mail` now supports `cc`, `bcc`, and reply threading via `in_reply_to` /
  `references`, with recipient-address validation.

### Fixed
- Filesystem alias resolution now matches only on a path boundary.
- `send_mail` no longer silently drops a single string attachment path.
- Mailbox authentication/connection failures now surface as an "authentication failed"
  error from `mail_inbox` / `read_mail` instead of an empty "no messages" result.
- Email headers (From/To/Subject) are now RFC 2047-decoded and message bodies are decoded
  with the part's declared charset (previously hardcoded UTF-8).
- Switching to an unowned/new session now resets the agent's current user scope/username,
  preventing cross-user identity bleed; UUID-scoped network users' mailboxes are now
  included in email auto-sync.
- Cloud storage OAuth (Google Drive etc.) now opens in the system browser instead of the
  embedded desktop webview, and its callback uses the same effective HTTPS proxy port as
  email (shared `vaf/network/oauth_redirect` helper) instead of an unreliable
  `request.base_url`, so connecting cloud accounts works on the Linux/macOS desktop.
- Cloud OAuth tokens for the local admin are found again: the cloud credential key is now
  normalized identically for storage and lookup (tokens were stored under the raw admin
  username but looked up normalized, causing a false "Credentials not found").

<!--
Template for a new release (see docs/setup/RELEASING.md):

## [X.Y.Z] - YYYY-MM-DD
### Added
### Changed
### Fixed
### Removed
-->
