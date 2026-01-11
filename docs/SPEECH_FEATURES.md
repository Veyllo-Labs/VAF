# Local Speech Integration (TTS & STT)

This document outlines the final architecture for VAF's offline Text-to-Speech (TTS) and Speech-to-Text (STT) capabilities.

## 🎯 Goals
1.  **Strictly Offline:** High-quality neural voices without external APIs or cloud dependencies.
2.  **Cross-Platform:** Works on Windows, Linux, and macOS (Intel & Silicon).
3.  **Human-like Quality:** Using Piper TTS for natural neural speech.
4.  **Separation of Concerns:** TTS only speaks the *answer*, skipping internal thinking processes.

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
*   **Filtering:** `_clean_markdown` removes code blocks, thinking tags, and VQ-1 specific thought patterns.

## ⚙️ Configuration

Managed via `vaf settings` or `vaf.config.json`:

```json
{
  "speech_tts_enabled": true,  // Toggle output
  "speech_stt_enabled": true   // Toggle input via /listen
}
```

## 🔄 User Flow

### Speech Output (TTS)
1.  Agent completes response generation.
2.  `chat_step` extracts the pure answer (`full_content`).
3.  `SpeechManager.speak()` is called asynchronously (non-blocking).
4.  Piper generates a temporary WAV and plays it via system tools (`powershell` on Win, `afplay` on Mac, `aplay` on Linux).

### Speech Input (STT)
1.  User types `/listen` in the chat.
2.  TUI shows a recording overlay (`● Recording`).
3.  User speaks; silence detection ends the capture.
4.  Text is transcribed and automatically "sent" as the next user message.

## 🚀 Troubleshooting
*   **No sound:** Check system volume and ensure `pyaudio` is installed correctly.
*   **Robotic voice:** VAF might be using the `pyttsx3` fallback. Check if `bin/piper/` contains the binary and `models/voices/` has the `.onnx` files.
*   **Still speaking after exit:** VAF kills the background `powershell` or playback process during `Agent.shutdown()`. Ensure you use `/exit` or `Ctrl+C` for clean termination.