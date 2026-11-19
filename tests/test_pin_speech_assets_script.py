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


def _piper(sha, program, tag="2023.11.14-2"):
    return {"repository": "rhasspy/piper", "tag": tag, "provenance": "test",
            "assets": {"piper_linux_x86_64.tar.gz": {"sha256": sha, "size": 1, "program_sha256": program}}}


def _models(eres, silero=H["f"]):
    return {"3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx": {"url": "https://x/e", "sha256": eres, "size": 1},
            "silero_vad.onnx": {"url": "https://x/s", "sha256": silero, "size": 1}}


VOICE = "de/de_DE/thorsten/high/de_DE-thorsten-high.onnx"


def _voices(sha, revision="0" * 40):
    return {"repository": "rhasspy/piper-voices", "revision": revision,
            "files": {VOICE: {"sha256": sha, "size": 1}, VOICE + ".json": {"sha256": H["e"], "size": 1}}}


def _manifest(piper_sha=H["a"], program=H["b"], eres=H["c"], silero=H["f"], voice=H["e"], revision="0" * 40,
              tag="2023.11.14-2"):
    return {"recorded": "2026-01-01", "piper": _piper(piper_sha, program, tag),
            "piper_voices": _voices(voice, revision), "models": _models(eres, silero)}


def test_a_changed_digest_at_the_same_address_is_reported_for_every_kind_of_file(script):
    """Any file whose URL is unchanged and whose digest moved, whichever source the digest
    came from: a Piper archive, only its program, the ERes2Net model, Silero (GitHub's own
    digest), a voice. MUTATIONS: compare only a hand-kept list of names (Silero and the
    voice slip through), or ignore the program digest: red."""
    old = _manifest()
    assert script.changed_digests(old, _manifest()) == []
    for changed in (dict(piper_sha=H["d"]), dict(program=H["d"]), dict(eres=H["d"]), dict(silero=H["d"]),
                    dict(voice=H["d"])):
        assert len(script.changed_digests(old, _manifest(**changed))) == 1, changed
    # A new address (another tag or revision), a first run and a file new to the manifest
    # are not changes.
    assert script.changed_digests(old, _manifest(piper_sha=H["d"], program=H["d"], tag="2024.1.1")) == []
    assert script.changed_digests(old, _manifest(voice=H["d"], revision="1" * 40)) == []
    assert script.changed_digests({}, _manifest(piper_sha=H["d"])) == []


def test_the_refusal_labels_each_digest_it_lists(script):
    """The listing names what each digest is of (the archive, the program inside it, a
    file) and shows only what moved, without Python tuple brackets. MUTATION: list the
    raw digest tuples again: red."""
    old = _manifest()
    [line] = script.changed_digests(old, _manifest(program=H["d"]))
    assert line.endswith(f": program {H['b']} -> {H['d']}"), line
    assert "archive" not in line and "(" not in line and "'" not in line, line
    [line] = script.changed_digests(old, _manifest(piper_sha=H["d"], program=H["d"]))
    assert f"archive {H['a']} -> {H['d']}; program {H['b']} -> {H['d']}" in line, line
    [line] = script.changed_digests(old, _manifest(silero=H["d"]))
    assert line == f"https://x/s: file {H['f']} -> {H['d']}", line


def _fake_run(script, monkeypatch, new: dict):
    monkeypatch.setattr(script, "_revision", lambda repo, rev: "0" * 40)
    monkeypatch.setattr(script, "pin_voices", lambda work, rev: (new["piper_voices"], {"models": {}}))
    monkeypatch.setattr(script, "pin_models", lambda work, rev: new["models"])
    monkeypatch.setattr(script, "pin_piper", lambda work: new["piper"])


def test_the_run_refuses_new_bytes_unless_told_to_accept_them(script, monkeypatch, tmp_path):
    """MUTATION: drop the refusal and the manifest is rewritten with the new digest."""
    committed = _manifest()
    script.speech_pin.PIN_PATH.write_text(json.dumps(committed), encoding="utf-8")
    _fake_run(script, monkeypatch, _manifest(silero=H["d"]))
    with pytest.raises(SystemExit, match="accept-changed-digest"):
        script.main([])
    assert json.loads(script.speech_pin.PIN_PATH.read_text(encoding="utf-8")) == committed
    assert not script.CONTAINER_TABLE.exists()
    assert script.main(["--accept-changed-digest"]) == 0
    written = json.loads(script.speech_pin.PIN_PATH.read_text(encoding="utf-8"))
    assert written["models"]["silero_vad.onnx"]["sha256"] == H["d"]


def test_a_committed_manifest_that_cannot_be_read_stops_the_run(script, monkeypatch, tmp_path):
    """A truncated manifest read as "none yet" would accept every changed digest without
    the flag. MUTATION: treat an unreadable manifest as a first run: red."""
    script.speech_pin.PIN_PATH.write_text('{"piper": {"tag": "2023.11', encoding="utf-8")
    _fake_run(script, monkeypatch, _manifest(piper_sha=H["d"]))
    with pytest.raises(SystemExit, match="cannot be read"):
        script.main([])
    assert script.speech_pin.PIN_PATH.read_text(encoding="utf-8") == '{"piper": {"tag": "2023.11'
    script.speech_pin.PIN_PATH.unlink()
    assert script.main([]) == 0, "no manifest at all is a first run"
