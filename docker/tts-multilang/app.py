#!/usr/bin/env python3
"""
VAF TTS All-in-One Multi-Language Server
=========================================
Single Flask server supporting multiple languages via Piper TTS.
Languages/models are downloaded on-demand based on user requests.

Endpoints:
- GET  /health          - Health check
- GET  /languages       - List available languages and installed models
- POST /install         - Install a language model
- POST /uninstall       - Remove a language model
- POST /synthesize      - Generate speech from text
- GET  /config          - Get current configuration
- POST /config          - Update configuration
"""

import os
import json
import subprocess
import hashlib
import threading
from pathlib import Path
from typing import Optional, Dict, List, Any
from flask import Flask, request, jsonify, Response, send_file
from flask_cors import CORS
import io
import wave

app = Flask(__name__)
CORS(app)

# Configuration
MODELS_DIR = Path(os.environ.get("TTS_MODELS_DIR", "/app/models"))
CONFIG_FILE = Path(os.environ.get("TTS_CONFIG_FILE", "/app/config/tts_config.json"))
DEFAULT_LANG = os.environ.get("TTS_DEFAULT_LANG", "de")

# Piper voice models - curated list of high-quality voices
AVAILABLE_MODELS = {
    "de": {
        "name": "German (Deutsch)",
        "voice": "thorsten",
        "quality": "medium",
        "model_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx",
        "config_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx.json"
    },
    "en": {
        "name": "English",
        "voice": "amy",
        "quality": "medium",
        "model_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/medium/en_US-amy-medium.onnx",
        "config_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/medium/en_US-amy-medium.onnx.json"
    },
    "fr": {
        "name": "French (Français)",
        "voice": "siwis",
        "quality": "medium",
        "model_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/fr/fr_FR/siwis/medium/fr_FR-siwis-medium.onnx",
        "config_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/fr/fr_FR/siwis/medium/fr_FR-siwis-medium.onnx.json"
    },
    "es": {
        "name": "Spanish (Español)",
        "voice": "carlfm",
        "quality": "medium",
        "model_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/es/es_ES/carlfm/x_low/es_ES-carlfm-x_low.onnx",
        "config_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/es/es_ES/carlfm/x_low/es_ES-carlfm-x_low.onnx.json"
    },
    "it": {
        "name": "Italian (Italiano)",
        "voice": "riccardo",
        "quality": "medium",
        "model_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/it/it_IT/riccardo/x_low/it_IT-riccardo-x_low.onnx",
        "config_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/it/it_IT/riccardo/x_low/it_IT-riccardo-x_low.onnx.json"
    },
    "nl": {
        "name": "Dutch (Nederlands)",
        "voice": "mls",
        "quality": "medium",
        "model_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/nl/nl_NL/mls/medium/nl_NL-mls-medium.onnx",
        "config_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/nl/nl_NL/mls/medium/nl_NL-mls-medium.onnx.json"
    },
    "pl": {
        "name": "Polish (Polski)",
        "voice": "gosia",
        "quality": "medium",
        "model_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/pl/pl_PL/gosia/medium/pl_PL-gosia-medium.onnx",
        "config_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/pl/pl_PL/gosia/medium/pl_PL-gosia-medium.onnx.json"
    },
    "pt": {
        "name": "Portuguese (Português)",
        "voice": "faber",
        "quality": "medium",
        "model_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx",
        "config_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx.json"
    },
    "ru": {
        "name": "Russian (Русский)",
        "voice": "irina",
        "quality": "medium",
        "model_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx",
        "config_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx.json"
    },
    "uk": {
        "name": "Ukrainian (Українська)",
        "voice": "ukrainian_tts",
        "quality": "medium",
        "model_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/uk/uk_UA/ukrainian_tts/medium/uk_UA-ukrainian_tts-medium.onnx",
        "config_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/uk/uk_UA/ukrainian_tts/medium/uk_UA-ukrainian_tts-medium.onnx.json"
    },
    "zh": {
        "name": "Chinese (中文)",
        "voice": "huayan",
        "quality": "medium",
        "model_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx",
        "config_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx.json"
    },
    "ja": {
        "name": "Japanese (日本語)",
        "voice": "kokoro",
        "quality": "medium",
        "model_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ja/ja_JP/kokoro/medium/ja_JP-kokoro-medium.onnx",
        "config_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ja/ja_JP/kokoro/medium/ja_JP-kokoro-medium.onnx.json"
    },
    "tr": {
        "name": "Turkish (Türkçe)",
        "voice": "dfki",
        "quality": "medium",
        "model_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/tr/tr_TR/dfki/medium/tr_TR-dfki-medium.onnx",
        "config_url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/tr/tr_TR/dfki/medium/tr_TR-dfki-medium.onnx.json"
    }
}

# Track download progress
download_progress: Dict[str, Dict[str, Any]] = {}
download_lock = threading.Lock()


def load_config() -> Dict[str, Any]:
    """Load configuration from file."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "installed_languages": [],
        "language_priority": ["de", "en"],
        "auto_detect": True,
        "default_language": DEFAULT_LANG
    }


def save_config(config: Dict[str, Any]) -> None:
    """Save configuration to file."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_model_path(lang: str) -> Optional[Path]:
    """Get path to model file for a language."""
    if lang not in AVAILABLE_MODELS:
        return None
    model_dir = MODELS_DIR / lang
    model_file = model_dir / f"{lang}.onnx"
    if model_file.exists():
        return model_file
    return None


def is_language_installed(lang: str) -> bool:
    """Check if a language model is installed."""
    model_path = get_model_path(lang)
    if model_path and model_path.exists():
        config_path = model_path.with_suffix(".onnx.json")
        return config_path.exists()
    return False


def download_model(lang: str) -> bool:
    """Download a language model. Returns True on success."""
    if lang not in AVAILABLE_MODELS:
        return False

    model_info = AVAILABLE_MODELS[lang]
    model_dir = MODELS_DIR / lang
    model_dir.mkdir(parents=True, exist_ok=True)

    model_file = model_dir / f"{lang}.onnx"
    config_file = model_dir / f"{lang}.onnx.json"

    try:
        # Update progress
        with download_lock:
            download_progress[lang] = {"status": "downloading", "progress": 0}

        # Download model file
        import requests

        # Download ONNX model
        response = requests.get(model_info["model_url"], stream=True)
        response.raise_for_status()
        total_size = int(response.headers.get('content-length', 0))

        downloaded = 0
        with open(model_file, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    with download_lock:
                        download_progress[lang]["progress"] = int(downloaded / total_size * 90)

        # Download config file
        with download_lock:
            download_progress[lang]["progress"] = 95

        response = requests.get(model_info["config_url"])
        response.raise_for_status()
        with open(config_file, 'wb') as f:
            f.write(response.content)

        # Update config
        config = load_config()
        if lang not in config["installed_languages"]:
            config["installed_languages"].append(lang)
        save_config(config)

        with download_lock:
            download_progress[lang] = {"status": "completed", "progress": 100}

        return True

    except Exception as e:
        with download_lock:
            download_progress[lang] = {"status": "error", "error": str(e)}
        # Cleanup partial files
        if model_file.exists():
            model_file.unlink()
        if config_file.exists():
            config_file.unlink()
        return False


def detect_language(text: str) -> str:
    """Detect the language of the text."""
    try:
        from langdetect import detect
        detected = detect(text)
        # Map langdetect codes to our codes
        lang_map = {
            "de": "de", "en": "en", "fr": "fr", "es": "es",
            "it": "it", "nl": "nl", "pl": "pl", "pt": "pt",
            "ru": "ru", "uk": "uk", "zh-cn": "zh", "zh-tw": "zh",
            "ja": "ja"
        }
        return lang_map.get(detected, DEFAULT_LANG)
    except Exception:
        return DEFAULT_LANG


def synthesize_speech(text: str, lang: str, output_format: str = "wav") -> Optional[bytes]:
    """Synthesize speech using Piper.

    Args:
        text: Text to synthesize
        lang: Language code
        output_format: "wav" or "ogg" (OGG/Opus for Telegram)

    Returns: Audio bytes in requested format
    """
    model_path = get_model_path(lang)
    if not model_path or not model_path.exists():
        return None

    config_path = model_path.with_suffix(".onnx.json")
    if not config_path.exists():
        return None

    try:
        # Use piper-tts Python library
        from piper import PiperVoice

        voice = PiperVoice.load(str(model_path), str(config_path))

        # Synthesize - returns AudioChunk objects
        chunks = list(voice.synthesize(text))
        if not chunks:
            return None

        # Combine all audio chunks
        all_audio = b''.join(chunk.audio_int16_bytes for chunk in chunks)

        # Get audio parameters from first chunk
        first_chunk = chunks[0]

        # Create WAV file
        audio_buffer = io.BytesIO()
        with wave.open(audio_buffer, 'wb') as wav_file:
            wav_file.setnchannels(first_chunk.sample_channels)
            wav_file.setsampwidth(first_chunk.sample_width)
            wav_file.setframerate(first_chunk.sample_rate)
            wav_file.writeframes(all_audio)

        audio_buffer.seek(0)
        wav_data = audio_buffer.read()

        # Convert to OGG/Opus if requested (for Telegram voice messages)
        if output_format.lower() == "ogg":
            return convert_wav_to_ogg(wav_data)

        return wav_data

    except Exception as e:
        print(f"Synthesis error: {e}")
        import traceback
        traceback.print_exc()
        return None


def convert_wav_to_ogg(wav_data: bytes) -> Optional[bytes]:
    """Convert WAV audio to OGG/Opus format using ffmpeg."""
    import tempfile

    try:
        # Write WAV to temp file
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_file:
            wav_file.write(wav_data)
            wav_path = wav_file.name

        ogg_path = wav_path.replace(".wav", ".ogg")

        try:
            # Run ffmpeg to convert
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", wav_path, "-c:a", "libopus", "-b:a", "64k", ogg_path],
                capture_output=True,
                timeout=30
            )

            if result.returncode != 0:
                print(f"ffmpeg conversion failed: {result.stderr.decode()[:200]}")
                return None

            # Read OGG file
            with open(ogg_path, "rb") as f:
                return f.read()

        finally:
            # Cleanup temp files
            import os
            for path in [wav_path, ogg_path]:
                try:
                    if os.path.exists(path):
                        os.unlink(path)
                except Exception:
                    pass

    except Exception as e:
        print(f"WAV to OGG conversion error: {e}")
        return None


# ============================================================================
# API Endpoints
# ============================================================================

@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    config = load_config()
    return jsonify({
        "status": "healthy",
        "installed_languages": config["installed_languages"],
        "available_languages": list(AVAILABLE_MODELS.keys())
    })


@app.route("/languages", methods=["GET"])
def list_languages():
    """List all available languages and their installation status."""
    config = load_config()
    languages = []

    for code, info in AVAILABLE_MODELS.items():
        installed = is_language_installed(code)
        lang_data = {
            "code": code,
            "name": info["name"],
            "voice": info["voice"],
            "quality": info["quality"],
            "installed": installed,
            "priority": config["language_priority"].index(code) if code in config["language_priority"] else 999
        }

        # Add download progress if downloading
        with download_lock:
            if code in download_progress:
                lang_data["download_status"] = download_progress[code]

        languages.append(lang_data)

    # Sort by priority
    languages.sort(key=lambda x: x["priority"])

    return jsonify({
        "languages": languages,
        "config": {
            "language_priority": config["language_priority"],
            "auto_detect": config.get("auto_detect", True),
            "default_language": config.get("default_language", DEFAULT_LANG)
        }
    })


@app.route("/install", methods=["POST"])
def install_language():
    """Install a language model."""
    data = request.get_json() or {}
    lang = data.get("language")

    if not lang:
        return jsonify({"error": "Missing 'language' parameter"}), 400

    if lang not in AVAILABLE_MODELS:
        return jsonify({"error": f"Unknown language: {lang}"}), 400

    if is_language_installed(lang):
        return jsonify({"status": "already_installed", "language": lang})

    # Start download in background thread
    def do_download():
        download_model(lang)

    thread = threading.Thread(target=do_download, daemon=True)
    thread.start()

    return jsonify({
        "status": "downloading",
        "language": lang,
        "message": f"Started downloading {AVAILABLE_MODELS[lang]['name']}"
    })


@app.route("/install/status/<lang>", methods=["GET"])
def install_status(lang: str):
    """Get installation status for a language."""
    with download_lock:
        if lang in download_progress:
            return jsonify(download_progress[lang])

    if is_language_installed(lang):
        return jsonify({"status": "completed", "progress": 100})

    return jsonify({"status": "not_started", "progress": 0})


@app.route("/uninstall", methods=["POST"])
def uninstall_language():
    """Remove a language model."""
    data = request.get_json() or {}
    lang = data.get("language")

    if not lang:
        return jsonify({"error": "Missing 'language' parameter"}), 400

    model_dir = MODELS_DIR / lang
    if model_dir.exists():
        import shutil
        shutil.rmtree(model_dir)

    # Update config
    config = load_config()
    if lang in config["installed_languages"]:
        config["installed_languages"].remove(lang)
    if lang in config["language_priority"]:
        config["language_priority"].remove(lang)
    save_config(config)

    # Clear progress
    with download_lock:
        if lang in download_progress:
            del download_progress[lang]

    return jsonify({"status": "uninstalled", "language": lang})


@app.route("/config", methods=["GET"])
def get_config():
    """Get current TTS configuration."""
    return jsonify(load_config())


@app.route("/config", methods=["POST"])
def update_config():
    """Update TTS configuration."""
    data = request.get_json() or {}
    config = load_config()

    if "language_priority" in data:
        # Validate all languages exist
        priority = data["language_priority"]
        if isinstance(priority, list):
            config["language_priority"] = [l for l in priority if l in AVAILABLE_MODELS]

    if "auto_detect" in data:
        config["auto_detect"] = bool(data["auto_detect"])

    if "default_language" in data:
        if data["default_language"] in AVAILABLE_MODELS:
            config["default_language"] = data["default_language"]

    save_config(config)
    return jsonify(config)


@app.route("/synthesize", methods=["POST"])
def synthesize():
    """Synthesize speech from text.

    Request body (JSON):
        text: str - Text to synthesize
        language: str (optional) - Language code (auto-detected if not provided)
        format: str (optional) - Output format: "wav" (default) or "ogg" (Opus, for Telegram)

    Returns: audio/wav or audio/ogg
    """
    # Handle both JSON and form data
    if request.is_json:
        data = request.get_json() or {}
        text = data.get("text", "")
        lang = data.get("language")
        output_format = data.get("format", "wav")
    else:
        text = request.form.get("text", "") or request.args.get("text", "")
        lang = request.form.get("language") or request.args.get("language")
        output_format = request.form.get("format") or request.args.get("format", "wav")

    if not text:
        return jsonify({"error": "Missing 'text' parameter"}), 400

    # Validate format
    if output_format.lower() not in ("wav", "ogg"):
        output_format = "wav"

    config = load_config()

    # Determine language
    if not lang:
        if config.get("auto_detect", True):
            lang = detect_language(text)
        else:
            lang = config.get("default_language", DEFAULT_LANG)

    # Check if language is installed
    if not is_language_installed(lang):
        # Try fallback to default language
        fallback_lang = config.get("default_language", DEFAULT_LANG)
        if is_language_installed(fallback_lang):
            lang = fallback_lang
        else:
            # Try any installed language
            for installed in config.get("installed_languages", []):
                if is_language_installed(installed):
                    lang = installed
                    break
            else:
                return jsonify({
                    "error": f"Language '{lang}' not installed and no fallback available",
                    "available": config.get("installed_languages", [])
                }), 400

    # Synthesize
    audio_data = synthesize_speech(text, lang, output_format)
    if not audio_data:
        return jsonify({"error": "Synthesis failed"}), 500

    # Set MIME type based on format
    if output_format.lower() == "ogg":
        mimetype = "audio/ogg"
        filename = "speech.ogg"
    else:
        mimetype = "audio/wav"
        filename = "speech.wav"

    return Response(
        audio_data,
        mimetype=mimetype,
        headers={
            "Content-Disposition": f"inline; filename={filename}",
            "X-TTS-Language": lang
        }
    )


@app.route("/", methods=["GET"])
def index():
    """Simple info page."""
    return jsonify({
        "service": "VAF TTS Multi-Language Server",
        "version": "1.0.0",
        "endpoints": {
            "GET /health": "Health check",
            "GET /languages": "List available languages",
            "POST /install": "Install a language model",
            "GET /install/status/<lang>": "Check installation progress",
            "POST /uninstall": "Remove a language model",
            "GET /config": "Get configuration",
            "POST /config": "Update configuration",
            "POST /synthesize": "Generate speech from text"
        }
    })


if __name__ == "__main__":
    # Ensure directories exist
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Auto-install default language if nothing installed
    config = load_config()
    if not config.get("installed_languages"):
        print(f"No languages installed. Installing default: {DEFAULT_LANG}")
        if download_model(DEFAULT_LANG):
            print(f"Successfully installed {DEFAULT_LANG}")
        else:
            print(f"Failed to install {DEFAULT_LANG}")

    port = int(os.environ.get("TTS_PORT", 5000))
    print(f"Starting TTS server on port {port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
