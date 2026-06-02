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
    permission_level = "write"
    side_effect_class = "irreversible"
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
    
    def _ensure_server(self, server_command: str, env: Optional[Dict[str, str]] = None) -> Optional[subprocess.Popen]:
        """Spawn (if needed) and initialize the stdio MCP server, cached by command. Returns the
        running process, or None on failure. Shared by mcp_call, discovery, and dynamic MCP tools.
        `env` (e.g. API tokens from the manifest) is merged onto os.environ for the child process."""
        cache_key = server_command
        proc = self._server_processes.get(cache_key)
        if proc is not None and proc.poll() is None:
            return proc
        # (Re)spawn the server process.
        try:
            cmd_parts = server_command.split() if isinstance(server_command, str) else server_command
            import platform
            popen_kwargs = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "bufsize": 1,
            }
            if env:
                popen_kwargs["env"] = {**os.environ, **{str(k): str(v) for k, v in env.items()}}
            if platform.system() == "Windows":
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            process = subprocess.Popen(cmd_parts, **popen_kwargs)
            self._server_processes[cache_key] = process
        except Exception:
            return None
        # Initialize handshake.
        init = self._json_rpc(process, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "VAF", "version": "1.0"},
        }, request_id=1)
        if init is None or "error" in init:
            try:
                process.terminate()
            except Exception:
                pass
            self._server_processes.pop(cache_key, None)
            return None
        # Per the MCP spec, signal initialized (fire-and-forget notification).
        try:
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            process.stdin.flush()
        except Exception:
            pass
        self._server_initialized[cache_key] = True
        return process

    def _json_rpc(self, process: subprocess.Popen, method: str, params: Dict[str, Any],
                  request_id: int, max_reads: int = 20) -> Optional[Dict[str, Any]]:
        """Send a JSON-RPC request and return the response object matching request_id, skipping
        notifications / unrelated lines. Returns None on EOF or error (never raises)."""
        try:
            req = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            process.stdin.write(json.dumps(req) + "\n")
            process.stdin.flush()
        except Exception:
            return None
        for _ in range(max_reads):
            try:
                line = process.stdout.readline()
            except Exception:
                return None
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == request_id:
                return msg
            # Otherwise a notification or another response — keep reading.
        return None

    def list_server_tools(self, server_command: str, transport: str = "stdio",
                          server_url: str = "", env: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """Discover the tools a server offers via JSON-RPC tools/list. Returns a list of tool dicts
        (each with name / description / inputSchema), or [] on any failure (never raises)."""
        try:
            if transport == "http":
                # Best-effort HTTP discovery (mirrors the simple _call_http shape). SSE is not
                # supported for discovery.
                if not server_url:
                    return []
                import requests
                r = requests.post(f"{server_url}/tools/list", json={}, timeout=15)
                r.raise_for_status()
                tools = (r.json() or {}).get("tools", [])
                return tools if isinstance(tools, list) else []
            if transport != "stdio":
                return []  # sse discovery not supported
            process = self._ensure_server(server_command, env)
            if process is None:
                return []
            resp = self._json_rpc(process, "tools/list", {}, request_id=3)
            if not resp or "error" in resp or "result" not in resp:
                return []
            tools = resp["result"].get("tools", [])
            return tools if isinstance(tools, list) else []
        except Exception:
            return []

    def _call_stdio(self, server_command: str, tool_name: str, arguments: Dict[str, Any],
                    env: Optional[Dict[str, str]] = None) -> str:
        """Call an MCP tool via stdio transport (JSON-RPC over stdin/stdout) — the most common MCP
        transport."""
        process = self._ensure_server(server_command, env)
        if process is None:
            return "Error: Failed to initialize MCP server"

        response = self._json_rpc(process, "tools/call",
                                  {"name": tool_name, "arguments": arguments or {}}, request_id=2)
        if response is None:
            return "Error: No response from MCP server"
        if "error" in response:
            error = response["error"]
            return f"MCP Error: {error.get('message', 'Unknown error')} (code: {error.get('code', 'unknown')})"
        if "result" not in response:
            return "Error: No result in MCP response"

        result = response["result"]
        # MCP tools return a content array.
        if "content" in result:
            content = result["content"]
            if isinstance(content, list) and len(content) > 0:
                text_parts = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                        elif "text" in item:
                            text_parts.append(str(item["text"]))
                return "\n".join(text_parts) if text_parts else str(result)
            return str(result)
        return str(result)
    
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


# Process-wide shared MCP client. mcp_call, discovery, and dynamic MCP tools all go through this so
# they reuse the same warm server processes (cached in _server_processes) instead of re-spawning.
_shared_client: Optional[MCPClientTool] = None


def get_mcp_client() -> MCPClientTool:
    """Return the process-wide shared MCPClientTool (created on first use)."""
    global _shared_client
    if _shared_client is None:
        _shared_client = MCPClientTool()
    return _shared_client

