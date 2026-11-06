# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Send a message to the user's own paired Veyllo App devices.

No recipient parameter: the message goes to the devices of the account the turn runs for
(``identity_kwargs``), so it never reaches a third party and the outward hold has nothing to
hold (vaf/core/outbound_hold.py).
"""
from pathlib import Path

from vaf.tools.base import BaseTool
from vaf.tools.send_telegram import _resolve_path


class SendVeylloAppTool(BaseTool):
    """Send a message to the user on their Veyllo App, optionally with a file."""
    name = "send_veyllo_app"
    category = "veyllo_app"
    identity_kwargs = ("user_role", "user_scope_id", "username")
    # "write", not "read": the mode names the ROOT SET a file may come from (see send_discord).
    file_access = "write"
    permission_level = "write"
    side_effect_class = "irreversible"
    description = (
        "Send a message to the user on their Veyllo App (their phone). "
        "Use when the user asked to get something on the app or their main messenger is the "
        "Veyllo App. A phone that is not connected right now gets it when it connects again. "
        "For a document, pass file_path with the full path to the file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string",
                        "description": "The message text to send to the user's Veyllo App."},
            "file_path": {"type": "string",
                          "description": "Optional. Full path to a file to send with it."},
        },
        "required": ["message"],
    }

    def run(self, **kwargs) -> str:
        message = (kwargs.get("message") or "").strip()
        if not message:
            return "No message provided. Pass the message text to send."
        file_path_str = (kwargs.get("file_path") or "").strip()
        file_path: Path | None = None
        if file_path_str:
            resolved, path_error = _resolve_path(file_path_str)
            if path_error:
                return path_error
            file_path = resolved
        if file_path and not file_path.is_file():
            return f"File not found or not a file: {file_path}"
        scope = kwargs.get("user_scope_id")
        username = kwargs.get("username") or ""
        from vaf.core.app_link import devices
        from vaf.core.messaging_connections import channel_enabled_for_scope
        if not scope or not devices.for_scope(scope):
            return ("No Veyllo App is paired for this account. The user pairs one in Settings, "
                    "Connections, Veyllo App.")
        if not channel_enabled_for_scope("veyllo_app", scope):
            return "The Veyllo App is switched off for this account in Settings, Connections."
        try:
            from vaf.core.outbound_sanitizer import sanitize_outgoing_message
            out = sanitize_outgoing_message(message)
        except Exception:
            out = message
        if not out:
            return ("Message was blocked (contained internal system content). Send a clean "
                    "user-facing message without any internal context markers.")
        from vaf.core.app_link import bridge
        if not bridge.deliver(scope, username, out, str(file_path) if file_path else None):
            return "The message could not be stored for the Veyllo App."
        try:
            from vaf.core.messaging_connections import _record_outbound
            _record_outbound("veyllo_app", devices.chat_key(scope), out, username, scope,
                             str(file_path) if file_path else None)
        except Exception:
            pass
        reached = len(bridge.connected_devices(scope))
        if reached:
            return "Message sent to the Veyllo App."
        return "Message stored for the Veyllo App; the phone gets it when it connects again."
