# Local Speech Integration (TTS & STT)

This document outlines the final architecture for VAF's offline Text-to-Speech (TTS) and Speech-to-Text (STT) capabilities.

## 🎯 Goals
1.  **Strictly Offline:** High-quality neural voices without external APIs or cloud dependencies.
2.  **Cross-Platform:** Works on Windows, Linux, and macOS (Intel & Silicon).
3.  **Human-like Quality:** Using Piper TTS for natural neural speech.
4.  **Separation of Concerns:** TTS only speaks the *answer*, skipping internal thinking processes.
5.  **Continuous Feedback:** Filler words during processing to eliminate dead silence.
6.  **Non-Interference:** TTS automatically stops when STT is initiated.

## 🛠️ Technology Stack

### 1. Text-to-Speech (TTS)
*   **Piper (local):** Neural (VITS) via ONNX binary in `bin/piper/`; high-quality voices (e.g. Thorsten for German).
*   **System:** `pyttsx3` (SAPI5/nsss/espeak) as fallback.
*   **Docker:** HTTP TTS service (e.g. Piper in a container). One voice per container (set via `TTS_MODEL_URL` in `.env`). For multi-language (DE + EN) on demand, use **Piper (local)** instead; VAF then downloads and uses the right voice per detected language.

### 2. Speech-to-Text (STT)
*   **Trigger:** Manual (`L + Enter` in chat) or Web UI voice button.
*   **Engine:** **Docker** (default): HTTP STT service (e.g. whisper-asr-webservice). Set `speech_stt_engine` to `"docker"` and `speech_stt_docker_url` to `http://localhost:5003`. **Local:** faster-whisper + ffmpeg for Web UI; `SpeechRecognition` for CLI.

## 🏗️ Core Implementation (`vaf/core/speech.py`)

VAF uses **SpeechManager** for TTS and STT.

### SpeechManager (TTS & STT)
Handles the lifecycle of speech operations:

#### Automated Setup
On the first run with TTS enabled, VAF automatically:
1.  Detects OS and Architecture (amd64, arm64, etc.).
2.  Downloads the correct **Piper binary** from GitHub.
3.  Downloads high-quality **ONNX voice models** (e.g., `de_DE-thorsten-high.onnx`) into `models/voices/`.

#### Reasoning vs. Content Separation
To ensure the agent doesn't read its "thoughts" aloud, the integration in `vaf/core/agent.py` distinguishes between `reasoning_content` and `content`.
*   **TTS Source:** Uses `full_content` (the "blue text" in TUI).
*   **Filtering:** `_clean_markdown` removes:
    *   Code blocks (`` ``` ``) and complex inline code
    *   Thinking tags (`<think>`, `<redacted_reasoning>`)
    *   VQ-1 specific thought patterns
    *   **Emojis** (all Unicode emoji ranges: emoticons, symbols, flags, etc.)
    *   Markdown formatting (bold, italic, headers, lists)
    *   Long URLs (shortened to domain only)

## ⚙️ Configuration

Managed via `vaf settings` or `vaf.config.json`:

```json
{
  "speech_tts_enabled": true,        // Toggle TTS output
  "speech_tts_engine": "piper",      // "piper" | "system" | "docker"
  "speech_tts_docker_url": "",       // When engine=docker: e.g. http://localhost:5002/synthesize
  "speech_stt_enabled": true,        // STT via /listen or 'L' or Web UI
  "speech_stt_engine": "docker",    // "docker" (default) or "local"
  "speech_stt_docker_url": "http://localhost:5003",  // When engine=docker
  "speech_language": "de-DE",        // Language for STT (e.g., "en-US", "tr-TR")
  "speech_mic_index": 0              // Microphone device index (optional)
}
```

**Docker TTS API:** POST JSON `{"text": "...", "lang": "de"}` to the URL; response body is raw WAV bytes or JSON with `"audio_base64"`.

### Available Commands
*   `l` or `/listen` - Start speech-to-text input (auto-stops TTS)
*   `/halt`, `/stop`, `/quiet`, `/stfu` - Manually stop TTS playback

## 🔄 User Flow

### Speech Output (TTS)
1.  **Filler Word:** Agent speaks context-aware filler (e.g., "Einen kleinen Moment") before processing.
2.  **Processing:** Agent generates response (thinking + tools).
3.  **Answer Extraction:** `chat_step` extracts the pure answer (`full_content`).
4.  **Text Cleaning:** `_clean_markdown()` removes emojis, code, markdown, thinking tags.
5.  **Speech Synthesis:** `SpeechManager.speak()` is called asynchronously (non-blocking).
6.  **Audio Playback:** Piper generates a temporary WAV and plays it via system tools.

### Speech Input (STT)
1.  **Trigger:** User types `l` and presses `Enter` (CLI) or uses the voice button (Web UI).
3.  **TTS Stop:** Any active TTS playback is immediately stopped.
4.  **Recording:** TUI shows `● Recording` with real-time energy bar visualization.
5.  **Silence Detection:** After 1.5 seconds of silence, recording ends.
6.  **Transcription:** Audio is processed and transcribed.
7.  **Auto-Send:** Transcribed text is automatically sent as the next user message.

## 🚀 Troubleshooting

### TTS Issues
*   **Robotic voice:** VAF might be using the `pyttsx3` fallback. Check if `bin/piper/` contains the binary.

## 📁 File Structure

```
vaf/
├── core/
│   ├── speech.py              # SpeechManager (TTS/STT)
│   ├── speech_fillers.py      # Filler phrases configuration
│   └── agent.py               # Integration: _speak(), _speak_filler()
├── cli/
│   ├── cmd/run.py             # Main loop, voice via L+Enter
│   └── tui.py                 # Input box
bin/
└── piper/                     # Piper TTS binary (auto-downloaded)
models/
└── voices/                    # ONNX voice models (auto-downloaded)
```

## 🌍 Supported Languages

### TTS (Piper Neural Voices)
40+ languages including:
*   **Major:** English, German, French, Spanish, Chinese, Italian, Portuguese, Russian
*   **Nordic:** Danish, Finnish, Icelandic, Norwegian, Swedish
*   **Eastern Europe:** Czech, Hungarian, Polish, Romanian, Slovak, Slovenian, Serbian, Ukrainian, Bulgarian
*   **Middle East/Asia:** Arabic, Persian, Turkish, Georgian, Kazakh, Nepali, Vietnamese
*   **Others:** Catalan, Welsh, Greek, Luxembourgish, Dutch, Swahili

### STT (Google Speech API)
100+ languages supported by Google Speech Recognition.

### Filler Words
Currently configured for:
*   German (de)
*   English (en)
*   Turkish (tr)
*   Spanish (es)
*   French (fr)
*   Italian (it)
*   Portuguese (pt)
*   Russian (ru)
*   Chinese (zh)
*   Arabic (ar)

**Note:** Easily extensible by editing `vaf/core/speech_fillers.py`.