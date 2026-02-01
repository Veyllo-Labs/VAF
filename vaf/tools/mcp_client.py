"""
MCP Client Tool - Bridge to external Model Context Protocol servers

This tool allows VAF to interact with external MCP servers, enabling
integration with tools written in any language and following the MCP standard.

MCP (Model Context Protocol) uses JSON-RPC over stdio, HTTP, or SSE.
This implementation supports stdio transport (most common).
"""

import json
import subprocess
import sys
import os
from typing import Dict, Any, Optional, List
from pathlib import Path

from vaf.tools.base import BaseTool
from vaf.cli.ui import UI


class MCPClientTool(BaseTool):
    """
    Tool that acts as a bridge to external MCP servers.
    
    This allows VAF to use tools from the MCP ecosystem without
    requiring them to be rewritten as VAF tools.
    """
    
    name = "mcp_call"
    description = "Call external tools via Model Context Protocol (MCP). Connects to MCP servers to access tools written in any language."
    
    parameters = {
        "type": "object",
        "properties": {
            "server_command": {
                "type": "string",
                "description": "Command to start the MCP server (e.g., 'npx -y @modelcontextprotocol/server-filesystem' or path to executable)"
            },
            "tool_name": {
                "type": "string",
                "description": "Name of the MCP tool to call"
            },
            "arguments": {
                "type": "object",
                "description": "Arguments to pass to the MCP tool (as key-value pairs)"
            },
            "transport": {
                "type": "string",
                "enum": ["stdio", "http", "sse"],
                "description": "Transport method (default: stdio)",
                "default": "stdio"
            },
            "server_url": {
                "type": "string",
                "description": "Server URL (required for http/sse transport)"
            }
        },
        "required": ["server_command", "tool_name"]
    }
    
    def __init__(self):
        super().__init__()
        # Cache for active MCP server processes
        self._server_processes: Dict[str, subprocess.Popen] = {}
        self._server_initialized: Dict[str, bool] = {}
    
    def run(self, **kwargs) -> str:
        """
        Execute an MCP tool call.
        
        Args:
            server_command: Command to start MCP server
            tool_name: Name of tool to call
            arguments: Tool arguments (dict)
            transport: Transport method (stdio/http/sse)
            server_url: Server URL (for http/sse)
        
        Returns:
            Tool result as string
        """
        server_command = kwargs.get("server_command", "")
        tool_name = kwargs.get("tool_name", "")
        arguments = kwargs.get("arguments", {})
        transport = kwargs.get("transport", "stdio")
        server_url = kwargs.get("server_url", "")
        
        if not server_command or not tool_name:
            return "Error: server_command and tool_name are required"
        
        try:
            if transport == "stdio":
                return self._call_stdio(server_command, tool_name, arguments)
            elif transport == "http":
                return self._call_http(server_url, tool_name, arguments)
            elif transport == "sse":
                return self._call_sse(server_url, tool_name, arguments)
            else:
                return f"Error: Unsupported transport '{transport}'"
        except Exception as e:
            return f"Error calling MCP tool: {e}"
    
    def _call_stdio(self, server_command: str, tool_name: str, arguments: Dict[str, Any]) -> str:
        """
        Call MCP tool via stdio transport (JSON-RPC over stdin/stdout).
        
        This is the most common MCP transport method.
        """
        # Use server_command as cache key
        cache_key = server_command
        
        # Initialize server if not already done
        if cache_key not in self._server_initialized:
            if not self._initialize_server(server_command):
                return "Error: Failed to initialize MCP server"
        
        # Get or create server process
        if cache_key not in self._server_processes:
            try:
                # Parse command (handle both string and list)
                if isinstance(server_command, str):
                    cmd_parts = server_command.split()
                else:
                    cmd_parts = server_command
                
                # Start MCP server process
                import platform
                popen_kwargs = {
                    "stdin": subprocess.PIPE,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                    "text": True,
                    "bufsize": 1
                }
                if platform.system() == "Windows":
                    popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                process = subprocess.Popen(cmd_parts, **popen_kwargs)
                self._server_processes[cache_key] = process
                
                # Send initialize request
                init_request = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "VAF",
                            "version": "1.0"
                        }
                    }
                }
                
                process.stdin.write(json.dumps(init_request) + "\n")
                process.stdin.flush()
                
                # Read initialize response
                response_line = process.stdout.readline()
                if response_line:
                    response = json.loads(response_line.strip())
                    if "error" in response:
                        return f"Error initializing MCP server: {response['error']}"
                
                self._server_initialized[cache_key] = True
                
            except Exception as e:
                return f"Error starting MCP server: {e}"
        
        process = self._server_processes[cache_key]
        
        # Send tools/call request
        request_id = 2
        call_request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments
            }
        }
        
        try:
            # Send request
            process.stdin.write(json.dumps(call_request) + "\n")
            process.stdin.flush()
            
            # Read response
            response_line = process.stdout.readline()
            if not response_line:
                return "Error: No response from MCP server"
            
            response = json.loads(response_line.strip())
            
            # Check for errors
            if "error" in response:
                error = response["error"]
                return f"MCP Error: {error.get('message', 'Unknown error')} (code: {error.get('code', 'unknown')})"
            
            # Extract result
            if "result" in response:
                result = response["result"]
                
                # MCP tools return content array
                if "content" in result:
                    content = result["content"]
                    if isinstance(content, list) and len(content) > 0:
                        # Extract text from content items
                        text_parts = []
                        for item in content:
                            if isinstance(item, dict):
                                if item.get("type") == "text":
                                    text_parts.append(item.get("text", ""))
                                elif "text" in item:
                                    text_parts.append(str(item["text"]))
                        return "\n".join(text_parts) if text_parts else str(result)
                    else:
                        return str(result)
                else:
                    return str(result)
            else:
                return "Error: No result in MCP response"
                
        except json.JSONDecodeError as e:
            return f"Error parsing MCP response: {e}"
        except Exception as e:
            return f"Error communicating with MCP server: {e}"
    
    def _initialize_server(self, server_command: str) -> bool:
        """
        Initialize MCP server connection.
        Returns True if successful.
        """
        # This is a placeholder - actual initialization happens in _call_stdio
        # when the process is first created
        return True
    
    def _call_http(self, server_url: str, tool_name: str, arguments: Dict[str, Any]) -> str:
        """
        Call MCP tool via HTTP transport.
        
        Note: HTTP transport requires the MCP server to be running
        as an HTTP server. This is less common than stdio.
        """
        import requests
        
        if not server_url:
            return "Error: server_url required for HTTP transport"
        
        try:
            # MCP HTTP uses POST requests
            response = requests.post(
                f"{server_url}/tools/call",
                json={
                    "name": tool_name,
                    "arguments": arguments
                },
                timeout=30
            )
            
            response.raise_for_status()
            result = response.json()
            
            if "content" in result:
                content = result["content"]
                if isinstance(content, list):
                    return "\n".join(item.get("text", "") for item in content if isinstance(item, dict))
            
            return str(result)
            
        except requests.RequestException as e:
            return f"HTTP Error: {e}"
        except Exception as e:
            return f"Error: {e}"
    
    def _call_sse(self, server_url: str, tool_name: str, arguments: Dict[str, Any]) -> str:
        """
        Call MCP tool via Server-Sent Events (SSE) transport.
        
        Note: SSE is used for streaming responses.
        """
        # SSE implementation would require more complex handling
        # For now, return a message indicating it's not fully implemented
        return "Error: SSE transport not yet fully implemented. Use stdio or http transport."
    
    def __del__(self):
        """Cleanup: Close all server processes on tool destruction."""
        for process in self._server_processes.values():
            try:
                if process.poll() is None:  # Process still running
                    process.terminate()
                    process.wait(timeout=5)
            except Exception:
                pass

