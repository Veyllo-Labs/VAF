# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Pin the llama.cpp build a VAF release ships with, digests included.

    venv/bin/python scripts/pin_llama_cpp.py b12359

Writes ``vaf/core/llama_server_pin.json``: the tag, and for every release asset the
launcher can pick (``backend.LLAMA_ASSET_PATTERN``) its SHA-256 and size. The launcher
downloads only what is listed here and refuses bytes that hash differently, so a release
asset replaced after the pin, a truncated transfer or a poisoned mirror never runs.

Where the digests come from, and what is checked before they are written down:

1. The GitHub release API reports a ``digest`` per asset (GitHub hashes the upload).
2. llama.cpp's release workflow attests its assets (SLSA v1 provenance, stored by GitHub
   under /attestations/<digest>). Since llama.cpp split its release into a build workflow
   and a publish workflow, the statement names ``.github/workflows/release-publish.yml``
   (a ``workflow_run`` job that attests the artifacts of the build run); older builds
   name ``.github/workflows/release.yml``. Exactly these two paths of ``ggml-org/llama.cpp``
   (repository id ``LLAMA_REPO_ID``) are accepted as the builder.
   The statements are looked up by the digest of a ``llama-<tag>-`` asset, which is unique
   to the build; the ``cudart-llama-bin-*`` archives are byte-identical across builds, so
   their digest returns the statements of many builds. Of the statements returned, the
   first one that names an accepted builder and lists EVERY selected asset with the
   digest the API reports is the one taken, wherever it sits in the list. A digest no
   accepted builder attested is not pinned.

This is a structural cross-check of the provenance statement, not a verification of its
Sigstore signature; for that, run ``gh attestation verify --repo ggml-org/llama.cpp
<downloaded asset>`` on a machine with the GitHub CLI. ``--download`` additionally pulls
every selected asset and hashes it locally against the API digest (about 2 GB for b12359).

Bumping the pin is a release decision: VAF never updates llama.cpp on its own, a newer
build arrives with the VAF release that carries the new manifest.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vaf.core.backend import LLAMA_ASSET_PATTERN, LLAMA_REPO, LLAMA_REPO_ID, PIN_PATH  # noqa: E402

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "vaf-pin-llama-cpp"}
# The workflows of ggml-org/llama.cpp that may attest a release, compared exactly.
ALLOWED_BUILDERS = (".github/workflows/release.yml", ".github/workflows/release-publish.yml")


def _get(url: str):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def _digest(asset: dict) -> str:
    d = str(asset.get("digest") or "")
    if not d.startswith("sha256:"):
        raise SystemExit(f"{asset.get('name')}: the API reports no sha256 digest ({d!r}); refusing to pin")
    return d.split(":", 1)[1].lower()


def _lookup_digest(tag: str, digests: dict) -> str:
    """The digest the attestations are looked up by: that of a ``llama-<tag>-`` asset."""
    prefix = f"llama-{tag}-"
    for name, digest in digests.items():
        if name.startswith(prefix):
            return digest
    raise SystemExit(f"{tag}: no selected asset is named {prefix}*; the shared cudart archives cannot "
                     "identify the build's provenance statement")


def _attesting_builder(tag: str, digests: dict) -> str:
    """The builder of the provenance statement that attests every selected asset with the
    digest the API reports; SystemExit with the reason when no returned statement does."""
    data = _get(f"{API}/repos/{LLAMA_REPO}/attestations/sha256:{_lookup_digest(tag, digests)}?per_page=100")
    atts = data.get("attestations") or []
    if not atts:
        raise SystemExit("no provenance attestation for the release assets; refusing to pin")
    reasons: list[str] = []
    closest: list[str] | None = None
    for att in atts:
        if int(att.get("repository_id") or 0) != LLAMA_REPO_ID:
            reasons.append(f"an attestation of repository {att.get('repository_id')}, not {LLAMA_REPO_ID}")
            continue
        try:
            payload = ((att.get("bundle") or {}).get("dsseEnvelope") or {}).get("payload")
            statement = json.loads(base64.b64decode(payload))
        except Exception as e:
            reasons.append(f"an unreadable statement ({e})")
            continue
        build = (statement.get("predicate") or {}).get("buildDefinition") or {}
        wf = (build.get("externalParameters") or {}).get("workflow") or {}
        builder = f"{wf.get('repository')} {wf.get('path')}"
        if wf.get("repository") != f"https://github.com/{LLAMA_REPO}" or wf.get("path") not in ALLOWED_BUILDERS:
            reasons.append(f"a statement by an unexpected builder: {builder}")
            continue
        subjects = {s.get("name"): str((s.get("digest") or {}).get("sha256", "")).lower()
                    for s in statement.get("subject") or []}
        missing = [n for n, d in digests.items() if subjects.get(n) != d]
        if not missing:
            return builder
        if closest is None or len(missing) < len(closest):
            closest = missing
    if closest is not None:
        raise SystemExit(f"no provenance statement attests these assets with the API's digest: {closest}")
    raise SystemExit("no usable provenance statement; refusing to pin: " + "; ".join(dict.fromkeys(reasons)))


def main(argv: list[str]) -> int:
    if len(argv) < 2 or not re.fullmatch(r"b\d+", argv[1]):
        print(__doc__)
        return 2
    tag = argv[1]
    verify_download = "--download" in argv
    release = _get(f"{API}/repos/{LLAMA_REPO}/releases/tags/{tag}")
    selected = [a for a in release.get("assets") or [] if LLAMA_ASSET_PATTERN.fullmatch(a["name"])]
    if not selected:
        raise SystemExit(f"{tag}: no asset matches the launcher's pattern; nothing to pin")
    digests = {a["name"]: _digest(a) for a in selected}
    builder = _attesting_builder(tag, digests)
    if verify_download:
        with tempfile.TemporaryDirectory(prefix="vaf-pin-") as tmp:
            for a in selected:
                h = hashlib.sha256()
                with requests.get(a["browser_download_url"], stream=True, timeout=60) as r:
                    r.raise_for_status()
                    for chunk in r.iter_content(1 << 16):
                        h.update(chunk)
                if h.hexdigest() != digests[a["name"]]:
                    raise SystemExit(f"{a['name']}: downloaded bytes hash to {h.hexdigest()}, the API says {digests[a['name']]}")
                print(f"  downloaded and hashed: {a['name']}")
    manifest = {
        "tag": tag,
        "repository": LLAMA_REPO,
        "recorded": date.today().isoformat(),
        "provenance": (f"SLSA v1 statement by {builder} (repository id {LLAMA_REPO_ID}) lists every "
                       f"digest below; recorded by scripts/pin_llama_cpp.py from the release API"
                       + (", each asset downloaded and hashed locally" if verify_download else "")),
        "assets": {a["name"]: {"sha256": digests[a["name"]], "size": int(a["size"])} for a in selected},
    }
    PIN_PATH.write_bytes((json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    print(f"pinned {tag}: {len(selected)} assets -> {PIN_PATH.relative_to(ROOT).as_posix()}")
    for name in manifest["assets"]:
        print("  " + name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
