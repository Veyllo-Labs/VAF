#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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
import re
import stat
import subprocess
import hashlib
import threading
from pathlib import Path
from typing import Optional, Dict, List, Any
from flask import Flask, request, jsonify, Response, send_file
from flask_cors import CORS
import io
import shutil
import wave
from urllib.parse import quote

from verified_download import DownloadIntegrityError, download_verified, has_pinned_digest

app = Flask(__name__)
CORS(app)

# Configuration
MODELS_DIR = Path(os.environ.get("TTS_MODELS_DIR", "/app/models"))
CONFIG_FILE = Path(os.environ.get("TTS_CONFIG_FILE", "/app/config/tts_config.json"))
DEFAULT_LANG = os.environ.get("TTS_DEFAULT_LANG", "de")

# The voices this container may install, each pinned by SHA-256 at one commit of
# rhasspy/piper-voices: models.json, written by scripts/pin_speech_assets.py in the VAF
# repository from the same commit as the voices of the host. A voice is downloaded only
# as the bytes recorded there (verified_download.py, a byte-identical copy of VAF's
# vaf/core/verified_download.py: the build context holds no vaf package).
# TTS_MODELS_TABLE points at another table: a seam for tests, the image uses the default.
MODELS_TABLE = Path(os.environ.get("TTS_MODELS_TABLE", Path(__file__).with_name("models.json")))


def load_models_table(path: Path = MODELS_TABLE) -> Dict[str, Dict[str, Any]]:
    """{lang: {name, voice, quality, licence, model_url, model_sha256, model_size,
    config_url, config_sha256, config_size}} from the pinned table."""
    table = json.loads(Path(path).read_bytes().decode("utf-8"))
    source, revision = table["source"].rstrip("/"), table["revision"]
    out = {}
    for lang, row in table["models"].items():
        entry = {"name": row["name"], "voice": row["voice"], "quality": row["quality"],
                 "licence": row.get("licence", "")}
        for part in ("model", "config"):
            entry[f"{part}_url"] = f"{source}/resolve/{revision}/{quote(row[part]['path'])}"
            entry[f"{part}_sha256"] = row[part]["sha256"]
            entry[f"{part}_size"] = int(row[part]["size"])
        out[lang] = entry
    return out


def load_retired_languages(path: Path = MODELS_TABLE) -> List[str]:
    """The languages an earlier table held and this one drops ("retired"): their voices are
    removed at start. A code that is not two or three lowercase letters, or that is still a
    language of the table, is ignored with a log line, so the list can never name a folder
    outside MODELS_DIR or an offered voice."""
    table = json.loads(Path(path).read_bytes().decode("utf-8"))
    out = []
    for code in table.get("retired") or []:
        if not isinstance(code, str) or not re.fullmatch(r"[a-z]{2,3}", code) or code in table["models"]:
            print(f"[TTS] Ignoring the retired language {code!r}: not a language code, or still offered",
                  flush=True)
            continue
        out.append(code)
    return out


AVAILABLE_MODELS = load_models_table()
RETIRED_LANGUAGES = load_retired_languages()

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
    """The installed model of `lang`, or None unless the installed pair is the voice the
    table pins: the model a regular file of the pinned size, the settings a regular file
    with the pinned SHA-256 (hashed once per process). A voice of another build or another
    speaker (an earlier VAF fetched other voices under the same language) is therefore not
    installed and never synthesized. Named boundary: the 63 to 114 MB model is identified
    by its size and its settings, not hashed again at every start."""
    if lang not in AVAILABLE_MODELS:
        return None
    info = AVAILABLE_MODELS[lang]
    model_file = MODELS_DIR / lang / f"{lang}.onnx"
    try:
        st = os.lstat(model_file)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size != info["model_size"]:
        return None
    if not has_pinned_digest(MODELS_DIR / lang / f"{lang}.onnx.json", info["config_sha256"], info["config_size"]):
        return None
    return model_file


def is_language_installed(lang: str) -> bool:
    """Whether the pinned voice of `lang` is installed (get_model_path)."""
    return get_model_path(lang) is not None


def download_error_message(name: str, error: Exception) -> str:
    """One short line for the person, by cause; the raw error goes to the log."""
    import requests

    if isinstance(error, DownloadIntegrityError):
        return f"The {name} voice did not match its expected checksum, so it was not installed. Try again later."
    if isinstance(error, (requests.ConnectionError, requests.Timeout)):
        return f"The {name} voice could not be downloaded: the download server could not be reached."
    if isinstance(error, requests.HTTPError):
        status = getattr(error.response, "status_code", None)
        return f"The {name} voice could not be downloaded: the download server answered with an error ({status})."
    return f"The {name} voice could not be installed. Try again later."


class SwapRestoreError(RuntimeError):
    """The new voice could not be put in place, and putting the installed one back failed
    too: the installed voice is kept aside in .<lang>.previous."""


def swap_paths(lang: str):
    """The installed folder of `lang`, its staging folder and the name the installed one
    is kept under while a new one takes its place."""
    return MODELS_DIR / lang, MODELS_DIR / f".{lang}.incoming", MODELS_DIR / f".{lang}.previous"


def remove_path(path: Path) -> None:
    """Remove a folder with everything in it, or a file or link (never followed)."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def recover_previous(lang: str) -> None:
    """Settle what an interrupted swap of `lang` left behind, before anything else may
    touch it. A kept .<lang>.previous with no installed folder: an earlier swap moved the
    installed voice aside and neither finished nor put it back (the container was killed,
    or the rename back failed), so it is renamed back now. A .previous next to an installed
    folder: that swap finished and only the removal of the old voice failed, so it is
    stale and removed. A later install therefore never replaces a kept voice before it
    was put back or known to be stale. Raises OSError when either step fails."""
    final, _, previous = swap_paths(lang)
    if not os.path.lexists(previous):
        return
    if os.path.lexists(final):
        remove_path(previous)
    else:
        os.rename(previous, final)


def recover_interrupted_installs() -> None:
    """recover_previous for every language, at start: a voice an interrupted install kept
    aside is installed again before the first request."""
    for lang in AVAILABLE_MODELS:
        try:
            recover_previous(lang)
        except OSError as e:
            print(f"[TTS] The voice of {lang} kept from an interrupted install could not be put back: {e}",
                  flush=True)


def swap_in(lang: str) -> None:
    """Put the staged folder of `lang` in place of the installed one, by renames only.

    The installed folder (if any) is renamed to .<lang>.previous, the staging folder to
    the installed name; if that second rename fails, nothing new is in place and the
    installed folder is renamed back. If that fails too, SwapRestoreError says so (both
    errors), and the voice stays whole in .previous for recover_previous. On success the
    old voice is removed; a failure there only leaves a stale .previous behind. The two
    files of a voice always move together, so the installed pair is never a mix of two
    downloads."""
    final, staging, previous = swap_paths(lang)
    moved_aside = False
    if os.path.lexists(final):
        os.rename(final, previous)
        moved_aside = True
    try:
        os.rename(staging, final)
    except OSError as e:
        if moved_aside:
            try:
                os.rename(previous, final)
            except OSError as restore_error:
                raise SwapRestoreError(f"the new voice could not be put in place ({e}), and putting the "
                                       f"installed one back failed too ({restore_error})") from e
        raise
    try:
        remove_path(previous)
    except OSError as e:
        print(f"[TTS] The replaced voice of {lang} could not be removed: {e}", flush=True)


def download_model(lang: str) -> bool:
    """Download a language model, verified against its pinned SHA-256. Returns True on
    success.

    Both files are fetched into a staging folder (.<lang>.incoming), each only as the
    bytes pinned for it (download_verified: its own partial file, renamed into place only
    once its content matched), and the staging folder replaces the installed one whole
    (swap_in), so the pair is never a mix. Before anything else, what an interrupted
    earlier install left is settled (recover_previous). A failure removes the staging
    folder first and only then reports the error status, so a retry the status allows
    never meets this attempt's cleanup; an installed voice stays as it was, and the status
    says why (a checksum mismatch, the download error, or a voice that could not be put
    back).

    Named boundary: a container killed between the two renames of swap_in, or a rename
    back that fails, leaves the language without an installed folder (it shows as not
    installed) and its old voice whole in .<lang>.previous. The next start of the
    container, or the next install of that language, puts it back before anything else."""
    if lang not in AVAILABLE_MODELS:
        return False

    model_info = AVAILABLE_MODELS[lang]
    final, staging, _ = swap_paths(lang)

    try:
        # Update progress
        with download_lock:
            download_progress[lang] = {"status": "downloading", "progress": 0}

        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        recover_previous(lang)
        remove_path(staging)              # a leftover of an earlier attempt of this language
        staging.mkdir()
        total_size = model_info["model_size"]

        def on_progress(received: int) -> None:
            with download_lock:
                download_progress[lang]["progress"] = int(received / total_size * 90)

        download_verified(model_info["model_url"], model_info["model_sha256"], staging / f"{lang}.onnx",
                          size=model_info["model_size"], timeout=60, progress=on_progress)

        with download_lock:
            download_progress[lang]["progress"] = 95

        download_verified(model_info["config_url"], model_info["config_sha256"], staging / f"{lang}.onnx.json",
                          size=model_info["config_size"], timeout=60)
        swap_in(lang)

        # Update config
        config = load_config()
        if lang not in config["installed_languages"]:
            config["installed_languages"].append(lang)
        save_config(config)

        with download_lock:
            download_progress[lang] = {"status": "completed", "progress": 100}

        return True

    except Exception as e:
        print(f"[TTS] Installing {lang} failed: {e}", flush=True)
        try:
            remove_path(staging)
        except OSError:
            pass
        if isinstance(e, SwapRestoreError):
            message = (f"The {model_info['name']} voice could not be installed or the old one restored; "
                       "install it again to restore it.")
        else:
            message = download_error_message(model_info["name"], e)
        with download_lock:
            download_progress[lang] = {"status": "error", "error": message, "detail": str(e)}
        return False


def remove_retired_languages() -> None:
    """At start: remove the installed voice of every retired language (and what an
    interrupted install of it left), and take the language out of the settings. A default
    language that was retired becomes DEFAULT_LANG. Removed in the order of /uninstall."""
    config = load_config()
    changed = False
    for code in RETIRED_LANGUAGES:
        final, staging, previous = swap_paths(code)
        removed = False
        for path in (previous, staging, final):
            if os.path.lexists(path):
                try:
                    remove_path(path)
                    removed = True
                except OSError as e:
                    print(f"[TTS] The voice of {code}, which this release no longer offers, could not be "
                          f"removed: {e}", flush=True)
        for key in ("installed_languages", "language_priority"):
            if code in config.get(key, []):
                config[key] = [c for c in config[key] if c != code]
                changed = True
        if config.get("default_language") == code:
            config["default_language"] = DEFAULT_LANG
            changed = True
        if removed:
            print(f"[TTS] Removed the voice of {code}: this release no longer offers that language.", flush=True)
    if changed:
        save_config(config)


def _download_guarded(lang: str) -> None:
    """download_model in a background thread: whatever it did not report itself still ends
    the "downloading" mark, or every later install of the language would be refused."""
    try:
        download_model(lang)
    except Exception as e:
        with download_lock:
            download_progress[lang] = {"status": "error", "detail": str(e),
                                       "error": f"The {AVAILABLE_MODELS[lang]['name']} voice could not be "
                                                "installed. Try again later."}


def _replacement_order() -> List[str]:
    """The table's languages, the default language first and then the priority list, so
    the voice a person hears first is installed first."""
    config = load_config()
    first = [config.get("default_language", DEFAULT_LANG), *config.get("language_priority", [])]
    ordered = [lang for lang in first if lang in AVAILABLE_MODELS]
    ordered += [lang for lang in AVAILABLE_MODELS if lang not in ordered]
    return list(dict.fromkeys(ordered))


def replace_unpinned_installs() -> List[str]:
    """At start: install again every language whose folder is there but whose voice is not
    the one the table pins (an earlier VAF installed another build or another speaker),
    the default language first. The languages are marked as downloading first, so the
    settings card shows the progress and /install does not start a second download of
    them; one background thread fetches them one after another, and the server answers
    meanwhile. A queued language whose mark is gone or was replaced before its turn (it
    was uninstalled, or installed anew) is skipped, so an uninstall is not undone by the
    queue. Returns the languages."""
    queued = {}
    for lang in _replacement_order():
        if os.path.lexists(MODELS_DIR / lang) and not is_language_installed(lang):
            with download_lock:
                if (download_progress.get(lang) or {}).get("status") == "downloading":
                    continue
                mark = download_progress[lang] = {"status": "downloading", "progress": 0}
            print(f"[TTS] The installed voice of {lang} is not the one this release pins; installing it again.",
                  flush=True)
            queued[lang] = mark

    def work() -> None:
        for lang, mark in queued.items():
            with download_lock:
                if download_progress.get(lang) is not mark:
                    continue
            _download_guarded(lang)

    if queued:
        threading.Thread(target=work, daemon=True).start()
    return list(queued)


def prepare_voices() -> None:
    """At start, before the server answers: a voice an interrupted install kept aside is
    put back; the voices of languages this release no longer offers go; an installed voice
    that is not the pinned one is installed again (in the background); and when nothing is
    installed, the default language is installed now, unless the background already
    replaces it (two downloads of one language would share its staging folder)."""
    recover_interrupted_installs()
    remove_retired_languages()
    replacing = replace_unpinned_installs()
    config = load_config()
    if not config.get("installed_languages") and DEFAULT_LANG not in replacing:
        print(f"No languages installed. Installing default: {DEFAULT_LANG}")
        if download_model(DEFAULT_LANG):
            print(f"Successfully installed {DEFAULT_LANG}")
        else:
            print(f"Failed to install {DEFAULT_LANG}")


def detect_language(text: str) -> str:
    """Detect the language of the text."""
    try:
        from langdetect import detect
        detected = detect(text)
        # Map langdetect codes to our codes
        lang_map = {
            "de": "de", "en": "en", "fr": "fr", "es": "es",
            "it": "it", "nl": "nl", "pl": "pl", "pt": "pt",
            "ru": "ru", "uk": "uk", "zh-cn": "zh", "zh-tw": "zh"
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

    # One download per language at a time: a second click while one runs is answered with
    # the running one instead of a second thread writing the same staged files.
    with download_lock:
        if (download_progress.get(lang) or {}).get("status") == "downloading":
            return jsonify({
                "status": "downloading",
                "language": lang,
                "message": f"{AVAILABLE_MODELS[lang]['name']} is already being downloaded"
            })
        download_progress[lang] = {"status": "downloading", "progress": 0}

    # Start download in background thread
    thread = threading.Thread(target=lambda: _download_guarded(lang), daemon=True)
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

    # Only a language of the table names a folder below MODELS_DIR ("../.." would not).
    if lang not in AVAILABLE_MODELS:
        return jsonify({"error": f"Unknown language: {lang}"}), 400

    # What an interrupted install left goes first, the installed folder last: a removal cut
    # short after it took the installed folder would otherwise leave a kept older voice
    # that the next start puts back.
    final, staging, previous = swap_paths(lang)
    for path in (previous, staging, final):
        if os.path.lexists(path):
            remove_path(path)

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

    prepare_voices()

    port = int(os.environ.get("TTS_PORT", 5000))
    print(f"Starting TTS server on port {port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
