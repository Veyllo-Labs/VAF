"""
VAF Python Sandbox - Safe Python Code Execution
Executes Python code in a restricted environment for mathematical calculations and data processing.

OS-Independent: Uses only Python standard library modules (math, random, datetime, json, re, etc.)
No external dependencies required - works on Windows, Linux, and macOS.
"""
import io
import contextlib
from typing import Dict, Any, Tuple
from vaf.tools.base import BaseTool

# Blocked modules and functions for security
BLOCKED_MODULES = {
    'os', 'sys', 'subprocess', 'shutil', 'socket', 'urllib', 'http',
    'importlib', '__import__', 'eval', 'exec', 'compile', 'open',
    'file', 'input', 'raw_input', 'exit', 'quit', 'help'
}

BLOCKED_ATTRIBUTES = {
    '__import__', '__builtins__', '__file__', '__name__', '__doc__',
    '__package__', '__loader__', '__spec__', '__path__', '__cached__'
}


class PythonSandboxTool(BaseTool):
    """
    Safe Python Sandbox for executing Python code.
    
    Use for:
    - Mathematical calculations
    - Data processing
    - Algorithm implementations
    - Scientific computations
    
    Security: Restricted environment, no file system access, no network access.
    """
    
    name = "python_sandbox"
    description = """Execute Python code safely in a sandboxed environment.
Use for mathematical calculations, data processing, algorithms, and scientific computations.
The code runs in a restricted environment without file system or network access.
Returns the result or any output from print statements."""

    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to execute (e.g., 'result = 2 + 2 * 3' or 'import math; print(math.sqrt(16))')"
            }
        },
        "required": ["code"]
    }
    
    def __init__(self):
        super().__init__()
        # Blocked builtins - only block dangerous operations
        self.blocked_builtins = {
            'open', 'file', 'input', 'raw_input', 'exec', 'eval', 'compile',
            '__import__', 'exit', 'quit', 'help', 'license', 'credits',
            'copyright', 'vars', 'dir', 'globals', 'locals', 'reload',
        }
    
    def _is_safe_code(self, code: str) -> Tuple[bool, str]:
        """Check if code contains dangerous operations."""
        code_lower = code.lower()
        
        # Check for blocked imports
        for blocked in BLOCKED_MODULES:
            if f'import {blocked}' in code_lower or f'from {blocked}' in code_lower:
                return False, f"Blocked module: {blocked}"
        
        # Check for dangerous function calls
        dangerous_patterns = [
            'eval(', 'exec(', '__import__', 'open(', 'file(',
            'input(', 'raw_input(', 'exit(', 'quit(',
            'subprocess', 'os.', 'sys.', 'shutil', 'socket'
        ]
        
        for pattern in dangerous_patterns:
            if pattern in code_lower:
                return False, f"Blocked operation: {pattern}"
        
        # Check for attribute access to blocked attributes
        for attr in BLOCKED_ATTRIBUTES:
            if f'.{attr}' in code or f'[{attr}]' in code:
                return False, f"Blocked attribute access: {attr}"
        
        return True, ""
    
    def _create_safe_namespace(self) -> Dict[str, Any]:
        """Create a safe namespace with normal Python builtins (except dangerous ones)."""
        import math
        import random
        import datetime
        import json
        import re
        import statistics
        import decimal
        import fractions
        import collections
        import itertools
        import functools
        import operator
        import builtins
        
        # Get all builtins and filter out dangerous ones
        safe_builtins_dict = {}
        for name in dir(builtins):
            # Skip private attributes and blocked builtins
            if name.startswith('_') and name not in ['__name__', '__doc__', '__package__']:
                continue
            if name in self.blocked_builtins:
                continue
            
            try:
                attr = getattr(builtins, name)
                # Only include callables and types, not modules
                if not isinstance(attr, type(__import__)):
                    safe_builtins_dict[name] = attr
            except:
                pass
        
        # Create namespace with normal Python environment
        namespace = {
            '__builtins__': safe_builtins_dict,
            # Math and scientific modules
            'math': math,
            'random': random,
            'statistics': statistics,
            'decimal': decimal,
            'fractions': fractions,
            # Data structures
            'collections': collections,
            'itertools': itertools,
            'functools': functools,
            'operator': operator,
            # String and data processing
            're': re,
            'json': json,
            'datetime': datetime,
            # Common types (already in builtins, but explicit for clarity)
            'int': int,
            'float': float,
            'str': str,
            'list': list,
            'dict': dict,
            'tuple': tuple,
            'set': set,
            'bool': bool,
            'None': None,
            'True': True,
            'False': False,
        }
        
        # Add all safe builtins directly to namespace for normal Python experience
        namespace.update(safe_builtins_dict)
        
        return namespace
    
    def run(self, **kwargs) -> str:
        """Execute Python code safely."""
        code = kwargs.get("code", "")
        
        if not code:
            return "Error: No code provided."
        
        # Security check
        is_safe, error_msg = self._is_safe_code(code)
        if not is_safe:
            return f"Security Error: {error_msg}. This operation is not allowed in the sandbox."
        
        # Create safe namespace
        namespace = self._create_safe_namespace()
        
        # Capture stdout
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        
        try:
            with contextlib.redirect_stdout(stdout_capture), \
                 contextlib.redirect_stderr(stderr_capture):
                # Execute code
                exec(code, namespace)
            
            # Get output
            stdout_output = stdout_capture.getvalue()
            stderr_output = stderr_capture.getvalue()
            
            # Check for errors
            if stderr_output:
                return f"Error: {stderr_output}"
            
            # If there's print output, return it
            if stdout_output:
                return stdout_output.strip()
            
            # Try to find a result variable
            if 'result' in namespace:
                return str(namespace['result'])
            
            # Return success message
            return "Code executed successfully. (No output or result variable found. Use 'print()' or assign to 'result' variable.)"
            
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)
            return f"Execution Error ({error_type}): {error_msg}"
        except SystemExit:
            return "Error: exit() or quit() calls are not allowed."
        except KeyboardInterrupt:
            return "Error: Execution was interrupted."
        except BaseException as e:
            return f"Unexpected Error: {type(e).__name__}: {str(e)}"

