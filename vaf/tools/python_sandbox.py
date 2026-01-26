"""
VAF Python Sandbox - Safe Python Code Execution
Executes Python code in a restricted environment for mathematical calculations and data processing.

OS-Independent: Uses only Python standard library modules (math, random, datetime, json, re, etc.)
No external dependencies required - works on Windows, Linux, and macOS.
"""
import io
import contextlib
from typing import Dict, Any, Tuple
from pathlib import Path
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

Available modules: math, random, statistics, decimal, fractions, collections, itertools, 
functools, operator, re, json, datetime, time, string, textwrap, copy, hashlib, base64, 
uuid, secrets, heapq, bisect.

Returns the result automatically for expressions (e.g., '1+1' returns '2'), 
or output from print statements, or the 'result' variable."""

    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to execute (e.g., '1 + 1', 'math.sqrt(16)', or 'print(sum(range(10)))')"
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
        import time
        import hashlib
        import base64
        import uuid
        import string
        import copy
        import heapq
        import bisect
        import secrets
        import textwrap
        
        # Get all builtins and filter out dangerous ones
        safe_builtins_dict = {}
        for name in dir(builtins):
            # Skip private attributes (except a few allowed ones)
            if name.startswith('_') and name not in ['__name__', '__doc__', '__package__']:
                continue
            # Skip blocked builtins
            if name in self.blocked_builtins:
                continue
            
            try:
                attr = getattr(builtins, name)
                # Include everything except modules (we'll add safe modules separately)
                # This ensures print, len, range, etc. are all included
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
            'string': string,
            'textwrap': textwrap,
            # Utilities
            'time': time,
            'copy': copy,
            # Cryptography (safe - no network)
            'hashlib': hashlib,
            'base64': base64,
            'uuid': uuid,
            'secrets': secrets,
            # Algorithms
            'heapq': heapq,
            'bisect': bisect,
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
        # Convert Path objects to strings (OS-independent defensive handling)
        # str() works for both strings and Path objects
        code = str(kwargs.get("code", ""))
        
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
            # Capture stdout/stderr
            with contextlib.redirect_stdout(stdout_capture), \
                 contextlib.redirect_stderr(stderr_capture):
                # Try to evaluate as a single expression first (like IPython/Jupyter)
                # This allows "1 + 1" to return "2" automatically
                last_expr_result = None
                is_expression = False
                
                try:
                    # Compile as 'eval' to check if it's a single expression
                    compile(code, '<string>', 'eval')
                    is_expression = True
                except SyntaxError:
                    # Not a single expression, will execute as statements
                    is_expression = False
                
                if is_expression:
                    # Evaluate as expression (both globals and locals set to namespace)
                    last_expr_result = eval(code, namespace, namespace)
                else:
                    # Execute as statements
                    exec(code, namespace, namespace)
            
            # Get output
            stdout_output = stdout_capture.getvalue()
            stderr_output = stderr_capture.getvalue()
            
            # Check for errors
            if stderr_output:
                return f"Error: {stderr_output}"
            
            # Priority 1: If there's print output, return it
            if stdout_output:
                return stdout_output.strip()
            
            # Priority 2: If we evaluated a single expression, return its result
            if last_expr_result is not None:
                return str(last_expr_result)
            
            # Priority 3: Try to find a result variable
            if 'result' in namespace:
                return str(namespace['result'])
            
            # Priority 4: Return success message (only for statement-only code with no output)
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

