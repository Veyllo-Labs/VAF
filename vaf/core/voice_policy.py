# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Stage 1 of the local voice reflex policy (docs/agents/VOICE_REFLEX.md).

The policy layer decides, WITHOUT the big LLM, whether an utterance should make the
agent respond, be merely remembered, or be ignored. Stage 1 is fully deterministic and
local: a vocab-backed keyword/trigger prefilter plus embedding similarity against the
owner's interests/topics. Stage 2 (a small ONNX classifier for the ambiguous middle)
plugs in later. This never becomes a second inference on the one llama server.

"interesting" REQUIRES grounding: the utterance must be relevant to the owner's
configured topics (`voice_awareness_topics`), i.e. embedding similarity above the
activity-scaled bar. A vocab trigger phrase ("can you", "remind me", ...) only LOWERS
that bar - it is
a cheap prefilter, never sufficient on its own. No grounding, no chime-in (this is the
anti-fabrication guard: a bare cue word about something the owner does not care about
must not make the agent grasp for a reason to speak).

Phase 0 is a SKELETON: it computes the interestingness signal but does NOT yet change
behavior - `classify()` returns the same three-way verdict as the Tier-1 gate
(`voice_agent.classify_utterance`). Phase 2 wires the signal into a chime-in upgrade.
Everything catches and degrades to a safe default (never break the realtime path).
"""
from __future__ import annotations

import math
import threading
from typing import List, Optional, Sequence

from vaf.core import voice_agent

# activity in [0,1] shifts the interestingness threshold: quiet (0) => high bar
# (rarely interesting), active (1) => low bar. Orthogonal to the internal scene modes.
# Calibrated against REAL embeddings (2026-07-18 live call): MiniLM cosine of a short
# overheard utterance vs a keyword-rich owner topic sits around 0.35-0.45 for genuinely
# on-topic speech and below ~0.28 for off-topic/generic speech (player names, filler,
# STT noise). The band lives in that gap so on-topic side-talk clears it while chatter
# does not. The earlier 0.42-0.78 band was mock-tested only and never fired live.
_THR_QUIET = 0.40
_THR_ACTIVE = 0.28
_TRIGGER_RELAX = 0.08   # a cue phrase eases the grounding bar...
_GROUND_FLOOR = 0.28    # ...but never below this: no grounding, no chime-in

_triggers_cache: Optional[List[str]] = None
_triggers_lock = threading.Lock()


def _load_triggers() -> List[str]:
    """All awareness-trigger phrases across languages, lowercased (fail-open to [])."""
    global _triggers_cache
    if _triggers_cache is None:
        with _triggers_lock:
            if _triggers_cache is None:
                out: List[str] = []
                try:
                    from vaf.core import vocab
                    for lang in vocab.available_languages("awareness_triggers"):
                        for p in vocab.phrasings("awareness_triggers", lang):
                            p = (p or "").strip().lower()
                            if p:
                                out.append(p)
                except Exception:
                    out = []
                _triggers_cache = out
    return _triggers_cache


def trigger_match(text: str) -> Optional[str]:
    """Return the first awareness-trigger phrase contained in the utterance, or None.
    Language-agnostic substring match (the phrases are per-language but combined)."""
    try:
        core = str(text or "").strip().lower()
        if not core:
            return None
        for phrase in _load_triggers():
            if phrase in core:
                return phrase
    except Exception:
        pass
    return None


def _embed_one(text: str) -> Optional[Sequence[float]]:
    """One normalized embedding via the shared MiniLM singleton, or None. Isolated
    behind a helper so tests can inject deterministic vectors and the realtime path
    degrades cleanly when embeddings are unavailable."""
    try:
        from vaf.memory.embeddings import get_embedding_service
        v = get_embedding_service().embed_sync(str(text or ""))
        return v or None
    except Exception:
        return None


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    try:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na <= 0 or nb <= 0:
            return 0.0
        return max(0.0, min(1.0, dot / (na * nb)))
    except Exception:
        return 0.0


def interest_score(text: str, topics: Optional[Sequence[str]]) -> float:
    """Max embedding similarity of the utterance to any of the owner's interest
    topics, in [0,1]. 0.0 when there are no topics or embeddings are unavailable."""
    if not text or not topics:
        return 0.0
    v = _embed_one(text)
    if not v:
        return 0.0
    best = 0.0
    for t in topics:
        if not str(t or "").strip():
            continue
        tv = _embed_one(t)
        if tv:
            best = max(best, _cosine(v, tv))
    return best


def _threshold(activity: float) -> float:
    a = 0.0 if activity is None else max(0.0, min(1.0, float(activity)))
    return _THR_QUIET + (_THR_ACTIVE - _THR_QUIET) * a


def is_interesting(text: str, topics: Optional[Sequence[str]] = None,
                   activity: float = 0.5) -> bool:
    """True only when the utterance is GROUNDED in the owner's configured topics above the
    activity-scaled bar. A trigger phrase lowers that bar but is never sufficient alone
    - no grounding, no chime-in. This is the signal a chime-in decision will read."""
    thr = _threshold(activity)
    if trigger_match(text):
        thr = max(_GROUND_FLOOR, thr - _TRIGGER_RELAX)
    return interest_score(text, topics) >= thr


# Internal, system-chosen behavior modes (docs/agents/VOICE_REFLEX.md). These are
# NOT a user toggle and are never switched by voice command - the policy derives
# them deterministically from the scene, the speaker label and the activity dial.
MODE_ACTIVE = "active"        # 1:1 with the owner - ready to chime in (low bar)
MODE_NOTES = "notes_only"     # record only, never chime in audibly
MODE_QUIET = "quiet"          # default - audible only on a high interestingness score

_NOTES_FLOOR = 0.05     # dial at/below this = notes-only (record, never interrupt)
_SCENE_BIAS = 0.15      # 1:1-with-owner eases the bar; a busy room raises it
_DEDUP_SIM = 0.86       # a chime-in this close to a recent one is a repeat


def derive_scene(label: Optional[str], recent_labels: Optional[Sequence[str]] = None) -> str:
    """'one_to_one' when only the owner is speaking, else 'multi' (someone else is
    in the room, or the owner is talking to another person). Deterministic, no LLM."""
    if label in ("other", "named"):
        return "multi"
    for prev in list(recent_labels or []):
        if prev in ("other", "named"):
            return "multi"
    return "one_to_one"


def derive_mode(scene: str, label: Optional[str], activity: float = 0.5) -> str:
    """The internal behavior mode for this scene. The dial at its floor pins
    notes_only (record, never interrupt); a 1:1 with the verified owner tends to
    active; anything else stays quiet (the safe default)."""
    a = 0.0 if activity is None else max(0.0, min(1.0, float(activity)))
    if a <= _NOTES_FLOOR:
        return MODE_NOTES
    if scene == "one_to_one" and label == "self":
        return MODE_ACTIVE
    return MODE_QUIET


def _mode_activity(mode: str, activity: float) -> float:
    """Fold the scene mode into the ONE activity dial as a threshold shift (the
    owner sets a single ruler; the mode biases it per scene). Active eases the bar,
    quiet raises it - never a separate knob for the user to manage."""
    a = 0.0 if activity is None else max(0.0, min(1.0, float(activity)))
    if mode == MODE_ACTIVE:
        return min(1.0, a + _SCENE_BIAS)
    if mode == MODE_QUIET:
        return max(0.0, a - _SCENE_BIAS)
    return a


def chime_decision(text: str, label: Optional[str], *,
                   recent_labels: Optional[Sequence[str]] = None,
                   topics: Optional[Sequence[str]] = None,
                   activity: float = 0.5) -> dict:
    """Whether to AUDIBLY chime in on an overheard (store_only) utterance. Returns
    {mode, scene, score, interesting, trigger, speak}. `speak` requires GROUNDING
    (is_interesting: embedding match to the owner's topics above the mode-scaled bar)
    AND a mode that permits audible output - notes_only never speaks. Never forced:
    the content LLM still gets the final say and may stay silent. Fail-safe to no
    chime-in on any error."""
    try:
        scene = derive_scene(label, recent_labels)
        mode = derive_mode(scene, label, activity)
        if mode == MODE_NOTES:
            return {"mode": mode, "scene": scene, "score": 0.0,
                    "interesting": False, "trigger": None, "speak": False}
        eff = _mode_activity(mode, activity)
        trig = trigger_match(text)
        score = interest_score(text, topics) if topics else 0.0
        thr = _threshold(eff)
        if trig:
            thr = max(_GROUND_FLOOR, thr - _TRIGGER_RELAX)
        interesting = score >= thr
        return {"mode": mode, "scene": scene, "score": round(score, 4),
                "interesting": interesting, "trigger": trig, "speak": interesting}
    except Exception:
        return {"mode": MODE_QUIET, "scene": "multi", "score": 0.0,
                "interesting": False, "trigger": None, "speak": False}


def similar_to_any(text: str, recent_texts: Optional[Sequence[str]],
                   threshold: float = _DEDUP_SIM) -> bool:
    """True if `text` is embedding-close to any recent chime-in, so the agent does
    not repeat itself within a call. Local, embedding-based, fail-open to False
    (an embedding hiccup must never block a genuinely new remark)."""
    try:
        cand = str(text or "").strip()
        if not cand or not recent_texts:
            return False
        v = _embed_one(cand)
        if not v:
            return False
        for prev in recent_texts:
            pv = _embed_one(str(prev or ""))
            if pv and _cosine(v, pv) >= threshold:
                return True
    except Exception:
        return False
    return False


def classify(text: str, label: Optional[str], agent_name: str = "", *,
             topics: Optional[Sequence[str]] = None, activity: float = 0.5) -> dict:
    """The reflex decision. Returns a dict with the three-way `verdict`, the Tier-1
    `reason`, and the interestingness signals (`trigger`, `score`, `interesting`).

    Phase 0 (skeleton): `verdict` mirrors `voice_agent.classify_utterance` - no chime-in
    upgrade yet - while the signals are computed so Phase 2 can use them. All local, no
    LLM, and top-level guarded so a policy hiccup never breaks the realtime path.
    """
    try:
        verdict, reason = voice_agent.classify_utterance(text, label, agent_name)
        trig = trigger_match(text)
        score = interest_score(text, topics) if topics else 0.0
        # Grounding required (see is_interesting): a trigger only lowers the bar.
        thr = _threshold(activity)
        if trig:
            thr = max(_GROUND_FLOOR, thr - _TRIGGER_RELAX)
        interesting = score >= thr
        return {
            "verdict": verdict,
            "reason": reason,
            "trigger": trig,
            "score": round(score, 4),
            "interesting": interesting,
        }
    except Exception:
        return {"verdict": "ignore", "reason": "error", "trigger": None,
                "score": 0.0, "interesting": False}
