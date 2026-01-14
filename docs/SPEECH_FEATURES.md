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
*   **Primary Engine:** **Piper TTS**
    *   **Architecture:** Neural (VITS) based, running locally via ONNX.
    *   **Quality:** High-fidelity human-like voices (e.g., "Thorsten" for German).
    *   **Distribution:** Managed as an OS-independent binary in `bin/piper/`.
*   **Fallback Engine:** `pyttsx3` (SAPI5/nsss/espeak)
    *   Used as a robotic fallback if Piper is unavailable or setup is pending.

### 2. Speech-to-Text (STT)
*   **Library:** `SpeechRecognition` + `pyaudio`.
*   **Trigger:** Manual (`L + Enter` in chat) or Automatic (Wake Word).
*   **Engine:** Currently using the system default (Google Web Speech as standard wrapper). 
*   **Future Upgrade:** Integration of local Whisper (OpenAI) or Vosk for 100% offline STT.

### 3. Wake Word Detection
*   **Primary Engine:** **openWakeWord**
    *   **Architecture:** 100% Local, neural network based (ONNX).
    *   **Features:** Extremely low resource usage, runs in a background thread.
    *   **Privacy:** No audio is sent to the cloud for detection.
    *   **Available Models:** `hey_jarvis` (default), `alexa`, `hey_mycroft`, `hey_rhasspy`.

## 🏗️ Core Implementation (`vaf/core/speech.py`)

VAF uses two distinct managers for audio interaction:

### 1. SpeechManager (TTS & STT)
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

### 2. WakeWordManager (Background Detection)
Runs a continuous background loop to listen for specific keywords.

#### Trigger Mechanism
1.  **Detection:** When the wake word (e.g., "Hey Jarvis") is recognized, the manager sets a thread-safe `Event` flag.
2.  **UI Interruption:** The `input_box` in `tui.py` monitors this flag. If set, it **immediately exits** the input prompt.
3.  **Auto-Start:** `run.py` detects the exit trigger and instantly launches the `listen_overlay()` (STT window).
4.  **Hands-Free:** This allows the user to trigger a voice command from across the room without touching the keyboard.

## ⚙️ Configuration

Managed via `vaf settings` or `vaf.config.json`:

```json
{
  "speech_tts_enabled": true,      // Toggle TTS output
  "speech_stt_enabled": true,      // Toggle STT input via /listen or 'L'
  "stt_wake_word_enabled": true,   // Enable background wake word detection
  "stt_wake_word": "hey_jarvis",   // Choice: hey_jarvis, alexa, hey_rhasspy, etc.
  "speech_language": "de-DE",      // Language for STT (e.g., "en-US", "tr-TR")
  "speech_mic_index": 0            // Microphone device index (optional)
}
```

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
1.  **Trigger:**
    *   **Manual:** User types `l` and presses `Enter`.
    *   **Automatic:** User says "Hey Jarvis".
2.  **Auto-Exit:** If `input_box` was active, it closes immediately.
3.  **TTS Stop:** Any active TTS playback is immediately stopped.
4.  **Recording:** TUI shows `● Recording` with real-time energy bar visualization.
5.  **Silence Detection:** After 1.5 seconds of silence, recording ends.
6.  **Transcription:** Audio is processed and transcribed.
7.  **Auto-Send:** Transcribed text is automatically sent as the next user message.

## 🚀 Troubleshooting

### Wake Word Issues
*   **Wake Word not recognized:** Check if `openwakeword` and `pyaudio` are installed. Speak clearly. Ensure the correct microphone is selected in settings.
*   **"File doesn't exist" Error:** VAF automatically attempts to download missing models. Ensure you have an internet connection during the first run.
*   **UI doesn't react:** Ensure `stt_wake_word_enabled` is set to `true` in settings.

### TTS Issues
*   **Robotic voice:** VAF might be using the `pyttsx3` fallback. Check if `bin/piper/` contains the binary.

## 📁 File Structure

```
vaf/
├── core/
│   ├── speech.py              # SpeechManager & WakeWordManager classes
│   ├── speech_fillers.py      # Filler phrases configuration
│   └── agent.py               # Integration: _speak(), _speak_filler()
├── cli/
│   ├── cmd/run.py             # Main loop with wake word flag checking
│   └── tui.py                 # Input box with wake word monitoring
bin/
└── piper/                     # Piper TTS binary (auto-downloaded)
models/
└── voices/                    # ONNX voice models (auto-downloaded)
```

**Note:** Wake Word models (`.onnx`) are stored within the `openwakeword` package or auto-downloaded to cache. They are **not** committed to the VAF repository.

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