# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Context Manager State Provider

Persists the state of VAF's ContextManager including intent and state tracking.
"""

from typing import Dict, Any
from dataclasses import asdict
from vaf.core.session_state import StateProvider
import logging

log = logging.getLogger(__name__)


class ContextStateProvider(StateProvider):
    """
    State provider for VAF's ContextManager.
    
    Persists:
    - Intent context (goals, constraints, keywords)
    - State context (files, errors, decisions, snippets)
    - Token limits and usage
    """
    
    def __init__(self, context_manager):
        """
        Initialize with a ContextManager instance.
        
        Args:
            context_manager: ContextManager instance to track
        """
        self.context_manager = context_manager
    
    @property
    def state_version(self) -> str:
        return "1.0"
    
    def get_state(self) -> Dict[str, Any]:
        """Capture current context state."""
        try:
            return {
                "intent": asdict(self.context_manager.intent),
                "state": {
                    "files_created": self.context_manager.state.files_created,
                    "files_read": self.context_manager.state.files_read,
                    "files_modified": self.context_manager.state.files_modified,
                    "errors_encountered": self.context_manager.state.errors_encountered,
                    "tools_used": self.context_manager.state.tools_used,
                    "key_decisions": self.context_manager.state.key_decisions,
                    "narrative_summary": self.context_manager.state.narrative_summary,
                    "last_updated": self.context_manager.state.last_updated
                    # Note: code_snippets excluded as they're ephemeral
                },
                "max_tokens": self.context_manager.max_tokens,
                "trigger_threshold": self.context_manager.trigger_threshold
            }
        except Exception as e:
            log.error(f"Failed to capture context state: {e}")
            return {}
    
    def restore_state(self, state: Dict[str, Any]) -> None:
        """Restore context state."""
        try:
            # Restore intent
            if "intent" in state:
                intent_data = state["intent"]
                self.context_manager.intent.primary_goal = intent_data.get("primary_goal", "")
                self.context_manager.intent.sub_goals = intent_data.get("sub_goals", [])
                self.context_manager.intent.constraints = intent_data.get("constraints", [])
                self.context_manager.intent.keywords = intent_data.get("keywords", [])
                self.context_manager.intent.last_updated = intent_data.get("last_updated", "")
            
            # Restore state
            if "state" in state:
                state_data = state["state"]
                self.context_manager.state.files_created = state_data.get("files_created", [])
                self.context_manager.state.files_read = state_data.get("files_read", [])
                self.context_manager.state.files_modified = state_data.get("files_modified", [])
                self.context_manager.state.errors_encountered = state_data.get("errors_encountered", [])
                self.context_manager.state.tools_used = state_data.get("tools_used", [])
                self.context_manager.state.key_decisions = state_data.get("key_decisions", [])
                self.context_manager.state.narrative_summary = state_data.get("narrative_summary", "")
                self.context_manager.state.last_updated = state_data.get("last_updated", "")
            
            # Restore settings
            if "max_tokens" in state:
                self.context_manager.max_tokens = state["max_tokens"]
            if "trigger_threshold" in state:
                self.context_manager.trigger_threshold = state["trigger_threshold"]
            
            log.debug("Successfully restored context state")
        except Exception as e:
            log.error(f"Failed to restore context state: {e}")
