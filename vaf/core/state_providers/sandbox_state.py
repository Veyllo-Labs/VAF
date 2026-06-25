# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Python Sandbox State Provider

Persists the state of the Python sandbox including variables, imports, and execution history.
"""

from typing import Dict, Any, List
from pathlib import Path
from vaf.core.session_state import StateProvider
import logging
import pickle
import base64

log = logging.getLogger(__name__)


class PythonSandboxStateProvider(StateProvider):
    """
    State provider for Python sandbox tool.
    
    Persists:
    - Global variables (serializable ones)
    - Import statements
    - Execution history
    - Working directory
    """
    
    def __init__(self, sandbox):
        """
        Initialize with a Python sandbox instance.
        
        Args:
            sandbox: PythonSandbox instance to track
        """
        self.sandbox = sandbox
    
    @property
    def state_version(self) -> str:
        return "1.0"
    
    def get_state(self) -> Dict[str, Any]:
        """Capture current sandbox state."""
        try:
            state = {
                "variables": self._serialize_variables(),
                "imports": self._get_imports(),
                "execution_history": self._get_execution_history(),
                "working_directory": str(getattr(self.sandbox, 'cwd', Path.cwd()))
            }
            return state
        except Exception as e:
            log.error(f"Failed to capture sandbox state: {e}")
            return {}
    
    def restore_state(self, state: Dict[str, Any]) -> None:
        """Restore sandbox state."""
        try:
            # Restore variables
            variables = state.get("variables", {})
            self._restore_variables(variables)
            
            # Restore imports (re-execute import statements)
            imports = state.get("imports", [])
            self._restore_imports(imports)
            
            # Note: execution_history is read-only, used for display purposes only
            # We don't re-execute the history, just log that it exists
            history_count = len(state.get("execution_history", []))
            if history_count > 0:
                log.debug(f"Session had {history_count} previous sandbox executions")
            
            # Restore working directory if changed
            if "working_directory" in state:
                try:
                    wd = Path(state["working_directory"])
                    if wd.exists() and wd.is_dir():
                        if hasattr(self.sandbox, 'cwd'):
                            self.sandbox.cwd = wd
                except Exception as e:
                    log.warning(f"Could not restore working directory: {e}")
            
            log.debug("Successfully restored sandbox state")
        except Exception as e:
            log.error(f"Failed to restore sandbox state: {e}")
    
    def _serialize_variables(self) -> Dict[str, Any]:
        """
        Serialize sandbox global variables.
        
        Only serializes JSON-compatible types and pickable objects.
        Non-serializable objects are converted to their string representation.
        """
        if not hasattr(self.sandbox, 'namespace'):
            return {}
        
        serialized = {}
        namespace = getattr(self.sandbox, 'namespace', {})
        
        # Skip built-ins and modules
        skip_names = {'__builtins__', '__name__', '__doc__', '__file__', '__package__'}
        
        for name, value in namespace.items():
            if name.startswith('_') or name in skip_names:
                continue
            
            # Try to serialize
            try:
                # First try JSON-compatible types
                if value is None or isinstance(value, (bool, int, float, str)):
                    serialized[name] = {"type": "primitive", "value": value}
                elif isinstance(value, (list, tuple, dict)):
                    # Try JSON serialization
                    import json
                    json.dumps(value)  # Test if serializable
                    serialized[name] = {"type": type(value).__name__, "value": value}
                else:
                    # Try pickle for other objects
                    pickled = pickle.dumps(value)
                    encoded = base64.b64encode(pickled).decode('utf-8')
                    serialized[name] = {
                        "type": "pickled",
                        "value": encoded,
                        "class": type(value).__name__
                    }
            except Exception as e:
                # Fallback: store string representation
                serialized[name] = {
                    "type": "string_repr",
                    "value": str(value),
                    "class": type(value).__name__
                }
                log.debug(f"Variable '{name}' stored as string representation: {e}")
        
        return serialized
    
    def _restore_variables(self, variables: Dict[str, Any]) -> None:
        """Restore variables to sandbox namespace."""
        if not hasattr(self.sandbox, 'namespace'):
            log.warning("Sandbox has no namespace attribute")
            return
        
        namespace = self.sandbox.namespace
        restored_count = 0
        
        for name, var_data in variables.items():
            try:
                var_type = var_data.get("type")
                value = var_data.get("value")
                
                if var_type == "primitive":
                    namespace[name] = value
                    restored_count += 1
                elif var_type in ("list", "tuple", "dict"):
                    namespace[name] = value
                    restored_count += 1
                elif var_type == "pickled":
                    decoded = base64.b64decode(value.encode('utf-8'))
                    namespace[name] = pickle.loads(decoded)
                    restored_count += 1
                elif var_type == "string_repr":
                    # Can't restore from string representation, skip
                    log.debug(f"Skipping non-restorable variable: {name} ({var_data.get('class')})")
            except Exception as e:
                log.warning(f"Failed to restore variable '{name}': {e}")
        
        log.debug(f"Restored {restored_count}/{len(variables)} sandbox variables")
    
    def _get_imports(self) -> List[str]:
        """Extract import statements from sandbox history."""
        if not hasattr(self.sandbox, 'execution_history'):
            return []
        
        history = getattr(self.sandbox, 'execution_history', [])
        imports = []
        
        for entry in history:
            code = entry.get('code', '') if isinstance(entry, dict) else str(entry)
            # Simple heuristic: lines starting with 'import' or 'from'
            for line in code.split('\n'):
                stripped = line.strip()
                if stripped.startswith(('import ', 'from ')):
                    if stripped not in imports:
                        imports.append(stripped)
        
        return imports
    
    def _restore_imports(self, imports: List[str]) -> None:
        """Re-execute import statements."""
        if not hasattr(self.sandbox, 'execute'):
            log.warning("Sandbox has no execute method")
            return
        
        for import_stmt in imports:
            try:
                # Re-execute import in sandbox
                self.sandbox.execute(import_stmt)
            except Exception as e:
                log.warning(f"Failed to restore import '{import_stmt}': {e}")
    
    def _get_execution_history(self) -> List[Dict[str, Any]]:
        """Get execution history for reference."""
        if not hasattr(self.sandbox, 'execution_history'):
            return []
        
        history = getattr(self.sandbox, 'execution_history', [])
        
        # Limit history size to last 50 executions
        limited_history = list(history)[-50:] if history else []
        
        # Ensure history entries are serializable
        serializable_history = []
        for entry in limited_history:
            if isinstance(entry, dict):
                serializable_history.append({
                    "code": str(entry.get('code', '')),
                    "timestamp": entry.get('timestamp', ''),
                    "success": entry.get('success', True)
                })
            else:
                serializable_history.append({"code": str(entry)})
        
        return serializable_history
