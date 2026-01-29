# VAF Tools - Developer Guide

This directory contains all tools for the VAF (AI Agent Framework) system. Tools are Python classes that are automatically discovered and loaded.

---

## 📚 Table of Contents

1. [How VAF Tools Work](#how-vaf-tools-work)
2. [Adding a Custom Tool](#adding-a-custom-tool)
3. [Adding an MCP Tool](#adding-an-mcp-tool)
4. [Tool Examples](#tool-examples)
5. [Best Practices](#best-practices)

---

## 🔧 How VAF Tools Work

### Architecture

VAF uses an **automatic plugin system**:

1. **Automatic Discovery**: All Python files in `vaf/tools/` are scanned at startup
2. **BaseTool Pattern**: Each tool inherits from `BaseTool` and implements `run()`
3. **JSON Schema**: Tools define their parameters using JSON Schema
4. **In-Process Execution**: Tools run in the same Python process (very fast, <1ms latency)

### Tool Structure

```python
from vaf.tools.base import BaseTool

class MyTool(BaseTool):
    # REQUIRED: Tool name (used by the LLM)
    name = "my_tool"
    
    # REQUIRED: Description for the LLM
    description = "What does this tool do?"
    
    # OPTIONAL: Parameter schema (JSON Schema)
    parameters = {
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": "Input parameter"
            }
        },
        "required": ["input"]
    }
    
    # OPTIONAL: Only available to Coder Sub-Agent
    coder_only = False
    
    # REQUIRED: Main logic
    def run(self, **kwargs) -> str:
        input_text = kwargs.get("input", "")
        # Tool logic here
        return "Result as string"
```

### Tool Lifecycle

1. **Startup**: VAF scans `vaf/tools/` and loads all `BaseTool` subclasses
2. **Registration**: Tools are registered in `agent.tools` dictionary
3. **Execution**: LLM calls tools via `tool_calls`
4. **Result**: Tool returns a string that is read by the LLM

---

## 🛠️ Adding a Custom Tool

### Step 1: Create New File

Create a new Python file in `vaf/tools/`:

```bash
# Example: vaf/tools/my_custom_tool.py
```

### Step 2: Implement Tool Class

```python
# vaf/tools/my_custom_tool.py
from vaf.tools.base import BaseTool

class MyCustomTool(BaseTool):
    name = "my_custom_tool"
    description = "Describes what this tool does. The LLM reads this!"
    
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query"
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results",
                "default": 10
            }
        },
        "required": ["query"]
    }
    
    def run(self, **kwargs) -> str:
        query = kwargs.get("query", "")
        limit = kwargs.get("limit", 10)
        
        # Your tool logic here
        result = f"Searching for: {query}, Limit: {limit}"
        
        return result
```

### Step 3: Done! 🎉

The tool is **automatically discovered** on the next VAF startup. No manual registration needed!

### Example: Simple Calculator Tool

```python
# vaf/tools/calculator.py
from vaf.tools.base import BaseTool

class CalculatorTool(BaseTool):
    name = "calculator"
    description = "Performs mathematical calculations"
    
    parameters = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "Mathematical expression (e.g., '2 + 2' or '10 * 5')"
            }
        },
        "required": ["expression"]
    }
    
    def run(self, **kwargs) -> str:
        expression = kwargs.get("expression", "")
        
        try:
            # Security: Only allow mathematical expressions
            allowed_chars = set("0123456789+-*/.() ")
            if not all(c in allowed_chars for c in expression):
                return "Error: Only mathematical expressions allowed"
            
            result = eval(expression)  # In production: use a safer parser
            return f"Result: {result}"
        except Exception as e:
            return f"Error: {e}"
```

---

## 🔌 Adding an MCP Tool

MCP (Model Context Protocol) enables integration of tools from other languages and ecosystems.

### What is MCP?

- **Standardized protocol** for tool integration
- **Language-agnostic**: Tools can be written in Rust, Go, Node.js, etc.
- **JSON-RPC**: Communication over stdio, HTTP, or SSE
- **Ecosystem**: Many pre-built MCP servers available

### Option 1: Use MCP Client Tool (Recommended)

VAF already has an `mcp_client` tool that calls external MCP servers:

```python
# The LLM can now call MCP tools:
# mcp_call(
#   server_command="npx -y @modelcontextprotocol/server-filesystem",
#   tool_name="read_file",
#   arguments={"path": "/path/to/file.txt"}
# )
```

**Example: Using GitHub MCP Server**

```python
# VAF can now use GitHub tools without rewriting them:
# mcp_call(
#   server_command="npx -y @modelcontextprotocol/server-github",
#   tool_name="search_repositories",
#   arguments={"query": "python ai"}
# )
```

### Option 2: Create Your Own MCP Server

If you have your own MCP server:

1. **Start the server** (e.g., as Node.js/Go/Rust process)
2. **Use MCP Client Tool**:

```python
# Example: Custom MCP server
# mcp_call(
#   server_command="python my_mcp_server.py",
#   tool_name="my_custom_tool",
#   arguments={"param1": "value1"}
# )
```

### MCP Server Examples

**Filesystem MCP Server:**
```bash
# Install
npm install -g @modelcontextprotocol/server-filesystem

# Use in VAF:
# mcp_call(
#   server_command="npx -y @modelcontextprotocol/server-filesystem",
#   tool_name="read_file",
#   arguments={"path": "~/Documents/file.txt"}
# )
```

**GitHub MCP Server:**
```bash
# Install
npm install -g @modelcontextprotocol/server-github

# Use in VAF:
# mcp_call(
#   server_command="npx -y @modelcontextprotocol/server-github",
#   tool_name="search_repositories",
#   arguments={"query": "machine learning"}
# )
```

### MCP vs. Native VAF Tools

| Aspect | Native VAF Tool | MCP Tool |
|--------|----------------|----------|
| **Performance** | <1ms (in-process) | 5-50ms (IPC/Network) |
| **Development** | Very simple (Python) | More complex (JSON-RPC) |
| **Language** | Python only | Any language |
| **Isolation** | Low | High (separate process) |
| **Reusability** | VAF only | Any MCP system |

**Recommendation:**
- **Native VAF Tools** for core functionality (fast, simple)
- **MCP Tools** for external integrations (GitHub, APIs, etc.)

---

## 📖 Tool Examples

### Example 1: Web Search Tool

```python
# vaf/tools/search.py (simplified)
from vaf.tools.base import BaseTool
import requests

class WebSearchTool(BaseTool):
    name = "web_search"
    description = "Searches the internet"
    
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "default": 5}
        },
        "required": ["query"]
    }
    
    def run(self, **kwargs) -> str:
        query = kwargs.get("query")
        # Search logic here
        return f"Search results for: {query}"
```

### Example 2: File System Tool

```python
# vaf/tools/filesystem.py (simplified)
from vaf.tools.base import BaseTool
from pathlib import Path

class ReadFileTool(BaseTool):
    name = "read_file"
    description = "Reads a file"
    coder_only = True  # Only for Coder Sub-Agent
    
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"}
        },
        "required": ["path"]
    }
    
    def run(self, **kwargs) -> str:
        path = kwargs.get("path")
        file = Path(path)
        if file.exists():
            return file.read_text()
        return f"Error: File not found: {path}"
```

### Example 3: MCP Client Tool

```python
# vaf/tools/mcp_client.py
# See full implementation above
```

---

## ✅ Best Practices

### 1. Tool Names

- **Clear and descriptive**: `web_search`, not `ws`
- **Lowercase with underscores**: `read_file`, not `ReadFile`
- **No abbreviations**: `coding_agent`, not `ca`

### 2. Descriptions

- **Precise**: Explain exactly what the tool does
- **Context**: Mention important limitations
- **Examples**: Provide examples if helpful

```python
# ✅ GOOD
description = "Searches the internet for current information. Supports deep search for detailed page analysis."

# ❌ BAD
description = "Searches stuff"
```

### 3. Parameter Schema

- **Complete**: Define all parameters with types
- **Descriptions**: Every parameter needs a description
- **Defaults**: Use defaults where appropriate

```python
parameters = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Search query (e.g., 'Python AI Framework')"
        },
        "max_results": {
            "type": "integer",
            "description": "Maximum number of results (1-20)",
            "default": 5
        }
    },
    "required": ["query"]
}
```

### 4. Error Handling

- **Always return strings**: Even on errors
- **Clear error messages**: Explain what went wrong
- **Don't throw exceptions**: Catch all exceptions

```python
def run(self, **kwargs) -> str:
    try:
        # Tool logic
        return result
    except FileNotFoundError:
        return "Error: File not found"
    except Exception as e:
        return f"Error: {e}"
```

### 5. coder_only Flag

Use `coder_only = True` for:
- **File Operations**: `read_file`, `write_file`, `list_files`
- **Shell Commands**: `bash`, `python_exec`
- **Code-specific Tools**: `codesearch`, `batch`

**Why?** These tools are too "low-level" for the Main Agent and are better used by Sub-Agents (e.g., `coding_agent`).

### 6. Performance

- **Fast**: Tools should take <100ms (if possible)
- **Caching**: Cache expensive operations
- **Async**: For I/O-intensive tools, use async if needed

### 7. Testing

```python
# Test your tool:
tool = MyTool()
result = tool.run(input="test")
assert "Result" in result
```

---

## 🚀 Advanced Features

### Sub-Agent Filtering

Tools can be available only to specific sub-agents:

```python
class MyTool(BaseTool):
    coder_only = True  # Only available to coding_agent
    # Or: Automatically excluded if name in MAIN_AGENT_EXCLUDED_TOOLS
```

### Workflow Integration

Tools can be used in workflows:

```python
# workflow_steps:
{
    "tool": "my_tool",
    "input": "{variable}",
    "output": "result_variable"
}
```

### Trust System

VAF has a trust system for unsafe operations:

```python
from vaf.core.trust import is_trusted_dir, mark_trusted_dir

if not is_trusted_dir(Path.cwd()):
    return "Error: Directory not trusted"
```

---

## 📚 Additional Resources

- **BaseTool Documentation**: `vaf/tools/base.py`
- **Tool Examples**: See other tools in this directory
- **MCP Specification**: https://modelcontextprotocol.io
- **VAF Documentation**: `docs/TOOL_ROUTER_ARCHITECTURE.md`

---

## ❓ FAQ

**Q: Why isn't my tool being recognized?**
A: Make sure:
- The file is in `vaf/tools/`
- The class inherits from `BaseTool`
- `name` and `description` are defined
- `run()` is implemented

**Q: Can I organize tools in subdirectories?**
A: Not currently. All tools must be directly in `vaf/tools/`.

**Q: How do I test my tool?**
A: Restart VAF and ask the LLM if it sees the tool. Or test directly:
```python
from vaf.tools.my_tool import MyTool
tool = MyTool()
print(tool.run(input="test"))
```

**Q: MCP vs. Native Tool - which to choose?**
A: 
- **Native**: For VAF-specific, performance-critical tools
- **MCP**: For external integrations, tools in other languages

---

*Last updated: 2026*

