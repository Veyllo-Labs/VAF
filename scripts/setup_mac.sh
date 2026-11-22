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

# 2. Python and Virtual Environment
# Supported range, the CI-tested one; tests/test_installer_python_gate.py keeps it equal to
# install.sh. A bare python3 is Homebrew's newest (3.14 and up), which the dependencies do
# not ship wheels for yet, so the newest supported versioned interpreter is preferred.
MIN_PYTHON_VERSION="3.10"
MAX_PYTHON_VERSION="3.13"

# Exit 0 when interpreter "$1" runs and its major.minor lies within the supported range.
# The interpreter compares its own version_info, so no version text is parsed here.
python_in_range() {
    "$1" -c "import sys; sys.exit(0 if (${MIN_PYTHON_VERSION/./,}) <= sys.version_info[:2] <= (${MAX_PYTHON_VERSION/./,}) else 1)" 2>/dev/null
}

PY_MAJOR=${MAX_PYTHON_VERSION%%.*}
PY_MINOR=${MAX_PYTHON_VERSION#*.}
PY_CANDIDATES=""
while [ "$PY_MINOR" -ge "${MIN_PYTHON_VERSION#*.}" ]; do
    PY_CANDIDATES="$PY_CANDIDATES python$PY_MAJOR.$PY_MINOR"
    PY_MINOR=$((PY_MINOR - 1))
done
PYTHON_CMD=""
for cmd in $PY_CANDIDATES python3; do
    if command -v "$cmd" &> /dev/null && python_in_range "$cmd"; then
        PYTHON_CMD="$cmd"
        break
    fi
done
if [ -z "$PYTHON_CMD" ]; then
    echo "No supported Python found. VAF supports Python $MIN_PYTHON_VERSION to $MAX_PYTHON_VERSION."
    echo "Install Python $MAX_PYTHON_VERSION with:  brew install python@$MAX_PYTHON_VERSION"
    echo "Then run this script again."
    exit 1
fi
echo "Using $("$PYTHON_CMD" --version 2>&1) ($(command -v "$PYTHON_CMD"))"

# A venv built with an unsupported Python (for example 3.14 from an earlier run) reproduces
# the failed wheel builds. It holds only the packages this script installs, so it is rebuilt;
# with the dependency install skipped, a rebuilt venv would stay empty, so stop instead.
if [ -x "venv/bin/python" ] && ! python_in_range "venv/bin/python"; then
    if [ "$VAF_SKIP_PIP_INSTALL" = "1" ]; then
        echo "The existing venv uses $(venv/bin/python --version 2>&1), outside $MIN_PYTHON_VERSION to $MAX_PYTHON_VERSION."
        echo "Remove the venv folder and run the installer again."
        exit 1
    fi
    echo "The existing venv uses $(venv/bin/python --version 2>&1), outside $MIN_PYTHON_VERSION to $MAX_PYTHON_VERSION - recreating it..."
    rm -rf venv
fi

if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    "$PYTHON_CMD" -m venv venv
fi

source venv/bin/activate
echo "Python Version: $(python3 --version)"

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
