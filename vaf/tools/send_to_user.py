# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Channel-agnostic proactive delivery to the session owner.

Thin wrapper over the one canonical "reach the user on their main channel"
helper (vaf/core/messaging_connections.send_to_main_messenger): the concrete
platform (Telegram/WhatsApp/Discord) is resolved at RUN time from the user's
main_messenger identity field, never chosen by the model. This is the channel
model's rule side: workflows and automations teach ONE delivery step; adding a
messaging platform means extending the router adapter, not any prompt.

Delivery contract (inherited from the router): the text send decides success,
a file attachment is best-effort and sent separately. When no messenger is
reachable the tool never drops the content silently: it posts a Web UI
notification preview and reports the fallback honestly in its result string.
"""

import re

from vaf.tools.base import BaseTool
from vaf.tools.send_telegram import _resolve_path


class SendToUserTool(BaseTool):
    """
    Deliver a message (and optionally a produced file) to the session owner on
    their configured main messenger, whichever platform that is.
    """
    name = "send_to_user"
    permission_level = "write"
    side_effect_class = "irreversible"
    channel_restrictions = ()
    admin_only = False
    description = (
        "Send a message to the user on their configured main messenger (channel-agnostic: "
        "resolves Telegram/WhatsApp/Discord at runtime from main_messenger). "
        "Preferred delivery step for automations/workflows and whenever the user did not name a platform. "
        "Use a platform tool (send_telegram, ...) ONLY when the user explicitly asked for that platform. "
        "Optionally attaches a file (best-effort). Falls back to a Web UI notification when no messenger is connected."
    )
    input_examples = [
        {"message": "Your report is ready."},
        {"message": "Weather summary: 18-24 C, 10% rain.", "file_path": "/home/user/Documents/report.html"},
    ]
    parameters = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The final, user-facing message text. Sent VERBATIM - no LLM processes it after this call.",
            },
            "file_path": {
                "type": "string",
                "description": "Optional. Full path to a produced file (report, PDF, HTML, ...) to attach. Attachment is best-effort; the message itself always takes priority.",
            },
        },
        "required": ["message"],
    }

    def run(self, **kwargs) -> str:
        message = (kwargs.get("message") or "").strip()
        if not message:
            return "No message provided. Pass the final user-facing message text to send."

        username = kwargs.get("username") or "admin"
        user_scope_id = kwargs.get("user_scope_id")

        # Same outgoing hygiene as the per-platform send tools: this wrapper must
        # not become the one unfiltered outbound lane.
        out = re.sub(r"<think>.*?</think>", "", message, flags=re.DOTALL)
        out = re.sub(r"\n{3,}", "\n\n", out).strip()
        try:
            from vaf.core.headless_runner import _sanitize_outgoing_message
            out = _sanitize_outgoing_message(out)
        except Exception:
            pass
        if not out:
            return (
                "Message was blocked (contained internal system content). "
                "Send a clean user-facing message without any internal context markers."
            )

        # Attachment is best-effort by contract: a missing file must not kill the
        # text delivery, but an unsafe path is refused outright.
        attach = None
        attach_warning = ""
        file_path_str = (kwargs.get("file_path") or "").strip()
        if file_path_str:
            resolved, path_error = _resolve_path(file_path_str)
            if path_error:
                return path_error
            if resolved and resolved.is_file():
                attach = str(resolved)
            else:
                attach_warning = f" WARNING: attachment skipped - file not found: {file_path_str}"

        try:
            from vaf.core.messaging_connections import send_to_main_messenger
        except ImportError as e:
            return f"Messenger delivery unavailable: {e}"

        sent, channel = send_to_main_messenger(user_scope_id, username, out, file_path=attach)
        if sent and channel:
            label = {"telegram": "Telegram", "whatsapp": "WhatsApp", "discord": "Discord"}.get(channel, channel)
            if attach:
                import os as _os
                return f"Message and document {_os.path.basename(attach)} sent to the user via {label}." + attach_warning
            return f"Message sent to the user via {label}." + attach_warning

        # No messenger reachable (main_messenger unset, channel not connected, or the
        # send failed). Never drop silently: surface a preview in the Web UI
        # notifications, scoped to this user at the emit site.
        try:
            from vaf.core.user_notifications import append_notification
            preview = (out[:400] + "...") if len(out) > 400 else out
            append_notification(
                user_scope_id,
                kind="channel_reply",
                title="Message for you (no messenger connected)",
                status="skipped",
                summary=preview,
            )
        except Exception:
            pass
        return (
            "Could not deliver via messenger: no main_messenger configured, its channel is not "
            "connected, or the send failed. A preview was posted to the user's Web UI notifications."
            " If you are in an interactive chat, present the content directly in your reply."
            + attach_warning
        )
