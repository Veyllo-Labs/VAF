# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Discord reply hook: headless_runner calls send_discord_reply(channel_id, text)
when a task has source=discord. The bridge registers a callback that enqueues
the reply for its sender thread to post to the Discord API.
"""
from typing import Callable, Optional

_send_callback: Optional[Callable[..., None]] = None


def set_discord_reply_callback(cb: Optional[Callable[..., None]]) -> None:
    """Register a callback (channel_id, text). Called by the Discord bridge."""
    global _send_callback
    _send_callback = cb


def send_discord_reply(channel_id: str, text: str) -> None:
    """If a Discord reply callback is registered, invoke it. Used by headless_runner and send_discord tool."""
    try:
        from vaf.core.log_helper import log_discord_reply
        log_discord_reply(
            f"REPLY channel_id={channel_id} len={len(text)} callback={_send_callback is not None}"
        )
    except Exception:
        pass
    if _send_callback and channel_id and text:
        try:
            _send_callback(channel_id, text)
        except Exception:
            pass
