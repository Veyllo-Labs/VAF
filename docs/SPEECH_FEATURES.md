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
*   **Engine:** Currently using the system default (Google Web Speech as standard wrapper). 
*   **Future Upgrade:** Integration of local Whisper (OpenAI) or Vosk for 100% offline STT.

## 🏗️ Core Implementation (`vaf/core/speech.py`)

The `SpeechManager` class handles the lifecycle of speech operations:

### Automated Setup
On the first run with TTS enabled, VAF automatically:
1.  Detects OS and Architecture (amd64, arm64, etc.).
2.  Downloads the correct **Piper binary** from GitHub.
3.  Downloads high-quality **ONNX voice models** (e.g., `de_DE-thorsten-high.onnx`) into `models/voices/`.

### Reasoning vs. Content Separation
To ensure the agent doesn't read its "thoughts" aloud, the integration in `vaf/core/agent.py` distinguishes between `reasoning_content` and `content`.
*   **TTS Source:** Uses `full_content` (the "blue text" in TUI).
*   **Filtering:** `_clean_markdown` removes:
    *   Code blocks (`` ``` ``) and complex inline code
    *   Thinking tags (`<think>`, `<redacted_reasoning>`)
    *   VQ-1 specific thought patterns
    *   **Emojis** (all Unicode emoji ranges: emoticons, symbols, flags, etc.)
    *   Markdown formatting (bold, italic, headers, lists)
    *   Long URLs (shortened to domain only)

### Filler Words (Gap Fillers)
To provide continuous audio feedback during processing delays, VAF speaks context-aware filler phrases:
*   **Thinking Phase:** "Einen kleinen Moment", "Moment bitte", etc.
*   **Tool Execution:** Specific phrases for web search, file operations, code execution, sub-agents
*   **Multi-Language:** Supports 10+ languages (German, English, Turkish, Spanish, French, etc.)
*   **Configuration:** Centralized in `vaf/core/speech_fillers.py`
*   **Implementation:** `Agent._speak_filler()` selects random phrases based on context and language

### Sub-Agent Acknowledgments
When delegating tasks to sub-agents (e.g., `research_agent`, `coding_agent`), the main agent immediately speaks the acknowledgment message:
*   **Example:** "Der research_agent arbeitet jetzt an deiner Anfrage..."
*   **Technical:** Uses `[ASYNC_ACK]` marker with `time.sleep(0.05)` to ensure TTS thread starts before function returns

### STT/TTS Non-Interference
To prevent TTS from interfering with microphone input:
*   **Auto-Stop:** `SpeechManager.stop()` is called automatically when `/listen` or `/l` is triggered
*   **Multiple Layers:** Stop calls in `run.py`, `tui.py`, and `speech.py` ensure complete silence
*   **Manual Stop:** Commands `/halt`, `/stop`, `/quiet`, `/stfu` explicitly stop TTS playback

### STT Success Feedback
After successful speech recognition:
*   **Sound Effect:** Plays `sounds/sst.mp3` using OS-specific audio players
*   **Non-Blocking:** Runs in background thread to avoid delays
*   **OS-Independent:** PowerShell (Windows), `afplay` (macOS), `mpg123`/`mpv`/`ffplay` (Linux)

## ⚙️ Configuration

Managed via `vaf settings` or `vaf.config.json`:

```json
{
  "speech_tts_enabled": true,     // Toggle TTS output
  "speech_stt_enabled": true,     // Toggle STT input via /listen
  "speech_language": "de-DE",     // Language for STT (e.g., "en-US", "tr-TR")
  "speech_mic_index": 0           // Microphone device index (optional)
}
```

### Available Commands
*   `/listen` or `/l` - Start speech-to-text input (auto-stops TTS)
*   `/halt`, `/stop`, `/quiet`, `/stfu` - Manually stop TTS playback

## 🔄 User Flow

### Speech Output (TTS)
1.  **Filler Word:** Agent speaks context-aware filler (e.g., "Einen kleinen Moment") before processing.
2.  **Processing:** Agent generates response (thinking + tools).
3.  **Answer Extraction:** `chat_step` extracts the pure answer (`full_content`).
4.  **Text Cleaning:** `_clean_markdown()` removes emojis, code, markdown, thinking tags.
5.  **Speech Synthesis:** `SpeechManager.speak()` is called asynchronously (non-blocking).
6.  **Audio Playback:** Piper generates a temporary WAV and plays it via system tools (`powershell` on Win, `afplay` on Mac, `aplay` on Linux).

### Speech Input (STT)
1.  **User Trigger:** User types `/listen` or `/l` in the chat.
2.  **TTS Stop:** Any active TTS playback is immediately stopped.
3.  **Calibration:** System calibrates ambient noise level (1 second).
4.  **Recording:** TUI shows `● Recording` with real-time energy bar visualization.
5.  **Silence Detection:** After 1.5 seconds of silence following speech, recording ends.
6.  **Transcription:** Audio is sent to Google Speech API for recognition.
7.  **Success Feedback:** `sounds/sst.mp3` plays to confirm successful capture.
8.  **Auto-Send:** Transcribed text is automatically sent as the next user message.

## 🚀 Troubleshooting

### TTS Issues
*   **No sound:** Check system volume and ensure `pyaudio` is installed correctly.
*   **Robotic voice:** VAF might be using the `pyttsx3` fallback. Check if `bin/piper/` contains the binary and `models/voices/` has the `.onnx` files.
*   **Still speaking after exit:** VAF kills the background `powershell` or playback process during `Agent.shutdown()`. Ensure you use `/exit` or `Ctrl+C` for clean termination.
*   **Emojis being spoken:** This should be fixed. If you still hear emojis, check that `speech.py` has the emoji removal pattern (lines 655-675).
*   **Code being spoken:** Short inline code (< 20 chars) is intentionally spoken. Long/complex code is removed.

### STT Issues
*   **Microphone not detected:** Run `vaf settings` and select the correct microphone index.
*   **No speech detected:** Ensure ambient noise calibration completes (1 second). Speak louder or move closer to the microphone.
*   **TTS interfering with STT:** This should be fixed. TTS auto-stops when `/listen` is triggered. If issues persist, manually use `/halt` before `/listen`.
*   **Success sound not playing:** Ensure `sounds/sst.mp3` exists. Check that your OS has the required audio player (PowerShell on Windows, `afplay` on macOS, `mpg123`/`mpv`/`ffplay` on Linux).

### Filler Words
*   **Too frequent:** Filler words are spoken once per thinking phase and once per tool execution. This is intentional for continuous feedback.
*   **Wrong language:** Filler language is determined by the agent's response language. If incorrect, check `speech_language` in config.
*   **Customize phrases:** Edit `vaf/core/speech_fillers.py` to add/modify filler phrases for any language.

## 📁 File Structure

```
vaf/
├── core/
│   ├── speech.py              # Main SpeechManager class
│   ├── speech_fillers.py      # Filler phrases configuration (10+ languages)
│   └── agent.py               # Integration: _speak(), _speak_filler()
├── cli/
│   ├── cmd/run.py             # /listen command, TTS stop commands
│   └── tui.py                 # STT overlay with auto-stop
bin/
└── piper/                     # Piper TTS binary (auto-downloaded)
models/
└── voices/                    # ONNX voice models (auto-downloaded)
sounds/
└── sst.mp3                    # STT success sound
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