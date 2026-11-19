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
   SHA-256 Hugging Face publishes (nothing downloaded); each ``.onnx.json`` is a plain git
   file, so it is downloaded (a few KB), its git blob id recomputed and compared with the
   published one, and then its SHA-256 recorded. Every voice of
   ``SpeechManager.PIPER_VOICES`` and of the container's table must exist there.
3. Models: Silero VAD from the digest GitHub reports for it; the ERes2Net speaker model
   has none, so it is downloaded (about 26.5 MB) and hashed under the same
   ``updated_at`` rule; Smart Turn from its LFS SHA-256 at the given revision.

Nothing downloaded is executed or imported. Downloads go to a temporary folder (under
``TMPDIR``) that is removed at the end.
"""
from __future__ import annotations

import argparse
import hashlib
import json
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
REPLACED_AFTER_UPLOAD = 60.0       # seconds between created_at and updated_at


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


def _container_voices() -> dict:
    table = json.loads(CONTAINER_TABLE.read_bytes().decode("utf-8"))
    out = {}
    for lang, row in table["models"].items():
        out[lang] = {k: row[k] for k in ("name", "locale", "voice", "quality")}
    return out


def pin_voices(work: Path, revision: str) -> tuple[dict, dict]:
    from vaf.core.speech import SpeechManager

    container = _container_voices()
    names = sorted(set(SpeechManager.PIPER_VOICES.values())
                   | {f"{v['locale']}-{v['voice']}-{v['quality']}" for v in container.values()})
    wanted = []
    for name in names:
        path = speech_pin.voice_path(name)
        if path is None:
            raise SystemExit(f"voice {name!r} is not locale-voice-quality")
        wanted += [path, path + ".json"]
    info = _paths_info(speech_pin.VOICES_REPO, revision, wanted)
    missing = [p for p in wanted if p not in info]
    if missing:
        raise SystemExit(f"not in {speech_pin.VOICES_REPO} at {revision}: {missing}")
    files = {}
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
                raise SystemExit(f"{path}: git blob id {_git_blob_id(data)} differs from the published {row.get('oid')}")
            files[path] = {"sha256": sha, "size": size}
    total = sum(m["size"] for p, m in files.items())
    print(f"  {len(names)} voices, {len(files)} files, {total} bytes in all")
    table = {"description": "The Piper voices the speech container may install, pinned by SHA-256 "
                            "(written by scripts/pin_speech_assets.py)",
             "source": f"{HF}/{speech_pin.VOICES_REPO}", "revision": revision, "models": {}}
    for lang, v in container.items():
        path = speech_pin.voice_path(f"{v['locale']}-{v['voice']}-{v['quality']}")
        table["models"][lang] = dict(v, model=dict(path=path, **files[path]),
                                     config=dict(path=path + ".json", **files[path + ".json"]))
    return {"repository": speech_pin.VOICES_REPO, "revision": revision, "files": files}, table


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
        else:
            rev = _revision(repo, smart_turn_revision)
            row = _paths_info(repo, rev, [name]).get(name)
            lfs = (row or {}).get("lfs") or {}
            if not speech_pin._HEX64.fullmatch(str(lfs.get("oid") or "")):
                raise SystemExit(f"{repo}/{name}: no LFS SHA-256 at {rev}")
            out[name] = {"url": f"{HF}/{repo}/resolve/{rev}/{quote(name)}", "sha256": lfs["oid"],
                         "size": int(lfs.get("size") or row["size"])}
            how = "LFS SHA-256"
        print(f"  {name}: {out[name]['size']} bytes, sha256 {out[name]['sha256']} ({how})")
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--voices-revision")
    parser.add_argument("--smart-turn-revision")
    parser.add_argument("--download-dir", help="keep the downloads here and reuse them on a rerun "
                                               "(default: a temporary folder removed at the end)")
    args = parser.parse_args(argv)
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
    speech_pin.PIN_PATH.write_bytes((json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    CONTAINER_TABLE.write_bytes((json.dumps(table, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    speech_pin.load_speech_pin()          # what was written must load
    print(f"Wrote {speech_pin.PIN_PATH.relative_to(ROOT).as_posix()} and {CONTAINER_TABLE.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
