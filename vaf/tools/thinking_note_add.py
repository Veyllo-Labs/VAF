# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Thinking-note-add tool: only available when VAF_THINKING_MODE=1.

The agent calls this to persist a note for its next thinking run —
e.g. "User confirmed Yasin birthday automation is handled, do not ask again"
or "User wants to keep Daily calendar check, do not suggest deleting it".

Notes are stored in a per-user SQLite DB (Platform.data_dir()/thinking_notes.db)
and are injected into the system prompt at the start of every subsequent thinking run.
They auto-expire after 30 days.
"""
import os

from vaf.tools.base import BaseTool


class ThinkingNoteAddTool(BaseTool):
    """Persist a note for your next thinking run (Thinking Mode only)."""

    name = "thinking_note_add"
    permission_level = "system"
    side_effect_class = "reversible"
    description = (
        "Save a persistent note for your next thinking run. "
        "Use this to remember context, user decisions, or things to avoid asking again. "
        "Good examples: "
        "'User confirmed Yasin birthday automation is fully handled — do not mention it again', "
        "'User wants to keep Daily calendar check, it is intentional', "
        "'User said Git setup is not needed for now'. "
        "Notes are shown at the start of every future thinking pass and expire after 30 days."
    )
    parameters = {
        "type": "object",
        "properties": {
            "note": {
                "type": "string",
                "description": (
                    "The note to remember. Be specific and self-contained so it is useful "
                    "without extra context. Max 500 characters."
                ),
            },
            "user_scope_id": {
                "type": "string",
                "description": "Internal: user scope id injected by the framework. Leave blank.",
            },
        },
        "required": ["note"],
    }

    def run(self, **kwargs) -> str:
        note = (kwargs.get("note") or "").strip()
        if not note:
            return "Error: note must not be empty."

        # Resolve scope_key the same way thinking_mode.py does
        user_scope_id = kwargs.get("user_scope_id") or os.environ.get("VAF_THINKING_SCOPE_ID") or None
        try:
            from vaf.core.thinking_mode import _key
            scope_key = _key(user_scope_id)
        except Exception:
            scope_key = str(user_scope_id).strip() if user_scope_id else "default"

        try:
            from vaf.core.thinking_notes import add_note
            add_note(scope_key, note)
            return f"Note saved: {note[:120]}{'...' if len(note) > 120 else ''}"
        except Exception as exc:
            return f"Error saving note: {exc}"
