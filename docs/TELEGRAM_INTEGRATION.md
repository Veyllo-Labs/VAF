# Telegram Integration

VAF provides a full-featured Telegram bot integration with support for text messages, voice messages, and bidirectional voice communication.

## Overview

The Telegram bridge allows users to interact with VAF through Telegram, supporting:

- **Text Messages**: Standard text-based conversations
- **Voice Messages**: Automatic transcription via Whisper STT
- **Voice Replies**: Agent responses as voice messages (TTS)
- **Incoming Documents**: PDF, DOCX, XLSX, PPTX, TXT, MD, CSV, JSON, XML – downloaded, text extracted via Librarian, passed to agent as context
- **Photos**: Placeholder – replies with "coming soon"; full OCR/Vision implementation planned
- **Multi-User Support**: User whitelisting with scope isolation
- **Memory Integration**: Conversations are stored in VAF's memory system

---

## Architecture

```
Telegram User
     │
     ▼ (voice/text message)
┌─────────────────────────────┐
│  Telegram Bot API           │
│  (python-telegram-bot)      │
└─────────────────────────────┘
     │
     ▼
┌─────────────────────────────┐
│  telegram_bridge.py         │
│  ├─ handle_message()        │  ◄── Text messages
│  ├─ handle_voice()          │  ◄── Voice messages
│  ├─ handle_document()       │  ◄── PDF, DOCX, etc. (extract → agent)
│  ├─ handle_photo()          │  ◄── Placeholder (OCR/Vision planned)
│  ├─ _transcribe_voice()     │  ◄── Whisper STT
│  └─ _send_voice_reply()     │  ◄── TTS response
└─────────────────────────────┘
     │
     ▼
┌─────────────────────────────┐
│  VAF Agent                  │
│  (per-user session)         │
└─────────────────────────────┘
     │
     ▼
┌─────────────────────────────┐
│  Response Queue             │
│  _sender_loop()             │
└─────────────────────────────┘
     │
     ▼
Telegram User (text or voice reply)
```

---

## Configuration

### Bot Setup

1. Create a bot via [@BotFather](https://t.me/botfather) on Telegram
2. Copy the bot token
3. Configure in `~/.vaf/config.json`:

```json
{
  "telegram_config": {
    "enabled": true,
    "bot_token": "YOUR_BOT_TOKEN",
    "bot_username": "your_bot_name",
    "verified": true,
    "whitelist": [
      {
        "telegram_user_id": "123456789",
        "telegram_username": "YourUsername",
        "user_scope_id": "00000000-0000-0000-0000-000000000001",
        "vaf_username": "admin"
      }
    ]
  }
}
```

### Configuration Options

| Key | Type | Description |
|-----|------|-------------|
| `enabled` | bool | Enable/disable Telegram bot |
| `bot_token` | string | Bot token from BotFather |
| `bot_username` | string | Bot username (without @) |
| `verified` | bool | Set to `true` after token verification |
| `whitelist` | array | List of authorized users |

### User Whitelist

Each whitelist entry maps a Telegram user to a VAF user scope:

| Field | Description |
|-------|-------------|
| `telegram_user_id` | Telegram user ID (numeric) |
| `telegram_username` | Telegram username |
| `user_scope_id` | VAF user scope UUID |
| `vaf_username` | VAF username for display |

---

## Voice Message Support

### Incoming Voice Messages

When a user sends a voice message:

1. **Download**: Audio file is downloaded from Telegram (OGA/Opus format)
2. **Transcription**: Sent to Docker Whisper STT container
3. **Language Detection**: Whisper returns detected language
4. **Processing**: Transcribed text is processed by the agent
5. **Voice Reply**: Response is synthesized and sent as voice message

### Transcription Flow

```python
async def _transcribe_voice(bot_token: str, file_id: str) -> tuple[Optional[str], Optional[str]]:
    """Download voice message and transcribe via Docker Whisper STT."""

    # 1. Get file info from Telegram
    bot = telegram.Bot(token=bot_token)
    file_info = await bot.get_file(file_id)

    # 2. Download audio
    audio_bytes = await file_info.download_as_bytearray()

    # 3. Send to Whisper STT
    stt_url = Config.get("speech_stt_docker_url", "http://localhost:5003")
    response = requests.post(
        f"{stt_url}/asr",
        files={"audio_file": ("voice.oga", audio_bytes, "audio/ogg")},
        params={"encode": "true", "output": "json"}
    )

    # 4. Parse response
    result = response.json()
    return result.get("text"), result.get("language")
```

### Voice Reply Flow

```python
async def _send_voice_reply(bot_token: str, chat_id: str, text: str, language: str) -> bool:
    """Synthesize TTS and send as Telegram voice message."""

    # 1. Request OGG format from TTS
    tts_url = Config.get("speech_tts_docker_url", "http://localhost:5002")
    response = requests.post(
        f"{tts_url}/synthesize",
        json={
            "text": text,
            "language": language[:2].lower(),
            "format": "ogg"
        },
        timeout=60
    )

    # 2. Send as voice message
    bot = telegram.Bot(token=bot_token)
    await bot.send_voice(
        chat_id=chat_id,
        voice=BytesIO(response.content),
        filename="response.ogg"
    )
    return True
```

### Language Detection

Whisper automatically detects the spoken language and returns it in the response:

```json
{
  "text": "Hallo, wie geht es dir?",
  "language": "de"
}
```

VAF uses this to:
- Route the response to the correct TTS voice (German, English, French)
- Maintain conversation language consistency
- Enable multilingual voice conversations

---

## Message Handling

### Text Messages

```python
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming text messages."""

    # 1. Verify user is whitelisted
    user = get_whitelisted_user(update.effective_user.id)
    if not user:
        return  # Silently ignore non-whitelisted users

    # 2. Get or create session
    session = get_or_create_session(user["user_scope_id"])

    # 3. Queue message for processing
    await queue_message(
        chat_id=update.effective_chat.id,
        text=update.message.text,
        user_scope_id=user["user_scope_id"]
    )
```

### Voice Messages

```python
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming voice messages."""

    # 1. Verify user is whitelisted
    user = get_whitelisted_user(update.effective_user.id)
    if not user:
        return

    # 2. Get voice file ID
    voice = update.message.voice
    file_id = voice.file_id

    # 3. Transcribe via Whisper
    text, detected_lang = await _transcribe_voice(bot_token, file_id)

    if not text:
        await update.message.reply_text("Could not transcribe voice message.")
        return

    # 4. Queue with voice language for TTS reply
    await queue_message(
        chat_id=update.effective_chat.id,
        text=text,
        user_scope_id=user["user_scope_id"],
        voice_lang=detected_lang  # Triggers voice reply
    )
```

---

## Response Queue

VAF uses an async response queue to handle message sending:

```python
async def _sender_loop():
    """Background loop that sends responses to Telegram."""

    while True:
        item = await response_queue.get()

        chat_id = item["chat_id"]
        text = item["text"]
        voice_lang = item.get("voice_lang")

        if voice_lang:
            # Send as voice message
            await _send_voice_reply(bot_token, chat_id, text, voice_lang)
        else:
            # Send as text message
            await bot.send_message(chat_id=chat_id, text=text)
```

### Response Item Structure

| Field | Type | Description |
|-------|------|-------------|
| `chat_id` | string | Telegram chat ID |
| `text` | string | Response text (or caption for documents) |
| `voice_lang` | string | Language code for TTS (optional). Set when user sent voice (auto-reply) or when agent calls `send_telegram` with `voice_lang` (proactive voice). |
| `file_path` | string | Full path or folder alias (e.g. `Downloads\file.pdf`) for send_document (optional) |
| `user_scope_id` | string | VAF user scope UUID |

### Proactive Document Delivery

When the user asks for a document (e.g. "Send me the contract") via Telegram, the agent uses `send_telegram` with `file_path`:

```
send_telegram(message="Here is your contract", file_path="/path/to/invoice.pdf")
```

The Telegram bridge sends the file via `sendDocument` API with the message as caption. Supports PDF, DOCX, and other document types.

### Proactive Voice Delivery

When the user asks for a voice message via Telegram (e.g. "send it as voice via Telegram"), the agent uses `send_telegram` with `voice_lang`:

```
send_telegram(message="Here is the summary", voice_lang="de")
```

The bridge synthesizes audio via TTS and sends it as a Telegram voice message.

**Path resolution:** The `file_path` argument supports:
- Absolute paths (e.g. `C:\Users\...\Downloads\file.pdf`)
- Folder aliases: `Downloads`, `Desktop`, `Documents` (and German variants like `Herunterladen`) resolve to the user's home directory
- Relative paths with aliases (e.g. `Downloads\file.pdf`) are resolved correctly regardless of the agent's working directory

### Incoming Documents (User → Agent)

When the user sends a **document** (PDF, DOCX, XLSX, PPTX, TXT, etc.) via Telegram:

1. Bridge downloads the file via Telegram Bot API
2. Librarian extracts text (PyPDF2, python-docx, OCR for scanned PDFs)
3. Agent receives: `[Document: filename.pdf] (User caption if any)\n\n--- Document content ---\n{extracted text}`

The agent can then answer questions about the document, summarize it, or extract data.

**Supported formats:** `.pdf`, `.docx`, `.xlsx`, `.pptx`, `.xls`, `.txt`, `.md`, `.csv`, `.json`, `.xml`

### Incoming Photos (Placeholder)

Photo support is **planned** but not yet implemented. When a user sends a photo, the bot replies with a "coming soon" message. Future implementation options:

- **OCR (pytesseract):** Extract text from receipts, screenshots, photographed documents
- **Vision API:** Pass image to multimodal LLM (GPT-4V, Claude) for content understanding

See the inline comment block in `telegram_bridge.py` above `handle_photo` for implementation notes.

---

## Docker Requirements

For voice message support, ensure these containers are running:

```bash
docker compose -f docker-compose.memory.yml up -d
```

Required containers:
- `vaf-stt` (port 5003) - Whisper STT for transcription
- `vaf-tts` (port 5002) - Piper TTS for voice synthesis

### Verify Services

```bash
# Check STT
curl -X POST "http://localhost:5003/asr?encode=true&output=json" \
  -F "audio_file=@test.wav"

# Check TTS with OGG output
curl -X POST http://localhost:5002/synthesize \
  -H "Content-Type: application/json" \
  -d '{"text": "Test", "format": "ogg"}' \
  -o test.ogg
```

---

## Idle Timeout

To conserve resources, Telegram sessions can have an idle timeout:

```json
{
  "telegram_idle_timeout": 120,
  "telegram_debounce_seconds": 5
}
```

| Setting | Default | Description |
|---------|---------|-------------|
| `telegram_idle_timeout` | 120 | Minutes before idle session cleanup |
| `telegram_debounce_seconds` | 5 | Debounce time for rapid messages |

---

## Activity Tracking

VAF tracks Telegram activity for session management:

```json
{
  "telegram_config": {
    "chat_activity": [
      {
        "chat_id": "123456789",
        "user_scope_id": "00000000-0000-0000-0000-000000000001",
        "ts": 1770322598.073,
        "direction": "in"
      }
    ]
  }
}
```

---

## Troubleshooting

### Bot Not Responding

1. **Check bot is enabled:**
   ```json
   "telegram_config": { "enabled": true }
   ```

2. **Verify token:**
   ```bash
   curl https://api.telegram.org/bot<YOUR_TOKEN>/getMe
   ```

3. **Check user is whitelisted:**
   Verify `telegram_user_id` in whitelist matches your Telegram ID.

### Voice Transcription Fails

1. **Check STT container:**
   ```bash
   docker logs vaf-stt
   ```

2. **Test STT directly:**
   ```bash
   curl -X POST "http://localhost:5003/asr?encode=true&output=json" \
     -F "audio_file=@test.oga"
   ```

### Voice Reply Not Sending

1. **Check TTS container:**
   ```bash
   docker logs vaf-tts
   ```

2. **Test TTS OGG output:**
   ```bash
   curl -X POST http://localhost:5002/synthesize \
     -H "Content-Type: application/json" \
     -d '{"text": "Test", "format": "ogg"}' \
     -o test.ogg

   # Verify file
   file test.ogg
   # Should show: Ogg data, Opus audio
   ```

### Get Your Telegram User ID

1. Send a message to [@userinfobot](https://t.me/userinfobot) on Telegram
2. It will reply with your user ID

---

## Security

### User Whitelisting

Only users in the whitelist can interact with the bot. Non-whitelisted users are silently ignored.

### Scope Isolation

Each whitelisted user is mapped to a VAF user scope, ensuring:
- Separate memory contexts
- Isolated conversation history
- Per-user agent sessions

### Token Security

- Store bot tokens securely in `~/.vaf/config.json`
- Never commit tokens to version control
- Use environment variables in production

---

## Related Documentation

- [SPEECH_FEATURES.md](./SPEECH_FEATURES.md) - TTS/STT technical details
- [DOCKER_SERVICES.md](./DOCKER_SERVICES.md) - Container setup
- [MEMORY_SYSTEM.md](./MEMORY_SYSTEM.md) - Memory and user scopes
