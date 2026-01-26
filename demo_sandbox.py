#!/usr/bin/env python3
"""Demo: Show how the sandbox now auto-evaluates expressions."""

import sys
sys.path.insert(0, '/Users/m.c.elsner/VAF')

from vaf.tools.python_sandbox import PythonSandboxTool

sandbox = PythonSandboxTool()

print("=" * 70)
print("🎯 DEMO: Auto-Expression Evaluation (Like IPython/Jupyter)")
print("=" * 70)

demos = [
    ("1 + 1", "Simple arithmetic"),
    ("2 ** 10", "Power calculation"),
    ("'hello'.upper()", "String method"),
    ("[x * 2 for x in range(5)]", "List comprehension"),
    ("{'a': 1, 'b': 2}", "Dictionary literal"),
    ("math.factorial(5)", "Math function"),
    ("sum(range(100))", "Sum of numbers"),
    ("len('Python')", "String length"),
]

for code, description in demos:
    print(f"\n📌 {description}")
    print(f"   Input:  {code}")
    result = sandbox.run(code=code)
    print(f"   Output: {result}")

print("\n" + "=" * 70)
print("✅ All expressions auto-evaluated without print() or result=")
print("=" * 70)
