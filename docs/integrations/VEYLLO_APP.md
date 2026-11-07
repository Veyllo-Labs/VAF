# Connecting the Veyllo App

The Veyllo App on your phone talks to your own agent on your VAF: you chat with it from
anywhere, it sends you its messages (reminders, automation results, sub-agent results), and
your agent can hand a task to the phone (`user_phone`). This page says how the link works,
what each party can see, what it costs and where its limits are.

The code lives in `vaf/core/app_link/` (its module docstring names the pieces), the routes in
`vaf/api/app_link_routes.py`, the terminal commands in `vaf/cli/cmd/devices.py` and
`vaf/cli/cmd/relay.py`.

## Two ways, one protocol

- **Through the Veyllo relay** (from anywhere). VAF opens ONE outbound connection to the
  relay, and only while it is needed: a relay QR code was scanned, or a device uses the
  relay. Without that there is no connection at all. The relay forwards bytes between the
  app and VAF; nothing reaches VAF through it except the link protocol of paired devices.
  The sign-in, the web UI and the API are never reachable through the relay.
- **Directly** (`wss://<address>:<https port>/api/app-link`). Your phone reaches VAF on the
  local network or through your own VPN on the phone. Offered only while Local Network and
  its TLS are on (the protocol is `wss` only); the address is admitted like every other
  connection ([NETWORK_FEATURES.md](../setup/NETWORK_FEATURES.md)). Nothing is sent to
  Veyllo for these messages and they cost nothing.

Both ways carry the same bytes: a TLS 1.3 session between the app and VAF that ends inside
VAF (ALPN `vaf-link/1`). The relay only adds a 17-byte envelope (`0x01` and a 16-byte
connection id). A relay QR code that also lists direct addresses lets the app switch to the
direct way later without pairing again.

## Pairing

Pairing happens in the web UI, in **Settings, Connections, Veyllo App**
(`web/components/connections/VeylloAppSetupWizard.tsx`):

1. The window first says what the app can do, what Veyllo sees on each way and what each way
   costs, and then asks for **your own** 2FA code. That opens a pairing window for your account
   for ten minutes; nobody can create a QR code for another account, an admin neither.
   With the relay switched off and no direct address either, the window says pairing is not
   possible and asks for no code.
2. VAF shows a QR code, drawn on this machine (the routes answer it as a PNG; no renderer
   elsewhere ever sees it), with the time it is still valid. Below it a switch chooses the way:
   **Veyllo Relay** (from anywhere, one cent per message, Veyllo sees only metadata) or
   **Local** (only on your own network or over your VPN, free, nothing through Veyllo).
   Switching issues a new QR code without a second 2FA code and withdraws the earlier one.
   While an admin has the relay switched off there is no switch, only the Local code. The QR
   code carries a one-time value of 32 random bytes, the fingerprint of this machine's local
   certificate authority, and for the relay a separate relay value. Everything secret sits in
   the URL's fragment, which a browser never sends; the code is valid for ten minutes and is
   neither logged nor stored on disk.
3. The app creates a key in the phone's hardware store, checks VAF's certificate against the
   authority's fingerprint from the QR code, and sends a certificate request with the
   one-time value. VAF checks the request first (a bad one does not use up the code), then
   the code: a code used once is "used", an unknown or expired one "expired", a second
   pairing while one waits for confirmation "busy".
4. The window asks "Is this your device?" and shows the phone's model, platform and app version
   and its key fingerprint (four groups of hex, exactly as the app shows them). Only after you
   confirm, within two minutes, is the device created, its certificate issued by VAF's separate
   device authority, and the Veyllo App switched on for your account. The window then says
   "Connected" with the phone's model; in the app, your VAF is called "Main-Agent (VAF)".

The window asks the server every two seconds while it is open, visible and the pairing is still
running. Closing it (the X, Escape, a click beside it) withdraws a QR code that is still live
and refuses a phone that is waiting for the confirmation, so no pairing nobody watches stays
open. Refused, expired and failed pairings each say what happened and offer a new code.

Every refused pairing is a security event (`app_pairing_refused`), every paired and removed
device too (`channel_paired`, `channel_unpaired`).

The protocol's vectors (the QR code, the frames, the fingerprint) are in
`tests/fixtures/app_link/`, made by `scripts/app_link_vectors.py`.

## What each party sees

- **The relay (Veyllo)** sees that this installation has an instance, how many devices are
  members, their IP addresses, when they connect and how many bytes flow. It cannot read the
  content and cannot act as VAF: the app checks VAF's certificate against the authority from
  the QR code, and VAF knows a device only by its certificate. A relay that changes, replays,
  reorders or cuts bytes ends the session; a relay that moves one device's stream to another
  connection id still gets that device, because the id never decides who is talking. The
  relay's connection goes only to the API's host (`relay_api_base`) or `relay.veyllo.com`,
  at an address VAF resolved and checked as public itself, with TLS checked against the
  name; behind a site proxy the proxy resolves the name, as for every outbound fetch.
  A session that has not said who it is gets 20 seconds and small frames only.
- **The agent in the Veyllo app** uses your VAF as a tool and reads its answers with the app's
  own chat model; there is no switch for that. The pairing window says so in one sentence and
  links Veyllo's privacy policy (https://veyllo.app/legal/privacy), which names it.
- **Google and Apple** see when a push notice reaches the phone (it carries no content).
- **The app is closed source.** The end-to-end promise holds against the relay, not against a
  changed app. When you use Veyllo's models in the app, that content goes to Veyllo.

## Messages, replies and the phone being away

Your message becomes a turn of your agent in your account's one Veyllo App chat; the account
comes from the device, never from the message. The reply streams to the phone and ends with
its final text. A message VAF sends by itself (`send_to_user` with the Veyllo App as your main
messenger, the `send_veyllo_app` tool) and every finished reply are stored once in VAF's chat
history and held per device until that device confirms it: a phone that was away gets them,
in order, when it connects again, a reply that finished meanwhile as one message. Up to 200
entries and seven days are held per device; older ones stay in VAF's chat history, and the
phone gets one notice that they are there. A file sent while a phone is away reaches it as its
text line only.

**Push.** When something waits for a phone that is not connected, VAF waits 30 seconds (the
phone often reconnects), then asks the relay to notify it, and then at most once every five
minutes while something still waits. Push needs the phone to be paired with Veyllo: without
Veyllo there is no push, and on the direct way alone the app learns about new messages only
while it is open. With the relay switched off there are no push notices either.

**Tasks for the phone.** `user_phone` hands the phone's agent a task (a calendar entry, a
reminder) and waits for its answer, up to its timeout. With no phone connected it says so at
once and nothing is sent or kept for later. Its answer reaches your agent marked as data the
phone reported, never as instructions. The phone asks you itself before sensitive actions.

**Tools in the app chat** follow the rules of every messaging channel
(`vaf/core/tool_contract.py`, `evaluate_tool_policy`), because protocol v1 has no confirmation
message. The host shell, host Python, SSH and FTP never run there: they act on a computer and
need a confirmation only the web UI can show. The other tools kept off chats (the sandbox
environments, the browser agent, the builders, background commands, timers) depend on the
admin's `channel_tools_unrestricted`: on, the default, they run in the app chat without a
confirmation, as on Telegram; off, they stay off. A request of the app's own agent never gets
them, nor the tools that start work which keeps running after it (next section).

## The app's agent can ask your agent

The agent in the Veyllo app can hand your agent a task while the app is connected
(`agent_ask`, its own message type, so an older VAF answers "unknown type" and the app's tool
stays away). There is no switch for it: every `welcome` says that VAF takes it. Such a request
is a turn of your account's app chat, with these differences
(`vaf/core/app_link/bridge.py`, `vaf/core/agent_origin.py`):

- **It is the agent's text, not yours.** VAF's chat shows it with the tag "From your app's
  Veyllo agent" and a sky-blue ring, the terminal with a label line. Your agent reads it inside
  a fence that says the app's agent wrote it on your behalf and that it may carry text that
  agent read elsewhere; every later reader of the chat (a reload, the compaction, the chat
  history tools) reads it the same way. It is not stored as your message in the inbox, does not
  become your last words shown in your other chats, and does not answer a question your agent
  is waiting on.
- **Work that would run later and changes to how VAF works ask you on the phone first.** A
  coder run (`coding_agent`), an automation (`create_automation`, `update_automation`,
  `restore_automation`, and the notes later automation runs act on, `add_automation_note`,
  `add_automation_todo`), a workflow (`execute_workflow`), a skill (`create_skill`,
  `update_skill`, `delete_skill`) or a change of your profile (`update_user_identity`) does not
  run on a request: the call is parked, the request's turn ends there, and the phone that asked
  shows a confirmation card (each such tool declares its card,
  `BaseTool.agent_request_confirm_kind`, read by `vaf/core/agent_origin.py`). So does a mail
  (`send_mail`, `reply_mail`, `forward_mail`) or a WhatsApp message to a number
  (`send_whatsapp` with `to_phone` or `phone_number`). The card is VAF's own text of exactly
  that call: what it does, then every argument the call carries (for a message every
  recipient, Cc and Bcc, the subject, the attachment names and the whole text). The values are
  shown safely rather than byte for byte, so no value can pass itself off as another argument
  or push one out of sight. The card opens with one line per argument, in the tool's own
  order: its name and the first line of its value (at most 80 characters, then "(continued
  below)"; "(starts with an empty line)" when the value begins with one). The arguments a
  tool names as its targets (`BaseTool.agent_request_confirm_whole_args`: the coder's project,
  environment and deploy target, an automation's output path and triggers, an id) show their
  whole first line there, because their tail is their meaning. Below that block, each value
  continues under its own name, every line indented, with runs of empty lines counted.
  Invisible characters (direction overrides, zero-width and control characters, blank
  fillers, combining marks with no letter before them or stacked beyond two) appear as
  `⟨U+XXXX⟩`; a mark after a space, a tab or another such escape counts as having no letter,
  because a space with a combining slash draws as a slash. The two brackets `⟨` and `⟩`
  themselves are escaped too, so every `⟨U+XXXX⟩` on a card is VAF's own. A Windows line
  break is just a line break. An argument name shows every character outside plain ASCII,
  and any space or colon, as `⟨U+XXXX⟩`. A call with an argument the tool does not declare is
  not parked at all: the app's agent is told to call again without it. A message card is
  laid out the same way, the recipient first and the text last. Every recipient, Cc and Bcc
  reads address or number first and whole, then the name (at most 40 characters, ending in
  `…` when cut, an `@` in it marked, because a name written as `"boss@firma.de (Boss)"` is
  not an address): `to: x@evil.test (boss⟨U+0040⟩firma.de (Boss))`. An address shows every
  character outside plain ASCII marked, and a domain with such characters its punycode in
  square brackets: `alice@ex⟨U+0430⟩mple.com [xn--exmple-4nf.com]`. A number shows only its
  digits and a leading `+`, anything else marked. Two sends never reach a card at all: a mail
  to an address with a character outside plain ASCII is refused when it is queued (the mail
  lane has no SMTPUTF8 and could never deliver it), and a WhatsApp number with anything but
  digits, a leading `+` and the separators space, `-`, `.`, `/`, `(` and `)` is refused
  before it is parked; the app's agent gets the reason. What runs is still exactly the
  stored call. Nothing is cut: a call whose text as shown would be longer than
  4000 characters is refused, and the app's agent is told to make the request smaller. Your answer counts only from a tap on that card on that
  phone:
  - **Yes** runs exactly that call once (a message leaves as when you press Send in the web
    UI), and the app's agent gets its outcome as the answer to its request. The confirmed work
    then runs with its own limits, as when you ask for it in the app chat yourself: the card
    lifts the refusal for that one call and nothing else, and a policy confirmation of that
    call counts as given once, never always. Your VAF chat shows the run as a "Confirmed in the
    app" line.
  - **No** answers the request that it did not run; a message is discarded.
  - **No answer within 10 minutes** answers the request that it did not run; a message still
    waits as a draft in the web UI.
  - **A decision in the web UI** settles the card: a message you send, discard or edit in the
    web UI's Veyllo App chat answers the request with what became of it, within seconds, and a
    later tap on the phone is told it is decided. One decision wins: a draft leaves at most
    once, and the phone never sends a draft that was edited after its card went out.
  - **Cancel** of the request on the phone drops the card (and the message).
  - One card waits per request at a time, and a request opens at most three in a row. A
    second action in the same round is refused. A message prepared in the same round as an
    action (or a second message) gets its own card, with its own id, once the card before it
    was answered Yes and its outcome is in; the request's answer then carries both outcomes.
    When the card before it ends any other way (No, no answer, a decision in the web UI), no
    further card comes: the message waits as a draft in the web UI, and the answer says so.
  - **The phone is away:** the card waits until it runs out, the relay sends a push (a
    relay-paired phone), and the card comes again after the app connects, with the same id.
    It is never in the outbox.
  - **An older app** that does not know the card answers "unknown type": every card sent to
    that connection is answered as not run (a message waits in the web UI), and the next one
    is not sent.
  - **A restart of VAF** drops every open card; nothing runs after it, and a message still
    waits in the web UI.

  On the wire: VAF sends `{"t": "confirm", "id", "ask", "kind", "what", "exp"}` to the asking
  device only (`kind` one of `coder`, `automation`, `workflow`, `skill`, `identity`, `send`;
  `exp` ten minutes on), the app answers `{"t": "confirm_answer", "id", "ok"}`, and VAF sends
  no frame back for an answer that counts: the outcome is the request's own `done`. An answer
  is refused with `expired` after `exp`, `used` once the card is decided, and
  `confirm_invalid` from another device, another account or for an unknown id. Every `welcome`
  carries `"confirm": true` (`vaf/core/app_link/confirmations.py`).
- **Tools that need you in person stay off.** Each tool declares how it answers such a request
  (`BaseTool.agent_request`); refused ones are not offered either, and the app's agent is told
  why. Refused: every tool kept off chats, whatever `channel_tools_unrestricted` says (the
  sandbox, the browser agent, timers, background processes); work whose own limits a request
  cannot reach (`set_timer`, the room tools that make other agents act, `room_send`,
  `room_open`, `room_invite`, `room_join`, `room_react`, and the librarian, `librarian_agent`,
  whose own file tools would run outside these limits); changes to how VAF works for you that
  no card covers yet (`create_contact`, `update_contact`, `thinking_note_add`, a context
  checkpoint, `checkpoint_context`, whose summary later turns read as yours, the coder's codex
  and scratchpad, `update_codex` and `add_memory`, and the co-author line of VAF's commits,
  `set_git_coauthor`); every tool that writes files, for every account and wherever it writes
  (`write_file`, `edit_file`, `download_file`, `document_writer`, the document agent
  `document_agent`, the git tools `git_init` and `git_add_commit`, and `cloud_storage` with
  `save`, `download` or `retrieve`; a tool declaring `file_access = "write"` is refused
  whatever its path), so no request writes a `VAF.md` or a file that runs later (`.git/hooks`,
  `package.json` scripts, a `Makefile`, `.envrc`, `.vscode/tasks.json`, `~/.bashrc`, an
  autostart entry); code in the Python sandbox (`python_sandbox`, whatever its arguments: your
  sandbox container lives on between runs, so what a request leaves there would run again in
  your later runs); a message to your own messenger (`send_to_user`, `send_telegram`,
  `send_discord`, `send_veyllo_app`, and `send_whatsapp` without a number, which goes to your
  own; the app's agent is told that it sends a message, not that it writes files); and what
  needs your confirmation for another reason (`user_phone`, `store_credential`,
  `github_create_issue`, `github_update_file`, and every MCP tool an administrator has not set
  to read, `mcp_call` included). A call that needs a policy confirmation is refused at once; no
  dialog appears in the web UI and nothing waits. Answers you gave ahead still hold: a tool you
  allowed always, a trusted folder, an "allow for this chat" in the app chat, and the admin
  bypass. A reminder (`schedule_reminder`) stays possible: it only delivers its own text to
  your own messenger at the time you set, and no turn runs from it.
- **A message that cannot be parked is not sent.** When the draft of a message to somebody
  else cannot be prepared, it is not sent either, and the app's agent is told so.
- **What stays possible, deliberately.** Everything else the app chat may do without a
  confirmation: reading your mail, calendar and contacts; memory entries (`memory_save`,
  `memory_update`, `learn_document`), which carry a mark of their origin and come back inside
  the same fence in every later turn and background run (the chat's own memory learning skips
  these requests and their answers); the app chat's own goal and plan
  (`update_intent`, `update_working_memory`), which later turns of that chat read unfenced (so
  does the context VAF compresses on its own if such a request ever fills the chat's context); a
  question back (`ask_user`: the question goes out as the answer, and the app's agent puts it
  to you); the MCP tools set to read; and the research agent, whose own tools hold nothing a
  request is refused. A few tools still write a file whose name the request does not choose:
  the research agent's report (`research_<topic>_<time>` in the chat's workspace), VAF's own
  caches of web searches and fetched pages, and the code audit's last result
  (`vaf-audit/last.json` inside the git folder of the project it audits). Your cloud storage
  stays readable (`cloud_storage` browse, search, read, and showing a file in the document
  viewer).
- **The answer goes to the phone that asked.** It streams as `delta` and ends with `done`, both
  under the request's own id, to that device only, without "read" or "typing"; it is held in
  that device's outbox only, and only that device gets a push. Your other phones see nothing
  of it in the app (VAF's chat history keeps both). After a dropped connection the app sends the
  same id again and gets only an acknowledgement; the answer reaches the device on its new
  connection, or later from the outbox as a message with that id.
- **Limits.** At most three requests per device are queued or running at once, and they count
  against the device's 30 messages per minute; an id seen in the last 24 hours, or one that
  already names a stored answer, is only acknowledged. A request that fails answers with
  `done` and the failure; the app cancels one with its id (only the device that asked can), and
  one that ends without an answer (stopped in the web UI, or its device removed) gets an empty
  `done` from the next cleanup, within seconds, or its place is freed after 24 hours.
- **Who reads the answers.** The app's agent reads VAF's answers with the app's own chat
  model, so with Veyllo's models in the app, what VAF answers goes to Veyllo, even when VAF
  itself runs fully local.

## Cost

The relay is billed to the app user's Veyllo account, not by VAF: one cent per message in
each direction, also for the messages VAF sends by itself (reminders, automations, sub-agent
results), and half a cent per direction for a task for the phone. The direct way is free.

## Managing devices and the relay

- Your paired phones are rows of **Settings, Account, Signed-in devices**, with the way they
  last used (relay or direct), whether one is connected now, when it last connected, when it
  was paired and its fingerprint. **Remove** asks first; its access then ends at once, also
  when its certificate is still valid (a lost phone too). In the terminal: `vaf devices list`,
  `vaf devices remove <id>`.
- An admin sees every account's devices, with the account, in the admin section of the Veyllo
  App window ("Veyllo relay (for every account)") and removes one there.
- Devices also go when their account is deactivated or deleted, after 90 days without use
  (`device_idle_days`), and when the account signs out every other device. A password change
  does not remove them: the device key, not the password, is their credential.
- An admin switches the relay off and on (`vaf relay off`, `vaf relay on`, or the switch in
  the admin section of the Veyllo App window, which also shows the relay's state and how many
  devices are members and use it now): off keeps the instance, its secret and the devices, so
  on again needs no new pairing. `vaf relay rotate` (or "Replace relay secret" in the window,
  after a question) replaces the relay secret. `vaf relay dissolve` (or "Dissolve relay
  instance" in the window, with the admin's own 2FA code) deletes the instance; the devices
  stay for the direct way and need a new pairing for the relay. A failed action says whether
  no relay is set up, the relay could not be reached or it refused (`reason` in the route's
  answer).
- `vaf health` has an `app_link` row, `vaf doctor` reports a relay that carries devices and an
  instance whose secret is missing.

## Named boundaries

- **The local CA's key is the app link's identity.** The app pins this machine's local CA;
  its key (`~/.vaf/ssl/ca-key.pem`) is an owner-only file, not sealed, like the device CA's
  key beside it. Whoever copies `ca-key.pem` can pass as this VAF to every paired app, and
  with the relay in hand read that traffic. The key is in the encrypted backup; replacing it
  (`vaf server ca renew`) means every device pairs again.
- **Renewing the certificate authority** (`vaf server ca renew`) means every device pairs
  again: the app pins that authority. So does an authority VAF had to replace because its
  files were damaged.
- **A server behind 1:1 NAT** (a public address mapped onto a private one) is not detected as
  public; see [NETWORK_FEATURES.md](../setup/NETWORK_FEATURES.md#scope-private-networks-only).
- **A backup restored onto a second running machine** holds the same relay secret: the two
  take the relay connection from each other (VAF waits five minutes each time and says so in
  `vaf relay status`). Rotate the secret on one, or dissolve on one.
- **Post-quantum key exchange** is used only where both TLS stacks offer it; there is no extra
  key from the QR code in every session.
- **Pairing from the terminal** is not offered: the one-time value lives in the web server's
  process and the confirmation is a step of the signed-in account in the web UI.
- **The pairing window learns of a scan by asking**, every two seconds, not by a push over the
  WebSocket: the state lives in the web server's process, and the shortest deadline (two
  minutes for the confirmation) is sixty of those polls long.
