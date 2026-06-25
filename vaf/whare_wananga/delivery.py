# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Whare Wananga -- delivery side: surface learned tool know-how to the agent at runtime.

The store is the WRITE path (training); this is the READ path. Two consumers:

- **Proactive (A-track):** the tool-schema builder (`Agent.TOOLS`) calls `tool_pitfalls(name)` for
  each ROUTER-SELECTED tool and appends the learned pitfalls to its description, so the model sees
  them before forming the call (no extra generation, Action-tag independent).
- **Reactive (B-track):** when a tool call FAILS at runtime, the agent loop calls `tool_knowhow(name)`
  (fuller: pitfalls + procedure/verification) and nudges it into the context so the natural retry is
  informed; `known_pitfall_hit(name, error)` tells a known pitfall from a novel error.

Only the learned baskets are delivered (`aronui` overlaps the static tool description). Everything is
gated on reliable knowledge and is **hard fail-safe** -- it must NEVER raise, since the callers are
on the critical path of every LLM call / tool result.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import List, Optional

from vaf.whare_wananga import store

# ── Quality gate + size policy (calibrate against real data later) ────────────
_GATE_STATUS = "confirmed"
# Only knowledge that was actually PROBED -- excludes "declare" (pitfalls inferred from the schema,
# never exercised) and a missing/None learn_mode.
_GATE_LEARN_MODES = ("probe", "error_path", "runtime", "teacher")
_MIN_CONFIDENCE = 0.0          # soft floor; 0.0 = off. challenge_passed is the hard gate.
_DEFAULT_MAX_PITFALLS = 3
_DEFAULT_MAX_CHARS = 320
_KNOWHOW_MAX_CHARS = 600       # reactive block carries pitfalls + procedure + checks


def _mtime(name: str) -> float:
    """File mtime of the tool's record (0.0 if absent) -- used only as a cache key, so the cache
    self-invalidates when a tool is re-trained."""
    try:
        return os.path.getmtime(store._path(name))
    except Exception:
        return 0.0


@lru_cache(maxsize=512)
def _load_gated(name: str, _mtime_key: float):
    """Return the record IF it passes the quality gate, else None. Cached per (name, mtime).
    Treat the result as read-only (it is shared from the cache)."""
    try:
        rec = store.load(name)
        if not rec:
            return None
        if rec.get("status") != _GATE_STATUS:
            return None
        if rec.get("challenge_passed") is not True:
            return None
        if rec.get("learn_mode") not in _GATE_LEARN_MODES:
            return None
        conf = rec.get("confidence")
        if _MIN_CONFIDENCE and isinstance(conf, (int, float)) and conf < _MIN_CONFIDENCE:
            return None
        return rec
    except Exception:
        return None


def _gated(name: str):
    try:
        return _load_gated(name, _mtime(name))
    except Exception:
        return None


def _norm(s) -> str:
    return " ".join(str(s or "").split())


def _texts(items, limit: int) -> List[str]:
    out = []
    for it in (items or [])[:limit]:
        t = _norm(it.get("text") if isinstance(it, dict) else it)
        if t:
            out.append(t)
    return out


def _cap(block: str, max_chars: int) -> str:
    return block if len(block) <= max_chars else block[:max_chars - 1].rstrip() + "…"


# ── Proactive (A-track): pitfalls only ────────────────────────────────────────
def tool_pitfalls(name: str, *, max_pitfalls: int = _DEFAULT_MAX_PITFALLS,
                  max_chars: int = _DEFAULT_MAX_CHARS) -> Optional[str]:
    """Compact 'learned pitfalls' block (tuatea) for a tool, or None. Gated + fail-safe."""
    try:
        rec = _gated(name)
        if not rec:
            return None
        raw = [p for p in ((rec.get("tuatea") or {}).get("pitfalls") or [])
               if not store.is_vacuous_pitfall(p)]
        pits = _texts(raw, max_pitfalls)
        if not pits:
            return None
        return _cap("Learned pitfalls (from practice): " + " ; ".join(pits), max_chars)
    except Exception:
        return None


# ── Reactive (B-track): fuller know-how + surprise classification ─────────────
def tool_knowhow(name: str, *, procedure_first: bool = False,
                 max_chars: int = _KNOWHOW_MAX_CHARS) -> Optional[str]:
    """Fuller learned know-how (pitfalls + procedure + checks) for a failed tool, or None.

    `procedure_first` puts the procedure ahead of the pitfalls -- used for a KNOWN pitfall, where
    the model already saw the pitfall (the A-track put it in the schema) and the new value on the
    retry is how to call the tool correctly. Gated + fail-safe.
    """
    try:
        rec = _gated(name)
        if not rec:
            return None
        raw = [p for p in ((rec.get("tuatea") or {}).get("pitfalls") or [])
               if not store.is_vacuous_pitfall(p)]
        pits = _texts(raw, 3)
        tuarua = rec.get("tuarua") or {}
        proc = _texts(tuarua.get("procedure"), 4)
        checks = _texts(tuarua.get("verification"), 2)
        blocks = []
        pit_b = ("Pitfalls: " + " ; ".join(pits)) if pits else None
        proc_b = ("Correct procedure: " + " ; ".join(proc)) if proc else None
        check_b = ("Verify: " + " ; ".join(checks)) if checks else None
        order = [proc_b, pit_b, check_b] if procedure_first else [pit_b, proc_b, check_b]
        blocks = [b for b in order if b]
        if not blocks:
            return None
        return _cap("Learned tool know-how (from practice). " + " | ".join(blocks), max_chars)
    except Exception:
        return None


def known_pitfall_hit(name: str, error_text: str) -> bool:
    """True if the runtime error corresponds to a learned pitfall for this tool (so the agent hit
    something we already know about), False otherwise. Pass the RAW, uncompressed error string.
    Fail-safe."""
    try:
        rec = _gated(name)
        if not rec:
            return False
        err = _norm(error_text).lower()
        if not err:
            return False
        etoks = set(re.findall(r"[a-z0-9']{3,}", err))
        for p in (rec.get("tuatea") or {}).get("pitfalls") or []:
            pt = _norm(p.get("text") if isinstance(p, dict) else p).lower()
            if not pt:
                continue
            if err in pt:                       # the exact error is quoted in the pitfall
                return True
            if etoks:
                hit = sum(1 for t in etoks if t in pt)
                if hit / len(etoks) >= 0.6:     # most of the error's content words are in the pitfall
                    return True
        return False
    except Exception:
        return False
