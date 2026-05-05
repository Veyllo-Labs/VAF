"""
Tool Activity State Provider

Tracks active and recent tool executions for session resumption.
"""

from typing import Dict, Any, List
from datetime import datetime
from collections import deque
from vaf.core.session_state import StateProvider
import logging

log = logging.getLogger(__name__)


class ToolActivityStateProvider(StateProvider):
    """
    State provider for tracking tool activity and execution history.
    
    Persists:
    - Currently active tools
    - Recent tool execution history
    - Tool usage statistics
    """
    
    def __init__(self, agent=None, max_history=50):
        """
        Initialize tool activity tracker.
        
        Args:
            agent: Agent instance to track (optional, can be set later)
            max_history: Maximum number of tool executions to track
        """
        self.agent = agent
        self.max_history = max_history
        self._active_tools: Dict[str, Dict[str, Any]] = {}
        self._tool_history: deque = deque(maxlen=max_history)
        self._tool_stats: Dict[str, int] = {}
    
    @property
    def state_version(self) -> str:
        return "1.0"
    
    def record_tool_start(self, tool_name: str, args: Dict[str, Any] = None) -> None:
        """
        Record the start of a tool execution.
        
        Args:
            tool_name: Name of the tool being executed
            args: Tool arguments (optional)
        """
        self._active_tools[tool_name] = {
            "started_at": datetime.now().isoformat(),
            "args": args or {}
        }
    
    def record_tool_end(self, tool_name: str, result: Any = None, error: str = None) -> None:
        """
        Record the completion of a tool execution.
        
        Args:
            tool_name: Name of the tool that completed
            result: Tool result (optional)
            error: Error message if tool failed (optional)
        """
        if tool_name in self._active_tools:
            start_info = self._active_tools.pop(tool_name)
            
            # Add to history
            self._tool_history.append({
                "tool": tool_name,
                "started_at": start_info["started_at"],
                "completed_at": datetime.now().isoformat(),
                "success": error is None,
                "error": error
            })
            
            # Update stats
            self._tool_stats[tool_name] = self._tool_stats.get(tool_name, 0) + 1
    
    def get_active_tools(self) -> Dict[str, Dict[str, Any]]:
        """Get currently active tools."""
        return self._active_tools.copy()
    
    def get_recent_history(self, limit: int = None) -> List[Dict[str, Any]]:
        """
        Get recent tool execution history.
        
        Args:
            limit: Maximum number of entries to return
            
        Returns:
            List of tool execution records
        """
        history = list(self._tool_history)
        if limit:
            history = history[-limit:]
        return history
    
    def get_tool_stats(self) -> Dict[str, int]:
        """Get tool usage statistics."""
        return self._tool_stats.copy()
    
    def get_state(self) -> Dict[str, Any]:
        """Capture current tool activity state."""
        return {
            "active_tools": self._active_tools,
            "recent_history": list(self._tool_history),
            "tool_stats": self._tool_stats
        }
    
    def restore_state(self, state: Dict[str, Any]) -> None:
        """Restore tool activity state."""
        try:
            # Restore active tools (though they won't actually be running)
            self._active_tools = state.get("active_tools", {})
            
            # Restore history
            history = state.get("recent_history", [])
            self._tool_history = deque(history, maxlen=self.max_history)
            
            # Restore stats
            self._tool_stats = state.get("tool_stats", {})
            
            log.debug(f"Restored tool activity state: {len(self._tool_history)} history entries, "
                     f"{len(self._tool_stats)} tools tracked")
        except Exception as e:
            log.error(f"Failed to restore tool activity state: {e}")
