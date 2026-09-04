# WhatsApp Integration

VAF provides a full-featured WhatsApp integration via a Node.js bridge (Baileys) with support for text messages, voice messages, document delivery, and bidirectional voice communication. The integration runs as a linked device (similar to WhatsApp Web) and uses the same TTS/STT services as Telegram.

## Roles: the linked account is the agent

The WhatsApp account you link (QR scan) is **your agent's own number**. The agent writes to your contacts and to other people from it and reads their replies. Nobody chats with the agent from that phone: a message typed in the linked account's own chat ("message yourself") is dropped by the bridge. Use a spare phone or the company number for the link, never the phone you chat from.

Who may write IN is decided per message, in this order:

| Sender | Match | What happens |
|---|---|---|
| the linked account itself | - | dropped (`SELF_CHAT dropped` in `whatsapp_inbound.log`) |
| your **registered main-user number** (`whatsapp_config.whitelist` entry for your VAF account) | `explicit_pair` | full chat, like Telegram: all tools, session `whatsapp_<user>_<digits>` |
| a **contact** with "Can reach your assistant" | `contact_fallback` (policy permitting) | Front Office (restricted tools, dedicated prompt) |
| a number **your agent wrote to** inside the reply window (`reply_window_hours`, default 72) | `open_conversation` | Front Office; the prompt says there is no contact record and that the agent started the conversation |
| anyone else | `not_paired` | rejected, security event `channel_rejected` |

Registering your own number is optional. Without it the agent is **outbound only**: `send_whatsapp(to_phone=...)` and contact conversations work, but `send_to_user` and `main_messenger = whatsapp` have no endpoint. Each VAF user links their own account; there is no shared credential set (two Baileys sockets on one credential set evict each other).

## Overview

The WhatsApp bridge lets the agent communicate over WhatsApp, supporting:

- **Text Messages**: Standard text-based conversations
- **Voice Messages**: Incoming voice transcribed via the configured STT lane, outgoing voice synthesized via the configured TTS lane (local containers by default, or cloud providers via `speech_stt_provider`/`speech_tts_provider` - see [SPEECH_FEATURES.md](../web-ui/SPEECH_FEATURES.md)); auto-reply or via `send_whatsapp(voice_lang="...")`
- **Documents**: Agent can send PDF, DOCX, and other files via `send_whatsapp(file_path="...")`; the path is confined to the caller's own data like every other path tool (`file_access = "write"`, see [USER_ISOLATION.md](../security/USER_ISOLATION.md))
- **Per-User Isolation**: Each VAF user links their own WhatsApp account (their agent's number); credentials stored under `~/.vaf/users/<username>/whatsapp/`
- **Ingress control**: Only the registered main-user number, contacts with "Can reach your assistant" (Front Office) and numbers inside the reply window are answered (see Roles above)
- **Agent Tools**: `whatsapp_inbox`, `find_whatsapp_messages`, `read_whatsapp_chat`, `send_whatsapp` for listing, searching, reading, and sending
- **WhatsApp Inbox (message store)**: Like mail/Telegram, every message that passes through the bridge is stored in a local SQLite DB (`whatsapp_messages.db`). The chat list (dashboard and `whatsapp_inbox`) is built from the **bridge** list + **activity** (including rejected senders) + **store** (chats that have at least one stored message). So all chats with messages stay visible and searchable even after a reconnect or when WhatsApp sends only a subset of chats.
- **Chat history download**: When WhatsApp sends **messaging-history.set** (on connect, with `syncFullHistory: true`), the Node forwards those messages to Python and they are written to the message store. So after linking or reconnecting, the inbox can be filled with history that WhatsApp provides (DMs only for now; groups are skipped). The **periodic sync every 10 minutes** (`chat_sync_interval_sec`, default 600) refreshes the **chat list** (getChats); it does not re-request full message history (that only happens on connect via messaging-history.set).
- **Optional Send-Only Mode**: When `inbound_to_agent` is `false`, incoming messages do not trigger the agent; the bot can still send content to you

---

## Architecture

The integration consists of two processes communicating over stdio (JSON lines):

```
WhatsApp User
     │
     ▼ (message / voice / document)
┌─────────────────────────────┐
│  WhatsApp Servers           │
└─────────────────────────────┘
     │
     ▼
┌─────────────────────────────┐
│  Node: wa-bridge.js         │
│  (Baileys, one subprocess   │
│   per user)                 │
│  ├─ messages.upsert         │  ◄── Incoming (text, audio, etc.)
│  ├─ downloadContentFromMsg  │  ◄── Voice → temp file
│  ├─ send / send_voice /     │  ◄── Outgoing commands from stdin
│  │   send_document          │
│  └─ emit(send_result)       │  ◄── Delivery confirmation
└─────────────────────────────┘
     │ stdout (JSON lines)     │ stdin (JSON lines)
     ▼                         ▲
┌─────────────────────────────┐
│  Python: whatsapp_bridge.py │
│  ├─ _read_user_process()    │  ◄── Parses Node stdout, enqueues tasks
│  ├─ _transcribe_voice_file()│  ◄── Whisper STT (incoming voice)
│  ├─ _synthesize_voice_...() │  ◄── TTS (auto voice reply)
│  ├─ _enqueue_reply()        │  ◄── Reply callback from agent
│  └─ _sender_loop()          │  ◄── Writes send/send_voice/send_document to Node
└─────────────────────────────┘
     │
     ▼
┌─────────────────────────────┐
│  Task Queue → VAF Agent     │
│  (per-session: whatsapp_    │
│   username_digits)          │
└─────────────────────────────┘
     │
     ▼
WhatsApp User (text, voice, or document reply)
```

Key components:

- **Node (vaf/whatsapp_node/wa-bridge.js)**: Started by Python with `node wa-bridge.js --auth-dir <path>`. Reads JSON commands from stdin (`send`, `send_voice`, `send_document`, `getChats`), writes events to stdout (`message`, `send_result`, `qr`, `connected`, etc.).
- **Python (vaf/api/whatsapp_bridge.py)**: Spawns and manages the Node process with **stdout/stderr opened as UTF-8** so JSON lines (including transcribed text with non-ASCII) decode correctly on all platforms. Maintains `_outgoing_queue`, implements STT/TTS for voice, and enqueues incoming messages to the VAF task queue with session ID `whatsapp_{username}_{digits}`.
- **Background send IPC**: Proactive sends from background/subprocess runs (for example automations) do not share the main process memory. For those cases, `send_whatsapp` writes a request into a small file-based IPC queue under the platform data directory. The main WhatsApp bridge process reads that request, forwards it to the correct Node session, and writes back the delivery result. This lets background runs use the same live WhatsApp connection without needing their own bridge process.

---

## Configuration

### Setup

1. Install Node.js (>= 18) and ensure it is in your PATH.
2. Nothing to install by hand: the bridge's Node dependencies (`vaf/whatsapp_node/node_modules`) are installed from the lockfile (`npm ci`) the first time the bridge or the QR login starts, and again by `vaf update` when the lockfile moved (`ensure_bridge_deps` in `vaf/api/whatsapp_bridge.py`). The manual `npm install` in `vaf/whatsapp_node/` remains the fallback when npm is missing or the directory is not writable; the wizard shows that message.
3. In the Web UI, go to **Settings → Connections** and click **Connect** on WhatsApp.
4. Scan the displayed QR code with WhatsApp on the **agent's** phone (a spare phone or the company number; Linked Devices). That account becomes the agent's number; the wizard shows it. The same step carries a warning the reader should take at face value: WhatsApp's terms of service do not allow automated use of a regular account, and a number linked to VAF can be restricted or banned by WhatsApp, so link only a number you can afford to lose. The wizard's first step is translated (`settings.whatsappWizard` in `web/messages/*.json`).
5. Optionally register **your own number** (the one you chat from) in the wizard's second step or later in the dashboard. Nothing is added automatically; the agent's own number is refused there.
6. Turn the connection **on**; the bridge starts when enabled and restarts automatically after VAF restarts if WhatsApp is enabled.

### Config File

WhatsApp configuration is stored in `~/.vaf/config.json` (or your platform config path) under `whatsapp_config`:

```json
{
  "whatsapp_config": {
    "enabled": true,
    "inbound_to_agent": true,
    "reply_window_hours": 72,
    "whitelist": [
      {
        "phone_number": "+49123456789",
        "user_scope_id": "<uuid-from-auth-or-local-admin-scope>",
        "vaf_username": "admin"
      }
    ]
  }
}
```

The `whitelist` entry is the **registered main-user number** of that VAF account (the number the user chats from), never the linked account. **Best practice:** Use the same `user_scope_id` as the Web UI for that user. For the local admin, use the value of `local_admin_scope_id` in config (set automatically by bootstrap when the first admin is created, or set manually). The bridge resolves missing `user_scope_id` in whitelist entries via `get_local_admin_scope_id()`, so the local admin's WhatsApp sessions use the same scope as CLI and localhost - one identity across Web, CLI, and WhatsApp.

Authentication (Baileys session) is stored per user under `~/.vaf/users/<username>/whatsapp/` (or the platform-specific data directory). Do not commit these directories to version control.

### Configuration Options

| Key | Type | Description |
|-----|------|-------------|
| `enabled` | bool | Enable/disable the WhatsApp bridge |
| `inbound_to_agent` | bool | When `true`, accepted incoming messages are enqueued and the agent replies. When `false`, WhatsApp is send-only: the agent can still send, but no incoming message triggers it |
| `whitelist` | array | Registered main-user numbers (E.164) with `phone_number`, `user_scope_id`, `vaf_username`: one per VAF account, full chat as the owner. The linked account's own number is refused here |
| `reply_window_hours` | number | How long a number the agent wrote to may reply without being a contact (Front Office). Default `72`; `0` switches the window off. The window is measured from the newest outbound message in the per-user message store |
| `chat_sync_interval_sec` | number | Interval in seconds for periodic full chat list sync (default: 600 = 10 min). The bridge requests `getChats` from the Node so the bot and dashboard have the latest chat list (names, last_ts). Set to `0` to disable. |

### Who may write in

- **Registered main-user number** (config whitelist): Each entry maps a phone number (E.164, e.g. `+491761234567`) to a VAF user. A message from it is the owner talking to the agent: full chat, all tools. Only this number is the target of `send_whatsapp()` without `to_phone`, of `send_to_user` and of `main_messenger = whatsapp`.
- **Contacts (Front Office)**: Contacts in the VAF contact list with **Can reach your assistant** enabled can send messages to your assistant (handled in your context, restricted tools). For WhatsApp, the contact **must** have that WhatsApp number stored in their **Channels** (type "phone" or "WhatsApp").
- **Open conversations (reply window)**: When the agent writes to a number (`send_whatsapp(to_phone=...)`, or a reply in a Front Office chat), that number may answer for `reply_window_hours` (default 72) even without a contact record. The message is handled in Front Office; the prompt states that there is no contact record and that the agent started the conversation, and the owner is informed through the usual back-channel. Every agent reply extends the window; a one-sided sender the agent never answers expires. The decision is `channel_ingress_policy.evaluate_ingress(..., conversation_match=True)` with reason `open_conversation`, computed from the newest outbound row in the message store (`channel_message_store.last_message_ts`).

The bridge builds the allowed set from the config whitelist plus all WhatsApp/phone channel values from contacts with "Can reach your assistant" enabled, and adds the reply window on top. The same three answers gate the reply lanes: the headless runner's reply and the owner delivery go out only to a registered number, a Front Office contact or an open conversation (`_is_reply_allowed`); an unresolved `@lid` matches none of them.

**Chat list sync:** So the bot and dashboard always see the latest chats (e.g. for `whatsapp_inbox`, Connections UI), the bridge runs a **periodic sync** every 10 minutes by default: it sends `getChats` to the Node, which returns the full chat list (Baileys `chatStore`); the Python side merges that with **chat_activity** and with **chats from the message store** (all chat_ids that have at least one message in `whatsapp_messages.db`), so the inbox is persistent like mail/Telegram. You can change the interval with `whatsapp_config.chat_sync_interval_sec` (seconds; `0` = disabled). **All chats** from the linked device (including @lid chats) appear in the list; the agent can use `whatsapp_inbox` to list them and `read_whatsapp_chat(chat_id=...)` to read messages. The chat list is whatever WhatsApp sends in the initial **messaging-history.set** (on connect) plus any **chats.upsert** / **chats.update** / **messages.upsert** (new chats when someone writes). After a bridge reconnect the Node may have only a subset of chats; the dashboard also shows chats with **activity** (including rejected senders), so numbers that have written at least once stay visible as Read-only. If you only see a few chats (e.g. only newsletters or recent ones), WhatsApp may be sending the full list in batches – wait 1–2 minutes after connecting and click **Refresh** again; the Node merges every batch into the list. **"Load all WhatsApp chats"** (Refresh when connected) re-requests the current list from the Node; it does not trigger a new history sync from the phone (Baileys has no API for “sync all chat list again”). Message history the agent can read is what the bridge has received or sent, plus what **Load older messages** in the dashboard pulls in per chat (on-demand history sync, see the command table below); there is no whole-account re-sync after the initial one.

- **@lid (Linked ID)**: WhatsApp uses **LID** (Link ID) as a privacy-preserving identifier: it often replaces the phone number (JID `number@s.whatsapp.net`) with something like `XXXXXXXXXX@lid`, especially in groups and Communities, so participants don’t see each other’s numbers. The LID is **account-specific**, stable across chats, and sent over WhatsApp Web/Multi-Device. **On the phone app**, if you have the person in your **phone contacts**, WhatsApp can resolve the LID and show the saved name and number – but that resolution happens on the phone. **Our bridge uses the Linked Device API** (Baileys): it only knows a LID’s number if WhatsApp **sends that mapping to the linked device**. Baileys exposes this via `lidMapping.getPNForLID`; we call it (e.g. when building the dashboard) and show `resolved_e164_from_node` when we get a result. WhatsApp does **not** always sync “saved contact → LID resolution” to linked devices, so even with the contact saved on your phone, the bridge may never receive the number for that LID. When we do receive it, the chat is shown as resolved (no manual step). When we don’t, the only way to allow replies is a manual mapping in `whatsapp_config.lid_to_e164`. **Inbound rule:** A message is accepted only when the sender JID or the resolved `fromE164` matches the whitelist or a Front Office contact. **Unresolved @lid** (no `fromE164` from Node and no `lid_to_e164` entry) are **rejected** so that strangers cannot reach the assistant.

- **Self-chat (the linked account's own chat)**: A message in the linked account's "message yourself" chat is somebody typing on the agent's phone, not the owner talking to the agent. The bridge drops it before the store, the activity and the queue; a downloaded voice file is deleted. In logs you will see `SELF_CHAT dropped`. The Node decides `selfChat` by comparing the sender with the linked account (for `@lid` senders only after resolving the LID to a number).

- **Human takeover on the agent's phone**: When a person answers a contact from the agent's phone itself (e.g. someone opens the chat with Bob on the company phone and types), the bridge records "owner has control" for that chat. The agent **pauses replying** in that chat for **10 minutes** after the last such message. Incoming messages from the contact are still stored (message store + activity) but not handed to the agent until 10 minutes have passed with no further human message. In logs you will see `owner_sent chat=… → owner has control` and `owner_control: skip reply (owner has control, 10 min not elapsed)`. **Echo handling:** Only real human messages (sent from the phone) set owner control. When the bridge sends a reply, WhatsApp echoes it back with `fromMe: true`; the Node tracks recent sends per chat (text and voice) and does **not** emit `owner_sent` for those echoes, so the agent can keep replying to the next contact message.

- **Migration from the old model**: Installs that linked the owner's own phone have that number in the whitelist (the old wizard added it). On the next `connected` event the bridge removes a whitelist entry equal to the linked number, logs `WHITELIST removed agent's own number` and leaves a notification asking you to register the number you chat from. Re-link with a spare phone or the company number if the linked account is really your personal one.

### Best practices

- **Whitelist format:** Use E.164 for all phone numbers (e.g. `+491761234567`). The bridge normalizes JIDs; leading zeros or missing country codes can cause mismatches.
- **user_scope_id and username:** Use the same `user_scope_id` and username as the Web UI for that user. For the local admin, use `local_admin_scope_id` and the configured local admin username so Web, CLI, WhatsApp, and other tools (e.g. `list_contacts`, `get_contact`, `send_whatsapp`) resolve the same identity. Consistent identity avoids "no contacts" or "no Telegram/WhatsApp contact" when the agent runs from the Web UI or from a bridge.
- **Automations and WhatsApp:** Scheduled automations (e.g. Daily calendar check or reminder tasks) run with the **task owner's** `user_scope_id`. When such an automation calls `send_whatsapp` (or other messaging tools), the backend injects that scope so the correct WhatsApp session and identity are used. Use the same `user_scope_id` in whitelist/contacts as in the Web UI so reminders and automation-driven messages go to the right user.
- **Background delivery path:** If `send_whatsapp` is called from a subprocess that does not hold the live bridge state in memory, the request is forwarded to the main bridge over the IPC queue instead of failing immediately. The WhatsApp bridge still has to be running and connected.
- **Credentials:** Do not commit `~/.vaf/users/<username>/whatsapp/` (or the platform data dir equivalent) to version control; it contains the Baileys session.
- **Send-only mode:** Set `inbound_to_agent: false` when you only want the bot to send you content (e.g. reports, voice notes); incoming messages will not trigger the agent.
- **Front Office:** For contacts who can reach your assistant via WhatsApp, add their number in the contact’s **Channels** (type "phone" or "WhatsApp"). Without a WhatsApp channel, messages from that number are rejected.

### Troubleshooting: A number is shown as "Owner" but it is not mine

In the dashboard (Settings → Connections → WhatsApp), a chat carries the **Owner** badge only when the number is a registered main-user number (`whatsapp_config.whitelist`). Remove it in the dashboard's "Your number (main user)" card (or `POST /api/whatsapp/whitelist/remove` with `{"phone_number": "+49…"}`); messages from it then fall back to the contact and reply-window rules.

### Troubleshooting: Proactive Send Fails From Automation Or Background Task

**Symptom:** A scheduled automation or other background run tries to call `send_whatsapp`, but the send does not go through.

**How it works now:** Background runs no longer need direct access to the in-memory bridge objects. They hand the send request to the main WhatsApp bridge process through the IPC queue in the platform data directory, and the main bridge returns the delivery result.

**What to check:**

1. **Bridge status:** The main WhatsApp bridge must still be running and connected in Settings → Connections → WhatsApp.
2. **User session:** The target user must have a linked WhatsApp session and, if applicable, the correct whitelist entry or contact mapping.
3. **Result message:** If `send_whatsapp` returns an error, treat it as a real delivery problem. The tool now waits for bridge confirmation instead of assuming success from the caller's process alone.

### Troubleshooting: Reply went to "unknown number" instead of the contact

**Symptom:** The agent sent a voice reply but it appeared in WhatsApp as coming from an unknown number, not the known contact.

**Root cause – fake LID in `lid_to_e164`:** WhatsApp sometimes sends messages with a JID like `491701234567@lid` where the numeric part equals the actual phone digits. This is *not* a real privacy LID (a genuine LID looks like `123456789012345@lid` with completely different digits). If such a fake entry ends up in `whatsapp_config.lid_to_e164`, the sender loop resolves the outbound JID to that fake `@lid` and sends there - WhatsApp has no account under that address, so the message appears from "unknown number".

**Fix (applied automatically in bridge code):**
- The bridge now **rejects** any new `lid_to_e164` entry where the LID digits equal the phone digits (i.e. only genuine LIDs are persisted).
- Both `VOICE_LID_RESOLVE` and `TEXT_LID_RESOLVE` in the sender loop skip any map entry where `lid_digits == phone_digits`.
- If you have stale fake entries in config, remove them manually: open `whatsapp_config.lid_to_e164` in your VAF config and delete entries where the key (before `@lid`) has the same digits as the value (E.164 phone number). Example - **remove**: `"491701234567@lid": "+491701234567"`. **Keep**: `"123456789012345@lid": "+491702345678"` (digits differ → genuine LID).

### Troubleshooting: Strange number or LID – agent replied to someone I didn't add

If the agent wrote to a "number" that is a long digit string and not a real phone number (e.g. `+12345678901234` or similar), it is likely a **WhatsApp LID** (the numeric part of a `…@lid` chat). Unresolved @lid chats are **rejected** unless the sender is matched by resolved E.164 or whitelist/contact, so unknown senders cannot reach the assistant.

**Logs to check:** Under the VAF log directory (e.g. `logs/` in the project, or `Platform.data_dir()/logs`), see:

- **whatsapp_inbound.log** – each inbound message: `ACCEPT`, `REJECT`, `REJECT unresolved @lid`, `SELF_CHAT`, etc.
- **whatsapp_qr.log** – QR flow and bridge events.

Search for `from=…@lid` or `REJECT unresolved @lid` to confirm rejections. New messages from unknown LIDs are rejected unless you add a manual mapping (see below).

### Troubleshooting: Bot doesn't reply to a contact (e.g. Bob) – REJECT unresolved @lid

If a **known** Front Office contact (e.g. Bob) uses a chat that WhatsApp sends as **@lid** (e.g. `123456789012345@lid`) and the Node never sends `fromE164`, the bridge **rejects** their messages (`REJECT unresolved @lid from=123456789012345@lid`). To allow that contact again, add a **manual LID→E.164 mapping** in config so the bridge can treat that LID as their phone number:

1. In **whatsapp_inbound.log** note the rejected JID (e.g. `123456789012345@lid`).
2. In your VAF config (`~/.vaf/config.json` or `%APPDATA%\\vaf\\config.json`), under `whatsapp_config`, add or extend `lid_to_e164` with that JID as key and the contact’s **real E.164 number** (as in Front Office) as value. Example: `"lid_to_e164": { "123456789012345@lid": "+491702345678" }` (use Bob’s actual number from Contacts).
3. Save config and restart the WhatsApp bridge (or wait for the next message). Messages from that LID will then be accepted and the bot will reply.

The contact must have that E.164 number in whitelist or in Front Office with “Can reach your assistant” enabled; the mapping only tells the bridge which allowed number that @lid represents.

**Why the bridge often has no number for a LID:** We use whatever the Linked Device API (Baileys) gives us via `lidMapping.getPNForLID`. If WhatsApp never sends that LID→number mapping to the linked device, we have no way to “ask WhatsApp for the number” – there is no such API. That can happen even if the contact is **saved on your phone**: the phone app may show name/number (WhatsApp resolves there), but the multi-device protocol does not always sync that resolution to linked devices. So the dashboard shows an info that the chat has no number from WhatsApp; to allow the agent to reply, you can add a mapping in config (`whatsapp_config.lid_to_e164`). When Baileys does receive the mapping later (e.g. after more traffic), we show the chat as resolved and no config change is needed.

**Event-based LID resolution:** The Node bridge also builds a LID→E.164 map from events, so more chats can be resolved without config: (1) **senderPn** – when an incoming message has `remoteJid` ending with `@lid`, Baileys sometimes includes a Sender Phone Number (`msg.key.senderPn`); we store that and use it for resolution and for `getLidMappings`. (2) **chats.phoneNumberShare** – Baileys can emit this event with `lid` and `jid` (phone JID); we store that pair and re-emit the chat list so the dashboard shows the resolved number. This map is in-memory (per Node process); `getLidMappings` returns both Baileys `lidMapping` results and these event-derived entries, so the dashboard and inbound logic see all resolved LIDs.

### Troubleshooting: Agent sent duplicate messages or reported "I sent Alice a message" in voice

**Symptom:** When a contact (e.g. Alice) sends a voice message, the agent sends 2–3 extra voice messages proactively *to* Alice (via `send_whatsapp(to_phone=...)` tool calls) and then also sends the normal headless reply. The agent's final text reply includes phrases like "I have sent Alice a message via WhatsApp."

**Root cause:** The agent sees the contact's phone number in the front-office contact block and uses the `send_whatsapp` tool to "send the reply" - instead of just writing the reply as plain text (which the headless runner delivers automatically).

**Fix (applied in code):**
1. **Prompt guard:** The front-office input now explicitly states: *"CRITICAL: Do NOT call send_whatsapp - your reply text is automatically delivered to the contact. Just write your reply as plain text."*
2. **Tool guard:** `send_whatsapp` checks `_agent._front_office_mode` at the top of `run()`. When the agent is handling an inbound contact message (no explicit `to_phone`) and `_front_office_mode` is `True`, the tool returns `[TOOL BLOCKED]` immediately instead of sending.
3. **Sanitize filter:** `[TOOL BLOCKED]` is in `_INTERNAL_PHRASES` (`vaf/core/outbound_sanitizer.py` - the shared net the send tools and the headless runner both consume) so it is never delivered to the contact even if the agent quotes the blocked message.

### Troubleshooting: Error message was read aloud as voice ("Sorry, something went wrong: …")

**Symptom:** The agent crashes mid-response; the error handler sends an error text, but it is TTS-synthesized in the contact's language and sent as a voice note.

**Root cause:** The crash happened *before* the normal reply consumed `_voice_reply_pending` (the per-chat language flag set when a voice message is received). So the error-handler's text also gets TTS'd.

**Fix:** The error handler in `headless_runner.py` now explicitly pops `_voice_reply_pending` for the affected chat before calling `send_whatsapp_reply`, so error messages are always sent as plain text.

### Troubleshooting: Voice reply contained raw JSON (tool_calls)

If a voice or text reply to a contact contained literal JSON (e.g. `{"tool_calls": [{"function": {"name": "memory_save", ...}}]}`) instead of normal speech, the model output had leaked tool-call payloads into the reply. The headless runner now **strips** any such JSON from the text before sending to WhatsApp (and Telegram/Discord), so TTS and chat only receive clean text. Subagent summaries sent to Telegram, Discord, or WhatsApp are sanitized the same way (tool_calls JSON stripped) before delivery. If you still see this, ensure you are on the latest code and that the reply path goes through `headless_runner` (not a custom sender).

### Troubleshooting: Reply to contact A had context meant for contact B

Each WhatsApp chat has a **session** `whatsapp_{username}_{digits}` (e.g. `whatsapp_admin_491701234567` for Alice). Context and history are per session. If the agent replied to Alice with content that clearly referred to another contact (e.g. Bob), possible causes: (1) the same LID was used for two different people (e.g. one contact’s chat not yet resolved to E.164), so both shared one session; (2) two messages processed close together and a reply was associated with the wrong chat. Check **whatsapp_inbound.log** and **whatsapp_reply.log** for the order of `ENQUEUED session=…`, `HEADLESS task_source=whatsapp jid=…`, and `REPLY … jid=…` to see which session and JID each reply used. Ensure each contact has a distinct phone/JID in Front Office so they get distinct sessions.

---

## Voice Message Support

WhatsApp uses the same STT and TTS services as Telegram (`speech_stt_docker_url`, default port 5003; `speech_tts_docker_url`, default port 5002). Voice flows are bidirectional.

### Incoming Voice Messages

When a user sends a voice message:

1. **Node (Baileys)**: Detects `audioMessage`, downloads content from `msg.message.audioMessage` via `downloadContentFromMessage` (PTT → .ogg, else .opus), writes to a temp file (e.g. `os.tmpdir()/vaf_wa_voice_*.ogg`), logs the download (`voice downloaded: <path> (<bytes> bytes)`), and emits a JSON line: `{ "type": "message", "body": "<voice>", "voice_path": "/path/to/file.ogg", "from": "<jid>", ... }`.
2. **Python**: `_read_user_process()` receives the line; when `voice_path` is set and `body === "<voice>"`, it calls `_transcribe_voice_file(voice_path)`.
3. **Transcription**: The file is POSTed to the STT service (`/asr`, or `/transcribe` on 404) with the correct MIME type (audio/ogg or audio/opus). The response is parsed for `text` or `transcript` or `results[0].transcript`; the detected language is returned.
4. **Enqueue**: The transcribed text (or `<media:audio>` on failure) is enqueued as the user message. The detected language is stored in `_voice_reply_pending` so the agent reply can be sent as voice (TTS) in the same language.

### Transcription Flow (Python)

Implemented in `vaf/api/whatsapp_bridge.py` as `_transcribe_voice_file(voice_path)`:

```python
def _transcribe_voice_file(voice_path: str) -> tuple[Optional[str], Optional[str]]:
    """Transcribe a voice file via Docker Whisper STT. Returns (text, language) or (None, None)."""
    path_obj = Path(voice_path)
    if not path_obj.is_file():
        logger.warning("WhatsApp STT: voice file not found: %s", voice_path)
        return None, None
    file_size = path_obj.stat().st_size
    stt_url = (Config.get("speech_stt_docker_url") or "http://localhost:5003").strip().rstrip("/")
    asr_endpoint = f"{stt_url}/asr"
    logger.info("WhatsApp STT: transcribing %s (%d bytes) via %s", voice_path, file_size, asr_endpoint)
    with open(voice_path, "rb") as f:
        stt_resp = requests.post(
            asr_endpoint,
            files={"audio_file": ("voice.ogg", f, "audio/ogg")},
            params={"encode": "true", "output": "json"},
            timeout=60,
        )
    if not stt_resp.ok:
        logger.warning("WhatsApp STT failed: %s - %s", stt_resp.status_code, stt_resp.text[:200])
        return None, None
    data = stt_resp.json()
    text = (data.get("text") or "").strip()
    language = data.get("language", "en")
    return text or None, language
```

### Outgoing Voice (Auto-Reply)

When the agent sends a reply and the user had previously sent a voice message, the bridge checks `_voice_reply_pending` for that chat. If a language is present:

1. **TTS**: `_synthesize_voice_for_reply(text, lang)` POSTs to the TTS service (`/synthesize`, JSON: `text`, `language`, `format`: `"ogg"`), receives OGG bytes, writes to a temp file. Logs the TTS URL, response status, and file size on success; logs detailed error info on failure.
2. **Queue**: The reply is put on `_outgoing_queue` as `(username, chat_jid, text, voice_path, None, None)` (no req_id for this path).
3. **Sender**: `_sender_loop()` sends a JSON command to Node: `{ "cmd": "send_voice", "to": "<jid>", "path": "<absolute path>" }`.
4. **Node**: Reads the file, calls `sendMessage(to, { audio: buf, mimetype: "audio/ogg; codecs=opus" }, { sendAudioAsVoice: true })`, and emits `send_result` with success/failure.

### Outgoing Voice (Tool)

When the agent calls `send_whatsapp(message="...", voice_lang="de")` (in `vaf/tools/send_whatsapp.py`):

1. **TTS**: The tool calls its own `_synthesize_voice(text, lang)` (same TTS URL and `/synthesize` payload).
2. **Confirmation**: The tool calls `send_whatsapp_with_confirmation(..., voice_path=path, timeout=45)` so that the bridge can wait for Node’s `send_result` (req_id) and return a clear success or error message to the agent.

### Language Detection

Whisper returns the detected language in the STT response. VAF uses it to:

- Route the auto-reply to the correct TTS voice
- Maintain conversation language consistency
- Support multilingual voice conversations

---

## Message Handling

### Incoming Messages

1. **Node** emits a JSON line: `{ "type": "message", "from": "<jid>", "body": "...", "voice_path": "<path or omit>", "fromE164": "+49...", "selfChat": false, ... }`.
2. **Python** (`_dispatch_bridge_event`): Drops `selfChat` messages first (the linked account is the agent). Then resolves the sender against the registered main-user number (`explicit_pair`), the reply window (`open_conversation`: the store holds an outbound message to this number inside `reply_window_hours`) and Front Office contacts (`contact_fallback`), through `channel_ingress_policy.evaluate_ingress`. A rejected sender is logged and mirrored as a security event; nothing is stored.
3. **Voice**: If `voice_path` is set and `body === "<voice>"`, Python transcribes the file and replaces `body` with the transcript (or `<media:audio>` on failure); stores language in `_voice_reply_pending` for TTS reply.

#### LID (Linked ID)

WhatsApp uses **LID** (Linked ID) for some chat identifiers; JIDs may end with `@lid` instead of `@s.whatsapp.net`. LID identifies regular 1:1 contacts as well as the linked account's own chat. To avoid accepting messages from senders that match nothing:

- **Node (wa-bridge.js)**: For any `@lid` JID, the bridge does *not* assume self-chat. It resolves the LID to E.164 via Baileys' `lidMapping` and only sets `selfChat: true` when the resolved number is the linked account's own number. For `@s.whatsapp.net` chats, self-chat is the numeric part of the JID equalling the linked account's JID.
- **Python**: Drops on the Node-emitted `selfChat` flag only (never on `@lid` alone). For everyone else the resolved `fromE164` (or a manual `lid_to_e164` mapping) is what gets matched against the registered number, the contacts and the reply window; an unresolved `@lid` matches none of them and is rejected.
4. **Activity**: Appends to `chat_activity` (for dashboard) and to the per-user message store (`direction="in"`).
5. **Enqueue**: Task is added with `session_id = whatsapp_{username}_{digits}`, `input_text = body`, and metadata: `from_contact` (set for every sender except the registered main-user number), `ingress_reason` (`explicit_pair` / `contact_fallback` / `contact_fallback_override` / `open_conversation`), `whatsapp_chat_jid`, `voice_lang`, `user_scope_id`, `username`. When `inbound_to_agent` is `false`, this enqueue is skipped.

### Outgoing Queue and Node Commands

Outgoing items are tuples: `(username, chat_jid, text, voice_path, req_id, document_path)`. The sender loop in `whatsapp_bridge.py`:

- If `voice_path` is set: sends `{ "cmd": "send_voice", "to": chat_jid, "path": "<abs path>", "req_id": "<uuid>" }` to Node stdin.
- Else if `document_path` is set: sends `{ "cmd": "send_document", "to": chat_jid, "path": "<path>", "caption": "<text>", "req_id": "..." }`.
- Else: chunks text and sends one or more `{ "cmd": "send", "to": chat_jid, "text": "<chunk>" }`; only the last chunk includes `req_id` for delivery confirmation.

Node responds with `{ "type": "send_result", "req_id": "...", "success": true|false, "error": "..." }`. Python delivers this to the waiting caller (e.g. the `send_whatsapp` tool) via a per-request queue.

### Outbound Item and Node Command Reference

| Python outbound tuple | Node command | Description |
|------------------------|--------------|-------------|
| (username, chat_jid, text, None, req_id, None) | `send` | Text message; req_id on last chunk only |
| (username, chat_jid, text, voice_path, req_id, None) | `send_voice` | Voice message; Node reads file, sends with sendAudioAsVoice |
| (username, chat_jid, caption, None, req_id, document_path) | `send_document` | Document with optional caption |
| (getChats from API) | `getChats` | Node responds with `type: "chats", chats: [...]` |
| (get_avatar from the dashboard) | `getAvatar` (`jid`, `req_id`) | Baileys `profilePictureUrl(jid, "preview")`, downloaded by the Node; answers `type: "avatar"` with `found` and, when found, `mime` + `b64`. A hidden picture (privacy setting, 401/404) is `found: false`, not an error. Python caches the bytes and the "none" answer for 24 h under `users/<username>/whatsapp_avatars/` and runs one query at a time |
| (fetch_older_messages from the dashboard) | `fetchHistory` (`jid`, `count`, `oldestId`, `oldestFromMe`, `oldestTs`, `req_id`) | Baileys `fetchMessageHistory(count, oldestKey, oldestTsMs)`: asks the phone for `count` messages older than the oldest one in the store. Node answers `type: "fetch_history_result"` (did the request leave); the messages arrive later as a `messaging-history.set` of type ON_DEMAND and reach the store through the ordinary `history_messages` path. Python waits until the chat's row count grows (or 20 s) |

### Agent Tools

| Tool | Purpose | When to use |
|------|---------|-------------|
| `whatsapp_inbox` | List WhatsApp chats (chat_id, name, last_ts) | User asks to list or show WhatsApp conversations |
| `find_whatsapp_messages` | Search messages by query (body, chat name, sender); optional `chat_id` | User asks "find messages from X" or "what did X say in WhatsApp" |
| `read_whatsapp_chat` | Read messages from a chat (`chat_id`, `limit`) | Read full thread; use chat_id from inbox or find |
| `send_whatsapp` | Send text, voice (`voice_lang`), or document (`file_path`) | User asks to send something via WhatsApp; use contact’s preferred_language for voice_lang when sending to a contact |
| `whatsapp_call` | Placeholder (not implemented) | Do not use; voice/video calls are not supported. Use `send_whatsapp` for text or `send_whatsapp(voice_lang="...")` for voice messages. |

**Best practice:** For all WhatsApp communication (text, voice, documents), use `send_whatsapp`. The `whatsapp_call` tool is intentionally unimplemented and returns a message directing the agent to use `send_whatsapp` instead.

---

## Dashboard

The WhatsApp dashboard is available under **Settings → Connections → WhatsApp** (or the Dashboard tab). Implemented in `web/components/connections/WhatsAppDashboard.tsx`; data is provided by `GET /api/whatsapp/dashboard`.

The window is laid out like the mail client: the chat list on the left (name, newest message as preview from the message store, time, badge), the active conversation in the middle as bubbles with day separators and an in-chat search, and a gear in the header that opens the settings overlay. Every string comes from `settings.whatsappDashboard` in the message catalogues.

- **Names and pictures, as WhatsApp Web shows them**: the Node keeps a contact store from `contacts.upsert` (the phone's address-book names from the history sync and app-state `contactAction`, verified business names) and `contacts.update` / `msg.pushName` (the name a person set for themselves), keyed by JID with LID and phone JID pointing at one entry; the chat list resolves names through it, and every accepted inbound message is stored with that name (`chat_name`). A chat with no name anywhere still shows the number, an unresolved LID "Unknown chat". Pictures come from `GET /api/whatsapp/avatar?chat_id=...` (see `getAvatar` in the command table); a person who hides their picture gets the initials.
- **Contact book**: every chat-list update (throttled to once a minute per user) and every accepted inbound message with a name is folded into the contact book: a named direct chat with a phone number is matched by number and linked, or created with its WhatsApp name; groups, newsletters and number-only chats are not; "Can reach your assistant" is never set. Rules and the `links.whatsapp` field in [CONNECTIONS.md](CONNECTIONS.md#channel-links-whatsapp-today).
- **Header**: the linked account (`linked_phone` from `GET /api/whatsapp/status`) with the connection dot, a filter over the chat list, Refresh (re-syncs the chat list when connected, starts the bridge when it is enabled but not running), the gear, Close.
- **Chat badges**: **Owner** = registered main-user number (full chat), **Contact** = Front Office contact, **Conversation** = the agent wrote to this number inside the reply window (the conversation header says until when), **Assign number** = a `@lid` chat WhatsApp has not resolved to a number yet (the conversation header offers an input that calls `POST /api/whatsapp/lid-assign`), **Read-only** = everything else (session `type` values `owner` / `contact` / `conversation` / `unknown` plus `needs_assign` in `GET /api/whatsapp/dashboard`).
- **Conversation**: the messages of the chat come from the per-user **message store** (`GET /api/whatsapp/chat-messages?chat_id=...`, oldest first; "in" = the other side, "out" = what left the agent's number), not from the agent session: a chat that arrived through the history sync has a full conversation in the store and no session until the agent itself answers there. The Memory Learning counter still reads the session (`whatsapp_<user>_<digits>`) when one exists. **Load older messages** in the conversation header (`POST /api/whatsapp/chat-messages/older`, body `chat_id`, `count`) asks the phone for up to 50 messages older than the oldest stored one (`channel_message_store.oldest_message` is the cursor) and reloads the pane once they landed; WhatsApp hands over only what the phone still has, so "nothing older arrived" is a normal answer.
- **Conversation header**: name, badge, number and a one-line explanation of what the agent does with this chat; **Add as contact** on a conversation or read-only chat creates a contact with "Can reach your assistant" (`POST /api/contacts`). The footer names the mode (full agent, Front Office, read-only) and the message count until the next Memory Learning.
- **Settings (gear)**: *Agent number* with connection state, Restart bridge and Re-link; *Your number (main user)* to register or remove the number you chat from (`POST /api/whatsapp/whitelist/add` / `remove`; the linked account's own number is refused with HTTP 400); *Who else can write to your agent* listing the Front Office contacts with a link to the contacts dashboard; *Reply window* (`reply_window_hours`, saved through `PATCH /api/config`) with the `inbound_to_agent` switch; *Activity* (messages per 4-hour interval, 7 days) and the log path.
- **Connection status**: Indicator next to "Chats": green = WhatsApp connected, amber = bridge running but not connected, grey = bridge not started. Status is determined by ping/pong with the Node process.
- **Chat list**: Built from (1) Node’s chat list (Baileys), (2) `chat_activity` (incoming/outgoing activity), and (3) Front Office contacts (contacts with "Can reach your assistant" and a WhatsApp channel) so that chats appear even before Baileys has synced them. **Deduplication:** Each contact is shown once; the same person as E.164 and as @lid is merged into one row when LID→E.164 is known (config or Node). Phone numbers are normalized to a single leading `+` to avoid duplicate entries (e.g. `++49...`). The **message count** shown per chat is the **session message count** (number of messages in that chat’s session file), so it matches the session history and "Memory Learning" view when you open the chat. **Contact names** are resolved from the contact list; matching uses canonical phone form (0-prefix German numbers, e.g. `0152...`, are treated as `+49...`) so names appear even if the contact was stored as `0152...` and the session uses `+49152...`.
- **Refresh**: Re-fetches chat list and pings the bridge.
- **Reconnection**: If the bridge is running but WhatsApp is not connected, VAF periodically restarts the bridge. You can also use "Restart bridge" or Settings → Stop then Start.

Sessions (chats) in the dashboard are keyed by E.164-style `chat_id` (e.g. `+491761234567`). The main Web UI chat list excludes channel sessions (IDs starting with `whatsapp_`, `telegram_`, `discord_`) so that WhatsApp conversations appear only in the WhatsApp dashboard.

---

## Docker Requirements

For voice message support (STT and TTS), the same containers as for Telegram are used:

```bash
docker compose -f docker-compose.memory.yml up -d
```

Required containers:

- **vaf-stt** (port 5003) – Whisper STT for transcription of incoming voice messages
- **vaf-tts** (port 5002) – Piper TTS for voice synthesis (auto-reply and `send_whatsapp(voice_lang="...")`)

Config keys: `speech_stt_docker_url` (default `http://localhost:5003`), `speech_tts_docker_url` (default `http://localhost:5002`). See [SPEECH_FEATURES.md](../web-ui/SPEECH_FEATURES.md) for details.

### Verify Services

```bash
# Check STT
curl -X POST "http://localhost:5003/asr?encode=true&output=json" \
  -F "audio_file=@test.ogg"

# Check TTS with OGG output
curl -X POST http://localhost:5002/synthesize \
  -H "Content-Type: application/json" \
  -d '{"text": "Test", "language": "de", "format": "ogg"}' \
  -o test.ogg
```

---

## Troubleshooting

### Bridge running, WhatsApp not connected (amber status)

The dashboard shows **Bridge running, WhatsApp not connected** when the Node process is alive but the Baileys socket has not reached `connection=open`. The Python bridge sends a **ping** to Node; Node replies with **pong** and `connected: true` only when `connectionState === "open"`. So amber means either the socket never opened or it closed.

**Common causes:**

| Cause | What you see in `logs/whatsapp_qr.log` | Action |
|-------|----------------------------------------|--------|
| Still connecting | `connection=connecting` or no `connection=open` yet | Wait 10–30 s and click Refresh. |
| **Bad MAC / session keys** | `Failed to decrypt message with any known session` and `Session error: Error: Bad MAC` | **Reset & get new QR code**, scan again. Stored Signal/session keys are invalid or out of sync; only a fresh link fixes it. |
| Session invalid | `connection=close status=401` or `device_removed` | **Reset & get new QR code**, scan again. |
| Restart required | `connection=close status=515` or `516` | Baileys auto-reconnects; wait 20–30 s or use **Restart bridge**. |
| Logged out | `connection=close` with loggedOut | Reset and scan a new QR code. |
| Network/firewall | Repeated `connection=close` or timeout | Test [web.whatsapp.com](https://web.whatsapp.com) on the same PC; disable VPN; avoid VPS if WhatsApp blocks the IP. |

**Best practice:** When the status stays amber, open `logs/whatsapp_qr.log`. Search for `connection.update: connection=close` (for status codes) or for **`Bad MAC`** / **`Failed to decrypt`** – in that case the session keys are broken; reset and scan a new QR code. The **status code** (e.g. 401, 515, 516) tells you whether to Reset (401, device_removed, loggedOut) or wait/restart (515/516). VAF automatically restarts the bridge periodically when it detects this state; if reconnection still fails, use **Restart bridge** in the dashboard or Settings → Stop then Start.

### Bridge Not Responding / No Reply

1. **Check bridge is enabled:** `whatsapp_config.enabled` should be `true` in config.
2. **Ensure bridge process is running:** Settings → Connections → WhatsApp toggle on; after VAF restart the bridge starts automatically when enabled.
3. **Verify sender is whitelisted:** Your phone number (E.164) must be in the config whitelist or in a contact with "Can reach your assistant" and that contact must have the WhatsApp number in Channels. Check `logs/whatsapp_inbound.log` for `ACCEPT` vs `REJECT not_whitelist`.
4. **Human takeover:** If somebody recently answered in that contact chat from the agent's phone itself, the agent intentionally does not reply for 10 minutes. Check `logs/whatsapp_inbound.log` or `logs/whatsapp_qr.log` for `owner_control: skip reply (owner has control, 10 min not elapsed)`. After 10 minutes without you sending another message, the next contact message will be answered by the agent again.
5. **Diagnose in `logs/whatsapp_qr.log`:** Python logs each received event as `[Python] got type='message'` (or `chats`, `connected`, etc.). If Node logs `emitting message to Python` but you never see `[Python] got type='message'`, the read loop may have failed (e.g. encoding). The bridge uses UTF-8 for Node stdout/stderr; restart the **full VAF application** and try again. Look for `[Python] JSON decode error` or `[Python] FATAL read loop` if the loop crashed.

### Diagnostic logs (`logs/whatsapp_qr.log`)

Both Node stderr and Python bridge logs are written here. Use it to verify that messages reach Python and how they are handled.

| Source | Log line | Meaning |
|--------|----------|---------|
| Node | `emitting message to Python from=<jid>` | Node sent a message event to stdout. |
| Node | `message resolve error: ...` / `message emit failed: ...` | LID resolution or stdout write failed; message may still be sent. |
| Python | `[Python] got type='message'` (or `chats`, `connected`, `connection_closed`) | Python received and parsed this event type from Node stdout. |
| Python | `[inbound] MESSAGE from=<jid>` | Incoming message is being processed. |
| Python | `[inbound] REJECT` / `ACCEPT` / `ENQUEUED` | Sender not allowed / allowed / task enqueued. |
| Python | `[inbound] owner_sent chat=...` | Account owner sent a message in that contact chat; owner has control for 10 min. |
| Python | `[inbound] owner_control: skip reply ...` | Agent skip: owner has control, 10 min not elapsed. |
| Python | `[Python] JSON decode error: ...` | A non-JSON or empty line was read (e.g. stray output); that line is skipped. |
| Python | `[Python] FATAL read loop: ...` | The stdout read loop crashed; restart VAF. |
| Node | `syncChats fetchMessageHistory: Cannot read properties of undefined (reading 'remoteJid')` | Obsolete: syncChats no longer calls fetchMessageHistory (that API is per-chat message history, not full chat list). The full chat list comes only from WhatsApp’s initial `messaging-history.set` on connect. |

Best practice: if the bot does not reply, check that you see `[Python] got type='message'` and then `[inbound] ACCEPT` or `ENQUEUED` after Node’s `emitting message to Python`. If not, see "Bridge Not Responding / No Reply" and "Front Office Contact Does Not Get a Reply" above.

### QR Code / Linking

- **Node.js not found:** Install Node.js 18+ and ensure it is in your PATH.
- **Bridge dependency install failed:** the wizard or `logs/whatsapp_qr.log` (`[deps] FAILED ...`) names the npm error. Usual causes: npm missing (install Node.js 18+ with npm), no network, or `vaf/whatsapp_node/` not writable. Fallback: run `npm install` there by hand.
- **QR or terminal issues:** Stderr of the Node process (including `connection.update` events) is logged to `logs/whatsapp_qr.log`. After scanning, WhatsApp may disconnect with 515/516; the bridge then reconnects with stored credentials. If "logging in" stays stuck, check network/firewall.
- **Session expired:** When the bridge needs a new QR but cannot show it, VAF disables the bridge. Use Reset and scan a new QR code.
- **Repeated `GET /api/whatsapp/qr` in the server log:** This endpoint is polled by the WhatsApp setup wizard (every ~1.5s before a QR is shown, ~2.5s after) to detect when the QR appears and when your phone finishes linking. Polling runs **only while the setup wizard is open** and stops when you close it. Seeing it continuously means the wizard is open in a browser tab; close it to stop the polling.

### Voice: STT Fails (Incoming Voice Not Transcribed)

1. **STT service:** Ensure the STT container is running and `speech_stt_docker_url` is correct (default port 5003). Test with the curl command above.
2. **Node download:** Check Node stderr for `voice downloaded: <path> (<bytes> bytes)`. If missing, Baileys failed to download the audio from WhatsApp. The bridge uses `downloadContentFromMessage(msg.message.audioMessage, ...)` - ensure you are on a compatible Baileys version.
3. **File path:** Node writes the voice file to a temp directory and sends the absolute path to Python. Python must be able to read that path (same machine). Look for `WhatsApp STT: voice file not found` if the file disappeared before transcription.
4. **Transcription:** Look for `WhatsApp STT: transcribing <path> (<bytes> bytes) via <url>` to confirm the request was sent. On failure, `WhatsApp STT failed: <status> - <body>` shows the HTTP status and error from the STT service.
5. **Success:** `WhatsApp voice transcribed: lang=<lang>, text=<preview>` confirms a successful transcription.

### Voice: TTS / Outgoing Voice Not Received

1. **TTS service:** Ensure the TTS container is running and `speech_tts_docker_url` is correct (default port 5002). Test with the curl command above.
2. **Synthesis logs:** Look for `WhatsApp TTS: synthesizing lang=<lang> text_len=<n> url=<url>` to confirm the TTS request was sent. On failure: `WhatsApp TTS failed: <status> - <body>` (HTTP error), `WhatsApp TTS: empty response body` (no audio returned), or `WhatsApp TTS: unknown audio format (magic: ...)` (unexpected format).
3. **Success:** `WhatsApp TTS: wrote <bytes> bytes to <path>` confirms the OGG file was created.
4. **Node send:** The sender passes an absolute path to the OGG file to Node. Node must run on the same machine and be able to read that path. Check Node stderr for `Voice file not found: <path>` or `Voice send failed: <error>`.
5. **Mimetype:** Outgoing voice uses `audio/ogg; codecs=opus` for correct playback on recipients' devices. If the TTS service returns WAV instead of OGG, the file is saved with `.wav` extension and sent with `audio/mpeg` mimetype (may not play as voice note).

### send_whatsapp Reports Success but No Message on Phone

1. **logs/whatsapp_reply.log:** Look for `SENDER ok` (message was sent to Node) or `DROPPED process_not_running` / `ERROR` (send failed before reaching Node).
2. **Phone number format:** Whitelist and `to_phone` must use E.164 (e.g. `+491761234567`). Incorrect format can lead to wrong JID and the message not reaching the recipient.
3. **Bridge/Node:** Restart the bridge (Settings → Connections → Stop then Start). Ensure WhatsApp shows as "Linked" after QR scan.

### Front Office Contact Does Not Get a Reply

The contact must have their **WhatsApp number** stored in the contact’s **Channels** (type "phone" or "WhatsApp", value E.164). If "Can reach your assistant" is enabled but the contact has no WhatsApp channel, incoming messages are rejected. Add the number in Settings → Connections → Contacts → edit contact → Channels. **Diagnose:** Check `logs/whatsapp_qr.log` for `[inbound] MESSAGE`, `[inbound] REJECT` (with `allowed_count`), or `[inbound] ACCEPT`/`ENQUEUED`. Python also logs each received event as `[Python] got type='message'` and any `[Python] JSON decode error` or `[Python] FATAL read loop` in the same file. If you see "voice downloaded" but no `[inbound]` or `[Python] got type='message'` lines, restart the **full VAF application** (not only the bridge) so the bridge runs with UTF-8 encoding for Node pipes, then try again.

### Chat List Empty or Duplicate Number (e.g. ++49...)

- **Empty list:** WhatsApp (Baileys) syncs chats over time; the list may be empty until someone messages you or after a refresh and wait. Use the dashboard Refresh button; check `GET /api/whatsapp/dashboard/debug` for `raw_chats_count`. Restarting the bridge and waiting 30–60 seconds can help.
- **Duplicate with double plus:** Phone numbers are normalized to a single leading `+` when appending to chat_activity and when building the dashboard list. If you still see `++49...`, ensure you are on a version that includes this normalization; existing activity entries may be normalized when read.

### 401 / device_removed

Often related to the VAF machine or network:

- Disable VPN and try again.
- Some server/VPS IPs are blocked by WhatsApp; a home or office PC may work better.
- After repeated failures, wait 24 hours and try again, or use a different network/machine.
- After each failure, use "Reset & get new QR code" before scanning again.

---

## Security

### Whitelist-Only Replies

Only numbers in the config whitelist or in contacts with "Can reach your assistant" and a WhatsApp channel can send messages and receive replies. The account owner’s own messages (self-chat, e.g. “saved messages”) are also accepted. All other senders are ignored (no reply, no notification). Chats identified by LID (`@lid`) are only treated as self-chat when the resolved E.164 matches the linked account owner’s number; other LID chats are subject to the same whitelist/contact checks as normal JIDs.

### Per-User Auth and Isolation

Each VAF user has a separate WhatsApp session. Credentials and Baileys state are stored under the user’s directory (e.g. `~/.vaf/users/<username>/whatsapp/`). One Node process per user (or shared only when a single user is configured) keeps sessions isolated.

### Sensitive Data

- Do not commit `whatsapp_config` (or any config containing secrets) or the per-user WhatsApp auth directories to version control.
- Auth directories are created and used by the bridge; ensure appropriate filesystem permissions.

---

## Related Documentation

- [SPEECH_FEATURES.md](../web-ui/SPEECH_FEATURES.md) – TTS/STT services and WhatsApp voice flow summary
- [CONNECTIONS.md](CONNECTIONS.md) – High-level setup and troubleshooting for all connections
- [DOCKER_SERVICES.md](../setup/DOCKER_SERVICES.md) – Container setup (STT, TTS, etc.)
- [FRONT_OFFICE.md](../agents/FRONT_OFFICE.md) – Front office and contacts (if present)
- [MEMORY_SYSTEM.md](../memory/MEMORY_SYSTEM.md) – Memory and user scopes (session context)
