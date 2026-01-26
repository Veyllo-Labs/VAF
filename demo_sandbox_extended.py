#!/usr/bin/env python3
"""Demo: Show all new modules and capabilities of the enhanced sandbox."""

import sys
sys.path.insert(0, '/Users/m.c.elsner/VAF')

from vaf.tools.python_sandbox import PythonSandboxTool

sandbox = PythonSandboxTool()

print("=" * 80)
print("🚀 PYTHON SANDBOX - EXTENDED CAPABILITIES DEMO")
print("=" * 80)

demos = [
    # Time & Performance
    ("⏰ Time & Date", [
        ("time.time()", "Current timestamp"),
        ("datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')", "Formatted date"),
    ]),
    
    # Cryptography & Encoding
    ("🔒 Hashing & Encoding", [
        ("hashlib.sha256(b'Hello').hexdigest()", "SHA-256 hash"),
        ("base64.b64encode(b'Hello').decode()", "Base64 encode"),
        ("uuid.uuid4()", "Generate UUID"),
        ("secrets.token_hex(8)", "Secure random token"),
    ]),
    
    # String Processing
    ("📝 String & Text", [
        ("string.ascii_uppercase", "ASCII uppercase letters"),
        ("string.punctuation", "Punctuation characters"),
        ("textwrap.shorten('This is a very long text', width=15)", "Text shortening"),
    ]),
    
    # Algorithms & Data Structures
    ("🧮 Algorithms", [
        ("bisect.bisect_left([1, 2, 4, 5], 3)", "Binary search insertion point"),
        ("list(heapq.nlargest(3, [1, 9, 2, 8, 3, 7]))", "Top 3 largest items"),
    ]),
    
    # Copy Operations
    ("📋 Object Copying", [
        ("copy.copy([1, 2, 3])", "Shallow copy"),
        ("copy.deepcopy({'a': [1, 2]})", "Deep copy"),
    ]),
    
    # Math & Statistics (existing, but showing advanced usage)
    ("📊 Advanced Math", [
        ("math.comb(10, 3)", "Combinations (10 choose 3)"),
        ("statistics.median([1, 2, 3, 4, 5])", "Median calculation"),
        ("decimal.Decimal('0.1') + decimal.Decimal('0.2')", "Precise decimal math"),
    ]),
    
    # Collections & Itertools
    ("🗂️ Advanced Collections", [
        ("collections.Counter('abracadabra').most_common(3)", "Count letter frequency"),
        ("list(itertools.permutations([1, 2, 3], 2))", "Permutations"),
        ("list(itertools.combinations([1, 2, 3, 4], 2))", "Combinations"),
    ]),
    
    # JSON & Data Processing
    ("💾 Data Processing", [
        ("json.dumps({'name': 'Alice', 'age': 30}, indent=2)", "JSON formatting"),
        ("re.findall(r'\\d+', 'Order 123 costs $45')", "Regex extraction"),
    ]),
]

for category, tests in demos:
    print(f"\n{category}")
    print("─" * 80)
    for code, description in tests:
        print(f"\n  📌 {description}")
        print(f"     Code: {code}")
        try:
            result = sandbox.run(code=code)
            # Shorten long results
            result_str = str(result)
            if len(result_str) > 70:
                result_str = result_str[:67] + "..."
            print(f"     Result: {result_str}")
        except Exception as e:
            print(f"     Error: {e}")

print("\n" + "=" * 80)
print("✅ All new modules demonstrated!")
print("=" * 80)

# Summary
print("\n📦 Available Module Categories:")
print("   • Math & Science: math, random, statistics, decimal, fractions")
print("   • Data Structures: collections, itertools, functools, operator, heapq, bisect")
print("   • Text Processing: re, json, string, textwrap")
print("   • Date & Time: datetime, time")
print("   • Encoding & Security: hashlib, base64, uuid, secrets")
print("   • Utilities: copy")
print("\n   Plus all standard Python builtins (print, len, sum, etc.)")
