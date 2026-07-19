# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Dedicated voice-agent model lane (server side).

When `voice_agent_provider` is "local", the live call runs on its OWN GGUF
(default: Gemma 4 E4B - chosen for natural spoken German; see
docs/agents/VOICE_AGENT.md) instead of the main model. The single-server
invariant stays untouched: the ONE llama server SWAPS models - the voice
model holds it during the call, the main model takes it back while a
delegated task runs (`backend.ensure_local_model` is model-aware and
serialized). Never two servers, never two concurrent local inferences.

This module owns: the recommended default ref, download + swap of the voice
model, and the async kick used by `voice_call_start` (with a `model_state`
push so the frontend's existing self-heal re-opens the call when ready).
"""
import logging
import os
import threading

_log = logging.getLogger(__name__)

# Recommended dedicated voice model: Gemma 4 E4B (Apache 2.0). Verified
# 2026-07: third-party German evaluations rate its spoken German noticeably
# more natural than Qwen 3.5's, it has no thinking preamble to fight, and
# llama.cpp b8746+ runs the E-series GGUFs. ~5.4 GB download.
DEFAULT_VOICE_MODEL = "bartowski/google_gemma-4-E4B-it-GGUF/google_gemma-4-E4B-it-Q4_K_M.gguf"

_ENSURE_THREAD_LOCK = threading.Lock()
_ensure_running = False

_DOWNLOAD_THREAD_LOCK = threading.Lock()
_download_running = False


def voice_model_ref() -> str:
    """The configured dedicated voice model ref (config, else the default)."""
    from vaf.core.config import Config
    return (Config.get("voice_agent_model", "") or "").strip() or DEFAULT_VOICE_MODEL


def voice_model_path(download: bool = True) -> str:
    """Local filesystem path of the voice GGUF; downloads it on first use via
    the shared single download entry point (file lock + WebUI progress
    banner + self-heal come for free). Returns "" on failure."""
    from vaf.core.backend import ensure_model_available
    try:
        ref = voice_model_ref()
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        models_dir = os.path.join(base_dir, "models")
        if not download:
            from vaf.core.backend import _resolve_model_ref
            _, filename = _resolve_model_ref(ref)
            candidate = os.path.join(models_dir, filename or "")
            return candidate if filename and os.path.exists(candidate) else ""
        return ensure_model_available(ref, models_dir) or ""
    except Exception as e:
        _log.warning("voice_model: resolving the voice model failed: %s", e)
        return ""


def voice_model_loaded() -> bool:
    """True when the one llama server currently holds the voice model."""
    from vaf.core.backend import get_loaded_model_id, _resolve_model_ref
    loaded = get_loaded_model_id()
    if not loaded:
        return False
    _, filename = _resolve_model_ref(voice_model_ref())
    return bool(filename) and loaded.lower() == filename.lower()


def ensure_voice_model(reason: str = "voice call") -> bool:
    """Blocking: download if needed, then swap the one server to the voice
    model. Safe to call per turn - a matching server returns immediately."""
    from vaf.core.backend import ensure_local_model
    path = voice_model_path(download=True)
    if not path:
        return False
    # skip_provider_gate: a dedicated local voice lane is legitimate even
    # when the MAIN provider is an API (then the llama server serves ONLY
    # the call and no swap-back ever happens).
    return ensure_local_model(path, reason=reason, skip_provider_gate=True)


def ensure_voice_model_downloaded_async() -> None:
    """Non-blocking: fetch the dedicated voice GGUF to disk if it isn't there
    yet, WITHOUT swapping the running server (used when the user picks the
    voice model in Settings, so the recommended Gemma default is fetched at
    selection instead of only lazily at the first call). The WebUI download
    banner comes for free from the shared download entry point
    (`ensure_model_available` -> `_download` broadcasts progress). No-op when
    the file is already present; at most one download runs at a time.

    This is deliberately download-only: at save time the live server may hold
    the MAIN model and no call is in progress, so it must NOT swap - that is
    what `ensure_voice_model` (call time) does."""
    global _download_running
    # Fast path: already on disk -> no thread, no download, and never a swap.
    try:
        if voice_model_path(download=False):
            return
    except Exception:
        pass
    with _DOWNLOAD_THREAD_LOCK:
        if _download_running:
            return
        _download_running = True

    def _work():
        global _download_running
        try:
            voice_model_path(download=True)  # download only; no ensure_local_model swap
        except Exception as e:
            _log.warning("voice_model: async voice-model download failed: %s", e)
        finally:
            with _DOWNLOAD_THREAD_LOCK:
                _download_running = False

    threading.Thread(target=_work, name="voice-model-download", daemon=True).start()


def ensure_voice_model_async(on_ready=None) -> None:
    """Non-blocking kick for `voice_call_start` (the WS handler must not
    freeze behind a download or swap). At most one ensure runs at a time;
    `on_ready(ok: bool)` fires from the worker thread when it finishes."""
    global _ensure_running
    with _ENSURE_THREAD_LOCK:
        if _ensure_running:
            return
        _ensure_running = True

    def _work():
        global _ensure_running
        ok = False
        try:
            ok = ensure_voice_model(reason="voice call start")
        except Exception as e:
            _log.warning("voice_model: async ensure failed: %s", e)
        finally:
            with _ENSURE_THREAD_LOCK:
                _ensure_running = False
            if on_ready is not None:
                try:
                    on_ready(ok)
                except Exception:
                    pass

    threading.Thread(target=_work, name="voice-model-ensure", daemon=True).start()
