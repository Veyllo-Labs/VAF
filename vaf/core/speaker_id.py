# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Speaker identification ("Mert vs. anderer_Sprecher") - enroll-and-verify.

Local lane on sherpa-onnx (Apache-2.0, onnxruntime-only, no torch): Silero VAD
segments the audio, a 3D-Speaker ERes2Net model (Apache-2.0) produces 192-dim
embeddings, and cosine similarity against the user's enrolled centroid decides
the label. Models are plain ungated downloads (GitHub releases) following the
Piper auto-download precedent in speech.py.

Embeddings are computed over the CONCATENATED speech of a round/utterance,
never averaged from short segments: measured on identical clips, per-segment
scores of 1-2s snippets do not separate speakers at all, while one embedding
over 5-20s of concatenated speech separated same/other by ~0.5 cosine
(ERes2Net: same 0.92 vs other 0.32/0.44; CAM++ was unreliable and is not used).

Rules (from the RAG memory-safety charter, vaf/memory/rag.py):
- process-wide singletons behind a lock, num_threads=1, CPU provider default;
- gated behind config ``speaker_id_enabled`` (default OFF, fail-closed);
- every public function catches everything and returns a safe default;
- profiles are stored PER user_scope_id (user-isolation invariant), and are
  only ever written by explicit enrollment - never from live conversations
  (anti-spoofing rule: confirmations re-label segments, they never touch the
  profile).

Labeling contract (three tiers around ``speaker_id_threshold`` t and
``speaker_id_band`` b): score >= t -> "self"; t-b <= score < t -> "unsure";
score < t-b -> "other".
"""
from __future__ import annotations

import json
import logging
import threading
import time
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
_MIN_SEGMENT_SECONDS = 1.0     # embeddings below ~1s are unreliable
_ENROLL_TARGET_SECONDS = 25.0  # net speech target for a confident profile
_ENROLL_MAX_ROUNDS = 15

_VAD_MODEL = "silero_vad.onnx"
_EMBED_MODEL = "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx"
_MODEL_URLS = {
    _VAD_MODEL: "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
    _EMBED_MODEL: "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx",
}
# A new round must not diverge wildly from the session's running mean:
# below this cosine the round is rejected as "inconsistent_voice" (someone
# else answered) and does not count. Deliberately loose - same-speaker
# concat embeddings sit far above it (~0.9), other voices far below (~0.4).
_ROUND_CONSISTENCY_MIN = 0.55
_MIN_ROUND_SPEECH_SECONDS = 2.0

_engine_lock = threading.Lock()
_extractor = None
_enroll_sessions: Dict[str, Dict] = {}
_enroll_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Config / paths
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    try:
        from vaf.core.config import Config
        return bool(Config.get("speaker_id_enabled", False))
    except Exception:
        return False


def _threshold() -> float:
    try:
        from vaf.core.config import Config
        return float(Config.get("speaker_id_threshold", 0.60))
    except Exception:
        return 0.60


def _band() -> float:
    try:
        from vaf.core.config import Config
        return float(Config.get("speaker_id_band", 0.05))
    except Exception:
        return 0.05


def _models_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "models" / "speaker"


def _profiles_root() -> Path:
    return Path.home() / ".vaf" / "speaker_profiles"


def _profile_dir(scope_id: str) -> Path:
    safe = "".join(c for c in str(scope_id) if c.isalnum() or c in "-_") or "default"
    return _profiles_root() / safe


# ---------------------------------------------------------------------------
# Engine (singleton, lazy, CPU)
# ---------------------------------------------------------------------------

def _ensure_models() -> bool:
    """Download missing model files (Piper precedent). Returns True when present."""
    try:
        import requests
        d = _models_dir()
        d.mkdir(parents=True, exist_ok=True)
        for name, url in _MODEL_URLS.items():
            path = d / name
            if path.exists() and path.stat().st_size > 10000:
                continue
            _log.info("speaker_id: downloading %s", name)
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                tmp = path.with_suffix(".part")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        f.write(chunk)
                tmp.replace(path)
        return all((d / n).exists() for n in _MODEL_URLS)
    except Exception as e:
        _log.warning("speaker_id: model download failed: %s", e)
        return False


def _get_extractor():
    """Process-wide embedding extractor singleton (double-checked locking)."""
    global _extractor
    if _extractor is not None:
        return _extractor
    with _engine_lock:
        if _extractor is not None:
            return _extractor
        if not _ensure_models():
            return None
        try:
            import sherpa_onnx
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(_models_dir() / _EMBED_MODEL),
                num_threads=1,
                provider="cpu",
            )
            _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
            _log.info("speaker_id: extractor loaded (dim=%d)", _extractor.dim)
        except Exception as e:
            _log.warning("speaker_id: extractor load failed: %s", e)
            _extractor = None
        return _extractor


def unload() -> None:
    """Free the model (mirrors unload_whisper_model)."""
    global _extractor
    with _engine_lock:
        _extractor = None
    import gc
    gc.collect()


def _new_vad():
    """A fresh VAD per request (holds per-stream state; the model file is mmapped)."""
    try:
        import sherpa_onnx
        cfg = sherpa_onnx.VadModelConfig(
            silero_vad=sherpa_onnx.SileroVadModelConfig(
                model=str(_models_dir() / _VAD_MODEL),
                threshold=0.5,
                min_silence_duration=0.4,
                min_speech_duration=0.25,
                max_speech_duration=15.0,
            ),
            sample_rate=SAMPLE_RATE,
            num_threads=1,
            provider="cpu",
        )
        return sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=180)
    except Exception as e:
        _log.warning("speaker_id: VAD init failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def wav_bytes_to_samples(wav_bytes: bytes):
    """16-bit PCM WAV bytes -> float32 mono samples at 16 kHz (or None)."""
    try:
        import io
        import wave
        import numpy as np

        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            channels = w.getnchannels()
            width = w.getsampwidth()
            rate = w.getframerate()
            frames = w.readframes(w.getnframes())
        if width != 2:
            _log.warning("speaker_id: unsupported sample width %d", width)
            return None
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        if rate != SAMPLE_RATE:
            n_out = int(len(samples) * SAMPLE_RATE / rate)
            samples = np.interp(
                np.linspace(0.0, len(samples) - 1, n_out),
                np.arange(len(samples)),
                samples,
            ).astype(np.float32)
        return samples
    except Exception as e:
        _log.warning("speaker_id: WAV decode failed: %s", e)
        return None


def _vad_segments(samples) -> List[Tuple[float, float, "object"]]:
    """Run VAD; return [(start_s, end_s, segment_samples)]."""
    vad = _new_vad()
    if vad is None:
        return []
    out = []
    window = 512
    i = 0
    while i < len(samples):
        vad.accept_waveform(samples[i:i + window])
        i += window
        while not vad.empty():
            seg = vad.front
            start = seg.start / SAMPLE_RATE
            out.append((start, start + len(seg.samples) / SAMPLE_RATE, seg.samples))
            vad.pop()
    vad.flush()
    while not vad.empty():
        seg = vad.front
        start = seg.start / SAMPLE_RATE
        out.append((start, start + len(seg.samples) / SAMPLE_RATE, seg.samples))
        vad.pop()
    return out


def _embed(segment_samples):
    import numpy as np

    ex = _get_extractor()
    if ex is None:
        return None
    stream = ex.create_stream()
    stream.accept_waveform(SAMPLE_RATE, segment_samples)
    stream.input_finished()
    if not ex.is_ready(stream):
        return None
    emb = np.array(ex.compute(stream), dtype=np.float32)
    norm = float(np.linalg.norm(emb))
    if norm == 0.0:
        return None
    return emb / norm


def cosine(a, b) -> float:
    import numpy as np
    return float(np.dot(a, b))  # both are L2-normalized


def classify(score: float) -> str:
    t = _threshold()
    b = _band()
    if score >= t:
        return "self"
    if score >= t - b:
        return "unsure"
    return "other"


# ---------------------------------------------------------------------------
# Profile store (per user_scope_id; explicit enrollment only)
# ---------------------------------------------------------------------------

def load_profile(scope_id: str) -> Optional[Dict]:
    """Return {'meta': dict, 'centroid': ndarray} or None."""
    try:
        import numpy as np
        d = _profile_dir(scope_id)
        meta_path = d / "profile.json"
        emb_path = d / "centroid.npy"
        if not meta_path.exists() or not emb_path.exists():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        centroid = np.load(emb_path)
        return {"meta": meta, "centroid": centroid}
    except Exception as e:
        _log.warning("speaker_id: profile load failed: %s", e)
        return None


def delete_profile(scope_id: str) -> bool:
    try:
        d = _profile_dir(scope_id)
        removed = False
        for name in ("profile.json", "centroid.npy",
                     "enroll_centroid.npy", "adaptive.npy"):
            p = d / name
            if p.exists():
                p.unlink()
                removed = True
        return removed
    except Exception as e:
        _log.warning("speaker_id: profile delete failed: %s", e)
        return False


def _save_profile(scope_id: str, display_name: str, centroid, net_seconds: float, rounds: int) -> Dict:
    import numpy as np
    d = _profile_dir(scope_id)
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "display_name": display_name,
        "created_at": time.strftime("%Y-%m-%d"),
        "net_speech_seconds": round(net_seconds, 1),
        "rounds": rounds,
        "embedding_model": _EMBED_MODEL,
        "dim": int(centroid.shape[0]),
    }
    np.save(d / "centroid.npy", centroid.astype(np.float32))
    # Fresh enrollment resets ALL adaptive state (the pristine-centroid copy
    # and confirmed samples belong to the previous enrollment).
    for _stale in ("enroll_centroid.npy", "adaptive.npy"):
        try:
            (d / _stale).unlink(missing_ok=True)
        except Exception:
            pass
    (d / "profile.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        import os
        os.chmod(d / "profile.json", 0o600)
        os.chmod(d / "centroid.npy", 0o600)
    except Exception:
        pass
    return meta


# ---------------------------------------------------------------------------
# Enrollment (guided rounds; in-memory session until finalize)
# ---------------------------------------------------------------------------

# Spoken lines of the guided call, per language. The agent talks in the USER'S
# language (config "language" first, then the UI locale) - the visual UI keeps
# its own locale. Served by enroll_start so the client never hardcodes them
# (and a later iteration can generate questions via the LLM instead).
# The scripted lines live in the vocabulary book (vaf/core/vocab, keys
# speaker_enroll_*) - new languages are added THERE, this module only selects.
def _enroll_lines(lang: str) -> Dict:
    from vaf.core import vocab
    return {
        "intro1": vocab.pick("speaker_enroll_intro1", lang),
        "intro2": vocab.pick("speaker_enroll_intro2", lang),
        "questions": vocab.phrasings("speaker_enroll_questions", lang),
        "done": vocab.pick("speaker_enroll_done", lang),
        "q_inconsistent": vocab.pick("speaker_enroll_q_inconsistent", lang),
        "q_no_speech": vocab.pick("speaker_enroll_q_no_speech", lang),
        "q_too_short": vocab.pick("speaker_enroll_q_too_short", lang),
        "q_error": vocab.pick("speaker_enroll_q_error", lang),
    }


def _enroll_lang(ui_lang: str) -> str:
    try:
        from vaf.core import vocab
        available = set(vocab.available_languages("speaker_enroll_questions"))
        from vaf.core.config import Config
        cfg_lang = (Config.get("language", "auto") or "auto")[:2].lower()
        if cfg_lang in available:
            return cfg_lang
        ui = (ui_lang or "")[:2].lower()
        return ui if ui in available else "en"
    except Exception:
        return "en"


_ADAPTIVE_MAX_SAMPLES = 10        # FIFO cap: old adaptive samples age out
_ADAPTIVE_MIN_SIMILARITY = 0.30   # a confirmed segment WILDLY unlike the
                                  # profile is rejected even on a Yes (guards
                                  # against misclicks and noise segments)
_ADAPTIVE_ENROLL_WEIGHT = 0.7     # centroid = 0.7*enrollment + 0.3*adaptive
                                  # mean: bounded drift - confirmations can
                                  # sharpen the profile, never replace it


def add_owner_sample(scope_id: str, wav_bytes: bytes) -> bool:
    """OWNER-CONFIRMED adaptive learning (user decision 2026-07-15).

    When the owner answers YES to "was that your voice?" over an
    AUTHENTICATED channel (web session / main messenger), the confirmed
    segment is added to the profile as an adaptive sample and the centroid
    is re-blended. The voice itself still cannot modify anything: only the
    owner's authenticated answer triggers this, embed_wav enforces the
    minimum-speech gate, a similarity floor rejects absurd segments, and the
    enrollment centroid keeps 70% weight (kept separately in
    enroll_centroid.npy, so drift is bounded and re-enrollment resets all
    adaptive state).
    """
    try:
        import numpy as np
        profile = load_profile(scope_id)
        if profile is None:
            return False
        got = embed_wav(wav_bytes)
        if got is None:
            return False
        emb = np.asarray(got["embedding"], dtype=np.float32)
        emb = emb / (np.linalg.norm(emb) or 1.0)
        d = _profile_dir(scope_id)
        enroll_path = d / "enroll_centroid.npy"
        if not enroll_path.exists():
            # First adaptive add: preserve the pristine enrollment centroid.
            np.save(enroll_path, np.asarray(profile["centroid"], dtype=np.float32))
        enroll_centroid = np.load(enroll_path)
        enroll_centroid = enroll_centroid / (np.linalg.norm(enroll_centroid) or 1.0)
        if float(np.dot(emb, enroll_centroid)) < _ADAPTIVE_MIN_SIMILARITY:
            _log.info("speaker_id: adaptive sample rejected (similarity floor)")
            return False
        adaptive_path = d / "adaptive.npy"
        samples = []
        if adaptive_path.exists():
            arr = np.load(adaptive_path)
            samples = [arr[i] for i in range(arr.shape[0])]
        samples.append(emb)
        samples = samples[-_ADAPTIVE_MAX_SAMPLES:]
        stacked = np.stack(samples).astype(np.float32)
        np.save(adaptive_path, stacked)
        adaptive_mean = stacked.mean(axis=0)
        adaptive_mean = adaptive_mean / (np.linalg.norm(adaptive_mean) or 1.0)
        centroid = (_ADAPTIVE_ENROLL_WEIGHT * enroll_centroid
                    + (1.0 - _ADAPTIVE_ENROLL_WEIGHT) * adaptive_mean)
        centroid = (centroid / (np.linalg.norm(centroid) or 1.0)).astype(np.float32)
        np.save(d / "centroid.npy", centroid)
        meta = dict(profile["meta"])
        meta["adaptive_samples"] = len(samples)
        meta["adaptive_updated_at"] = time.strftime("%Y-%m-%d")
        (d / "profile.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(adaptive_path, 0o600)
            os.chmod(enroll_path, 0o600)
        except Exception:
            pass
        _log.info("speaker_id: owner profile sharpened (adaptive sample %d/%d)",
                  len(samples), _ADAPTIVE_MAX_SAMPLES)
        return True
    except Exception as e:
        _log.warning("speaker_id: add_owner_sample failed: %s", e)
        return False


def enroll_start(scope_id: str, ui_lang: str = "en") -> Dict:
    with _enroll_lock:
        _enroll_sessions[scope_id] = {
            "embeddings": [],
            "net_seconds": 0.0,
            "rounds": 0,
            "started": time.time(),
        }
    lang = _enroll_lang(ui_lang)
    return {
        "target_seconds": _ENROLL_TARGET_SECONDS,
        "max_rounds": _ENROLL_MAX_ROUNDS,
        "lang": lang,
        "lines": _enroll_lines(lang),
    }


def _concat_speech(samples):
    """VAD, then concatenate all speech samples. Returns (speech, net_seconds)."""
    import numpy as np
    segments = _vad_segments(samples)
    if not segments:
        return None, 0.0
    speech = np.concatenate([seg for _s, _e, seg in segments])
    return speech, len(speech) / SAMPLE_RATE


def enroll_round(scope_id: str, wav_bytes: bytes) -> Dict:
    """Process one guided-dialog answer. Returns progress + quality verdict.

    ONE embedding over the round's concatenated speech (short-segment
    embeddings do not separate speakers). Quality checks: enough net speech,
    and consistency against the session's running mean - a round answered by
    a different voice is rejected ('inconsistent_voice') and does not count.
    """
    result = {
        "ok": False, "quality": "error", "gained_seconds": 0.0,
        "net_seconds": 0.0, "rounds": 0, "confidence": "niedrig", "done": False,
    }
    try:
        with _enroll_lock:
            sess = _enroll_sessions.get(scope_id)
        if sess is None:
            result["quality"] = "no_session"
            return result

        samples = wav_bytes_to_samples(wav_bytes)
        if samples is None or len(samples) < SAMPLE_RATE // 2:
            result["quality"] = "too_short"
            result.update(_progress(sess))
            return result

        speech, gained = _concat_speech(samples)
        if speech is None or gained < 0.25:
            result["quality"] = "no_speech"
            result.update(_progress(sess))
            return result
        if gained < _MIN_ROUND_SPEECH_SECONDS:
            result["quality"] = "too_short"
            result.update(_progress(sess))
            return result

        emb = _embed(speech)
        if emb is None:
            result["quality"] = "no_speech"
            result.update(_progress(sess))
            return result

        with _enroll_lock:
            sess = _enroll_sessions.get(scope_id)
            if sess is None:
                result["quality"] = "no_session"
                return result
            if sess["embeddings"]:
                import numpy as np
                mean = np.stack(sess["embeddings"]).mean(axis=0)
                mean = mean / float(np.linalg.norm(mean))
                if cosine(emb, mean) < _ROUND_CONSISTENCY_MIN:
                    result["quality"] = "inconsistent_voice"
                    result.update(_progress(sess))
                    return result
            sess["embeddings"].append(emb)
            sess["net_seconds"] += gained
            sess["rounds"] += 1
            snapshot = dict(sess)

        result["ok"] = True
        result["quality"] = "ok"
        result["gained_seconds"] = round(gained, 1)
        result.update(_progress(snapshot))
        result["done"] = (snapshot["net_seconds"] >= _ENROLL_TARGET_SECONDS
                          or snapshot["rounds"] >= _ENROLL_MAX_ROUNDS)
        return result
    except Exception as e:
        _log.warning("speaker_id: enroll_round failed: %s", e)
        return result


def _progress(sess: Dict) -> Dict:
    net = sess.get("net_seconds", 0.0)
    conf = ("hoch" if net >= _ENROLL_TARGET_SECONDS else
            "gut" if net >= 16 else "mittel" if net >= 8 else "niedrig")
    return {
        "net_seconds": round(net, 1),
        "rounds": sess.get("rounds", 0),
        "confidence": conf,
        "target_seconds": _ENROLL_TARGET_SECONDS,
        "max_rounds": _ENROLL_MAX_ROUNDS,
    }


def enroll_finalize(scope_id: str, display_name: str) -> Optional[Dict]:
    """Average the round embeddings into the profile centroid and persist."""
    try:
        import numpy as np
        with _enroll_lock:
            sess = _enroll_sessions.pop(scope_id, None)
        if not sess or not sess["embeddings"]:
            return None
        stack = np.stack(sess["embeddings"])
        centroid = stack.mean(axis=0)
        norm = float(np.linalg.norm(centroid))
        if norm == 0.0:
            return None
        centroid = centroid / norm
        return _save_profile(scope_id, display_name, centroid,
                             sess["net_seconds"], sess["rounds"])
    except Exception as e:
        _log.warning("speaker_id: enroll_finalize failed: %s", e)
        return None


def enroll_abort(scope_id: str) -> None:
    with _enroll_lock:
        _enroll_sessions.pop(scope_id, None)


# ---------------------------------------------------------------------------
# Named third-party profiles (the per-user voice DB beyond the owner)
#
# Created ONLY from a confirmation answer like "no, that's Peter" given by the
# verified owner over their authenticated channel. Layout: <scope>/others/
# <safe_name>.json + .npy. The OWNER profile (profile.json/centroid.npy) is
# never touched by any of this - hard anti-spoofing rule.
# ---------------------------------------------------------------------------

def safe_profile_name(name: str) -> str:
    """File-key form of a spoken name: alnum/-_ only, lowercase, max 32."""
    cleaned = "".join(c for c in (name or "").strip() if c.isalnum() or c in "-_")
    return cleaned[:32].lower() or "unbekannt"


def _others_dir(scope_id: str):
    return _profile_dir(scope_id) / "others"


def save_named_profile(scope_id: str, name: str, embedding, net_seconds: float) -> Optional[Dict]:
    """Create or MERGE a named third-party profile from one confirmed segment.

    Merge = weighted mean over `samples` (renormalized), so each additional
    confirmed segment sharpens the centroid. Returns the meta dict or None.
    """
    try:
        import numpy as np
        key = safe_profile_name(name)
        d = _others_dir(scope_id)
        d.mkdir(parents=True, exist_ok=True)
        emb = np.asarray(embedding, dtype=np.float32)
        meta_path, emb_path = d / f"{key}.json", d / f"{key}.npy"
        samples_n, total_net = 1, float(net_seconds)
        centroid = emb
        if meta_path.exists() and emb_path.exists():
            old_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            old = np.load(emb_path).astype(np.float32)
            n = max(1, int(old_meta.get("samples", 1)))
            merged = (old * n + emb) / (n + 1)
            norm = float(np.linalg.norm(merged))
            if norm > 0:
                centroid = (merged / norm).astype(np.float32)
            samples_n = n + 1
            total_net = float(old_meta.get("net_speech_seconds", 0.0)) + float(net_seconds)
        meta = {
            "display_name": (name or "").strip()[:48] or key,
            "created_at": time.strftime("%Y-%m-%d"),
            "net_speech_seconds": round(total_net, 1),
            "samples": samples_n,
            "embedding_model": _EMBED_MODEL,
            "dim": int(centroid.shape[0]),
        }
        np.save(emb_path, centroid)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            import os
            os.chmod(meta_path, 0o600)
            os.chmod(emb_path, 0o600)
        except Exception:
            pass
        return meta
    except Exception as e:
        _log.warning("speaker_id: save_named_profile failed: %s", e)
        return None


def load_named_profiles(scope_id: str) -> List[Dict]:
    """[{'key', 'meta', 'centroid'}] for every named third-party profile."""
    out: List[Dict] = []
    try:
        import numpy as np
        d = _others_dir(scope_id)
        if not d.is_dir():
            return out
        for meta_path in sorted(d.glob("*.json")):
            emb_path = meta_path.with_suffix(".npy")
            if not emb_path.exists():
                continue
            try:
                out.append({
                    "key": meta_path.stem,
                    "meta": json.loads(meta_path.read_text(encoding="utf-8")),
                    "centroid": np.load(emb_path),
                })
            except Exception:
                continue
    except Exception as e:
        _log.warning("speaker_id: load_named_profiles failed: %s", e)
    return out


def list_named_profiles(scope_id: str) -> List[Dict]:
    """Metas only (for settings/UI listings)."""
    return [{"key": p["key"], **p["meta"]} for p in load_named_profiles(scope_id)]


def delete_named_profile(scope_id: str, name: str) -> bool:
    try:
        key = safe_profile_name(name)
        removed = False
        for suffix in (".json", ".npy"):
            p = _others_dir(scope_id) / f"{key}{suffix}"
            if p.exists():
                p.unlink()
                removed = True
        return removed
    except Exception as e:
        _log.warning("speaker_id: delete_named_profile failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Recognition-test feedback (threshold calibration ONLY - never profiles)
#
# The Settings "test recognition" view records a snippet, shows who was
# detected and lets the user judge the verdict. Those judgments feed a
# per-scope calibration store from which a threshold suggestion is derived.
# HARD RULE: this data never modifies any voice profile.
# ---------------------------------------------------------------------------

def _feedback_path(scope_id: str):
    return _profile_dir(scope_id) / "feedback.json"


def record_test_feedback(scope_id: str, score: float, label: str, verdict: str,
                         was: Optional[str] = None) -> Dict:
    """Store one test verdict ('correct'|'wrong') and return feedback_stats.

    `was` ("owner"|"other") resolves cases the (label, verdict) pair alone
    cannot: e.g. an "unsure" result where the user then NAMES the speaker -
    naming the owner means "that was me" (a false reject, owner-side data),
    naming a third party means other-side data.
    """
    try:
        import os
        p = _feedback_path(scope_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        items = []
        if p.exists():
            items = json.loads(p.read_text(encoding="utf-8"))
        item = {"score": float(score), "label": str(label),
                "verdict": str(verdict), "at": time.strftime("%Y-%m-%d %H:%M")}
        if was in ("owner", "other"):
            item["was"] = was
        items.append(item)
        items = items[-100:]
        p.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
    except Exception as e:
        _log.warning("speaker_id: feedback store failed: %s", e)
    return feedback_stats(scope_id)


def feedback_stats(scope_id: str) -> Dict:
    """Owner/non-owner score averages from UNambiguous verdicts plus a
    suggested threshold (midpoint, clamped to 0.35-0.75).

    Unambiguous mapping: (self, correct) and (other/named, wrong) were the
    OWNER's voice; (other/named, correct) and (self, wrong) were someone
    else. 'unsure' verdicts are stored but ambiguous, so they are skipped.
    A suggestion needs at least 2 samples on each side.
    """
    try:
        p = _feedback_path(scope_id)
        if not p.exists():
            return {"n": 0}
        items = json.loads(p.read_text(encoding="utf-8"))
        # An explicit `was` wins; otherwise derive the side from (label, verdict)
        owner = [i["score"] for i in items
                 if i.get("was") == "owner"
                 or (not i.get("was")
                     and ((i["label"] == "self" and i["verdict"] == "correct")
                          or (i["label"] in ("other", "named") and i["verdict"] == "wrong")))]
        others = [i["score"] for i in items
                  if i.get("was") == "other"
                  or (not i.get("was")
                      and ((i["label"] in ("other", "named") and i["verdict"] == "correct")
                           or (i["label"] == "self" and i["verdict"] == "wrong")))]
        stats: Dict = {"n": len(items), "n_owner": len(owner), "n_other": len(others)}
        if owner:
            stats["owner_avg"] = round(sum(owner) / len(owner), 3)
        if others:
            stats["other_avg"] = round(sum(others) / len(others), 3)
        if len(owner) >= 2 and len(others) >= 2:
            owner_mean = sum(owner) / len(owner)
            other_mean = sum(others) / len(others)
            mid = (owner_mean + other_mean) / 2
            # Security-aware floor (user question 2026-07-15: "wouldn't the
            # midpoint let voices through that sound LESS like me?"): the
            # midpoint only separates the SAMPLED impostors - an untested
            # similar voice could land between it and the owner's range, and
            # the threshold gates DELEGATION AUTHORITY (false accept >>
            # false reject). Never suggest below owner_mean - 0.15: enough
            # slack to stop the owner's everyday false rejects, but the gate
            # stays anchored to the owner's own score range instead of
            # drifting toward whoever happened to be tested.
            mid = max(mid, owner_mean - 0.15)
            stats["suggested_threshold"] = round(min(0.75, max(0.35, mid)), 2)
        return stats
    except Exception as e:
        _log.warning("speaker_id: feedback stats failed: %s", e)
        return {"n": 0}


# ---------------------------------------------------------------------------
# Verification / labeling
# ---------------------------------------------------------------------------

def embed_wav(wav_bytes: bytes) -> Optional[Dict]:
    """One embedding over the clip's concatenated speech.

    Returns {'embedding': ndarray, 'net_seconds': float} or None. Used by the
    confirmation flow to turn a stored segment into a named profile.
    """
    try:
        samples = wav_bytes_to_samples(wav_bytes)
        if samples is None:
            return None
        speech, net = _concat_speech(samples)
        if speech is None or net < _MIN_SEGMENT_SECONDS:
            return None
        emb = _embed(speech)
        if emb is None:
            return None
        return {"embedding": emb, "net_seconds": round(net, 1)}
    except Exception as e:
        _log.warning("speaker_id: embed_wav failed: %s", e)
        return None


def match_embedding(embedding, owner_centroid, named_profiles: List[Dict]) -> Dict:
    """Pure decision: owner verification first, then named identification.

    Owner cosine decides self/unsure/other; an "other" is then compared
    against every named profile and becomes {'label': 'named', 'name': ...}
    when the best named cosine clears the same threshold. "unsure" is NEVER
    upgraded to a name - it stays the confirmation trigger.
    """
    score = cosine(embedding, owner_centroid)
    label = classify(score)
    result = {"score": round(score, 3), "label": label}
    if label == "other" and named_profiles:
        best_name, best_score = None, -1.0
        for p in named_profiles:
            s = cosine(embedding, p["centroid"])
            if s > best_score:
                best_name, best_score = p["meta"].get("display_name") or p["key"], s
        if best_name is not None and best_score >= _threshold():
            result["label"] = "named"
            result["name"] = best_name
            result["named_score"] = round(best_score, 3)
    return result


def score_wav(wav_bytes: bytes, scope_id: str) -> Optional[Dict]:
    """Whole-utterance verification: one score for the full clip.

    Returns {'score', 'label', 'net_seconds'[, 'name']} or None (feature off,
    no profile, no speech, or any error) - callers degrade to unlabeled text.
    Label 'named' means: not the owner, but a known named third party.
    """
    try:
        if not is_enabled():
            return None
        profile = load_profile(scope_id)
        if profile is None:
            return None
        got = embed_wav(wav_bytes)
        if got is None:
            return None
        result = match_embedding(got["embedding"], profile["centroid"],
                                 load_named_profiles(scope_id))
        result["net_seconds"] = got["net_seconds"]
        return result
    except Exception as e:
        _log.warning("speaker_id: score_wav failed: %s", e)
        return None


def analyze_segments(wav_bytes: bytes, scope_id: str) -> Optional[List[Dict]]:
    """Per-segment scores for the 'Erkennung testen' debug view."""
    try:
        if not is_enabled():
            return None
        profile = load_profile(scope_id)
        if profile is None:
            return None
        samples = wav_bytes_to_samples(wav_bytes)
        if samples is None:
            return None
        out = []
        for start, end, seg_samples in _vad_segments(samples):
            if (end - start) < _MIN_SEGMENT_SECONDS:
                continue
            emb = _embed(seg_samples)
            if emb is None:
                continue
            score = cosine(emb, profile["centroid"])
            out.append({
                "start": round(start, 2), "end": round(end, 2),
                "score": round(score, 3), "label": classify(score),
            })
        return out or None
    except Exception as e:
        _log.warning("speaker_id: analyze_segments failed: %s", e)
        return None


def label_prefix(score_result: Optional[Dict], display_name: str = "Mert") -> str:
    """Transcript prefix for the LLM, e.g. '[Mert]: ' / '[anderer_Sprecher]: '."""
    if not score_result:
        return ""
    label = score_result.get("label")
    if label == "self":
        return f"[{display_name}]: "
    if label == "named":
        return f"[{score_result.get('name') or 'anderer_Sprecher'}]: "
    if label == "other":
        return "[anderer_Sprecher]: "
    return "[unsicher]: "
