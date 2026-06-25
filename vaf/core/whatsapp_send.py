# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
WhatsApp send utilities: text chunking for Baileys.
WhatsApp message limit: ~65536 bytes per message; we chunk at 4000 chars (like OpenClaw) for readability.
Actual send goes through the Node bridge (stdin to wa-bridge.js).
"""
import logging
from typing import List

logger = logging.getLogger("vaf.core.whatsapp_send")

WHATSAPP_CHUNK_LIMIT = 4000


def chunk_whatsapp_text(text: str, max_chars: int = WHATSAPP_CHUNK_LIMIT) -> List[str]:
    """
    Split text into WhatsApp-safe chunks. Tries to break at newlines when possible.
    """
    if not text or not text.strip():
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: List[str] = []
    lines = text.split("\n")
    current = ""
    current_len = 0

    for line in lines:
        line_with_nl = line + "\n"
        next_len = current_len + len(line_with_nl)

        if next_len <= max_chars:
            current += line_with_nl
            current_len = next_len
            continue

        # Would exceed - flush current
        if current:
            chunks.append(current.rstrip("\n"))
            current = ""
            current_len = 0

        # If single line is too long, split by size
        if len(line) > max_chars:
            rest = line
            while len(rest) > max_chars:
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
                current_len = len(current)
        else:
            current = line_with_nl
            current_len = len(current)

    if current:
        chunks.append(current.rstrip("\n"))

    return chunks
