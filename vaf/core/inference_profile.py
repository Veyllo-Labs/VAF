# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Which inference this installation runs itself: all of it, or none of it.

``inference_profile`` is chosen when VAF is set up (``vaf profile``, ``vaf server
provision --profile``, the installers) and is one of:

- ``local`` (the default, and what every installation ran before the key existed): the
  chat model, the speech containers and the voice model can run on this machine.
- ``api``: no chat, voice or speech model runs here. Chat, speech-to-text and
  text-to-speech go to the providers set under Settings; none of those models is
  downloaded and the speech containers are not started. Two small CPU models stay (see
  below). What a small rented server (a VPS) can carry.

Measured before this existed, on the desktop that wrote it: the speech-to-text image is
7.53 GB on disk with a 2 GB memory limit, the text-to-speech image 1.13 GB with another
2 GB, and the chat model 2.7 to 17.9 GB depending on the GPU. The compose memory limits
add up to about 8.3 GB with them and about 4.3 GB without. With an API provider the chat
model was already loaded only on demand, but three settings still pulled a local model
onto the machine: a voice lane, a thinking or sub-agent lane on ``local``, and the local
failover link.

The profile is enforced at the places that exist once each, never by checks spread over
the code:

- ``backend.ensure_model_available``, the one download entry for a model;
- ``ServerManager.start_server``, the one start of the local server (also when the voice
  lane bypasses the provider gate);
- ``Agent.load_model``, the one local backend entry of an agent;
- ``service_stack.needed_services``, the one list of the services to run, read by the
  stack start, the status, the repair and the installers;
- the speech clients' local fallback (``speech_api.resolve_stt_engine``,
  ``speech_client.transcribe``, ``SpeechManager.synthesize_audio`` and the Piper lane of
  ``SpeechManager.speak``);
- the failover chain, whose local link is left out (``APIBackendManager``);
- a running agent and a running server: ``Agent.chat_step`` and ``Agent.complete`` (with
  the shared ``completion.complete``) refuse a local backend that was loaded before a
  switch, and the tray unloads the local model as it does for an API provider; an
  in-process model is released by the switch (``agent.release_disallowed_local_backends``).

A setting that names a local lane while the profile is ``api`` is not refused when it is
written: ``config.json`` stays editable by hand, and the choke points above are the
guarantee. ``conflicts()`` names such settings for ``vaf doctor`` and the status.

What stays local in both profiles, deliberately: the database, the cache, the document
conversion, the browser (an agent tool, not a model), the sandbox environments, the
embedding model of the memory (130 MB on the CPU; every stored memory is a vector of
it, so an API embedding would mean re-embedding everything and sending every memory
out), and the speaker recognition (27 MB). A text-to-speech server the admin runs
elsewhere (``speech_tts_engine = chatterbox``) and the macOS voice (``system``) download
nothing here and stay usable.

Not on the facade: an embedder chooses the provider per agent; this is a rule about what
an installation of the product carries.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("vaf.core.inference_profile")

LOCAL = "local"
API = "api"
PROFILES = (LOCAL, API)
DEFAULT = LOCAL


def current(config: Optional[Dict] = None) -> str:
    """The profile of this installation: ``api`` when the value, trimmed and ignoring
    case, is ``api``, else ``local``. An unreadable or mistyped value must not take the
    local models away from an installation that has run on them."""
    try:
        if config is None:
            from vaf.core.config import Config
            value = Config.get("inference_profile", DEFAULT)
        else:
            value = config.get("inference_profile", DEFAULT)
        return API if str(value or "").strip().lower() == API else LOCAL
    except Exception:
        return DEFAULT


def local_inference_allowed(config: Optional[Dict] = None) -> bool:
    return current(config) == LOCAL


def refusal(what: str = "a local model") -> str:
    """The sentence every choke point says, so a person sees one reason in one wording."""
    return (f"This installation runs without local models (profile \"api\"), so {what} is "
            "not used here. Choose an AI provider under Settings > AI & Model, or switch "
            "the profile with `vaf profile local`.")


def refuse(what: str = "a local model") -> str:
    """Log the refusal once per call site and return its text."""
    text = refusal(what)
    logger.info(text)
    return text


def validate(profile: str, prune: bool = False) -> str:
    """The profile ``apply`` would switch to, or ValueError for an unknown profile or
    ``prune`` without ``api``. Nothing is written: a caller that runs ``apply`` in the
    background (the web switch) refuses a wrong request here, before it starts."""
    wanted = str(profile or "").strip().lower()
    if wanted not in PROFILES:
        raise ValueError(f"Unknown profile {profile!r}: use local or api.")
    if prune and wanted != API:
        raise ValueError("Pruning goes with api: it removes what the local profile runs.")
    return wanted


def download_guard(interval: float = 2.0) -> Callable[[], None]:
    """For a model download's progress hook: a callable that raises InterruptedError once
    the profile is ``api``, reading it at most every ``interval`` seconds. The check at the
    start of a download cannot see a switch made while it runs, and a chat model is up to
    17.9 GB: without this the download finished, on exactly the small server the switch was
    for. Every download lane passes one (the automatic one in ``backend``, the web
    settings, the terminal settings)."""
    last = [0.0]

    def check() -> None:
        now = time.monotonic()
        if now - last[0] < interval:
            return
        last[0] = now
        if not local_inference_allowed():
            raise InterruptedError(refusal("downloading a local model"))

    return check


def without_profile_key(payload: Dict) -> Dict:
    """A Settings save without ``inference_profile``. The key changes only through
    ``apply``, which also stops or starts the speech containers; a save sends the whole
    page, and a page loaded before a switch (another tab, another admin, the terminal, or
    the switch still running) would write the old profile back without touching a
    container. Both save lanes pass it: the WebSocket ``save_config`` and
    ``PATCH /api/config``."""
    if isinstance(payload, dict) and "inference_profile" in payload:
        return {k: v for k, v in payload.items() if k != "inference_profile"}
    return payload


def apply(profile: str, *, prune: bool = False, stack: bool = True) -> Dict:
    """Switch the installation to ``profile`` - the one implementation behind `vaf profile`
    and the web settings. Writes the key, then (unless ``stack`` is False, the installers'
    case) stops the speech containers for ``api`` (``prune`` also removes their containers,
    images and volumes) or starts the stack with them for ``local``.

    The key is written first, deliberately, and stays written when the stop fails: every
    choke point above reads it, so downloads and the local server are refused from that
    moment, and keeping the old profile because Docker did not answer would leave them
    open too. A stop, removal or start that failed is in ``errors``; callers treat a non-empty
    ``errors`` as an incomplete switch (`vaf profile` exits 1, the web switch reports it).

    Raises ValueError as ``validate`` does. Returns
    {"profile", "previous", "stopped", "started", "removed", "errors", "conflicts"}."""
    wanted = validate(profile, prune)
    from pathlib import Path

    from filelock import FileLock

    from vaf.core.config import Config
    # One switch at a time across processes (the web switch, `vaf profile`, an installer):
    # two interleaved switches could leave the key on one profile and the containers on
    # the other. The second one waits and then runs whole, so the last switch wins.
    app_dir = Path(Config.APP_DIR)
    app_dir.mkdir(parents=True, exist_ok=True)      # a fresh install may not have it yet
    with FileLock(str(app_dir / "inference_profile.lock")):
        return _apply_locked(wanted, prune, stack)


def _apply_locked(wanted: str, prune: bool, stack: bool) -> Dict:
    from vaf.core.config import Config
    previous = current()
    Config.set("inference_profile", wanted)
    out: Dict = {"profile": wanted, "previous": previous, "stopped": False, "started": False,
                 "removed": [], "errors": [], "conflicts": []}
    if stack:
        from vaf.core import service_stack
        if wanted == API:
            _release_in_process_models()
            result = service_stack.retire_local_inference_services(prune=prune)
            out.update({"stopped": result["stopped"], "removed": result["removed"],
                        "errors": result["errors"]})
        else:
            out["started"] = bool(service_stack.ensure_service_stack())
            # Without a compose file (a pip install) there is no stack to start, which is
            # no failure; with one, a start that did not come up leaves the switch
            # incomplete: the speech containers stay down until a repair or the next launch.
            if not out["started"] and service_stack.find_stack_root() is not None:
                out["errors"].append("The service stack did not start; `vaf repair` names "
                                     "why and starts it.")
    out["conflicts"] = conflicts()
    return out


def _release_in_process_models() -> None:
    """The in-process models of this process's live agents (agent.py). Only where agents
    exist: `vaf profile` in a terminal has none and must not import the engine for it. A
    switch made in another process is picked up there at the next refused turn
    (headless_runner)."""
    import sys
    agent_module = sys.modules.get("vaf.core.agent")
    if agent_module is not None:
        agent_module.release_disallowed_local_backends()


def conflicts(config: Optional[Dict] = None) -> List[str]:
    """Settings that name a local lane while the profile is ``api``, one sentence each.
    Empty under ``local``. Read by ``vaf doctor`` and the status; nothing is refused."""
    try:
        if config is None:
            from vaf.core.config import Config
            config = Config.load()
    except Exception:
        return []
    if local_inference_allowed(config):
        return []

    def _v(key: str, default: str = "") -> str:
        return str(config.get(key, default) or default).strip().lower()

    out: List[str] = []
    if _v("provider", LOCAL) == LOCAL:
        out.append("The chat provider is the local model: choose an AI provider under "
                   "Settings > AI & Model.")
    for key, label in (("vision_provider", "image understanding"),
                       ("voice_agent_provider", "the voice call"),
                       ("browser_agent_provider", "the browser agent"),
                       ("thinking_provider", "the thinking runs"),
                       ("subagent_provider", "the sub-agents")):
        if _v(key) == LOCAL:
            out.append(f"{key} is \"local\": {label} would need a local model.")
    if _v("failover_level", "off") == "basic":
        out.append("failover_level is \"basic\", whose only link is the local model: it adds "
                   "nothing here (\"balanced\" keeps a backup API provider).")
    stt_on = config.get("speech_stt_enabled") or config.get("stt_enabled")
    if stt_on and not _v("speech_stt_provider"):
        out.append("Speech-to-text is on without a provider (speech_stt_provider): there is "
                   "no local engine to fall back to here.")
    if config.get("speech_tts_enabled") and not _v("speech_tts_provider") \
            and _v("speech_tts_engine", "docker") in ("docker", "piper"):
        out.append("Text-to-speech is on without a provider (speech_tts_provider): there is "
                   "no local engine to fall back to here.")
    return out
