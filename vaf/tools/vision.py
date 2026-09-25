# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""analyze_image - on-demand vision inspection of an image: one attached to the chat, or an
image file the caller may read.

The main reasoning model is text-only: when a user attaches an image it is described
once and that description is injected as VISUAL CONTEXT. When the model needs more than
the description covers - exact colours, positions, small text, locating a specific
object - it calls this tool with a targeted prompt. The tool re-reads the raw image
(persisted in the session) and runs a fresh, focused vision pass, returning text.

It is also how the agent looks at an image it made ITSELF: a screenshot a command took, a
chart it exported, a render it produced. Those used to be reachable only inside the chat's
workspace, by a hand-built check; a screenshot saved anywhere else in the person's own files
could not be looked at at all, and `read_file` handed the model the raw bytes. The file is
now held to the same per-user READ boundary `read_file` uses (`file_access = "read"`,
installed around run() by BaseTool), so what the person may read, the agent may look at.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

from vaf.tools.base import BaseTool


class AnalyzeImageTool(BaseTool):
    name = "analyze_image"
    category    = "documents"
    identity_kwargs = ("user_role", "user_scope_id")
    file_access = "read"
    description = (
        "Take a closer, targeted look at an image: one the user attached to this chat, OR an "
        "image FILE you can read - a screenshot you took, a chart you exported, a render you "
        "produced (pass its path in `image_path`; a relative path means this chat's workspace). "
        "Use it whenever you need detail the VISUAL CONTEXT description doesn't already cover: "
        "exact colours, exact positions/layout, reading small or partial text, counting items, "
        "or finding a specific object ('is there a red ball?', 'what does the small status line say?'). "
        "You do NOT see images directly: this tool is how you look. Pass a precise question in `prompt`."
    )
    permission_level = "read"
    side_effect_class = "none"

    input_examples: List[Dict] = [
        {"prompt": "What exact color is the send button in the bottom-right corner?"},
        {"prompt": "Read the small status line at the top verbatim."},
        {"prompt": "Are all three scenario lines labeled and readable?", "image_path": "chart.png"},
    ]

    # Extensions the vision pipeline can ingest from disk.
    _IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The specific question to answer about the image (be precise).",
            },
            "image": {
                "type": "string",
                "description": (
                    "Optional: which ATTACHED image to look at: a filename substring (e.g. 'screenshot') "
                    "or a 0-based index. Defaults to the most recently attached image."
                ),
            },
            "image_path": {
                "type": "string",
                "description": (
                    "Optional: an image FILE to inspect - an absolute path in your files "
                    "(a screenshot, a render), or a path relative to this chat's workspace "
                    "(e.g. 'chart.png' or the exported path from python_sandbox). The same "
                    "files read_file may read."
                ),
            },
        },
        "required": ["prompt"],
    }

    def run(self, **kwargs) -> str:
        prompt = (kwargs.get("prompt") or "").strip()
        if not prompt:
            return "Error: analyze_image needs a `prompt` describing what to look at."

        session_id = (kwargs.get("session_id") or "").strip()
        if not session_id:
            try:
                from vaf.core.subagent_ipc import get_current_session_id
                session_id = (get_current_session_id() or "").strip()
            except Exception:
                session_id = ""

        # File lane: inspect an image file (a chart exported via python_sandbox, a
        # screenshot). Held to the caller's READ jail, the one read_file obeys - an
        # arbitrary host path would let a remote user exfiltrate foreign files through
        # the vision model's description. A relative path means THIS chat's workspace,
        # keyed on the dispatcher-injected session id. Fail-closed.
        image_path = str(kwargs.get("image_path") or "").strip()
        if image_path:
            target = self._image_from_path(image_path, session_id)
            if isinstance(target, str):
                return target
            from vaf.core.config import Config
            from vaf.core.vision_infer import vision_infer
            _max = int(Config.get("vision_description_max_tokens", 1024) or 1024)
            result = vision_infer([target], prompt, max_tokens=_max)
            if not result:
                return (
                    "Vision analysis is unavailable (no vision model configured or the call failed). "
                    "Configure a Vision Model in Settings → AI & Model, or switch to a vision-capable provider."
                )
            return f"[analyze_image · `{target.get('name', 'image')}`]\n{result}"

        # Prefer the LIVE agent history: on the turn the image is uploaded it lives in
        # agent.history but is not persisted to disk until the turn ends, so a disk-only
        # read would miss it. Fall back to disk by session_id (covers images that aged
        # out of history via compaction but are still persisted).
        images = self._images_from_history(getattr(kwargs.get("_agent"), "history", None))
        if not images and session_id:
            images = self._collect_session_images(session_id)
        if not images and not session_id:
            return "Error: no active session: analyze_image needs a chat session with an attached image."
        if not images:
            return (
                "No image is attached to this conversation. Ask the user to attach one, "
                "then call analyze_image again."
            )

        target = self._select_image(images, kwargs.get("image"))
        if target is None:
            names = ", ".join(f"`{im.get('name', 'image')}`" for im in images)
            return f"Could not match image '{kwargs.get('image')}'. Available image(s): {names}."

        from vaf.core.config import Config
        from vaf.core.vision_infer import vision_infer
        _max = int(Config.get("vision_description_max_tokens", 1024) or 1024)
        result = vision_infer([target], prompt, max_tokens=_max)
        if not result:
            return (
                "Vision analysis is unavailable (no vision model configured or the call failed). "
                "Configure a Vision Model in Settings → AI & Model, or switch to a vision-capable provider."
            )
        name = target.get("name", "image")
        return f"[analyze_image · `{name}`]\n{result}"

    @classmethod
    def _image_from_path(cls, image_path: str, session_id: str):
        """Resolve `image_path` to a vision-ingestible dict, held to the caller's read boundary.

        Returns the image dict on success or an ERROR STRING for the model. A relative path
        resolves against this chat's workspace; an absolute one is taken as given. Either way
        `is_safe_path` decides, which is the per-user READ jail BaseTool installed around
        run() (plus the protected program and system locations) - the one `read_file` obeys.
        Missing, not a file, or not an image: refused (fail-closed)."""
        from pathlib import Path

        from vaf.tools.filesystem import is_safe_path

        p = Path(os.path.expanduser(str(image_path)))
        if not p.is_absolute():
            ws = None
            if session_id:
                try:
                    from vaf.core.session import get_session_workspace_dir
                    ws = get_session_workspace_dir(session_id, create=False)
                except Exception:
                    ws = None
            if not ws:
                return (
                    "Error: a relative image_path means this chat's workspace, and this chat "
                    "has none yet. Pass the image's absolute path."
                )
            p = Path(ws) / image_path
        safe, resolved = is_safe_path(str(p))
        if not safe:
            return resolved
        p = Path(resolved)
        if not p.is_file():
            return f"Error: image file not found: {p}"
        if p.suffix.lower() not in cls._IMAGE_SUFFIXES:
            return (
                f"Error: '{p.name}' is not an image file this tool can inspect "
                f"(supported: {', '.join(cls._IMAGE_SUFFIXES)})."
            )
        mime = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        }[p.suffix.lower()]
        return {"name": p.name, "mime_type": mime, "path": str(p)}

    @staticmethod
    def _images_from_history(history) -> List[Dict]:
        """Attached images from the LIVE agent history (oldest→newest), incl. the image
        attached on the current turn (which is not yet persisted to disk)."""
        out: List[Dict] = []
        for m in history or []:
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            for img in m.get("images") or []:
                if isinstance(img, dict) and (img.get("data") or img.get("path")):
                    out.append(img)
        return out

    @staticmethod
    def _collect_session_images(session_id: str) -> List[Dict]:
        """All attached images across the session, oldest→newest (each {name, mime_type, data|path})."""
        try:
            from vaf.core.session import SessionManager
            session = SessionManager().load(session_id)
        except Exception:
            return []
        out: List[Dict] = []
        for m in getattr(session, "messages", None) or []:
            if getattr(m, "role", None) != "user":
                continue
            imgs = (getattr(m, "metadata", None) or {}).get("images")
            if imgs:
                out.extend(img for img in imgs if isinstance(img, dict) and (img.get("data") or img.get("path")))
        return out

    @staticmethod
    def _select_image(images: List[Dict], selector: Optional[str]) -> Optional[Dict]:
        """Pick an image: by 0-based index, by filename substring, else the most recent one."""
        if not selector or not str(selector).strip():
            return images[-1]
        sel = str(selector).strip()
        if sel.isdigit():
            i = int(sel)
            return images[i] if 0 <= i < len(images) else None
        low = sel.lower()
        for img in reversed(images):  # most recent match wins
            if low in (img.get("name", "") or "").lower():
                return img
        return None
