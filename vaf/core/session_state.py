# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Session State Management - Dynamic Runtime State Persistence

Provides infrastructure for persisting and restoring contextual runtime state
including tool activity, variables, preferences, and intermediate results.

This allows sessions to be resumed seamlessly with full context restoration.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional, Callable
from datetime import datetime
import logging
import json

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# STATE PROVIDER INTERFACE
# ═══════════════════════════════════════════════════════════════════════════════

class StateProvider(ABC):
    """
    Abstract base class for tool-specific state providers.
    
    Tools implement this interface to expose their runtime state for persistence.
    The state must be serializable to JSON.
    
    Example:
        class MyToolStateProvider(StateProvider):
            def __init__(self, tool):
                self.tool = tool
            
            @property
            def state_version(self) -> str:
                return "1.0"
            
            def get_state(self) -> Dict[str, Any]:
                return {"variables": self.tool.variables}
            
            def restore_state(self, state: Dict[str, Any]) -> None:
                self.tool.variables = state.get("variables", {})
    """
    
    @abstractmethod
    def get_state(self) -> Dict[str, Any]:
        """
        Capture current runtime state as a serializable dictionary.
        
        Returns:
            Dictionary containing the current state. Must be JSON-serializable.
        """
        pass
    
    @abstractmethod
    def restore_state(self, state: Dict[str, Any]) -> None:
        """
        Restore runtime state from a previously captured state dictionary.
        
        Args:
            state: Dictionary containing serialized state to restore
        """
        pass
    
    @property
    @abstractmethod
    def state_version(self) -> str:
        """
        Version string for this state provider.
        
        Used for backward compatibility when state schema changes.
        Format: "major.minor" (e.g., "1.0", "1.1", "2.0")
        
        Returns:
            Version string
        """
        pass
    
    def validate_state(self, state: Dict[str, Any]) -> bool:
        """
        Optional validation of state before restoration.
        
        Override to add custom validation logic.
        
        Args:
            state: State dictionary to validate
            
        Returns:
            True if state is valid, False otherwise
        """
        return True
    
    def migrate_state(self, state: Dict[str, Any], from_version: str) -> Dict[str, Any]:
        """
        Optional migration of state from older versions.
        
        Override to handle state schema changes across versions.
        
        Args:
            state: State dictionary in old format
            from_version: Version string of the old state
            
        Returns:
            Migrated state dictionary
        """
        return state


# ═══════════════════════════════════════════════════════════════════════════════
# STATE SNAPSHOT
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class StateSnapshot:
    """
    Versioned snapshot of runtime state from all providers.
    
    This is what gets serialized and stored with sessions.
    """
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    schema_version: str = "1.0"  # Overall state format version
    providers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to serializable dictionary."""
        return {
            "timestamp": self.timestamp,
            "schema_version": self.schema_version,
            "providers": self.providers
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StateSnapshot":
        """Create from dictionary."""
        return cls(
            timestamp=data.get("timestamp", datetime.now().isoformat()),
            schema_version=data.get("schema_version", "1.0"),
            providers=data.get("providers", {})
        )


# ═══════════════════════════════════════════════════════════════════════════════
# STATE REGISTRY
# ═══════════════════════════════════════════════════════════════════════════════

class StateRegistry:
    """
    Central registry for all state providers in the system.
    
    Coordinates state collection and restoration across multiple providers.
    Supports event listeners for real-time state updates.
    """
    
    def __init__(self):
        self._providers: Dict[str, StateProvider] = {}
        self._listeners: List[Callable[[str, Dict[str, Any]], None]] = []
        self._enabled = True
    
    def register(self, name: str, provider: StateProvider) -> None:
        """
        Register a state provider.
        
        Args:
            name: Unique name for this provider (e.g., "sandbox", "context")
            provider: StateProvider implementation
        """
        if name in self._providers:
            log.warning(f"State provider '{name}' is already registered. Replacing.")
        
        self._providers[name] = provider
        log.debug(f"Registered state provider: {name} (version {provider.state_version})")
    
    def unregister(self, name: str) -> None:
        """
        Unregister a state provider.
        
        Args:
            name: Name of provider to remove
        """
        if name in self._providers:
            del self._providers[name]
            log.debug(f"Unregistered state provider: {name}")
    
    def get_provider(self, name: str) -> Optional[StateProvider]:
        """
        Get a specific state provider by name.
        
        Args:
            name: Provider name
            
        Returns:
            StateProvider instance or None if not found
        """
        return self._providers.get(name)
    
    def list_providers(self) -> List[str]:
        """
        Get list of all registered provider names.
        
        Returns:
            List of provider names
        """
        return list(self._providers.keys())
    
    def capture_snapshot(self) -> StateSnapshot:
        """
        Capture state from all registered providers.
        
        Returns:
            StateSnapshot containing all provider states
        """
        if not self._enabled:
            return StateSnapshot()
        
        snapshot = StateSnapshot()
        
        for name, provider in self._providers.items():
            try:
                state = provider.get_state()
                snapshot.providers[name] = {
                    "version": provider.state_version,
                    "state": state,
                    "captured_at": datetime.now().isoformat()
                }
                log.debug(f"Captured state from provider: {name}")
            except Exception as e:
                log.error(f"Failed to capture state from provider '{name}': {e}")
                # Continue with other providers
        
        return snapshot
    
    def restore_snapshot(self, snapshot: StateSnapshot) -> None:
        """
        Restore state to all registered providers.
        
        Args:
            snapshot: StateSnapshot to restore
        """
        if not self._enabled:
            return
        
        for name, provider_data in snapshot.providers.items():
            provider = self._providers.get(name)
            
            if not provider:
                log.warning(f"No provider registered for '{name}'. Skipping restoration.")
                continue
            
            try:
                state = provider_data.get("state", {})
                stored_version = provider_data.get("version", "1.0")
                current_version = provider.state_version
                
                # Handle version migration if needed
                if stored_version != current_version:
                    log.info(f"Migrating state for '{name}' from v{stored_version} to v{current_version}")
                    state = provider.migrate_state(state, stored_version)
                
                # Validate state
                if not provider.validate_state(state):
                    log.error(f"State validation failed for provider '{name}'. Skipping restoration.")
                    continue
                
                # Restore
                provider.restore_state(state)
                log.debug(f"Restored state to provider: {name}")
                
                # Notify listeners
                self._notify_listeners(name, state)
                
            except Exception as e:
                log.error(f"Failed to restore state to provider '{name}': {e}")
                # Continue with other providers
    
    def update_provider_state(self, name: str, state: Dict[str, Any]) -> None:
        """
        Update state for a specific provider and notify listeners.
        
        Args:
            name: Provider name
            state: New state to apply
        """
        if not self._enabled:
            return
        
        provider = self._providers.get(name)
        if not provider:
            log.warning(f"No provider registered for '{name}'")
            return
        
        try:
            provider.restore_state(state)
            self._notify_listeners(name, state)
        except Exception as e:
            log.error(f"Failed to update state for provider '{name}': {e}")
    
    def add_listener(self, callback: Callable[[str, Dict[str, Any]], None]) -> None:
        """
        Add a listener for state updates.
        
        Listeners are called when provider state changes.
        
        Args:
            callback: Function called with (provider_name, state) on updates
        """
        self._listeners.append(callback)
    
    def remove_listener(self, callback: Callable[[str, Dict[str, Any]], None]) -> None:
        """
        Remove a state update listener.
        
        Args:
            callback: Listener to remove
        """
        if callback in self._listeners:
            self._listeners.remove(callback)
    
    def _notify_listeners(self, provider_name: str, state: Dict[str, Any]) -> None:
        """Notify all listeners of a state update."""
        for listener in self._listeners:
            try:
                listener(provider_name, state)
            except Exception as e:
                log.error(f"Error in state listener: {e}")
    
    def enable(self) -> None:
        """Enable state capture and restoration."""
        self._enabled = True
    
    def disable(self) -> None:
        """Disable state capture and restoration."""
        self._enabled = False
    
    def is_enabled(self) -> bool:
        """Check if state management is enabled."""
        return self._enabled
    
    def clear(self) -> None:
        """Clear all registered providers."""
        self._providers.clear()
        log.debug("Cleared all state providers")


# ═══════════════════════════════════════════════════════════════════════════════
# SERIALIZATION UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

class StateSerializer:
    """
    Utilities for serializing and deserializing state snapshots.
    
    Handles edge cases like non-serializable objects gracefully.
    """
    
    @staticmethod
    def serialize(snapshot: StateSnapshot) -> str:
        """
        Serialize state snapshot to JSON string.
        
        Args:
            snapshot: StateSnapshot to serialize
            
        Returns:
            JSON string
        """
        try:
            return json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            log.error(f"Failed to serialize state snapshot: {e}")
            # Return minimal snapshot on error
            return json.dumps({"timestamp": snapshot.timestamp, "providers": {}})
    
    @staticmethod
    def deserialize(data: str) -> StateSnapshot:
        """
        Deserialize JSON string to state snapshot.
        
        Args:
            data: JSON string
            
        Returns:
            StateSnapshot instance
        """
        try:
            parsed = json.loads(data)
            return StateSnapshot.from_dict(parsed)
        except Exception as e:
            log.error(f"Failed to deserialize state snapshot: {e}")
            # Return empty snapshot on error
            return StateSnapshot()
    
    @staticmethod
    def sanitize_state(state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Sanitize state dictionary to ensure JSON serializability.
        
        Converts non-serializable objects to string representations.
        
        Args:
            state: State dictionary to sanitize
            
        Returns:
            Sanitized state dictionary
        """
        def sanitize_value(val):
            if val is None or isinstance(val, (bool, int, float, str)):
                return val
            elif isinstance(val, dict):
                return {k: sanitize_value(v) for k, v in val.items()}
            elif isinstance(val, (list, tuple)):
                return [sanitize_value(item) for item in val]
            else:
                # Convert to string for non-serializable objects
                return str(val)
        
        return sanitize_value(state)
