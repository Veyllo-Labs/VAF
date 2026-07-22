#!/bin/bash

# VAF Mac Setup Script (Robust)
# Installs system dependencies for Audio/TTS on macOS (Apple Silicon compatible)
# Handles potential ONNXRuntime failures gracefully.

echo "🍎 VAF Mac Setup (Robust)..."

# 1. System Dependencies
if ! command -v brew &> /dev/null; then
    echo "❌ Homebrew not found. Please install Homebrew first!"
    exit 1
fi

echo "📦 Installing system libraries..."
brew install portaudio git ffmpeg || echo "⚠️  Brew finished with warnings."

# 2. Virtual Environment
if [ ! -d "venv" ]; then
    echo "🐍 Creating virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate
echo "ℹ️ Python Version: $(python3 --version)"

# 3. Critical Dependencies First (Ensure Agent runs even if AI fails)
if [ "$VAF_SKIP_PIP_INSTALL" = "1" ]; then
    echo "ℹ️ Skipping dependency installation (managed by the outer installer)..."
else
    echo "⬇️ Installing Core Audio Components..."
    export LDFLAGS="-L$(brew --prefix portaudio)/lib"
    export CFLAGS="-I$(brew --prefix portaudio)/include"

    pip install --upgrade pip
    # Explicitly install core libs so they are guaranteed present
    # pyttsx3 removed — caused RAM explosion on Windows. TTS is via Docker (Piper).
    pip install SpeechRecognition pyaudio requests beautifulsoup4 rich typer prompt_toolkit

    # Install the project itself in editable mode (Creates 'vaf' command)
    pip install -e .

    # 4. Attempt Full Installation
    echo "⬇️ Installing remaining requirements..."
    # Try to install onnxruntime specifically for Mac if needed
    # (Sometimes just 'onnxruntime' fails on new Pythons/M1 without specific pip version)
    pip install "onnxruntime>=1.16.0" 2>/dev/null || echo "⚠️  Standard ONNX Runtime install failed. Skipping for now (Wake Word might not work)."

    # Install requirements but ignore errors to ensure setup finishes
    pip install -r requirements.txt || echo "⚠️  Some optional requirements failed to install (likely onnxruntime). Core functionality should still work."
fi

# 5. Verification
echo "🔍 Verifying Installation..."

# TTS: pyttsx3 removed — TTS is now via Docker (Piper). No local install check needed.
# See docs/web-ui/SPEECH_FEATURES.md and docker-compose.memory.yml (vaf-tts service).

# Audio Check
if python3 -c "import pyaudio" &> /dev/null; then
    echo "✅ Audio Engine (PyAudio) installed."
else
    echo "❌ Audio Engine failed to install."
fi

# 6. Global Shortcut (Alias)
echo "🔗 Creating 'vaf' shortcut..."
SHELL_CONFIG="$HOME/.zshrc"
# Get absolute path to run_vaf.sh
RUN_SCRIPT="$(pwd)/run_vaf.sh"

# Check if alias exists and replace it, or append if missing
if grep -q "alias vaf=" "$SHELL_CONFIG"; then
    # Use sed to replace the existing alias line
    # (Using a different separator | to avoid clash with slashes in path)
    sed -i '' "s|alias vaf=.*|alias vaf='$RUN_SCRIPT'|" "$SHELL_CONFIG"
    echo "✅ Shortcut updated in $SHELL_CONFIG"
else
    echo "" >> "$SHELL_CONFIG"
    echo "# VAF Shortcut" >> "$SHELL_CONFIG"
    echo "alias vaf='$RUN_SCRIPT'" >> "$SHELL_CONFIG"
    echo "✅ Shortcut added to $SHELL_CONFIG"
fi

# 7. Create Application Bundle
echo "📱 Creating Application Bundle..."
python3 scripts/create_app_shortcut.py

# 8. Microphone for WebUI voice input in the desktop window: WKWebView only exposes
# navigator.mediaDevices when the host Python.app declares NSMicrophoneUsageDescription.
# Idempotent; a brew upgrade of python@X.Y reverts it (re-run this setup then).
echo "Enabling microphone for the desktop window (WKWebView)..."
bash scripts/macos_mic_plist.sh ./venv/bin/python || true

echo "🎉 Setup Finished!"
echo "👉 Please RESTART your terminal (or run 'source ~/.zshrc')."
echo "👉 Then just type:  vaf"
