# Speech Integration (TTS & STT)

This document provides comprehensive documentation for VAF's Text-to-Speech (TTS) and Speech-to-Text (STT) capabilities across all interfaces: CLI, Web UI, and Telegram.

## Overview

VAF's speech processing is local by default: Docker containers provide high-quality neural TTS (Piper) and Whisper-based STT. Optionally, an admin can select a cloud voice provider per direction (TTS: ElevenLabs or OpenAI; STT: Veyllo, ElevenLabs, or OpenAI); the local lane remains the fallback. The system supports multiple languages and interfaces.

### Key Features

- **Local by Default**: All processing runs locally via Docker unless an admin selects a cloud voice provider
- **Optional Cloud Providers**: ElevenLabs and OpenAI for TTS and STT, plus Veyllo for STT (`veyllo-transcribe`), selectable independently (`speech_tts_provider` / `speech_stt_provider`), with automatic fallback to the local lane on any API error
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
| `system` | macOS `say` command only (pyttsx3 removed - caused 1-4 GB RAM explosion on Windows via SAPI/comtypes) | macOS fallback only |

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
- `language` (optional): ISO 639-1 code (`de`, `en`, `fr`). Default: `de`. The multi-language
  container **selects the voice from this code**, so callers control the spoken voice by the
  language they send. A live call voices a model reply in ITS OWN detected language when the
  lane can speak it (`web_server._tts_lang_for` + `SpeechManager.call_lane_speaks`; see
  [VOICE_AGENT.md](../agents/VOICE_AGENT.md) step 7), else it stays on the call language.
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

When `speech_stt_provider` is set (`veyllo`, `elevenlabs`, or `openai`), the cloud
lane takes precedence over `speech_stt_engine`, with automatic fallback to the local
lane on any API error. See [Cloud provider lane](#cloud-provider-lane-elevenlabs--openai).

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
| `speech_stt_provider` | `""`, `veyllo`, `elevenlabs`, `openai` | Cloud STT provider; `""` = local engine |
| `speech_stt_api_model` | model ID | `""` = default (`veyllo-transcribe` / `scribe_v2` / `whisper-1`) |
| `api_key_elevenlabs` | key | ElevenLabs API key (read-redacted for non-admins) |

The OpenAI lane reuses the existing `api_key_openai`; the Veyllo STT lane reuses
`api_key_veyllo` and `veyllo_base_url` (the same key and endpoint as the Veyllo
chat/vision provider). ElevenLabs is an audio-only vendor and is deliberately
NOT part of the LLM provider catalog (see
[PROVIDER_MODES.md](../llm/PROVIDER_MODES.md)); Veyllo IS a chat provider, but its
`veyllo-transcribe` audio model is filtered out of chat-model dropdowns
(`provider_registry.is_veyllo_chat_model`). Unlike the other lanes, `speech_stt_provider`
is seeded to `veyllo` the first time a Veyllo key is added, whether at onboarding, in
Settings, or via the CLI provider menu, as long as no STT provider was chosen yet.
`Config.apply_veyllo_stt_default` runs centrally inside `Config.save`, so every
config-write path is covered and none can consume the absent-to-present key
transition without seeding. "No STT provider chosen" means an empty
`speech_stt_provider` AND a default `speech_stt_engine`: an explicit `local_whisper`
pick (`speech_stt_engine='local'`, non-default) blocks the seed so a deliberate local
opt-out is never flipped to the metered cloud (`local_docker`/unset both leave the
default engine, so that pristine-default case is seeded). Runtime selection stays
explicit opt-in: the seed just writes that explicit value, and any later choice
(local, OpenAI, ElevenLabs) overwrites it.

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
- **Veyllo STT**: `POST {veyllo_base_url}/audio/transcriptions` (default
  `https://api.veyllo.app/v1`, which already includes the `/v1` suffix) with a
  Bearer `api_key_veyllo`, multipart field `file`, `model=veyllo-transcribe`,
  `response_format=verbose_json`, and a `language` field: a pinned ISO-639-1 code
  when a hint is known (see the per-speaker hint above) or `multi` otherwise
  (automatic code-switching across the supported languages). Unlike OpenAI whisper
  (which returns an English language NAME), Veyllo returns an ISO-639-1 language code
  directly, so the reply-in-same-language pairing works without a name-to-code table.
  Batch only; live-mic streaming stays on the local lane.

### Per-speaker language hint (precise call, zero overhead)

Passing a known language to a cloud STT gives a more precise, cheaper call than
auto-detect. Rather than run a local model to pre-detect the language (which would
add a heavy optional dependency and duplicate compute for something the cloud does
for free), `speech_client.transcribe` caches the language the provider ALREADY
returns and hints it on the next turn (`speech_api.transcribe(..., language=...)`,
threaded into each provider request: Veyllo/OpenAI `language`, ElevenLabs
`language_code`). The cache is keyed on a caller-supplied `cache_key` - the web mic
passes the speaker's user scope, so it is user-isolated and language is treated as a
trait of the speaker, not global state. Providers like Veyllo/Deepgram treat
`language` as a hard selection, so to catch a mid-conversation language switch the
client sends hint-free (re-detects) every few turns and always refreshes the cache
from the actually-detected language, bounding staleness. The Docker lane always
auto-detects. The web voice CALL passes both the speaker's scope as `cache_key` and the
user's PROFILE language (identity `preferred_language`) as a `default_language` seed, so
the very first (often short) clip is pinned to the known language instead of auto-detecting
a wrong one (a brief German clip misheard as French); the cache + periodic re-detect then
still catch a genuine mid-call switch. The seed fills only the cold-cache first turn - it
never overrides a detected language or the re-detect. Messaging voice notes
(Telegram/WhatsApp) currently transcribe without a cache_key (auto-detect); wiring a
per-sender key there is a follow-up.

The hint is **language-agnostic** - it is whatever the provider itself detected on a
prior turn, so it works across every language a provider supports, not a hardcoded
subset. Codes are normalized to ISO-639-1 (`_norm_iso_lang`: 2-letter passthrough,
locale like `zh-TW`/`de-CH` to its base, ISO-639-3 like `spa` mapped to `es` via
`_ISO639_3_TO_1`, unknown to None so a bad code is never cached or re-sent). When
there is no specific hint, the **Veyllo** lane auto-detects with `language=multi`
(Deepgram automatic code-switching across all its supported languages), so a
multilingual or unknown-language utterance is handled robustly; a confidently
detected single language then pins the following turns. `multi` is never cached as if
it were a language, and it is dropped for OpenAI/ElevenLabs (which have no `multi`).

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

## Speaker identification and the confirmation flow

Voice utterances (Web UI mic and live calls) can be labeled against the owner's
enrolled voice profile (`vaf/core/speaker_id.py`, sherpa-onnx, local CPU lane):
the LLM sees transcripts prefixed `[<owner>]: `, `[<named person>]: `,
`[anderer_Sprecher]: ` or `[unsicher]: `. Enrollment is an explicit guided live
call (Settings > Voice); without a stored profile the feature is inert.

Hard rules:

- Identity comes from the VOICE, never from spoken claims; on live calls only a
  verified owner turn may trigger delegation (code guard, not just prompt). The same
  label also gates guest privacy: a non-owner (`speaker_ok=False`) reply is built
  without the owner's private context. See the voice reflex system in
  [../agents/VOICE_REFLEX.md](../agents/VOICE_REFLEX.md).
- The owner profile is written only by explicit re-enrollment or by
  OWNER-APPROVED adaptive learning (`speaker_id_adaptive_enabled`, default
  on): a YES answer to the confirmation question over an authenticated
  channel feeds that segment into the profile (`add_owner_sample`:
  similarity floor, 10-sample FIFO cap, the enrollment centroid keeps 70%
  weight, re-enrollment resets the adaptive state). The VOICE itself can
  never trigger a profile write - authorization always comes from the
  authenticated owner, not from audio.

The per-turn label is stabilized by IN-CALL HYSTERESIS (`speaker_id.resolve_label`,
per-call `last_self_ts`): once the owner is confidently verified in a call, a
following borderline/short/missing score keeps them the owner for `STICKY_WINDOW_S`,
so their own quick replies do not flicker to `[anderer_Sprecher]` (a clip below
~1.5s scores too noisily to trust a downgrade). A CLEAR stranger - a reliable-length
`other` well below the band, or a named match - flips immediately and ends the sticky.
This never lowers the impostor bar for a clearly different voice; it is an
owner-approved usability trade-off for the borderline band right after the owner spoke.

Confirmation flow (`vaf/core/speaker_confirm.py`, gated by
`speaker_id_confirmation_enabled`) has TWO trigger paths, both scoped to a
non-owner turn (`label != self`) and both throttled to max one pending question
per user with a 1 h expiry:

- **Claim (spoofing check).** When a non-owner voice (`unsure`, `other` or a named
  third party) whose transcript CLAIMS to be the owner - `claims_to_be_owner`
  matches the `owner_claim` vocab templates ("ich bin NAME", "this is NAME", ...
  with the owner's name substituted, multilingual) - the owner is asked promptly
  (short cooldown, `speaker_confirm_claim` question) so a stranger using the owner's
  name cannot pass silently. It authorizes nothing; the voice-verified label still
  gates all action.
- **Unsure (adaptive reclaim).** A borderline OWN voice with no such claim is asked
  with the plain `speaker_confirm_question`, but restrained (long cooldown) so it does
  not ask on every unsure turn - this exists mainly for the adaptive-training reclaim.

A plain non-owner utterance with no claim and no "unsure" score is left alone (no
question). Delivery: main messenger first (question text + the audio segment as
attachment via `send_to_main_messenger`), else a web-chat card (audio player +
buttons; events `speaker_confirm_pending` / `speaker_confirm_reply` /
`speaker_confirm_result`, see WEBUI_WEBSOCKET_FLOW.md). Answers:

- yes: the segment was the owner; nothing is stored (profile untouched).
- no: another speaker; nothing is stored.
- "no, that's Peter": the segment embedding is stored as (or merged into) the
  NAMED third-party profile "Peter" in the per-user voice DB
  (`~/.vaf/speaker_profiles/<scope>/others/`). Future utterances by that voice
  are labeled `[Peter]: `. Named speakers are known but never authorized:
  delegation still requires the owner.

Messenger replies are consumed deterministically before any agent turn
(headless runner) - only for the owner's authenticated scope, never for
contact-relay messages; anything that does not parse as yes/no flows into the
normal chat turn. Segment audio is stored under the user's own
`VAF_Projects/<uid8>/voice_confirm/` (served by `/api/file` with ownership
enforcement) and deleted on resolve/expiry.

Recognition test (Settings > Voice, `web/components/SpeakerTest.tsx`): record
a few seconds, `POST /api/speaker/test` scores it against the user's voice DB
(read-only) and shows the detected speaker with score, threshold and band
markers; admins get a live threshold slider. The correct/wrong verdict goes to
`POST /api/speaker/feedback` into a per-user calibration store
(`speaker_id.record_test_feedback` / `feedback_stats`, capped at 100 entries)
from which a threshold suggestion is derived (midpoint of owner vs non-owner
averages, needs 2+ samples per side, clamped 0.35-0.75, and floored at
owner_mean - 0.15: the midpoint only separates SAMPLED impostors, and the
threshold gates delegation authority - the suggestion must never drift far
below the owner's own score range just because only very unlike voices were
tested). Owner-confirmed test
clips ("correct" on a self label, or the "it was me" false-reject path) also
train the owner profile adaptively via `add_owner_sample` (same guardrails
and kill switch as the confirmation flow); other calibration data never
modifies any voice profile.

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
   `brew upgrade python@X.Y`) - re-run `bash scripts/macos_mic_plist.sh ./venv/bin/python`.
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
