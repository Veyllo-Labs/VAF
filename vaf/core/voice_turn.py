# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The voice-call turn pipeline, as an engine object (docs/agents/VOICE_AGENT.md).

For its whole life this pipeline was inline in ONE ~1100-line branch of the WebSocket
handler in ``web_server.py`` - untestable, and every change to voice behavior was
surgery inside that branch. This module is the pipeline as a sync object the handler
consumes: noise gate -> STT -> language follow -> speaker label + hysteresis ->
speaker recovery -> pending-answer resolution -> addressee clarification -> engage
gate + chime-in -> voice_reply -> delegate decision -> state writes. One call to
``turn()`` returns ONE :class:`TurnOutcome`; every mid-pipeline exit of the old
branch was terminal, so no mid-turn sends are needed.

What deliberately stays OUTSIDE, in the handler: the wire (ownership gate, base64,
send_json), TTS with its per-variant timeouts (the only thing ``asyncio.wait_for``
ever wrapped - keeping it out is what lets this object be plain sync, per the house
rule that no engine object is natively async), the TaskQueue enqueue (the one
external write, visible in the handler), the call-start lane-readiness work (tray
heartbeat, model loading), and the greeting TTS (its history append is
success-gated, hence :meth:`note_greeting`).

STATE: the engine does not own a private copy - ``self.state`` IS the per-call dict
stored in ``web_server._VOICE_CALLS``. That registry's name, key (``id(websocket)``)
and dict shape are a hard external contract (the tray's model-keepalive probe
intersects its keys with the live sockets; tests pin the teardown pops), so the
extraction shares the dict instead of replacing it.

Exported on the facade (``from vaf import VoiceTurnEngine, TurnOutcome``) since
2026-08-13. Deliberate: the deferral boundary ("no public surface until proven
need") had its acceptance criterion met from day one - the web handler is a thin
consumer of this exact object, which is the proof the surface suffices - and the
module level is pure stdlib, so the export costs the slim base nothing. The embedder
contract lives in docs/EMBEDDING.md ("Running a voice turn yourself") and
tests/contract/test_contract_voice_turn.py.

History entries are stored under ONE cap (_HISTORY_MAX_CHARS, applied centrally in
_history_add) - the number the consumer enforces anyway: voice_reply caps every
entry at 800 when building the prompt. The extraction itself preserved the old
inconsistent per-site caps; this normalization was a separate, deliberate change
with a regenerated baseline (tests/test_voice_call_baseline.py).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# ONE history-content cap, applied centrally in _history_add. The extraction had
# preserved the measured status quo (200 on early exits, 400 on model silence,
# UNCAPPED on the normal path, 800 in voice_call_speak - four rules, grown one
# incident at a time); this deliberate normalization uses the number the CONSUMER
# already enforces: voice_reply caps every history entry at 800 when it builds the
# prompt, so storing more was waste and storing less threw context away.
_HISTORY_MAX_CHARS = 800
_HISTORY_RING = 16          # the call keeps the last N history entries
_CHIME_RING = 6             # recent chime-ins kept for dedup
_RECHECK_PENDING_TTL_S = 30.0   # "did you mean me?" answer window
_RECHECK_COOLDOWN_S = 60.0      # never nag: one recheck per cooldown


def timings_report(marks: Dict[str, float], browser: Optional[dict] = None) -> dict:
    """The per-turn `timings` dict: consecutive deltas of the perf_counter marks,
    plus the browser's own share (bounds-checked). Shared by the handler's reply
    payload and its forensic log line."""
    out: dict = {}
    try:
        keys = [k for k in marks if k != "t0"]
        keys.sort(key=lambda k: marks[k])
        prev = marks["t0"]
        for k in keys:
            out[k + "_ms"] = int((marks[k] - prev) * 1000)
            prev = marks[k]
        out["total_ms"] = int((prev - marks["t0"]) * 1000)
        for k in ("endpoint_wait_ms", "encode_ms"):
            v = (browser or {}).get(k)
            if isinstance(v, (int, float)) and 0 <= v < 600000:
                out[k] = int(v)
    except Exception:
        pass
    return out


@dataclass
class TurnOutcome:
    """What one turn decided. The handler maps this to exactly one wire message:
    ``error`` set -> voice_call_error; else voice_call_reply with the one variant
    flag, TTS synthesized by the handler (`reply` in `tts_lang`, following the
    reply's own language when `tts_follow`)."""
    kind: str                      # busy_local|no_speech|clarify|reask|chime_in|silent|llm_failed|reply
    error: Optional[str] = None
    user_text: str = ""
    speaker_label: Optional[str] = None
    reply: str = ""
    tts_lang: str = "de"
    tts_follow: bool = False       # chime/reply speak in the REPLY's language when the lane can
    flags: Dict[str, bool] = field(default_factory=dict)
    delegate: Optional[str] = None
    speaker_ok: bool = True
    active_s: float = 0.0
    marks: Dict[str, float] = field(default_factory=dict)


class VoiceTurnEngine:
    """One live call's turn pipeline. One instance per call, next to (and sharing)
    the call record; never across users (Rule 4.4 - everything keys on the record).

    All model/IO seams are injectable with real defaults, so the unit tests drive
    the pipeline without any model: `transcribe`, `lane_speaks`, `subagents_busy`,
    `request_confirmation`, `clock`, `log`.
    """

    def __init__(self, state: Dict[str, Any], *,
                 transcribe: Optional[Callable] = None,
                 lane_speaks: Optional[Callable[[str], bool]] = None,
                 subagents_busy: Optional[Callable[[], bool]] = None,
                 request_confirmation: Optional[Callable] = None,
                 clock: Callable[[], float] = time.monotonic,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.state = state
        self._transcribe = transcribe
        self._lane_speaks = lane_speaks
        self._subagents_busy = subagents_busy
        self._request_confirmation = request_confirmation
        self._clock = clock
        self._log = log or (lambda msg: None)

    # ── small shared helpers ─────────────────────────────────────────────────

    def _history_add(self, role: str, content: str) -> None:
        # The cap lives HERE, not at the call sites - four hand-rolled sites is how
        # the old 200/400/uncapped inconsistency grew in the first place.
        self.state["history"].append(
            {"role": role, "content": (content or "")[:_HISTORY_MAX_CHARS]})
        self.state["history"] = self.state["history"][-_HISTORY_RING:]

    def note_greeting(self, text: str) -> None:
        """Greeting went OUT (TTS succeeded, handler sent it) - only then does it
        enter the history; a failed greeting never happened, same as before."""
        self._history_add("assistant", text)

    def note_spoken(self, text: str) -> None:
        """voice_call_speak: a server-delivered result was read into the call."""
        self._history_add("assistant", text or "")

    def end(self) -> None:
        """Call ended: drop the rolling transcript (it is context, not a record)."""
        try:
            from vaf.core import voice_context as _vctx
            _vctx.clear(self.state.get("scope"), self.state.get("session"))
        except Exception:
            pass

    def _do_transcribe(self, wav: bytes):
        if self._transcribe is not None:
            return self._transcribe(wav, mime="audio/wav", filename="call.wav",
                                    cache_key=self.state.get("scope"),
                                    default_language=self.state.get("lang"))
        from vaf.core import speech_client as _vsc
        return _vsc.transcribe(wav, mime="audio/wav", filename="call.wav",
                               cache_key=self.state.get("scope"),
                               default_language=self.state.get("lang"))

    def _do_lane_speaks(self, lang: str) -> bool:
        if self._lane_speaks is not None:
            return bool(self._lane_speaks(lang))
        from vaf.core.speech import SpeechManager
        return bool(SpeechManager.get_instance().call_lane_speaks(lang))

    def _do_subagents_busy(self) -> bool:
        if self._subagents_busy is not None:
            return bool(self._subagents_busy())
        from vaf.core.subagent_ipc import get_ipc
        return bool(get_ipc().get_active_tasks())

    # ── the pipeline ─────────────────────────────────────────────────────────

    def turn(self, wav: bytes, *, session_id: str = "", main_busy: bool = False,
             pending_task: str = "", username: str = "") -> TurnOutcome:
        """One utterance through the whole pipeline. Sync on purpose: the handler
        runs it in one executor hop; TTS and the wire stay outside. The step
        numbering (0a, 0, 1, 1b, 2, 2a/2b/2c, 2d, 3, 4) mirrors the pipeline
        section of docs/agents/VOICE_AGENT.md and the branch this code moved
        out of - the comments moved with it."""
        from vaf.core import voice_agent as _va

        _call = self.state          # the shared per-call record - same dict as the registry
        _tm: Dict[str, float] = {"t0": time.perf_counter()}

        def _tm_mark(name: str) -> None:
            _tm[name] = time.perf_counter()

        def _out(kind: str, **kw) -> TurnOutcome:
            kw.setdefault("tts_lang", _call.get("lang", "de"))
            return TurnOutcome(kind=kind, marks=_tm, **kw)

        # 0a. Exclusive-model belt (local time-sharing): while the main agent holds
        # the ONE local model, a voice turn must not queue behind it (it would stall
        # the call for the whole tool run) - the frontend shows the muted state and
        # normally never sends these. SERVER-SIDE truth on top of the frontend flag:
        # live SUB-AGENTS of this session also hold the one model (the main turn may
        # have ended, clearing the frontend's mainTask) - a voice turn then would
        # swap the server to the voice GGUF mid-inference and crash the sub-agent
        # (live incident).
        if _va.is_exclusive():
            _busy_belt = bool(main_busy)
            if not _busy_belt:
                try:
                    # ANY session's live sub-agent holds the one model - a swap
                    # would crash it.
                    _busy_belt = self._do_subagents_busy()
                except Exception:
                    _busy_belt = False
            if _busy_belt:
                return _out("busy_local", error="busy_local")

        # 0. Noise gate (backend belt to the frontend VAD gate): clicks/near-silence
        # never reach STT - Whisper-class models hallucinate text on silence.
        _active_s = _va.active_speech_seconds(wav)
        _tm_mark("gate")
        if _active_s < 0.3:
            self._log(f"voice_call: turn gated as noise (active={_active_s:.2f}s)")
            return _out("no_speech", error="no_speech", active_s=_active_s)

        # 1. STT (provider lane first inside speech_client). Seed the cloud provider
        # (Veyllo/Deepgram treats `language` as a HARD selection) with the user's
        # PROFILE language so a short first clip is not auto-detected as the wrong
        # language (German misheard as French). cache_key engages the per-speaker
        # language cache + periodic re-detect, so a genuine mid-call switch is still
        # caught; default_language only fills the cold-cache first turn.
        # state["lang"] = identity preferred_language (voice_call_start).
        _text, _stt_lang = self._do_transcribe(wav)
        _tm_mark("stt")
        if not _text:
            return _out("no_speech", error="no_speech", active_s=_active_s)

        # 1b. Language follow: when STT detects a DIFFERENT language and the lane
        # the call actually speaks with (cloud TTS, else the Docker container's
        # INSTALLED voices) can speak it, this turn answers AND speaks in that
        # language. Never a download mid-call; the per-language verdict is cached
        # on the call record.
        _turn_lang = _call["lang"]
        try:
            _sl = (_stt_lang or "")[:2].lower()
            if _sl and _sl != _turn_lang:
                _lok = _call.setdefault("lang_ok", {})
                if _sl not in _lok:
                    _lok[_sl] = self._do_lane_speaks(_sl)
                if _lok[_sl]:
                    _turn_lang = _sl
                    self._log(f"voice_call: language follow {_call['lang']} -> {_sl}")
        except Exception:
            _turn_lang = _call["lang"]

        # 2. Speaker label (voice profile), same contract as the chat mic. With an
        # enrolled profile the voice check is authoritative for delegation: only a
        # verified "self" may trigger real work. Unsure, other or a FAILED scoring
        # all leave _speaker_ok False (fail-closed) - the code guard in voice_reply
        # drops the marker.
        _label = None
        _display = "Ich"
        _speaker_ok = True
        _confident = None   # 'self' | 'other' | 'borderline' | None (no profile)
        try:
            from vaf.core import speaker_id as _vsid
            if _vsid.is_enabled():
                _prof = _vsid.load_profile(_call["scope"])
                if _prof is not None:
                    _speaker_ok = False
                    _display = (_prof.get("meta") or {}).get("display_name", "Ich")
                    _score = _vsid.score_wav(wav, _call["scope"])
                    # In-call owner hysteresis + length-awareness
                    # (speaker_id.resolve_label): a confident self verifies and makes
                    # following borderline/short/missing scores count as the owner
                    # for STICKY_WINDOW_S; a clear stranger (reliable-length "other"
                    # well below the band, or a named match) flips immediately.
                    # Owner-approved bridged action gate: a short reply right after a
                    # confident self may still act. Runs even when score_wav returned
                    # None (too-short clip) so a quick clip does not demote a
                    # just-verified owner.
                    _now_s = self._clock()
                    _last_self = _call.get("last_self_ts")
                    _sticky = (_last_self is not None
                               and (_now_s - _last_self) <= _vsid.STICKY_WINDOW_S)
                    _res = _vsid.resolve_label(_score, sticky_self=_sticky)
                    _label = _res.get("label")
                    _speaker_ok = bool(_res.get("speaker_ok"))
                    _confident = _res.get("confident")
                    if _confident == "self":
                        _call["last_self_ts"] = _now_s
                    elif _confident == "other":
                        _call["last_self_ts"] = None
                    if _label:
                        _text = _vsid.label_prefix(_res, _display) + _text
                    if not _speaker_ok:
                        # Non-owner turn: let speaker_confirm decide whether to queue
                        # ONE confirmation (messenger/web) without interrupting the
                        # call - a spoofing check when this speaker CLAIMS to be the
                        # owner (transcript), or a restrained adaptive reclaim on a
                        # plain unsure.
                        try:
                            if self._request_confirmation is not None:
                                self._request_confirmation(
                                    _call["scope"], username or "admin", wav, _score,
                                    session_id=session_id or "",
                                    transcript=_text, owner_name=_display)
                            else:
                                from vaf.core import speaker_confirm as _vsc
                                _vsc.maybe_request_confirmation(
                                    _call["scope"], username or "admin", wav, _score,
                                    session_id=session_id or "",
                                    transcript=_text, owner_name=_display)
                        except Exception:
                            pass
        except Exception:
            pass

        # 2b. Rolling transcript (durable, session/scope-scoped): every heard
        # utterance becomes context the reflex policy can read, outliving the
        # 16-entry call ring (VOICE_REFLEX.md). Best-effort, never blocks.
        _session = _call.get("session") or ""
        try:
            from vaf.core import voice_context as _vctx
            # Store the spoken words WITHOUT the "[label]: " prefix - the speaker
            # label is kept separately, so the transcript digest renders one clean
            # "[label] text" (not a double "[self] [Alice]: text") and no display
            # name is embedded in the guest-facing group context.
            _vctx.record(_call["scope"], _session,
                         _va.strip_speaker_label(_text), label=_label)
        except Exception:
            pass

        _tm_mark("speaker")
        base = dict(user_text=_text, speaker_label=_label, speaker_ok=_speaker_ok,
                    active_s=_active_s)

        # 2a-recover. Speaker recovery (VOICE_REFLEX.md): if we just asked "did you
        # mean me?" (2c-recheck) and THIS turn re-verifies as the owner with a REAL
        # confident self (not a bridged borderline), the owner is recovered - drop
        # the pending check and let this turn continue as a normal owner turn. Guest
        # engagement arms ONLY from an engage command spoken on THIS verified-self
        # turn (not the earlier, unverified asked-about text): the recheck turn is
        # by construction a non-owner (speaker_ok False) and may be a guest, so
        # honoring its stored command would let guest content arm the mode via an
        # unrelated owner turn (confused deputy). Requiring the command on the
        # current authenticated turn keeps the invariant "a guest can never arm
        # engagement". A guest answering never scores confident self, so it can
        # never recover the owner. The pending check expires on its own.
        _recheck = _call.get("pending_speaker_check")
        if _recheck:
            if _confident == "self":
                try:
                    if _va.engage_command_match(_text):
                        from vaf.core import voice_policy as _vpolR
                        _call["engage_guests"] = {
                            "expires_at": self._clock() + _vpolR.GUEST_ENGAGE_TTL_S,
                            "since_wall": (_call.get("engage_guests") or {}).get(
                                "since_wall") or time.time()}
                        self._log("voice_call: guest-engagement ON (owner recovered)")
                except Exception:
                    pass
                _call.pop("pending_speaker_check", None)
                self._log("voice_call: speaker recovered as owner")
            else:
                # The answer to "did you mean me?" arrived but did NOT verify as the
                # owner (voice still not placed). NEVER leave it silent (the live
                # gap: an affirmative reply was dropped as side-talk): an
                # affirmative "yes" means the speaker IS addressing us, so speak a
                # short "I could not place your voice, confirm on screen/messenger"
                # and lean on the confirmation card already queued in the speaker
                # block (an authenticated yes learns the voice). The voice alone
                # still grants nothing. A clear "no" or an expired window just drops
                # the pending check.
                _yn = None
                try:
                    from vaf.core import speaker_confirm as _vscR
                    _p = _vscR.parse_reply(_va.strip_speaker_label(_text))
                    _yn = _p[0] if _p else None
                except Exception:
                    _yn = None
                _expired = (self._clock()
                            - float(_recheck.get("asked_at") or 0.0) > _RECHECK_PENDING_TTL_S)
                if _yn == "yes":
                    _cl = _va.speaker_recheck_confirm_line(_turn_lang, _call["scope"])
                    _call.pop("pending_speaker_check", None)
                    self._history_add("user", _text)
                    self._history_add("assistant", _cl)
                    self._log("voice_call: speaker recheck answered yes but unverified"
                              " -> asked to confirm")
                    return _out("clarify", reply=_cl, tts_lang=_turn_lang,
                                flags={"clarify": True}, **base)
                elif _yn == "no" or _expired:
                    _call.pop("pending_speaker_check", None)

        # 2b-answer. In-call pending-answer resolution (VOICE_REFLEX.md): if the
        # agent JUST asked a question, this utterance is probably its answer. A
        # local, no-LLM verdict decides: owner reply -> ANSWER (the Q&A link is
        # injected into voice_reply below); a "say that again" -> REASK the same
        # question (capped); a guest's ON-TOPIC remark -> a spoken (never acting)
        # reply while the owner's question stays open; anything else -> CONTINUE as
        # a normal turn. Authorizes nothing: a non-owner stays tool-locked by
        # speaker_ok below.
        _answer_ctx = ""      # the owner's question to inject (owner ANSWER only)
        _force_reply = False  # bypass the side-talk gate: this IS a reply
        _pq = _call.get("pending_q")
        if _pq:
            from vaf.core import voice_policy as _vpolA
            # Scene + relevance inputs (Step B). Best-effort; a hiccup degrades to
            # a 1:1/no-topic decision, never breaks the turn.
            _recent_labels_a, _activity_a = [], 0.5
            try:
                from vaf.core import voice_context as _vctxA
                from vaf.core.config import Config as _CfgB
                _recent_labels_a = [e[1] for e in
                                    _vctxA.recent(_call["scope"], _session, n=8)]
                _activity_a = _CfgB.get("voice_awareness_activity", 0.5)
            except Exception:
                pass
            try:
                _av = _vpolA.answer_verdict(
                    _pq.get("text", ""), _text, _label,
                    speaker_ok=_speaker_ok,
                    asked_ago_s=self._clock() - float(_pq.get("asked_at") or 0.0),
                    reask_count=int(_pq.get("reask_count") or 0),
                    recent_labels=_recent_labels_a, activity=_activity_a)
            except Exception:
                _av = {"verdict": _vpolA.CONTINUE, "reason": "error", "guest": False}
            _averdict = _av.get("verdict")
            if _averdict == _vpolA.ANSWER:
                _force_reply = True
                if _av.get("guest"):
                    # Guest on-topic remark earns a spoken reply below (speaker_ok
                    # False keeps it tool-locked AND withholds the owner's question -
                    # no _answer_ctx). The OWNER's question stays open (unanswered)
                    # within its budget.
                    _pq["turns_left"] = int(_pq.get("turns_left") or 0) - 1
                    if _pq["turns_left"] <= 0:
                        _call.pop("pending_q", None)
                    else:
                        _call["pending_q"] = _pq
                    self._log(f"voice_call: pending-answer GUEST on-topic text={_text[:50]!r}")
                else:
                    _answer_ctx = _pq.get("text", "")
                    _call.pop("pending_q", None)
                    self._log(f"voice_call: pending-answer ANSWER q={_answer_ctx[:60]!r}")
            elif _averdict == _vpolA.REASK:
                # Owner asked us to repeat: re-ask the SAME question (spoken), keep
                # the pending state with a fresh window, keep listening.
                _reask = ""
                try:
                    from vaf.core import vocab as _vocabR
                    _reask = _vocabR.pick("reask_pending", _turn_lang,
                                          scope=_call["scope"],
                                          question=_pq.get("text", "")[:160])
                except Exception:
                    _reask = ""
                if not _reask:
                    _reask = _pq.get("text", "")
                _pq["reask_count"] = int(_pq.get("reask_count") or 0) + 1
                _pq["asked_at"] = self._clock()
                _pq["turns_left"] = _vpolA.PENDING_Q_TURNS
                _call["pending_q"] = _pq
                self._history_add("user", _text)
                self._history_add("assistant", _reask)
                self._log(f"voice_call: pending-answer REASK text={_text[:50]!r}")
                return _out("reask", reply=_reask, tts_lang=_turn_lang,
                            flags={"reask": True}, **base)
            else:  # CONTINUE - not the answer; drop when stale or budget spent
                _pq["turns_left"] = int(_pq.get("turns_left") or 0) - 1
                if _av.get("reason") == "expired" or _pq["turns_left"] <= 0:
                    _call.pop("pending_q", None)

        # 2c. Addressee ambiguity (tier 1, no LLM): an address-check cue ("kannst du
        # mich hoeren", "bist du da") from a NON-owner speaker who did not name the
        # agent - ask "did you mean me?" instead of answering or silently ignoring.
        # Authorizes nothing (anti-spoofing unchanged); it is a spoken question.
        try:
            if not _force_reply and _va.wants_addressee_clarification(
                    _text, _label, _call.get("agent_name", "")):
                _clar = _va.addressee_clarify_line(_turn_lang, _call["scope"])
                self._history_add("user", _text)
                self._history_add("assistant", _clar)
                self._log(f"voice_call: addressee clarify text={_text[:60]!r}")
                return _out("clarify", reply=_clar, tts_lang=_turn_lang,
                            flags={"clarify": True}, **base)
        except Exception as _clar_e:
            self._log(f"voice_call clarify failed: {_clar_e}")

        # 2c-recheck. Speaker recovery (VOICE_REFLEX.md): an AMBIGUOUS turn (label
        # 'unsure', profile enrolled but not verified) that is clearly DIRECTED at
        # the agent is probably the owner mislabeled in a noisy multi-person call.
        # Ask "did you mean me?" in the turn language: the answer is a fresh voice
        # sample that can re-verify the owner next turn (2a-recover), and the
        # out-of-band confirmation to screen/messenger already fired in the speaker
        # block (maybe_request_confirmation, which learns the owner's voice on an
        # authenticated yes). Per-call cooldown so it never nags. Authorizes
        # NOTHING - it is a spoken question.
        try:
            if (not _force_reply and not _speaker_ok
                    and _call.get("pending_speaker_check") is None
                    and self._clock() >= _call.get("recheck_cooldown_until", 0.0)
                    and _va.wants_speaker_recheck(
                        _text, _label, _call.get("agent_name", ""))):
                _rc = _va.addressee_clarify_line(_turn_lang, _call["scope"])
                _call["pending_speaker_check"] = {
                    "text": _text, "asked_at": self._clock()}
                _call["recheck_cooldown_until"] = self._clock() + _RECHECK_COOLDOWN_S
                self._history_add("user", _text)
                self._history_add("assistant", _rc)
                self._log(f"voice_call: speaker recheck (did you mean me?) text={_text[:60]!r}")
                return _out("clarify", reply=_rc, tts_lang=_turn_lang,
                            flags={"clarify": True}, **base)
        except Exception as _rc_e:
            self._log(f"voice_call recheck failed: {_rc_e}")

        # Owner-toggled guest engagement (VOICE_REFLEX.md): while active, a guest
        # turn that would be side_talk is engaged instead (spoken reply, still
        # tool-locked via speaker_ok). Sliding TTL; an expired toggle is cleared
        # here. Set/ended/refreshed from the reply markers after voice_reply below.
        _eg = _call.get("engage_guests")
        _eg_on = bool(_eg and self._clock() <= _eg.get("expires_at", 0.0))
        if _eg and not _eg_on:
            _call.pop("engage_guests", None)

        # 2d. Addressee gate (tier 1, no LLM): side talk from other speakers and
        # garbled non-owner input never reach the LLM - the text still enters the
        # call history as room context (the labels exist for this).
        if _force_reply:
            # Already resolved as the answer to the agent's own question (owner) or
            # a guest's on-topic remark -> always engage. The owner Q&A link
            # (_answer_ctx) is injected into voice_reply; a guest gets a normal,
            # tool-locked spoken reply (no link).
            _engage, _gate_reason = True, "answer"
        else:
            _engage, _gate_reason = _va.should_engage(
                _text, _label, agent_name=_call.get("agent_name", ""),
                engage_guests=_eg_on)
        if not _engage:
            # Reflex chime-in: side talk from another speaker normally stores +
            # stays silent, but the LOCAL policy may find it interesting enough
            # (GROUNDED in the owner's topics) for a brief spoken remark - a living
            # presence, not a chatbot. Never forced: grounding is required AND the
            # content LLM may still stay silent. Skipped on garbled noise, while the
            # main agent is busy (a chime-in over a running task is noise), and
            # deduped against recent chime-ins.
            try:
                if _gate_reason == "side_talk" and not bool(main_busy):
                    from vaf.core import voice_policy as _vpol
                    from vaf.core import voice_context as _vctx
                    from vaf.core.config import Config as _CfgA
                    _topics = _CfgA.get("voice_awareness_topics", []) or []
                    if not isinstance(_topics, list):
                        _topics = []
                    _activity = _CfgA.get("voice_awareness_activity", 0.5)
                    _recent_labels = [e[1] for e in
                                      _vctx.recent(_call["scope"], _session, n=8)]
                    _dec = _vpol.chime_decision(
                        _text, _label, recent_labels=_recent_labels,
                        topics=_topics, activity=_activity)
                    if _dec.get("speak"):
                        # Owner privacy: the rolling transcript can hold the owner's
                        # earlier private [self] talk from before this guest
                        # arrived; a guest chime-in (speaker_ok False) must not
                        # receive it. Do not even build it here (chime_in_reply also
                        # withholds it - belt and suspenders).
                        _digest = (_vctx.digest(_call["scope"], _session, n=8)
                                   if _speaker_ok else "")
                        _remark = _va.chime_in_reply(
                            _text, scope_id=_call["scope"],
                            lang=_turn_lang, user_name=_display,
                            agent_name=_call.get("agent_name", ""),
                            speaker_ok=_speaker_ok, transcript=_digest)
                        _chime_recent = _call.setdefault("chime_recent", [])
                        _dup = False
                        if _remark:
                            _dup = _vpol.similar_to_any(_remark, list(_chime_recent))
                        if _remark and not _dup:
                            _chime_recent.append(_remark)
                            _call["chime_recent"] = _chime_recent[-_CHIME_RING:]
                            self._history_add("user", _text)
                            self._history_add("assistant", _remark)
                            try:
                                _vctx.record(_call["scope"], _session, _remark,
                                             label="agent", verdict="chime_in")
                            except Exception:
                                pass
                            self._log(f"voice_call: CHIME-IN mode={_dec.get('mode')} "
                                      f"score={_dec.get('score')} text={_remark[:80]!r}")
                            return _out("chime_in", reply=_remark, tts_lang=_turn_lang,
                                        tts_follow=True, flags={"chime_in": True}, **base)
            except Exception as _chime_e:
                self._log(f"voice_call chime-in failed: {_chime_e}")
            self._history_add("user", _text)
            self._log(f"voice_call: not engaging ({_gate_reason}) text={_text[:60]!r}")
            return _out("silent", flags={"silent": True}, **base)

        # 3. First-layer reply (one step + RAG; may delegate). While the main agent
        # works on an earlier delegation, further delegation is suppressed - casual
        # talk must never spawn or disturb a running main-agent turn.
        _busy = bool(main_busy)
        _pending = (pending_task or "")[:300]
        _uname = username or ""
        # Scene awareness for the reply prompt: multi-party (a guest is present,
        # from the current label + recent transcript labels) and whether the owner
        # has toggled guest engagement on. Drives the dynamic scene block; a 1:1
        # call leaves the prompt unchanged.
        try:
            from vaf.core import voice_policy as _vpolS
            from vaf.core import voice_context as _vctx
            _scene_labels = [e[1] for e in
                             _vctx.recent(_call["scope"], _session, n=8)]
            _multi = (_vpolS.derive_scene(_label, _scene_labels) == "multi"
                      or _gate_reason == "engage_guest" or _eg_on)
        except Exception:
            _multi = (_gate_reason == "engage_guest" or _eg_on)
        _scene = {"multi": bool(_multi), "engage_guests": _eg_on}
        # Group-conversation context (VOICE_REFLEX.md): while guest engagement is
        # active, the model gets the SHARED, spoken-aloud room transcript so it can
        # follow the multi-person, multi-language dynamic instead of seeing one
        # context-free line and stalling. Scoped to talk AFTER engagement started
        # (since_wall) so the owner's earlier private 1:1 is never shown -
        # everything after was heard by everyone present, so it is safe even on a
        # guest turn.
        _group_ctx = ""
        if _eg_on:
            try:
                from vaf.core import voice_context as _vctx
                _since = (_call.get("engage_guests") or {}).get("since_wall")
                # Fail-CLOSED on a missing boundary: without since_wall,
                # digest(since=None) would return the WHOLE transcript, including
                # the owner's pre-engagement private 1:1. Only build the group
                # context when the post-engagement boundary is known.
                if _since is not None:
                    _group_ctx = _vctx.digest(_call["scope"], _session, n=12,
                                              since=_since)
            except Exception:
                _group_ctx = ""
        _tm_mark("policy")
        _res = _va.voice_reply(
            _text, scope_id=_call["scope"], lang=_turn_lang,
            user_name=_display, history=_call["history"],
            main_busy=_busy, pending_task=_pending,
            speaker_ok=_speaker_ok,
            chat_context=_call.get("chat_context", ""),
            username=_uname,
            addressed=(bool(_force_reply)
                       or _gate_reason == "wake_word"
                       or _va.addressed_by_name(
                           _text, _call.get("agent_name", ""))),
            pending_question=_answer_ctx,
            agent_name=_call.get("agent_name", ""),
            persona=_call.get("agent_soul", ""),
            scene=_scene,
            group_context=_group_ctx,
        )
        _tm_mark("llm")
        if _res is None:
            return _out("llm_failed", error="llm_failed", **base)
        if _res.get("silent"):
            # Tier 2: the model itself judged this as not addressed to it. Keep the
            # utterance as context, skip TTS, keep listening.
            self._history_add("user", _text)
            self._log(f"voice_call: model chose silence text={_text[:60]!r}")
            return _out("silent", flags={"silent": True}, **base)

        # 4. Delegation is a DECISION here; the enqueue (the one external write)
        # stays visible in the handler, which fills the reply's `delegated` field.
        _delegate = _res.get("delegate") or None

        # History + rolling transcript for the normal reply (the shared 800 cap
        # applies inside _history_add, like everywhere else).
        self._history_add("user", _text)
        self._history_add("assistant", _res["reply"])
        # Record the agent's own spoken reply into the rolling transcript (label
        # 'agent') so the shared group-conversation context shows the full
        # back-and-forth, not just the human turns. Best-effort.
        try:
            from vaf.core import voice_context as _vctx
            if _res.get("reply"):
                _vctx.record(_call["scope"], _session, _res["reply"],
                             label="agent", verdict="reply")
        except Exception:
            pass

        # Arm the in-call pending-question state: if this reply is itself a
        # question, the NEXT utterance is probably its answer (resolved at
        # 2b-answer next turn). Owner-only: a NON-owner turn must never touch the
        # owner's pending_q - a guest's words are never taken as the answer to an
        # owner-directed question, the question (which may hold owner-private
        # context) is never replayed to a guest, AND an engaged/on-topic guest reply
        # must not clear a question the owner has not answered yet (2b-answer keeps
        # it open). So gate the whole block on speaker_ok; a non-question OWNER
        # reply still clears any stale pending question.
        if _speaker_ok:
            try:
                if _res.get("reply") and _va.is_question(_res["reply"]):
                    from vaf.core import voice_policy as _vpolB
                    _call["pending_q"] = {
                        "text": _res["reply"], "asked_at": self._clock(),
                        "turns_left": _vpolB.PENDING_Q_TURNS, "reask_count": 0,
                    }
                else:
                    _call.pop("pending_q", None)
            except Exception:
                _call.pop("pending_q", None)

        # Owner-toggled guest engagement: set/end/refresh from the reply markers OR
        # a deterministic engage command. ONLY a VERIFIED-self owner turn may toggle
        # it - a guest can never enroll the agent into talking to strangers. The arm
        # gate is tightened from speaker_ok to (speaker_ok AND confident !=
        # 'borderline'): a bridged-borderline sticky turn can SPEAK as the owner but
        # must not ARM engagement, so a short/ambiguous clip right after the owner
        # can never turn the mode on. confident is None with no profile enrolled
        # (fail-open owner) - that still arms. A deterministic command
        # (engage_command_match) arms even when the weak local model never emits the
        # <talk_to_guest/> marker (live: the model chose silence and never armed).
        # Any active turn slides the TTL so an ongoing exchange does not lapse.
        try:
            from vaf.core import voice_policy as _vpolG
            _arm_ok = _speaker_ok and _confident != "borderline"
            _cmd_arm = _arm_ok and _va.engage_command_match(_text)
            if _arm_ok and _res.get("end_guest"):
                _call.pop("engage_guests", None)
                self._log("voice_call: guest-engagement ended by owner")
            elif _arm_ok and (_res.get("engage_guest") or _cmd_arm):
                _call["engage_guests"] = {
                    "expires_at": self._clock() + _vpolG.GUEST_ENGAGE_TTL_S,
                    # since_wall scopes the group-conversation context to talk AFTER
                    # engagement (privacy); preserved across a re-arm.
                    "since_wall": (_call.get("engage_guests") or {}).get(
                        "since_wall") or time.time()}
                self._log("voice_call: guest-engagement ON (owner-initiated%s)"
                          % (", command" if _cmd_arm else ""))
            elif _call.get("engage_guests"):
                _call["engage_guests"]["expires_at"] = (
                    self._clock() + _vpolG.GUEST_ENGAGE_TTL_S)
        except Exception:
            pass

        return _out("reply", reply=_res["reply"], tts_lang=_turn_lang,
                    tts_follow=True, delegate=_delegate, **base)

# ── Call-lifecycle surface (the handler's ONE door into the engine) ───────────
# The turn pipeline above is the physics; these thin wrappers are the rest of
# what a call's LIFECYCLE needs from the engine side - lane readiness, the
# greeting, the semantic endpointer, the speaker prewarm, result sanitizing.
# They exist so the consumer imports ONE module instead of reaching into
# voice_agent/voice_vad/voice_model/speaker_id through the back door (the same
# shape as agent.py consuming the tool pipeline only through tool_dispatch).
# Each is a deliberate pass-through, not an adapter: no signature invents
# anything the wrapped function does not have. TTS, auth, the task queue and
# tray concerns are NOT here - those are the caller's half by design.

def lane_status() -> Dict[str, Any]:
    """Readiness of the voice lane: {"available", "exclusive", "dedicated_model"}.
    `exclusive` means ONE local model is time-shared with the main agent (the
    caller mutes the voice lane while main-agent work runs)."""
    from vaf.core import voice_agent as _va
    return {"available": _va.available(),
            "exclusive": _va.is_exclusive(),
            "dedicated_model": _va.dedicated_local_model()}


def greeting_for(state: Dict[str, Any]) -> str:
    """The deterministic call-opening line, in the call language, addressed to
    the enrolled display name when a profile exists (no LLM round-trip)."""
    name = ""
    try:
        from vaf.core import speaker_id as _sid
        prof = _sid.load_profile(state.get("scope"))
        name = ((prof or {}).get("meta") or {}).get("display_name", "")
    except Exception:
        name = ""
    from vaf.core import voice_agent as _va
    return _va.greeting_line(state.get("lang", "de"), name,
                             scope_id=state.get("scope", ""))


def build_chat_digest(messages) -> str:
    """Compact structural digest of an open chat session (pass-through to
    voice_agent.build_chat_digest) - the "what does 'here' refer to" context."""
    from vaf.core import voice_agent as _va
    return _va.build_chat_digest(messages)


def make_endpointer():
    """A per-call StreamEndpointer when the semantic turn-end lane is armed
    (voice_semantic_endpoint_enabled + model present), else None."""
    from vaf.core.voice_vad import StreamEndpointer, get_turn_judge
    judge = get_turn_judge()
    return StreamEndpointer(judge=judge) if judge is not None else None


def prewarm_speaker(scope: str) -> bool:
    """True when a profile exists and the extractor prewarm should be kicked
    (the caller runs speaker prewarm off its own loop). Prewarming matters:
    during the cold load the owner scores as unsure and is treated as a guest."""
    from vaf.core import speaker_id as _sid
    if not (_sid.is_enabled() and _sid.load_profile(scope) is not None):
        return False
    _sid.prewarm()
    return True


def prepare_dedicated_model_async(on_ready=None) -> None:
    """Kick the dedicated voice GGUF load/swap in the background (pass-through
    to voice_model.ensure_voice_model_async)."""
    from vaf.core import voice_model as _vvm
    if on_ready is None:
        _vvm.ensure_voice_model_async()
    else:
        _vvm.ensure_voice_model_async(on_ready=on_ready)


def subagents_hold_model() -> bool:
    """A live sub-agent (any session) holds the ONE local model - no model swap
    may run then (a swap mid-inference crashed a sub-agent live). The same probe
    the engine's busy belt uses; exposed so the caller and the engine share one
    truth. Fail-open False: a broken probe must not block a call."""
    try:
        from vaf.core.subagent_ipc import get_ipc
        return bool(get_ipc().get_active_tasks())
    except Exception:
        return False


def strip_spoken_result(text: str) -> str:
    """Remove leaked reasoning from a main-agent result before it is spoken or
    stored (VOICE_AGENT.md invariant 3) - pass-through to the voice agent's
    reasoning stripper, fail-open to the original text."""
    try:
        from vaf.core.voice_agent import _strip_reasoning
        return _strip_reasoning(text)
    except Exception:
        return text


def clear_transcript(scope, session) -> None:
    """Drop a call's rolling transcript (the engine-less fallback of
    VoiceTurnEngine.end(), for a record whose engine never initialized)."""
    try:
        from vaf.core import voice_context as _vctx
        _vctx.clear(scope, session)
    except Exception:
        pass
