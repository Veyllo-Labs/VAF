# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Speaker-confirmation flow contracts (vaf/core/speaker_confirm.py) plus the
named-profile voice DB (speaker_id named profiles + match_embedding).

Pins the hard rules: the OWNER profile is never modified by any answer; a
named profile is only written from an explicit "no, that's NAME"; one pending
question per scope with cooldown; unparseable messages are NOT consumed (they
must reach the normal agent turn). No sherpa-onnx, no network - embeddings
are synthetic vectors, delivery is mocked.
"""
import json
import time

import numpy as np
import pytest

import vaf.core.speaker_confirm as sc
import vaf.core.speaker_id as sid


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(sid, "_profiles_root", lambda: tmp_path / "profiles")
    monkeypatch.setattr(sc, "_store_dir", lambda: tmp_path / "pending")
    (tmp_path / "pending").mkdir(parents=True, exist_ok=True)
    from vaf.core.config import Config
    cfg = {"speaker_id_enabled": True, "speaker_id_threshold": 0.60,
           "speaker_id_band": 0.05, "speaker_id_confirmation_enabled": True,
           "default_language": "de"}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d=None: cfg.get(k, d)))
    # Never touch the real ~/.vaf user identities from tests
    import vaf.auth.user_workspace as uw

    class _NoIdentity:
        def get_user_identity(self):
            return {}
    monkeypatch.setattr(uw, "get_user_workspace", lambda username: _NoIdentity())
    import vaf.core.session as session_mod
    monkeypatch.setattr(session_mod, "get_user_projects_root",
                        lambda scope: tmp_path / "projects" / str(scope)[:8])
    yield


def _unit(dim=8, hot=0):
    v = np.zeros(dim, dtype=np.float32)
    v[hot] = 1.0
    return v


# ---------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------

def test_parse_reply_matrix():
    assert sc.parse_reply("Ja") == ("yes", None)
    assert sc.parse_reply("ja klar, das war ich") == ("yes", None)
    assert sc.parse_reply("Yes") == ("yes", None)
    assert sc.parse_reply("Nein") == ("no", None)
    assert sc.parse_reply("nein, das ist Peter") == ("no", "Peter")
    assert sc.parse_reply("Nein das war Anna") == ("no", "Anna")
    assert sc.parse_reply("no, that's Peter") == ("no", "Peter")
    assert sc.parse_reply("[Voice message, transcribed]: ja") == ("yes", None)
    # NOT confirmation answers - must flow into the normal agent turn
    assert sc.parse_reply("Wie wird das Wetter morgen?") is None
    assert sc.parse_reply("") is None
    assert sc.parse_reply("x" * 200) is None
    assert sc.parse_reply("Jahrelang habe ich gewartet") is None  # 'Ja' word boundary


def test_parse_reply_word_boundary():
    """'Jasmin ist da' must not count as 'ja'."""
    assert sc.parse_reply("Jasmin ist da") is None
    assert sc.parse_reply("Neinhorn") is None


# ---------------------------------------------------------------------------
# Named-profile voice DB (speaker_id)
# ---------------------------------------------------------------------------

def test_named_profile_save_merge_and_match():
    scope = "scope-a"
    peter = _unit(hot=1)
    meta = sid.save_named_profile(scope, "Peter", peter, 2.0)
    assert meta["display_name"] == "Peter" and meta["samples"] == 1
    # Merge a second sample: still unit-normalized, samples counted
    meta2 = sid.save_named_profile(scope, "Peter", _unit(hot=1), 3.0)
    assert meta2["samples"] == 2
    assert meta2["net_speech_seconds"] == pytest.approx(5.0)
    profiles = sid.load_named_profiles(scope)
    assert len(profiles) == 1
    assert np.linalg.norm(profiles[0]["centroid"]) == pytest.approx(1.0, abs=1e-5)

    owner = _unit(hot=0)
    # A voice matching Peter: owner-cosine 0 -> "other" -> named match
    res = sid.match_embedding(peter, owner, profiles)
    assert res["label"] == "named" and res["name"] == "Peter"
    assert sid.label_prefix(res) == "[Peter]: "
    # The owner still wins as "self"
    assert sid.match_embedding(owner, owner, profiles)["label"] == "self"
    # An unknown third voice stays "other"
    res3 = sid.match_embedding(_unit(hot=2), owner, profiles)
    assert res3["label"] == "other" and "name" not in res3


def test_unsure_is_never_upgraded_to_named():
    """The unsure band must stay the confirmation trigger, even if a named
    profile would match - identity claims need the owner's confirmation."""
    owner = _unit(hot=0)
    v = (0.58 * owner + np.sqrt(1 - 0.58 ** 2) * _unit(hot=1)).astype(np.float32)
    profiles = [{"key": "peter", "meta": {"display_name": "Peter"}, "centroid": _unit(hot=1)}]
    res = sid.match_embedding(v, owner, profiles)
    assert res["label"] == "unsure"


def test_named_profiles_are_scope_isolated():
    sid.save_named_profile("scope-a", "Peter", _unit(hot=1), 2.0)
    assert sid.load_named_profiles("scope-b") == []
    assert sid.list_named_profiles("scope-a")[0]["display_name"] == "Peter"
    assert sid.delete_named_profile("scope-a", "Peter") is True
    assert sid.load_named_profiles("scope-a") == []


# ---------------------------------------------------------------------------
# Pending lifecycle
# ---------------------------------------------------------------------------

def _request(monkeypatch, scope="scope-a", messenger_ok=False):
    sent = {}
    def fake_send(scope_id, username, text, file_path=None, record=True):
        sent["text"], sent["file"] = text, file_path
        return (messenger_ok, "telegram" if messenger_ok else None)
    import vaf.core.messaging_connections as mc
    monkeypatch.setattr(mc, "send_to_main_messenger", fake_send)
    emitted = {}
    monkeypatch.setattr(sc, "_emit_web_card", lambda rec, q: emitted.update(rec=rec, q=q))
    rec = sc.maybe_request_confirmation(
        scope, "admin", b"\x00" * 400,
        {"label": "unsure", "score": 0.57}, session_id="s1")
    return rec, sent, emitted


def test_request_and_resolve_yes_never_touches_owner_profile(monkeypatch, tmp_path):
    # Enrolled owner profile on disk
    sid._save_profile("scope-a", "Mert", _unit(hot=0), 25.0, 5)
    before = (sid._profile_dir("scope-a") / "centroid.npy").read_bytes()

    rec, sent, emitted = _request(monkeypatch)
    assert rec is not None and rec["channel"] == "web"
    assert emitted["rec"]["id"] == rec["id"]     # web card, messenger not reachable
    assert sc.get_pending("scope-a") is not None

    res = sc.resolve("scope-a", "yes", confirm_id=rec["id"])
    assert res["ok"] and res["outcome"] == "self"
    assert sc.get_pending("scope-a") is None
    # HARD RULE: owner profile byte-identical after a "yes"
    assert (sid._profile_dir("scope-a") / "centroid.npy").read_bytes() == before
    # Segment audio cleaned up
    assert not any((tmp_path / "projects").rglob("*.wav"))


def test_resolve_no_with_name_creates_named_profile(monkeypatch):
    rec, _, _ = _request(monkeypatch)
    monkeypatch.setattr(sid, "embed_wav",
                        lambda wav: {"embedding": _unit(hot=3), "net_seconds": 2.5})
    res = sc.resolve("scope-a", "no", "Peter", confirm_id=rec["id"])
    assert res["ok"] and res["outcome"] == "named"
    profs = sid.load_named_profiles("scope-a")
    assert len(profs) == 1 and profs[0]["meta"]["display_name"] == "Peter"


def test_one_pending_and_cooldown(monkeypatch):
    rec1, _, _ = _request(monkeypatch)
    assert rec1 is not None
    # Second unsure while pending -> no new question
    assert _request(monkeypatch)[0] is None
    sc.resolve("scope-a", "no", confirm_id=rec1["id"])
    # Cooldown active right after -> still no new question
    assert _request(monkeypatch)[0] is None


def test_messenger_channel_preferred(monkeypatch):
    rec, sent, emitted = _request(monkeypatch, messenger_ok=True)
    assert rec["channel"] == "telegram"
    assert "Score" in sent["text"] or "score" in sent["text"]
    assert sent["file"] and sent["file"].endswith(".wav")
    assert not emitted  # no web card when the messenger delivered


def test_channel_reply_consumption(monkeypatch):
    rec, _, _ = _request(monkeypatch)
    # Unrelated message: NOT consumed
    assert sc.try_consume_channel_reply("scope-a", "Wie ist das Wetter?") is None
    assert sc.get_pending("scope-a") is not None
    # Wrong scope: NOT consumed
    assert sc.try_consume_channel_reply("scope-b", "Ja") is None
    # Owner answers on the channel
    ack = sc.try_consume_channel_reply("scope-a", "Ja")
    assert ack and "unveraendert" in ack
    assert sc.get_pending("scope-a") is None


def test_question_language_from_user_identity(monkeypatch):
    """Live bug: the Telegram question was always English although the user's
    identity says preferred_language 'de' (the global config key is unset)."""
    import vaf.auth.user_workspace as uw

    class _WS:
        def __init__(self, pref):
            self._pref = pref
        def get_user_identity(self):
            return {"preferred_language": self._pref}

    monkeypatch.setattr(uw, "get_user_workspace", lambda username: _WS("de"))
    # Config 'language' unset (the _isolated fixture cfg has 'de', so drop it)
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, k, d=None: {"speaker_id_enabled": True,
                                "speaker_id_confirmation_enabled": True}.get(k, d)))
    assert sc._lang("Mert") == "de"
    monkeypatch.setattr(uw, "get_user_workspace", lambda username: _WS("tr"))
    assert sc._lang("Mert") == "tr"  # raw code - the VOCAB BOOK resolves it
    assert sc._lang(None) == "en"    # no identity, config unset -> en
    # Vocab gap: languages without an entry fall back to the English phrasing
    from vaf.core import vocab
    assert vocab.pick("speaker_confirm_yes", "tr") == vocab.pick("speaker_confirm_yes", "en")
    assert vocab.pick("speaker_confirm_yes", "de") != vocab.pick("speaker_confirm_yes", "en")


def test_expired_pending_resolves_gracefully(monkeypatch):
    rec, _, _ = _request(monkeypatch)
    p = sc._pending_path("scope-a")
    old = json.loads(p.read_text())
    old["created_at"] = time.time() - 2 * sc._PENDING_TTL_SECONDS
    p.write_text(json.dumps(old))
    res = sc.resolve("scope-a", "yes")
    assert res["ok"] is False and res["outcome"] == "expired"
