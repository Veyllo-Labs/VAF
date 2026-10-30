# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Which provider and model does each job use: the one answer, for every lane.

A job (a "role") is everything VAF sends to a model besides the chat itself: reading
images, text recognition in scanned pages, the browser agent, the sub-agents, the
background thinking runs, the voice call, speech in both directions, and the failover
backup. Each has its own setting, and an empty one (``""`` or ``inherit``) follows the
main ``provider``. That rule existed before this module, but it was decided in eight
places by hand: four copies for the thinking runs alone, two settings screens that
printed a sub-agent provider whose gate was off, a TypeScript copy of the vision
capability and a hand-written list of the role keys. A screen that summarised it
would have been a ninth copy, and the first to disagree with the backend.

Now every lane asks ``resolve_role`` (directly, or through its old entry point, which
is a one-line delegation: ``vision_infer.select_vision_backend``,
``voice_agent._resolve_backend``, ``browser_agent._build_vaf_bridge``,
``thinking_mode.resolve_thinking_provider``), and so do the settings overview
(``POST /api/config/model-roles``) and ``vaf providers``. The overview cannot disagree
with the backend because both call the same function; tests/test_model_roles.py
compares every entry point with its row.

Rules that are decisions, not accidents:

- **No silent fallback to another vendor.** When the main provider cannot serve a role
  (DeepSeek reading images), the role is ``unavailable`` with the reason, and nothing
  sends the user's images to a vendor nobody chose for them.
- **Speech and failover never follow the main provider.** An empty speech provider has
  always meant the local speech engine (following would start sending recorded voice
  to a cloud API on every OpenAI install), and the failover backup exists to differ
  from the main provider.
- **The thinking row reports the model the run really uses.** ``thinking_model`` is
  read only on the local path today, so for an API thinking provider the row names
  ``api_model_<provider>`` and the reason ``thinking_model_ignored``.

``config=None`` reads the installation's configuration through ``Config.get``, exactly
as the lanes did. A mapping is read with ``Config.get``'s default rule
(``config.config_reader``). API key presence is always the machine's, because keys
never travel in a config dict. ``resolve_role`` never raises: it sits on spawn and turn
paths, and an unreadable configuration yields an ``unavailable`` row.

Named boundary: not on the ``vaf`` facade. The role lanes read the MACHINE
configuration (``Config.get`` / ``Config.load``), never an embedded Agent's
``config=``: ``select_vision_backend`` has four callers and none passes the agent's
config, and the browser bridge, the voice lane, the thinking run and the sub-agent
spawn read the machine's too. A public ``vaf.model_roles(config)`` would promise a
per-agent answer those lanes cannot keep. The ``config`` parameter is the seam for the
change that makes them read the agent's config; the export belongs to that change
(docs/EMBEDDING.md, Configuration).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Tuple

from vaf.core import config as _config


@dataclass(frozen=True)
class Role:
    """One job, the setting that changes it and where that setting lives."""
    name: str
    setting: str                         # the config key that changes it
    tab: str                             # the settings tab holding it: ai | voice | advanced
    follows_main: bool                   # an empty setting means the main provider
    local_conflict_label: Optional[str]  # what inference_profile.conflicts calls the job


# THE list of roles, in the order every view shows them.
ROLES: Tuple[Role, ...] = (
    Role("chat", "provider", "ai", True, None),
    Role("vision", "vision_provider", "ai", True, "image understanding"),
    Role("ocr", "ocr_engine", "ai", True, None),
    Role("browser_agent", "browser_agent_provider", "ai", True, "the browser agent"),
    Role("subagents", "subagent_provider", "ai", True, "the sub-agents"),
    Role("thinking", "thinking_provider", "ai", True, "the thinking runs"),
    Role("voice_call", "voice_agent_provider", "voice", True, "the voice call"),
    Role("speech_to_text", "speech_stt_provider", "voice", False, None),
    Role("text_to_speech", "speech_tts_provider", "voice", False, None),
    Role("failover", "failover_level", "advanced", False, None),
)
_BY_NAME: Dict[str, Role] = {r.name: r for r in ROLES}

# Every key a settings screen can change that the resolver reads: the allowlist of an
# unsaved draft (POST /api/config/model-roles). Read but deliberately absent:
# `inference_profile` and `local_engine` (machine decisions with their own commands,
# `vaf profile` and `vaf engine`), every key, URL and secret.
ROLE_CONFIG_KEYS: FrozenSet[str] = frozenset(
    ("provider", "model")
    + tuple(f"api_model_{p}" for p in _config.PROVIDER_MODELS)
    + ("vision_provider", "vision_model", "ocr_engine",
       "browser_agent_provider", "browser_agent_model")
    + _config.SUBAGENT_PROVIDER_KEYS
    + ("thinking_provider", "thinking_model",
       "voice_agent_provider", "voice_agent_model",
       "speech_stt_enabled", "stt_enabled", "speech_stt_provider", "speech_stt_api_model",
       "speech_stt_engine",
       "speech_tts_enabled", "speech_tts_provider", "speech_tts_api_model",
       "speech_tts_api_voice", "speech_tts_engine",
       "failover_level", "failover_backup_provider", "failover_backup_model",
       "failover_local_model")
)

# The failover level and the links it appends after the primary, in order.
FAILOVER_LINKS: Dict[str, List[str]] = {
    "off": [],
    "basic": ["local"],
    "balanced": ["backup", "local"],
    "maximum": ["backup", "local"],
}

# A closed set: the web overview translates each code.
REASON_TEXT: Dict[str, str] = {
    "no_api_key": "No API key is set for this provider.",
    "main_cannot_see": "The main provider cannot read images; choose a vision provider.",
    "ocr_no_engine": "Neither Tesseract nor a vision model is available.",
    "browser_described": "This model cannot see; screenshots are described by the vision model.",
    "browser_dom_only": "No model here can see; the browser agent reads the page text only.",
    "choice_inactive": ("A sub-agent provider is stored but not switched on; save \"inherit\" "
                        "once, then the provider, to use it."),
    "thinking_model_ignored": "thinking_model is used for the local model only; this provider's own model runs.",
    "vllm_voice_api_main": "A local voice call under vLLM needs the local model as the main provider.",
    "no_local_models": "This installation runs without local models (profile \"api\").",
    "speech_provider_unusable": "The chosen speech provider is unknown, cannot do this, or has no API key.",
    "failover_no_link": "No backup link can be used (no backup provider with a key, no local model).",
    "config_unreadable": "The configuration could not be read.",
}

SOURCES = ("main", "follows_main", "set", "unavailable", "off", "local_engine", "via_vision")


@dataclass(frozen=True)
class RoleAssignment:
    """Who serves one role. ``provider`` None: nothing serves it, or an engine that is
    not a provider does (``source`` local_engine). ``model`` None: the provider's own
    default, or the engine decides. ``also``: further providers in order (the failover
    links after the first). ``also_reasons``: further reason codes behind ``reason``
    (a row that is both ``no_local_models`` and, say, ``choice_inactive``)."""
    role: str
    provider: Optional[str]
    model: Optional[str]
    source: str
    reason: str = ""
    setting: str = ""
    tab: str = "ai"
    also: Tuple[str, ...] = ()
    also_reasons: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        """The JSON shape of the API and of `vaf providers --json`."""
        return {"role": self.role, "provider": self.provider, "model": self.model,
                "source": self.source, "reason": self.reason, "setting": self.setting,
                "tab": self.tab, "also": list(self.also),
                "also_reasons": list(self.also_reasons)}


class _Reader:
    """The configuration one resolution reads, plus what several roles share."""

    def __init__(self, config: Optional[Mapping], warn: bool, probe_local: bool = True):
        self.config = config
        self.get = _config.config_reader(config)
        self.warn = warn
        self.probe_local = probe_local
        self._snap: Optional[Mapping] = None
        self.memo: Dict[str, RoleAssignment] = {}

    def text(self, key: str, default: str = "") -> str:
        return str(self.get(key, default) or default).strip()

    def snapshot(self) -> Mapping:
        """The whole configuration, as the lanes that read it whole (vision, browser) did."""
        if self._snap is None:
            cfg = _config.Config
            self._snap = cfg.load() if self.config is None else {**cfg.DEFAULTS, **self.config}
        return self._snap

    def main(self) -> str:
        return (self.text("provider", "local") or "local").lower()

    def has_key(self, provider: str) -> bool:
        try:
            return bool(_config.Config.get_api_key(provider))
        except Exception:
            return False


def _row(role: str, provider: Optional[str], model: Optional[str], source: str,
         reason: str = "", also: Tuple[str, ...] = (),
         also_reasons: Tuple[str, ...] = ()) -> RoleAssignment:
    spec = _BY_NAME[role]
    return RoleAssignment(role, provider, model, source, reason, spec.setting, spec.tab, also,
                          also_reasons)


def _api_model(r: _Reader, provider: str) -> Optional[str]:
    """``api_model_<provider>``, else the provider's catalog default: the model an agent
    on that provider sends when no lane names one."""
    try:
        return r.text(f"api_model_{provider}") or _config.Config.get_default_model(provider) or None
    except Exception:
        return None


def _local_model(r: _Reader, preferred: str = "") -> Optional[str]:
    """The model the local engine serves: the vLLM repo under vLLM, else the GGUF name."""
    try:
        from vaf.core import local_engine
        if local_engine.is_vllm(r.config):
            return local_engine.resolve_vllm_model(r.config)
    except Exception:
        pass
    return preferred or r.text("model") or None


def _model_for(r: _Reader, provider: str, local_preferred: str = "") -> Optional[str]:
    return _local_model(r, local_preferred) if provider == "local" else _api_model(r, provider)


# ── the roles ────────────────────────────────────────────────────────────────────────

def _chat(r: _Reader, explain: bool) -> RoleAssignment:
    p = r.main()
    reason = "" if p == "local" or r.has_key(p) else "no_api_key"
    return _row("chat", p, _model_for(r, p), "main", reason)


def _vision(r: _Reader, explain: bool) -> RoleAssignment:
    if "vision" in r.memo:
        return r.memo["vision"]
    from vaf.core.provider_registry import default_vision_model, model_supports_vision
    vp = r.text("vision_provider")
    if vp:
        vm = r.text("vision_model") or default_vision_model(vp)
        row = _row("vision", vp, vm, "set")
    else:
        # The main provider, if it can see images. The local probe (one GET of
        # 127.0.0.1:8080/v1/models, 2 s) is kept as the lane always had it; a hot path
        # that needs only an explicit choice passes probe_local=False.
        mp = r.text("provider", "local") or "local"
        snap = r.snapshot()
        mm = (snap.get(f"api_model_{mp}", "") if mp != "local" else snap.get("model", "")) or ""
        if model_supports_vision(mp, mm, probe_local=r.probe_local):
            row = _row("vision", mp, mm or None, "follows_main")
        else:
            row = _row("vision", None, None, "unavailable", "main_cannot_see")
    r.memo["vision"] = row
    return row


def _ocr(r: _Reader, explain: bool) -> RoleAssignment:
    from vaf.core.pdf_extract import resolve_ocr_engine

    # The vision row of THIS reading (a draft's, when one is previewed), resolved once.
    def _vision_backend():
        v = _vision(r, explain)
        return (v.provider, v.model) if v.provider else (None, None)
    engine, _why = resolve_ocr_engine(r.config, vision=_vision_backend)
    if engine == "tesseract":
        return _row("ocr", None, "tesseract", "local_engine")
    if engine == "vision":
        v = _vision(r, explain)
        return _row("ocr", v.provider, v.model, "via_vision")
    return _row("ocr", None, None, "unavailable", "ocr_no_engine")


def browser_lane(config: Optional[Mapping] = None) -> Tuple[str, Optional[str]]:
    """(lane provider, lane model) as stored for browser runs: "" provider = ride the
    main provider, None model = the provider's own config chain."""
    get = _config.config_reader(config)
    lane_provider = str(get("browser_agent_provider", "") or "").strip()
    lane_model = str(get("browser_agent_model", "") or "").strip()
    return lane_provider, (lane_model or None)


def _browser(r: _Reader, explain: bool) -> RoleAssignment:
    from vaf.core.provider_registry import (is_local_model_id, model_fits_provider,
                                            model_supports_vision, note_foreign_model)
    cfg = _config.Config
    snap = r.snapshot()
    lane_provider, lane_model = browser_lane(r.config)
    source = "set" if (lane_provider or lane_model) else "follows_main"
    provider = lane_provider or snap.get("provider", "local")

    # A lane model of another provider (browser_agent_model alone rides the MAIN
    # provider, which may have been switched since) is dropped for that provider's
    # own chain. A local name stays: the provider's chain below replaces it.
    if lane_model and provider != "local" and not model_fits_provider(provider, lane_model, snap):
        if r.warn and not is_local_model_id(lane_model):
            note_foreign_model(provider, lane_model, "browser_agent_model")
        lane_model = None

    if lane_model:
        model = lane_model
    elif provider == "local":
        model = snap.get("model", "llama3")
    else:
        model = snap.get(f"api_model_{provider}", "") or snap.get("model", "llama3")

    # Never pass a model that is not this provider's (a local GGUF name in `model`)
    # to a cloud API provider: the same predicate the API backend applies.
    if provider != "local" and not model_fits_provider(provider, model, snap):
        model = cfg.get_default_model(provider) or cfg.get_default_model("openai")

    reason = ""
    if explain:
        # How the run gets to see, as browser_agent._browser_vision_mode decides it
        # (probe_local=False, as on that hot path).
        try:
            sees = model_supports_vision(provider, model, probe_local=False)
        except Exception:
            sees = False
        if not sees:
            reason = "browser_described" if _vision(r, explain).provider else "browser_dom_only"
    return _row("browser_agent", provider, model, source, reason)


def _subagents(r: _Reader, explain: bool) -> RoleAssignment:
    from vaf.core.config import subagent_provider_override
    override = subagent_provider_override(r.config)
    p = override or r.main()
    reason = ""
    if not override and r.text("subagent_provider", "inherit").lower() not in ("", "inherit"):
        reason = "choice_inactive"
    return _row("subagents", p, _model_for(r, p), "set" if override else "follows_main", reason)


def _thinking(r: _Reader, explain: bool) -> RoleAssignment:
    configured = str(r.get("thinking_provider") or "inherit").strip().lower()
    if configured and configured != "inherit":
        p, source = configured, "set"
    else:
        p, source = r.main(), "follows_main"
    t_model = str(r.get("thinking_model") or "").strip()
    reason = "thinking_model_ignored" if (p != "local" and t_model) else ""
    return _row("thinking", p, _model_for(r, p, t_model), source, reason)


def _voice(r: _Reader, explain: bool) -> RoleAssignment:
    override = r.text("voice_agent_provider").lower()
    source = "set" if override else "follows_main"
    if override == "local":
        from vaf.core import local_engine
        if local_engine.is_vllm(r.config):
            # Under the vLLM engine the call rides the running vLLM model: a swap to a
            # voice GGUF would be a multi-minute vLLM restart (and a GGUF vLLM does not
            # run). That model is the main agent's; with an API main provider nothing
            # starts vLLM for a call, so the combination is refused here and named by
            # local_engine.conflicts().
            if r.main() != "local":
                return _row("voice_call", None, None, "unavailable", "vllm_voice_api_main")
            return _row("voice_call", "local", None, source)
        from vaf.core import voice_model
        return _row("voice_call", "local", voice_model.voice_model_ref(r.config), source)
    if override:
        if not r.has_key(override):
            return _row("voice_call", None, None, "unavailable", "no_api_key")
        return _row("voice_call", override, r.text("voice_agent_model") or None, source)
    provider = r.main()
    if provider == "local":
        return _row("voice_call", "local", None, source)
    if not r.has_key(provider):
        return _row("voice_call", None, None, "unavailable", "no_api_key")
    return _row("voice_call", provider, r.text(f"api_model_{provider}") or None, source)


def _speech(role: str, r: _Reader) -> RoleAssignment:
    from vaf.core import speech_api
    from vaf.core.inference_profile import local_inference_allowed
    stt = role == "speech_to_text"
    if stt:
        on = r.get("speech_stt_enabled") or r.get("stt_enabled")
    else:
        on = r.get("speech_tts_enabled")
    if not on:
        return _row(role, None, None, "off")
    picked = (speech_api.select_stt_backend(r.config, warn=r.warn) if stt
              else speech_api.select_tts_backend(r.config, warn=r.warn))
    if picked[0]:
        return _row(role, picked[0], picked[1], "set")
    stored = r.text("speech_stt_provider" if stt else "speech_tts_provider")
    reason = "speech_provider_unusable" if stored else ""
    engine = r.text("speech_stt_engine" if stt else "speech_tts_engine", "docker").lower()
    # Without local models only a text-to-speech engine that downloads nothing here
    # stays (a server run elsewhere, the macOS voice); inference_profile.conflicts names
    # the rest.
    if local_inference_allowed(r.config) or (not stt and engine not in ("docker", "piper")):
        return _row(role, None, engine or None, "local_engine", reason)
    return _row(role, None, None, "unavailable", reason or "no_local_models")


def failover_links(get: Callable[..., Any], primary: str,
                   has_key: Callable[[str], bool],
                   local_allowed: Callable[[], bool]) -> List[Tuple[str, Optional[str]]]:
    """The links the failover level appends after ``primary``, as (provider, model).

    Empty, duplicate and key-less links are dropped; the local link only where local
    models run at all. ``get(key, default)`` reads the failover keys (an agent's own
    config first in ``APIBackendManager``), ``has_key`` decides a backup provider's key
    (the caller's keys travel into the fallback too)."""
    level = str(get("failover_level", "off") or "off").lower()
    links: List[Tuple[str, Optional[str]]] = []
    seen = {primary}
    for kind in FAILOVER_LINKS.get(level, []):
        if kind == "backup":
            bp = str(get("failover_backup_provider", "") or "").strip()
            if not bp or bp == "local" or bp in seen or not has_key(bp):
                continue
            links.append((bp, str(get("failover_backup_model", "") or "").strip() or None))
            seen.add(bp)
        elif kind == "local":
            if "local" in seen or not local_allowed():
                continue
            links.append(("local", str(get("failover_local_model", "") or "").strip() or None))
            seen.add("local")
    return links


def _failover(r: _Reader, explain: bool) -> RoleAssignment:
    from vaf.core.inference_profile import local_inference_allowed
    level = r.text("failover_level", "off").lower() or "off"
    if not FAILOVER_LINKS.get(level):
        return _row("failover", None, None, "off")
    # With the local model as the main provider the chat loop talks to the local server
    # directly, but the lanes that reach it through the API backend (the browser agent)
    # still run this chain, so the row is the chain's answer, not "nothing".
    primary = r.main()
    links = failover_links(r.get, primary, r.has_key, lambda: local_inference_allowed(r.config))
    if not links:
        return _row("failover", None, None, "unavailable", "failover_no_link")
    (provider, model), rest = links[0], links[1:]
    return _row("failover", provider, model, "set", also=tuple(p for p, _ in rest))


_RESOLVERS: Dict[str, Callable[[_Reader, bool], RoleAssignment]] = {
    "chat": _chat,
    "vision": _vision,
    "ocr": _ocr,
    "browser_agent": _browser,
    "subagents": _subagents,
    "thinking": _thinking,
    "voice_call": _voice,
    "speech_to_text": lambda r, explain: _speech("speech_to_text", r),
    "text_to_speech": lambda r, explain: _speech("text_to_speech", r),
    "failover": _failover,
}


def _resolve(name: str, r: _Reader, explain: bool) -> RoleAssignment:
    try:
        row = _RESOLVERS[name](r, explain)
    except Exception:
        return _row(name, None, None, "unavailable", "config_unreadable")
    # The same facts inference_profile.conflicts names: a local lane while this
    # installation runs no local models. That is the reason that matters most; one the
    # row already had stays in also_reasons.
    if row.provider == "local" and row.source in ("main", "follows_main", "set"):
        try:
            from vaf.core.inference_profile import local_inference_allowed
            if not local_inference_allowed(r.config):
                earlier = (row.reason,) if row.reason else ()
                row = _row(name, row.provider, row.model, row.source, "no_local_models", row.also,
                           earlier + row.also_reasons)
        except Exception:
            pass
    return row


def resolve_role(role: str, config: Optional[Mapping] = None, *, warn: bool = True,
                 explain: bool = True, probe_local: bool = True) -> RoleAssignment:
    """Who serves ``role`` under ``config`` (None: the installation's configuration).

    ``warn`` False keeps the lanes' one-time warnings (a foreign lane model, an unusable
    speech provider) out of a preview. ``explain`` False skips the reason codes that
    cost a lookup of their own (the browser row's vision tier), for a hot path that
    needs only provider and model. ``probe_local`` False skips the one network touch,
    the vision row's probe of the local server (a local main then counts as able to
    see, as on the agent loop's hot path). Never raises for a known role; an unknown
    role name is a KeyError."""
    if role not in _RESOLVERS:
        raise KeyError(role)
    return _resolve(role, _Reader(config, warn, probe_local), explain)


def resolve_roles(config: Optional[Mapping] = None, *, warn: bool = False) -> List[RoleAssignment]:
    """Every role, in ``ROLES`` order: the overview. One reading of the configuration is
    shared, so the vision row (and its local probe) is resolved once; the OCR and
    browser rows reuse it."""
    r = _Reader(config, warn)
    return [_resolve(spec.name, r, True) for spec in ROLES]
