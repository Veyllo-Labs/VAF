# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Pin the llama.cpp build a VAF release ships with, digests included.

    venv/bin/python scripts/pin_llama_cpp.py b10955

Writes ``vaf/core/llama_server_pin.json``: the tag, and for every release asset the
launcher can pick (``backend.LLAMA_ASSET_PATTERN``) its SHA-256 and size. The launcher
downloads only what is listed here and refuses bytes that hash differently, so a release
asset replaced after the pin, a truncated transfer or a poisoned mirror never runs.

Where the digests come from, and what is checked before they are written down:

1. The GitHub release API reports a ``digest`` per asset (GitHub hashes the upload).
2. llama.cpp's release workflow attests its assets (SLSA v1 provenance, stored by GitHub
   under /attestations/<digest>). The statement is fetched for the first asset and must
   name the workflow ``.github/workflows/release.yml`` of ``ggml-org/llama.cpp`` as the
   builder and list EVERY selected asset with the same digest the API reports. A digest
   the builder did not attest is not pinned.

This is a structural cross-check of the provenance statement, not a verification of its
Sigstore signature; for that, run ``gh attestation verify --repo ggml-org/llama.cpp
<downloaded asset>`` on a machine with the GitHub CLI. ``--download`` additionally pulls
every selected asset and hashes it locally against the API digest (about 1.7 GB).

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


def _get(url: str):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def _digest(asset: dict) -> str:
    d = str(asset.get("digest") or "")
    if not d.startswith("sha256:"):
        raise SystemExit(f"{asset.get('name')}: the API reports no sha256 digest ({d!r}); refusing to pin")
    return d.split(":", 1)[1].lower()


def _attested_digests(first_digest: str) -> tuple[dict, str]:
    """(name -> sha256 of every subject in the provenance statement, builder workflow)."""
    data = _get(f"{API}/repos/{LLAMA_REPO}/attestations/sha256:{first_digest}")
    atts = data.get("attestations") or []
    if not atts:
        raise SystemExit("no provenance attestation for the release assets; refusing to pin")
    att = atts[0]
    if int(att.get("repository_id") or 0) != LLAMA_REPO_ID:
        raise SystemExit(f"attestation belongs to repository {att.get('repository_id')}, not {LLAMA_REPO_ID}")
    payload = ((att.get("bundle") or {}).get("dsseEnvelope") or {}).get("payload")
    statement = json.loads(base64.b64decode(payload))
    build = (statement.get("predicate") or {}).get("buildDefinition") or {}
    wf = (build.get("externalParameters") or {}).get("workflow") or {}
    builder = f"{wf.get('repository')} {wf.get('path')}"
    if wf.get("repository") != f"https://github.com/{LLAMA_REPO}" or not str(wf.get("path", "")).endswith("release.yml"):
        raise SystemExit(f"provenance names an unexpected builder: {builder}")
    subjects = {s["name"]: (s.get("digest") or {}).get("sha256", "").lower() for s in statement.get("subject") or []}
    return subjects, builder


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
    subjects, builder = _attested_digests(next(iter(digests.values())))
    missing = [n for n, d in digests.items() if subjects.get(n) != d]
    if missing:
        raise SystemExit(f"the provenance statement does not attest these assets with the API's digest: {missing}")
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
