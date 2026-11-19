# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The speech files a VAF release may download, with the SHA-256 of each.

``vaf/core/speech_assets_pin.json`` is written by ``scripts/pin_speech_assets.py`` when a
release is prepared and only read here. It names:

- ``piper``: the Piper release (``rhasspy/piper``, an archived repository) and, per
  archive, its SHA-256, its size and the SHA-256 of the ``piper`` program inside it. The
  program digest is what decides whether an installed Piper is the pinned one;
- ``piper_voices``: one commit of ``rhasspy/piper-voices`` and every voice file VAF may
  fetch from it (the ``.onnx`` model and its ``.onnx.json`` settings), by path;
- ``models``: the speaker recognition and turn-end models, each with its URL.

Every download of these goes through ``vaf/core/verified_download.py`` with the digest
recorded here. A name this manifest does not hold gets ``None``, and the caller refuses:
there is no fallback to an unpinned URL. A missing or malformed manifest raises
``RuntimeError``, so a broken release installs nothing rather than something unchecked.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote

PIN_PATH = Path(__file__).with_name("speech_assets_pin.json")
PIPER_REPO = "rhasspy/piper"
VOICES_REPO = "rhasspy/piper-voices"
# The archives the installer can pick: Linux x86_64, aarch64 and armv7l, macOS arm64 and
# x64, Windows x64.
PIPER_ASSETS = (
    "piper_linux_x86_64.tar.gz", "piper_linux_aarch64.tar.gz", "piper_linux_armv7l.tar.gz",
    "piper_macos_aarch64.tar.gz", "piper_macos_x64.tar.gz", "piper_windows_amd64.zip",
)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX40 = re.compile(r"[0-9a-f]{40}")
# A voice file's path in the repository: language/locale/voice/quality/file, plain
# characters only (the path ends up in a URL and in a file name).
_VOICE_PATH = re.compile(r"[a-z]{2,3}/[A-Za-z]{2,3}_[A-Za-z]{2}/[A-Za-z0-9_]+/[a-z_]+/[A-Za-z0-9_.-]+\.onnx(\.json)?")


def _bad(what: str) -> RuntimeError:
    return RuntimeError(f"speech pin manifest at {PIN_PATH}: {what}")


def _check_file_entry(where: str, meta) -> None:
    if not isinstance(meta, dict):
        raise _bad(f"{where} is not an entry")
    if not _HEX64.fullmatch(str(meta.get("sha256") or "")):
        raise _bad(f"{where} has no SHA-256")
    size = meta.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise _bad(f"{where} has no size")


def load_speech_pin() -> dict:
    """The manifest, validated. RuntimeError when it is missing or malformed: every
    ``sha256`` and ``program_sha256`` 64 lowercase hex digits, every ``size`` a positive
    integer, the voices' ``revision`` a 40-digit commit, the Piper archives among the six
    known names, voice paths plain relative paths, model URLs https."""
    try:
        data = json.loads(PIN_PATH.read_bytes().decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"speech pin manifest unreadable at {PIN_PATH}: {e}") from e
    if not isinstance(data, dict):
        raise _bad("not an object")
    piper = data.get("piper")
    if not isinstance(piper, dict) or not str(piper.get("tag") or "").strip() \
            or not isinstance(piper.get("assets"), dict) or not piper["assets"]:
        raise _bad("no Piper release")
    if not re.fullmatch(r"[0-9A-Za-z._-]+", str(piper["tag"])):
        raise _bad("bad Piper tag")
    for name, meta in piper["assets"].items():
        if name not in PIPER_ASSETS:
            raise _bad(f"unknown Piper archive {name!r}")
        _check_file_entry(name, meta)
        if not _HEX64.fullmatch(str(meta.get("program_sha256") or "")):
            raise _bad(f"{name} has no program digest")
    voices = data.get("piper_voices")
    if not isinstance(voices, dict) or not _HEX40.fullmatch(str(voices.get("revision") or "")) \
            or not isinstance(voices.get("files"), dict) or not voices["files"]:
        raise _bad("no voice revision")
    for path, meta in voices["files"].items():
        if not _VOICE_PATH.fullmatch(path) or ".." in path.split("/"):
            raise _bad(f"bad voice path {path!r}")
        _check_file_entry(path, meta)
    models = data.get("models")
    if not isinstance(models, dict):
        raise _bad("no models")
    for name, meta in models.items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise _bad(f"bad model name {name!r}")
        _check_file_entry(name, meta)
        if not str(meta.get("url") or "").startswith("https://"):
            raise _bad(f"{name} has no https URL")
    return data


def piper_asset(name: str) -> Optional[dict]:
    """``{name, url, sha256, size, program_sha256}`` of a pinned Piper archive, or None
    when the manifest holds no such archive."""
    pin = load_speech_pin()
    meta = pin["piper"]["assets"].get(name)
    if meta is None:
        return None
    return {"name": name, "url": f"https://github.com/{PIPER_REPO}/releases/download/{pin['piper']['tag']}/{name}",
            "sha256": meta["sha256"], "size": meta["size"], "program_sha256": meta["program_sha256"]}


def voice_path(model_name: str) -> Optional[str]:
    """Where a voice lives in rhasspy/piper-voices, from its name alone:
    ``de_DE-thorsten-high`` -> ``de/de_DE/thorsten/high/de_DE-thorsten-high.onnx``. The
    language folder is the locale's language, so an English fallback for another language
    still finds the English voice. None for a name that is not locale-voice-quality."""
    parts = (model_name or "").split("-")
    if len(parts) != 3 or "_" not in parts[0] or not all(parts):
        return None
    locale, voice, quality = parts
    return f"{locale.split('_')[0]}/{locale}/{voice}/{quality}/{model_name}.onnx"


def voice_files(model_name: str) -> Optional[Tuple[dict, dict]]:
    """The pinned ``.onnx`` and ``.onnx.json`` of a voice, each ``{path, url, sha256,
    size}``, or None when either is not in the manifest."""
    path = voice_path(model_name)
    if path is None:
        return None
    pin = load_speech_pin()
    voices = pin["piper_voices"]
    out = []
    for p in (path, path + ".json"):
        meta = voices["files"].get(p)
        if meta is None:
            return None
        url = f"https://huggingface.co/{VOICES_REPO}/resolve/{voices['revision']}/{quote(p)}"
        out.append({"path": p, "url": url, "sha256": meta["sha256"], "size": meta["size"]})
    return out[0], out[1]


def model(name: str) -> Optional[dict]:
    """``{name, url, sha256, size}`` of a pinned model file, or None."""
    meta = load_speech_pin()["models"].get(name)
    if meta is None:
        return None
    return {"name": name, "url": meta["url"], "sha256": meta["sha256"], "size": meta["size"]}
