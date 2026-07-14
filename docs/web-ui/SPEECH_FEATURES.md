# Speech Integration (TTS & STT)

This document provides comprehensive documentation for VAF's Text-to-Speech (TTS) and Speech-to-Text (STT) capabilities across all interfaces: CLI, Web UI, and Telegram.

## Overview

VAF's speech processing is local by default: Docker containers provide high-quality neural TTS (Piper) and Whisper-based STT. Optionally, an admin can select a cloud voice provider (ElevenLabs or OpenAI) per direction; the local lane remains the fallback. The system supports multiple languages and interfaces.

### Key Features

- **Local by Default**: All processing runs locally via Docker unless an admin selects a cloud voice provider
- **Optional Cloud Providers**: ElevenLabs and OpenAI for TTS and STT, selectable independently (`speech_tts_provider` / `speech_stt_provider`), with automatic fallback to the local lane on any API error
- **Multi-Language Support**: Automatic language detection and routing to appropriate TTS voices
- **Multi-Interface**: Speech works across CLI, Web UI, and Telegram
- **Bidirectional Voice**: Telegram and WhatsApp support receiving and sending voice messages (STT for incoming, TTS for outgoing)
- **Browser-Native Audio**: Web UI handles audio conversion client-side for optimal compatibility

---

## Architecture

### Docker Services

| Service | Container | Port | Purpose |
|---------|-----------|------|---------|
| TTS Multi-Lang | `vaf-tts` | 5002 | Piper TTS (single container, multi-language, voices installed on demand) |
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
│   ├── telegram_bridge.py     # Telegram voice message handling
│   └── whatsapp_bridge.py      # WhatsApp voice (STT + TTS reply)
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
| `chatterbox` | Chatterbox-style HTTP TTS server (`speech_tts_chatterbox_url`) | Alternative HTTP TTS backend |
| `piper` | Local Piper binary | Offline without Docker |
| `system` | macOS `say` command only (pyttsx3 removed — caused 1-4 GB RAM explosion on Windows via SAPI/comtypes) | macOS fallback only |

When `speech_tts_provider` is set (`elevenlabs` or `openai`), the cloud lane takes
precedence over `speech_tts_engine`. On any provider error (quota, rate limit,
network) the request falls back to the configured local engine. See
[Cloud provider lane](#cloud-provider-lane-elevenlabs--openai).

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

When `speech_stt_provider` is set (`elevenlabs` or `openai`), the cloud lane takes
precedence over `speech_stt_engine`, with automatic fallback to the local lane on
any API error. See [Cloud provider lane](#cloud-provider-lane-elevenlabs--openai).

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

## Cloud provider lane (ElevenLabs / OpenAI)

Implemented in `vaf/core/speech_api.py` (mirroring the vision pattern in
`vaf/core/vision_infer.py`): a never-raise choke point that is consulted BEFORE
the local engine dispatch. Selection is explicit opt-in only; an empty provider
key means the local Docker lane. On ANY provider problem (missing key, quota
402, rate limit 429, timeout, non-audio response) the functions return `None`
and the caller degrades to the local engine - a voice turn never breaks because
a cloud API is down. All call sites go through `vaf/core/speech_client.py`, so
the Web UI, Telegram, WhatsApp, and the CLI mic all honour the same provider
selection.

### Configuration keys (admin-write-only)

| Key | Values | Meaning |
|-----|--------|---------|
| `speech_tts_provider` | `""`, `elevenlabs`, `openai` | Cloud TTS provider; `""` = local engine |
| `speech_tts_api_model` | model ID | `""` = default (`eleven_flash_v2_5` / `gpt-4o-mini-tts`) |
| `speech_tts_api_voice` | voice ID / name | `""` = default (ElevenLabs Rachel / OpenAI `alloy`) |
| `speech_stt_provider` | `""`, `elevenlabs`, `openai` | Cloud STT provider; `""` = local engine |
| `speech_stt_api_model` | model ID | `""` = default (`scribe_v2` / `whisper-1`) |
| `api_key_elevenlabs` | key | ElevenLabs API key (read-redacted for non-admins) |

The OpenAI lane reuses the existing `api_key_openai`. ElevenLabs is an
audio-only vendor and is deliberately NOT part of the LLM provider catalog
(see [PROVIDER_MODES.md](../llm/PROVIDER_MODES.md)).

### Request contracts

- **ElevenLabs TTS**: `POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}`
  with header `xi-api-key` and query `output_format=wav_24000` (browser/CLI) or
  `opus_48000_64` (messenger voice notes). `language_code` is sent only for the
  flash models; `eleven_multilingual_v2` auto-detects from the text.
- **ElevenLabs STT (Scribe)**: `POST https://api.elevenlabs.io/v1/speech-to-text`
  with multipart field `file` (the local Whisper container uses `audio_file`) and
  `model_id`. The response carries `text` and `language_code`, which keeps the
  reply-in-same-language pairing for voice notes working.
- **OpenAI TTS**: `POST https://api.openai.com/v1/audio/speech` with
  `response_format: "wav"`. OGG requests are converted locally via ffmpeg.
  The endpoint caps `input` at 4096 characters; longer texts are truncated.
  Voice availability is model-dependent: `tts-1`/`tts-1-hd` accept the 9
  classic voices, `gpt-4o-mini-tts` additionally accepts `ballad`, `verse`,
  `marin`, `cedar` (OpenAI recommends `marin`/`cedar` for quality).
- **OpenAI STT**: `POST https://api.openai.com/v1/audio/transcriptions`.
  `response_format: "verbose_json"` (which carries the detected language) is
  supported by `whisper-1` only; the `gpt-4o-*-transcribe` models are called
  with plain `json` and return no language, so voice replies default to
  English with them. For the reply-in-same-language pairing, `whisper-1` is
  the recommended STT model.

### Settings catalogs (fetched vs hardcoded)

The Settings UI fetches the ElevenLabs model and voice catalogs live through
an admin-only backend proxy (`GET /api/voice/elevenlabs/models` and
`/api/voice/elevenlabs/voices` in `vaf/api/voice_routes.py`): the stored API
key never reaches the browser, responses are cached for 5 minutes per key,
and restricted ElevenLabs keys need the `voices_read`/`models_read` scopes.
When the fetch is unavailable the UI falls back to hardcoded model options
and a plain voice-ID input. OpenAI lists are hardcoded by necessity: OpenAI
has no API that enumerates TTS voices or tags audio models. ElevenLabs STT
models (`scribe_v2`) are also hardcoded because they do not appear in
`GET /v1/models`.

The backend always returns WAV to the web client regardless of the provider, so
the browser playback path is provider-agnostic. With a cloud TTS provider
selected, the Piper language manager in Settings is hidden; the per-message
Read Aloud button is the test path.

---

## Web UI Voice Integration

### Architecture

```
Browser (page.tsx)          WebSocket          Backend (web_server.py)
     │                          │                        │
     ├─ MediaRecorder ──────────┼──────────────────────► │
     │  (WebM/Opus)             │                        │
     ├─ convertToWav() ─────────┼──────────────────────► │
     │  (16-bit PCM WAV)        │   process_audio        │
     │                          │                        ├─► cloud STT provider
     │ ◄────────────────────────┼────────────────────────┤   or Docker STT
     │    stt_result {text}     │                        │
     │                          │                        │
     ├─ Request TTS ────────────┼──────────────────────► │
     │                          │   speak                ├─► cloud TTS provider
     │ ◄────────────────────────┼────────────────────────┤   or Docker TTS
     │  tts_audio (base64 WAV)  │                        │
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

**STT Request** (the `format` field is omitted when the client falls back to
sending the raw WebM/OGG recording):
```json
{
  "type": "process_audio",
  "audio": "<base64-encoded-wav>",
  "format": "wav"
}
```

**STT Responses:**
```json
{ "type": "stt_result", "text": "Transcribed text here" }
{ "type": "stt_error", "error": "..." }
```

**TTS Request:**
```json
{
  "type": "speak",
  "text": "Text to speak"
}
```

**TTS Responses** (audio is always WAV, regardless of the configured voice
provider; `tts_state` drives the loading/playing indicators):
```json
{ "type": "tts_audio", "audio": "<base64-encoded-wav>", "format": "wav" }
{ "type": "tts_state", "status": "loading|playing|stopped" }
```

**Stop playback:**
```json
{ "type": "stop_speech" }
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

### WhatsApp Voice Messages

WhatsApp supports the same bidirectional voice flow as Telegram:

1. **Incoming:** User sends voice message → Node bridge downloads via Baileys → Python transcribes via Whisper STT → Agent processes text
2. **Outgoing:** Agent response → TTS synthesizes → Sent as voice via Node bridge (`sendAudioAsVoice`)
3. **Proactive voice:** The agent can explicitly send voice via `send_whatsapp(voice_lang="de")` or `send_telegram(voice_lang="de")` when the user requests it (e.g. "send as voice via WhatsApp")

Requires `speech_stt_docker_url` (port 5003) and `speech_tts_docker_url` (port 5002). See [CONNECTIONS.md](../integrations/CONNECTIONS.md) for WhatsApp setup.

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
  "speech_tts_provider": "",
  "speech_tts_api_model": "",
  "speech_tts_api_voice": "",

  "speech_stt_enabled": true,
  "speech_stt_engine": "docker",
  "speech_stt_docker_url": "http://localhost:5003",
  "speech_stt_provider": "",
  "speech_stt_api_model": "",

  "speech_language": "de-DE",
  "speech_mic_index": 0
}
```

`speech_language` and `speech_mic_index` are runtime keys written by the CLI
wizard (not part of `Config.DEFAULTS`).

### Configuration Options

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `speech_tts_enabled` | bool | `false` | Enable TTS output |
| `speech_tts_engine` | string | `"docker"` | TTS engine: `docker`, `chatterbox`, `piper`, `system` |
| `speech_tts_docker_url` | string | `http://localhost:5002` | Default Docker TTS URL |
| `speech_tts_docker_url_de` | string | `http://localhost:5002` | German TTS URL (optional) |
| `speech_tts_docker_url_en` | string | `http://localhost:5004` | English TTS URL (optional) |
| `speech_tts_docker_url_fr` | string | `http://localhost:5006` | French TTS URL (optional) |
| `speech_tts_chatterbox_url` | string | `http://localhost:4123` | Chatterbox-style HTTP TTS server |
| `speech_tts_provider` | string | `""` | Cloud TTS provider: `""`, `elevenlabs`, `openai` |
| `speech_tts_api_model` | string | `""` | Cloud TTS model (`""` = provider default) |
| `speech_tts_api_voice` | string | `""` | Cloud TTS voice (`""` = provider default) |
| `speech_stt_enabled` | bool | `false` | Enable STT input (canonical; legacy `stt_enabled` is ORed in) |
| `speech_stt_engine` | string | `"docker"` | STT engine: `docker`, `local` |
| `speech_stt_docker_url` | string | `http://localhost:5003` | Docker STT URL |
| `speech_stt_provider` | string | `""` | Cloud STT provider: `""`, `elevenlabs`, `openai` |
| `speech_stt_api_model` | string | `""` | Cloud STT model (`""` = provider default) |
| `api_key_elevenlabs` | string | `""` | ElevenLabs API key (speech only) |
| `speech_language` | string | `"de-DE"` | Default language (CLI wizard runtime key) |
| `speech_mic_index` | int | `0` | Microphone device index (CLI wizard runtime key) |

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

4. **TTS over LAN (Web UI):** Playback is **proxied** via the backend: the browser sends `speak` over WebSocket and receives audio as base64 (`tts_audio`). The client never calls the TTS URL directly. If TTS only “loads” and no sound plays from a LAN device, the backend may be failing to reach the TTS service. Ensure `speech_tts_docker_url` is reachable from where the backend runs (e.g. if the backend runs inside Docker, use the Docker service name like `http://vaf-tts:5000` instead of `http://localhost:5002`). Check server logs for TTS synthesis errors or timeouts.

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

4. **macOS desktop window:** "Microphone access is not supported by this browser" means the
   host Python.app lost its `NSMicrophoneUsageDescription` (typically after a
   `brew upgrade python@X.Y`) — re-run `bash scripts/macos_mic_plist.sh ./venv/bin/python`.
   The startup log warns about this state.

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
