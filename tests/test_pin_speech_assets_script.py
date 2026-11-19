# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""scripts/pin_speech_assets.py records the Piper archives and the ERes2Net model from
their bytes (no digest is published for them): trust on first use. A later run that sees
other bytes for a file already pinned must refuse instead of recording them, unless the
release manager says so with --accept-changed-digest. Offline: the network steps are
replaced, the manifests point into tmp_path.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
H = {c: c * 64 for c in "abcdef"}


@pytest.fixture
def script(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("pin_speech_assets_under_test",
                                                  REPO / "scripts" / "pin_speech_assets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.speech_pin, "PIN_PATH", tmp_path / "speech_assets_pin.json")
    monkeypatch.setattr(mod, "CONTAINER_TABLE", tmp_path / "models.json")
    return mod


def _piper(sha, program):
    return {"repository": "rhasspy/piper", "tag": "2023.11.14-2", "provenance": "test",
            "assets": {"piper_linux_x86_64.tar.gz": {"sha256": sha, "size": 1, "program_sha256": program}}}


def _models(eres):
    return {"3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx": {"url": "https://x/e", "sha256": eres, "size": 1},
            "silero_vad.onnx": {"url": "https://x/s", "sha256": H["f"], "size": 1}}


def test_a_changed_digest_of_a_file_pinned_from_its_bytes_is_reported(script):
    old = {"piper": _piper(H["a"], H["b"]), "models": _models(H["c"])}
    assert script.changed_digests(old, _piper(H["a"], H["b"]), _models(H["c"])) == []
    assert len(script.changed_digests(old, _piper(H["d"], H["b"]), _models(H["c"]))) == 1
    assert len(script.changed_digests(old, _piper(H["a"], H["d"]), _models(H["c"]))) == 1
    assert len(script.changed_digests(old, _piper(H["a"], H["b"]), _models(H["d"]))) == 1
    # A new release tag, a first run and a file new to the manifest are not changes.
    moved = dict(_piper(H["d"], H["d"]), tag="2024.1.1")
    assert script.changed_digests(old, moved, _models(H["c"])) == []
    assert script.changed_digests({}, _piper(H["d"], H["d"]), _models(H["d"])) == []


def test_the_run_refuses_new_bytes_unless_told_to_accept_them(script, monkeypatch, tmp_path):
    """MUTATION: drop the refusal and the manifest is rewritten with the new digest."""
    voices = {"repository": "rhasspy/piper-voices", "revision": "0" * 40,
              "files": {"de/de_DE/thorsten/high/de_DE-thorsten-high.onnx": {"sha256": H["e"], "size": 1},
                        "de/de_DE/thorsten/high/de_DE-thorsten-high.onnx.json": {"sha256": H["e"], "size": 1}}}
    committed = {"recorded": "2026-01-01", "piper": _piper(H["a"], H["b"]), "piper_voices": voices,
                 "models": _models(H["c"])}
    script.speech_pin.PIN_PATH.write_text(json.dumps(committed), encoding="utf-8")
    monkeypatch.setattr(script, "_revision", lambda repo, rev: "0" * 40)
    monkeypatch.setattr(script, "pin_voices", lambda work, rev: (voices, {"models": {}}))
    monkeypatch.setattr(script, "pin_models", lambda work, rev: _models(H["c"]))
    monkeypatch.setattr(script, "pin_piper", lambda work: _piper(H["d"], H["b"]))
    with pytest.raises(SystemExit, match="accept-changed-digest"):
        script.main([])
    assert json.loads(script.speech_pin.PIN_PATH.read_text(encoding="utf-8")) == committed
    assert not script.CONTAINER_TABLE.exists()
    assert script.main(["--accept-changed-digest"]) == 0
    written = json.loads(script.speech_pin.PIN_PATH.read_text(encoding="utf-8"))
    assert written["piper"]["assets"]["piper_linux_x86_64.tar.gz"]["sha256"] == H["d"]
