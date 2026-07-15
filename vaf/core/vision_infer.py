# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""On-demand vision inference — turn an image into text via the configured vision backend.

This is the single choke point for "describe / answer about this image", used by:
  * the one-time *base description* generated when an image is first attached
    (so follow-up text turns stay grounded without re-sending the raw image), and
  * the ``analyze_image`` tool (targeted, on-demand re-analysis the agent triggers
    when it needs detail the base description doesn't cover).

The main reasoning model is kept text-only: it never receives raw image bytes.
Vision is a separate, on-demand service that returns text.

Backend selection (first match wins):
  1. ``vision_provider`` / ``vision_model`` from config (explicit override),
  2. the main provider, if it is vision-capable (e.g. veyllo, anthropic, gpt-4o),
  3. a safe per-provider default vision model,
  4. otherwise ``None`` — no vision backend available.

Design: this runs in the hot path and must never break a turn. It never raises;
on any problem it returns ``None`` so callers degrade gracefully.
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

# Comprehensive, deliberately NEUTRAL prompt for the one-time base description. Kept
# question-agnostic so every later turn (and the analyze_image tool) shares a stable,
# objective grounding instead of an answer shaped by the first question.
BASE_DESCRIPTION_PROMPT = (
    "Describe this image comprehensively and objectively for someone who cannot see it. "
    "Cover: the overall type/layout, ALL visible text (verbatim where short), the UI "
    "elements or objects present, their colours, and their spatial arrangement (what is "
    "where), plus anything notable or unusual. Do not interpret intent or give advice — "
    "only describe what is visibly there."
)


def _model_supports_vision(provider: str, model: str) -> bool:
    """Whether (provider, model) accepts image input.

    Mirror of the same helper in agent.py and browser_agent.py — kept in sync manually
    (a tiny pure function; a shared import would pull the 10k-line agent module into core).
    """
    provider = (provider or "").lower()
    model = (model or "").lower()
    if provider in ("anthropic", "google", "veyllo"):
        return True
    if provider == "openai":
        return any(k in model for k in ("gpt-4o", "gpt-4-turbo", "gpt-4-vision", "o1", "o3"))
    if provider == "deepseek":
        return False  # api.deepseek.com rejects image_url content blocks
    if provider == "openrouter":
        return any(k in model for k in ("gpt-4o", "claude-3", "gemini", "vision", "vl", "llava", "pixtral"))
    if provider == "local":
        # Honest capability: with vision_provider=local the llama server is
        # launched with the mmproj projector (backend.resolve_mmproj_for) and
        # reports image support on /v1/models; without it every image call
        # would burn a round-trip + retries into the sentinel error. Probe
        # defensively; an unreachable server counts as capable so the normal
        # lazy-load/error path stays intact (mirrors stay optimistic - this
        # is the default description path's gate).
        try:
            import requests as _rq
            r = _rq.get("http://127.0.0.1:8080/v1/models", timeout=2)
            if r.status_code == 200:
                return "multimodal" in r.text.lower() or "image" in r.text.lower()
        except Exception:
            pass
        return True
    return True  # unknown: let the model decide


def _default_vision_model(provider: str) -> Optional[str]:
    """Safe default vision model for an explicit vision_provider without a vision_model."""
    from vaf.core.config import Config
    defaults = {
        "openai": Config.get_default_model("openai"),
        "anthropic": Config.get_default_model("anthropic"),
        "google": Config.get_default_model("google"),
        "veyllo": Config.get_default_model("veyllo"),
        "openrouter": "openai/gpt-4o",  # explicit vision-capable route
    }
    return defaults.get(provider)


def select_vision_backend() -> Tuple[Optional[str], Optional[str]]:
    """Return ``(provider, model)`` to use for vision, or ``(None, None)`` if none is available."""
    from vaf.core.config import Config
    cfg = Config.load()

    # 1. Explicit override.
    vp = (Config.get("vision_provider", "") or "").strip()
    if vp:
        vm = (Config.get("vision_model", "") or "").strip() or _default_vision_model(vp)
        return vp, vm

    # 2. Main provider, if it can see images.
    mp = (Config.get("provider", "local") or "local").strip()
    mm = (cfg.get(f"api_model_{mp}", "") if mp != "local" else cfg.get("model", "")) or ""
    if _model_supports_vision(mp, mm):
        return mp, (mm or None)

    # 3. Nothing configured that can see.
    return None, None


def vision_available() -> bool:
    """True if some vision backend is configured/derivable (cheap; no network)."""
    try:
        provider, _ = select_vision_backend()
        return bool(provider)
    except Exception:
        return False


def image_to_b64(img: Dict) -> Optional[Tuple[str, str]]:
    """Return ``(base64_without_prefix, mime_type)`` for an image dict, or ``None``.

    Source order: inline ``img["data"]`` (legacy base64; a ``data:...;base64,`` prefix is
    stripped), else the file at ``img["path"]`` (the current on-disk storage). Never raises —
    returns ``None`` if neither is usable (e.g. the file was removed), so callers degrade
    gracefully. This is the single accessor every byte consumer should use.
    """
    if not isinstance(img, dict):
        return None
    mime = img.get("mime_type") or "image/jpeg"
    raw = img.get("data") or ""
    if raw:
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[1] if "," in raw else raw
        return raw, mime
    path = img.get("path")
    if path:
        try:
            import base64
            from pathlib import Path
            data = Path(path).read_bytes()
            if data:
                return base64.b64encode(data).decode("ascii"), mime
        except Exception as e:
            _log.debug("image_to_b64: cannot read %s: %s", path, e)
    return None


def vision_infer(
    images: List[Dict],
    prompt: str,
    *,
    max_tokens: int = 1024,
    temperature: float = 0.2,
) -> Optional[str]:
    """Run vision on one or more images with a custom prompt; return the text, or ``None``.

    Args:
        images: ``[{"data": base64, "mime_type": str, "name": str}]`` — a leading
            ``data:...;base64,`` URI on ``data`` is tolerated. Oversized images are
            downscaled (vision_image_max_edge / vision_image_jpeg_quality).
        prompt: what to ask the vision model about the image(s).
        max_tokens: output bound for the vision response.

    Never raises; returns ``None`` if no vision backend is available or the call fails.
    """
    if not images or not (prompt or "").strip():
        return None
    try:
        from vaf.core.api_backend import APIBackendManager
        from vaf.core.config import Config
        from vaf.core.image_utils import downscale_image_b64

        provider, model = select_vision_backend()
        if not provider:
            return None

        max_edge = int(Config.get("vision_image_max_edge", 2000) or 2000)
        quality = int(Config.get("vision_image_jpeg_quality", 85) or 85)

        blocks: List[Dict] = [{"type": "text", "text": prompt}]
        for img in images:
            got = image_to_b64(img)
            if not got:
                continue
            raw, mime = got
            raw, mime = downscale_image_b64(raw, mime, max_edge, quality)
            blocks.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{raw}"}})
        if len(blocks) == 1:
            return None  # no usable image bytes (e.g. file gone)

        backend = APIBackendManager(provider)
        text = ""
        saw_error = False
        for chunk in backend.chat_completion(
            [{"role": "user", "content": blocks}],
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            model=model,
            tools=None,
        ):
            if isinstance(chunk, dict):
                text += chunk.get("content") or ""
                continue
            if not isinstance(chunk, str):
                continue
            # Providers DON'T raise on a backend failure — they yield a sentinel string
            # ("[API Error from <provider>: …]"). Treat that as a failure (return None) so a
            # poisoned error string never becomes an image description; a later turn can retry.
            if "[API Error from" in chunk:
                saw_error = True
                continue
            # Skip streamed control payloads the OpenAI-compatible backend yields alongside
            # content (tool_calls / finish_reason JSON) — same guard as BaseTool.query_llm.
            _s = chunk.strip()
            if _s.startswith("{") and any(k in chunk for k in ("tool_calls", "tool_use", "finish_reason")):
                continue
            text += chunk

        if saw_error:
            _domain_log(f"[VISION_INFER] {provider}/{model or 'default'} backend error — no description")
            return None
        text = _strip_reasoning(text)
        _domain_log(f"[VISION_INFER] {provider}/{model or 'default'} imgs={len(images)} -> {len(text)} chars")
        return text or None
    except Exception as e:  # never break the caller's turn
        _log.debug("vision_infer failed: %s", e)
        _domain_log(f"[VISION_INFER] FAILED: {e}")
        return None


# Process-wide memo of one-time image descriptions, keyed by image identity (resolved file
# path, else a hash of the inline bytes). Shared by the chat-upload base-description path
# (agent._ensure_image_base_descriptions) and the Image Viewer's /api/image/describe — both run
# in the same process (the headless agent is a thread inside the web server) — so the SAME image
# is described only ONCE, even in the race where the viewer is opened mid-turn. Bounded; lost on
# restart (regenerated; the persisted caches still apply). The per-key lock is held across the
# (slow) vision call so a concurrent request for the same image waits and reuses it.
_DESC_CACHE: Dict[str, str] = {}
_DESC_CACHE_MAX = 256
_DESC_KEY_LOCKS: Dict[str, "threading.Lock"] = {}
_DESC_GUARD = threading.Lock()


def _desc_cache_key(image: Dict) -> Optional[str]:
    if not isinstance(image, dict):
        return None
    path = image.get("path")
    if path:
        try:
            from pathlib import Path
            return "p:" + str(Path(path).resolve())
        except Exception:
            return "p:" + str(path)
    data = image.get("data") or ""
    if data:
        import hashlib
        return "d:" + hashlib.sha1(data[:4096].encode("utf-8", "ignore")).hexdigest()
    return None


def describe_image_cached(image: Dict, *, max_tokens: Optional[int] = None) -> Optional[str]:
    """One-time base description of a single image, memoised process-wide by image identity.

    Use this (instead of calling vision_infer directly) wherever the *base description* is
    needed — the chat-upload path and the Image Viewer share this cache, so the same image is
    never described (and billed) twice. Returns None if no vision backend / the call fails.
    """
    if max_tokens is None:
        try:
            from vaf.core.config import Config
            max_tokens = int(Config.get("vision_description_max_tokens", 1024) or 1024)
        except Exception:
            max_tokens = 1024

    key = _desc_cache_key(image)
    if not key:
        return vision_infer([image], BASE_DESCRIPTION_PROMPT, max_tokens=max_tokens)

    cached = _DESC_CACHE.get(key)
    if cached is not None:
        return cached
    with _DESC_GUARD:
        klock = _DESC_KEY_LOCKS.setdefault(key, threading.Lock())
    with klock:
        cached = _DESC_CACHE.get(key)
        if cached is not None:
            return cached
        desc = vision_infer([image], BASE_DESCRIPTION_PROMPT, max_tokens=max_tokens)
        if desc:
            _DESC_CACHE[key] = desc
            if len(_DESC_CACHE) > _DESC_CACHE_MAX:
                try:
                    _DESC_CACHE.pop(next(iter(_DESC_CACHE)))
                except Exception:
                    pass
        return desc


def build_visual_context_text(images: List[Dict], user_text: str = "") -> str:
    """Build the text block that replaces a raw image for the (text-only) main model.

    Each image becomes its persisted base description (or a clear "unavailable" marker),
    followed by an instruction to call ``analyze_image`` for anything the description
    doesn't cover. The original user text (if any) is appended after the context.
    """
    parts: List[str] = []
    for img in images or []:
        if not isinstance(img, dict):
            continue
        name = img.get("name") or "image"
        desc = (img.get("base_description") or "").strip()
        loc = ""
        path = img.get("path")
        if path:
            import os as _os
            loc = f" (file: attachments/{_os.path.basename(path)})"
        if desc:
            parts.append(f"[VISUAL CONTEXT — analysis of the attached image `{name}`{loc}:]\n{desc}")
        else:
            parts.append(f"[Attached image `{name}`{loc} — automatic analysis is unavailable.]")
    hint = (
        "(You cannot see the raw image directly — the text above IS your view of it, treat it as "
        "ground truth. The image file is saved in this chat's attachments/ folder; for anything the "
        "description doesn't cover — exact colours, positions, small text, or locating a specific "
        "object — call analyze_image(prompt=…), or read the file directly.)"
    )
    block = "\n\n".join(parts + [hint])
    return (block + "\n\n" + user_text).strip() if user_text else block


def _strip_reasoning(text: str) -> str:
    """Remove reasoning-model ``<think>…</think>`` blocks (and stray markers) from a
    vision response so chain-of-thought never lands in a stored image description."""
    if not text:
        return ""
    import re
    text = re.sub(r"<think>[\s\S]*?</think>", "", text)
    text = re.sub(r"</?think>", "", text)
    return text.strip()


def _domain_log(msg: str) -> None:
    try:
        from vaf.core.log_helper import append_domain_log
        append_domain_log("backend", msg)
    except Exception:
        pass
