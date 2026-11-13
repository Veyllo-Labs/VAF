# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
from typing import Optional
from vaf.tools.base import BaseTool
from vaf.core.persistence import PersistenceManager
from vaf.tools.filesystem import tool_work_folder
import os


def _project_notes(base_dir, kwargs):
    """``(PersistenceManager, "")`` for the project's notes, or ``(None, refusal)``.

    `tool_work_folder` judged the project folder; the notes go into its ``.vaf``, which code
    in the project's sandbox can replace with a link to another account's folder. So the real
    path of that folder is asked the caller's file jail again, and every write is pinned to
    it (`PersistenceManager.notes_folder`); a ``.vaf`` that is a link is not used. The
    project folder itself is opened as a handle and compared with the text that was judged
    (``expect_folder``), so ``.vaf`` is never made in a folder swapped in after the check."""
    pm = PersistenceManager(base_dir, expect_folder=base_dir)
    folder = pm.notes_folder()
    if folder is None:
        return None, ("❌ Refused: the project's .vaf is a link or not a folder; project notes "
                      "are written only into the project's own .vaf folder.")
    from vaf.tools.filesystem import jail_allows
    if not jail_allows(folder, user_scope_id=kwargs.get("user_scope_id"),
                       user_role=kwargs.get("user_role"), mode="write"):
        return None, ("❌ Refused: the project's .vaf leads outside the folders this account "
                      "may write.")
    return pm, ""

class UpdateCodexTool(BaseTool):
    """
    Save a persistent engineering pattern, convention, or architectural decision to the project's Codex.
    Use this when you learn something important that should be remembered for future tasks.
    """
    name = "update_codex"
    # The folder rule in tool_work_folder judges base_dir for this caller.
    identity_kwargs = ("user_scope_id", "user_role")
    # It writes <base_dir>/.vaf/codex.md, which the coder reads back as its own context.
    agent_request = "refuse_standing"
    category    = "memory"
    permission_level = "write"
    side_effect_class = "irreversible"
    description = "Save a persistent pattern or convention to the project Codex (Long-term memory)."
    
    parameters = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short title for the pattern (e.g., 'Error Handling Strategy', 'React Component Structure')"
            },
            "content": {
                "type": "string",
                "description": "The detailed pattern, rule, or learning to save."
            },
            "base_dir": {
                "type": "string",
                "description": "The project folder to work in: one you may work in (default: the folder VAF runs from)."
            }
        },
        "required": ["title", "content"]
    }
    
    def run(self, **kwargs) -> str:
        # Before PersistenceManager creates <base_dir>/.vaf: see tool_work_folder.
        base_dir, refusal = tool_work_folder(kwargs)
        if refusal:
            return refusal
        title = kwargs.get('title', 'Untitled Pattern')
        content = kwargs.get('content', '')
        
        if not content:
            return "Error: Content is required."
        
        pm, refusal = _project_notes(base_dir, kwargs)
        if refusal:
            return refusal
        try:
            entry = f"### {title}\n{content}"
            pm.append_codex(entry)
            return f"✅ Saved to Codex: {title}"
        except Exception as e:
            return f"❌ Error updating Codex: {e}"

class AddMemoryTool(BaseTool):
    """
    Add a note to the current session memory. 
    Use this for short-term reminders or scratchpad notes for the CURRENT workflow.
    """
    name = "add_memory"
    # The folder rule in tool_work_folder judges base_dir for this caller.
    identity_kwargs = ("user_scope_id", "user_role")
    # It writes <base_dir>/.vaf/memory.md, which the coder reads back as its own context.
    agent_request = "refuse_standing"
    category    = "memory"
    permission_level = "write"
    side_effect_class = "reversible"
    description = "Add a note to the session memory (Short-term scratchpad)."
    
    parameters = {
        "type": "object",
        "properties": {
            "note": {
                "type": "string",
                "description": "The note to add."
            },
            "base_dir": {
                "type": "string",
                "description": "The project folder to work in: one you may work in (default: the folder VAF runs from)."
            }
        },
        "required": ["note"]
    }
    
    def run(self, **kwargs) -> str:
        # Before PersistenceManager creates <base_dir>/.vaf: see tool_work_folder.
        base_dir, refusal = tool_work_folder(kwargs)
        if refusal:
            return refusal
        note = kwargs.get('note', '')
        
        if not note:
            return "Error: Note is required."
        
        pm, refusal = _project_notes(base_dir, kwargs)
        if refusal:
            return refusal
        try:
            pm.append_memory(note)
            return "✅ Added to session memory."
        except Exception as e:
            return f"❌ Error updating memory: {e}"
