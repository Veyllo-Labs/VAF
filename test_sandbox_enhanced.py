#!/usr/bin/env python3
"""Test the enhanced Python sandbox with automatic expression evaluation."""

import sys
sys.path.insert(0, '/Users/m.c.elsner/VAF')

from vaf.tools.python_sandbox import PythonSandboxTool

sandbox = PythonSandboxTool()

print("=" * 60)
print("Testing Python Sandbox - Auto Expression Evaluation")
print("=" * 60)

# Test 1: Simple expression (should return "2")
print("\n1️⃣  Test: 1 + 1")
result = sandbox.run(code="1 + 1")
print(f"   Result: {result}")
print(f"   ✅ Expected: 2")

# Test 2: Math expression
print("\n2️⃣  Test: 42 * 7 - 10")
result = sandbox.run(code="42 * 7 - 10")
print(f"   Result: {result}")
print(f"   ✅ Expected: 284")

# Test 3: Using print (should still work)
print("\n3️⃣  Test: print('Hello')")
result = sandbox.run(code="print('Hello')")
print(f"   Result: {result}")
print(f"   ✅ Expected: Hello")

# Test 4: Using result variable (should still work)
print("\n4️⃣  Test: result = 5 * 5")
result = sandbox.run(code="result = 5 * 5")
print(f"   Result: {result}")
print(f"   ✅ Expected: 25")

# Test 5: Multiple statements (last one not an expression)
print("\n5️⃣  Test: x = 10\\ny = 20")
result = sandbox.run(code="x = 10\ny = 20")
print(f"   Result: {result}")
print(f"   ✅ Expected: No output message")

# Test 6: Math module expression
print("\n6️⃣  Test: import math; math.sqrt(16)")
result = sandbox.run(code="import math\nmath.sqrt(16)")
print(f"   Result: {result}")
print(f"   ✅ Expected: 4.0")

# Test 7: List comprehension
print("\n7️⃣  Test: [x**2 for x in range(5)]")
result = sandbox.run(code="[x**2 for x in range(5)]")
print(f"   Result: {result}")
print(f"   ✅ Expected: [0, 1, 4, 9, 16]")

print("\n" + "=" * 60)
print("✅ All tests completed!")
print("=" * 60)
