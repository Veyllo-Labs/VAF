# Changelog

All notable changes to VAF are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and VAF aims to follow
[Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`, with PEP 440
prerelease suffixes such as `a0` / `b1` / `rc1`).

Each released version has a matching git tag `v<version>` and a GitHub Release.
To update an installed VAF, run `vaf update` (on Windows, from the install folder:
`run_vaf.bat update`).

## [Unreleased]

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
