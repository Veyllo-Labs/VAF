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
  fetch from it (the ``.onnx`` model and its ``.onnx.json`` settings), by path, and per
  voice (``voices``) its licence record;
- ``models``: the speaker recognition and turn-end models, each with its URL and licence.

Every download of these goes through ``vaf/core/verified_download.py`` with the digest
recorded here. A name this manifest does not hold gets ``None``, and the caller refuses:
there is no fallback to an unpinned URL. A missing or malformed manifest raises
``RuntimeError``, so a broken release installs nothing rather than something unchecked.

Licences. Model weights carry a licence of their own, independent of the code that loads
them, and VAF offers only weights whose licence allows any use, commercial use and
redistribution included: ``ALLOWED_WEIGHT_LICENCES``, the rule of "Container images and
model weights" in ``docs/legal/THIRD_PARTY.md``. Each voice row (``piper_voices.voices``)
records its licence, its dataset, the model it was fine-tuned from (``base``; a fine-tune
carries the licence of the weights it started from, so only the bases in ``CLEAN_BASES``
count), its ``MODEL_CARD`` by digest, and ``host_piper``: whether the pinned Piper program
can read the voice's settings at all (that 2023 build refuses a phoneme of more than one
codepoint, and reads every phoneme type but ``text`` as espeak). Each model records its
licence and where it was read. A licence outside the list is not a load error, which would
take every other speech file down with it: ``voice_files`` and ``model`` answer None for
that one entry, so it is never downloaded. A base outside ``CLEAN_BASES`` is refused by
the loader (the pin script never writes one).

Named boundary: nothing outside ``vaf/core`` calls this module (measured: 0 callers), and
text-to-speech is the embedder's own (``docs/EMBEDDING.md``, "TTS is YOURS"), so the
facade exports none of it. The speaker models and the turn-end model an embedder does
reach through ``VoiceTurnEngine`` are refused here like any other weights.
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
# The licences under which VAF offers model weights, as SPDX identifiers (public domain has
# none, hence the LicenseRef). Each allows any use, commercial use and redistribution
# included; CC0 and a public-domain dedication impose no condition at all. Weights with a
# non-commercial clause, a share-alike term, a revenue threshold or any other use
# restriction are not offered: VAF is distributed under the AGPL, which forbids passing such
# restrictions on, and is also licensed commercially (docs/legal/THIRD_PARTY.md).
ALLOWED_WEIGHT_LICENCES = ("MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "CC-BY-4.0", "CC0-1.0",
                           "LicenseRef-public-domain")
# What a voice may have been fine-tuned from, with that base's licence: trained from scratch,
# or one of the two bases rhasspy/piper-voices trained from scratch on clean data for this.
# A voice fine-tuned from anything else (en_US-lessac, licensed for non-commercial use only,
# is the one most voices started from) is not offered, whatever its own dataset allows.
CLEAN_BASES = {"scratch": None, "en_US-hifi_tts_92-medium": "CC-BY-4.0", "nl_BE-rdh-medium": "CC0-1.0"}
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
    known names, voice paths plain relative paths, model URLs https; every voice file
    belongs to a voice row with a licence, a ``host_piper`` flag and its pinned
    ``MODEL_CARD``, and every model names its licence and an https source for it."""
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
    rows = voices.get("voices")
    if not isinstance(rows, dict) or not rows:
        raise _bad("no voice licences")
    owned = set()
    for name, row in rows.items():
        path = voice_path(name)
        if path is None or not _VOICE_PATH.fullmatch(path):
            raise _bad(f"bad voice name {name!r}")
        if not isinstance(row, dict):
            raise _bad(f"voice {name} is not an entry")
        if path not in voices["files"] or path + ".json" not in voices["files"]:
            raise _bad(f"voice {name} has no pinned files")
        owned |= {path, path + ".json"}
        if not isinstance(row.get("licence"), str) or not row["licence"].strip():
            raise _bad(f"voice {name} has no licence")
        if not isinstance(row.get("host_piper"), bool):
            raise _bad(f"voice {name} does not say whether the host Piper reads it")
        if not isinstance(row.get("base"), str) or row["base"] not in CLEAN_BASES:
            raise _bad(f"voice {name} was not trained from scratch or from a clean base")
        card = row.get("card")
        _check_file_entry(f"the card of {name}", card)
        if card.get("path") != path.rsplit("/", 1)[0] + "/MODEL_CARD":
            raise _bad(f"the card of {name} is not its MODEL_CARD")
    stray = sorted(set(voices["files"]) - owned)
    if stray:
        raise _bad(f"voice files without a licence entry: {stray[:3]}")
    models = data.get("models")
    if not isinstance(models, dict):
        raise _bad("no models")
    for name, meta in models.items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise _bad(f"bad model name {name!r}")
        _check_file_entry(name, meta)
        if not str(meta.get("url") or "").startswith("https://"):
            raise _bad(f"{name} has no https URL")
        if not isinstance(meta.get("licence"), str) or not meta["licence"].strip():
            raise _bad(f"{name} has no licence")
        if not str(meta.get("licence_source") or "").startswith("https://"):
            raise _bad(f"{name} does not say where its licence was read")
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
    """The pinned ``.onnx`` and ``.onnx.json`` of a voice for Piper on this computer, each
    ``{path, url, sha256, size}``. None when the voice is not in the manifest, when its
    licence is not on the list, or when the pinned Piper cannot read it (``host_piper``):
    such a voice is never downloaded. A base outside ``CLEAN_BASES`` never gets here: the
    loader refuses the whole manifest, as only a broken release can carry one."""
    path = voice_path(model_name)
    if path is None:
        return None
    pin = load_speech_pin()
    voices = pin["piper_voices"]
    row = voices["voices"].get(model_name)
    if row is None or row["licence"] not in ALLOWED_WEIGHT_LICENCES or row["host_piper"] is not True:
        return None
    out = []
    for p in (path, path + ".json"):
        meta = voices["files"].get(p)
        if meta is None:
            return None
        url = f"https://huggingface.co/{VOICES_REPO}/resolve/{voices['revision']}/{quote(p)}"
        out.append({"path": p, "url": url, "sha256": meta["sha256"], "size": meta["size"]})
    return out[0], out[1]


def model(name: str) -> Optional[dict]:
    """``{name, url, sha256, size}`` of a pinned model file, or None (also when its licence
    is not on the list)."""
    meta = load_speech_pin()["models"].get(name)
    if meta is None or meta.get("licence") not in ALLOWED_WEIGHT_LICENCES:
        return None
    return {"name": name, "url": meta["url"], "sha256": meta["sha256"], "size": meta["size"]}
