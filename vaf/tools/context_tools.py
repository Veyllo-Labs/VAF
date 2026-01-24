from typing import Optional, List
from vaf.tools.base import BaseTool
from vaf.core.main_persistence import MainPersistenceManager
import os

class UpdateIntentTool(BaseTool):
    """
    Update the User Intent (The North Star). 
    Use this when the user clarifies or changes their main goal.
    This persists across the entire session and is always visible.
    """
    name = "update_intent"
    description = "Update the primary User Intent that guides the entire session."
    
    parameters = {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "description": "The full, updated user intent description."
            }
        },
        "required": ["intent"]
    }
    
    def run(self, **kwargs) -> str:
        base_dir = kwargs.get('base_dir', '.')
        intent = kwargs.get('intent', '')
        
        if not intent:
            return "Error: Intent content is required."
        
        try:
            mpm = MainPersistenceManager(base_dir)
            mpm.update_user_intent(intent)
            return "✅ User Intent updated successfully."
        except Exception as e:
            return f"❌ Error updating intent: {e}"

class UpdateWorkingMemoryTool(BaseTool):
    """
    Update the Main Agent's Working Memory (Scratchpad).
    Use this to store your internal notes, plans, and observations.
    """
    name = "update_working_memory"
    description = "Update the internal working memory (notes and plan)."
    
    parameters = {
        "type": "object",
        "properties": {
            "notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of observations or facts to remember."
            },
            "plan": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of high-level steps remaining."
            }
        },
        "required": []
    }
    
    def run(self, **kwargs) -> str:
        base_dir = kwargs.get('base_dir', '.')
        notes = kwargs.get('notes')
        plan = kwargs.get('plan')
        
        try:
            mpm = MainPersistenceManager(base_dir)
            mpm.update_working_memory(notes=notes, plan=plan)
            return "✅ Working Memory updated."
        except Exception as e:
            return f"❌ Error updating working memory: {e}"

class RequestClarificationTool(BaseTool):
    """
    [SUB-AGENT ONLY] Signal that you are blocked and need user input.
    Use this instead of failing or guessing.
    """
    name = "request_clarification"
    description = "Signal a blocker and ask the Main Agent/User for clarification."
    coder_only = True  # Only available to sub-agents
    
    parameters = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The specific question for the user."
            },
            "context": {
                "type": "string",
                "description": "Why you need this info (context)."
            },
            "task_id": {
                "type": "string",
                "description": "Your current task ID."
            }
        },
        "required": ["question", "task_id"]
    }
    
    def run(self, **kwargs) -> str:
        base_dir = kwargs.get('base_dir', '.')
        question = kwargs.get('question', '')
        context = kwargs.get('context', '')
        task_id = kwargs.get('task_id', '')
        
        # Sub-agents know their type implicitly, but we need it for the manager
        # In a real run, this might be injected or inferred.
        # For now, we assume 'coder' as it's the main user.
        agent_type = "coding_agent" 
        
        if not question or not task_id:
            return "Error: Question and Task ID are required."
        
        try:
            mpm = MainPersistenceManager(base_dir)
            mpm.update_subagent_status(
                task_id=task_id,
                agent_type=agent_type,
                status="needs_clarification",
                question=f"{question} (Context: {context})"
            )
            return "✅ Clarification request sent to Main Agent. Waiting for update..."
        except Exception as e:
            return f"❌ Error requesting clarification: {e}"
