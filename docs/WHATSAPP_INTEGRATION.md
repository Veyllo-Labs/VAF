# WhatsApp Integration

VAF provides a full-featured WhatsApp integration via a Node.js bridge (Baileys) with support for text messages, voice messages, document delivery, and bidirectional voice communication. The integration runs as a linked device (similar to WhatsApp Web) and uses the same TTS/STT services as Telegram.

## Overview

The WhatsApp bridge allows users to interact with VAF through WhatsApp, supporting:

- **Text Messages**: Standard text-based conversations
- **Voice Messages**: Incoming voice transcribed via Whisper STT; outgoing voice synthesized via TTS (auto-reply or via `send_whatsapp(voice_lang="...")`)
- **Documents**: Agent can send PDF, DOCX, and other files via `send_whatsapp(file_path="...")`
- **Per-User Isolation**: Each VAF user has a separate WhatsApp session; credentials stored under `~/.vaf/users/<username>/whatsapp/`
- **Whitelist**: Only configured phone numbers (E.164) and contacts with "Can reach your assistant" (Front Office) can send messages and receive replies
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

---

## Configuration

### Setup

1. Install Node.js (>= 18) and ensure it is in your PATH.
2. Run `npm install` in `vaf/whatsapp_node/` (project root: directory containing `vaf/`).
3. In the Web UI, go to **Settings → Connections** and click **Connect** on WhatsApp.
4. Scan the displayed QR code with WhatsApp on your phone (Linked Devices).
5. Your linked phone number is automatically added to the whitelist.
6. Turn the connection **on**; the bridge starts when enabled and restarts automatically after VAF restarts if WhatsApp is enabled.

### Config File

WhatsApp configuration is stored in `~/.vaf/config.json` (or your platform config path) under `whatsapp_config`:

```json
{
  "whatsapp_config": {
    "enabled": true,
    "inbound_to_agent": true,
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

**Best practice:** Use the same `user_scope_id` as the Web UI for that user. For the local admin, use the value of `local_admin_scope_id` in config (set automatically by bootstrap when the first admin is created, or set manually). The bridge resolves missing `user_scope_id` in whitelist entries via `get_local_admin_scope_id()`, so the local admin's WhatsApp sessions use the same scope as CLI and localhost — one identity across Web, CLI, and WhatsApp.

Authentication (Baileys session) is stored per user under `~/.vaf/users/<username>/whatsapp/` (or the platform-specific data directory). Do not commit these directories to version control.

### Configuration Options

| Key | Type | Description |
|-----|------|-------------|
| `enabled` | bool | Enable/disable the WhatsApp bridge |
| `inbound_to_agent` | bool | When `true`, incoming messages are enqueued and the agent replies (two-way). When `false`, WhatsApp is send-only: bot can send you content but incoming messages do not trigger the agent |
| `whitelist` | array | List of allowed phone numbers (E.164) with `phone_number`, `user_scope_id`, `vaf_username` |
| `chat_sync_interval_sec` | number | Interval in seconds for periodic full chat list sync (default: 600 = 10 min). The bridge requests `getChats` from the Node so the bot and dashboard have the latest chat list (names, last_ts). Set to `0` to disable. |

### Whitelist and Front Office

- **Config whitelist**: Each entry maps a phone number (E.164, e.g. `+491761234567`) to a VAF user. Only these numbers can send messages and receive replies.
- **Contacts (Front Office)**: Contacts in the VAF contact list with **Can reach your assistant** enabled can also send messages to your assistant (handled in your context). For WhatsApp, the contact **must** have that WhatsApp number stored in their **Channels** (type "phone" or "WhatsApp"). If the contact has no WhatsApp channel, incoming messages from that number are rejected.

The bridge builds the allowed set from the config whitelist plus all WhatsApp/phone channel values from contacts with "Can reach your assistant" enabled.

**Chat list sync:** So the bot and dashboard always see the latest chats (e.g. for `whatsapp_inbox`, Connections UI), the bridge runs a **periodic sync** every 10 minutes by default: it sends `getChats` to the Node, which returns the full chat list (Baileys `chatStore`); the Python side merges that with **chat_activity** and with **chats from the message store** (all chat_ids that have at least one message in `whatsapp_messages.db`), so the inbox is persistent like mail/Telegram. You can change the interval with `whatsapp_config.chat_sync_interval_sec` (seconds; `0` = disabled). **All chats** from the linked device (including @lid chats) appear in the list; the agent can use `whatsapp_inbox` to list them and `read_whatsapp_chat(chat_id=...)` to read messages. The chat list is whatever WhatsApp sends in the initial **messaging-history.set** (on connect) plus any **chats.upsert** / **chats.update** / **messages.upsert** (new chats when someone writes). After a bridge reconnect the Node may have only a subset of chats; the dashboard also shows chats with **activity** (including rejected senders), so numbers that have written at least once stay visible as Read-only. If you only see a few chats (e.g. only newsletters or recent ones), WhatsApp may be sending the full list in batches – wait 1–2 minutes after connecting and click **Refresh** (🔄) again; the Node merges every batch into the list. **"Alle Chats von WhatsApp laden"** (Refresh when connected) re-requests the current list from the Node; it does not trigger a new history sync from the phone (Baileys has no API for “sync all chat list again”). Message history the agent can read is what the bridge has received or sent (no full server-side history fetch yet).

- **@lid (Linked ID)**: WhatsApp uses **LID** (Link ID) as a privacy-preserving identifier: it often replaces the phone number (JID `number@s.whatsapp.net`) with something like `XXXXXXXXXX@lid`, especially in groups and Communities, so participants don’t see each other’s numbers. The LID is **account-specific**, stable across chats, and sent over WhatsApp Web/Multi-Device. **On the phone app**, if you have the person in your **phone contacts**, WhatsApp can resolve the LID and show the saved name and number – but that resolution happens on the phone. **Our bridge uses the Linked Device API** (Baileys): it only knows a LID’s number if WhatsApp **sends that mapping to the linked device**. Baileys exposes this via `lidMapping.getPNForLID`; we call it (e.g. when building the dashboard) and show `resolved_e164_from_node` when we get a result. WhatsApp does **not** always sync “saved contact → LID resolution” to linked devices, so even with the contact saved on your phone, the bridge may never receive the number for that LID. When we do receive it, the chat is shown as resolved (no manual step). When we don’t, the only way to allow replies is a manual mapping in `whatsapp_config.lid_to_e164`. **Inbound rule:** A message is accepted only when the sender JID or the resolved `fromE164` matches the whitelist or a Front Office contact. **Unresolved @lid** (no `fromE164` from Node and no `lid_to_e164` entry) are **rejected** so that strangers cannot reach the assistant.

- **Self-chat (admin = bridge number)**: When the linked WhatsApp number (the one that scanned the QR code) is the same as the admin’s number (e.g. you message yourself or use “Saved Messages”), the bridge does **not** hand the message to the agent. It is stored as a note/backlog only (message store + activity), so the agent does not “talk to itself”. In logs you will see `SELF_CHAT stored as note (no agent reply)`.

- **Owner takeover**: When the account owner (main user) replies themselves in a contact chat (e.g. you open the chat with baba and send a message from your phone), the bridge records “owner has control” for that chat. The agent **pauses replying** in that chat for **10 minutes** after your last message. Incoming messages from the contact are still stored (message store + activity) but not handed to the agent until 10 minutes have passed with no further message from you. In logs you will see `owner_sent chat=… → owner has control` and `owner_control: skip reply (owner has control, 10 min not elapsed)`. **Echo handling:** Only real owner messages (sent from the phone) set owner control. When the bridge sends a reply, WhatsApp echoes it back with `fromMe: true`; the Node tracks recent sends per chat (text and voice) and does **not** emit `owner_sent` for those echoes, so the agent can keep replying to the next contact message.

### Best practices

- **Whitelist format:** Use E.164 for all phone numbers (e.g. `+491761234567`). The bridge normalizes JIDs; leading zeros or missing country codes can cause mismatches.
- **user_scope_id and username:** Use the same `user_scope_id` and username as the Web UI for that user. For the local admin, use `local_admin_scope_id` and the configured local admin username so Web, CLI, WhatsApp, and other tools (e.g. `list_contacts`, `get_contact`, `send_whatsapp`) resolve the same identity. Consistent identity avoids "no contacts" or "no Telegram/WhatsApp contact" when the agent runs from the Web UI or from a bridge.
- **Credentials:** Do not commit `~/.vaf/users/<username>/whatsapp/` (or the platform data dir equivalent) to version control; it contains the Baileys session.
- **Send-only mode:** Set `inbound_to_agent: false` when you only want the bot to send you content (e.g. reports, voice notes); incoming messages will not trigger the agent.
- **Front Office:** For contacts who can reach your assistant via WhatsApp, add their number in the contact’s **Channels** (type "phone" or "WhatsApp"). Without a WhatsApp channel, messages from that number are rejected.

### Troubleshooting: Number shown as "admin" but I never added it

In the dashboard (Settings → Connections → WhatsApp), sessions are shown as **admin** only when the phone number is in the **config whitelist** (`whatsapp_config.whitelist`). Numbers that come only from a Front Office contact (with "Can reach your assistant") appear as **contact**, not admin.

If a number you did not add appears as admin and the agent writes to it:

1. **Remove from whitelist:** In the Web UI (Settings → Connections → WhatsApp), remove that number if the UI offers it, or call `POST /api/whatsapp/whitelist/remove` with body `{"phone_number": "+49…"}` (use the full E.164 number).
2. **Or edit config:** Open your VAF config (e.g. `~/.vaf/config.json` or `%APPDATA%\\vaf\\config.json` on Windows), find `whatsapp_config.whitelist`, and delete the entry with that `phone_number`. Save and restart the WhatsApp bridge if needed.

The whitelist is the only source for the "admin" label; removing the entry stops that number from being treated as admin and from being allowed to receive/send via the bridge.

### Troubleshooting: Strange number or LID – agent replied to someone I didn't add

If the agent wrote to a "number" that is a long digit string and not a real phone number (e.g. `+12345678901234` or similar), it is likely a **WhatsApp LID** (the numeric part of a `…@lid` chat). Previously, the bridge accepted **any** unresolved @lid when any whitelist/contact existed, so unknown senders could get through. That is fixed: unresolved @lid are now **rejected** unless the sender is matched by resolved E.164 or whitelist/contact.

**Logs to check:** Under the VAF log directory (e.g. `logs/` in the project, or `Platform.data_dir()/logs`), see:

- **whatsapp_inbound.log** – each inbound message: `ACCEPT`, `REJECT`, `REJECT unresolved @lid`, `SELF_CHAT`, etc.
- **whatsapp_qr.log** – QR flow and bridge events.

Search for `from=…@lid` or `REJECT unresolved @lid` to confirm rejections. New messages from unknown LIDs are rejected unless you add a manual mapping (see below).

### Troubleshooting: Bot doesn't reply to a contact (e.g. Baba) – REJECT unresolved @lid

If a **known** Front Office contact (e.g. Baba) uses a chat that WhatsApp sends as **@lid** (e.g. `55877994332394@lid`) and the Node never sends `fromE164`, the bridge will **reject** their messages after the security fix (`REJECT unresolved @lid from=55877994332394@lid`). To allow that contact again, add a **manual LID→E.164 mapping** in config so the bridge can treat that LID as their phone number:

1. In **whatsapp_inbound.log** note the rejected JID (e.g. `55877994332394@lid`).
2. In your VAF config (`~/.vaf/config.json` or `%APPDATA%\\vaf\\config.json`), under `whatsapp_config`, add or extend `lid_to_e164` with that JID as key and the contact’s **real E.164 number** (as in Front Office) as value. Example: `"lid_to_e164": { "55877994332394@lid": "+4915256564444" }` (use Baba’s actual number from Contacts).
3. Save config and restart the WhatsApp bridge (or wait for the next message). Messages from that LID will then be accepted and the bot will reply.

The contact must have that E.164 number in whitelist or in Front Office with “Can reach your assistant” enabled; the mapping only tells the bridge which allowed number that @lid represents.

**Why the bridge often has no number for a LID:** We use whatever the Linked Device API (Baileys) gives us via `lidMapping.getPNForLID`. If WhatsApp never sends that LID→number mapping to the linked device, we have no way to “ask WhatsApp for the number” – there is no such API. That can happen even if the contact is **saved on your phone**: the phone app may show name/number (WhatsApp resolves there), but the multi-device protocol does not always sync that resolution to linked devices. So the dashboard shows an info that the chat has no number from WhatsApp; to allow the agent to reply, you can add a mapping in config (`whatsapp_config.lid_to_e164`). When Baileys does receive the mapping later (e.g. after more traffic), we show the chat as resolved and no config change is needed.

**Event-based LID resolution:** The Node bridge also builds a LID→E.164 map from events, so more chats can be resolved without config: (1) **senderPn** – when an incoming message has `remoteJid` ending with `@lid`, Baileys sometimes includes a Sender Phone Number (`msg.key.senderPn`); we store that and use it for resolution and for `getLidMappings`. (2) **chats.phoneNumberShare** – Baileys can emit this event with `lid` and `jid` (phone JID); we store that pair and re-emit the chat list so the dashboard shows the resolved number. This map is in-memory (per Node process); `getLidMappings` returns both Baileys `lidMapping` results and these event-derived entries, so the dashboard and inbound logic see all resolved LIDs.

### Troubleshooting: Voice reply contained raw JSON (tool_calls)

If a voice or text reply to a contact contained literal JSON (e.g. `{"tool_calls": [{"function": {"name": "memory_save", ...}}]}`) instead of normal speech, the model output had leaked tool-call payloads into the reply. The headless runner now **strips** any such JSON from the text before sending to WhatsApp (and Telegram/Discord), so TTS and chat only receive clean text. If you still see this, ensure you are on the latest code and that the reply path goes through `headless_runner` (not a custom sender).

### Troubleshooting: Reply to contact A had context meant for contact B

Each WhatsApp chat has a **session** `whatsapp_{username}_{digits}` (e.g. `whatsapp_admin_4917642996812` for Anne). Context and history are per session. If the agent replied to Anne with content that clearly referred to another contact (e.g. Baba), possible causes: (1) the same LID was used for two different people (e.g. one contact’s chat not yet resolved to E.164), so both shared one session; (2) two messages processed close together and a reply was associated with the wrong chat. Check **whatsapp_inbound.log** and **whatsapp_reply.log** for the order of `ENQUEUED session=…`, `HEADLESS task_source=whatsapp jid=…`, and `REPLY … jid=…` to see which session and JID each reply used. Ensure each contact has a distinct phone/JID in Front Office so they get distinct sessions.

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
2. **Python** (`_read_user_process`): Resolves allowed senders (config whitelist + contacts with Front Office and WhatsApp channel). Messages from the account owner (self-chat, e.g. “saved messages”) are also allowed. If the sender is not allowed, the message is ignored (no reply).
3. **Voice**: If `voice_path` is set and `body === "<voice>"`, Python transcribes the file and replaces `body` with the transcript (or `<media:audio>` on failure); stores language in `_voice_reply_pending` for TTS reply.

#### Self-chat and LID (Linked ID)

WhatsApp uses **LID** (Linked ID) for some chat identifiers; JIDs may end with `@lid` instead of `@s.whatsapp.net`. LID is used for more than just “saved messages” (self-chat)—it can also identify regular 1:1 contacts. To avoid accepting messages from non-whitelisted contacts:

- **Node (wa-bridge.js)**: For any `@lid` JID, the bridge does *not* assume self-chat. It resolves the LID to E.164 via Baileys’ `lidMapping` and only sets `selfChat: true` when the resolved number matches the linked account owner’s number. For `@s.whatsapp.net` chats, self-chat is determined by comparing the numeric part of the JID with the owner’s JID.
- **Python**: Uses the Node-emitted `selfChat` flag only (must not treat a JID as self-chat solely because it ends with `@lid`). Together with `fromE164` (when present) and the whitelist/contact list, only senders in the allowed set or with `selfChat: true` are accepted; all others are rejected and not forwarded to the agent.
4. **Activity**: Appends to `chat_activity` (for dashboard) and optionally to the message store.
5. **Enqueue**: Task is added with `session_id = whatsapp_{username}_{digits}`, `input_text = body`, and metadata: `from_contact`, `whatsapp_chat_jid`, `voice_lang`, `user_scope_id`, `username`. When `inbound_to_agent` is `false`, this enqueue is skipped.

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

Config keys: `speech_stt_docker_url` (default `http://localhost:5003`), `speech_tts_docker_url` (default `http://localhost:5002`). See [SPEECH_FEATURES.md](./SPEECH_FEATURES.md) for details.

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
4. **Owner takeover:** If you (the account owner) recently replied in that contact chat from your phone, the agent intentionally does not reply for 10 minutes. Check `logs/whatsapp_inbound.log` or `logs/whatsapp_qr.log` for `owner_control: skip reply (owner has control, 10 min not elapsed)`. After 10 minutes without you sending another message, the next contact message will be answered by the agent again.
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
- **wa-bridge.js not found:** Run `npm install` in `vaf/whatsapp_node/` from the project root.
- **QR or terminal issues:** Stderr of the Node process (including `connection.update` events) is logged to `logs/whatsapp_qr.log`. After scanning, WhatsApp may disconnect with 515/516; the bridge then reconnects with stored credentials. If "logging in" stays stuck, check network/firewall.
- **Session expired:** When the bridge needs a new QR but cannot show it, VAF disables the bridge. Use Reset and scan a new QR code.

### Voice: STT Fails (Incoming Voice Not Transcribed)

1. **STT service:** Ensure the STT container is running and `speech_stt_docker_url` is correct (default port 5003). Test with the curl command above.
2. **Node download:** Check Node stderr for `voice downloaded: <path> (<bytes> bytes)`. If missing, Baileys failed to download the audio from WhatsApp. The bridge uses `downloadContentFromMessage(msg.message.audioMessage, ...)` — ensure you are on a compatible Baileys version.
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

- [SPEECH_FEATURES.md](./SPEECH_FEATURES.md) – TTS/STT services and WhatsApp voice flow summary
- [CONNECTIONS.md](./CONNECTIONS.md) – High-level setup and troubleshooting for all connections
- [DOCKER_SERVICES.md](./DOCKER_SERVICES.md) – Container setup (STT, TTS, etc.)
- [FRONT_OFFICE.md](./FRONT_OFFICE.md) – Front office and contacts (if present)
- [MEMORY_SYSTEM.md](./MEMORY_SYSTEM.md) – Memory and user scopes (session context)
