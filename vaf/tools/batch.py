# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Batch Tool - Execute multiple tools in parallel
Reduces latency by running independent operations concurrently
"""
import concurrent.futures
from typing import Dict, Any, List
import importlib
import traceback

from vaf.tools.base import BaseTool


class BatchTool(BaseTool):
    """Execute multiple tool calls concurrently."""
    
    name = "batch"
    permission_level = "system"
    side_effect_class = "reversible"
    coder_only = True  # Only available to Coder Sub-Agent
    description = """Execute multiple independent tool calls concurrently.

Use this tool when you need to perform several operations that don't depend on each other's results.
This significantly reduces latency compared to sequential execution.

WHEN TO USE:
✓ Reading multiple files at once
✓ Running several independent searches
✓ Executing independent shell commands

WHEN NOT TO USE:
✗ When operations depend on previous results
✗ When order of execution matters

Example:
batch(operations=[
    {"tool": "bash", "args": {"command": "ls"}},
    {"tool": "bash", "args": {"command": "pwd"}}
])"""
    
    parameters = {
        "type": "object",
        "properties": {
            "operations": {
                "type": "array",
                "description": "List of tool operations: [{tool: 'name', args: {...}}, ...]"
            },
            "max_concurrent": {
                "type": "integer",
                "description": "Maximum concurrent operations (default: 5)"
            }
        },
        "required": ["operations"]
    }
    
    # Reference to available tools (set by agent)
    available_tools: Dict = {}
    
    def run(self, **kwargs) -> str:
        operations = kwargs.get("operations", [])
        max_concurrent = min(kwargs.get("max_concurrent", 5), 10)
        
        if not operations:
            return "Error: No operations provided"
        
        results = []
        
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as executor:
                futures = []
                
                for op in operations:
                    if not isinstance(op, dict):
                        continue
                    
                    tool_name = op.get("tool")
                    args = op.get("args", {})
                    
                    if not tool_name:
                        results.append({"tool": "?", "success": False, "result": "No tool specified"})
                        continue
                    
                    future = executor.submit(self._execute_tool, tool_name, args)
                    futures.append((tool_name, future))
                
                for tool_name, future in futures:
                    try:
                        result = future.result(timeout=120)
                        results.append({"tool": tool_name, "success": True, "result": result})
                    except Exception as e:
                        results.append({"tool": tool_name, "success": False, "result": str(e)})
        
        except Exception as e:
            return f"Error in batch execution: {e}"
        
        # Format output
        output = ["═" * 50, "BATCH RESULTS", "═" * 50, ""]
        
        for i, res in enumerate(results, 1):
            status = "✓" if res.get("success") else "✗"
            output.append(f"[{i}] {status} {res['tool']}")
            
            result_text = str(res.get('result', ''))
            if len(result_text) > 200:
                result_text = result_text[:200] + "..."
            output.append(f"    {result_text}")
            output.append("")
        
        return "\n".join(output)
    
    def _execute_tool(self, tool_name: str, args: Dict[str, Any]) -> str:
        """Execute a single tool."""
        # Try to find tool in available_tools
        if tool_name in self.available_tools:
            tool = self.available_tools[tool_name]
            return tool.run(**args)
        
        # Try to dynamically load
        try:
            module = importlib.import_module(f"vaf.tools.{tool_name}")
            
            # Find BaseTool subclass
            import inspect
            for _, obj in inspect.getmembers(module):
                if inspect.isclass(obj) and issubclass(obj, BaseTool) and obj is not BaseTool:
                    instance = obj()
                    return instance.run(**args)
            
            return f"Tool '{tool_name}' has no executable class"
        except ImportError:
            return f"Tool '{tool_name}' not found"
        except Exception as e:
            return f"Error executing {tool_name}: {e}"
