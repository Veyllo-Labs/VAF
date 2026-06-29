# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Discord send utilities: REST API helpers and text chunking.
Discord message limit: 2000 characters. We chunk long messages and preserve code blocks.
"""
import json
import logging
import os
from typing import List, Optional

import requests

logger = logging.getLogger("vaf.core.discord_send")

DISCORD_API = "https://discord.com/api/v10"
DISCORD_MAX_CHARS = 2000
DISCORD_MAX_LINES = 17  # Soft limit; very tall messages get clipped in Discord UI


def _chunk_discord_text(text: str, max_chars: int = DISCORD_MAX_CHARS, max_lines: int = DISCORD_MAX_LINES) -> List[str]:
    """
    Split text into Discord-safe chunks (max 2000 chars, ~17 lines per message).
    Tries to break at newlines; within long lines breaks at whitespace.
    """
    if not text or not text.strip():
        return []
    if len(text) <= max_chars and text.count("\n") + 1 <= max_lines:
        return [text]

    chunks: List[str] = []
    lines = text.split("\n")
    current = ""
    current_lines = 0

    for line in lines:
        line_with_nl = line + "\n"
        next_len = len(current) + len(line_with_nl)
        next_lines = current_lines + 1

        if next_len <= max_chars and next_lines <= max_lines:
            current += line_with_nl
            current_lines = next_lines
            continue

        # Would exceed - flush current if we have something
        if current:
            chunks.append(current.rstrip("\n"))
            current = ""
            current_lines = 0

        # If single line is too long, split it
        if len(line) > max_chars:
            rest = line
            while len(rest) > max_chars:
                # Try to break at whitespace
                chunk = rest[:max_chars]
                break_at = chunk.rfind(" ")
                if break_at > max_chars // 2:
                    chunk = rest[: break_at + 1]
                    rest = rest[break_at + 1 :]
                else:
                    rest = rest[max_chars:]
                chunks.append(chunk)
            if rest:
                current = rest + "\n"
                current_lines = 1
        else:
            current = line_with_nl
            current_lines = 1

    if current:
        chunks.append(current.rstrip("\n"))

    return chunks


def _get_dm_channel_id(bot_token: str, user_id: str) -> Optional[str]:
    """Create or get DM channel with user. Returns channel_id or None."""
    url = f"{DISCORD_API}/users/@me/channels"
    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
    payload = {"recipient_id": str(user_id)}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if not resp.ok:
            logger.warning("Discord create DM failed: %s %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        return data.get("id")
    except Exception as e:
        logger.warning("Discord create DM error: %s", e)
        return None


def send_discord_message(
    bot_token: str,
    channel_id: str,
    text: str,
    *,
    chunk: bool = True,
    file_path: Optional[str] = None,
) -> bool:
    """
    Send a message to a Discord channel (DM or guild channel).
    Automatically chunks if text exceeds 2000 chars.
    When ``file_path`` is given and the file exists, the file is uploaded as an attachment via a
    single multipart request (content truncated to the 2000-char limit) — used to deliver an
    automation's output document to the user's main channel.
    Returns True on success.
    """
    if not bot_token or not channel_id:
        return False

    # Attachment path: one multipart POST with the file + (truncated) content.
    if file_path and os.path.isfile(file_path):
        url = f"{DISCORD_API}/channels/{channel_id}/messages"
        headers = {"Authorization": f"Bot {bot_token}"}  # no Content-Type: requests sets multipart
        payload = {"content": (text or "")[:DISCORD_MAX_CHARS]}
        try:
            with open(file_path, "rb") as fh:
                resp = requests.post(
                    url,
                    headers=headers,
                    data={"payload_json": json.dumps(payload)},
                    files={"files[0]": (os.path.basename(file_path), fh)},
                    timeout=60,
                )
            if not resp.ok:
                logger.warning("Discord file send failed: %s %s", resp.status_code, resp.text[:200])
                return False
            return True
        except Exception as e:
            logger.warning("Discord file send error: %s", e)
            return False

    if not text:
        return False

    texts: List[str] = []
    if chunk:
        texts = _chunk_discord_text(text)
    if not texts:
        texts = [text[:DISCORD_MAX_CHARS]]

    url_base = f"{DISCORD_API}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}

    for i, part in enumerate(texts):
        payload = {"content": part}
        try:
            resp = requests.post(url_base, json=payload, headers=headers, timeout=15)
            if not resp.ok:
                logger.warning(
                    "Discord send failed (part %d/%d): %s %s",
                    i + 1,
                    len(texts),
                    resp.status_code,
                    resp.text[:200],
                )
                return False
        except Exception as e:
            logger.warning("Discord send error (part %d): %s", i + 1, e)
            return False

    return True


def send_discord_dm(
    bot_token: str,
    user_id: str,
    text: str,
    *,
    chunk: bool = True,
    file_path: Optional[str] = None,
) -> bool:
    """
    Send a DM to a Discord user. Resolves DM channel from user_id, then sends.
    ``file_path`` (optional) uploads a document attachment to the DM.
    Returns True on success.
    """
    channel_id = _get_dm_channel_id(bot_token, user_id)
    if not channel_id:
        return False
    return send_discord_message(bot_token, channel_id, text, chunk=chunk, file_path=file_path)
