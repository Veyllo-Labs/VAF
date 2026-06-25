# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
from typing import Optional
from vaf.tools.base import BaseTool
from vaf.core.persistence import PersistenceManager
import os

class UpdateCodexTool(BaseTool):
    """
    Save a persistent engineering pattern, convention, or architectural decision to the project's Codex.
    Use this when you learn something important that should be remembered for future tasks.
    """
    name = "update_codex"
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
            }
        },
        "required": ["title", "content"]
    }
    
    def run(self, **kwargs) -> str:
        base_dir = kwargs.get('base_dir', '.')
        title = kwargs.get('title', 'Untitled Pattern')
        content = kwargs.get('content', '')
        
        if not content:
            return "Error: Content is required."
        
        try:
            pm = PersistenceManager(base_dir)
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
    permission_level = "write"
    side_effect_class = "reversible"
    description = "Add a note to the session memory (Short-term scratchpad)."
    
    parameters = {
        "type": "object",
        "properties": {
            "note": {
                "type": "string",
                "description": "The note to add."
            }
        },
        "required": ["note"]
    }
    
    def run(self, **kwargs) -> str:
        base_dir = kwargs.get('base_dir', '.')
        note = kwargs.get('note', '')
        
        if not note:
            return "Error: Note is required."
        
        try:
            pm = PersistenceManager(base_dir)
            pm.append_memory(note)
            return "✅ Added to session memory."
        except Exception as e:
            return f"❌ Error updating memory: {e}"
