"""
Last user interaction store for system prompt context.
Persists timestamp, channel (web/telegram/cli), and short preview per user_scope_id.
Used to inject "Last user interaction: X min ago via Telegram; currently in WebUI" into the system prompt.
"""
from pathlib import Path
from typing import Any, Dict, Optional
import json
import time
import re

from vaf.core.platform import Platform


PREVIEW_MAX_CHARS = 80
FILENAME = "last_interaction.json"


def _store_path() -> Path:
    """OS-independent path for last_interaction.json (data dir)."""
    return Platform.data_dir() / FILENAME


def _key(user_scope_id: Any) -> str:
    """Normalize key for storage (support multi-user)."""
    if user_scope_id is None:
        return "default"
    return str(user_scope_id).strip()


def _sanitize_preview(text: str, max_len: int = PREVIEW_MAX_CHARS) -> str:
    """Single line, no leading/trailing whitespace, truncated."""
    if not text:
        return ""
    one_line = re.sub(r"\s+", " ", str(text).strip())
    return (one_line[:max_len] + "…") if len(one_line) > max_len else one_line


def update_last_interaction(
    user_scope_id: Optional[Any] = None,
    source: str = "web",
    preview: str = "",
) -> None:
    """
    Record the last user interaction for the given user (or default).
    Call when a user message is about to be processed (e.g. in headless before chat_step).

    Args:
        user_scope_id: User scope (UUID or None for single-user).
        source: Channel: "web", "telegram", or "cli".
        preview: Short preview of the user message (will be sanitized and truncated to PREVIEW_MAX_CHARS).
    """
    try:
        path = _store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data: Dict[str, Dict[str, Any]] = {}
        if path.exists():
            raw = path.read_text(encoding="utf-8")
            if raw.strip():
                data = json.loads(raw)
        key = _key(user_scope_id)
        data[key] = {
            "ts": time.time(),
            "source": str(source).strip().lower() or "web",
            "preview": _sanitize_preview(preview),
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def get_last_interaction(
    user_scope_id: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """
    Return the last recorded interaction for the given user, or None.

    Returns:
        Dict with "ts" (float), "source" (str), "preview" (str), or None if missing.
    """
    try:
        path = _store_path()
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return None
        data = json.loads(raw)
        key = _key(user_scope_id)
        entry = data.get(key)
        if not entry or not isinstance(entry, dict):
            return None
        ts = entry.get("ts")
        if ts is None:
            return None
        return {
            "ts": float(ts),
            "source": str(entry.get("source", "web")),
            "preview": str(entry.get("preview", "")),
        }
    except Exception:
        return None
