# Changelog

All notable changes to VAF are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and VAF aims to follow
[Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`, with PEP 440
prerelease suffixes such as `a0` / `b1` / `rc1`).

Each released version has a matching git tag `v<version>` and a GitHub Release.
To update an installed VAF, run `vaf update` (on Windows, from the install folder:
`run_vaf.bat update`).

## [Unreleased]

### Changed
- **`vaf run --web` no longer starts the background service behind your back.**
  The README has always described this command as the dashboard WITHOUT the tray,
  and the lane hosts the dashboard itself - but if the background service was not
  running, the command quietly launched it as a detached process that outlived
  your session. Now the dashboard lives and dies with your session, as promised.
  If you relied on `vaf run --web` to bootstrap the persistent service, start it
  the intended way: `vaf tray`. A service that is already running keeps serving
  unchanged.

### Fixed
- **A long conversation no longer animates dozens of invisible dots.** Every reply
  in the terminal app carries the agent's living dot, but only the newest one shows
  it - the hidden ones kept animating anyway, ten times a second each, so a long
  session burned more and more CPU drawing into nothing. The visible dot animates
  exactly as before; the hidden ones now rest until it is their turn again.
- **`vaf update` now tells you why it could not answer.** Three different situations
  used to collapse into one sentence, "offline, or none published yet", whose two
  halves call for opposite reactions. The real case that exposed it was neither:
  GitHub allows unsigned-in requests only 60 per hour for a whole network address,
  so any busy tool on the same connection could make the updater claim there was no
  release minutes after one went live. It now says which it is - rate limit (with
  the time to try again), unreachable, or genuinely no release - and a plain
  permission error is not dressed up as a rate limit.

## [0.1.0a20] - 2026-08-05

### Added
- **The terminal now shows how far a sub-agent has got.** The task line above the
  prompt showed only that something was running and for how long. It now also shows
  the count, like `2/5`, for the two sub-agents that plan their work upfront: the
  coder and the document agent. It is a count and never a percentage, because a
  coding run legitimately finishes below its total when a task fails, and a bar that
  has to reach 100% would have to lie about that. The three sub-agents that do not
  plan upfront show nothing rather than a made-up number.
- **A session now opens by telling you where you are.** Instead of a bare "new
  session" line, `vaf run` greets you with the Veyllo mark and the facts beside it,
  centred on screen: version, your agent's name, the session name and id, the
  active model, the local date and time - and the one line that was missing
  entirely, how to get back into an earlier conversation.
- **Most settings are editable inside the terminal app now.** Rows that used to say
  "use `vaf settings`" work where they can: the AI provider and its model take effect
  on the running agent without a restart, and the speech engine, input language,
  sub-agent provider, sub-agent timeout, auto-open tab limit and local-server
  auto-start are simply set. The three that genuinely need a fresh start - the local
  model, the context limit and downloading a model - still point at `vaf settings`,
  and now say why rather than just deferring.
- **Sessions you never wrote in do not pile up any more.** Opening `vaf run` and
  closing it again left an empty session behind every time, and the list filled up
  with rows that held nothing. An untouched session is now dropped when you leave
  it, whether you quit or load a different one; anything you actually wrote in is
  kept as before.
- **You can switch sessions from inside the terminal app.** The sessions panel was
  a list you could look at but not use; the only way back into an earlier
  conversation was quitting and passing its id on the command line. Ctrl+S now
  opens it, the arrow keys walk it and enter loads the one you picked. And when
  you leave, VAF prints the session id and both commands that bring you back,
  instead of letting it disappear.
- **The prompt remembers, suggests and completes again.** The full-screen `vaf run`
  had a plain text box: no history on the arrow keys, no inline suggestion, no
  completion for commands or file paths. All three are back, and they share the
  older interface's history file, so your past messages are the same list whichever
  way you start VAF. Typing `/` or `@` opens a menu you can walk with the arrow
  keys; while it is open Enter picks an entry, and the next Enter sends.
- **The terminal commands are back, and all of them work in both interfaces.** Typing
  `clear`, `tools`, `undo`, `restore`, `context`, `halt` or `restart` in the new
  full-screen `vaf run` used to send the word to the model as a chat message. They
  are commands again, alongside the ones that already worked, and a mistyped
  `/command` now says so (with the closest match) instead of costing a turn.
  Arguments work too: `theme dark` picks that theme instead of cycling, and
  `session <id>` loads that session. The ones that cannot be undone - clearing the
  conversation, rolling back files, restarting - ask first. Behind this, the command
  list lived in six places that had already drifted apart: the older interface
  offered completions for words it could not run, and ran a word it did not offer.
  There is one list now, and both interfaces read it.
- **Answers in the terminal are formatted again.** The full-screen `vaf run` showed
  the model's markdown verbatim: literal asterisks around bold text, raw list
  markers, and code fences as three backticks and unhighlighted text. Headings,
  lists and emphasis now render, and code blocks are syntax-highlighted and follow
  the active theme. Long answers stream just as smoothly as before, because the
  text is redrawn on a fixed rhythm instead of on every word.
- **Tool cards in the terminal now show what the tool actually returned.** The
  observation events an application can subscribe to reported that a tool finished,
  how long it took and whether it failed, but never what came back - so anyone
  building on VAF had to correlate results out of debug log files, and three places
  inside VAF carried their own private copy of the result for the same reason, each
  cut to a different length. The `tool_end` event now carries a `result` field
  (capped at 800 characters, always a string, safe to serialize). In `vaf run` the
  tool card is no longer an empty fold: it opens onto the result.
- **`vaf run` opens a full-screen terminal app.** The terminal chat is no longer a
  scrolling prompt: it is a full-screen app with a live transcript (strictly
  chronological, streamed answers with the model's reasoning as a separate muted
  think block), tool cards, event narration, the sub-agent status line, the
  context-usage bar, the agent's animated avatar beside the newest reply, and
  keyboard-complete overlays for settings, model, history, sessions and help. The
  classic run-loop letters (`s`, `c`, `t`, `h`, `l`, `?`) still work typed into the
  prompt, as do slash commands and `@file` attachments. Tool confirmations finally
  work in the terminal: previously they silently waited on the web dashboard and
  timed out after five minutes if no browser was open; the app shows the question
  and answers it in place. The previous interfaces stay available - `vaf run
  --classic` for the plain prompt, and the new `tui_mode` config key to pick the
  default lane (`vaf run --web` keeps the previous lane, which owns the web-server
  startup). Voice capture and provider switching inside the app land next.
- **You can now ask for one completion without a conversation.** Building something on
  VAF that just needs one answer - a classification, a summary, a commit message -
  meant either running a full chat turn (history, tools, memory, routers) or
  hand-rolling the backend call, and about twenty places inside VAF had done exactly
  that, each slightly differently wrong. `agent.complete(prompt)` is one call with the
  agent's configured backend: it never enters the conversation, runs no tools, writes
  no memory, strips model reasoning from the result, and returns text or None - never
  an exception and never an error message dressed as an answer. The same primitive now
  powers the tools' own utility completions and the CLI features below.
- **An application built on VAF now decides which tools each account may use - with one
  registered resolver instead of VAF's own user database.** The per-account tool allowlist
  used to be wired straight to the product's auth DB inside the dispatch pipeline, so an
  application embedding VAF got a check it could neither feed nor replace.
  `set_account_allowlist_resolver` is now part of the public interface: register one
  function that answers "which tools for this account" from your own storage, and it is
  enforced everywhere the pipeline runs - before the per-call authorizer, so an `allow()`
  cannot lift an account-level ban, and inside the coding agent, where the answer crosses
  into the child process as data. VAF's own product registers its resolver through the
  same primitive, and the pipeline no longer imports the product's auth layer at all (a
  test now keeps it that way). A registered resolver that crashes refuses rather than
  quietly enforcing nothing; registering nothing means unrestricted, as before.
- **A tool can now say that it touches files, and the per-user boundary is installed for
  it.** Building something on VAF that reads or writes a user's files meant declaring who is
  calling and then writing the confinement yourself, inside every tool, exactly right, every
  time. Eleven built-in tools did precisely that, across five files, and five of the
  twenty-two that needed it had simply forgotten. A tool now declares `file_access = "read"`
  or `"write"` next to its identity, and the boundary is applied around it on every path -
  including when something calls the tool directly, with no dispatcher involved. Declaring a
  mode without the matching identity is refused when the class is defined, rather than
  quietly doing nothing later: a tool that receives no identity would otherwise run
  completely unconfined while looking confined. A boundary inside another can only narrow
  what it inherited, never widen it.
- **A new example runs without needing a model at all.**
  `examples/07_tool_caller_and_authorizer.py` shows the two pieces below in one runnable
  script: running a tool with VAF's rules but no conversation, and deciding about each call.
  It needs no API key, no provider and no network, which makes it the quickest way to check
  a fresh install actually works.
- **An application built on VAF can now decide about each tool call itself.** Until now the
  only way to keep a tool away from someone was to leave it out entirely - a choice made once
  at startup, for everybody. `set_tool_authorizer` is asked before every call and can refuse
  it, insist on a confirmation question, or let one through without the question. It sees who
  is calling and what the call would do, so decisions like "this customer's plan has no shell
  access" or "not that file, it belongs to someone else" are finally expressible. Refusing is
  the safe direction throughout: an authorizer that answers nothing changes nothing, and one
  that crashes refuses rather than waving the call through.
- **You can now run a single tool without starting a conversation.** Building something on
  VAF that just needs a tool done - a scheduled job, a queue worker, your own agent loop -
  meant either wrapping a whole chat agent around it or rebuilding the safety checks by
  hand, and rebuilt checks drift apart from the real ones. `ToolCaller` is now part of the
  public interface, and it is the exact same one the agent itself uses: the same permission
  rules, the same confirmation question, the same identity handling, the same time limits.
  Documented in the embedding guide.

### Changed
- **An embedded agent created with the machine owner's account key now reads the
  owner's profile** instead of an empty per-key workspace. Keys belonging to anyone
  else are unaffected and still never resolve to the owner.
- **The repository root is quieter.** The contributor, conduct and security documents
  moved into `.github/`, where GitHub still finds them, and the commercial terms and
  third-party inventory moved to `docs/legal/`. The separate `ruff.toml` is gone; its one
  setting lives in `pyproject.toml`. Nothing a user runs changed: the installers, the
  start scripts and the licence files stayed exactly where they were.
- **The default look is monochrome.** The terminal theme is a black-to-white ramp
  now, so nothing in the frame competes with what the agent is actually saying, and
  the agent's white dot is the brightest thing on screen. Success, warning and error
  keep a colour, muted enough to belong but distinct enough to still be read at a
  glance. Both terminal interfaces share the theme, so both change together, and
  whichever one you pick is the one you get next time. The light theme is gone:
  it sat at the end of the cycle, so pressing the theme key often enough landed
  on it and stuck, and on a white background the agent's white mark and every
  white accent simply disappeared. Anyone who had it selected comes up on the
  default instead.
- The terminal app's bottom row no longer cuts itself in half. The key hints and
  the context usage share one line and do not fit together below roughly 120
  columns, and the overflow used to be resolved by clipping - mid-label, so
  `/exit` appeared without its `Quit` and on a busy context bar even `Help` was
  gone. Now the context bar gives up its token counts first and hint pairs drop
  whole, so whatever is on the line is readable.
- The terminal transcript follows a streaming answer again. It scrolled when a
  message was mounted but not while one grew, so a long reply ran on below the
  fold while the view sat on its first lines. It now follows the text - and
  stops following the moment you scroll up to read something, instead of
  yanking you back down.
- Long command descriptions in the terminal help wrap under themselves instead
  of continuing beneath the key column, where they read like another command.
  The session list, the start block and the mark itself also survive a narrow
  window now rather than losing their last characters.
- The mark in the terminal's start block is now the Veyllo logo converted to
  terminal art rather than drawn in solid blocks.
- The README opens on the mark rather than on an ASCII wordmark, and shows the
  terminal and the web UI as one image above the installation steps.
  The desktop and terminal modes each show what they look like.
- **Workflow steps now pass the same security checks as chat.** A step used to run its
  tool directly, skipping every rule a chat turn answers: admin-only tools, channel
  policy, the per-user tool selection, and the veto an application embedding VAF can
  attach. Steps now run through the shared dispatch pipeline, so all of those hold
  inside workflows too. Three deliberate differences remain: the confirmation question
  stays off for workflows (they run unattended - taking that away is its own decision),
  the heavy sub-agent steps of a temporary workflow still run as child processes outside
  the step pipeline (their internal tools remain constrained separately), and a step
  whose tool crashes now fails that step and lets the workflow branch, instead of
  aborting the whole run. Malformed step arguments are now refused cleanly before the
  tool runs. The existing rollback switch restores the entire previous behaviour if
  needed.
- **A workflow now runs as the person who started it, by default.** Until this release a
  saved workflow always acted as the machine owner, whoever ran it: its files went to the
  owner's folders, its GitHub calls used the owner's account, and anything it created was
  filed under the owner. That was switchable before and is now the default. What changes in
  practice is that 47 further tools finally learn who is running the workflow - files,
  GitHub, automations, skills and reading messages among them - and none of them lose an
  identity they already had. If a workflow of yours relied on reaching the owner's files by
  an absolute path or a folder name like `Desktop/...`, that step will now be refused for
  anyone who is not an administrator; the setting `workflow_identity_injection` set to
  `legacy` restores the previous behaviour.
- **Workflows carrying an identity now pass it to far more tools.** The setting introduced
  in 0.1.0a19 only reached a fixed list of tools; set to `declared` it now asks each tool
  what it needs, the same way a normal chat message already did. In practice that means
  files, GitHub, the browser, skills and automations finally know who is running the
  workflow, and mail learns the person's role rather than only their name. Nothing loses
  access - every tool that had an identity before keeps exactly the one it had. Still off
  by default (`legacy`).

### Removed
- **Three launcher scripts that nothing called.** `stop_vaf.sh`, `launch_vaf.scpt` and
  `Start VAF.command` were not referenced anywhere: not by the installers, not by the
  Linux desktop entry or the systemd service (which stops VAF by signalling the process),
  not by any documentation. On macOS the installers already build a proper `VAF.app` that
  launches from wherever the project actually lives, while `launch_vaf.scpt` still assumed
  it sat in `~/VAF`. Stopping a persisted server is documented separately and unchanged.

### Fixed
- **`vaf run --web` now says that it changes the interface.** The flag has always
  routed to the previous prompt interface, because that is where the web server and
  its watchers are wired, but it did so without a word: you typed a flag you had used
  for months and got a different-looking VAF with no reason given. It now says so at
  startup, and only when the interface actually changes.
- **The terminal app asks for an API key instead of storing a provider that cannot
  work.** Switching to a provider you had no key for wrote it to the config anyway
  and told you to restart, which was the one thing that could not help: the next
  start came up on a provider with no way to reach it. Now a provider with no key
  opens a masked key field, `k` on a provider row opens it for a key you want to
  replace, and the key is checked with one real request before anything moves. A
  key that does not verify is kept but the provider stays where it was, because
  that request can fail on the network as easily as on the key. The key itself is
  never shown, never written into the conversation, and the check runs in the
  background so the app keeps responding while the provider answers.
- **Choosing a provider for sub-agents in the terminal app now actually moves them.**
  The row wrote the provider's name but not the switch that turns the choice on, so
  it reported success, moved its marker onto your choice, and every sub-agent went
  on using the main agent's provider. Six places worked out that pair by hand and
  now ask one function, so the two halves cannot drift apart again. Two more things
  come with it: the older `vaf settings` menu announced the chosen provider in its
  panel even when the switch was off, and it does not any more; and picking a
  provider you have no API key for is now refused in the terminal app too, instead
  of being stored and failing inside every sub-agent afterwards.
- **VAF no longer names one theme while showing another.** If your saved theme was
  not the default, the terminal app's Theme list put its marker on the default row,
  and the `vaf settings` menu labelled the default too, while the screen was painted
  in the theme you had actually chosen. The name came from a value that only two of
  the four terminal lanes ever loaded from your config; every surface now reads the
  saved choice, and a `--theme` flag reaches them all. Worth knowing, because it made
  the above look like the theme was not applied at all: `t` writes your choice
  immediately, so cycling through the themes to look at them leaves you on the last
  one you stopped at, and that is what the next start uses.
- **A timer set in the terminal app now actually arrives.** Asking for a reminder
  in `vaf run` set the timer, said so, and then nothing happened when it elapsed -
  the terminal app never looked at the queue the timer fires into, so it expired
  in silence. It now shows an amber card with what woke the agent, followed by the
  agent's answer. A timer belonging to a different conversation is dropped with a
  note saying so, rather than swapping the conversation you are reading.
- **A note the agent saves for itself now belongs to the person it was talking to.**
  On an installation with more than one account, a note written while a background
  pass happened to be running was filed under that pass's account instead - and notes
  are read back into that account's next background pass as instructions to follow.
- **Background thinking no longer changes what a waiting person's turn is allowed to
  do.** While it ran, anyone chatting at the same time silently got a fraction of the
  tool budget, a cap after three lookups, no retry when the model returned nothing -
  which is what leaves the web page stuck on a loading block - and a refusal from
  their own "update what I'm working on" action. Each of those limits is meant for
  the background pass alone.
- **The program keeps asking you for confirmation after an automation has run.**
  The first automation switched the whole program into a mode that never prompts and
  never switched it back, so from then on confirmations a person should have seen were
  answered without them.
- **A scheduled automation can no longer put its output in someone else's place.**
  On an installation with more than one account, a background task had no session
  of its own and quietly adopted whichever conversation was last active in the
  program. Its document was then created inside that person's workspace folder,
  announced in that person's browser, and their conversation was permanently
  re-pointed at the folder. In the same run their Stop button would cancel the
  background task, while the task's real owner could not stop it at all. A run now
  states which conversation it belongs to - a scheduled task says "none" - and
  nothing falls back to a program-wide value. Single-user installations were never
  exposed to the cross-account part of this.
- **An update can no longer make sub-agent results stop arriving.** If a newer VAF
  wrote one entry into the shared sub-agent file and an older one read it, the older
  one gave up on the entire file rather than on that one entry - so every finished
  sub-agent result was silently dropped, with nothing in any log. This only needed
  one mixed-version moment to trigger and did not repair itself.
- **A coding run without an open Web UI no longer does the work of drawing one.**
  Every loop iteration and every written file gathered a full project snapshot -
  several `git` calls and a walk of the project tree - before checking whether
  anyone was watching. On the terminal, where nobody is, that work was thrown away.
  The librarian did the same with a filesystem scan that also wrote a cache file.
- **A skill you shared with one person is visible to that person again.** On the
  lanes that stored the account key as a parsed object rather than as text, the
  visibility check compared two different kinds of value and never matched, so
  `use_skill` and `read_skill` answered "not available to you" to the very person
  the skill had been shared with.
- **The machine owner is no longer locked out of their own files.** If the owner
  key was ever stored in a different spelling than the one VAF generates, the
  terminal app rewrote it on startup and then compared the rewritten value against
  the stored one. It never matched, so the owner was treated as a guest: jailed out
  of their own project folder and refused on their own files.
- **A timer or automation can no longer leave a conversation without an owner.**
  Stamping the owner onto a fresh conversation could fail silently on the
  background lanes, and a conversation with no owner loses the channel it came
  from, so a later sub-agent result had nowhere to go back to.
- **A background "thinking" run no longer borrows the machine owner's profile.**
  After loading a person's chat history, the run took its identity from that
  conversation; if the conversation had no name stored, three fallbacks quietly
  substituted the owner's, and the owner's personal profile went into a different
  person's prompt and into the message sent to them.
- **An elevated permission no longer survives from one queued message into the
  next.** The role was only ever cleared on two of the paths that start a turn.
- **A damaged owner key in a restored or hand-edited automation file no longer
  resolves to the machine owner.** It gets its own isolated bucket instead.
- **The classic CLI no longer loses its prompt for good after a second message
  from the web UI.** Once two messages had been queued for the same conversation,
  the terminal dropped to a plain `Message:` line with no completion, no history
  and no voice, and stayed there until you restarted it. Behind it, a finished
  task was never marked finished, so the conversation looked permanently busy and
  every later message for it was held back with nothing to release it. Both the
  hold and the crash it caused are gone, and any lane that consumes queued work -
  including ones written later - is covered by the same fix rather than having to
  remember the release itself.
- **A long answer no longer streams past the bottom of the terminal app.** The
  transcript stopped following after the first few lines and the reply ran on
  below the fold, so you had to scroll down yourself to see the agent finish.
  Whether it happened at all depended on your terminal size, which is why it
  looked intermittent. Scrolling up still leaves you where you are reading, and
  coming back down picks the answer up again.
- **On Windows, attaching a file with `@` works.** Typing `@C:\Users\me\notes.txt`
  attached nothing and left the path sitting in your message: the reference stopped at
  the drive letter's colon, so VAF only ever saw `@C`. Relative paths and every other
  platform were unaffected, which is why it went unnoticed.
- **The licence note now points somewhere you can actually go.** `vaf info` and the
  licence panel in settings told you to read `LICENSE`, `LICENSING.md` and
  `COMMERCIAL.md`, which is fine in a checkout and useless after `pip install vaf`:
  the licence texts live inside the package metadata there, and the commercial terms
  were never part of the distribution at all. Both now show a link instead.
- **VAF understands German written without umlauts.** Plenty of people type "taeglich"
  or "ueberweisung" because their keyboard has no umlaut keys, and language models have
  lately taken to writing German that way too. Everything VAF matches on - the words that
  start a workflow, the risk check on an outgoing mail, the phishing score, the guard that
  catches the assistant claiming success it never earned - looked only for the spelling
  with umlauts and quietly did nothing for the other one. Both spellings now reach the
  same place. The guard against unearned success claims is the one that mattered most: it
  had been blind to exactly the models it exists to catch.
- **Sending a message no longer stalls while VAF saves what it learned.** Every
  submitted line rewrote the entire learned-phrases file before anything else could
  happen - on a well-used install that is a 2.5 MB write and about a seventh of a
  second, paid in the terminal and in the web app alike. The learning is kept in
  memory immediately and written out a few seconds later, in the background, and
  the file is replaced atomically so an interrupted save cannot corrupt it.
- **A sub-agent no longer draws over the terminal app.** When the coding agent, the
  librarian or the research agent ran without their own window, each painted a live
  progress panel straight onto the terminal - which shredded the display of the new
  full-screen `vaf run`. All three now ask one shared question first, and while an
  application owns the screen they report their progress into the transcript instead
  of over it.
- **The terminal app no longer loses its model, its settings or its crash reports.**
  Five things went wrong quietly in the new full-screen `vaf run`: the desktop tray
  could unload the local model in the middle of a session, because the app never
  told it that a session was alive; a failing turn left no traceback anywhere; a
  theme picked in Settings only changed half the colors; the model name in the top
  bar always read "local"; the speech-input row could show "off" while the
  microphone was actually live; and switching server persistence did not reach the
  running agent. `vaf run` now also checks for Git before it starts, instead of
  letting tools fail deep inside an answer, and falls back to the previous
  interface if the full-screen one cannot start at all.
- **Files sent through the messengers are confined to your own data on shared
  machines.** Attaching a file to an outgoing Telegram, WhatsApp or Discord message
  (or to the main-messenger delivery) accepted any path the general safety checks
  allow - including another user's project tree. Only email attachments were
  confined. All senders now enter the same per-user boundary as the file tools, so
  a non-admin can only send files from their own tree.
- **A symbolic link can no longer smuggle a protected file into a file operation or
  an outgoing attachment.** The shared path rule checked the link's own location and
  the file was then opened at the link's target - so a link inside an allowed folder
  could reach VAF's credential store or system files. The rule now resolves links
  and re-checks the real target, for every caller.
- **Generated text no longer carries stream debris or hidden reasoning.** Several
  features collected the model's streamed answer with their own hand-written loops,
  and the loops disagreed: AI git commit messages could end in a raw
  `{"finish_reason": "stop"}` control frame, tool utility completions could return a
  provider ERROR message as if it were the answer, and on local reasoning models the
  coder's template detection and other short calls received empty text because the
  whole token budget went into hidden reasoning. All of these now share one collector
  that filters control frames, treats backend errors as "no answer", strips reasoning
  blocks, and disables reasoning for local utility calls.
- **Streamed memory answers work again.** Asking the memory panel a question with
  streaming on crashed on the very first token and returned an error string instead of
  the answer - every time, on every backend. The stream now delivers the text.
- **Attachment summaries respect their time budget.** The per-section summarizer
  declared a timeout that was never wired up, so a slow backend could stall document
  ingestion far beyond the budget the caller computed. The timeout now binds.
- **Settings no longer claims "API key missing" for a key that is safely stored.** Since
  keys moved into the encrypted store, the browser's copy of the config deliberately
  carries no key values - but six places in Settings still judged "is a key configured"
  from that copy: the warning banner in the provider section, the model-refresh buttons
  next to the provider, vision and voice-agent model pickers, and the three voice hints.
  A stored key therefore showed as missing, and refresh buttons stayed disabled although
  the server could resolve the key fine. All of them now ask the same stored-state the
  key fields already use, count a freshly typed key as present, and stay silent while the
  state is still loading or unreadable - "nothing configured" and "cannot tell" are
  different answers and no longer render as the same warning.
- **Editing an offline user no longer deactivates their account.** The user table showed
  "Active/Inactive" for presence - is the user connected right now - but the edit dialog
  read that same field as the account state and wrote it back on save. Editing anyone who
  happened to be offline and clicking Save silently blocked their sign-in, and a
  reactivation appeared not to stick because the list refresh dropped the mapped fields.
  Presence and account state are now two separate things everywhere, and the list refresh
  is one shared loader instead of four drifting copies. The user list also says
  "Online/Offline" for presence now, so it no longer borrows the words the account
  setting uses, and a deactivated account is marked as such right in the list.
- **An admin can no longer lock themselves - or everyone - out.** You cannot deactivate,
  demote or delete your own account (another admin can still do all three to you), and
  nobody can deactivate, demote or delete the last remaining admin who is able to sign
  in. Deleting was the gap: it only checked that some other admin account existed, even
  a deactivated one that could never log in to repair anything. The server refuses with a
  clear message, and the dialog does not offer the self-lockout controls in the first
  place.
- **A generated password is shown once instead of being thrown away.** Creating a user
  with the password field left empty is the advertised way to let VAF generate one - and
  the generated password was then discarded without ever being displayed, so the new
  account could not be used until an admin ran a separate password reset. It now appears
  in a confirmation dialog with a copy button, once, because it exists nowhere else.
- **Opening the user editor from the detail view no longer carries the previous user's
  state.** The access preset picked for the last user stayed selected and immediately
  rewrote the newly opened user's tool selection, and a temporary password from the
  previous user could appear in the new user's dialog.
- **The account status toggle says what it does.** It flips whether the user may sign
  in, but showed only a nameless switch. It now shows the state in words - Active or
  Deactivated - plus what each means, including the honest limit: deactivating blocks
  sign-in and token renewal immediately, while a session that is already signed in ends
  when its token expires.
- **The per-user access picker is searchable.** With 117 tools in the grid, finding the
  one to grant or revoke meant scrolling; both the tool and the workflow list now have a
  search field. Filtering is visual only - presets and "Select All" keep operating on
  the full list, so a filter can never silently shrink what they apply to.
- **The per-user tool permission is now actually enforced.** Since user management
  existed, an admin could choose which tools each user may use - and the choice was
  saved, displayed, and checked by nothing (the interface said so honestly). It is now
  enforced on every tool call: a user can only run the tools selected for their account,
  in chat and inside the coding agent, and a revocation takes effect within seconds, not
  at the next login. The coding agent's internal tools - the shell above all - now appear
  in the admin's picker as well, so "this user gets the coder but not the shell" is a
  choice the admin can express. Admins themselves are never restricted and an empty
  selection means unrestricted.
- **The per-user workflow permission is enforced as well.** The other half of the same
  admin choice - which saved workflows a user may run - was also stored and checked by
  nothing. A saved workflow now passes that list when it starts, wherever it is started
  from: chat, the workflow tool, an automation, the command line, or the resume of a
  paused run - so revoking a workflow while it is paused means it does not come back.
  The same rules apply as for tools: admins are never restricted, an empty selection
  means unrestricted. One-off workflows the agent designs on the fly have no saved
  identity to check; what governs them is the tool permission of the feature that
  builds them.
- **The coding agent now acts as the person who asked.** It runs as a separate process,
  and no identity crossed that boundary - so its file tools ran with the machine owner's
  rights for every caller. The caller's identity now travels with the task, and the
  coder's file tools are confined to that person's own workspace. The owner's own runs are
  unchanged, and the shell deliberately keeps full strength - controlling who may use the
  coder at all remains the admin's lever.
- **Each user's cloud accounts are their own.** Connected cloud storage - Google Drive,
  OneDrive, Dropbox, Nextcloud, iCloud - was addressed by user NAME, and any part of VAF
  that had no name to give fell back to the machine owner's. On a shared installation that
  meant another person's request could list, read and download from the owner's cloud
  accounts using the owner's own login. Credentials are now addressed by the account they
  belong to, end to end, including when a login token is refreshed. Existing connections
  keep working and move across by themselves the first time they are used; the owner's own
  accounts are unchanged. Cloud downloads follow the same rule: they used to land in the
  owner's Downloads folder no matter who asked, and now arrive in the requester's own
  space.
- **The OAuth client secrets get the same treatment as the API keys - and stop travelling
  to the browser too.** The Google, Microsoft, Dropbox and GitHub client secrets in
  Settings showed their stored value to any admin session and echoed it back on every
  save - the same mechanism that corrupted a stored API key. They are now write-only like
  the keys: the field shows a partial glimpse and locks until you choose to change it,
  deleting is its own confirmed action, and what never leaves the server cannot be echoed
  back into it. One consequence to know: clearing such a field and saving no longer
  removes the secret - the delete action does, deliberately.
- **A stored key field shows you which key it holds.** "Key stored" answers whether, not
  which - so each field with a stored key now shows a recognisable glimpse in grey: the
  first characters, a run of dots, the tail (like `vaf_live_q0``...``Ab4d`). It is a display
  built on the server and deliberately partial; the key itself still never travels to the
  browser, which is what keeps the save round-trip incapable of corrupting it.
- **Your API keys no longer travel to the browser at all - and a Settings save can no
  longer corrupt a stored key.** The Settings page used to receive the key values still
  sitting in the configuration file and showed them as dots; saving any setting then sent
  them back, and VAF stored that echo as if it were the key. For most keys the echo
  happened to be identical and nothing broke; for one whose on-disk form was encoded, the
  stored key was silently replaced by its encoded shell and every request with it would
  have been refused - found because one key field looked different from the others. Key
  fields now always arrive empty (the stored value is confirmed, never displayed), the
  save response no longer carries secrets to anyone, and the round trip is pinned by a
  test that replays it against the real save route.
- **"Fetch models" works again for a stored key.** It used to read the key from the form
  field, which is empty by design now; the server resolves the stored key itself, and the
  form only needs a value while you are typing a new one.
- **A new or changed API key is checked against the provider when you save it.** Until now
  a wrong key looked exactly like a right one until the next message failed, and the answer
  arrived as a chat error with nothing connecting it to the screen that caused it. Saving a
  key you entered or changed now asks the provider whether it works, and reports back next to
  that key. A refused key says so with the provider's own error number and is left in place
  for you to correct; VAF does not delete it, because a check is not a revocation. A provider
  that cannot be reached is reported as exactly that and never as a bad key - an outage or a
  rate limit says nothing about what you typed, and treating it as a verdict would send you
  chasing a key that was never the problem.
- **Settings no longer closes when you save.** Saving applies your changes; closing the
  window is your decision, with Escape or a click outside. They used to be the same action,
  which also meant a key check had nowhere to report to.
- **You can now actually delete an API key, and Settings shows you which keys are stored.**
  Clearing the field and saving did nothing: a blank value has always meant "the form did not
  re-send this", which is what stops a half-filled page from wiping a key, so there was no way
  at all to remove one. That matters more than it sounds, because the usual reason to delete a
  key is that it leaked - and the interface reported success while the key stayed live. Each
  key now has its own delete action, and it removes the key from both places it can be stored,
  in the order that matters: get it wrong and the next use quietly restores it from the older
  location. If any part of the removal fails, you are told the key is still live and should be
  rotated at the provider instead of being told it worked.
- **Settings tells you again which API keys are stored.** When keys moved out of the plain
  configuration file into the encrypted store, the key fields in Settings went blank and
  stayed blank - a configured, working key looked exactly like no key at all, on the one page
  where you go to check. Each field now says whether a key is stored, without ever showing the
  key itself, and says so honestly when the stored keys cannot be read rather than reporting
  "none".
- **Changing your API key now takes effect without restarting VAF.** Saving a new key
  appeared to work while the old one kept answering, and only a full restart applied the
  change - which meant a key you had just deleted or replaced could still be in use, with
  nothing on screen saying so. VAF runs several chat workers side by side, and the change
  was being applied to one of them; the rest carried on with the key they started with. It
  now reaches every one of them. Agents belonging to an application that embeds VAF are
  deliberately left alone, so a key passed in by that application is never replaced by the
  one on the machine.
- **The protection shield no longer reports "no anomalies" on a day something was
  blocked.** The shield summarised the state of each protection module but ignored what had
  actually happened that day, so a stopped high-risk skill installation and an admin
  overriding a security refusal appeared only as two small numbers in the Skills panel,
  next to a large green "no anomalies". Today's blocked attempts now appear at the shield
  itself as a clickable badge that opens the full list, and an overridden refusal or a
  worsened re-scan raises the shield to amber. A block on its own stays green with a count:
  a blocked attempt is the protection working, not a problem.
- **The security event list now names every kind of event.** Half of what VAF records -
  every skill event and both mail events - had no label anywhere in the interface and would
  have been shown as a raw internal identifier instead of a description.
- **A broken stored API key now says so instead of looking unconfigured.** Until now any
  problem reading a key produced an empty value, and an empty value means "not set up" to
  everything that asks - so a damaged key silently dropped you to the local model, or to no
  provider at all, with nothing to indicate why. It now reports the problem for the provider
  you are actually using, at the moment it is used: a damaged entry for a provider you never
  touch cannot take the installation down. If you see it, re-enter that key in Settings.
- **Your API keys move out of the plain configuration file.** They were stored in
  `config.json`, scrambled but not encrypted - readable to anyone who opened the file, and
  carried along in any backup or screenshot of it. They now live in the same encrypted store
  as your mail, GitHub and cloud logins. Existing keys keep working and move across by
  themselves the first time they are used; nothing is deleted, so going back to an older
  version still finds them. Worth being precise about what this buys: unless you set a master
  passphrase, the protection is still only your file permissions - the win is that the secret
  is no longer sitting in the file everything else reads.
- **Cloud storage no longer treats everyone as the machine owner.** The assistant's cloud
  tool worked out who was calling from a setting that is never actually set, so it always
  concluded it was the owner - whoever was really asking. On a shared installation that
  meant anyone could list, read, download and upload through the owner's connected Google
  Drive, OneDrive, Dropbox, Nextcloud and iCloud accounts, using the owner's stored logins.
  It now uses the identity of the actual caller. In a normal chat that means you see your
  own connected accounts and nobody else's. When the assistant reaches the tool through its
  file assistant, your name is not passed along yet, so you see nothing rather than someone
  else's accounts - safe, but not yet complete; the remaining half is in progress.
  Single-user installations are unaffected.
  In the same place, two file paths are no longer taken at face value: asking the tool to
  upload a local file, or to fetch one back out of the sync folder, is refused when the path
  points outside your own area instead of copying the file somewhere the assistant can serve
  it back.
- **A tool run without a username no longer guesses whose data it is.** Some runs carry no
  username - after switching into a chat that has none stored, from the command line, from a
  workflow started without one - and the assistant used to fill in the literal name "admin".
  On any installation where you registered under a different name, that named nobody: things
  stored under a name (your cloud accounts and their sync folder, your GitHub account,
  credentials in the lanes that have no scope) are filed under the name you registered with,
  so the same person could be recognised in the web interface and be a stranger with an empty
  account list one lane over. The missing name is now resolved from the user the run belongs
  to: your own runs get your account, and a run belonging to somebody else gets their own
  separate space instead of yours. Installations with a single user called "admin" behave
  exactly as before. Some traces of the old behaviour remain visible in an old credential key
  shape the store keeps reading, and in stored chat history filed under the literal name.
- **The macOS launcher works on any machine.** `launch_vaf.scpt` opened a hardcoded home
  directory belonging to one account, so it failed for everybody else. It now uses `$HOME`.
- **Examples in the assistant's own instructions no longer name one particular person.** The
  shipped prompt text used a real first name in its examples ("... said", "Tell ... to call me
  back"), which read oddly for anybody else and put a private name in a public repository.
  Examples, default display names and documentation now use neutral placeholders. A guard
  checks committed content for real home directories by shape, so the next one is caught
  before it is published rather than after.
- **Code search and the linter stayed inside your own files.** Both took a path and worked on
  it without asking whether it was yours. Code search looked contained - it clamps a path back
  into the project it was given - but that only applies to the copy the coding assistant uses;
  the one in normal chat is created without a project, so the clamp did nothing and an absolute
  path was searched and its matching lines returned. Both now confine to the calling user's
  own files, on every path they can be called from.
- **On a shared machine, one person could reach into another's chat session.** Three of the
  commands the browser sends act on a session named in the message itself, and they never
  checked that the session belonged to whoever sent it. The consequences differed: the
  attachment panel of another user's chat could be emptied or filled with someone else's
  documents, which they would then find there and could unknowingly teach to VAF; a voice call
  could place a task into another person's chat queue; and a Stop press could interrupt
  another person's running answer. All three now refuse to act on a session that is not
  yours, the same way the other commands already did.
- **Asking VAF to write a document can no longer put it outside your documents folder.**
  The tool takes a file NAME, but never checked that it got one: a name that was actually a
  path replaced the target folder entirely, so an absolute one wrote wherever it pointed. The
  file-type restriction did not help - it limits which formats may be written, not where. A
  name containing a path is now refused with a pointer to the tool that does take one, and
  the document lands in the calling user's own folder on a shared machine rather than
  wherever the name led.
- **Asking VAF to learn a document can no longer pull in files it protects.** This tool
  carried its own idea of which files were readable, and it disagreed with the one every
  other file tool uses in both directions: it allowed anything in your home folder, including
  SSH keys, `.env` files and VAF's own settings and credential store, while refusing ordinary
  files kept outside it. Learning is also the worst place for that to go
  wrong, because it keeps what it reads: the contents are summarised into long-term memory
  and stay searchable long after the conversation ends. The tool's private rule is gone and
  the shared one decides, which on a multi-user machine also applies the per-user boundary it
  previously skipped. Together with the document panel fix below, a protected file is now
  refused whether you ask to view it or to learn it.
- **Opening a protected file in the document panel no longer showed its contents.** The
  viewer read every file twice: once through the checked reader, and once raw to send the
  original bytes to the browser. Only the first read was checked, and its refusal was not
  acted on, so a file VAF protects for everyone - SSH keys, `.env` files, VAF's own settings
  and credential store - was refused in the text pane while the panel received the real file,
  and the tool reported it as opened. It is now one decision, taken before anything is read,
  and it covers both the protected locations and each user's own boundary.
  Its sibling, the document editor, now also reports honestly: it says it asked the
  interface to open a file, because whether the file is actually served is decided
  elsewhere and can still be refused. Claiming success for something that cannot
  happen is the same problem one level up.
- **The file assistant's own tools now follow the same rules as everything else - this time
  on both of its paths.** When you ask VAF to work with your files, it hands the job to an
  assistant that runs its own small agent, and that agent called its thirteen tools directly,
  bypassing the checks every other tool call goes through. An earlier entry said this was
  fixed; it was fixed for one of the assistant's two paths. The other one answers simple
  requests straight from the wording, without ever starting the inner agent - "how big is this
  folder", "rename this to that", "show me the structure" - and it kept calling five tools
  directly, including the one that moves files.
  Both paths go through the shared checks now, which also means that for the first time these
  actions appear in the activity log: previously the assistant could rename or write a file
  and leave no record of it anywhere. Your files were never less protected on that path - the
  per-user boundary applied throughout - but nothing recorded what happened.
  One thing reads differently as a result: when one of those actions fails, the message is now
  the same short one used everywhere else instead of a hand-written sentence, so the assistant
  says what it is about to do beforehand ("Renaming X to Y...") to keep the context.
- **A failed web search could answer with someone else's memories.** When every web provider
  is unavailable, VAF falls back to your own long-term memory - but on a server running
  several conversations at once, it picked whose memory to read from a process-wide value
  that any of them could have overwritten a moment earlier. The answer could therefore come
  from another person's memories. The tool is now told who is asking, the same way every
  other per-user tool already was. Single-user installations were never affected, and a
  request with no user attached was already refused rather than answered broadly.

## [0.1.0a19] - 2026-07-27

### Added
- **Workflows can now run as the person who started them.** Until now a saved workflow always
  acted as the machine owner, no matter who ran it - so anything it did with memory, mail,
  messages, the calendar or contacts was filed under the owner's account. The new setting
  `workflow_identity_injection` switches this over: leave it at `legacy` for the previous
  behaviour, set it to `declared` to have workflows carry the identity of whoever started
  them. Off by default, because it changes where a running workflow's data goes.
- **You can now choose who a skill is for.** The skill editor has a visibility setting -
  everyone, only you, or named people - the same one custom tools already had. It decides who
  sees the skill and who can use it, and now also whose agent may open the files bundled with
  it. Existing skills keep the setting they have; a new one starts as visible to everyone.
- **Tools you write yourself can now be told who is calling them.** VAF only handed the
  current user's identity to its own built-in tools, which meant a tool added through the
  framework could not tell one person from another - and the documentation could only warn
  about it. A tool now states what it needs and receives exactly that, whether it ships with
  VAF or you wrote it. Nothing changes for existing tools.
- A completely new built-in mail client, and it is now the only one
  (design doc `docs/integrations/EMAIL_CLIENT.md`). VAF has a real mail engine:
  a per-user local mail store (SQLite with full-text search over
  subject/sender/recipients/BODY, threaded conversations, encrypted-at-rest
  cached bodies, configurable retention with headers kept forever) and an
  RFC 4549 incremental IMAP sync engine (UID-based, UIDVALIDITY-safe, batched
  fetches, IMAP IDLE push on the inbox plus periodic sweeps, native Gmail
  thread/label handling). The mail window is a three-pane client: folder sidebar
  with unread counts and collapsible labels, conversation view, HTML mail
  rendered sanitized in a sandboxed frame with remote images blocked by default
  for tracking protection, attachment download, and search over message bodies.
  Mail opens offline from the local store. New permissive-licensed dependencies
  (in the `mail` extra): IMAPClient (BSD-3-Clause), nh3 (MIT),
  zstandard (BSD-3-Clause), listed in `THIRD_PARTY.md` and the About tab's
  third-party license list.
- Mail can be acted on, not just read: read/unread, star, archive and trash in
  the mail window (trash-only delete semantics, nothing is ever expunged),
  reply/reply-all/forward with proper quoting and threading, compose with a
  15-second undo window (the mail is held locally and can be withdrawn before it
  leaves the machine), and automatic filing of sent mail into the Sent folder.
  Changes apply locally first and replay to the mail server through a durable
  operation queue once `mail_engine_write_enabled` is on. The agent gains
  `reply_mail`, `forward_mail`, `archive_mail` and `delete_mail` tools (all
  excluded from the front-office contact lane by design).
- Outgoing mail is sent natively over SMTP (password or OAuth XOAUTH2), so it no
  longer depends on the provider REST APIs; an ambiguous failure after the message
  is handed to the server is parked, never re-sent, so a mail is never delivered
  twice. IMAP/SMTP server presets added for GMX, web.de, T-Online and outlook.de
  addresses.
- Blocked remote images in mail can now be loaded on explicit opt-in through
  a privacy proxy: the sender's server never sees the reader's address, SVG
  and non-image responses are refused, and refused hosts are logged to the
  security event log.
- The mail client warns about suspicious (possible phishing) messages: the
  conversation list shows a warning badge and the reader a warning banner on mails
  the agent's phishing filter would hide, so nothing dangerous is silently
  surfaced only to the human.
- The mail client shows which mail has already been answered: a reply marker with
  the date in the reader and a marker on answered conversations in the list, so a
  reply is not accidentally sent twice.
- Gmail-style categories: a category chip on Promotions/Social conversations and a
  relabel picker in the reader. Relabeling also teaches the category - VAF
  remembers a rule for that sender and labels every other mail from them, past and
  future. All of it is local; nothing is changed on the mail server.
- Mail Composer: the compose window can now write the reply for you, or rewrite
  what you typed. The window is wider now, with the message on the left and the
  Composer beside it on the right: a chat where you say what the reply should say,
  ask for changes ("shorter", "now more formal") and it refines what it just wrote,
  with Stop while it writes and an Undo that restores your text exactly. Draft and
  Rewrite sit in the footer next to Send. It puts a suggestion in the text field and stops there, so nothing is
  ever sent without you reading it and pressing Send, and it tells you how much of
  the conversation it actually read. It refuses to draft from a message flagged as
  possible phishing, and it can use what VAF remembers about you when you say what
  the reply should be about. New: a light-mode button in compose, so you can read
  the draft the way the recipient will. Admins can turn all of it off or change how
  much of a thread it reads (`mail_composer_*` settings). If the local model is not
  running yet, it is started for you instead of asking you to do it, with a note
  while it loads. Admins can additionally allow the Composer to quote older mail
  from other conversations (`mail_composer_mailbox_search_enabled`, off by default).
  The Composer knows what VAF remembers about you the same way the chat does, every
  time rather than only when you phrase a request a certain way, and it now writes a
  complete message (greeting, the point, a closing) in the language of the mail it is
  answering instead of a single bare sentence. It also tells apart which messages in
  a conversation you wrote and which the other person wrote, and writes in YOUR tone
  by following how you replied earlier in the same thread; if there is nothing of
  yours to go by it says so and stays neutral instead of inventing a style.
- The mail window's gear opens a built-in account panel: see your mail accounts,
  connect a new Gmail/Microsoft or IMAP account (with a Test button), reconnect,
  verify a connection, rename an account, toggle auto-sync, and remove an account.
  Removing an account that also powers your Calendar keeps it connected for
  Calendar.
- Library embedders can now set the agent's persona directly:
  `Agent(system_prompt="...")` replaces the on-disk "Soul" in the system prompt
  for that instance only, while the engine's technical instructions are kept.
  Previously the persona was a global on-disk file with no public API.
  Documented in EMBEDDING.md with a runnable example (examples/06_custom_persona.py).
- EMBEDDING.md now has a "Sub-agents as a library" section explaining that
  sub-agents run inline in a bare library process, while their windowed/async
  modes and the coder's sandbox need the full product's services.
- The skill scanner module gained a dependency-free content-hashing facility
  (SHA-2 and SHA-3): `hash_bytes` / `hash_text` and a deterministic,
  tamper-evident `hash_skill_folder` fingerprint, on a strong-only algorithm
  allow-list. Available for later integrity checks; not yet wired into the
  scan result.

### Changed
- The wording around mail image loading was corrected in the documentation. It
  protects the reader's browser identity (no cookies, referrer or browser
  fingerprint reach the sender) but it does NOT hide the reader's IP address, and
  it does not stop open-tracking: a tracking pixel's address is unique per
  recipient, so loading it still tells the sender the message was opened and when.
  Blocking images by default is the protection. `docs/integrations/EMAIL_CLIENT.md`
  now states both halves for privacy reviews.
- Server-side mailbox changes (read/unread, archive and delete replayed to the
  mail server) stay behind `mail_engine_write_enabled`, still off by default. Note
  that SENDING is deliberately NOT gated by it: a queued mail must be able to
  leave, so the agent's `reply_mail` and `forward_mail` verbs are live regardless
  (each still passes the high-risk send gate).
- Removed the dead Apple OAuth lane (provider entry, config keys
  `email_oauth_apple_client_id`/`_secret`, admin settings inputs and their
  locale strings, a dead sign-in URL branch in the setup wizard): Apple
  offers no OAuth mail API. iCloud Mail continues to connect via IMAP with
  an app-specific password, unchanged.

### Removed
- The `batch` tool is gone. It was listed as a Coder tool but could never be called: it was
  not registered for the main agent and not part of the Coder's tool set either. What it
  offered - running several tools at once - is what the agent already does in a single turn.
- The old mail dashboard and the separate email setup wizard are gone. Everything
  they did is in the mail window: reading mail, and the account panel behind its
  gear for connecting, reconnecting, testing, renaming, auto-sync and removing
  accounts (including the IMAP and SMTP server overrides the wizard offered, and
  the same hiding of sign-in buttons for providers an admin has not configured).
  The Overview security page reads its mail data from the new engine now.
- The old mail REST endpoints under `/api/email` are gone (message list, search,
  body, categories, category change, sender-rule backfill and the per-account
  sync). Mail is served from `/api/mail`. Sign-in and account management under
  `/api/email` are unchanged, because Calendar and the Connections page use them.
- The 30-minute background mail sync was removed. The mail engine's own sync
  (continuous, with push updates) is now the only one, so every mailbox was being
  fetched twice.
- The old mail transport (its own IMAP, Gmail API and Microsoft Graph paths for
  fetching, reading and sending) is gone; the mail engine handles all of it. An
  account that has not been connected for the engine can no longer send through the
  old path - it is refused with a message telling you to reconnect it, rather than
  failing quietly.
- The `mail_engine_v2_enabled` setting is gone. The mail engine is simply how mail
  works now, so there is nothing left to switch. `mail_engine_write_enabled` is
  unchanged and still off by default: it remains the separate switch for changes
  written back to the mail server (read/unread, archive, delete).
- Mail: with several accounts, only the first one kept its labels and "answered"
  markers when moving to the new mail engine. Every account now carries its own
  over, including labels belonging to mail that only arrives on a later sync, and
  it also happens when you sync manually rather than waiting for the background
  sync.
- Mail: adding an account with a password for an address that is already connected
  through sign-in is now refused with an explanation, instead of silently replacing
  the connection and disconnecting that account's calendar.
- The Logs page no longer dead-ends when debug logging is off. Three fixes
  from a live incident on a macOS install where a legacy config had
  `debug_logs_enabled: false`: the chain badge no longer claims "Chain
  intact" for an empty or missing timeline (an empty chain is vacuously
  intact; it now shows a neutral "No data yet" state), the timeline empty
  state is localized instead of hardcoded English, and the Debug Logs
  switch is back in Settings → Advanced - the empty states tell the user to
  enable it, so the switch has to exist in the UI.
- Corrected stale embedder docs: the FAQ said VAF was "not yet on PyPI" (it is,
  as a prerelease) and three docs claimed "no async API" despite the shipped
  `run_async`.

### Fixed
- **A tool the agent had just written for itself only worked after a restart.** When the
  agent creates a new tool, it is supposed to become usable straight away in the same
  conversation. The step that makes it live was never actually reached, so the tool was
  written correctly but stayed invisible until VAF was restarted next. It now appears
  immediately, as intended.
- **Mail did not work at all on some Windows and macOS installations.** The search index
  used a feature that needs a recent SQLite, and it was created together with the rest of
  the mail database - so on an older SQLite the whole mail store failed to be created, not
  just search. Affected setups running Python 3.10, which still ships an older SQLite on
  those two systems. The index now checks what the system supports and falls back to a form
  that works everywhere; searching, sorting by relevance and deleting behave the same, the
  index just takes a little more disk space there.
- **A workflow could open a skill somebody kept private.** Skills have a visibility setting,
  and in chat the agent respects it: a skill shared only with certain people stays closed to
  everyone else. A workflow step, however, was never told who had started it, so it acted as
  if an administrator were asking and could read any skill on the machine. Workflow steps now
  carry the identity of the person who started them. This closes the case for workflows you
  run from chat, for automations, and for a resumed workflow; saved workflows started from the
  library are covered by a broader fix still in progress.
- **The per-user tool and workflow selection said more than it does.** When creating or
  editing a user you can pick which tools and workflows they should have, and the wording read
  as though the choice were already in force. It is stored but not yet checked while the agent
  runs, so "Read-only" did not actually stop anything. The dialog now says so plainly instead
  of implying a restriction that is not there yet. The selection itself is unchanged and is
  kept for when it takes effect.
- **The "Add MCP server" dialog needed scrolling to reach its last field.** Test connection
  moved down next to Cancel and Save instead of occupying a row of its own, the empty band
  it left behind is gone, and the dialog is taller.
- **The failover setting looked navy in dark mode.** Its slider track and the step markers
  now use the same neutral tone as the other controls.
- **The agent could read files belonging to other people using the same VAF.** Writing was
  already confined to your own project folder, but reading was not, so the agent could open -
  or simply list - files in someone else's folder or elsewhere on the machine. Reading is now
  confined the same way. It is deliberately a little wider than writing: skills shared with
  you stay readable, because a skill may point the agent at its own reference files. A skill
  somebody kept private is not, which it previously was. Your own files, your uploads, and
  everything an administrator does are unaffected.
- **The local model stayed on your graphics card after switching to a cloud provider.** VAF
  already frees it in that situation, but two rules were holding it back for users who could
  never benefit from it. Background thinking asked to keep the model without checking that
  the next thinking run would run in the cloud and never touch it - and it asked that not
  only while a run was in progress but also while one was merely due, which stays true for as
  long as you are away. Separately, a setup with a local voice model reserved it whenever a
  VAF window was open, although a call loads what it needs when it starts. Roughly 3.4 GB now
  comes free on a provider switch. A call, a running task or a sub-agent still keeps the
  model; only the first call after an idle period waits briefly for it to load, which the
  call window already shows and handles.
- **Changing your API key only took effect after a restart.** Switching provider or key is
  handled in one place inside VAF, but the three parts that receive a settings change had
  each rebuilt that step by hand and each left pieces out. A key change with the provider
  unchanged was ignored entirely; VAF could keep telling the model, and the logs, that it
  was still the previous one; and if the new provider could not be reached, VAF could be
  left with no working model at all until you restarted, instead of simply staying on the
  one that was working. All three now go through the same implementation.
- **One of the agent's two ways to change a file skipped the per-user boundary.** When
  several people use one VAF, each is confined to their own project folder. Asking the
  agent to rewrite a file respected that; asking it to change a few lines in a file did
  not, so a file belonging to someone else could be altered, and a failed attempt even
  returned a few lines of that file. Both ways now enforce the same boundary. Nothing
  changes for a single-user VAF, for administrators, or for files of your own.
- **The agent could open VAF's own settings folder, where the keys live.** Asking the agent to
  read a file gave it access to the folder VAF stores itself in - the file holding every
  connected API key and the sign-in secret, saved copies of it, stored browser logins, voice
  profiles and every conversation. A key read out of there keeps working outside VAF and
  cannot be taken back, so the folder is now closed to the file tools for everyone, the owner
  of the machine included. Skills and workflows you create still live there and stay readable.
- **A second administrator was treated as an ordinary user by the file tools.** VAF lets you
  create more than one administrator, and they can do everything an administrator does -
  manage users, see every conversation, read the full settings. But when they asked the agent
  to work with a file, or opened a file from a chat, VAF checked only whether they were the
  very first administrator account and otherwise confined them to their own project folder.
  Administrators are now recognised as administrators everywhere. Nobody else gains access:
  ordinary users stay confined to their own files exactly as before.
- **A single device could lock everyone else out of signing in.** VAF blocks an address after
  repeated failed logins, but behind the built-in HTTPS proxy every device on the network
  counted as the same one. Five wrong passwords from anywhere blocked everybody for the next
  quarter of an hour, and the block could not be traced to whoever caused it. Attempts are now
  counted per device.
- **Conveniences meant for the computer VAF runs on reached the whole network.** Signing in at
  the machine itself gives a long-lived session, and an administrator there does not have to
  re-enter a two-factor code when the session refreshes - both because the person is physically
  at the keyboard. Behind the proxy every device on the network looked like the machine itself
  and received the same treatment. VAF now recognises which device is really the local one; the
  computer you run VAF on keeps both conveniences unchanged.
- **Your login history and the security log showed every device as the local computer.** Sessions
  and failed attempts from the network were recorded as coming from VAF's own machine, so it was
  impossible to see where a login had actually come from. Both now show the real device.
- **Devices on your network could reach VAF without logging in.** When network access was
  switched on, VAF's built-in HTTPS proxy passed every device to the backend as if it were
  the computer itself, so any device on the same network could open pages and APIs without
  a token, and even an expired or wrong token was let through. That included user
  management, the log viewer and the security dashboard. VAF now recognises which device a
  request really came from and asks anyone who is not on this computer to sign in.
  Signing in on another device, the desktop app, and first-time setup all keep working
  exactly as before. **After updating, restart VAF so the fix takes effect.**
- **A device on your network could pretend to be this computer.** The proxy added its own
  sender information alongside whatever a device had claimed, and the backend read the
  claimed value first. A device could therefore present itself as local and skip the check
  that ties a connected account (for example Gmail or a cloud drive) to the person who
  started the connection. The proxy now removes any claimed sender information before
  adding its own.
- Mail: an account the new mail engine does not sync yet (a Gmail or Microsoft
  account that has not completed the IMAP sign-in) is no longer reported as an
  empty mailbox. It stays visible in the mail client with a hint that it needs to
  be reconnected, instead of silently disappearing from the account list and
  reading as deleted.
- Mail: turning a per-account "Auto-sync" toggle off now actually stops that
  account from being polled by the new engine. It previously kept syncing (in fact
  more often than before), so the switch did the opposite of what it said.
- Mail: removing an account from Mail while keeping it connected for Calendar no
  longer resurrects itself - the background sync used to re-import the messages the
  removal had just deleted.
- Mail: Gmail's category tabs (Promotions, Social, Updates, Forums) are detected
  again. They are saved searches rather than labels, so reading them from the
  message labels never worked and every mail was filed as Primary; the category is
  now resolved through Gmail's own search over IMAP.
- Mail: a sender rule learned by relabelling a mail now also labels NEW mail from
  that sender. Previously the rule only re-labelled existing mail, so the promise
  that "future mail from this sender gets the same label" did not hold.
- Mail: connecting a Google account now grants everything the mail client needs in
  ONE sign-in. Previously a freshly connected account could not be used by the mail
  client at all and always needed a second, separate "Upgrade to IMAP" step. That
  separate step is gone: an older account that still needs it simply offers
  "Reconnect", the same sign-in as connecting. It preselects the account in
  question (so with several accounts the right one is reconnected), works in the
  standalone mail window, and the panel refreshes by itself once access is granted.
  Microsoft still needs a second sign-in for mail, because Microsoft issues mail and
  calendar access as two separate tokens that cannot be combined.
- Mail: accounts connected with a password or app password no longer show a
  permanent "IMAP not ready" warning - they speak IMAP by definition.
- Mail: opening a label or other non-inbox folder now fetches it on first open.
  Those folders sync on demand by design, but nothing ever requested them, so they
  stayed permanently empty.
- Mail: a message that could not be sent is no longer reported as sent. Such a
  message is held in the outbox and the mail client now shows a warning about it,
  with "Try again" and "Discard" so the warning can actually be cleared once the
  cause is fixed.
- Mail: the message list no longer keeps showing another folder's mail when a
  folder fails to load, and the list now names the folder it belongs to.
- Mail: the unread counts next to the folders update again. They were read once
  when the mail window opened and then never changed, so new mail, reading a
  message and syncing all left them stale.
- Mail: pressing Sync now reports when an account could not be synced, instead of
  spinning briefly and saying nothing.
- Mail: a new Gmail or Microsoft account can be connected straight from the mail
  window's account panel.
- Mail: three kinds of hidden tracker in HTML mail are now caught and reported.
  A CSS-escaped `url(`, `image-set(...)` and `src(...)` inside an inline style
  slipped past the filter with the third-party address intact, and the mail was
  shown as if nothing had been blocked. The browser's own rules stopped the actual
  request, so no address was ever contacted, but the warning was missing. Blocked
  styles are now counted, so the "external content blocked" notice is honest.
- Mail: image loading now uses the network proxy configured for the machine
  (`https_proxy`/`http_proxy`, honouring `no_proxy`). In managed networks that
  forbid direct internet access, loading images previously just failed, and the
  organisation could not see or filter what the mail view fetched.
- Testing or verifying a mail account no longer freezes the whole backend. The
  IMAP credential test/verify and OAuth token-exchange endpoints ran blocking
  network IO directly on the uvicorn event loop, so every other API request hung
  until the provider round-trip finished. All provider IO in the email routes now
  runs in worker threads.
- The Logs window no longer crashes with a blank "This page couldn't load"
  after a rebuild. A calendar-follow effect had been placed after the modal's
  early return, so the number of React hooks changed when the window opened
  (React error #310); the effect is back above the guard.
- A timed-out or stopped sandbox execution is now actually terminated inside
  the container. Slim sandbox images ship no pkill, so the old kill path
  silently did nothing: a timed-out package install kept running blind,
  finished after the workspace cleanup and left a 229MB orphan directory
  behind. The kill is also scoped to the single run's process tree, so it can
  never take down another user's concurrent sandbox execution in the shared
  container.
- The Logs window now follows the calendar: after midnight the audit chain,
  hero and activity panels advance to the new day as soon as it has events,
  instead of silently continuing to show yesterday (fresh tool runs appeared
  to be missing from the audit chain). Date handling also switched from UTC
  to local time, which had marked the previous day as "Today" until 02:00
  CEST - the same fix applies to the sidebar chain-alert probe. Explicitly
  picking an older day in the date selector still pins it.
- Running the test suite on a development machine no longer writes fake
  "Message sent via Discord" entries into the live Activity feed and channel
  history. A unit test executed the real send tool with only the network call
  mocked, so its bookkeeping side effects (activity notification, channel
  store row, outbound session stub for a placeholder recipient) landed in the
  real stores on every run - hundreds of entries had accumulated and looked
  like a compromise at first glance. No message ever left the machine; the
  test now isolates all side-effect stores and the debris has been removed.

### Security
- Whether an account counts as an administrator is now decided the same way everywhere.
  The check that lets administrators use restricted tools compared the stored role
  letter for letter, while the check that decides which files may be opened accepts the
  role regardless of capitalisation. An account whose role was stored as "Admin" would
  therefore have been given administrator access to files while still being refused
  restricted tools. Accounts created or edited in VAF always store the role in lower
  case, so this could not be triggered through the app; the two checks now share one
  rule regardless.
- Mail: a request carrying a user name but no user scope could be served from the
  local administrator's mailbox by the new mail engine (read and write). Such
  callers now stay on their own mail store, matching the isolation rule the other
  two layers already enforced.
- Outgoing email attachments now resolve their file paths under the same
  per-user filesystem jail that already protects librarian and file-write
  operations: in network mode a non-admin user's agent can no longer attach
  files outside that user's own data (previously only the static block list
  applied). The jail computation is now a single shared helper used by all
  three tools, with a regression test.
- The agent-side phishing-filter settings
  (`email_agent_phishing_filter_enabled`, `email_agent_phishing_score_threshold`,
  `email_agent_trusted_sender_domains`) and the IMAP/SMTP SSRF opt-out
  (`email_allow_private_hosts`) are now registered config keys and
  admin-write-only. Previously they were read with inline defaults and, in the
  SSRF case, any LAN user could have toggled the guard for the whole instance.
- Mail transport logs now mask account ids consistently (first 3 characters)
  and truncate provider error-response bodies; several error paths used to
  write the full account address and full API response into the always-on
  domain log.
- Dependency updates closing all 13 open Dependabot alerts: Next.js 16.2.9 to
  16.2.11 (nine advisories, incl. SSRF in Server Actions and in rewrites,
  middleware bypass with Turbopack, Server Action DoS and cache confusion),
  sharp 0.34.5 to 0.35.3 (inherited libvips CVEs, now forced via an override
  because Next.js still pins the vulnerable minor), brace-expansion 1.1.14 to
  1.1.16 (exponential-time expansion DoS, dev-only), DOMPurify 3.4.11 to
  3.4.12 (`CUSTOM_ELEMENT_HANDLING` bypass) and protobufjs 7.6.4 to 7.6.5 in
  the WhatsApp bridge (infinite loop in `.proto` option parsing).
- postcss is now forced to at least 8.5.12 via an override (arbitrary file
  read through attacker-controlled `sourceMappingURL`): Next.js exact-pins a
  vulnerable 8.4.31 copy, which the override dedupes away to a single
  patched node. Build-time tooling only, no runtime exposure.
- The session token that rides in the WebSocket handshake URL
  (`/ws?token=<jwt>`, unavoidable because WebSockets cannot send an
  Authorization header) is now masked to `token=***` in the server access log
  instead of being printed in full. A live, short-lived admin token could
  otherwise be replayed by anyone who read the terminal or the tray log. The
  same mask also covers `access_token` / `api_key` / `password` if they ever
  appear in a logged URL.

## [0.1.0a18] - 2026-07-23

### Security
- The ephemeral sandbox fallback (used when the persistent vaf-sandbox
  container is unavailable) now carries the same hardening as the persistent
  one: all Linux capabilities dropped, no-new-privileges, and its own isolated
  bridge network instead of Docker's default bridge - previously two
  concurrent ephemeral sandboxes (for example of two different users) could
  reach each other over the shared default bridge.
- Sandbox pip installs are now temporary and per-run: packages requested via
  the tool's packages parameter (and even in-code pip installs, redirected via
  PIP_TARGET) land in the run's private directory and are deleted with it, so
  nothing accumulates in the shared sandbox container across runs or users;
  package specs are validated before reaching the shell and the shared pip
  cache no longer grows.

### Fixed
- **The desktop window no longer navigation-loops between the dashboard and the
  login page.** With an expired auth cookie but a still-valid token in browser
  storage, the server-side route gate and the login page's "already logged in"
  check disagreed forever: the gate redirected to /login, the login page bounced
  back, and the window reloaded in a tight loop until the browser engine
  throttled navigation and the UI froze. The login page's bounce decision now
  uses the same authority as the server gate (the cookie only), stale
  browser-storage tokens are cleaned up, and a circuit breaker stops any future
  redirect ping-pong after two bounces instead of trapping the user. The root
  desync is also removed on the backend: the auth cookie's lifetime is now
  always derived from the token's own expiry instead of hardcoded 30-day
  values, so a cookie can never outlive the session it carries.
- **The Linux desktop window no longer stays dead after a GPU-driver crash.** Two
  incidents aborted the whole tray process from inside the host-side GPU
  compositing path (once inside the NVIDIA GL library while resizing the window),
  which the existing renderer-only crash recovery cannot catch. VAF now disables
  the NVIDIA driver's threaded optimizations for the tray process on NVIDIA hosts
  (GPU acceleration and vsync unaffected; export `__GL_THREADED_OPTIMIZATIONS`
  yourself to opt out), and the Linux shell launchers run the windowed tray under
  a bounded restart supervisor: an abnormal exit restarts the app within seconds
  (at most 3 times per 10 minutes), while a normal quit or Ctrl+C never does.

- **Chat no longer shows phantom date separators around invisible rows.** Live
  system-step notes are spliced into the message list with the current time;
  when one landed inside an older conversation, the day-separator logic compared
  against the invisible row and painted misordered "day ended / continued"
  pairs (e.g. July 21 / July 22 / July 22 / July 21) between two messages from
  the same day. Separators now only compare messages that actually render.

### Added
- **Installed skills are re-scanned periodically (post-install tamper
  detection).** The security scanner already checks every skill at create,
  update, upload and editor-save time (verified end-to-end against synthetic
  malicious skills); what it could not catch was a skill whose files change on
  disk AFTER installation. A background worker now re-scans all installed
  skills every `skills_rescan_interval_hours` (default 5, `0` disables),
  updates their persisted scan results, and raises a security event when a
  skill's risk level worsened. Blocked installs and admin overrides are
  recorded as security events too, so the Overview's skills panel shows real
  numbers: skills by risk level as a live donut, threats blocked today, admin
  overrides, re-scan alerts, the riskiest skills, and the time of the last
  full scan - an installed high-risk skill turns the protection banner red,
  a medium-risk one amber. A skill that re-scans as high-risk after install is
  automatically QUARANTINED: it disappears from every agent path (skill list,
  the read/list tools, the system prompt) until an admin resolves it on the
  dashboard - either delete it, or, if it is a false positive, restore it,
  which requires re-entering the admin's 2FA code so a stolen session alone
  cannot re-expose it to the agent. The protection banner now names the actual
  worst cause (e.g. "High-risk skill installed", "Channel in permissive mode")
  instead of always attributing a red state to the audit chain. Clicking a skill
  in the panel opens a detail view that re-scans it live and shows exactly WHY
  it was flagged (the matched rules with category, message and snippet) next to
  the resolution actions. A medium-risk skill can be acknowledged (2FA): it
  stays visible and still shown as medium, but the banner returns to green -
  the admin has reviewed and accepted it. When security events were recorded
  today (blocked access, rejected senders, skill blocks), the Log Files section
  shows a pulsing count that nudges the admin to open the security log, and the
  Logs button in the main sidebar shows an unread notification dot for new
  security events, so a blocked login or a quarantined skill is noticed without
  the window being open. Both the sidebar dot and the in-window log badge are
  unread-based against a shared marker: they clear once the admin opens the
  security log and re-light only on a newer event, rather than sticking as a
  permanent count.
- Logs Overview: the last three panels are now live. "Background agent" shows
  the proactive agent per user across all scopes (active run, the question it
  is waiting on with channel and nudge state, time since the last run with the
  tools it used, and the recent asked/replied/done/declined question history) -
  an admin oversight view served by a new admin-gated `GET /api/thinking/status`.
  "Recent supervised activity" lists the newest hash-chained tool actions of
  the selected day with per-user attribution, derived from already-loaded audit
  data. "Active supervised units" shows the live sub-agent watchdog (agent
  type, owner, runtime, heartbeat) with a stuck marker for stale units.
- Security: the supervisor watchdog endpoints are now caller-scoped. Previously
  any authenticated user could list ALL running sub-agent units (including
  other users' task text) and kill them by task id; now non-admins only see and
  cancel units of their own sessions, while the admin keeps the full attributed
  watchdog view.
- Fixed a test-isolation gap where a test suite wrote synthetic scope
  directories into the real thinking-requests store; the debris surfaced as
  phantom users in the new Background-agent panel and has been cleaned up.
- **Security event log: blocked access attempts are now recorded and visible.**
  Rejected connection attempts (non-LAN IPs, requests without or with invalid
  authentication, rejected WebSocket handshakes), failed login/2FA attempts,
  and unauthorized messenger senders (Telegram/WhatsApp/Discord messages
  dropped by the pairing gate, recorded with channel, sender id and time)
  are written to a new `security` log (visible in the Logs window's file rail)
  and to a structured store behind an admin-only API. Never logs passwords,
  codes, or tokens; a flood throttle keeps hammering attackers from growing the
  log unboundedly. In the Overview dashboard every protection module is now
  clickable: a detail popup shows the module's live data - for the firewall
  module the deflected attempts of the day (with counts for blocked requests
  and failed logins), the live-inspected Docker network isolation - every VAF
  container with its network and published ports, shown independent of LAN
  mode; a port bound beyond loopback (LAN-exposed) flips the module and the
  banner to the warning state - plus a button that jumps straight into the
  security log's history; for the code sandbox the live-inspected container hardening; for the
  audit chain the verification facts; for user isolation the enforcement mode
  (fail-closed in server mode), memory-DB reachability, and admin-level
  operational metrics: a live-measured RAG search latency (real pgvector
  distance query), the isolated per-user memory stores by username with their
  entry counts, the total memory-DB size, and the per-user folder totals (how
  many isolated folders each user owns and their summed disk usage, with
  legacy folders in an explicit unassigned bucket) - an unreachable memory
  database now shows as an amber warning on the dashboard instead of failing
  silently. The channel-perimeter module answers "is someone unauthorized
  talking to my bot?": per messenger it shows enabled state, ingress mode,
  paired-sender count, last activity and today's rejected count, with the
  rejected senders listed in the detail popup - and an enabled channel running
  in permissive mode turns the module and the banner amber. The audit-chain
  popup additionally breaks the day's secured events down per user, making
  visible that all users share one tamper-evident chain the admin verifies.
  The phishing-shield module shows how many synced messages are flagged and,
  per flagged mail, WHY: the matched heuristics (provider spam category,
  punycode sender domain, urgency/social-engineering language, executive
  impersonation via free-mail, phishing wording patterns) as readable chips
  with the score, plus a note that flagged mail is hidden from the agent's
  tools but never deleted. The guardrails module shows what actually governs
  the agent: the gate switches (plan gate, confirmation gate, incident gates,
  channel tool restrictions - with a visible warning when loosened), the live
  tool inventory grouped by permission level (read/write/dangerous/system,
  admin-only, channel-blocked), and - previously invisible anywhere - the
  standing permissions from the trust store: which tools carry a permanent
  "always allow" and which directories are trusted. With that, every
  protection module of the overview is wired to live data.
- **Logs window: new "Overview" tab (protection dashboard).** The Logs window now
  opens on an antivirus-style overview summarizing VAF's protection mechanisms:
  an overall protection-status banner, eight protection-module cards (audit
  hash-chain integrity, code sandbox, LAN firewall, user isolation, held actions,
  channel perimeter, phishing shield, guardrails/tool policy), an audit-chain
  panel, a skills-scanned/threats-blocked panel, a background-agent panel, and
  security-posture sections. The audit-integrity card, the audit-chain panel,
  and the overall status banner are wired live: chain verification verdict,
  events-secured count, the newest chain links (updating every 5 seconds while
  the tab is open), the last secured event, and a per-day selector. The status
  banner is a worst-of roll-up with four states (green "no anomalies", amber
  "attention", red "critical", grey "not measured") shown as a large shield
  motif filling the panel: it turns red the moment chain verification fails,
  amber on warning-level module states, and absent data renders as "not
  measured" instead of a false green. The code-sandbox module is wired live via
  a new admin-only status endpoint that inspects the real container: green
  while execution is container-enforced (running or on-demand), amber when the
  Docker daemon is unavailable (execution stays blocked, fail-closed) - the
  first warning input that can actually turn the banner amber. All remaining
  modules render an explicit
  "No data available" state; their bindings follow step by step.

## [0.1.0a17] - 2026-07-22

### Added
- **VAF is published to PyPI with every release.** Library users can install the
  framework with `pip install --pre vaf` (`--pre` is the recommended spelling while
  VAF is in alpha) and update it with `pip install -U --pre vaf`; the desktop/server installers
  keep working exactly as before. Publishing is tokenless (PyPI Trusted Publishing),
  and releases can be rehearsed against TestPyPI via a manual workflow first. The
  embedding guide gained a "Choosing a backend: local vs API" section, and the README
  now opens with a library quickstart.

### Changed
- **`vaf update` refuses to run outside a VAF source checkout.** On a pip-installed
  VAF (or a folder that is not a VAF source tree) the git self-updater now points to
  `pip install -U --pre vaf` instead of offering a git conversion that could have
  damaged the Python environment.
- **VAF's packaging moved to the modern Python standard (`pyproject.toml`).** The
  install commands do not change (`pip install -e .`, extras like `vaf[server]` and
  `vaf[all]` stay exactly the same), the license is now declared in the standard
  machine-readable form, and the `LICENSING.md` terms file ships inside the package.
- **Installing VAF with pip no longer runs platform setup scripts.** A plain
  `pip install -e .` used to silently trigger macOS/Windows provisioning scripts in
  some legacy flows; a pip install is now strictly a Python-package install. The
  desktop installers (`install.sh` / `install.ps1`) are unaffected and keep doing the
  full provisioning.
- **The `vaf` command now works on a minimal library install.** With only the base
  dependencies installed, `vaf --version`, `vaf prompt` and other light commands run
  normally, and commands that need optional components (for example `vaf run --web`
  or the Discord bridge) explain which extra to install (such as
  `pip install "vaf[server]"`) instead of crashing with an import error.

### Fixed
- **The document editor's page stays white in dark mode.** The DOCX editor's sheet is a
  rendering of real paper, and Print and PDF show exactly what is on screen, so it keeps its
  light appearance while the app around it goes dark.
- **A tool the local model tried to run no longer silently vanishes.** With the built-in
  local model, some tool calls (for example adding a calendar event) came out in a format the
  app recognised only for one specific model family. For every other local model the call was
  left as plain text in the chat and simply never ran, so nothing happened and no error was
  shown. That format is now recognised for any local model.
- **The app no longer runs its window on native Wayland, which could crash it on Linux.**
  VAF has always meant to run its window through XWayland (native Wayland conflicts with the
  browser engine and, with the GPU shared in-process, could freeze and then kill the app).
  That safeguard silently never applied on KDE and GNOME Wayland desktops, because those
  sessions set the display server themselves and VAF only filled in a value when none was
  set. It now sets it deliberately. If your system has no XWayland, VAF leaves your session
  alone instead of starting with no display at all, and `VAF_ALLOW_WAYLAND=1` keeps native
  Wayland if it works well for you. The choice is written to the startup log.
- **A slow or unreachable speech provider can no longer freeze the whole app.** The
  ElevenLabs voice/model catalog was fetched in a way that blocked the server's event loop,
  so opening Voice settings with a slow, unreachable or exhausted account could stall every
  request and the live connection for everyone until it timed out. It is now fetched without
  blocking, a failure is remembered briefly instead of being retried on every redraw, and two
  simultaneous requests share one lookup. The same blocking pattern was fixed in the email
  account verification and the Telegram dashboard.
- **The local model is no longer unloaded while it is still working.** When a longer task was
  running and you had not typed for a while, the app counted you as away and freed the model
  mid-task. The work then stalled behind failing retries and looked like a freeze, and two
  competing attempts to load the model again could collide. The app now keeps the model
  loaded whenever something is actually running, whether that is your message, a background
  helper or a live call. Loading is also serialized, so two parts of the app can no longer
  fight over it, a model that is merely still loading is waited for instead of killed, and
  stopped model processes are cleaned up instead of lingering.
- **A finished workflow is no longer labelled FAILED when it succeeded.** Workflows that run
  in their own process reported their outcome to the app, but the outcome itself was dropped
  on the way to the browser, so the panel fell back to showing a failure. Every step showed a
  green tick, the document was written, the chat said it worked, and the panel still said
  FAILED. The result now arrives intact, and so does the live output those runs stream.
- **The workflow panel no longer gets stuck showing a run that has long finished.** If the
  connection to the browser dropped while a workflow was running, everything that happened
  afterwards was lost, and the panel kept showing the last thing it had seen, with no way to
  close it. It now asks the app what the real state is when the connection returns, when you
  come back to the tab, and after a reload. If the run is over, the panel says so plainly and
  closes instead of inventing a result. It also has a close button now, which matters on
  phones where the panel covers the whole screen.
- **Terminal windows opened for background work now close again on Linux.** A window opened
  for a sub-agent or a workflow announced that it was closing and then stayed on a shell
  prompt forever, because a shell was started on top of the finished task. Windows now close
  when the work succeeded and stay open when it did not, so an error can still be read, and
  `--no-auto-close` keeps working as documented. On macOS this also depends on your Terminal
  profile setting for what happens when the shell exits.
- **No stray terminal window when the browser connection drops.** Work started from the app
  ran without a visible window only while a browser was connected. If the connection dropped
  during a long run, the next helper opened a terminal window on the desktop, and the app's
  Stop button could not reach it. The decision no longer depends on a browser being attached
  at that moment.
- **A busy workflow no longer floods and kills its own connection to the browser.** While a
  research step was running, its progress animation was forwarded to the browser frame by
  frame, hundreds of times per second. That was enough to drop the live connection in the
  middle of a run, after which the Workflow Runtime panel never advanced again and sat at
  the last state it had received. Progress output is now filtered and rate-limited before it
  is sent, in every place that forwards it, and the animation that produced it is no longer
  started when nobody is watching a real terminal. The separate workflow terminal keeps its
  colours and live display. Long, quiet steps are also no longer mistaken for stuck ones:
  the "no output" watchdog now allows a heavy step the time it is actually given.
- **A workflow that hands work to a background helper is no longer reported as crashed.**
  When a workflow step passes its work to a helper (writing a document, researching a
  topic), the workflow pauses and waits instead of finishing. Three of the places that read
  the result knew only "done" and "broken", so a perfectly healthy run was announced as
  failed while the work was still being produced, and the assistant apologized for a crash
  that never happened. A paused run is now recognized everywhere as still running, in chat,
  in the separate workflow terminal and in automation reports. Paused runs also remember
  which conversation they belong to, so switching conversations no longer discards them, and
  a run whose helper was the final step is now completed automatically in the app instead of
  only in the terminal client.
- **Telegram stays responsive while a voice message or file is transferred.** Sending or
  receiving audio, voice notes and documents held up the Telegram connection for as long as
  the transfer took, so other messages in the same chat had to wait behind it. Transfers now
  run out of the way of the message loop.

## [0.1.0a16] - 2026-07-19

### Added
- **The agent recovers when a call misreads your voice, instead of ignoring you.** In a
  room with other people, the voice check can briefly lose track of who is the owner (a
  guest speaking resets the in-call "this is the owner" bridge), and your own short command
  could be treated as a stranger's and silently dropped. Now, when a voice it cannot place
  is clearly talking TO the agent, it asks "did you mean me?" in the language being spoken;
  if your reply confirms your voice, it picks straight back up (and carries out what you
  asked). It never just goes quiet on your answer: if you say yes but it still cannot place
  your voice, it says so and asks you to confirm on screen or via your messenger so it can
  learn your voice and misread you less often. It never acts on an unconfirmed voice,
  and your voice profile is only ever updated from a confirmation you make yourself. The
  agent also no longer goes silent on a turn that was clearly addressed to it, and you can
  arm "talk to the other person" with a plain spoken command even when the local model is
  being unreliable.
- **In a voice call you can now ask the agent to talk to the other person with you.** By
  default the agent overhears the people around you and stays quiet, which is right when
  you are just talking to someone else. When you actually want it to take part - "answer
  her", "talk to my mother, she is asking you something" - it now does: it starts replying
  to the guest directly IN THEIR OWN LANGUAGE (even if your call started in another), and
  greets them. While it is talking with the guest it now follows the actual back-and-forth
  in the room - it is given the shared, spoken-aloud conversation (everyone's turns, in
  order, in whatever languages are spoken) so it can hold a real multi-person conversation
  instead of replying to each line in isolation; the guest still never sees your private
  information, only what was said out loud after you brought them in. It stays in that mode
  until you tell it you are done ("thanks, that's all"), the conversation goes quiet for a
  while, or the call ends.
- **The voice agent no longer refuses to speak a language it actually knows.** On a call it
  now replies in the language it is being spoken to, or the language you ask it to use,
  instead of being pinned to one language by its instructions - so asking it to talk to
  someone in another language works, rather than getting "I'm not fluent in that." Throughout, the guest can only be spoken to - the agent never shares your
  private information and never runs an action on a guest's request. On a call with more
  than one person the agent is also given the situation (who is present and the language
  being spoken) so it responds more naturally.
- **In a voice call, the agent now understands your answer to its own question.** When it
  asks you something, your next reply is treated as the answer even if it is brief ("yes",
  "at three"), instead of a stray remark; if you ask it to repeat, it re-asks the same
  question in your language. It is aware of the room: one-on-one it takes your reply
  directly; with other people around, a brief reply still counts, but a longer one is only
  treated as your answer when it is on-topic, so it does not mistake side-talk for an
  answer. Someone else can get a brief spoken reply when they say something clearly
  on-topic, but their words are never taken as your answer and can never trigger an action.

### Changed
- **Choosing the live-call voice model is now a dropdown, not a text field.** For a
  dedicated local model you pick from your already-downloaded models (the recommended
  Gemma 4 E4B is always offered and is fetched automatically when you select it); for an
  API provider you pick from that provider's models, with a refresh button to pull the
  live list, instead of typing a name by hand. Downloading local models stays in the AI &
  Model settings, and a value saved by an earlier version keeps working.
- **The agent stops asking "was that you?" on every unrecognized voice.** The speaker
  confirmation now fires in two more targeted cases: promptly when a voice it does not
  recognize CLAIMS to be you ("I'm NAME") - a spoofing check that asks you to confirm -
  and, far more rarely, on a borderline recording of your OWN voice (for the adaptive
  re-recognition). A stranger simply talking near the mic no longer triggers the
  question. The claim detection is multilingual.

### Fixed
- **The voice agent no longer speaks a stray fragment of its own reasoning.** Two cases are
  now covered: a model's thinking wrapped in a tag is always removed before anything is
  spoken - not only `<think>` but the variants other models use (`<thinking>`,
  `<reasoning>`, `<scratchpad>`, and more), whether the tag is closed or the stream was cut
  off mid-thought; and when a weak model leaks its thinking as PLAIN text (no tag) starting
  with a connective like "But we need to check: the user might be...", the filter now looks
  past a leading "but/so/and/well/okay" and drops the fragment to a short "say that again"
  instead of reading it aloud.
- **The voice agent recognizes you from the first words of a call.** The voice-recognition
  model is now warmed up the moment a call opens, instead of loading lazily on the first
  thing you say. During that cold load you were briefly treated as an unknown speaker -
  formal replies, a needless "did you mean me?", and it staying quiet on side-talk - until
  the model finished loading; now you are placed correctly from the start.
- **The agent no longer reads its own thinking out loud in a voice call.** When the local
  model announcing a delegated result leaked its internal reasoning into the text (for
  example while it was stuck), the voice could read that `<think>` reasoning aloud. It is
  now stripped before anything is spoken or stored, so you only ever hear the actual answer.
- **A voice-call reply in another language is now spoken by that language's voice.** When the
  agent answers in a different language than you spoke (e.g. Turkish in a German conversation),
  it uses a matching voice for that language instead of speaking it with your usual voice -
  whenever a voice for that language is available (a downloaded local voice, or any cloud voice
  provider). Otherwise it stays on your call voice.
- **Your spoken language is no longer mis-detected at the start of a voice call.** The call
  now tells the speech-to-text service the language from your profile up front, so a short
  first sentence in German is not transcribed as French (or another language) - which had
  the agent constantly asking you to repeat. It still notices if you genuinely switch
  languages mid-call.
- **The agent stops mistaking you for a stranger mid-call on short replies.** In a voice
  call, once it has clearly recognized your voice, a brief or noisy follow-up ("ja", "at
  three") no longer flips you to an unknown speaker and locks you out of acting - your
  recognition stays "sticky" for a while after it verifies you. A clearly different voice
  still switches immediately, so it does not weaken the guard against someone else acting
  as you.
- **The local voice model now starts on Apple Silicon Macs.** The recommended local
  German voice model (Gemma) could fail to start on macOS/Metal with only "Server failed
  to start" and an empty log. A fallback for the quantized cache existed but never fired:
  the non-debug log level wrote an empty log, hiding the very error the retry looked for.
  The fallback now runs whenever the first start attempt dies and an alternative exists,
  non-debug logging captures fatal errors again, and the retry scans the full log. So an
  empty or low-verbosity log can no longer silently disable it.
- **The one-time "what's new" alpha notice no longer reappears on every Settings close.**
  Closing Settings refreshed your time format and name, but also reset the seen-version
  gate, which re-showed the one-time notice each time you left Settings. It now refreshes
  those without touching the gate. (A deeper per-user workspace persistence quirk can
  still surface it once per app start; that root cause is tracked separately.)

## [0.1.0a15] - 2026-07-18

### Added
- **Voice call, the reflex features speak your language**: the agent's "did you mean me?"
  clarification and the cue words behind it now ship in about 35 languages (generated
  from English, so unlisted languages fall back to English), and it detects being spoken
  to across the major languages, not just German and English. What it hears (speech
  recognition) and what it says (the spoken reply) were already multilingual; this widens
  the small fixed phrases in between. The proactive chime-in was also tuned so it actually
  speaks up on genuinely on-topic overheard talk instead of staying silent.
- **Voice call, you can interrupt the agent**: while the agent is speaking you can now
  just start talking and it stops and listens instead of making you wait for it to
  finish. The microphone is opened with echo cancellation so it
  does not hear its own voice, and it only yields to a real, sustained interruption (a
  brief noise will not cut it off). For now this covers interrupting while it is
  speaking, on a web call; interrupting while it is still thinking, and having it resume
  where it left off, are planned next.
- **Voice call, the agent chimes in on interesting talk**: during a live call the agent
  now keeps a short rolling transcript of what it hears, and when someone else in the
  room says something that matches your configured interest topics, it can briefly and
  naturally chime in with a grounded remark instead of staying silent. It never invents
  a reason to speak (a chime-in must be grounded in your topics, and the agent may still stay
  silent), never chimes in while it is busy with a task, and does not repeat itself. One
  simple dial, `voice_awareness_activity` (quiet..active), sets how readily it joins in
  (at the lowest setting it only listens); it behaves calmly on its own when you are in a
  conversation with someone else and more readily one-to-one, without you managing any
  modes. It also asks "did you mean me?" when an unclear speaker says something like "can
  you hear me?" and it cannot tell whether it was addressed. Tool actions stay yours
  alone (a guest still cannot make it do anything), and your private context is never
  used to chime in for a guest.
- **Per-speaker language hint for cloud STT**: the shared speech client now caches
  the language the cloud provider already returns and passes it as a hint on the
  next transcription (a more precise, cheaper call), instead of running a local
  model to pre-detect it. The cache is keyed per speaker (the web mic uses the
  user's scope, so it stays user-isolated), and to catch a mid-conversation
  language switch it re-detects hint-free every few turns and always refreshes from
  the actually-detected language. No new dependency and no pre-call overhead. The
  hint is language-agnostic (normalized to ISO-639-1, ISO-639-3 mapped, locales
  reduced to base) so it works across every supported language, and the Veyllo lane
  auto-detects with `multi` (automatic code-switching) when no language is pinned.
- **Veyllo speech-to-text**: Veyllo is now selectable as a cloud STT provider
  (`speech_stt_provider = veyllo`, model `veyllo-transcribe`), using the same
  API key and base URL as the Veyllo chat/vision provider. The first time a Veyllo
  key is added (at onboarding or later in Settings) while no STT provider was
  chosen, Veyllo becomes the default STT lane, and it always falls back to the
  local engine on any failure (no internet, empty credits, timeout); an explicit
  later choice (local, OpenAI, ElevenLabs) overrides it. The `veyllo-transcribe`
  audio model is filtered out of the chat-model dropdowns.
- **Data Explorer legibility**: deleting a chat now saves the chat's title
  into the surviving workspace folder, so folders left behind by deleted
  chats keep their human name instead of showing a raw internal folder name
  (an explicit rename always wins; folders orphaned before this change keep
  their old names). The badge on such folders now reads "chat deleted"
  instead of the jargon "orphan", the list sorts live chats first and
  deleted-chat folders to the end, and a new info button in the header
  explains the folder colors, the badge, and the current-chat dot. The
  window now keeps a fixed height with a refined scrollbar instead of
  growing and shrinking with the workspace count, and each tile counts
  its folders too (a workspace holding only a subfolder no longer reads
  "0 files" as if it were empty).
- **Data Explorer search**: a search field in the "My Workspaces" header
  filters workspaces by name instantly and also searches file names and
  text-file contents inside them (server-side, scoped to the user's own
  workspaces, bounded per workspace); matching files are shown under each
  workspace tile with a content snippet in the tooltip.
- **Three new developer docs make the existing machinery usable from
  outside**, each verified against the code: an observability guide
  (structured tool/gate events via the engine's event sink, plus the
  machine-readable NDJSON mode of the scripting CLI for integrating VAF as a
  subprocess from any language), a debugging guide (the complete map of log
  files, how to redirect them, what the debug switch does and does not
  silence, how to read a session file), and an engine reference (the
  constructor, lifecycle, turn and tool-dispatch contracts, and concurrency
  rules of the advanced embedding class). The embedding guide gains a
  security-posture section (what needs Docker, what runs on the host, what
  ports are and are not opened), pointers to the other extension points
  (workflows, skills, MCP servers, the update-surviving custom-tools folder),
  and an honest note on custom OpenAI-compatible endpoints. Key engine
  methods now carry docstrings.

- **Persistent conversations, richer events, async and marker constants on
  the library facade.** Four additions for developers embedding VAF:
  `agent.save_session()` plus `Agent(session=<id>)` persist and resume a
  conversation across process restarts (idempotent updates, loud failure on
  unknown ids, tenant-ownership check under `user_scope`; runnable chatbot
  example included); `vaf.markers` exports the special return-value strings
  (`SYSTEM_LOG_ONLY`, `GENERATION_STOPPED`, ...) as constants with a CI
  guard pinning them against the engine source; the structured event sink
  grows `duration_ms` and a dispatch-level `ok` flag on `tool_end` plus new
  `llm_start`/`llm_end` events with token usage on API providers (attached
  across backend swaps; facade shortcut `agent.on_event(cb)`); and
  `await agent.run_async(...)` runs a turn in a worker thread for
  event-loop applications - documented honestly as a thread-executor
  wrapper, not native async.
- **Multi-tenant embedding: `Agent(user_scope=...)`.** An application
  embedding VAF can now assert which end user a conversation belongs to
  with one parameter. The value is validated as a UUID at construction
  (a bad scope fails loudly instead of silently operating on the machine
  owner's data), the account username is resolved together with the scope
  (never the admin fallback), the identity is bound before the system
  prompt is built and re-asserted on every turn, and memory/reminders/
  per-user files then key on that scope with the product's fail-closed
  filters. The embedding guide gains a "Multi-tenant embedding" section
  spelling out the trust model and the hard limits (one tenant per
  process, the machine-global trust store, shared on-disk config, no
  reliance on database-level isolation yet), and now states honestly that
  a bare unscoped agent acts as the machine owner in local mode. Also
  fixed: the sandbox tool now receives the user scope from the dispatcher
  (spoof-proof direct assignment), so its per-user container work
  directories key on the calling user (previously every main-agent run
  shared one prefix).

- **One provider registry instead of eleven copies.** The LLM provider set
  and its endpoints now live in a single source of truth
  (`vaf/core/provider_registry.py`); the backend factory, the coder's
  endpoint map, live model discovery (both copies), the vision-capability
  check (previously three manually-synced copies that had drifted apart) and
  the CLI settings menus all read from it, guarded by new CI sync tests plus
  a factory-pinning test that locks today's behavior byte for byte. Four
  real drift bugs died in the process: the CLI provider menus did not offer
  Veyllo at all, the provider-coverage test itself skipped Veyllo, and two
  of the three vision checks did not recognize Veyllo models as
  vision-capable. Provider-specific behavior (DeepSeek reasoning fields,
  OpenAI reasoning-parameter gating, Veyllo tool-call-id handling) stays in
  gated code paths, unchanged.
- **Per-instance tool registration on the library facade.** An embedded
  `Agent` can now be handed extra tools directly: `agent.add_tool(MyTool())`
  before the first run registers a `BaseTool` for that instance only - no
  pip package, no file drop-in. Late or invalid registrations raise clear
  errors, and the facade CI guard pins the behavior.
- **A developer FAQ.** Short verified answers to the questions developers
  actually hit: Docker requirements, the confirmation-gate error string,
  thread-safety, the engine's return contract, log redirection, custom
  endpoints, the tool lanes, and what the alpha stability promise covers.
- **A runnable examples/ directory.** Five self-contained artifacts for the
  embedding surface: the five-line quickstart, streaming plus the structured
  event sink, driving VAF as a subprocess via the NDJSON output format (the
  pattern for non-Python integrations), per-instance tool registration, and
  a complete pip-installable custom-tool package using the entry-point
  mechanism. A CI test keeps every
  example compiling and the example tool loadable, and the license-header
  check now covers the examples tree.
- **A CI guard now protects the public library surface.** The docs promise
  that `from vaf import Agent` is safe to build on; until now no test
  imported that facade, so a breaking change to it could have shipped with
  a green CI. A new executable contract pins the facade exports, the
  `Agent`/`run()` signatures, the `BaseTool` declarative defaults, and the
  pip entry-point tool discovery (`vaf.tools` group).
- Setting up a voice profile no longer stalls on slow connections: the
  speaker-engine models (26 MB) now download in the background during the
  enrollment intro instead of blocking the first round.

### Changed
- **Voice call, guest privacy**: on a live call with an enrolled voice profile, a guest
  (a speaker the voice check does not verify as you) who talks to the agent now gets a
  reply built WITHOUT your private context - your chat digest, your memory, and the
  prior call history are all withheld from that turn entirely, not just guarded by a
  prompt rule, and the agent is told to help only with general questions and never share
  your memory, notes, schedule, messages or contacts. So a guest cannot make the agent
  replay your earlier turns by asking "what did you just say?". Tool actions already
  stayed yours alone; this closes the matching information side.

### Fixed
- **A finished workflow now tells the model, imperatively, not to redo the
  work.** A verified successful run (finished HTML on disk) was followed
  by the model rebuilding every step manually and reporting total failure
  - it had skimmed past the bare success banner. Both completion messages
  now lead with an explicit directive (work is done; do not redo steps,
  re-run searches or rebuild files; present the results and file path)
  before the output and the per-step summary. The docs also gained an
  honest "small local models - known behavioral limits" section
  (PROVIDER_MODES.md): redo-after-success, narrated intentions and
  round-trip fragility are model limitations the framework mitigates but
  cannot remove; assigning a stronger sub-agent/coder model
  (subagent_model) is the user-facing lever.
- **A workflow step's instruction survives the presence of extra
  parameters.** The retest's model authored a step exactly the way the
  tool schema teaches - instruction in input, extra parameters in args
  ({"max_results": 3}) - and the engine built the tool call ONLY from
  args, silently dropping the input: web_search ran without a query and
  the whole run failed with "Error: No query provided." The resolved
  input now fills the tool's missing primary parameter; steps whose args
  already carry it (every saved template) are untouched.
- **A workflow's completion message shows the real work of every step, and
  three templates no longer end on a filesystem agent asked to write
  prose.** A retest run succeeded end to end (11-minute coder step, HTML
  written) - but the template's final step asked the librarian agent, a
  FILESYSTEM agent, to "write a short completion message ... where the
  file was saved". It parsed that as a file search and returned "No files
  found matching '*was*'", which became the workflow's final output. The
  model read that next to "completed successfully", concluded the run
  produced nothing, and redid every step manually - 42 steps, three
  duplicate deliverables. The librarian completion step is removed from
  research_and_code, generate_docs and create_file (the save step's own
  "File written successfully to <path>" is the honest completion), and
  both completion messages (saved workflows and temporary ones) now
  append a bounded per-step result summary, so one weird step can never
  hide the actual deliverables again.
- **The validation question no longer costs the workflow run - the system
  answers it itself.** When a temporary workflow's content steps carried
  no validate flags, run_temp bounced with "[VALIDATION CHECK] ... call
  run_temp again with the flags or skip_validation". A live retest showed
  a weak model bouncing off that twice (retrying without the flags both
  times) and then doing every step manually while its correctly authored
  workflow never ran. Validation on deliverable steps is exactly what the
  bounce text recommended - so run_temp now enables it on those steps
  automatically and runs; skip_validation stays the explicit opt-out.
- **A temporary workflow with mangled step field names is repaired instead
  of rejected.** The retest's model got everything right except the step
  FIELD NAMES: tool in "action", instruction in "description", no "input"
  - and the nested schema requirement rejected the entire call with
  "'input' is a required property", a message the model could not act on;
  it regressed into planning spin until the loop guards ended the turn
  with nothing produced. Steps are now repaired before validation (tool
  also accepted from action/agent_id; input falls back through
  task/prompt/instruction/query/description/name; args-only steps get a
  synthesized label), the hard nested requirement is gone, and a step
  with truly nothing usable gets a targeted error instead of a schema
  dump. Applies to temporary and saved workflow authoring alike.
- **A model can no longer talk itself into believing its own fictional
  diary.** Two consecutive retest chats failed the same way: the model
  narrated its INTENTIONS into working-memory notes ("Web-Suche: läuft",
  "Workflow wurde erfolgreich gestartet") without calling one real tool;
  the working-memory block re-injected that fiction as trusted context,
  the anti-spin guard then forced a text-only turn whose wording ("state
  your result") invited a result, and the model coherently reported a
  finished workflow with an invented file path. The result-grounding
  judge (the same small local model) waved it through. Four guards now
  close the loop end to end: (1) a note firewall refuses outcome- or
  progress-claiming notes while no real tool has run this turn (facts,
  not intentions - the note is re-fed as truth later); (2) result
  grounding gained a deterministic rule that needs no LLM judge: a final
  reply asserting tool outcomes after a bookkeeping-only turn is
  ungrounded by construction (purely conversational recap turns still go
  to the judge); (3) the anti-spin escalation now explicitly forbids
  claiming results and demands an honest still-open answer; (4)
  working-memory timestamps are rendered in the user's timezone in the
  prompt (they were UTC, so the model misdated its own recent actions by
  hours).
- **A healthy coder inside a workflow is no longer executed at minute
  five.** The generic sub-agent hard cap (300s) applied to workflow steps
  too, and killed a perfectly healthy coding-agent step mid-loop - linter
  green, actively streaming - with SIGTERM, failing the whole workflow at
  its last step (live incident). Heavy agent steps (coder, research,
  document) inside a workflow now get a worst-case floor of 30 minutes
  (workflow_agent_step_timeout_seconds); this is safe because a dead or
  stuck child is caught much earlier by the heartbeat liveness guard
  (~60s) - the hard cap is only the absolute-runaway backstop, and it was
  doing the killing instead. All other tools keep their normal budgets.
- **The SubAgent window no longer opens on top of a running workflow.**
  The intended design routes an embedded sub-agent step's output into the
  workflow panel's terminal, and the window-open helper honors that - but
  the sub-agent heartbeat handler set the window open directly, bypassing
  the guard, so the coder step opened a duplicate window next to the
  runtime panel. The heartbeat path now carries the same workflow guard;
  a manual open by the user still works.
- **The plan gate no longer bounces a workflow launch - the launch IS the
  plan.** The retest showed the exact cost of that bounce: the model
  committed to execute_workflow with the suggested template, got
  [PLAN REQUIRED], dutifully set a plan - and then did the steps manually,
  the workflow forgotten. A saved template id or a run_temp steps list is
  precisely the approach the gate exists to demand, so the gate now seeds
  working memory's plan from the launch call itself (observability
  preserved, placeholder detection unaffected) and lets it through.
  Launch calls without a plan payload (list/delete actions) still bounce,
  and every other state-changing tool is gated unchanged.
- **The duplicate-call guards now cover text-recovered tool calls too.**
  The in-batch dedupe and the windowed redundant-read check lived inside
  the streamed-call loop, but a weak local model's calls often arrive via
  the text-recovery fallback parsers, which bypassed both - the retest
  ran the same two searches twice within one second out of a
  fallback-parsed batch. Both checks moved into one cross-lane filter
  that runs after every parsing lane and before the calls are committed
  to history, so streamed and recovered calls get identical protection.
- **A temporary workflow's final build step now actually receives the data
  the earlier steps gathered.** The engine passes step results only through
  explicit placeholders in the next step's template - and a weak model
  reliably NAMES its outputs but never references them: the retest's
  workflow ran both web searches perfectly and then told the coder "use
  the results from the previous searches" in prose, with no placeholder
  anywhere. The coder received zero data and, correctly following the
  strict factual-data policy, rendered [DATA NOT FOUND] into every field
  of an otherwise finished page. When a task-consuming agent step (the
  coding agent, document writer, document agent, librarian and browser
  agents - builders and analyzers alike) references no prior step output,
  the engine now auto-attaches a bounded digest of the actual results to
  that step's instruction, in every lane the engine runs (temporary
  workflows, saved templates, the CLI lane, automations). The research
  agent is deliberately excluded (its input is a short topic query, and
  its job is producing data, not consuming it); templates that DO
  reference outputs - every saved template - are never touched. The
  authored step list of a temporary workflow is now also logged in full,
  since during the forensic it existed nowhere.
- **A pending background question can no longer hijack the user's next
  request.** The proactive thinking pass asks questions over the user's
  messenger and latches "waiting for a reply" per user. When the user
  then typed a brand-new task into a fresh chat, the pickup classified
  it as THE reply: the model got told "the user answered your background
  question", blended both topics (they happened to be similar), replied
  over the messenger unprompted, and the reply-confirmation gate then
  blocked the user's own workflow-builder calls twice - the turn ended
  after 27 confused steps with a stale answer, although the task had
  been completed midway. Three fixes: (1) the pickup lane now reads the
  RAW message - the WebUI workspace preamble had defeated the leading
  -text confirmation check (proven: raw "Okay fuehre bitte..." passes,
  enriched fails) and was even stored as the recorded reply; (2) the
  reply is classified three-way - short yes carries the proposal, short
  no declines it, and a LONG task-shaped message counts as a NEW topic:
  light context note only, no "carry out the proposal" framing, and the
  mutation gate stays disarmed so the user's own request is never
  confirmation-blocked; (3) a TTL safety net (default 12h) expires a
  stale waiting latch at read time, since the regular 10-minute skip
  only runs while thinking runs actually fire.
- **A model that builds the right workflow in the wrong wrapper now gets
  its own call handed back to copy.** Follow-up to the routing fix below,
  from the retest: the model merged the two workflow hints into
  execute_workflow(workflow_id="create_agent_workflow", variables={action,
  steps}) - a complete, CORRECT temporary-workflow payload inside the
  wrong tool. The prose-only redirect explained the mistake, and the
  model gave up on workflows and did every step manually. When the
  mistaken call carries usable steps, the redirect now echoes the exact
  create_agent_workflow(...) call to copy, arguments verbatim - weak
  models copy reliably but rephrase poorly. Still a message, never an
  auto-forward.
- **Verbatim re-runs of a lookup that already succeeded this turn are
  refused, and an identical duplicate within one response is dropped.**
  The redundant-call guard compared a new tool call only against the
  newest tool message, so a single interleaved call (a failed workflow
  attempt, a plan-gate bounce) hid an exact repeat from it - the same
  retest burned four calls re-running two web searches word for word,
  including the same search accepted twice in one second (for a send
  tool that same hole would have meant a double-send). Pure lookups
  (searches, reads, listings) whose identical call already succeeded in
  the current turn are now refused with a pointer to the existing
  result, EXCEPT when a mutating tool succeeded in between (a re-read
  after a write is legitimate; the guard fails open). Exact duplicates
  within one model response are dropped silently for all tools - one
  logical call, one result.
- **The workflow router routes on what the user actually said.** The WebUI
  lane prepends a workspace context preamble to the user message before the
  agent runs, and the workflow router matched templates against that
  combined text: the preamble's wording (coding_agent, projects,
  write_file) steered a plain websearch-and-HTML request to a CODE
  workflow, and the variable extractor stuffed the entire preamble into
  the workflow's query variable (live incident: a 44-step turn in which
  the model rightly declined the garbage suggestion and then did every
  step manually). Routing decisions - the template match, variable
  extraction, the explicit workflow parse, workflow-mention detection,
  language detection and the intent lock - now run on the raw,
  pre-enrichment message; the LLM still sees the enriched text.
- **A wrong template match can no longer eat an explicit workflow
  request.** The run_temp advisory used to exist only in the no-match
  branch, so when the router matched ANY template (even a bad one) for a
  message that explicitly asked for a workflow, the suggestion block was
  the only workflow path shown. When the user's own message mentions a
  workflow, the suggestion now also offers
  create_agent_workflow(action='run_temp') as the fallback - advisory,
  with the same typo-tolerant detection both hints share.
- **Two more weak-model argument names are repaired instead of rejected.**
  The same incident burned eight calls on schema rejections: write_file
  with file_content instead of content (four times) and python_exec with
  task instead of code (twice). Both observed names (plus contents and
  script) are now in the tools' alias maps, so the call is repaired and
  dispatched on the first attempt.
- **A tool call is no longer silently dropped when a model garbles its own
  closing tags.** The recovery parser for text-written tool calls (a
  reasoning model sometimes emits `<tool_call><function=NAME>...` as plain
  text instead of a native call) required an exact
  `</function></tool_call>` close to accept anything. A live case showed a
  local model trail off into hallucinated, unrelated closing tags instead
  of its own; the properly-closed parameter inside was perfectly
  recoverable, but the strict match returned nothing and the turn ended
  with tool-call-shaped text visible in history and no tool ever actually
  invoked. Three earlier drafts of this fix failed adversarial review
  with real false-positive execution risk (one could dispatch a
  destructive call out of a model's PROSE about the syntax if ordinary
  HTML appeared later in the reply; one silently dropped a legitimate
  parameter whose value merely mentioned closing-tag-shaped text; one
  accepted a single wrong-named closing tag, which an example wrapped in
  a code block satisfies by construction). The shipped design parses
  parameters one at a time, each bounded only by its own `</parameter>`;
  a strict close is accepted anywhere, while lenient recovery requires
  the call to start at a line start AND either two-plus consecutive
  wrong-named closing tags sitting immediately (whitespace only) after
  the last parameter, or the next `<tool_call>` beginning right there
  (back-to-back calls with the first close forgotten - recovering those
  was itself a review finding, they previously vanished silently). A
  well-formed call parses exactly as before, the incident's malformed
  close is recovered, and inline mentions in prose or markup are
  rejected.
- **A request to "build a workflow for this" no longer gets talked out of
  it.** When the router finds no SAVED workflow template for a request
  (most requests - templates are a fixed catalog, not every task fits
  one), the fallback hint told the model "most simple requests (weather,
  news, questions) don't need workflows" and pointed only at
  `list_workflows` (saved templates only) - never at the ad-hoc builder.
  A user who explicitly asked to run a weather lookup as a temporary
  workflow got a hint that contradicted their own request almost word
  for word, and the model complied with the hint over the user (live
  incident). The no-match hint now detects a workflow mention in the
  user's own message (typo-tolerant - the real request had "workflow"
  mistyped as "workflwo") and surfaces
  `create_agent_workflow(action="run_temp")` as the option to use
  instead; the detection is a cheap substring match that can also fire
  on an unrelated mention ("workforce", "my daily workflow" as small
  talk), so the wording stays advisory either way - it defers to the
  model's own judgment rather than pushing a directive - and now warns
  in both branches that `run_temp` needs 2+ chained steps, matching the
  tool's own single-step rejection.
- **Every chat now shows its workspace-folder chip immediately, and an
  unused one no longer lingers forever.** The chip is "this chat has its
  own workspace" - a standing affordance, not a "you already saved
  something" indicator - so opening a chat now creates its (empty)
  workspace folder right away instead of only after a tool happened to
  write into it. To avoid littering the projects folder with abandoned
  empty directories, deleting a chat now also removes its workspace
  folder when it is still empty at that point (a folder holding real
  content is left untouched either way).
- **The delete-time workspace cleanup got two safety rails** (audit
  findings on the entry above). While a sub-agent or workflow is still
  running for the chat, deleting the chat skips workspace removal
  entirely: the run may drop its first output file between the "is it
  empty" check and the removal, and files must never be deleted out from
  under a live run. And the recursive emptiness check now surfaces
  unreadable subdirectories instead of silently skipping them (the
  underlying directory walk ignores permission errors by default), so a
  permission-denied subtree full of files can no longer classify the
  whole workspace as "empty" and get it deleted - anything that cannot be
  fully inspected is kept.
- **A session without a recorded owner is no longer treated as owned by
  everyone in the workspace endpoints.** The HTTP workspace lane (browse,
  upload, file delete, plus the image-describe session cache) accepted
  any authenticated user as the owner of a legacy session that predates
  user scoping, while the WebSocket gate treats exactly those sessions as
  admin-only - so on a multi-user server, a non-admin user could browse,
  upload into, create, or delete files inside such a session's workspace.
  Both HTTP checks now enforce the same rule as the WebSocket gate:
  scopeless sessions are admin-only, admin detected role-aware. Scopeless
  sessions were not purely theoretical: an automation delivering to a
  messenger contact BEFORE that user ever wrote inbound created the
  channel session without an owner scope (the inbound lane always stamped
  it; the two outbound-first creators did not, despite having the scope
  in hand) - those sessions are now stamped with their owner at creation,
  so their real owner keeps normal access on multi-user servers instead
  of falling into the admin-only legacy bucket. Pre-existing scopeless
  session files on disk stay admin-only in both lanes, exactly as the
  WebSocket gate already treated them.
- **A local model calling the wrong "workflow" tool now gets redirected,
  not just a template list.** `execute_workflow(workflow_id=...)` takes a
  saved template id; a weak model tried
  `workflow_id="create_agent_workflow"` - the NAME of the other workflow
  tool, not a template - and got a plain "not found" listing that didn't
  explain the actual mistake. Both tools' descriptions are now explicit
  about the distinction, and the error message detects a live tool-name
  collision and points at the right tool to call instead.
- **A failed tool no longer reports itself to the model as "OK".** The
  per-turn tool-context summary (what the model reads to know what its
  tools did) labeled failures by an out-of-date heuristic that missed the
  standard `Tool Error:` / `Security Error:` / `[PLAN REQUIRED]` prefixes,
  so a failed `write_file` showed as `-> OK: Tool Error ...`; a local model
  then told the user the file existed when it did not (live incident). The
  three copies of the "is this a failed tool result" check (retry guard,
  per-turn summary) now share one prefix-anchored helper so they cannot
  drift again. A follow-up adversarial review of that fix then found the
  shared detector covered only a handful of the failure shapes shipping
  tools actually return; a repo-wide sweep added roughly thirty more
  currently-live families (the `[BLOCKED]`/`[HOST]`/`[SECURITY]` gate
  markers, the filesystem `Access denied:`/`Invalid path` refusals, the
  messaging "unavailable:" family, "X failed:" shapes, MCP/HTTP error
  idioms, connection preconditions, unimplemented-stub replies, and the
  banner-prefixed python runner marker), each pinned by a test built from
  the tool's real return string. A third adversarial pass then caught the
  first version of THAT expansion over-rotating: it scanned whole results
  with free substrings, so a successful `read_file` of a log mentioning
  "connection failed:" rendered as a failure (10/10 realistic
  content-carrying reads misclassified). The shipped detector therefore
  anchors every family that starts the message, bounds banner-tolerant
  markers to the first 200 characters, gates stub-reply phrases on the
  result being short, and pins the content-carrying success class with
  its own tests. The three thinking-mode soft-block nudges now lead with
  `[BLOCKED]` so a blocked gather call also reads as blocked instead of
  green.
- **Common weak-model argument-name mistakes are now repaired.** A local
  model called `write_file` with `file_path`/`message` instead of
  `path`/`content`; the call failed schema validation and the write was
  lost. Tools can now declare an `input_aliases` map (kept off the model-facing
  schema so no provider can reject it), and the input-repair layer remaps a
  present alias to the canonical name before dispatch (conservatively:
  never overwriting a supplied canonical key, never guessing between two
  aliases). `write_file` maps the obvious path/content synonyms.
- **Stop now actually stops a running web search.** Stopping a turn
  abandons the tool's worker thread (Python cannot kill threads), and the
  abandoned search kept crawling pages and calling the local model for
  summaries long after the stop - occupying the single llama server with
  dead work. The runner now hands every bounded worker a cancellation
  signal that, unlike the shared stop flag, cannot be cleared out from
  under it; the web search checks it before every page read, every
  summary call and the final synthesis, and exits early. Bonus fix found
  in the same logs: those summary calls burned their whole token budget
  on reasoning and returned empty text on thinking-capable local models -
  tool-side utility completions now disable thinking, like the voice lane
  already did.
- **A workflow cannot be started twice for the same chat anymore.** After
  an empty-response recovery reset, a local model could forget it had
  already delegated and call the same workflow again while the first run
  was still live (observed: two concurrent research workflows sharing one
  GPU). Both launch lanes now check the session's live tasks and refuse a
  duplicate with an honest still-in-progress status, mirroring the
  sub-agent re-delegation guard. An adversarial re-audit then found the
  first version of that guard structurally broken for the in-process
  `execute_workflow` lane: it read the task registry but the tool never
  registered its own runs there, so two concurrent calls both sailed past
  it (verified with a live repro). The tool now registers itself for the
  duration of the run (deregistered on every exit path), both lanes share
  one guard predicate, and that predicate also counts a freshly-created
  task whose spawned terminal has not reached "running" yet, closing the
  spawn-window race in the terminal lane. A second adversarial pass on
  the fix itself then forced two more rounds of hardening: the
  registration now heartbeats (without one, the zombie reaper failed it
  90 seconds into exactly the multi-minute runs the guard protects,
  silently reopening the guard and queueing a spurious crash report), and
  both lanes verify AFTER registering who won the registry slot, since
  the pre-check alone still let two barrier-synced concurrent calls both
  through. The tool's cleanup also consumes any result another actor
  (stop-all, a reaper) queued for its task id while it ran, so a
  synchronous run can never additionally surface as a phantom sub-agent
  delivery. A failed terminal spawn now deregisters its task and falls
  back to inline execution instead of reporting an async run that never
  started.
- **Sub-agent task registry updates can no longer erase each other.** The
  IPC task queues (pending/active/results) are JSON files mutated by
  read-modify-write sequences whose per-file locks only covered a single
  read or write - two concurrent mutations (for example two tasks being
  marked running at once) could interleave and silently drop one side's
  update, observed live as two workflow launches erasing each other's
  registry entry and both slipping past the duplicate guard. All registry
  mutations now serialize through one reentrant in-process lock plus a
  bounded cross-process file lock held for the whole read-modify-write,
  with a best-effort fallback (a stuck lock degrades to the old behavior
  after five seconds rather than wedging a chat turn).
- **The workflow terminal stream no longer freezes the app window.** When a
  workflow step drew a live progress animation (the research agent), every
  animation frame line was forwarded to the Web UI as its own event -
  hundreds per second, full of raw ANSI color codes - until the page froze
  and the WebSocket dropped (live incident). The mirror now enforces ticker
  semantics at the emit site: ANSI escapes stripped, empty animation frames
  and duplicate redraws dropped, and a hard rate cap with an honest
  "[... N lines skipped]" marker. The real terminal still shows everything.
- **`vaf prompt` works in local mode now.** Both scripting lanes
  (`vaf prompt` and the `vaf run prompt` alias) never loaded the local
  model, so every local-mode invocation returned an empty answer - the
  same bug class as the library-facade fix below, found by pre-push smoke
  testing and verified live against real hardware (model load, NDJSON
  stream, server reused by the running app afterwards).
- **Workflow steps run the sandbox under the calling user's scope.** The
  workflow engine's own tool-argument injection (a deliberate narrower
  copy of the agent dispatcher) did not cover the sandbox tool; its
  per-user container work directories now key on the user in workflow
  runs too, and a CI guard pins the engine copy's coverage.
- **Embedding a local model actually works now.** The documented library
  quickstart with the local provider silently returned an empty string: the
  facade never loaded the model, so the turn aborted before generation. The
  facade now downloads/starts (or reuses) the one local llama server on
  first use, exactly like the CLI does. Also corrected in the embedding
  guide: the real location of the trust store (the platform config dir, not
  the VAF home dir) and the fact that the "system" permission level bypasses
  the confirmation gate rather than triggering it.
- **Documentation corrections across the developer docs**, each verified
  against the code: the embedding guide now names PySide6 (not PyQt6) in
  the desktop extra and includes the `veyllo` provider; the memory doc no
  longer claims "all memory content" is encrypted at rest (embedding
  vectors and titles/tags metadata are not - the encryption section
  spells out exactly what is and is not); the contributing guide's lint
  and format instructions now match what CI actually gates on instead of
  commands that would reformat 480 files; the architecture doc reconciles
  the "stable surface" promise with the alpha status and links the
  backward-compatibility rules; the Web UI flow doc's log-directory
  resolution order matches the code; the tools guide recommends native
  MCP server registration over the raw low-level tool, and its FAQ now
  covers all three tool lanes (in-tree, update-surviving `custom_tools/`,
  pip entry points); the docs index describes server mode correctly and
  lists three previously missing pages; the config reference gains rows
  for `anthropic_prompt_cache`, `anthropic_thinking`, and the
  `ux_auto_open_*` keys.
- **Voice-profile enrollment works on fresh installs.** Setting up a voice
  profile answered "I could not hear speech" on every round of a clean
  install while the microphone was fine: the speaker-identification engine
  (sherpa-onnx) was never a declared dependency - it is in requirements
  now (hash-pinned lock refreshed) and in the `speech` extra. Two more
  layers of the same incident: the voice-activity model is now downloaded
  by the VAD path itself (the first enrollment round used to fail on a
  missing model file even with the engine installed), and an engine
  failure is spoken as its own message ("the voice-profile engine is not
  available - this is not about your voice") instead of sending the user
  into a speak-louder loop. The microphone WAV converter is also unified
  into one shared implementation that writes the recording's actual sample
  rate into the header (older WebKit builds ignore the requested 16 kHz
  and would have produced files whose header lied about the audio).

## [0.1.0a14] - 2026-07-16

### Fixed
- **The a12 updater self-heal never actually fired - fixed for real.** The
  live verification on a Mac caught it: the updater's git wrapper strips
  its output, the dirty-line parser read paths at a fixed offset, and the
  lockfile churn was misread as a real user edit - so updates kept
  aborting despite the a12 fix. The parser is position-independent now,
  the restore uses exact paths, and the test suite gained an end-to-end
  test through a real git repository (the fixture shape that would have
  caught this). Stuck installs still need the one-time
  `git checkout -- web/package-lock.json` before their old updater can
  reach this version.

## [0.1.0a13] - 2026-07-16

No functional changes: a verification release. Updating a12 to a13
exercises the fixed updater end to end on a real install - the npm
lockfile self-churn restore, the forced tag fetch and the renormalized
line endings must carry an update through without any manual step.

## [0.1.0a12] - 2026-07-16

### Fixed
- **`vaf update` can no longer deadlock itself.** Three causes found on a
  Mac that sat on a7 while four newer releases existed: (1) the updater's
  own npm step (and the first-run frontend install) rewrote
  `web/package-lock.json`, and the dirty-tree pre-check then refused every
  future update - npm runs `ci` now (never modifies the lockfile), and the
  pre-check restores updater-managed files instead of aborting on them;
  real user edits still abort. (2) Release tags that were ever recreated on
  the remote made `git fetch --tags` fail mid-update with a rollback - tags
  are fetched with `--force` now. (3) Two shell scripts were stored with
  Windows line endings despite their `eol=lf` attribute, so a fresh checkout
  started dirty - the repository is renormalized. If your install is
  currently stuck on an old version, run `git checkout -- web/package-lock.json`
  in the VAF folder once, then `vaf update`.

## [0.1.0a11] - 2026-07-16

The voice release: the live call becomes a first-class citizen on local
single-model setups - its own configurable model lane (Gemma 4 E4B as the
recommended voice model), local vision, a real level meter with a noise
gate, owner-approved adaptive voice learning, and a long list of hardening
fixes found in live testing.

### Added
- **Speaker confirmation with a named voice DB.** When speaker identification
  scores a voice as "unsure", VAF now asks the owner to confirm - via the main
  messenger (question + audio segment attached) or, without one, via a card in
  the web chat (audio player, yes/no, optional name). Answering "no, that's
  Peter" stores the voice as a named third-party profile in the per-user voice
  DB, and future utterances by that voice are labeled `[Peter]:` for the agent.
  The owner's own profile is never modified by an answer, named speakers can
  never trigger delegations, at most one question is pending per user, and
  replies on messengers are consumed without starting an agent turn.
- **Recognition test with threshold calibration (Settings > Voice).** Record a
  few seconds and see who the system detects, with score, threshold and
  uncertainty band visualized; admins tune the threshold with a live slider.
  Judging results as correct or wrong feeds a per-user calibration store that
  suggests a threshold from your own voice data. (Owner-confirmed clips
  additionally sharpen the voice profile since the adaptive-learning change
  below.)
- **First-call enrollment offer.** Clicking the call button without a voice
  profile now offers the guided enrollment (with the security rationale) or a
  remembered "call without profile" skip; after a successful setup the call
  starts directly. The voice profile is loaded on connect, and the
  recognition-test verdict flow is click-only: "Who was it then?" with
  Me / Someone else buttons - a name is only typed (optionally) for someone
  else, and every path now feeds the threshold calibration.
- **The voice agent knows when NOT to answer.** The live-call mic is always
  open, so utterances are now gated before they cost anything: side talk from
  other speakers (no agent address) and garbled speech-recognition noise never
  reach the LLM or the speakers - the text still enters the call context so
  the agent knows what happens in the room. For the owner's own side talk the
  model can answer with a silence marker in the same call that would have
  answered anyway (no extra LLM turns, no added latency).
- **Live calls work in local mode by time-sharing the one model.** Without an
  API provider the voice agent now talks to the local llama server directly,
  and the single model is shared instead of doubled: the voice agent has it
  first, and while a delegated task runs for the main agent the call goes
  temporarily mute - dimmed avatar, centered muted-mic badge and a "the model
  is working for the main agent" status, with turns paused on both ends until
  the result is spoken. A call started with no model at all keeps the
  distinct "no model available" state.
- **The voice agent can run its own model.** Settings > Voice (admin) now
  offers three choices for the live call's language model: same as the main
  agent (default, unchanged), a dedicated local model - recommended and
  preset: Gemma 4 E4B, whose spoken German third-party tests rate as
  noticeably more natural than Qwen's - or a separate API provider. With a
  dedicated local model the single llama server swaps models instead of
  running two: the voice model holds it during the call, the main model
  takes it back while a delegated task runs, and a safety belt makes sure a
  main-agent turn never runs on the voice model. The voice model downloads
  on the first call (about 5.4 GB for the default) with the usual progress
  banner.
- **Local vision: the local model can see images itself.** Settings > AI >
  Vision now offers "Local": the llama server is launched with the model's
  vision encoder (mmproj, about 650 MB, downloaded automatically from the
  model's own repo), and image descriptions plus the analyze_image tool run
  fully on-device - no cloud provider needed. Works with the Qwen3.5 default
  models and Gemma 4; the encoder size is budgeted into the VRAM context
  math and per-image context cost is capped. Takes effect on the next model
  start after enabling.
- **Wake word: calling the agent by name always gets an answer.** The live
  call sometimes chose silence when the speaker was not recognized or when
  the agent was addressed directly. An utterance that says the agent's
  persona name (fuzzy-matched, so speech recognition garbling like "Charvis"
  for "Jarvis" still counts) now always engages - for any speaker - and the
  model is told it was addressed and must answer instead of staying silent.
  The security rules are untouched: an unverified voice still cannot trigger
  delegations or get private information. The voice agent also carries its
  persona now: it introduces itself with the configured agent name (Settings >
  Persona) instead of a generic "VAF", and a compact excerpt of the Soul
  keeps it in character on the call.
- **Vision keeps working during a live call, calls load the model without
  the desktop tray, and the agent's bars are real too.** With local vision
  enabled, the dedicated voice model (Gemma 4 E4B can see) now loads its own
  vision projector when it takes the server, so image questions work
  mid-call; auto-downloaded projectors get per-model filenames (Qwen and
  Gemma both ship "mmproj-F16.gguf" - a shared name would have paired the
  wrong projector after a model swap). Starting a call now loads the local
  model directly in the backend instead of relying on the desktop tray's
  activity watchdog - headless/server installs get the self-healing call
  start too. And the gray bars while the agent speaks now show the agent's
  actual output level (shared analyser with the avatar's eye pulse) instead
  of an animation.
- **The call bar waveform is a real level meter now, with a draggable noise
  gate.** The red bars during a live call show your actual microphone
  amplitude (same mechanism as the recognition test) instead of a random
  animation - muting flattens them naturally. A slider line sits inline in
  the meter, exactly on the color boundary: everything left of it is gray
  and IGNORED (not recorded), only audio that swings past it is red and
  processed. Drag the line (it lights up on hover, chevron handles, with an
  inline explanation below the bar) to tune out background noise; the
  setting persists and takes effect live, mid-call. Also fixed on the way:
  the call UI re-rendered on every animation frame while speaking (the
  voice-activity loop wrote the store per frame), which made the whole call
  feel laggy - state now only updates on real speaker transitions, and the
  meter keeps one audio pipeline for the whole call.
- **The live call follows your spoken language.** Speak Turkish and the
  agent answers in Turkish with the Turkish voice - per turn, whenever the
  language is installed in the text-to-speech stack (cloud voices count as
  multilingual; never a surprise download mid-call). The call's base
  language now also honors the configured default language instead of only
  the browser locale, and the "is that your voice?" confirmation card is a
  centered dialog now instead of a top bar.
- **Voice delegations are marked in the chat.** A task the voice agent hands
  to the main agent now renders as its own message: a red-ringed bubble with
  a soft glow and a "voice agent" tag next to the timestamp, instead of
  looking like a typed user message. The tag is persisted with the session,
  so the styling survives reloads. The delegation prompt rule is also a
  blanket one now - every request that needs a tool, live data or an action
  goes to the main agent (verified against the local model: weather, mail
  and news requests delegate; clock questions and small talk do not).

### Fixed
- **Confirming "yes, that was my voice" now actually teaches the system your
  voice.** Answering the confirmation question (web card or main messenger)
  previously only relabeled the segment; now the confirmed segment flows
  into your voice profile as an adaptive sample - with guardrails: a
  similarity floor rejects noise segments, at most ten adaptive samples
  count (oldest age out), the original enrollment keeps 70 percent of the
  weight, and re-enrolling resets everything. Authorization still never
  comes from audio: only your authenticated answer can trigger a profile
  write (kill switch: `speaker_id_adaptive_enabled`). The voice agent also
  remembers twice as much of the call now - a slicing bug fed the model
  only the last 4 exchanges instead of the stored 8.
- **The call bar keeps one size, and the workflow terminal stops flooding
  the page.** The live-call bar no longer jumps 44 pixels wider when no
  stop button is shown (the button slot stays reserved during a call), and
  the workflow window's terminal caps every output entry at 500 characters
  (a single sub-agent output block could be tens of kilobytes; hundreds of
  those made the whole page lag - full outputs remain in the logs).
- **Three live-call bugs on local single-model setups.** Quitting the tray
  now really stops the llama server: after a model swap the running server
  belonged to a helper, and the quit path only looked at its own stale
  process handle, so the model survived until `vaf stop`. A live call is
  pinned to the chat it started in: switching chats mid-call no longer
  routes turns, delegation bubbles or the spoken result into the newly
  opened chat. And sub-agents are safe from the voice model swap: while a
  sub-agent computes on the one local model, the server itself now reports
  the call busy (the frontend flag alone dropped too early once the main
  turn ended) and no model swap can start - a voice turn during a sub-agent
  run used to swap the model out mid-inference and crash the sub-agent. The
  call window shows "background task running" during that window and heals
  afterwards. The same protection covers ALL main-lane work now: chat
  generations and running workflows also mute the call on single-model
  setups, not only voice-delegated tasks (a workflow's document generation
  could otherwise be swap-interrupted mid-write).
- **The Telegram bot token no longer leaks into terminal and log files.**
  The Telegram Bot API carries the token in the request URL, and the HTTP
  client's default INFO logging printed that URL on every polling tick -
  into the console and the log files. Request-URL logging is silenced now
  (warnings and errors still come through). If you ever copied terminal
  output containing `api.telegram.org/bot...`, revoke the token via
  `@BotFather` and set the new one in Settings.
- **Local voice turns answer instead of thinking.** A local reasoning model
  (Qwen) burned its entire voice token budget on internal reasoning: the turn
  ended with nothing to speak, no delegation was created, and the code then
  wrongly spoke the "one moment" acknowledgment - a promise with nothing
  behind it. Voice calls now disable thinking on the local server
  (runtime-verified: the same question answers in one sentence instead of
  timing out mid-thought), a reasoning-only reply degrades to the "please
  repeat" nudge, and the acknowledgment is only spoken when a delegation
  actually survived. The acknowledgment itself is now short ("Moment.",
  "One moment.", rotating variants in ten languages) instead of one fixed
  sentence, and the voice agent knows the user's current local date and time,
  so clock questions are answered directly instead of being delegated. The
  delegation instruction is also phrased capability-first with a worked
  example now: a small model read "you cannot use tools" as a reason to
  refuse real work ("I have no tools") instead of delegating it.
- **Starting a live call now loads the local model.** The call button only
  probed for a running model and opened a dead call when it was not loaded
  yet (a chat message was needed to trigger the load). Call start now feeds
  the same activity trigger a chat message feeds, the window shows "loading
  the model" instead of the muted-mic state, and the call comes alive by
  itself (greeting included) once the model is up.
- **web_search no longer claims the web is down when a filtered search finds
  nothing.** With a source filter (trusted sources or smart intent) that had
  zero hits, the internal-memory fallback silently prevented the retry
  without the filter, and a hard-coded banner told the model "the web
  providers are unreachable" on a healthy network - which the model repeated
  to the user. The plain query is now always retried on the real web before
  memory snippets are accepted, and the fallback banner only claims an
  outage when providers actually errored (including the recorded errors);
  otherwise it says the search found nothing.
- **Natural questions now find memories by name.** "Kannst du dich noch an
  Kai erinnern?" returned nothing while a bare "Kai" search hit - filler
  words diluted the lexical score of the one signal word. Query tokens are
  now filtered against per-language stopword lists (maintained in the
  vocabulary book for reuse), and the lexical tokenizer finally keeps
  umlauts, so German words and names like "Müller" are matchable at all.
- **Memory chunk text and the profile cache are now encrypted at rest.** Chunk
  texts (what RAG actually reads) are AES-256-GCM encrypted in place and
  decrypted on read; a startup migration encrypts existing rows, removes the
  unencrypted content previews from memory metadata and neutralizes
  content-derived titles of learned facts. The on-disk user-profile prompt
  cache is encrypted the same way. Docs now state the residual risk honestly:
  embedding vectors stay unencrypted by necessity and are practically
  invertible, so full-disk encryption remains the recommended complement.
- **Memory chunk rows are now row-level-security protected, and the encryption
  key is never silently replaced.** Chunks (the searchable text and embedding
  vectors the RAG actually reads) now carry their own owner scope and the same
  fail-closed forced RLS policy as the parent memories table, stamped at ingest
  and backfilled by a startup migration. A present but corrupt
  `memory_encryption_key` is now a hard startup error instead of being silently
  regenerated, which would have permanently orphaned all encrypted memories.
- **Memory learning produces higher-quality facts.** The extraction prompt now
  enforces self-contained facts (subjects named explicitly instead of "the
  patent"), absolute dating of drifting snapshot facts ("as of {date}"), and
  excludes short-lived conversation state; model-independent gates between
  parse and ingest add length bounds, junk-marker rejection, a per-run cap and
  a near-duplicate check, so a weak model can no longer flood the memory store.
- **Spoken voice-agent replies are capped at a sentence boundary.** A model
  derailed by garbled input could fill its whole token budget with a monologue
  (minutes of TTS); replies are now cut in code, and the prompt tells the
  agent to ask for a repeat instead of guessing at garbled transcripts.
- **Veyllo no longer 400s mid-task after a text-recovered tool call.** When a model
  leaks a tool call as text (deepseek-v4 does intermittently) or a stream loses the
  id, VAF must mint a tool_call id itself; those ids now carry a recognizable
  `call_synth_` prefix (also fixing an id collision when two recoveries happened
  within the same second), and for Veyllo such exchanges are folded into plain-text
  context before sending instead of being replayed structurally (the gateway only
  accepts ids it issued itself). Tasks that previously died with a visible API
  error (e.g. mail checks delegated from a live voice call) now complete.
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
- **Workflow runs open the Workflow Runtime panel again in TLS setups.** The `@workflow`
  subprocess posted its UI events (workflow_start/update/done, terminal lines) to a
  hardcoded plain-HTTP 127.0.0.1:8001 - with local_network_tls_enabled that port speaks
  HTTPS, every event died silently, and the frontend (never learning a workflow was
  running) showed the generic SubAgent window instead of the Workflow Runtime panel.
  Subprocess senders now resolve the backend through a shared TLS-aware helper
  (internal plain-HTTP port 8005 when TLS is on); the vaf-run terminal's
  heartbeat/health probes had the same blindness and use it too.
- **`@workflow` runs no longer fail with "Tool not found" for sandbox steps.** The
  `@workflow` CLI subprocess, the in-chat executor and the run_temp overlay each
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
