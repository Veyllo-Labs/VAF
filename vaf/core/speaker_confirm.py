# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Speaker-confirmation flow: ask the user when a voice lands in the unsure band.

When speaker verification scores an utterance as "unsure", this manager asks
the OWNER to confirm - via their main messenger (question text + the audio
segment attached, through the channel-agnostic ``send_to_main_messenger``)
when one is configured, else via a card in the web chat (audio player +
yes/no buttons + optional name field). Answers:

- "yes"                 -> the segment was the owner. The enrolled profile is
                           NEVER updated from a confirmation (anti-spoofing).
- "no"                  -> another speaker; nothing is stored.
- "no, that's Peter"    -> the segment embedding is stored as (or merged
                           into) the NAMED third-party profile "Peter" in the
                           per-user voice DB (speaker_id.save_named_profile).

Rules: at most ONE pending confirmation per user scope, a cooldown between
questions, pending state persisted per scope (survives restarts), segment
audio stored under the user's own VAF_Projects tree (served by /api/file
with ownership enforcement). Authorization comes from the transport: replies
are only consumed from the owner's authenticated channel or web session.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Dict, Optional, Tuple

_log = logging.getLogger(__name__)

_COOLDOWN_SECONDS = 10 * 60       # min gap between two confirmation questions
_PENDING_TTL_SECONDS = 60 * 60    # unanswered questions expire after an hour

# Question and acknowledgment texts live in the vocabulary book
# (vaf/core/vocab, keys speaker_confirm_*) - new languages are added THERE.

_YES_RE = re.compile(r"^\s*(ja+|jo|jep|jup|yes|yep|yeah|si)\b", re.I)
_NO_RE = re.compile(r"^\s*(nein+|ne+|noe|nö|no+|nope)\b", re.I)
# "nein, das ist Peter" / "no that's Peter" / "nein das war Peter" ...
_NAME_RE = re.compile(
    r"\b(?:(?:das|es|that|it)\s+(?:ist|war|is|was)|that'?s|it'?s)\s+(?:der|die|the\s+)?"
    r"([A-Za-zÄÖÜäöüß][\wÄÖÜäöüß-]{1,31})",
    re.I,
)
_VOICE_PREFIX_RE = re.compile(r"^\s*\[[^\]]*transcribed[^\]]*\]:\s*", re.I)


def _lang(username: Optional[str] = None) -> str:
    """The USER's language code, via the vocabulary book's shared resolver
    (identity preferred_language -> config default_language -> en). Returned
    RAW - vocab.pick resolves it and falls back per phrase."""
    from vaf.core.vocab import resolve_user_language
    return resolve_user_language(username=username)


def is_enabled() -> bool:
    try:
        from vaf.core.config import Config
        from vaf.core import speaker_id
        v = Config.get("speaker_id_confirmation_enabled", True)
        v = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
        return v and speaker_id.is_enabled()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Pending store: one JSON per scope under ~/.vaf/speaker_confirm/
# ---------------------------------------------------------------------------

def _store_dir() -> Path:
    from vaf.core.platform import Platform
    d = Platform.vaf_dir() / "speaker_confirm"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pending_path(scope_id: str) -> Path:
    safe = "".join(c for c in str(scope_id) if c.isalnum() or c in "-_") or "default"
    return _store_dir() / f"{safe}.json"


def get_pending(scope_id: str) -> Optional[Dict]:
    """The live pending confirmation for this scope, or None (expired = None)."""
    try:
        p = _pending_path(scope_id)
        if not p.exists():
            return None
        rec = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - float(rec.get("created_at", 0)) > _PENDING_TTL_SECONDS:
            _cleanup_record(rec)
            p.unlink(missing_ok=True)
            return None
        return rec
    except Exception:
        return None


def _write_pending(scope_id: str, rec: Dict) -> None:
    p = _pending_path(scope_id)
    p.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    try:
        import os
        os.chmod(p, 0o600)
    except Exception:
        pass


def _pop_pending(scope_id: str, confirm_id: Optional[str] = None) -> Optional[Dict]:
    rec = get_pending(scope_id)
    if rec is None:
        return None
    if confirm_id and rec.get("id") != confirm_id:
        return None
    _pending_path(scope_id).unlink(missing_ok=True)
    return rec


def _cleanup_record(rec: Dict) -> None:
    try:
        audio = rec.get("audio_path")
        if audio:
            Path(audio).unlink(missing_ok=True)
    except Exception:
        pass


def _last_asked_path(scope_id: str) -> Path:
    return _pending_path(scope_id).with_suffix(".last")


def _cooldown_active(scope_id: str) -> bool:
    try:
        p = _last_asked_path(scope_id)
        return p.exists() and (time.time() - p.stat().st_mtime) < _COOLDOWN_SECONDS
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Trigger: called from the scoring sites after an "unsure" result
# ---------------------------------------------------------------------------

def maybe_request_confirmation(
    scope_id: str,
    username: str,
    wav_bytes: bytes,
    score_result: Dict,
    session_id: str = "",
) -> Optional[Dict]:
    """Ask the owner to confirm an unsure segment. Never raises.

    Returns the pending record if a question went out, else None (feature
    off, wrong label, already pending, cooldown, storage/delivery failure).
    Delivery: main messenger first (text + audio attachment), else a
    user-scoped web card event - never a global broadcast.
    """
    try:
        if not is_enabled() or not scope_id:
            return None
        if (score_result or {}).get("label") != "unsure":
            return None
        if get_pending(scope_id) is not None or _cooldown_active(scope_id):
            return None

        from vaf.core.session import get_user_projects_root
        root = get_user_projects_root(scope_id)
        if root is None:
            return None
        seg_dir = root / "voice_confirm"
        seg_dir.mkdir(parents=True, exist_ok=True)
        confirm_id = uuid.uuid4().hex[:12]
        audio_path = seg_dir / f"segment_{confirm_id}.wav"
        audio_path.write_bytes(wav_bytes)
        try:
            import os
            os.chmod(audio_path, 0o600)
        except Exception:
            pass

        from vaf.core import vocab
        question = vocab.pick("speaker_confirm_question", _lang(username),
                              score=score_result.get("score", "?"))
        rec = {
            "id": confirm_id,
            "scope_id": str(scope_id),
            "username": username or "admin",
            "audio_path": str(audio_path),
            "score": score_result.get("score"),
            "session_id": session_id or "",
            "created_at": time.time(),
            "channel": "web",
        }

        # Channel routing: main messenger first (resolved at send time by
        # send_to_main_messenger), web card as the documented fallback lane.
        sent, channel = False, None
        try:
            from vaf.core.messaging_connections import send_to_main_messenger
            sent, channel = send_to_main_messenger(
                str(scope_id), rec["username"], question,
                file_path=str(audio_path), record=False,
            )
        except Exception as e:
            _log.warning("speaker_confirm: messenger delivery failed: %s", e)
        if sent and channel:
            rec["channel"] = channel
        else:
            _emit_web_card(rec, question)

        _write_pending(str(scope_id), rec)
        _last_asked_path(str(scope_id)).touch()
        _log.info("speaker_confirm: question sent (scope=%s channel=%s id=%s)",
                  str(scope_id)[:8], rec["channel"], confirm_id)
        return rec
    except Exception as e:
        _log.warning("speaker_confirm: maybe_request_confirmation failed: %s", e)
        return None


def _emit_web_card(rec: Dict, question: str) -> None:
    """User-scoped web card event (emit-site scoped, fail-closed)."""
    try:
        from vaf.core.web_interface import get_web_interface
        if not rec.get("scope_id"):
            return
        get_web_interface().push_update_to_user(rec["scope_id"], {
            "type": "speaker_confirm_pending",
            "confirmId": rec["id"],
            "question": question,
            "audioPath": rec["audio_path"],
            "score": rec.get("score"),
        })
    except Exception as e:
        _log.warning("speaker_confirm: web card emit failed: %s", e)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def parse_reply(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """Parse a free-text answer into ('yes'|'no', name|None), or None.

    Only clearly confirmation-shaped messages are consumed - anything else
    stays a normal chat turn. A name is only honored on a 'no'.
    """
    t = _VOICE_PREFIX_RE.sub("", (text or "").strip())
    if not t or len(t) > 120:
        return None
    if _YES_RE.match(t):
        return ("yes", None)
    if _NO_RE.match(t):
        m = _NAME_RE.search(t)
        return ("no", m.group(1).strip() if m else None)
    return None


def resolve(scope_id: str, answer: str, name: Optional[str] = None,
            confirm_id: Optional[str] = None) -> Dict:
    """Apply an owner's answer. Returns {'ok', 'outcome', 'ack'[, 'name']}.

    'yes' relabels the segment AND (user decision 2026-07-15,
    speaker_id_adaptive_enabled, default on) feeds it into the owner profile
    as an adaptive sample via speaker_id.add_owner_sample - the answer
    arrives over an AUTHENTICATED owner channel (web session / main
    messenger), so this is owner-approved learning, not voice-approved:
    the voice itself still cannot modify anything, and quality gates plus
    the enrollment-weighted blend bound the drift. 'no' + name stores/merges
    the segment as a named third-party profile via speaker_id.
    """
    from vaf.core import vocab
    rec = _pop_pending(scope_id, confirm_id)
    if rec is None:
        return {"ok": False, "outcome": "expired",
                "ack": vocab.pick("speaker_confirm_expired", _lang())}
    lang = _lang(rec.get("username"))
    try:
        if answer == "yes":
            outcome, ack = "self", vocab.pick("speaker_confirm_yes", lang)
            try:
                from vaf.core.config import Config
                if Config.get("speaker_id_adaptive_enabled", True):
                    from vaf.core import speaker_id
                    wav = Path(rec["audio_path"]).read_bytes()
                    speaker_id.add_owner_sample(scope_id, wav)
            except Exception as _ae:
                _log.warning("speaker_confirm: adaptive learn failed: %s", _ae)
        elif name:
            from vaf.core import speaker_id
            meta = None
            try:
                wav = Path(rec["audio_path"]).read_bytes()
                got = speaker_id.embed_wav(wav)
                if got is not None:
                    meta = speaker_id.save_named_profile(
                        scope_id, name, got["embedding"], got["net_seconds"])
            except Exception as e:
                _log.warning("speaker_confirm: named save failed: %s", e)
            if meta is not None:
                outcome, ack = "named", vocab.pick("speaker_confirm_named", lang,
                                                   name=meta["display_name"])
            else:
                outcome, ack = "other", vocab.pick("speaker_confirm_named_failed", lang)
        else:
            outcome, ack = "other", vocab.pick("speaker_confirm_no", lang)
        result = {"ok": True, "outcome": outcome, "ack": ack}
        if outcome == "named":
            result["name"] = name
        _log.info("speaker_confirm: resolved (scope=%s outcome=%s)",
                  str(scope_id)[:8], outcome)
        return result
    finally:
        _cleanup_record(rec)


def try_consume_channel_reply(scope_id: str, text: str) -> Optional[str]:
    """Messenger-side consumption: if a confirmation is pending for this scope
    and the text parses as an answer, resolve it and return the ack text to
    send back on the channel. Returns None when the message is NOT an answer
    (it must then flow into the normal agent turn). Never raises.
    """
    try:
        if not scope_id or get_pending(scope_id) is None:
            return None
        parsed = parse_reply(text)
        if parsed is None:
            return None
        answer, name = parsed
        return resolve(scope_id, answer, name).get("ack")
    except Exception as e:
        _log.warning("speaker_confirm: channel reply consumption failed: %s", e)
        return None
