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
- **Python (vaf/api/whatsapp_bridge.py)**: Spawns and manages the Node process, maintains `_outgoing_queue`, implements STT/TTS for voice, and enqueues incoming messages to the VAF task queue with session ID `whatsapp_{username}_{digits}`.

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
        "user_scope_id": "00000000-0000-0000-0000-000000000001",
        "vaf_username": "admin"
      }
    ]
  }
}
```

Authentication (Baileys session) is stored per user under `~/.vaf/users/<username>/whatsapp/` (or the platform-specific data directory). Do not commit these directories to version control.

### Configuration Options

| Key | Type | Description |
|-----|------|-------------|
| `enabled` | bool | Enable/disable the WhatsApp bridge |
| `inbound_to_agent` | bool | When `true`, incoming messages are enqueued and the agent replies (two-way). When `false`, WhatsApp is send-only: bot can send you content but incoming messages do not trigger the agent |
| `whitelist` | array | List of allowed phone numbers (E.164) with `phone_number`, `user_scope_id`, `vaf_username` |

### Whitelist and Front Office

- **Config whitelist**: Each entry maps a phone number (E.164, e.g. `+491761234567`) to a VAF user. Only these numbers can send messages and receive replies.
- **Contacts (Front Office)**: Contacts in the VAF contact list with **Can reach your assistant** enabled can also send messages to your assistant (handled in your context). For WhatsApp, the contact **must** have that WhatsApp number stored in their **Channels** (type "phone" or "WhatsApp"). If the contact has no WhatsApp channel, incoming messages from that number are rejected.

The bridge builds the allowed set from the config whitelist plus all WhatsApp/phone channel values from contacts with "Can reach your assistant" enabled.

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
2. **Python** (`_read_user_process`): Resolves allowed senders (config whitelist + contacts with Front Office and WhatsApp channel). If the sender is not allowed, the message is ignored (no reply).
3. **Voice**: If `voice_path` is set and `body === "<voice>"`, Python transcribes the file and replaces `body` with the transcript (or `<media:audio>` on failure); stores language in `_voice_reply_pending` for TTS reply.
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
| `whatsapp_call` | Placeholder | Not implemented; use send_whatsapp for communication |

---

## Dashboard

The WhatsApp dashboard is available under **Settings → Connections → WhatsApp** (or the Dashboard tab). Implemented in `web/components/connections/WhatsAppDashboard.tsx`; data is provided by `GET /api/whatsapp/dashboard`.

- **Connection status**: Indicator next to "Chats": green = WhatsApp connected, amber = bridge running but not connected, grey = bridge not started. Status is determined by ping/pong with the Node process.
- **Chat list**: Built from (1) Node’s chat list (Baileys), (2) `chat_activity` (incoming/outgoing activity), and (3) Front Office contacts (contacts with "Can reach your assistant" and a WhatsApp channel) so that chats appear even before Baileys has synced them. Phone numbers are normalized to a single leading `+` to avoid duplicate entries (e.g. `++49...`).
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

### Bridge Not Responding / No Reply

1. **Check bridge is enabled:** `whatsapp_config.enabled` should be `true` in config.
2. **Ensure bridge process is running:** Settings → Connections → WhatsApp toggle on; after VAF restart the bridge starts automatically when enabled.
3. **Verify sender is whitelisted:** Your phone number (E.164) must be in the config whitelist or in a contact with "Can reach your assistant" and that contact must have the WhatsApp number in Channels. Check `logs/whatsapp_inbound.log` for `ACCEPT` vs `REJECT not_whitelist`.

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

The contact must have their **WhatsApp number** stored in the contact’s **Channels** (type "phone" or "WhatsApp", value E.164). If "Can reach your assistant" is enabled but the contact has no WhatsApp channel, incoming messages from that number are rejected (not in the allowed set). Add the number in Settings → Connections → Contacts → edit contact → Channels.

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

Only numbers in the config whitelist or in contacts with "Can reach your assistant" and a WhatsApp channel can send messages and receive replies. All other senders are ignored (no reply, no notification).

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
