#!/usr/bin/env python3
"""Debug test to check what's in the namespace."""

import sys
sys.path.insert(0, '/Users/m.c.elsner/VAF')

from vaf.tools.python_sandbox import PythonSandboxTool

sandbox = PythonSandboxTool()
namespace = sandbox._create_safe_namespace()

print("Checking for 'print' in namespace:")
print(f"  'print' in namespace: {'print' in namespace}")
print(f"  'print' in namespace['__builtins__']: {'print' in namespace.get('__builtins__', {})}")

print("\nAll keys in namespace:")
for key in sorted(namespace.keys()):
    if not key.startswith('_'):
        print(f"  - {key}")

print("\nAll keys in __builtins__:")
builtins_dict = namespace.get('__builtins__', {})
if isinstance(builtins_dict, dict):
    for key in sorted(builtins_dict.keys())[:20]:  # Show first 20
        print(f"  - {key}")
