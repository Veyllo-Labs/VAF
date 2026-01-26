#!/usr/bin/env python3
"""Final comprehensive test of the enhanced Python sandbox."""

import sys
sys.path.insert(0, '/Users/m.c.elsner/VAF')

from vaf.tools.python_sandbox import PythonSandboxTool

sandbox = PythonSandboxTool()

print("=" * 70)
print("✅ PYTHON SANDBOX - COMPREHENSIVE TEST SUITE")
print("=" * 70)

tests_passed = 0
tests_failed = 0

def run_test(name, code, expected_result):
    global tests_passed, tests_failed
    print(f"\n📝 {name}")
    print(f"   Code: {repr(code)}")
    result = sandbox.run(code=code)
    print(f"   Result: {result}")
    
    if str(result) == str(expected_result):
        print(f"   ✅ PASS")
        tests_passed += 1
    else:
        print(f"   ❌ FAIL - Expected: {expected_result}")
        tests_failed += 1

# Test 1: Simple arithmetic (auto-eval)
run_test("Simple Expression (1 + 1)", "1 + 1", "2")

# Test 2: Complex arithmetic (auto-eval)
run_test("Complex Expression", "42 * 7 - 10", "284")

# Test 3: Print statement
run_test("Print Statement", "print('Hello World')", "Hello World")

# Test 4: Result variable
run_test("Result Variable", "result = 10 * 5", "50")

# Test 5: List comprehension (auto-eval)
run_test("List Comprehension", "[x**2 for x in range(5)]", "[0, 1, 4, 9, 16]")

# Test 6: Math module (pre-imported)
run_test("Math Module (sqrt)", "math.sqrt(16)", "4.0")

# Test 7: Math module (pi)
run_test("Math Module (pi)", "math.pi", str(__import__('math').pi))

# Test 8: String operations
run_test("String Join", "', '.join(['a', 'b', 'c'])", "a, b, c")

# Test 9: Multiple print statements
run_test("Multiple Prints", "print('Line 1')\\nprint('Line 2')", "Line 1\nLine 2")

# Test 10: Dictionary (auto-eval)
run_test("Dictionary Expression", "{'name': 'Alice', 'age': 30}", "{'name': 'Alice', 'age': 30}")

# Test 11: Boolean (auto-eval)
run_test("Boolean Expression", "5 > 3", "True")

# Test 12: Sum function
run_test("Sum Function", "sum([1, 2, 3, 4, 5])", "15")

# Test 13: Length function
run_test("Len Function", "len([1, 2, 3])", "3")

# Test 14: Range function
run_test("Range to List", "list(range(5))", "[0, 1, 2, 3, 4]")

print("\n" + "=" * 70)
print(f"📊 TEST RESULTS: {tests_passed} passed, {tests_failed} failed")
if tests_failed == 0:
    print("🎉 ALL TESTS PASSED!")
else:
    print(f"⚠️  {tests_failed} test(s) need attention")
print("=" * 70)
