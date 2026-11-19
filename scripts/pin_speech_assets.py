# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Pin the speech files a VAF release may download, digests included.

    venv/bin/python scripts/pin_speech_assets.py [--voices-revision <commit>]
                                                 [--smart-turn-revision <commit>]

Writes ``vaf/core/speech_assets_pin.json`` (read by ``vaf/core/speech_pin.py``) and the
voice table of the speech container, ``docker/tts-multilang/models.json``. VAF downloads
only what is listed there and refuses bytes that hash differently. Without a revision
option the current ``main`` commit of each Hugging Face repository is taken and printed.

Where each digest comes from, and what is checked before it is written down:

1. Piper (``rhasspy/piper``, release ``PIPER_TAG``). The repository is archived
   (read-only) and GitHub publishes no digest for these 2023 uploads, so the six
   archives are downloaded (about 139 MB) and hashed here: trust on first use. An asset
   replaced after its upload (``updated_at`` more than 60 s after ``created_at``) or not
   uploaded by ``github-actions[bot]`` is refused; a digest the API does report must
   match. Each archive is opened with the installer's own ``extract_archive`` into a
   temporary folder (unpacked, never run): every member must sit below one top folder
   ``piper/``, and the SHA-256 of the unpacked ``piper`` (``piper.exe``) program is
   recorded. The program's file header is printed so the architecture can be read off.
2. Voices (``rhasspy/piper-voices`` at one commit): the ``.onnx`` digests are the LFS
   SHA-256 Hugging Face publishes (nothing downloaded); each ``.onnx.json`` and each
   voice's ``MODEL_CARD`` is a plain git file, so it is downloaded (a few KB), its git blob
   id recomputed and compared with the published one, and then its SHA-256 recorded. Every
   voice of ``SpeechManager.PIPER_VOICES`` and of the container's table must exist there,
   and must have an entry in ``VOICE_LICENCES``, reviewed from its card by hand, with a
   licence in ``speech_pin.ALLOWED_WEIGHT_LICENCES``, a base in ``speech_pin.CLEAN_BASES``
   and the SHA-256 of the card it was reviewed from; a voice without one, or whose card
   now differs from the reviewed one at whatever revision, stops the run. ``host_piper`` is read
   from the pinned settings (``host_piper_can_load``), and a voice of
   ``PIPER_VOICES`` the pinned Piper cannot read stops the run too.
3. Models: Silero VAD and the ERes2Net speaker model from the digest GitHub reports for
   them; an asset without one (ERes2Net had none until its upload was renewed) is
   downloaded (about 26.5 MB) and hashed under the same ``updated_at`` rule; Smart Turn
   from its LFS SHA-256 at the given revision, and its licence read from the ``license:``
   line of its README at that revision. Each model is recorded with the licence in
   ``MODEL_LICENCES``.

Trust on first use means the first use, not every run: any file whose URL is the same as
in the committed manifest but whose digest (or program digest) differs is refused unless
``--accept-changed-digest`` is given, whatever the digest was taken from. A voice whose
``.onnx`` the committed manifest already pins is compared by its path across revisions
too: a moved revision serves new URLs, so the same voice name with other weights (a
retrain, possibly from another base) is refused the same way. A card is never accepted
by the flag: it must equal the one its licence entry was reviewed from. A committed
manifest that exists but cannot be read stops the run too.

Nothing downloaded is executed or imported. Downloads go to a temporary folder (under
``TMPDIR``) that is removed at the end.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
import tarfile
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from pin_llama_cpp import HEADERS, _get  # noqa: E402
from vaf.core import speech_pin  # noqa: E402
from vaf.core.verified_download import extract_archive  # noqa: E402

API = "https://api.github.com"
HF = "https://huggingface.co"
PIPER_TAG = "2023.11.14-2"
CONTAINER_TABLE = ROOT / "docker" / "tts-multilang" / "models.json"
# Where the container's voices live and what it calls each language; the voice itself
# (locale, voice, quality) is read from the existing table.
SHERPA = "k2-fsa/sherpa-onnx"
MODELS = {
    "silero_vad.onnx": ("github", SHERPA, "asr-models"),
    "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx": ("github", SHERPA, "speaker-recongition-models"),
    "smart-turn-v3.2-cpu.onnx": ("huggingface", "pipecat-ai/smart-turn-v3", None),
}
# The licence of each model and where it was read. Smart Turn's source is its README at the
# pinned revision, whose license: line is checked at every run.
MODEL_LICENCES = {
    "silero_vad.onnx": ("MIT", "https://github.com/snakers4/silero-vad/blob/master/LICENSE"),
    "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx": (
        "Apache-2.0", "https://modelscope.cn/models/iic/speech_eres2net_sv_en_voxceleb_16k"),
    "smart-turn-v3.2-cpu.onnx": ("BSD-2-Clause", None),
}
REPLACED_AFTER_UPLOAD = 60.0       # seconds between created_at and updated_at

# Every voice VAF may fetch, reviewed by hand from its MODEL_CARD at the pinned revision:
# the licence (an SPDX identifier from speech_pin.ALLOWED_WEIGHT_LICENCES), the dataset it
# was trained on, the model it was fine-tuned from ("scratch" when none), and the SHA-256 of
# the card that review read (card_sha256). A card that names no licence ("See URL") was
# followed to the dataset's own page, which the note names. A new voice needs an entry here
# before it can be pinned, and a card whose bytes differ from the reviewed one (at any
# revision) stops the run until the card is read again and its entry updated: the review
# is tied to the text it was made from, not to the voice's name.
_OHF = "https://github.com/OHF-Voice/voice-datasets"
_MLS = "https://www.openslr.org/94/"
_HIFI = "en_US-hifi_tts_92-medium"
# A fine-tune of the Hi-Fi TTS base carries that base's CC-BY-4.0 on top of its own licence.
HIFI_NOTE = ("fine-tuned from en_US-hifi_tts_92-medium, whose CC-BY-4.0 asks for credit to the Hi-Fi "
             "Multi-Speaker English TTS Dataset (NVIDIA Corporation) as well")
VOICE_LICENCES = {
    "de_DE-mls-medium": {
        "licence": "CC-BY-4.0",
        "dataset": _MLS,
        "base": "scratch",
        "note": "Multilingual LibriSpeech, speaker 0 of 236",
        "card_sha256": "ca1bf03a3c287fb6968acfa010e1917f85f0aa59db0f371efc3a2857f4035ffd",
    },
    "de_DE-thorsten-medium": {
        "licence": "CC0-1.0",
        "dataset": "https://github.com/thorstenMueller/Thorsten-Voice",
        "base": "nl_BE-rdh-medium",
        "card_sha256": "c638118fb06eb07d59e60270097c250cf2de81a037320a8a6306fec652ad13b0",
    },
    "el_GR-rapunzelina-medium": {
        "licence": "CC0-1.0",
        "dataset": "https://www.kaggle.com/datasets/bryanpark/greek-single-speaker-speech-dataset",
        "base": "scratch",
        "card_sha256": "dc69f15382dabed061fa8d31b65c14e5c4b57ba2fe6906012c756478da156b50",
    },
    "en_US-hifi_tts_92-medium": {
        "licence": "CC-BY-4.0",
        "dataset": "https://www.openslr.org/109/",
        "base": "scratch",
        "note": "Hi-Fi Multi-Speaker English TTS Dataset, NVIDIA Corporation; reader 92, Cori Samuel",
        "card_sha256": "62cafa98605100626d876aadcfd3dfa961cac263fa338b1fe17fe0989545e700",
    },
    "en_US-ljspeech-high": {
        "licence": "LicenseRef-public-domain",
        "dataset": "https://keithito.com/LJ-Speech-Dataset/",
        "base": "scratch",
        "card_sha256": "289f7421072d689a3d91f6c632f486af46f9b8b04417536104a4ea667b8a394b",
    },
    "es_ES-carlfm-x_low": {
        "licence": "LicenseRef-public-domain",
        "dataset": "https://github.com/carlfm01/my-speech-datasets",
        "base": "scratch",
        "card_sha256": "a6e62a3d36c37c7702d877f784001208b952e2bf4d17c72a6b5da442fbefb4b3",
    },
    "fr_FR-mls-medium": {
        "licence": "CC-BY-4.0",
        "dataset": _MLS,
        "base": "scratch",
        "note": "Multilingual LibriSpeech, 125 speakers",
        "card_sha256": "443ca90f1ed8e57d9fb802da6dc19c302d5d9b51ddbfbf27a17f3fb5c37ad154",
    },
    "fr_FR-siwis-medium": {
        "licence": "CC-BY-4.0",
        "dataset": "https://datashare.is.ed.ac.uk/handle/10283/2353",
        "base": _HIFI,
        "note": HIFI_NOTE,
        "card_sha256": "f75d76ec577b5e7a49a7462883d31ef65383022387818a9a0b75bdaf90b9010c",
    },
    "is_IS-bui-medium": {
        "licence": "CC-BY-4.0",
        "dataset": "https://hdl.handle.net/20.500.12537/104",
        "base": "scratch",
        "note": "the card says See URL; the dataset's page states CC BY 4.0",
        "card_sha256": "b01efb62ec770a1c1ebe2015d8f9985e6e1bf43fd72a42a6710d142e93752ce3",
    },
    "it_IT-riccardo-x_low": {
        "licence": "BSD-3-Clause",
        "dataset": "https://www.caito.de/2019/01/03/the-m-ailabs-speech-dataset/",
        "base": "scratch",
        "note": ("the card says See URL; the M-AILABS licence states BSD-3-Clause conditions in its own "
                 "wording, commercial use permitted"),
        "card_sha256": "7ce3b2fc4f454c84936887971574e436cc2154049b427a530f70bee050b2fdc6",
    },
    "kk_KZ-issai-high": {
        "licence": "CC-BY-4.0",
        "dataset": "https://github.com/IS2AI/Kazakh_TTS",
        "base": "scratch",
        "card_sha256": "1b0704348a17a5f84fba84caa20d14a116eea9953bb9bc147ada4234d18687a2",
    },
    "nl_NL-mls-medium": {
        "licence": "CC-BY-4.0",
        "dataset": _MLS,
        "base": "scratch",
        "note": "Multilingual LibriSpeech, 52 speakers",
        "card_sha256": "73d70ff5a8501a80335498135bd773cd9ae01133cf0ac33916538f4d51e13e6e",
    },
    "pl_PL-gosia-medium": {
        "licence": "CC0-1.0",
        "dataset": _OHF,
        "base": _HIFI,
        "note": HIFI_NOTE,
        "card_sha256": "238bae411adb04592aedec7f297270323b6f6f4a41542232cfc1c25fb61f75a9",
    },
    "pt_BR-faber-medium": {
        "licence": "CC0-1.0",
        "dataset": _OHF,
        "base": _HIFI,
        "note": HIFI_NOTE,
        "card_sha256": "a99caaa7e04fea58294ba4dea46a7ba07fa1f7902c6dff65b6ea2be0e98c5132",
    },
    "ru_RU-dmitri-medium": {
        "licence": "CC0-1.0",
        "dataset": _OHF,
        "base": _HIFI,
        "note": HIFI_NOTE,
        "card_sha256": "4da875aca6642bd96f0327705e874cbbcc44bc1790ee1d08b3fc0eb266523e5d",
    },
    "sv_SE-nst-medium": {
        "licence": "CC0-1.0",
        "dataset": "https://www.nb.no/sprakbanken/en/resource-catalogue/oai-nb-no-sbr-17/",
        "base": "scratch",
        "card_sha256": "c85d3e26e47d93e9d9b8572d3c89537c3f8ff212595cb9a3fbd2e07c19432317",
    },
    "uk_UA-ukrainian_tts-medium": {
        "licence": "CC0-1.0",
        "dataset": _OHF,
        "base": "scratch",
        "card_sha256": "ce09d0c9d1d90cdc98d1ea8a4ecbdeb5e937d44593876e4194302c5eabbaad9d",
    },
    "zh_CN-chaowen-medium": {
        "licence": "CC0-1.0",
        "dataset": _OHF,
        "base": _HIFI,
        "note": HIFI_NOTE,
        "card_sha256": "4f9386b738601ccfdcca09ba34deef9b976e60930a010c7d5444716d3cfdb0ef",
    },
}
# Languages an earlier container table held and this one drops; the container removes their
# installed voices at start. Japanese and Turkish: no voice of either has a licence on the list.
CONTAINER_RETIRED = ("ja", "tr")


def _post_form(url: str, data) -> list:
    r = requests.post(url, data=data, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=60)
    r.raise_for_status()
    return r.json()


def _download_hash(url: str, dest: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with requests.get(url, stream=True, timeout=60, headers={"User-Agent": HEADERS["User-Agent"]}) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    return digest.hexdigest(), size


def _cached_or_download(url: str, dest: Path, size: int) -> tuple[str, int]:
    """Hash `dest` when a file of the expected size is already there (a rerun with
    --download-dir), else download it."""
    if dest.is_file() and dest.stat().st_size == size:
        digest = hashlib.sha256()
        with open(dest, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest(), size
    return _download_hash(url, dest)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _check_github_asset(asset: dict, *, bot_upload: bool) -> None:
    name = asset.get("name")
    uploader = (asset.get("uploader") or {}).get("login")
    if bot_upload and uploader != "github-actions[bot]":
        raise SystemExit(f"{name}: uploaded by {uploader!r}, not github-actions[bot]; refusing to pin")
    gap = (_ts(asset["updated_at"]) - _ts(asset["created_at"])).total_seconds()
    if gap > REPLACED_AFTER_UPLOAD:
        raise SystemExit(f"{name}: updated {gap:.0f} s after its upload (replaced?); refusing to pin")


def _api_digest(asset: dict) -> str:
    d = str(asset.get("digest") or "")
    return d.split(":", 1)[1].lower() if d.startswith("sha256:") else ""


def _release_asset(repo: str, tag: str, name: str) -> dict:
    release = _get(f"{API}/repos/{repo}/releases/tags/{tag}")
    page = 1
    while True:
        assets = _get(f"{API}/repos/{repo}/releases/{release['id']}/assets?per_page=100&page={page}")
        for asset in assets:
            if asset.get("name") == name:
                return asset
        if len(assets) < 100:
            raise SystemExit(f"{repo} {tag}: no asset {name}")
        page += 1


def _header(path: Path) -> str:
    head = path.read_bytes()[:20]
    if head[:4] == b"\x7fELF":
        machine = int.from_bytes(head[18:20], "little")
        return f"ELF {'64' if head[4] == 2 else '32'}-bit, e_machine {machine:#x}"
    if head[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
        cpu = int.from_bytes(head[4:8], "little")
        return f"Mach-O, cputype {cpu:#x}"
    if head[:4] in (b"\xca\xfe\xba\xbe",):
        return "Mach-O universal"
    if head[:2] == b"MZ":
        return "PE (Windows)"
    return f"unknown header {head[:4]!r}"


def _members(archive: Path) -> list[str]:
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            return z.namelist()
    with tarfile.open(archive, "r:gz") as t:
        return t.getnames()


def pin_piper(work: Path) -> dict:
    release = _get(f"{API}/repos/{speech_pin.PIPER_REPO}/releases/tags/{PIPER_TAG}")
    by_name = {a["name"]: a for a in release.get("assets", [])}
    assets = {}
    for name in speech_pin.PIPER_ASSETS:
        asset = by_name.get(name)
        if asset is None:
            raise SystemExit(f"Piper {PIPER_TAG}: no asset {name}")
        _check_github_asset(asset, bot_upload=True)
        archive = work / name
        sha, size = _cached_or_download(asset["browser_download_url"], archive, asset["size"])
        if size != asset["size"]:
            raise SystemExit(f"{name}: {size} bytes downloaded, the API reports {asset['size']}")
        if _api_digest(asset) and _api_digest(asset) != sha:
            raise SystemExit(f"{name}: SHA-256 {sha} differs from the API digest {_api_digest(asset)}")
        names = _members(archive)
        stray = [n for n in names if n.replace("\\", "/").strip("/").split("/")[0] != "piper"]
        if stray:
            raise SystemExit(f"{name}: members outside the top folder piper/: {stray[:5]}")
        licenses = [n for n in names if any(k in n.lower() for k in ("license", "copying"))]
        program = "piper.exe" if name.endswith(".zip") else "piper"
        unpacked = work / (name + ".unpacked")
        if unpacked.exists():
            shutil.rmtree(unpacked)
        unpacked.mkdir()
        shutil.copyfile(archive, unpacked / name)      # extract_archive removes the archive it unpacks
        extract_archive(str(unpacked / name), str(unpacked), program=program, what="Piper")
        exe = unpacked / program
        if not exe.is_file() or exe.is_symlink():
            raise SystemExit(f"{name}: no regular {program} after unpacking")
        program_sha = hashlib.sha256(exe.read_bytes()).hexdigest()
        print(f"  {name}: {size} bytes, sha256 {sha}, {program} sha256 {program_sha} ({_header(exe)}), "
              f"{len(names)} members, license files: {licenses or 'none'}")
        for lic in licenses:
            p = unpacked / Path(lic).relative_to(Path(lic).parts[0]) if len(Path(lic).parts) > 1 else unpacked / lic
            if p.is_file():
                first = p.read_bytes()[:200].decode("utf-8", "replace").splitlines()[:2]
                print(f"    {lic}: {first}")
        assets[name] = {"sha256": sha, "size": size, "program_sha256": program_sha}
    return {
        "repository": speech_pin.PIPER_REPO, "tag": PIPER_TAG,
        "provenance": ("trust on first use from the archived (read-only) repository; GitHub publishes no "
                       "digest for these assets. Each asset was uploaded by github-actions[bot] and never "
                       "replaced (updated_at within 60 s of created_at); the archives were downloaded and "
                       "hashed, the program digest read from the unpacked archive."),
        "assets": assets,
    }


def _revision(repo: str, revision: str | None) -> str:
    sha = _get(f"{HF}/api/models/{repo}/revision/{revision or 'main'}")["sha"]
    print(f"  {repo}: {'main' if not revision else revision} = {sha}")
    return sha


def _paths_info(repo: str, revision: str, paths: list[str]) -> dict:
    out = {}
    for i in range(0, len(paths), 50):
        rows = _post_form(f"{HF}/api/models/{repo}/paths-info/{revision}",
                          [("paths", p) for p in paths[i:i + 50]] + [("expand", "false")])
        out.update({row["path"]: row for row in rows})
    return out


def _git_blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _one_codepoint(text) -> bool:
    """Piper's isSingleCodepoint: exactly one codepoint of valid UTF-8. A string that is
    not valid UTF-8 (a lone surrogate) makes Piper throw, so it fails closed."""
    if not isinstance(text, str):
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return len(text) == 1


def _all_finite(value) -> bool:
    """False when a NaN or an infinity sits anywhere in the parsed settings. Python's json
    reads NaN and Infinity, nlohmann::json in the Piper program refuses the whole file."""
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_all_finite(v) for v in value.values())
    if isinstance(value, list):
        return all(_all_finite(v) for v in value)
    return True


def host_piper_can_load(config: dict) -> bool:
    """Whether the pinned Piper program (2023.11.14-2) can read a voice's ``.onnx.json``.

    Mirrors ``parsePhonemizeConfig`` and ``parseSynthesisConfig`` in its ``piper.cpp``: a
    ``phoneme_type`` other than ``text`` is phonemized as espeak, so only ``espeak`` and
    ``text`` (or none) are read right; every key of ``phoneme_id_map``, every key and every
    value of ``phoneme_map`` and every key of ``inference.phoneme_silence`` must be one
    codepoint, or the program aborts. It also converts every id of ``phoneme_id_map`` to an
    integer and every value of ``phoneme_silence`` to a float, which throws for a string or
    a list; here an id must be an integer and a silence a number. Anything of another shape
    fails closed (a bool or a fractional id included, which no voice uses), and so does a
    NaN or an infinity anywhere in the file, which its JSON parser refuses."""
    if not isinstance(config, dict) or not _all_finite(config):
        return False
    if "phoneme_type" in config and config["phoneme_type"] not in ("espeak", "text"):
        return False
    id_map = config.get("phoneme_id_map", {})
    if not isinstance(id_map, dict) or not all(_one_codepoint(k) for k in id_map):
        return False
    for ids in id_map.values():
        if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            return False
    phoneme_map = config.get("phoneme_map", {})
    if not isinstance(phoneme_map, dict):
        return False
    for key, targets in phoneme_map.items():
        if not _one_codepoint(key) or not isinstance(targets, list) \
                or not all(_one_codepoint(t) for t in targets):
            return False
    inference = config.get("inference", {})
    if not isinstance(inference, dict):
        return False
    silence = inference.get("phoneme_silence", {})
    return isinstance(silence, dict) and all(
        _one_codepoint(k) and isinstance(v, (int, float)) and not isinstance(v, bool) for k, v in silence.items())


def _card_path(voice_file: str) -> str:
    return voice_file.rsplit("/", 1)[0] + "/MODEL_CARD"


def _curated(name: str) -> dict:
    """The reviewed licence entry of a voice; the run stops when there is none or it is not
    allowed."""
    entry = VOICE_LICENCES.get(name)
    if entry is None:
        raise SystemExit(f"voice {name} has no licence entry: review its MODEL_CARD and add it to VOICE_LICENCES")
    if entry.get("licence") not in speech_pin.ALLOWED_WEIGHT_LICENCES:
        raise SystemExit(f"voice {name}: licence {entry.get('licence')!r} is not in ALLOWED_WEIGHT_LICENCES")
    if entry.get("base") not in speech_pin.CLEAN_BASES:
        raise SystemExit(f"voice {name}: base {entry.get('base')!r} is not one of {sorted(speech_pin.CLEAN_BASES)}")
    if not str(entry.get("dataset") or "").startswith("https://"):
        raise SystemExit(f"voice {name}: the dataset is not an https URL")
    if not speech_pin._HEX64.fullmatch(str(entry.get("card_sha256") or "")):
        raise SystemExit(f"voice {name}: the entry does not name the SHA-256 of the card it was reviewed from")
    return entry


def _container_voices() -> dict:
    table = json.loads(CONTAINER_TABLE.read_bytes().decode("utf-8"))
    out = {}
    for lang, row in table["models"].items():
        out[lang] = {k: row[k] for k in ("name", "locale", "voice", "quality")}
    return out


def pin_voices(work: Path, revision: str) -> tuple[dict, dict]:
    from vaf.core.speech import SpeechManager

    container = _container_voices()
    kept = sorted(set(container) & set(CONTAINER_RETIRED))
    if kept:
        raise SystemExit(f"the container table still holds the retired languages {kept}")
    host = set(SpeechManager.PIPER_VOICES.values())
    names = sorted(host | {f"{v['locale']}-{v['voice']}-{v['quality']}" for v in container.values()})
    wanted = []
    for name in names:
        _curated(name)
        path = speech_pin.voice_path(name)
        if path is None:
            raise SystemExit(f"voice {name!r} is not locale-voice-quality")
        wanted += [path, path + ".json", _card_path(path)]
    info = _paths_info(speech_pin.VOICES_REPO, revision, wanted)
    missing = [p for p in wanted if p not in info]
    if missing:
        raise SystemExit(f"not in {speech_pin.VOICES_REPO} at {revision}: {missing}")
    files, cards, host_piper = {}, {}, {}
    for path in wanted:
        row = info[path]
        if path.endswith(".onnx"):
            lfs = row.get("lfs") or {}
            if not speech_pin._HEX64.fullmatch(str(lfs.get("oid") or "")):
                raise SystemExit(f"{path}: no LFS SHA-256 published")
            files[path] = {"sha256": lfs["oid"], "size": int(lfs.get("size") or row["size"])}
        else:
            dest = work / "voice.json"
            url = f"{HF}/{speech_pin.VOICES_REPO}/resolve/{revision}/{quote(path)}"
            sha, size = _download_hash(url, dest)
            data = dest.read_bytes()
            if _git_blob_id(data) != row.get("oid"):
                raise SystemExit(f"{path}: git blob id {_git_blob_id(data)} differs from the published "
                                 f"{row.get('oid')}")
            if path.endswith("/MODEL_CARD"):
                cards[path] = {"path": path, "sha256": sha, "size": size}
                continue
            files[path] = {"sha256": sha, "size": size}
            try:
                settings = json.loads(data.decode("utf-8"))
            except ValueError as e:
                raise SystemExit(f"{path}: not readable as JSON ({e})")
            host_piper[path[:-len(".json")]] = host_piper_can_load(settings)
    rows = {}
    for name in names:
        path = speech_pin.voice_path(name)
        entry = _curated(name)
        card = cards[_card_path(path)]
        if card["sha256"] != entry["card_sha256"]:
            raise SystemExit(f"voice {name}: its MODEL_CARD at {revision} (sha256 {card['sha256']}) is not the card "
                             f"its licence entry was reviewed from ({entry['card_sha256']}). Read the card again, "
                             "check licence, dataset and base, and update its VOICE_LICENCES entry.")
        if name in host and not host_piper[path]:
            raise SystemExit(f"voice {name} is in SpeechManager.PIPER_VOICES, but the pinned Piper {PIPER_TAG} "
                             "cannot read its settings (host_piper_can_load)")
        reviewed = {k: v for k, v in entry.items() if k != "card_sha256"}
        rows[name] = dict(reviewed, card=card, host_piper=host_piper[path])
        print(f"  {name}: {entry['licence']}, base {entry['base']}, "
              f"{'read' if host_piper[path] else 'not read'} by the pinned Piper")
    total = sum(m["size"] for p, m in files.items())
    print(f"  {len(names)} voices, {len(files)} files, {total} bytes in all")
    table = {"description": "The Piper voices the speech container may install, pinned by SHA-256 "
                            "(written by scripts/pin_speech_assets.py)",
             "source": f"{HF}/{speech_pin.VOICES_REPO}", "revision": revision, "models": {},
             "retired": list(CONTAINER_RETIRED)}
    for lang, v in container.items():
        name = f"{v['locale']}-{v['voice']}-{v['quality']}"
        path = speech_pin.voice_path(name)
        table["models"][lang] = dict(v, licence=rows[name]["licence"],
                                     licence_source=f"{HF}/{speech_pin.VOICES_REPO}/raw/{revision}/{_card_path(path)}",
                                     model=dict(path=path, **files[path]),
                                     config=dict(path=path + ".json", **files[path + ".json"]))
    return {"repository": speech_pin.VOICES_REPO, "revision": revision, "files": files, "voices": rows}, table


def pin_models(work: Path, smart_turn_revision: str | None) -> dict:
    out = {}
    for name, (kind, repo, tag) in MODELS.items():
        if kind == "github":
            asset = _release_asset(repo, tag, name)
            _check_github_asset(asset, bot_upload=False)
            print(f"  {name}: uploaded by {(asset.get('uploader') or {}).get('login')} at {asset['created_at']}")
            sha = _api_digest(asset)
            size = asset["size"]
            if not sha:
                sha, got = _cached_or_download(asset["browser_download_url"], work / name, size)
                if got != size:
                    raise SystemExit(f"{name}: {got} bytes downloaded, the API reports {size}")
                how = "downloaded and hashed (no published digest)"
            else:
                how = "GitHub digest"
            out[name] = {"url": asset["browser_download_url"], "sha256": sha, "size": size}
            licence, source = MODEL_LICENCES[name]
        else:
            rev = _revision(repo, smart_turn_revision)
            row = _paths_info(repo, rev, [name]).get(name)
            lfs = (row or {}).get("lfs") or {}
            if not speech_pin._HEX64.fullmatch(str(lfs.get("oid") or "")):
                raise SystemExit(f"{repo}/{name}: no LFS SHA-256 at {rev}")
            out[name] = {"url": f"{HF}/{repo}/resolve/{rev}/{quote(name)}", "sha256": lfs["oid"],
                         "size": int(lfs.get("size") or row["size"])}
            how = "LFS SHA-256"
            licence, source = MODEL_LICENCES[name][0], f"{HF}/{repo}/raw/{rev}/README.md"
            stated = _readme_licence(source)
            if stated != licence.lower():
                raise SystemExit(f"{repo} at {rev}: its README says license {stated!r}, not {licence}")
        if licence not in speech_pin.ALLOWED_WEIGHT_LICENCES:
            raise SystemExit(f"{name}: licence {licence!r} is not in ALLOWED_WEIGHT_LICENCES")
        out[name].update(licence=licence, licence_source=source)
        print(f"  {name}: {out[name]['size']} bytes, sha256 {out[name]['sha256']} ({how})")
    return out


def _readme_licence(url: str) -> str:
    """The ``license:`` value of a Hugging Face README's front matter, lowercased."""
    r = requests.get(url, timeout=60, headers={"User-Agent": HEADERS["User-Agent"]})
    r.raise_for_status()
    for line in r.text.splitlines():
        if line.strip().lower().startswith("license:"):
            return line.split(":", 1)[1].strip().lower()
    return ""


def _digests_by_url(manifest: dict) -> dict:
    """{url: {label: digest}} for every file a manifest pins: the Piper archives ("archive"
    and "program"), every voice file at its revision and every model ("file"), and every
    voice's MODEL_CARD ("card")."""
    out = {}
    piper = manifest.get("piper") or {}
    for name, meta in (piper.get("assets") or {}).items():
        url = f"https://github.com/{piper.get('repository')}/releases/download/{piper.get('tag')}/{name}"
        out[url] = {"archive": meta.get("sha256"), "program": meta.get("program_sha256")}
    voices = manifest.get("piper_voices") or {}
    for path, meta in (voices.get("files") or {}).items():
        url = f"{HF}/{voices.get('repository')}/resolve/{voices.get('revision')}/{quote(path)}"
        out[url] = {"file": meta.get("sha256")}
    for row in (voices.get("voices") or {}).values():
        card = (row or {}).get("card") or {}
        url = f"{HF}/{voices.get('repository')}/resolve/{voices.get('revision')}/{quote(str(card.get('path')))}"
        out[url] = {"card": card.get("sha256")}
    for meta in (manifest.get("models") or {}).values():
        out[meta.get("url")] = {"file": meta.get("sha256")}
    return out


def changed_digests(old: dict, new: dict) -> list[str]:
    """Every file whose URL is in both manifests and whose digest differs: the same
    address now serves other bytes. No hand-kept list: this holds for the Piper archives
    (also when only the program digest moved), the voices and every model, whichever
    source the digest came from. A file at a new URL (a new tag or revision) is not a
    change. One line per file, each changed digest labeled with what it is a digest of."""
    before = _digests_by_url(old)
    lines = []
    for url, now in _digests_by_url(new).items():
        was = before.get(url)
        if was is None or was == now:
            continue
        moved = [f"{label} {was.get(label)} -> {digest}" for label, digest in now.items() if was.get(label) != digest]
        lines.append(f"{url}: " + "; ".join(moved))
    return lines


def changed_voice_weights(old: dict, new: dict) -> list[str]:
    """Every voice model (``.onnx``) both manifests pin by the same path at different
    revisions, whose digest differs: the same voice name now stands for other weights (a
    retrain, possibly from another base), which a comparison by URL never sees because the
    URL carries the revision. One line per voice. At the same revision changed_digests
    already reports it."""
    was, now = old.get("piper_voices") or {}, new.get("piper_voices") or {}
    if was.get("revision") == now.get("revision"):
        return []
    before = was.get("files") or {}
    lines = []
    for path, meta in (now.get("files") or {}).items():
        prior = before.get(path)
        if path.endswith(".onnx") and prior is not None and prior.get("sha256") != meta.get("sha256"):
            lines.append(f"{path}: {prior.get('sha256')} at {was.get('revision')} -> {meta.get('sha256')} at "
                         f"{now.get('revision')}")
    return lines


def _shown(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _committed_manifest() -> dict:
    """The committed manifest, or {} when there is none yet (a first run). One that exists
    but cannot be read stops the run: comparing against nothing would accept every changed
    digest."""
    if not speech_pin.PIN_PATH.exists():
        return {}
    try:
        data = json.loads(speech_pin.PIN_PATH.read_bytes().decode("utf-8"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"{_shown(speech_pin.PIN_PATH)} exists but cannot be read ({e}); restore it from git "
                         "before pinning, so new digests can be compared with the committed ones.")
    if not isinstance(data, dict):
        raise SystemExit(f"{_shown(speech_pin.PIN_PATH)} is not a manifest; restore it from git before pinning.")
    return data


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--voices-revision")
    parser.add_argument("--smart-turn-revision")
    parser.add_argument("--download-dir", help="keep the downloads here and reuse them on a rerun "
                                               "(default: a temporary folder removed at the end)")
    parser.add_argument("--accept-changed-digest", action="store_true",
                        help="record a new digest for a file the committed manifest already pins at the same "
                             "URL; refused otherwise")
    args = parser.parse_args(argv)
    old = _committed_manifest()
    with tempfile.TemporaryDirectory(prefix="vaf-speech-pin-") as tmp:
        work = Path(args.download_dir) if args.download_dir else Path(tmp)
        work.mkdir(parents=True, exist_ok=True)
        # The metadata first, the large downloads last: a refused name stops the run early.
        print("Voices:")
        voices, table = pin_voices(work, _revision(speech_pin.VOICES_REPO, args.voices_revision))
        print("Models:")
        models = pin_models(work, args.smart_turn_revision)
        print("Piper:")
        piper = pin_piper(work)
    manifest = {"recorded": date.today().isoformat(), "piper": piper, "piper_voices": voices, "models": models}
    changes = changed_digests(old, manifest)
    retrained = changed_voice_weights(old, manifest)
    if changes:
        print("These files are pinned at the same address, and their digest is different now:")
        for line in changes:
            print(f"  {line}")
    if retrained:
        print("These voices are already pinned, and the new revision gives them other weights (review their "
              "cards and VOICE_LICENCES before you accept them):")
        for line in retrained:
            print(f"  {line}")
    if changes or retrained:
        if not args.accept_changed_digest:
            raise SystemExit("Refusing to record the new digests. Find out why the published file changed; "
                             "only then rerun with --accept-changed-digest.")
    speech_pin.PIN_PATH.write_bytes((json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    CONTAINER_TABLE.write_bytes((json.dumps(table, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    speech_pin.load_speech_pin()          # what was written must load
    print(f"Wrote {_shown(speech_pin.PIN_PATH)} and {_shown(CONTAINER_TABLE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
