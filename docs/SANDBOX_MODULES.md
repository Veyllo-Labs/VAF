# 🚀 Python Sandbox - Module Reference

This file documents the common standard-library modules that are expected to work in sandbox snippets.

> Runtime note: VAF executes `python_sandbox` in a dedicated Docker-based sandbox.  
> The exact import policy is enforced by the runtime image and sandbox guardrails, not by this markdown file.

## ✨ Newly Added:

### ⏰ Time & Performance
- **`time`** - Timestamps, performance measurement
  ```python
  time.time()  # → 1769448216.053
  ```

### 🔒 Cryptography & Encoding (safe, no network)
- **`hashlib`** - Hashing (SHA-256, MD5, etc.)
  ```python
  hashlib.sha256(b'Hello').hexdigest()  # → 185f8db32271...
  ```
  
- **`base64`** - Base64 Encoding/Decoding
  ```python
  base64.b64encode(b'Hello').decode()  # → 'SGVsbG8='
  ```
  
- **`uuid`** - UUID Generation
  ```python
  uuid.uuid4()  # → UUID('42526a69-3ada-4ab6-9a64-e7e01484db83')
  ```
  
- **`secrets`** - Secure random numbers (cryptographically secure)
  ```python
  secrets.token_hex(8)  # → '044c504a816efc2e'
  ```

### 📝 String & Text Processing
- **`string`** - String constants (ascii_letters, punctuation, etc.)
  ```python
  string.ascii_uppercase  # → 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
  string.punctuation      # → '!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~'
  ```
  
- **`textwrap`** - Text formatting & wrapping
  ```python
  textwrap.shorten('This is a very long text', width=15)  # → 'This is a [...]'
  ```

### 🧮 Algorithms
- **`heapq`** - Heap operations (Priority Queue)
  ```python
  heapq.nlargest(3, [1, 9, 2, 8, 3, 7])  # → [9, 8, 7]
  ```
  
- **`bisect`** - Binary search in sorted lists
  ```python
  bisect.bisect_left([1, 2, 4, 5], 3)  # → 2
  ```

### 📋 Utilities
- **`copy`** - Shallow/Deep Copy
  ```python
  copy.copy([1, 2, 3])              # Shallow copy
  copy.deepcopy({'a': [1, 2]})      # Deep copy
  ```

---

## 📦 Complete List of Available Modules:

### **Math & Science**
- `math` - Mathematical functions (sqrt, sin, cos, etc.)
- `random` - Random numbers
- `statistics` - Statistical functions (mean, median, stdev)
- `decimal` - Precise decimal arithmetic
- `fractions` - Rational numbers

### **Data Structures**
- `collections` - Counter, defaultdict, deque, etc.
- `itertools` - Iterators (permutations, combinations, etc.)
- `functools` - Higher-order functions (reduce, partial, cache)
- `operator` - Operator functions (add, mul, etc.)
- `heapq` - Heap/Priority Queue
- `bisect` - Binary Search

### **Text Processing**
- `re` - Regular Expressions
- `json` - JSON Parsing/Serialization
- `string` - String constants
- `textwrap` - Text formating

### **Date & Time**
- `datetime` - Date and Time
- `time` - Timestamps and Performance

### **Encoding & Security**
- `hashlib` - Hashing (SHA, MD5, etc.)
- `base64` - Base64 Encoding
- `uuid` - UUID Generation
- `secrets` - Secure random numbers

### **Utilities**
- `copy` - Object copying

### **Built-in Functions**
All standard Python builtins: `print`, `len`, `sum`, `map`, `filter`, `sorted`, `enumerate`, `zip`, `range`, etc.

---

## 💡 Examples:

```python
# 1. Simple Expressions (Auto-Evaluation)
1 + 1                                    # → 2
math.sqrt(16)                            # → 4.0
[x**2 for x in range(5)]                 # → [0, 1, 4, 9, 16]

# 2. Hashing & Encoding
hashlib.md5(b'password').hexdigest()     # → '5f4dcc3b5aa765d61d8327deb882cf99'
base64.b64encode(b'secret').decode()     # → 'c2VjcmV0'

# 3. UUID & Random
uuid.uuid4()                             # → UUID('...')
secrets.token_urlsafe(16)                # → Secure URL-safe Token

# 4. Timestamps
time.time()                              # → 1769448216.053
datetime.datetime.now()                  # → datetime.datetime(2026, 1, 26, 18, 23, 44)

# 5. Algorithms
heapq.nsmallest(3, [5, 1, 9, 2, 8])     # → [1, 2, 5]
bisect.insort([1, 3, 5], 4)             # Insert sorted

# 6. Collections
collections.Counter('hello').most_common()  # → [('l', 2), ('h', 1), ('e', 1), ('o', 1)]
list(itertools.combinations([1, 2, 3], 2))  # → [(1, 2), (1, 3), (2, 3)]

# 7. Text Processing
re.findall(r'\d+', 'Order 123')         # → ['123']
json.dumps({'key': 'value'})            # → '{"key": "value"}'
textwrap.wrap('Long text...', width=10) # → ['Long', 'text...']
```

---

## 🔒 Security

**Blocked / restricted** (policy enforced by sandbox runtime):
- ❌ Host file-system access outside sandbox policy
- ❌ Raw network/socket usage
- ❌ Process spawning (`subprocess`, `os.system`)
- ❌ Arbitrary runtime escapes / unsafe dynamic execution

**Allowed** (typical in-sandbox usage):
- ✅ Pure calculations and data processing
- ✅ String manipulation and Regex
- ✅ Hashing and Encoding
- ✅ Algorithms and Data Structures
- ✅ Timestamps (read-only, no system modification)

For architecture and isolation details, see [`SANDBOXING.md`](SANDBOXING.md).

---

## 🎯 Use Cases

1. **Mathematical Calculations**: `math.factorial(10)`
2. **Data Processing**: JSON parsing, Regex, Collections
3. **Password Hashing**: `hashlib.sha256(password.encode()).hexdigest()`
4. **Token Generation**: `secrets.token_hex(16)`
5. **UUID Generation**: `uuid.uuid4()`
6. **Algorithms**: Sorting, Searching, Heap operations
7. **Statistical Analysis**: `statistics.mean([...])`
8. **Time & Performance**: `time.time()` for timestamps
