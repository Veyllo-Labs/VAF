# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""scripts/pin_speech_assets.py records the Piper archives (and any model without a
published digest) from their bytes: trust on first use. A later run that sees
other bytes for a file already pinned must refuse instead of recording them, unless the
release manager says so with --accept-changed-digest; a voice's MODEL_CARD counts as such a
file. A voice is pinned only with a reviewed licence entry, and a voice of the computer's
table only when the pinned Piper can read its settings. Offline: the network steps are
replaced, the manifests point into tmp_path.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import random
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
    return {"3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx": {"url": "https://x/e", "sha256": eres, "size": 1,
                                                                  "licence": "Apache-2.0",
                                                                  "licence_source": "https://x/l"},
            "silero_vad.onnx": {"url": "https://x/s", "sha256": silero, "size": 1, "licence": "MIT",
                                "licence_source": "https://x/l"}}


VOICE = "de/de_DE/mls/medium/de_DE-mls-medium.onnx"
CARD = "de/de_DE/mls/medium/MODEL_CARD"


def _voices(sha, revision="0" * 40, card=H["a"]):
    return {"repository": "rhasspy/piper-voices", "revision": revision,
            "files": {VOICE: {"sha256": sha, "size": 1}, VOICE + ".json": {"sha256": H["e"], "size": 1}},
            "voices": {"de_DE-mls-medium": {"licence": "CC-BY-4.0", "dataset": "https://x/d", "base": "scratch",
                                            "card": {"path": CARD, "sha256": card, "size": 1},
                                            "host_piper": True}}}


def _manifest(piper_sha=H["a"], program=H["b"], eres=H["c"], silero=H["f"], voice=H["e"], revision="0" * 40,
              tag="2023.11.14-2", card=H["a"]):
    return {"recorded": "2026-01-01", "piper": _piper(piper_sha, program, tag),
            "piper_voices": _voices(voice, revision, card), "models": _models(eres, silero)}


def test_a_changed_digest_at_the_same_address_is_reported_for_every_kind_of_file(script):
    """Any file whose URL is unchanged and whose digest moved, whichever source the digest
    came from: a Piper archive, only its program, the ERes2Net model, Silero (GitHub's own
    digest), a voice. MUTATIONS: compare only a hand-kept list of names (Silero and the
    voice slip through), or ignore the program digest: red."""
    old = _manifest()
    assert script.changed_digests(old, _manifest()) == []
    for changed in (dict(piper_sha=H["d"]), dict(program=H["d"]), dict(eres=H["d"]), dict(silero=H["d"]),
                    dict(voice=H["d"]), dict(card=H["d"])):
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


def test_a_changed_card_is_refused(script):
    """A MODEL_CARD that changed at the same address may now carry another licence: it is
    refused like any changed digest, labeled as a card. MUTATION: drop the cards from
    _digests_by_url: red."""
    [line] = script.changed_digests(_manifest(), _manifest(card=H["d"]))
    assert line == (f"https://huggingface.co/rhasspy/piper-voices/resolve/{'0' * 40}/{CARD}: "
                    f"card {H['a']} -> {H['d']}"), line


# ── which voices may be pinned ──────────────────────────────────────────────────

def _cfg(**over) -> dict:
    """A voice's settings as Piper 1.0 wrote them: espeak, one-codepoint phonemes."""
    config = {"phoneme_type": "espeak", "phoneme_id_map": {"_": [0], "a": [1], "ɪ": [2], "ˈ": [3]},
              "inference": {"noise_scale": 0.667}}
    config.update(over)
    return config


@pytest.mark.parametrize("config,loads", [
    (_cfg(), True),
    (_cfg(phoneme_type="text"), True),
    ({"phoneme_id_map": {"a": [1]}}, True),
    (_cfg(phoneme_map={"a": ["b", "c"]}), True),
    (_cfg(inference={"phoneme_silence": {".": 0.2}}), True),
    (_cfg(phoneme_id_map={"a": [1], "aɪ": [2]}), False),
    (_cfg(phoneme_map={"a": ["aɪ"]}), False),
    (_cfg(phoneme_map={"aɪ": ["a"]}), False),
    (_cfg(inference={"phoneme_silence": {"aɪ": 0.2}}), False),
    (_cfg(phoneme_type="pinyin"), False),
    (_cfg(phoneme_type="japanese"), False),
    (_cfg(phoneme_id_map={"": [1]}), False),
    (_cfg(phoneme_id_map={"\ud800": [1]}), False),
    (_cfg(phoneme_id_map={"e\u0301": [1]}), False),
    (_cfg(phoneme_map={"a": "b"}), False),
    (_cfg(phoneme_id_map=["a"]), False),
    (_cfg(phoneme_id_map={"a": ["bad"]}), False),
    (_cfg(phoneme_id_map={"a": [1.5]}), False),
    (_cfg(phoneme_id_map={"a": [True]}), False),
    (_cfg(phoneme_id_map={"a": 1}), False),
    (_cfg(inference={"phoneme_silence": {".": "long"}}), False),
    (_cfg(inference={"phoneme_silence": {".": 1}}), True),
], ids=["espeak", "text", "no-type", "map-ok", "silence-ok", "vowel-cluster-key", "map-value", "map-key",
        "silence-key", "pinyin", "japanese", "empty-key", "lone-surrogate", "combining-mark", "map-value-not-list",
        "id-map-not-object", "id-not-a-number", "fractional-id", "bool-id", "ids-not-a-list", "silence-not-a-number",
        "silence-int"])
def test_host_loadability_mirrors_piper_cpp(script, config, loads):
    """The 2023 Piper program aborts on a phoneme of more than one codepoint (in the id map,
    the phoneme map keys and values, the silence map) and reads every phoneme type but text
    as espeak; it converts each id to an integer and each silence to a float, which throws
    for a string. MUTATIONS: drop the phoneme_map value check (red: map-value), accept
    pinyin (red: pinyin), drop the id value check (red: id-not-a-number), drop the silence
    value check (red: silence-not-a-number)."""
    assert script.host_piper_can_load(config) is loads


def _oracle_one_codepoint(text: str) -> bool:
    """Independent of the code under test: utf8::distance over the UTF-8 bytes counts the
    bytes that are not continuation bytes (10xxxxxx); a string that cannot be encoded
    strictly (a lone surrogate) makes utf8cpp throw."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    raw = text.encode("utf-8", "surrogatepass")
    return sum(1 for b in raw if b & 0xC0 != 0x80) == 1


def _oracle(config: dict) -> bool:
    if config.get("phoneme_type", "espeak") not in ("espeak", "text"):
        return False
    keys = list(config.get("phoneme_id_map", {})) + list(config.get("inference", {}).get("phoneme_silence", {}))
    for key, targets in config.get("phoneme_map", {}).items():
        keys += [key, *targets]
    return all(_oracle_one_codepoint(k) for k in keys)


_POOLS = ["abcxyz_^$ .,", "ɪʊəɛɔæʃʒθðŋˈˌː", "\u0301\u0303\u0308", "中文汉字", "\U0001F600\U0001F680",
          "\ud800\udfff"]


def _draw_text(rng: random.Random) -> str:
    return "".join(rng.choice(rng.choice(_POOLS)) for _ in range(rng.choice((0, 1, 1, 1, 1, 2, 3))))


def _draw_config(rng: random.Random) -> dict:
    config = {"phoneme_id_map": {_draw_text(rng): [i] for i in range(rng.randint(0, 40))}}
    if rng.random() < 0.3:
        config["phoneme_type"] = rng.choice(("espeak", "text", "pinyin", "japanese"))
    if rng.random() < 0.4:
        config["phoneme_map"] = {_draw_text(rng): [_draw_text(rng) for _ in range(rng.randint(0, 3))]
                                 for _ in range(rng.randint(0, 40))}
    if rng.random() < 0.4:
        config["inference"] = {"phoneme_silence": {_draw_text(rng): 0.1 for _ in range(rng.randint(0, 40))}}
    return config


def test_the_host_check_agrees_with_an_independent_oracle_on_random_configs(script):
    """Fuzz: random settings drawn from ASCII, IPA, combining marks, CJK, emoji, empty
    strings and lone surrogates, 0 to 40 keys per map, against an oracle that counts UTF-8
    lead bytes. 300 seeded draws here; VAF_FUZZ_DRAWS runs more outside the suite."""
    rng = random.Random(413)
    draws = int(os.environ.get("VAF_FUZZ_DRAWS", "300"))
    mismatches = []
    for draw in range(draws):
        config = _draw_config(rng)
        if script.host_piper_can_load(config) != _oracle(config):
            mismatches.append((draw, config))
    assert mismatches == [], mismatches[:3]


def _pin_voices_offline(script, monkeypatch, tmp_path, configs: dict | None = None, container: dict | None = None,
                        host: dict | None = None):
    """pin_voices against a fake Hugging Face: every .onnx gets an LFS digest, every
    .onnx.json and MODEL_CARD is served (its git blob id published), the settings from
    `configs` by voice name (Piper 1.0 settings otherwise). Returns the network calls."""
    from vaf.core.speech import SpeechManager
    if host is not None:
        monkeypatch.setattr(SpeechManager, "PIPER_VOICES", host)
    container = container if container is not None else {
        "de": {"name": "German", "locale": "de_DE", "voice": "thorsten", "quality": "medium"}}
    monkeypatch.setattr(script, "_container_voices", lambda: container)
    configs = configs or {}

    def body(path: str) -> bytes:
        if path.endswith("/MODEL_CARD"):
            return f"# card of {path}\n".encode()
        name = path.rsplit("/", 1)[1][:-len(".onnx.json")]
        return json.dumps(configs.get(name, _cfg())).encode("utf-8")

    def paths_info(repo, revision, paths):
        out = {}
        for p in paths:
            if p.endswith(".onnx"):
                out[p] = {"path": p, "size": 7, "lfs": {"oid": "1" * 64, "size": 7}}
            else:
                out[p] = {"path": p, "oid": script._git_blob_id(body(p))}
        return out

    calls = []

    def download(url, dest):
        path = url.split("/resolve/", 1)[1].split("/", 1)[1]
        calls.append(path)
        data = body(path)
        Path(dest).write_bytes(data)
        return hashlib.sha256(data).hexdigest(), len(data)

    monkeypatch.setattr(script, "_paths_info", paths_info)
    monkeypatch.setattr(script, "_download_hash", download)
    # The served cards are the reviewed ones: each entry names the digest of the fake card.
    for name, entry in list(script.VOICE_LICENCES.items()):
        card = script._card_path(script.speech_pin.voice_path(name))
        monkeypatch.setitem(script.VOICE_LICENCES, name,
                            dict(entry, card_sha256=hashlib.sha256(body(card)).hexdigest()))
    return calls


def test_pinned_voices_carry_their_licence_card_and_host_flag(script, monkeypatch, tmp_path):
    """What the run writes: per voice its reviewed licence, dataset and base, its card by
    digest and whether the pinned Piper reads it; per container language the licence and
    the card's address at the revision; the retired languages."""
    _pin_voices_offline(script, monkeypatch, tmp_path, configs={"de_DE-thorsten-medium": _cfg(
        phoneme_id_map={"aɪ": [1]})}, host={"en": "en_US-ljspeech-high"})
    voices, table = script.pin_voices(tmp_path, "0" * 40)
    row = voices["voices"]["de_DE-thorsten-medium"]
    assert row["licence"] == "CC0-1.0" and row["base"] == "nl_BE-rdh-medium" and row["host_piper"] is False
    assert row["card"]["path"] == "de/de_DE/thorsten/medium/MODEL_CARD" and len(row["card"]["sha256"]) == 64
    assert voices["voices"]["en_US-ljspeech-high"]["host_piper"] is True
    assert voices["voices"]["en_US-ljspeech-high"]["licence"] == "LicenseRef-public-domain"
    assert table["retired"] == ["ja", "tr"]
    de = table["models"]["de"]
    assert de["licence"] == "CC0-1.0"
    assert de["licence_source"] == ("https://huggingface.co/rhasspy/piper-voices/raw/" + "0" * 40 +
                                    "/de/de_DE/thorsten/medium/MODEL_CARD")


def test_a_card_that_is_not_the_reviewed_one_stops_the_run(script, monkeypatch, tmp_path):
    """The licence entry was reviewed from one card; at any revision, a card with other
    bytes may name another licence or base, so the run stops and asks for a new review, and
    --accept-changed-digest does not get past it. MUTATION: drop the card comparison in
    pin_voices: red."""
    _pin_voices_offline(script, monkeypatch, tmp_path, host={"en": "en_US-ljspeech-high"})
    entry = script.VOICE_LICENCES["en_US-ljspeech-high"]
    monkeypatch.setitem(script.VOICE_LICENCES, "en_US-ljspeech-high", dict(entry, card_sha256="9" * 64))
    with pytest.raises(SystemExit, match="is not the card its licence entry was reviewed from"):
        script.pin_voices(tmp_path, "1" * 40)


def test_the_reviewed_card_digest_is_not_copied_into_the_manifest(script, monkeypatch, tmp_path):
    _pin_voices_offline(script, monkeypatch, tmp_path, host={"en": "en_US-ljspeech-high"})
    voices, _ = script.pin_voices(tmp_path, "0" * 40)
    row = voices["voices"]["en_US-ljspeech-high"]
    assert "card_sha256" not in row
    assert row["card"]["sha256"] == script.VOICE_LICENCES["en_US-ljspeech-high"]["card_sha256"]


def test_other_weights_for_a_pinned_voice_at_a_new_revision_are_refused(script, monkeypatch, tmp_path):
    """Two manifests at different revisions: every URL is new, so the comparison by URL
    sees nothing, but de_DE-mls-medium.onnx now has another digest (a retrain). The run
    stops unless the release manager accepts it; the same weights at a new revision, or a
    first run, are not a change. MUTATIONS: compare by URL only (drop the
    changed_voice_weights check in main): red."""
    old, new = _manifest(revision="0" * 40), _manifest(voice=H["d"], revision="1" * 40)
    assert script.changed_digests(old, new) == []
    [line] = script.changed_voice_weights(old, new)
    assert line == f"{VOICE}: {H['e']} at {'0' * 40} -> {H['d']} at {'1' * 40}", line
    assert script.changed_voice_weights(old, _manifest(revision="1" * 40)) == []
    assert script.changed_voice_weights({}, new) == []
    script.speech_pin.PIN_PATH.write_text(json.dumps(old), encoding="utf-8")
    _fake_run(script, monkeypatch, new)
    with pytest.raises(SystemExit, match="accept-changed-digest"):
        script.main([])
    assert json.loads(script.speech_pin.PIN_PATH.read_text(encoding="utf-8")) == old
    assert script.main(["--accept-changed-digest"]) == 0


def test_a_voice_without_a_curated_licence_stops_the_run(script, monkeypatch, tmp_path):
    """A voice nobody reviewed is not pinned, and the run stops before it asks the network.
    MUTATION: drop the curated-entry check: red."""
    calls = _pin_voices_offline(script, monkeypatch, tmp_path, host={"en": "en_US-lessac-high"})
    monkeypatch.setattr(script, "_paths_info", lambda *a: pytest.fail("the network was asked"))
    with pytest.raises(SystemExit, match="en_US-lessac-high has no licence entry"):
        script.pin_voices(tmp_path, "0" * 40)
    assert calls == []


@pytest.mark.parametrize("entry,why", [
    ({"licence": "CC-BY-NC-SA-4.0", "dataset": "https://x/d", "base": "scratch"}, "not in ALLOWED_WEIGHT_LICENCES"),
    ({"licence": "CC0-1.0", "dataset": "https://x/d", "base": "en_US-lessac-high"}, "base"),
    ({"licence": "CC0-1.0", "dataset": "http://x/d", "base": "scratch"}, "not an https URL"),
    ({"licence": "CC0-1.0", "dataset": "https://x/d", "base": "scratch"}, "SHA-256 of the card"),
], ids=["non-commercial", "lessac-base", "plain-http-dataset", "no-reviewed-card"])
def test_a_disallowed_curated_licence_stops_the_run(script, monkeypatch, tmp_path, entry, why):
    """MUTATION: accept any licence in _curated: red (non-commercial)."""
    _pin_voices_offline(script, monkeypatch, tmp_path, host={"en": "en_US-ljspeech-high"})
    monkeypatch.setitem(script.VOICE_LICENCES, "en_US-ljspeech-high", entry)
    with pytest.raises(SystemExit, match=why):
        script.pin_voices(tmp_path, "0" * 40)


def test_a_computer_voice_the_pinned_piper_cannot_read_stops_the_run(script, monkeypatch, tmp_path):
    """A voice in the computer's table whose settings the 2023 Piper aborts on is refused
    (a container voice may have them: piper-tts reads them). MUTATION: drop the host check
    in pin_voices: red."""
    _pin_voices_offline(script, monkeypatch, tmp_path, host={"fr": "fr_FR-siwis-medium"},
                        configs={"fr_FR-siwis-medium": _cfg(phoneme_id_map={"aɪ": [1]})})
    with pytest.raises(SystemExit, match="fr_FR-siwis-medium is in SpeechManager.PIPER_VOICES"):
        script.pin_voices(tmp_path, "0" * 40)


def test_a_retired_language_left_in_the_container_table_stops_the_run(script, monkeypatch, tmp_path):
    _pin_voices_offline(script, monkeypatch, tmp_path, container={
        "tr": {"name": "Turkish", "locale": "tr_TR", "voice": "dfki", "quality": "medium"}})
    with pytest.raises(SystemExit, match="retired languages"):
        script.pin_voices(tmp_path, "0" * 40)


def test_every_curated_entry_is_complete():
    """VOICE_LICENCES, the reviewed record the run copies into the manifest: every entry has
    an allowed licence, a clean base and an https dataset."""
    spec = importlib.util.spec_from_file_location("pin_speech_assets_entries", REPO / "scripts" / "pin_speech_assets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in mod.VOICE_LICENCES:
        assert mod._curated(name)
