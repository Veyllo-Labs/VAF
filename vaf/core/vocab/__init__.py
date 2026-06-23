"""VAF Vocabulary Book — multilingual canned phrases for backend-generated agent messages.

The frontend i18n (next-intl) localizes UI strings only; backend/CLI text is explicitly out of scope
there. This module is the backend equivalent for short FIXED phrases the agent emits itself — e.g. the
thinking-run "are you there?" nudge — so they can vary and follow the user's language without an LLM call
at runtime.

Phrasings live as JSON data files (one per phrase key) under `data/`, e.g. `data/nudge.json`:

    { "en": ["Hey {name}, you around?", ...], "de": ["Hey {name}, bist du da?", ...], ... }

The data files can be (re)generated / expanded to many languages with `scripts/generate_vocab.py`.
"""
from __future__ import annotations

import json
import random
import threading
from pathlib import Path
from typing import Optional, Dict, List

_DATA_DIR = Path(__file__).resolve().parent / "data"
_cache: Dict[str, Dict[str, List[str]]] = {}
_cache_lock = threading.Lock()
_last_pick: Dict[tuple, int] = {}  # (scope, key) -> last index, to rotate so consecutive picks differ


def _load(key: str) -> Dict[str, List[str]]:
    """Load and cache the phrasings for `key` (lang -> list of phrasings)."""
    with _cache_lock:
        if key in _cache:
            return _cache[key]
        data: Dict[str, List[str]] = {}
        try:
            p = _DATA_DIR / f"{key}.json"
            if p.is_file():
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = {
                        str(lang).strip().lower(): [str(s) for s in items if str(s).strip()]
                        for lang, items in raw.items()
                        if isinstance(items, list)
                    }
                    data = {k: v for k, v in data.items() if v}
        except Exception:
            data = {}
        _cache[key] = data
        return data


def _norm_lang(lang: Optional[str]) -> str:
    """Normalize 'de-DE'/'de_DE'/'DE' -> 'de'."""
    s = str(lang or "").strip().lower().replace("_", "-")
    return s.split("-")[0] if s else ""


def pick(key: str, lang: Optional[str], scope: Optional[str] = None, **fmt) -> str:
    """Pick one phrasing for `key` in `lang`, rotating so consecutive picks for the same (scope, key)
    differ. Language fallback: exact base language -> 'en' -> first available. Formats `{placeholders}`
    from **fmt (a missing placeholder returns the raw phrasing instead of raising). Returns '' only when
    `key` has no phrasings at all."""
    data = _load(key)
    if not data:
        return ""
    base = _norm_lang(lang)
    phrasings = data.get(base) or data.get("en") or next(iter(data.values()), [])
    if not phrasings:
        return ""
    idx = random.randrange(len(phrasings))
    if len(phrasings) > 1:
        ck = (str(scope or ""), key)
        prev = _last_pick.get(ck)
        tries = 0
        while idx == prev and tries < 5:
            idx = random.randrange(len(phrasings))
            tries += 1
        _last_pick[ck] = idx
    text = phrasings[idx]
    try:
        return text.format(**fmt)
    except Exception:
        return text


def available_languages(key: str) -> List[str]:
    """Languages a phrase key currently has phrasings for (for the generation script / diagnostics)."""
    return sorted(_load(key).keys())


def resolve_user_language(user_scope_id: Optional[str] = None, username: Optional[str] = None) -> str:
    """Best-effort 2-letter language for the user: user_identity.preferred_language -> config
    `default_language` -> 'en'. Never raises."""
    try:
        from vaf.auth.user_workspace import get_user_workspace
        uname = (username or "").strip() or None
        if uname:
            ident = get_user_workspace(uname).get_user_identity() or {}
            lang = _norm_lang(ident.get("preferred_language"))
            if lang:
                return lang
    except Exception:
        pass
    try:
        from vaf.core.config import Config
        lang = _norm_lang(Config.get("default_language", "") or "")
        if lang:
            return lang
    except Exception:
        pass
    return "en"
