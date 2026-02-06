# Speech Integration (TTS & STT)

This document provides comprehensive documentation for VAF's Text-to-Speech (TTS) and Speech-to-Text (STT) capabilities across all interfaces: CLI, Web UI, and Telegram.

## Overview

VAF supports fully offline speech processing using Docker containers for high-quality neural TTS and Whisper-based STT. The system supports multiple languages and interfaces.

### Key Features

- **Offline Operation**: No cloud dependencies; all processing runs locally via Docker
- **Multi-Language Support**: Automatic language detection and routing to appropriate TTS voices
- **Multi-Interface**: Speech works across CLI, Web UI, and Telegram
- **Bidirectional Voice**: Telegram supports receiving and sending voice messages
- **Browser-Native Audio**: Web UI handles audio conversion client-side for optimal compatibility

---

## Architecture

### Docker Services

| Service | Container | Port | Purpose |
|---------|-----------|------|---------|
| TTS Multi-Lang | `vaf-tts` | 5002 | Piper TTS with German, English, French voices |
| TTS English | `vaf-tts-en` | 5004 | Dedicated English TTS (Kusal) |
| TTS French | `vaf-tts-fr` | 5006 | Dedicated French TTS (Siwis) |
| STT | `vaf-stt` | 5003 | Whisper ASR (onerahmet/openai-whisper-asr-webservice) |

### Core Components

```
vaf/
├── core/
│   ├── speech.py              # SpeechManager (TTS/STT engine)
│   ├── speech_fillers.py      # Filler phrases by language
│   ├── agent.py               # Integration: _speak(), _speak_filler()
│   └── web_server.py          # WebSocket handlers for Web UI speech
├── api/
│   └── telegram_bridge.py     # Telegram voice message handling
docker/
└── tts-multilang/
    ├── app.py                 # Flask TTS API with multi-voice support
    ├── Dockerfile             # Container build with ffmpeg
    └── requirements.txt       # Python dependencies
```

---

## Text-to-Speech (TTS)

### Engines

VAF supports multiple TTS engines configured via `speech_tts_engine`:

| Engine | Description | Use Case |
|--------|-------------|----------|
| `docker` | HTTP TTS service (Piper in container) | **Recommended** - Best quality |
| `piper` | Local Piper binary | Offline without Docker |
| `system` | pyttsx3 (SAPI5/nsss/espeak) | Fallback |

### Multi-Language Docker TTS

The `vaf-tts` container (`docker/tts-multilang`) supports multiple languages with automatic voice selection:

**Supported Languages and Voices:**

| Language | Voice Model | Quality |
|----------|-------------|---------|
| German (de) | `de_DE-thorsten-high` | High |
| English (en) | `en_US-kusal-medium` | Medium |
| French (fr) | `fr_FR-siwis-medium` | Medium |

**API Endpoint:** `POST /synthesize`

```json
{
  "text": "Hello, this is a test.",
  "language": "en",
  "format": "wav"
}
```

**Parameters:**
- `text` (required): Text to synthesize
- `language` (optional): ISO 639-1 code (`de`, `en`, `fr`). Default: `de`
- `format` (optional): Output format (`wav` or `ogg`). Default: `wav`

**Response:** Binary audio data (WAV or OGG/Opus)

### OGG/Opus Output

The TTS container supports OGG/Opus output for Telegram voice messages:

```python
# Request OGG format
response = requests.post(
    "http://localhost:5002/synthesize",
    json={"text": "Hello!", "language": "en", "format": "ogg"}
)
# Returns OGG/Opus audio (libopus codec, 64kbps)
```

The conversion uses ffmpeg built into the container - no local ffmpeg installation required.

### Content Filtering

Before TTS playback, content is cleaned via `_clean_markdown()`:

- **Removed:** Code blocks, thinking tags (`<think>`, `<redacted_reasoning>`), emojis, markdown formatting, long URLs
- **Kept:** Pure answer text (the "blue text" in TUI)

---

## Speech-to-Text (STT)

### Engines

| Engine | Description | Configuration |
|--------|-------------|---------------|
| `docker` | Whisper via HTTP API | **Recommended** |
| `local` | faster-whisper + ffmpeg | Requires local installation |

### Docker Whisper STT

VAF uses the `onerahmet/openai-whisper-asr-webservice` container.

**API Endpoint:** `POST /asr`

```bash
curl -X POST "http://localhost:5003/asr?encode=true&output=json" \
  -F "audio_file=@recording.wav"
```

**Response:**
```json
{
  "text": "This is the transcribed text.",
  "language": "en"
}
```

**Key Features:**
- Automatic language detection via `language` field
- Supports WAV, MP3, OGG, WebM input formats
- Returns detected language code for routing TTS responses

---

## Web UI Voice Integration

### Architecture

```
Browser (page.tsx)          WebSocket          Backend (web_server.py)
     │                          │                        │
     ├─ MediaRecorder ──────────┼──────────────────────► │
     │  (WebM/Opus)             │                        │
     ├─ convertToWav() ─────────┼──────────────────────► │
     │  (16-bit PCM WAV)        │   stt_stream           │
     │                          │                        ├─► Docker STT
     │ ◄────────────────────────┼────────────────────────┤
     │    {text, language}      │                        │
     │                          │                        │
     ├─ Request TTS ────────────┼──────────────────────► │
     │                          │   tts_stream           ├─► Docker TTS
     │ ◄────────────────────────┼────────────────────────┤
     │    audio/wav (base64)    │                        │
     ▼                          ▼                        ▼
```

### Browser Audio Processing

The Web UI handles audio conversion client-side for optimal STT compatibility:

**`convertToWav()` Function:**
1. Records audio via MediaRecorder (WebM/Opus format)
2. Decodes to AudioBuffer using Web Audio API
3. Resamples to 16kHz mono
4. Encodes as 16-bit PCM WAV
5. Sends to backend via WebSocket

```typescript
async function convertToWav(blob: Blob): Promise<Blob> {
  const arrayBuffer = await blob.arrayBuffer();
  const audioContext = new AudioContext({ sampleRate: 16000 });
  const audioBuffer = await audioContext.decodeAudioData(arrayBuffer);

  // Convert to 16-bit PCM WAV
  const samples = audioBuffer.getChannelData(0);
  const pcmData = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i++) {
    pcmData[i] = Math.max(-32768, Math.min(32767, samples[i] * 32768));
  }

  // Build WAV file with proper header
  return new Blob([wavData], { type: 'audio/wav' });
}
```

### WebSocket Protocol

**STT Request:**
```json
{
  "type": "stt_stream",
  "data": "<base64-encoded-wav>",
  "format": "wav"
}
```

**STT Response:**
```json
{
  "type": "stt_result",
  "text": "Transcribed text here",
  "language": "de"
}
```

**TTS Request:**
```json
{
  "type": "tts_stream",
  "text": "Text to speak"
}
```

**TTS Response:**
```json
{
  "type": "tts_audio",
  "audio": "<base64-encoded-wav>"
}
```

---

## Telegram Voice Messages

### Overview

VAF supports bidirectional voice communication on Telegram:
1. **Incoming:** User sends voice message → Whisper transcribes → Agent processes
2. **Outgoing:** Agent response → TTS synthesizes → Sent as voice message

### Voice Message Flow

```
User Voice Message (OGA/Opus)
         │
         ▼
┌─────────────────────┐
│ telegram_bridge.py  │
│ handle_voice()      │
└─────────────────────┘
         │
         ▼
┌─────────────────────┐
│ _transcribe_voice() │
│ - Download from TG  │
│ - Send to Whisper   │
│ - Get text+language │
└─────────────────────┘
         │
         ▼
┌─────────────────────┐
│ Agent Processing    │
│ (with voice_lang    │
│  context)           │
└─────────────────────┘
         │
         ▼
┌─────────────────────┐
│ _send_voice_reply() │
│ - TTS with language │
│ - OGG output        │
│ - Send as voice msg │
└─────────────────────┘
```

### Implementation Details

**Voice Transcription (`_transcribe_voice`):**

```python
async def _transcribe_voice(bot_token: str, file_id: str) -> tuple[Optional[str], Optional[str]]:
    """Download voice message from Telegram and transcribe via Docker Whisper STT."""
    # 1. Get file path from Telegram
    file_info = await bot.get_file(file_id)

    # 2. Download audio file
    audio_bytes = await file_info.download_as_bytearray()

    # 3. Send to Whisper STT
    stt_url = Config.get("speech_stt_docker_url", "http://localhost:5003")
    response = requests.post(
        f"{stt_url}/asr",
        files={"audio_file": ("voice.oga", audio_bytes, "audio/ogg")},
        params={"encode": "true", "output": "json"}
    )

    # 4. Return transcription and detected language
    result = response.json()
    return result.get("text"), result.get("language")
```

**Voice Reply (`_send_voice_reply`):**

```python
async def _send_voice_reply(bot_token: str, chat_id: str, text: str, language: str) -> bool:
    """Synthesize TTS audio and send as voice message to Telegram."""
    # 1. Request OGG format from TTS
    tts_url = Config.get("speech_tts_docker_url", "http://localhost:5002")
    response = requests.post(
        f"{tts_url}/synthesize",
        json={"text": text, "language": language[:2].lower(), "format": "ogg"}
    )

    # 2. Send as Telegram voice message
    await bot.send_voice(
        chat_id=chat_id,
        voice=BytesIO(response.content),
        filename="response.ogg"
    )
```

### Language Detection

Whisper returns the detected language in the response. VAF uses this to:
1. Route the agent response to the correct TTS voice
2. Maintain conversation language consistency
3. Enable multilingual voice conversations

---

## Configuration

### Config File (`~/.vaf/config.json`)

```json
{
  "speech_tts_enabled": true,
  "speech_tts_engine": "docker",
  "speech_tts_docker_url": "http://localhost:5002",
  "speech_tts_docker_url_de": "http://localhost:5002",
  "speech_tts_docker_url_en": "http://localhost:5004",
  "speech_tts_docker_url_fr": "http://localhost:5006",

  "speech_stt_enabled": true,
  "speech_stt_engine": "docker",
  "speech_stt_docker_url": "http://localhost:5003",

  "speech_language": "de-DE",
  "speech_mic_index": 0,

  "stt_wake_word_enabled": true,
  "stt_wake_word": "hey_jarvis"
}
```

### Configuration Options

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `speech_tts_enabled` | bool | `false` | Enable TTS output |
| `speech_tts_engine` | string | `"docker"` | TTS engine: `docker`, `piper`, `system` |
| `speech_tts_docker_url` | string | `""` | Default Docker TTS URL |
| `speech_tts_docker_url_de` | string | `""` | German TTS URL |
| `speech_tts_docker_url_en` | string | `""` | English TTS URL |
| `speech_tts_docker_url_fr` | string | `""` | French TTS URL |
| `speech_stt_enabled` | bool | `false` | Enable STT input |
| `speech_stt_engine` | string | `"docker"` | STT engine: `docker`, `local` |
| `speech_stt_docker_url` | string | `""` | Docker STT URL |
| `speech_language` | string | `"de-DE"` | Default language |
| `speech_mic_index` | int | `0` | Microphone device index |

---

## Starting Docker Services

### Start All Services

```bash
docker compose -f docker-compose.memory.yml up -d
```

### Verify Services

```bash
docker ps --filter "name=vaf-"
```

Expected containers:
- `vaf-tts` (port 5002)
- `vaf-tts-en` (port 5004)
- `vaf-tts-fr` (port 5006)
- `vaf-stt` (port 5003)

### Test TTS

```bash
curl -X POST http://localhost:5002/synthesize \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello, this is a test.", "language": "en"}' \
  --output test.wav
```

### Test STT

```bash
curl -X POST "http://localhost:5003/asr?encode=true&output=json" \
  -F "audio_file=@test.wav"
```

---

## Troubleshooting

### TTS Not Working

1. **Check Docker containers are running:**
   ```bash
   docker ps --filter "name=vaf-tts"
   ```

2. **Verify config setting:**
   ```json
   "speech_tts_engine": "docker"
   ```
   (Not `"chatterbox"` or `"piper"`)

3. **Test TTS endpoint directly:**
   ```bash
   curl -X POST http://localhost:5002/synthesize \
     -H "Content-Type: application/json" \
     -d '{"text": "Test"}' -o /dev/null -w "%{http_code}"
   ```

### STT Returns 422 Error

1. **Check audio format:** STT expects proper audio files. Web UI converts to WAV automatically.

2. **Verify field name:** API expects `audio_file`, not `file`.

3. **Check container logs:**
   ```bash
   docker logs vaf-stt
   ```

### Web UI Microphone Issues

1. **Browser permissions:** Ensure microphone access is granted.

2. **Device selection:** Check `speech_mic_index` in config.

3. **Console errors:** Open browser DevTools → Console for detailed errors.

### Telegram Voice Not Working

1. **Verify STT container:**
   ```bash
   docker logs vaf-stt
   ```

2. **Check TTS OGG support:**
   ```bash
   curl -X POST http://localhost:5002/synthesize \
     -H "Content-Type: application/json" \
     -d '{"text": "Test", "format": "ogg"}' -o test.ogg
   ```

3. **Verify Telegram bot token and permissions.**

---

## CLI Commands

| Command | Description |
|---------|-------------|
| `l` or `/listen` | Start speech-to-text input |
| `/halt`, `/stop`, `/quiet` | Stop TTS playback |

---

## Supported Languages

### TTS (Piper Neural Voices)

40+ languages including:
- **Major:** English, German, French, Spanish, Chinese, Italian, Portuguese, Russian
- **Nordic:** Danish, Finnish, Icelandic, Norwegian, Swedish
- **Eastern Europe:** Czech, Hungarian, Polish, Romanian, Slovak, Slovenian, Serbian, Ukrainian, Bulgarian
- **Middle East/Asia:** Arabic, Persian, Turkish, Georgian, Kazakh, Nepali, Vietnamese
- **Others:** Catalan, Welsh, Greek, Luxembourgish, Dutch, Swahili

### STT (Whisper)

100+ languages with automatic detection.

### Filler Words

Configured in `vaf/core/speech_fillers.py`:
- German (de), English (en), Turkish (tr), Spanish (es), French (fr)
- Italian (it), Portuguese (pt), Russian (ru), Chinese (zh), Arabic (ar)

---

## Building the TTS Container

To rebuild the multi-language TTS container:

```bash
cd docker/tts-multilang
docker build -t vaf-tts-multilang:latest .
```

The container includes:
- Piper TTS with ONNX runtime
- Pre-downloaded voice models (DE, EN, FR)
- ffmpeg for OGG/Opus conversion
- Flask API server

---

## API Reference

### TTS API

**Endpoint:** `POST /synthesize`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `text` | string | Yes | Text to synthesize |
| `language` | string | No | Language code (default: `de`) |
| `format` | string | No | Output format: `wav` or `ogg` (default: `wav`) |

**Response:** Binary audio data

### STT API

**Endpoint:** `POST /asr`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `audio_file` | file | Yes | Audio file (WAV, MP3, OGG, WebM) |
| `encode` | string | No | Set to `true` |
| `output` | string | No | Set to `json` for JSON response |

**Response:**
```json
{
  "text": "Transcribed text",
  "language": "en"
}
```
