"""
MCP server registry — discover the tools of configured MCP servers and expose each one as a native
VAF tool (a dynamically-built BaseTool named ``mcp_<server>_<tool>``).

Servers are declared in a hot-reloadable manifest ``mcp_servers.json`` (manifest style, mirroring
vaf/core/custom_tools_registry.py — not config.py, which is for core/sacred settings):

    {
      "servers": {
        "filesystem": {
          "command": "npx -y @modelcontextprotocol/server-filesystem /some/path",
          "transport": "stdio",          # stdio (default) | http | sse
          "enabled": true,
          "permission_level": "write",    # default "write" (plan-gated, automation-safe);
                                          # "dangerous" forces a confirmation prompt; "read" = no gate
          "url": ""                       # only for http/sse
        }
      }
    }

Discovery is eager + parallel with a per-batch deadline: a server that is slow / hung / misconfigured
is terminated and skipped — it never blocks VAF startup (same discipline as the bootstrap fix).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ── Manifest location + I/O (mirrors custom_tools_registry) ──────────────────────────────────────

def get_mcp_manifest_path() -> Path:
    """Path to mcp_servers.json (created lazily next to the custom-tools data)."""
    from vaf.core.platform import Platform
    directory = Platform.data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "mcp_servers.json"


def load_mcp_manifest() -> Dict[str, Any]:
    """Read mcp_servers.json. Returns {} on a missing or malformed file (fail-open, never raises)."""
    path = get_mcp_manifest_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.error("mcp_registry: failed to read %s: %s", path, exc)
        return {}


def save_mcp_manifest(data: Dict[str, Any]) -> None:
    """Atomically write the manifest (temp file + rename), like the custom-tools manifest."""
    path = get_mcp_manifest_path()
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        tmp.replace(path)
    except Exception as exc:
        logger.error("mcp_registry: failed to write %s: %s", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _safe(name: str) -> str:
    """Sanitize a server/tool name into a valid tool-name segment ([A-Za-z0-9_])."""
    s = re.sub(r"[^A-Za-z0-9_]", "_", str(name).strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "x"


# ── Dynamic tool factory ─────────────────────────────────────────────────────────────────────────

def make_mcp_tool(server_name: str, server_cfg: Dict[str, Any], tool_meta: Dict[str, Any]):
    """Build a BaseTool subclass wrapping a single MCP tool. The LLM sees a normal native tool; run()
    delegates to the shared MCP client (warm process cache) with server + tool pre-bound."""
    from vaf.tools.base import BaseTool
    from vaf.tools.mcp_client import get_mcp_client

    real_tool = str(tool_meta.get("name", "")).strip()
    tool_name = f"mcp_{_safe(server_name)}_{_safe(real_tool)}"
    base_desc = str(tool_meta.get("description") or real_tool).strip()
    description = f"{base_desc} (via MCP server '{server_name}')"
    parameters = tool_meta.get("inputSchema")
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    permission = str(server_cfg.get("permission_level", "write")).strip().lower()
    if permission not in ("read", "write", "dangerous", "system"):
        permission = "write"
    command = str(server_cfg.get("command", ""))
    transport = str(server_cfg.get("transport", "stdio"))
    server_url = str(server_cfg.get("url", ""))

    def _run(self, **kwargs) -> str:
        client = get_mcp_client()
        try:
            if transport == "stdio":
                return client._call_stdio(command, real_tool, kwargs)
            if transport == "http":
                return client._call_http(server_url, real_tool, kwargs)
            if transport == "sse":
                return client._call_sse(server_url, real_tool, kwargs)
            return f"Error: Unsupported MCP transport '{transport}'"
        except Exception as exc:
            return f"Error calling MCP tool '{real_tool}': {exc}"

    attrs = {
        "name": tool_name,
        "description": description,
        "parameters": parameters,
        "permission_level": permission,
        "side_effect_class": "irreversible",
        "run": _run,
        "__doc__": description,
    }
    return type(f"MCPTool_{tool_name}", (BaseTool,), attrs)


# ── Eager + parallel discovery ─────────────────────────────────────────────────────────────────────

def discover_mcp_tools(timeout_seconds: float = 5.0) -> Dict[str, Any]:
    """Connect to every enabled server in the manifest in parallel, list its tools, and return a
    {tool_name: BaseTool instance} dict. A server slower than the shared deadline is terminated and
    skipped — discovery never blocks longer than ~timeout_seconds and never raises."""
    manifest = load_mcp_manifest()
    servers = manifest.get("servers", {}) if isinstance(manifest, dict) else {}
    enabled = [
        (name, cfg) for name, cfg in servers.items()
        if isinstance(cfg, dict) and cfg.get("enabled", True) and cfg.get("command")
    ]
    if not enabled:
        return {}

    from vaf.tools.mcp_client import get_mcp_client
    client = get_mcp_client()

    discovered: Dict[str, List[Dict[str, Any]]] = {}

    def _discover(name: str, cfg: Dict[str, Any]) -> None:
        try:
            discovered[name] = client.list_server_tools(
                str(cfg.get("command", "")), str(cfg.get("transport", "stdio")), str(cfg.get("url", "")),
            )
        except Exception:
            discovered[name] = []

    threads = []
    for name, cfg in enabled:
        # Daemon threads so a hung server can never block process exit.
        th = threading.Thread(target=_discover, args=(name, cfg), daemon=True)
        th.start()
        threads.append((name, cfg, th))

    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    for _name, _cfg, th in threads:
        th.join(max(0.0, deadline - time.monotonic()))

    tools: Dict[str, Any] = {}
    for name, cfg, th in threads:
        if th.is_alive():
            # Timed out: terminate the server process to unblock the daemon thread, then skip it.
            try:
                proc = client._server_processes.get(str(cfg.get("command", "")))
                if proc is not None and proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
            logger.warning("MCP server '%s' timed out during discovery — skipped", name)
            continue
        for tm in discovered.get(name, []) or []:
            if not isinstance(tm, dict) or not tm.get("name"):
                continue
            try:
                inst = make_mcp_tool(name, cfg, tm)()
                tools[inst.name] = inst
            except Exception as exc:
                logger.warning("mcp_registry: failed to build tool %s/%s: %s", name, tm.get("name"), exc)
    if tools:
        logger.info("mcp_registry: registered %d MCP tool(s) from %d server(s)", len(tools), len(enabled))
    return tools
