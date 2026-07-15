# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Speaker-identification core contracts (vaf/core/speaker_id.py).

Logic-level tests run with numpy only (no sherpa-onnx, no model files, no
network): classification tiers, WAV decoding, profile store round-trip with
per-scope isolation, enrollment session bookkeeping with mocked embeddings,
and the fail-closed gates. Model-dependent paths are exercised separately in
the live test (sherpa-onnx is deliberately NOT a packaged dependency yet).
"""
import io
import wave

import numpy as np
import pytest

import vaf.core.speaker_id as sid


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Profiles under tmp; config gates ON with default thresholds."""
    monkeypatch.setattr(sid, "_profiles_root", lambda: tmp_path / "profiles")
    from vaf.core.config import Config
    cfg = {"speaker_id_enabled": True, "speaker_id_threshold": 0.60, "speaker_id_band": 0.05}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d=None: cfg.get(k, d)))
    sid.enroll_abort("scope-a")
    sid.enroll_abort("scope-b")
    yield


def _wav_bytes(samples: np.ndarray, rate: int = 16000, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
        if channels > 1:
            pcm = np.repeat(pcm[:, None], channels, axis=1).reshape(-1)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Classification tiers
# ---------------------------------------------------------------------------

def test_classify_three_tiers():
    assert sid.classify(0.75) == "self"
    assert sid.classify(0.60) == "self"      # >= threshold
    assert sid.classify(0.58) == "unsure"    # inside the band
    assert sid.classify(0.55) == "unsure"    # band lower edge inclusive
    assert sid.classify(0.54) == "other"
    assert sid.classify(0.22) == "other"


def test_label_prefix():
    assert sid.label_prefix({"label": "self"}, "Mert") == "[Mert]: "
    assert sid.label_prefix({"label": "other"}) == "[anderer_Sprecher]: "
    assert sid.label_prefix({"label": "unsure"}) == "[unsicher]: "
    assert sid.label_prefix(None) == ""


# ---------------------------------------------------------------------------
# WAV decoding
# ---------------------------------------------------------------------------

def test_wav_decode_mono_16k():
    samples = np.sin(np.linspace(0, 100, 16000)).astype(np.float32) * 0.5
    out = sid.wav_bytes_to_samples(_wav_bytes(samples))
    assert out is not None and abs(len(out) - 16000) <= 1


def test_wav_decode_resamples_and_downmixes():
    samples = np.sin(np.linspace(0, 100, 48000)).astype(np.float32) * 0.5
    out = sid.wav_bytes_to_samples(_wav_bytes(samples, rate=48000, channels=2))
    assert out is not None
    assert abs(len(out) - 16000) <= 2  # 48k -> 16k


def test_wav_decode_garbage_returns_none():
    assert sid.wav_bytes_to_samples(b"not a wav at all") is None


# ---------------------------------------------------------------------------
# Profile store (per-scope isolation, explicit lifecycle)
# ---------------------------------------------------------------------------

def test_profile_roundtrip_and_scope_isolation():
    rng = np.random.default_rng(1)
    c = rng.standard_normal(512).astype(np.float32)
    c /= np.linalg.norm(c)
    meta = sid._save_profile("scope-a", "Mert", c, 26.4, 6)
    assert meta["display_name"] == "Mert"
    assert meta["net_speech_seconds"] == 26.4

    loaded = sid.load_profile("scope-a")
    assert loaded is not None
    assert np.allclose(loaded["centroid"], c, atol=1e-6)
    # another scope must NOT see this profile
    assert sid.load_profile("scope-b") is None

    assert sid.delete_profile("scope-a") is True
    assert sid.load_profile("scope-a") is None
    assert sid.delete_profile("scope-a") is False


# ---------------------------------------------------------------------------
# Enrollment session bookkeeping (embeddings mocked; no models needed)
# ---------------------------------------------------------------------------

def _mock_pipeline(monkeypatch, embedding, seg_seconds=4.0):
    monkeypatch.setattr(sid, "_concat_speech", lambda samples: (samples, seg_seconds))
    monkeypatch.setattr(sid, "_embed", lambda seg: embedding)


def test_enroll_rounds_accumulate_and_finalize(monkeypatch):
    rng = np.random.default_rng(2)
    emb = rng.standard_normal(512).astype(np.float32)
    emb /= np.linalg.norm(emb)
    _mock_pipeline(monkeypatch, emb, seg_seconds=5.0)

    sid.enroll_start("scope-a")
    wav = _wav_bytes(np.zeros(16000, dtype=np.float32) + 0.01)
    for expected_rounds in (1, 2, 3, 4):
        r = sid.enroll_round("scope-a", wav)
        assert r["ok"] and r["quality"] == "ok"
        assert r["rounds"] == expected_rounds
    assert r["net_seconds"] == 20.0
    assert not r["done"]
    r = sid.enroll_round("scope-a", wav)
    assert r["done"] and r["net_seconds"] == 25.0 and r["confidence"] == "hoch"

    meta = sid.enroll_finalize("scope-a", "Mert")
    assert meta is not None and meta["rounds"] == 5
    prof = sid.load_profile("scope-a")
    assert prof is not None
    # centroid of identical embeddings is that embedding
    assert np.allclose(prof["centroid"], emb, atol=1e-5)
    # session is consumed
    assert sid.enroll_finalize("scope-a", "Mert") is None


def test_enroll_round_without_session():
    wav = _wav_bytes(np.zeros(16000, dtype=np.float32))
    r = sid.enroll_round("scope-a", wav)
    assert not r["ok"] and r["quality"] == "no_session"


def test_enroll_round_rejects_inconsistent_voice(monkeypatch):
    """A round answered by a DIFFERENT voice must not poison the profile."""
    rng = np.random.default_rng(3)
    a = rng.standard_normal(192).astype(np.float32); a /= np.linalg.norm(a)
    b = rng.standard_normal(192).astype(np.float32); b /= np.linalg.norm(b)
    monkeypatch.setattr(sid, "_concat_speech", lambda samples: (samples, 4.0))
    embs = iter([a, b, a])
    monkeypatch.setattr(sid, "_embed", lambda seg: next(embs))

    sid.enroll_start("scope-a")
    wav = _wav_bytes(np.zeros(16000, dtype=np.float32))
    r1 = sid.enroll_round("scope-a", wav)
    assert r1["ok"] and r1["rounds"] == 1
    r2 = sid.enroll_round("scope-a", wav)   # fremde Stimme
    assert not r2["ok"] and r2["quality"] == "inconsistent_voice"
    assert r2["rounds"] == 1                # zaehlt nicht
    r3 = sid.enroll_round("scope-a", wav)   # wieder die eigene Stimme
    assert r3["ok"] and r3["rounds"] == 2


def test_enroll_round_too_short():
    sid.enroll_start("scope-a")
    r = sid.enroll_round("scope-a", _wav_bytes(np.zeros(1000, dtype=np.float32)))
    assert not r["ok"] and r["quality"] == "too_short"


# ---------------------------------------------------------------------------
# Verification gates (fail-closed)
# ---------------------------------------------------------------------------

def test_score_wav_disabled_returns_none(monkeypatch):
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d=None: False if k == "speaker_id_enabled" else d))
    assert sid.score_wav(b"anything", "scope-a") is None


def test_score_wav_without_profile_returns_none():
    wav = _wav_bytes(np.zeros(16000, dtype=np.float32))
    assert sid.score_wav(wav, "scope-a") is None


def test_score_wav_with_mocked_pipeline(monkeypatch):
    rng = np.random.default_rng(4)
    emb = rng.standard_normal(512).astype(np.float32)
    emb /= np.linalg.norm(emb)
    sid._save_profile("scope-a", "Mert", emb, 26.0, 6)
    _mock_pipeline(monkeypatch, emb, seg_seconds=3.0)

    res = sid.score_wav(_wav_bytes(np.zeros(16000, dtype=np.float32)), "scope-a")
    assert res is not None
    assert res["label"] == "self" and res["score"] >= 0.99
    assert sid.label_prefix(res, "Mert") == "[Mert]: "


# ---------------------------------------------------------------------------
# Recognition-test feedback (threshold calibration only - never profiles)
# ---------------------------------------------------------------------------

def test_feedback_calibration_store_and_suggestion():
    scope = "scope-a"
    assert sid.feedback_stats(scope) == {"n": 0}
    sid.record_test_feedback(scope, 0.72, "self", "correct")
    sid.record_test_feedback(scope, 0.68, "self", "correct")
    sid.record_test_feedback(scope, 0.48, "other", "correct")
    stats = sid.record_test_feedback(scope, 0.52, "other", "correct")
    assert stats["n"] == 4 and stats["n_owner"] == 2 and stats["n_other"] == 2
    assert stats["owner_avg"] == pytest.approx(0.70)
    assert stats["other_avg"] == pytest.approx(0.50)
    assert stats["suggested_threshold"] == pytest.approx(0.60)
    # A wrong verdict maps the score to the OPPOSITE side: label self but the
    # user said wrong = someone else scored high
    stats = sid.record_test_feedback(scope, 0.66, "self", "wrong")
    assert stats["n_other"] == 3
    # 'unsure' verdicts are stored but ambiguous - no side assignment
    stats = sid.record_test_feedback(scope, 0.58, "unsure", "correct")
    assert stats["n"] == 6 and stats["n_owner"] == 2
    # Scope isolation: another scope sees nothing
    assert sid.feedback_stats("scope-b") == {"n": 0}


def test_feedback_owner_claim_resolves_ambiguous_unsure():
    """'Unsure' + wrong verdict is ambiguous UNTIL the user names the speaker:
    naming the OWNER ('that was me' - a false reject) counts as owner-side
    calibration data via was='owner'; naming a third party as other-side."""
    scope = "scope-a"
    stats = sid.record_test_feedback(scope, 0.58, "unsure", "wrong", was="owner")
    assert stats["n_owner"] == 1 and stats["n_other"] == 0
    assert stats["owner_avg"] == pytest.approx(0.58)
    stats = sid.record_test_feedback(scope, 0.57, "unsure", "wrong", was="other")
    assert stats["n_owner"] == 1 and stats["n_other"] == 1
    # Without `was`, unsure stays skipped (unchanged behavior)
    stats = sid.record_test_feedback(scope, 0.59, "unsure", "wrong")
    assert stats["n_owner"] == 1 and stats["n_other"] == 1


def test_feedback_suggestion_needs_both_sides_and_clamps():
    scope = "scope-b"
    sid.record_test_feedback(scope, 0.95, "self", "correct")
    stats = sid.record_test_feedback(scope, 0.93, "self", "correct")
    assert "suggested_threshold" not in stats  # only one side so far
    sid.record_test_feedback(scope, 0.90, "other", "correct")
    stats = sid.record_test_feedback(scope, 0.88, "other", "correct")
    assert stats["suggested_threshold"] == 0.75  # midpoint 0.915 clamped


def test_adaptive_owner_sample_blends_and_caps(monkeypatch, tmp_path):
    """Owner-approved adaptive learning: YES-confirmed segments sharpen the
    centroid with bounded drift (0.7 enrollment weight), FIFO cap, and a
    similarity floor; re-enrollment wipes the adaptive state."""
    import numpy as np
    base = np.zeros(8, dtype=np.float32); base[0] = 1.0
    sid._save_profile("s1", "Mert", base, 20.0, 3)

    near = np.zeros(8, dtype=np.float32); near[0] = 0.9; near[1] = 0.45
    monkeypatch.setattr(sid, "embed_wav",
                        lambda wav: {"embedding": near, "net_seconds": 2.0})
    assert sid.add_owner_sample("s1", b"wav") is True
    prof = sid.load_profile("s1")
    assert prof["meta"]["adaptive_samples"] == 1
    # Centroid moved toward the sample but the enrollment axis dominates.
    assert prof["centroid"][0] > abs(prof["centroid"][1]) > 0
    # Enrollment centroid was preserved pristine.
    assert (sid._profile_dir("s1") / "enroll_centroid.npy").exists()

    # Similarity floor: an absurd segment is rejected even on a Yes.
    far = np.zeros(8, dtype=np.float32); far[3] = 1.0
    monkeypatch.setattr(sid, "embed_wav",
                        lambda wav: {"embedding": far, "net_seconds": 2.0})
    assert sid.add_owner_sample("s1", b"wav") is False
    assert sid.load_profile("s1")["meta"]["adaptive_samples"] == 1

    # FIFO cap holds.
    monkeypatch.setattr(sid, "embed_wav",
                        lambda wav: {"embedding": near, "net_seconds": 2.0})
    for _ in range(15):
        assert sid.add_owner_sample("s1", b"wav") is True
    assert sid.load_profile("s1")["meta"]["adaptive_samples"] == sid._ADAPTIVE_MAX_SAMPLES

    # Fresh enrollment resets all adaptive state.
    sid._save_profile("s1", "Mert", base, 25.0, 3)
    d = sid._profile_dir("s1")
    assert not (d / "adaptive.npy").exists()
    assert not (d / "enroll_centroid.npy").exists()


def test_no_profile_no_adaptive_write(monkeypatch, tmp_path):
    assert sid.add_owner_sample("nobody", b"wav") is False


def test_suggested_threshold_has_security_floor(monkeypatch, tmp_path):
    """Mert's exact case: owner mean 0.61 vs others 0.15 - the naive midpoint
    (0.38) would accept untested similar voices; the suggestion is floored at
    owner_mean - 0.15 (delegation authority: false accept >> false reject)."""
    for s in (0.60, 0.62):
        sid.record_test_feedback("s9", s, "self", "correct")
    for s in (0.14, 0.16):
        sid.record_test_feedback("s9", s, "other", "correct")
    stats = sid.feedback_stats("s9")
    assert stats["suggested_threshold"] == 0.46  # max(0.38, 0.61-0.15)
