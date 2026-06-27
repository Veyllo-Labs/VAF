# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""analyze_image — on-demand vision inspection of an image attached to the chat.

The main reasoning model is text-only: when a user attaches an image it is described
once and that description is injected as VISUAL CONTEXT. When the model needs more than
the description covers — exact colours, positions, small text, locating a specific
object — it calls this tool with a targeted prompt. The tool re-reads the raw image
(persisted in the session) and runs a fresh, focused vision pass, returning text.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from vaf.tools.base import BaseTool


class AnalyzeImageTool(BaseTool):
    name = "analyze_image"
    description = (
        "Take a closer, targeted look at an image the user attached to this chat. "
        "Use it whenever you need detail the VISUAL CONTEXT description doesn't already cover: "
        "exact colours, exact positions/layout, reading small or partial text, counting items, "
        "or finding a specific object ('is there a red ball?', 'what does the small status line say?'). "
        "You do NOT see images directly — this tool is how you look. Pass a precise question in `prompt`."
    )
    permission_level = "read"
    side_effect_class = "none"

    input_examples: List[Dict] = [
        {"prompt": "What exact color is the send button in the bottom-right corner?"},
        {"prompt": "Read the small status line at the top verbatim."},
    ]

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
                    "Optional: which image to look at — a filename substring (e.g. 'screenshot') "
                    "or a 0-based index. Defaults to the most recently attached image."
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
        # Prefer the LIVE agent history: on the turn the image is uploaded it lives in
        # agent.history but is not persisted to disk until the turn ends, so a disk-only
        # read would miss it. Fall back to disk by session_id (covers images that aged
        # out of history via compaction but are still persisted).
        images = self._images_from_history(getattr(kwargs.get("_agent"), "history", None))
        if not images and session_id:
            images = self._collect_session_images(session_id)
        if not images and not session_id:
            return "Error: no active session — analyze_image needs a chat session with an attached image."
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
