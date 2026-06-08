from vaf.startup_logger import log
log("WebServer", "Module load started")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi import HTTPException, Query
from starlette.requests import Request
import asyncio
import uvicorn
import threading
import inspect
import html
import os
import time
import queue
log("WebServer", "Basic imports done")

from vaf.core.web_interface import get_web_interface
from vaf.core.session import SessionManager, Session
from vaf.cli.autosuggest import SmartAutoSuggest
import json
from vaf.core.config import Config
from vaf.core.log_helper import append_domain_log, get_dated_log_path, is_debug_logging_enabled
from pathlib import Path
from typing import Optional, List
import logging
from vaf.core.tray_context import TrayContext
log("WebServer", "VAF imports done")

log_uvicorn = logging.getLogger("uvicorn")

app = FastAPI(title="VAF Local Server")

# Active model download cancel events keyed by websocket id (for cancel_model_download)
_active_model_download_cancels: dict[int, threading.Event] = {}
_headless_agent_thread: Optional[threading.Thread] = None
_headless_agent_lock = threading.Lock()


def _ensure_headless_agent_runner(origin: str = "web_server") -> None:
    """
    Ensure exactly one in-process headless runner thread is alive.
    Queue producers (WebSocket/API) run in this process, so the consumer must also
    live here to avoid QUEUE_ADD without QUEUE_GET.
    """
    global _headless_agent_thread
    with _headless_agent_lock:
        if _headless_agent_thread is not None and _headless_agent_thread.is_alive():
            return
        from vaf.core.headless_runner import run_headless_agent

        _headless_agent_thread = threading.Thread(
            target=run_headless_agent,
            daemon=True,
            name="HeadlessAgent",
        )
        _headless_agent_thread.start()
        log("WebServer", f"Headless agent runner started ({origin})")


@app.exception_handler(Exception)
async def json_exception_handler(request, exc):
    """Ensure unhandled exceptions return JSON (for API clients) instead of HTML."""
    from fastapi.responses import JSONResponse
    # HTTPException: return JSON; other exceptions: 500 with error message
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "error": str(exc.detail)})
    log_uvicorn.exception("Unhandled exception: %s", exc)
    return JSONResponse(status_code=500, content={"ok": False, "error": str(exc), "detail": str(exc)})


# CORS: Use regex to allow localhost AND all RFC 1918 private network origins.
# This is safe because Layer 2 (IPValidationMiddleware) already blocks non-local IPs.
_CORS_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[0-1])\.\d+\.\d+|192\.168\.\d+\.\d+)(:\d+)?$"

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=_CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Max sessions sent to Web UI (sidebar list); increase if users have many chats.
SESSION_LIST_LIMIT = 500

# Session ID prefixes for channel chats (WhatsApp, Telegram, Discord). These are shown only in their dashboards, not in the main chat list.
_CHANNEL_SESSION_PREFIXES = ("whatsapp_", "telegram_", "discord_")


def _is_channel_session(session_id: str) -> bool:
    """True if this session is a channel/contact chat (WhatsApp, Telegram, Discord), not a main Web UI chat."""
    if not session_id or not isinstance(session_id, str):
        return False
    return session_id.startswith(_CHANNEL_SESSION_PREFIXES)


def _web_ui_sessions(sessions: list) -> list:
    """Filter to sessions that belong in the main Web UI sidebar (exclude channel and thinking-only sessions)."""
    out = []
    for s in sessions:
        sid = s.get("id") or ""
        if _is_channel_session(sid):
            continue
        # Thinking output now goes into web-default; hide legacy thinking_* sessions from the list
        meta = s.get("metadata") or {}
        if meta.get("source") == "thinking" or sid.startswith("thinking_"):
            continue
        out.append(s)
    return out

log("WebServer", "Getting WebInterfaceManager...")
manager = get_web_interface()

# ═══════════════════════════════════════════════════════════════════════════════
# WHISPER MODEL SINGLETON - Prevents memory leak from reloading model per request
# ═══════════════════════════════════════════════════════════════════════════════
_whisper_model = None
_whisper_model_lock = threading.Lock()

def get_whisper_model():
    """Get or lazily load the Whisper STT model (singleton)."""
    global _whisper_model
    if _whisper_model is None:
        with _whisper_model_lock:
            if _whisper_model is None:  # Double-check
                try:
                    import importlib
                    import psutil

                    model_size = Config.get("speech_stt_whisper_model", "base")
                    mem_before = psutil.Process().memory_info().rss / (1024 * 1024)
                    log("WebServer", f"Loading WhisperModel ({model_size}, CPU, int8) - Memory before: {mem_before:.0f}MB")

                    whisper_module = importlib.import_module("faster_whisper")
                    WhisperModel = getattr(whisper_module, "WhisperModel")
                    _whisper_model = WhisperModel(model_size, device="cpu", compute_type="int8")

                    mem_after = psutil.Process().memory_info().rss / (1024 * 1024)
                    log("WebServer", f"WhisperModel ({model_size}) loaded - Memory after: {mem_after:.0f}MB (delta: {mem_after-mem_before:.0f}MB)")

                    # Log to file (consolidated in memory.log)
                    append_domain_log("memory", f"[WHISPER] WhisperModel({model_size}): {mem_before:.0f}MB -> {mem_after:.0f}MB (delta: {mem_after-mem_before:.0f}MB)")

                except ImportError:
                    raise ImportError("faster-whisper not installed. Install with: pip install faster-whisper")
    return _whisper_model

def unload_whisper_model():
    """Unload Whisper model to free memory."""
    global _whisper_model
    with _whisper_model_lock:
        if _whisper_model is not None:
            del _whisper_model
            _whisper_model = None
            import gc
            gc.collect()
            log("WebServer", "WhisperModel unloaded")
log("WebServer", "Getting SessionManager...")
session_mgr = SessionManager()
log("WebServer", "SmartAutoSuggest will be lazy loaded...")
autosuggest = None
tray_context = TrayContext()

# Mount Memory System routes if enabled
if Config.get("memory_enabled", True):
    try:
        from vaf.memory.routes import memory_router
        app.include_router(memory_router, prefix="/api/memory", tags=["memory"])
        log("WebServer", "Memory system routes mounted at /api/memory")
    except ImportError as e:
        log("WebServer", f"Memory system not available: {e}")
    except Exception as e:
        log("WebServer", f"Failed to mount memory routes: {e}")

# Mount Discord Integration routes
try:
    from vaf.api.discord_routes import router as discord_router
    app.include_router(discord_router)
    log("WebServer", "Discord integration routes mounted at /api/discord")
except ImportError as e:
    log("WebServer", f"Discord integration not available: {e}")
except Exception as e:
    log("WebServer", f"Failed to mount Discord routes: {e}")

# Mount Telegram Integration routes
try:
    from vaf.api.telegram_routes import router as telegram_router
    app.include_router(telegram_router)
    log("WebServer", "Telegram integration routes mounted at /api/telegram")
except ImportError as e:
    log("WebServer", f"Telegram integration not available: {e}")
except Exception as e:
    log("WebServer", f"Failed to mount Telegram routes: {e}")

# Mount WhatsApp Integration routes
try:
    from vaf.api.whatsapp_routes import router as whatsapp_router
    app.include_router(whatsapp_router)
    log("WebServer", "WhatsApp integration routes mounted at /api/whatsapp")
except ImportError as e:
    log("WebServer", f"WhatsApp integration not available: {e}")
except Exception as e:
    log("WebServer", f"Failed to mount WhatsApp routes: {e}")

# Mount Contacts routes (central contact list with personal file)
try:
    from vaf.api.contact_routes import router as contact_router
    app.include_router(contact_router)
    log("WebServer", "Contacts routes mounted at /api/contacts")
except ImportError as e:
    log("WebServer", f"Contacts routes not available: {e}")
except Exception as e:
    log("WebServer", f"Failed to mount Contacts routes: {e}")

# Mount Auth routes (Local Network Authentication)
try:
    from vaf.api.auth_routes import router as auth_router
    app.include_router(auth_router)
    log("WebServer", "Auth routes mounted at /api/auth")
except ImportError as e:
    log("WebServer", f"Auth routes not available: {e}")
except Exception as e:
    log("WebServer", f"Failed to mount auth routes: {e}")

# Mount User Management routes (Admin only)
try:
    from vaf.api.user_routes import router as user_router
    app.include_router(user_router)
    log("WebServer", "User management routes mounted at /api/users")
except ImportError as e:
    log("WebServer", f"User routes not available: {e}")
except Exception as e:
    log("WebServer", f"Failed to mount user routes: {e}")

# Mount Network routes (topology, status)
try:
    from vaf.api.network_routes import router as network_router
    app.include_router(network_router)
    log("WebServer", "Network routes mounted at /api/network")
except ImportError as e:
    log("WebServer", f"Network routes not available: {e}")
except Exception as e:
    log("WebServer", f"Failed to mount network routes: {e}")

# Mount User Persona routes
try:
    from vaf.api.user_persona_routes import router as persona_router
    app.include_router(persona_router)
    log("WebServer", "User persona routes mounted at /api/user")
except Exception as e:
    log("WebServer", f"Failed to mount persona routes: {e}")

# Mount Config REST API (for onboarding connections step; config also available via WebSocket)
try:
    from vaf.api.config_routes import router as config_router
    app.include_router(config_router)
    log("WebServer", "Config REST API mounted at /api/config")
except Exception as e:
    log("WebServer", f"Failed to mount config routes: {e}")

# Mount TTS (Text-to-Speech) routes
try:
    from vaf.api.tts_routes import router as tts_router
    app.include_router(tts_router)
    log("WebServer", "TTS routes mounted at /api/tts")
except Exception as e:
    log("WebServer", f"Failed to mount TTS routes: {e}")

# Mount Email connection routes (OAuth2 PKCE + accounts CRUD)
try:
    from vaf.api.email_routes import router as email_router
    app.include_router(email_router)
    log("WebServer", "Email integration routes mounted at /api/email")
except Exception as e:
    log("WebServer", f"Failed to mount Email routes: {e}")

# Mount Cloud Storage routes (OAuth2 PKCE + sync + accounts CRUD)
try:
    from vaf.api.cloud_routes import router as cloud_router
    app.include_router(cloud_router)
    log("WebServer", "Cloud storage routes mounted at /api/cloud")
except Exception as e:
    log("WebServer", f"Failed to mount Cloud routes: {e}")

# Mount GitHub connection routes (OAuth2 + accounts CRUD)
try:
    from vaf.api.github_routes import router as github_router
    app.include_router(github_router)
    log("WebServer", "GitHub integration routes mounted at /api/github")
except Exception as e:
    log("WebServer", f"Failed to mount GitHub routes: {e}")

# Mount Calendar routes (status + events; uses same OAuth as email)
try:
    from vaf.api.calendar_routes import router as calendar_router
    app.include_router(calendar_router)
    log("WebServer", "Calendar routes mounted at /api/calendar")
except Exception as e:
    log("WebServer", f"Failed to mount Calendar routes: {e}")

# Mount Logs routes (admin-only debug log reader)
try:
    from vaf.api.logs_routes import router as logs_router
    app.include_router(logs_router)
    log("WebServer", "Logs routes mounted at /api/logs")
except Exception as e:
    log("WebServer", f"Failed to mount Logs routes: {e}")

# Mount Supervisor/Watchdog routes (live sub-agent units + kill)
try:
    from vaf.api.supervisor_routes import router as supervisor_router
    app.include_router(supervisor_router)
    log("WebServer", "Supervisor routes mounted at /api/supervisor")
except Exception as e:
    log("WebServer", f"Failed to mount Supervisor routes: {e}")

# Mount Agent Brain routes (working memory, plan, intent, team state)
try:
    from vaf.api.brain_routes import router as brain_router
    app.include_router(brain_router)
    log("WebServer", "Agent Brain routes mounted at /api/agent")
except Exception as e:
    log("WebServer", f"Failed to mount Agent Brain routes: {e}")

# Add authentication middleware if local network is enabled
if Config.get("local_network_enabled", False):
    try:
        from vaf.auth.middleware import AuthMiddleware, IPValidationMiddleware
        from vaf.auth.rate_limit import RateLimitMiddleware

        # Add rate limiting first (outermost)
        app.add_middleware(RateLimitMiddleware)
        # Add IP validation
        app.add_middleware(IPValidationMiddleware)
        # Add auth middleware (innermost - closest to route handlers)
        app.add_middleware(AuthMiddleware)

        log("WebServer", "Authentication middleware enabled for local network mode")
    except ImportError as e:
        log("WebServer", f"WARNING: Auth middleware import failed - network mode is INSECURE: {e}")
    except Exception as e:
        log("WebServer", f"WARNING: Failed to add auth middleware - network mode is INSECURE: {e}")

    # Auto-generate SSL certificates if TLS enabled but no certs configured
    if Config.get("local_network_tls_enabled", False):
        try:
            from vaf.network.ssl_utils import ensure_ssl_certificates
            _ssl_cert, _ssl_key = ensure_ssl_certificates()
            if _ssl_cert and _ssl_key:
                log("WebServer", f"SSL certificates ready: {_ssl_cert}")
            else:
                log("WebServer", "TLS enabled but no certificates available")
        except Exception as e:
            log("WebServer", f"SSL certificate setup failed: {e}")

# Security headers middleware (always active, stronger in network mode)
from starlette.middleware.base import BaseHTTPMiddleware as _BaseHM
from starlette.requests import Request as _Req
from starlette.responses import Response as _Resp

class _SecurityHeadersMiddleware(_BaseHM):
    """Add security headers to all responses."""
    async def dispatch(self, request: _Req, call_next):
        response: _Resp = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # HSTS only when TLS is active
        if Config.get("local_network_tls_enabled", False):
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

app.add_middleware(_SecurityHeadersMiddleware)

log("WebServer", "Module initialization complete")

def _mcp_servers_payload(agent) -> list:
    """
    Build the MCP-server list shown in the Settings UI: each configured server from the manifest,
    merged with its live connection status (connected / tool_count / error) from the agent's last
    discovery. Used by `get_mcp_servers` and returned directly in create/update/delete replies so
    the UI list updates without a separate refetch round-trip.
    """
    from vaf.core.mcp_registry import load_mcp_manifest
    servers = (load_mcp_manifest() or {}).get("servers", {}) or {}
    status = dict(getattr(agent, "_mcp_server_status", {}) or {})
    out = []
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        st = status.get(name, {})
        out.append({
            "name": name,
            "command": cfg.get("command", ""),
            "transport": cfg.get("transport", "stdio"),
            "url": cfg.get("url", ""),
            "enabled": bool(cfg.get("enabled", True)),
            "permission_level": cfg.get("permission_level", "write"),
            "env": cfg.get("env") if isinstance(cfg.get("env"), dict) else {},
            "connected": bool(st.get("connected", False)),
            "tool_count": int(st.get("tool_count", 0) or 0),
            "error": st.get("error"),
        })
    return out


def _attach_learned_states(tools_list: list) -> list:
    """Annotate each tool entry with its Whare Wananga learned_state (best-effort).

    learned_state in {"unlearned","learning","learned","stale"} (derived from the
    tool_knowledge store). Surfaced in the Settings tool list. Never raises.
    """
    try:
        from vaf.whare_wananga import learned_states
        from vaf.whare_wananga.preconditions import tool_precondition
        names = [e.get("name", "") for e in tools_list]
        states = learned_states(names)
        for e in tools_list:
            nm = e.get("name", "")
            e["learned_state"] = states.get(nm, "unlearned")
            pc = tool_precondition(nm)
            e["requires_config"] = pc["requires_config"]
            e["configured"] = pc["configured"]
    except Exception:
        pass
    return tools_list


async def _broadcast_tools_update(manager) -> None:
    """
    Push a refreshed tools_list to every connected WebSocket client.
    Called after any custom-tool mutation (create / update / delete / permissions)
    so all open browser tabs see the change without a manual refresh.

    Each client gets a filtered list based on their own scope/role, which means
    we must send individual responses rather than one broadcast payload.
    """
    if not manager:
        return
    try:
        from vaf.core.config import get_local_admin_scope_id
        from vaf.core.custom_tools_registry import (
            get_all_custom_tool_names,
            get_visible_tool_names_for_user,
            get_tool_manifest_entry,
        )

        agent          = manager.agent_instance
        local_admin    = get_local_admin_scope_id()
        all_custom     = set(get_all_custom_tool_names())

        # Iterate over all currently connected websockets
        for ws in list(manager.active_connections):
            try:
                _scope = manager.get_connection_user(ws)
                _role  = manager.get_connection_user_role(ws)
                _is_admin = (
                    _role == "admin"
                    or (_scope is not None and str(_scope) == str(local_admin))
                )
                _filter_scope = None if _is_admin else _scope
                visible_custom = set(get_visible_tool_names_for_user(_filter_scope))

                if agent and hasattr(agent, "tools"):
                    tools_list = []
                    for name, tool in agent.tools.items():
                        is_custom = name in all_custom
                        if is_custom and not _is_admin and name not in visible_custom:
                            continue
                        entry = {
                            "name":        name,
                            "description": getattr(tool, "description", ""),
                            "category":    getattr(tool, "category", "general"),
                            "is_custom":   is_custom,
                            "can_manage":  _is_admin,
                        }
                        if is_custom:
                            meta = get_tool_manifest_entry(name)
                            if meta:
                                entry["shared_with"] = meta.get("shared_with", ["*"])
                                entry["created_by"]  = meta.get("created_by", "")
                                entry["updated_at"]  = meta.get("updated_at", "")
                        tools_list.append(entry)
                    _attach_learned_states(tools_list)
                    await ws.send_json({"type": "tools_list", "tools": tools_list})
            except Exception:
                pass  # Ignore disconnected / errored sockets
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "_broadcast_tools_update failed: %s", exc
        )


def _scan_tool_modules() -> List[dict]:
    """
    Fallback tool list based on available Python modules.
    Tries to find Tool classes without full Agent initialization.
    """
    try:
        import pkgutil
        import importlib
        import inspect
        from vaf.tools.base import BaseTool
        import vaf.tools
        
        tools = []
        package_path = os.path.dirname(vaf.tools.__file__)
        excluded_mods = {"__init__", "base"}
        
        for _, name, _ in pkgutil.iter_modules([package_path]):
            if name in excluded_mods or name.startswith("_"):
                continue
            try:
                module = importlib.import_module(f"vaf.tools.{name}")
                for _, obj in inspect.getmembers(module):
                    if inspect.isclass(obj) and issubclass(obj, BaseTool) and obj is not BaseTool:
                        tools.append({
                            "name": getattr(obj, "name", name),
                            "description": getattr(obj, "description", "Python tool"),
                            "category": getattr(obj, "category", "general")
                        })
            except Exception:
                # Fallback to module name if import fails
                tools.append({
                    "name": name,
                    "description": "Python tool module (load failed)",
                    "category": "general"
                })
        return sorted(tools, key=lambda t: t["name"])
    except Exception:
        return []

def get_autosuggest():
    global autosuggest
    if autosuggest is None:
        log("WebServer", "Lazy loading SmartAutoSuggest...")
        autosuggest = SmartAutoSuggest()
    return autosuggest


def _get_trusted_sources_for_ui():
    """
    Build trusted sources list for Web UI from vaf/sources/*.json and config overrides.
    Order: custom categories first (right after creation box), then predefined from JSON.
    Returns: { "categories": [ { "id", "name", "description", "is_custom", "sources": [...] } ] }
    """
    import json as _json
    try:
        from vaf.core import sources as _sources_mod
        sources_dir = Path(_sources_mod.__file__).resolve().parent.parent / "sources"
    except Exception:
        sources_dir = Path(__file__).resolve().parents[1] / "sources"
    disabled = set(Config.get("trusted_sources_disabled") or [])
    custom = Config.get("trusted_sources_custom") or {}
    custom_only = []
    predefined = []
    seen_category_ids = set()
    # 1) Custom-only categories first (so they appear right after the creation box)
    for cid, cust_list in custom.items():
        custom_only.append({
            "id": cid,
            "name": cid if isinstance(cid, str) else cid.replace("_", " ").title(),
            "description": "Custom category",
            "is_custom": True,
            "sources": [
                {"name": c.get("name", ""), "url": c.get("url", ""), "domains": c.get("domains") or [], "trust_score": c.get("trust_score", 6), "is_custom": True}
                for c in (cust_list or [])
            ],
        })
        seen_category_ids.add(cid)
    # 2) Predefined categories from JSON
    if sources_dir.exists():
        for jpath in sources_dir.glob("*.json"):
            try:
                with open(jpath, "r", encoding="utf-8") as f:
                    data = _json.load(f)
                for cid, cdata in (data.get("categories") or {}).items():
                    if cid in seen_category_ids:
                        continue
                    seen_category_ids.add(cid)
                    sources_list = []
                    for s in (cdata.get("sources") or []):
                        doms = s.get("domains") or []
                        if any(d.lower() in disabled for d in doms):
                            continue
                        sources_list.append({
                            "name": s.get("name", ""),
                            "url": s.get("url", ""),
                            "domains": doms,
                            "trust_score": s.get("trust_score", 5),
                            "is_custom": False,
                        })
                    for cust in (custom.get(cid) or []):
                        doms = cust.get("domains") or []
                        sources_list.append({
                            "name": cust.get("name", ""),
                            "url": cust.get("url", ""),
                            "domains": doms,
                            "trust_score": cust.get("trust_score", 6),
                            "is_custom": True,
                        })
                    predefined.append({
                        "id": cid,
                        "name": cdata.get("name", cid),
                        "description": cdata.get("description", ""),
                        "is_custom": False,
                        "sources": sources_list,
                    })
            except Exception:
                continue
    categories_out = custom_only + predefined
    return {"categories": categories_out}

@app.on_event("startup")
async def startup_event():
    # Initialize auth database tables (creates if not exist)
    try:
        from vaf.auth.database import init_auth_db
        await init_auth_db()
        log("WebServer", "Auth database tables initialized")
    except Exception as e:
        log("WebServer", f"Auth database init warning: {e}")
    
    # Set the event loop for thread-safe broadcasting
    loop = asyncio.get_running_loop()
    manager.set_server_loop(loop)
    log("WebServer", "VAF Web Interface: Event loop registered")

    # Whare Wananga EAGER: opt-in background scanner that proactively trains safe, configured,
    # not-yet-learned tools (off by default; tolerates a not-yet-built agent). Guarded.
    try:
        from vaf.whare_wananga import eager
        eager.start(lambda: getattr(manager, "agent_instance", None))
        log("WebServer", "Whare Wananga eager scanner started (opt-in)")
    except Exception as e:
        log("WebServer", f"Whare Wananga eager scanner not started: {e}")
    
    # Register TTS Callbacks for UI sync
    from vaf.core.speech import SpeechManager
    sm = SpeechManager.get_instance()
    
    def on_tts_loading():
        if manager._server_loop:
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({"type": "tts_state", "status": "loading"}),
                manager._server_loop
            )

    def on_tts_start(text):
        if manager._server_loop:
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({"type": "tts_state", "status": "playing", "text": text}),
                manager._server_loop
            )
            
    def on_tts_end():
        if manager._server_loop:
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({"type": "tts_state", "status": "stopped"}),
                manager._server_loop
            )
            
    sm.on_speech_loading = on_tts_loading
    sm.on_speech_start = on_tts_start
    sm.on_speech_end = on_tts_end
    
    # Setup firewall rules if local network and firewall are enabled
    if Config.get("local_network_enabled", False) and Config.get("local_network_firewall_enabled", True):
        try:
            from vaf.network.firewall import setup_firewall, register_cleanup_on_exit
            import platform as _platform
            tls_on = bool(Config.get("local_network_tls_enabled", False))
            if tls_on:
                access_port = int(Config.get("local_network_https_port", 443) or 443)
                # Keep behavior aligned with tray/proxy: Windows defaults to 8443 when configured 443.
                if _platform.system() == "Windows" and access_port == 443:
                    access_port = 8443
                port = access_port
                # Keep backend port rule too for compatibility with current internal routing.
                port_frontend = int(Config.get("local_network_port", 8001) or 8001)
            else:
                port = int(Config.get("local_network_port", 8001) or 8001)
                port_frontend = int(Config.get("local_network_port_frontend", 3000) or 3000)
            
            success = setup_firewall(port, port_frontend)
            if success:
                register_cleanup_on_exit()
                log("WebServer", f"Firewall rules created for ports {port}, {port_frontend}")
            else:
                log("WebServer", f"Firewall setup failed for ports {port}, {port_frontend} - may need elevated privileges (Run as Administrator)")
        except Exception as e:
            log("WebServer", f"Firewall setup error: {e}")

    # Start garbage collector (logs, temp, cache, old thinking sessions) when running without tray
    try:
        from vaf.core.garbage_collector import GarbageCollector
        GarbageCollector.get_instance().start()
        log("WebServer", "Garbage collector started")
    except Exception as e:
        log("WebServer", f"Garbage collector start warning: {e}")
    
    # Email auto-sync: run every 30 min for accounts with "Auto sync every 30 min" enabled
    async def _email_auto_sync_loop():
        from vaf.api.email_routes import run_auto_sync_all_accounts, EMAIL_AUTO_SYNC_INTERVAL_SEC
        await asyncio.sleep(60)  # Delay first run so server is fully up
        while True:
            try:
                result = await run_auto_sync_all_accounts(max_messages=100)
                if result["synced"] or result["failed"]:
                    log("WebServer", f"Email auto-sync: {result['synced']} ok, {result['failed']} failed")
                if result["errors"]:
                    for err in result["errors"][:3]:
                        log("WebServer", f"Email auto-sync error: {err}")
            except Exception as e:
                log("WebServer", f"Email auto-sync loop error: {e}")
            await asyncio.sleep(EMAIL_AUTO_SYNC_INTERVAL_SEC)

    asyncio.create_task(_email_auto_sync_loop())

    # Cloud storage background sync
    if Config.get("cloud_sync_enabled", False):
        try:
            from vaf.cloud.sync_worker import cloud_sync_loop
            asyncio.create_task(cloud_sync_loop())
            log("WebServer", "Cloud storage background sync started")
        except Exception as e:
            log("WebServer", f"Cloud sync loop start error: {e}")

    # Thinking mode: background reflection when user idle
    if Config.get("thinking_enabled", True):
        try:
            from vaf.core.thinking_mode import start_thinking_mode_background
            start_thinking_mode_background()
            log("WebServer", "Thinking mode background loop started")
        except Exception as e:
            log("WebServer", f"Thinking mode start error: {e}")
    log("WebServer", "Email auto-sync background task started (every 30 min)")

    # Start the process-wide automation scheduler for existing timed automations.
    try:
        from vaf.core.automation import ensure_scheduler_started

        _scheduler_mgr, started_now = ensure_scheduler_started(origin="web_server_startup")
        if started_now:
            log("WebServer", "Automation scheduler started")
        else:
            log("WebServer", "Automation scheduler already running")
    except Exception as e:
        log("WebServer", f"Automation scheduler start error: {e}")

    # In Docker mode, the tray app doesn't run, so we must start the headless runner here.
    if Config.is_docker_mode():
        log("WebServer", "Docker mode detected - starting headless agent runner...")
        try:
            _ensure_headless_agent_runner(origin="docker_startup")
        except Exception as e:
            log("WebServer", f"Failed to start headless agent: {e}")
            import traceback
            log("WebServer", traceback.format_exc())

    # Start Auto-Capture Queue Worker - DISABLED: causes 13GB+ memory spikes
    # The memory leak occurs during pipeline.ingest() ~13 seconds after embedding
    # TODO: Investigate asyncpg/SQLAlchemy memory leak in auto_capture_memory()
    # if Config.get("memory_enabled", True) and Config.get("memory_auto_capture", True):
    #     asyncio.create_task(_auto_capture_worker())
    #     log("WebServer", "Auto-capture queue worker started")
    log("WebServer", "Auto-capture worker DISABLED (memory leak investigation)")

    # Auto-start Telegram bridge when configured and enabled (so Web UI shows "Connected" after restart)
    try:
        telegram_config = Config.get("telegram_config") or {}
        if isinstance(telegram_config, dict) and telegram_config.get("verified") and telegram_config.get("bot_token") and telegram_config.get("enabled"):
            from vaf.api.telegram_bridge import start_bridge, is_bridge_running
            if not is_bridge_running() and start_bridge():
                log("WebServer", "Telegram bridge auto-started (configured and enabled)")
            elif is_bridge_running():
                log("WebServer", "Telegram bridge already running")
    except Exception as e:
        log("WebServer", f"Telegram bridge auto-start skipped or failed: {e}")

    # Auto-start Discord bridge when configured and enabled
    try:
        discord_config = Config.get("discord_config") or {}
        if isinstance(discord_config, dict) and discord_config.get("verified") and discord_config.get("bot_token") and discord_config.get("admin_user_id") and discord_config.get("enabled"):
            from vaf.api.discord_bridge import start_bridge, is_bridge_running
            if not is_bridge_running() and start_bridge():
                log("WebServer", "Discord bridge auto-started (configured and enabled)")
            elif is_bridge_running():
                log("WebServer", "Discord bridge already running")
    except Exception as e:
        log("WebServer", f"Discord bridge auto-start skipped or failed: {e}")

    # Auto-start WhatsApp bridge when configured and enabled
    try:
        whatsapp_config = Config.get("whatsapp_config") or {}
        if isinstance(whatsapp_config, dict) and whatsapp_config.get("enabled"):
            whitelist = whatsapp_config.get("whitelist") or []
            if any(isinstance(e, dict) and e.get("phone_number") for e in whitelist):
                from vaf.api.whatsapp_bridge import start_bridge, is_bridge_running
                if not is_bridge_running() and start_bridge():
                    log("WebServer", "WhatsApp bridge auto-started (configured and enabled)")
                elif is_bridge_running():
                    log("WebServer", "WhatsApp bridge already running")
                asyncio.create_task(_whatsapp_reconnect_worker())
                log("WebServer", "WhatsApp auto-reconnect worker started")
    except Exception as e:
        log("WebServer", f"WhatsApp bridge auto-start skipped or failed: {e}")


async def _whatsapp_reconnect_worker():
    """
    Periodically check WhatsApp connection; if bridge is running but disconnected,
    restart the bridge so it reconnects with stored credentials (no user action needed).
    """
    from vaf.api.whatsapp_bridge import is_bridge_running, get_connection_status, restart_bridge
    loop = asyncio.get_running_loop()
    last_restart_at = 0.0
    check_interval = 120.0   # check every 2 minutes
    disconnected_since = None  # time when we first saw disconnected
    disconnect_grace = 90.0   # restart only if disconnected for this long (seconds)
    cooldown = 180.0          # after a restart, wait this long before next check

    while True:
        await asyncio.sleep(check_interval)
        try:
            whatsapp_config = Config.get("whatsapp_config") or {}
            if not isinstance(whatsapp_config, dict) or not whatsapp_config.get("enabled"):
                disconnected_since = None
                continue
            whitelist = whatsapp_config.get("whitelist") or []
            if not any(isinstance(e, dict) and e.get("phone_number") for e in whitelist):
                disconnected_since = None
                continue
            if not is_bridge_running():
                disconnected_since = None
                continue
            # Cooldown after a restart
            now = loop.time()
            if now - last_restart_at < cooldown:
                continue
            # Sync call in executor to avoid blocking
            connected = await loop.run_in_executor(
                None, lambda: get_connection_status("admin", wait_timeout=2.0)
            )
            if connected:
                disconnected_since = None
                continue
            if disconnected_since is None:
                disconnected_since = now
            if now - disconnected_since < disconnect_grace:
                continue
            # Restart bridge to reconnect
            log("WebServer", "WhatsApp disconnected; auto-restarting bridge to reconnect")
            await loop.run_in_executor(None, restart_bridge)
            last_restart_at = loop.time()
            disconnected_since = None
        except Exception as e:
            log("WebServer", f"WhatsApp reconnect worker error: {e}")
            disconnected_since = None


async def _auto_capture_worker():
    """
    Background worker that processes auto-capture queue in the main event loop.

    This is MEMORY-LEAK SAFE because:
    - Runs in main event loop (no daemon threads with asyncio.run())
    - ONNX model and DB connections are reused from main thread
    - Processes max 2 tasks per cycle to avoid blocking
    """
    from vaf.memory.rag import process_auto_capture_queue, get_auto_capture_queue_size

    while True:
        try:
            # Process pending captures (max 2 per cycle)
            processed = await process_auto_capture_queue(max_tasks=2)
            if processed > 0:
                log("WebServer", f"Auto-capture: processed {processed} tasks")
        except Exception as e:
            log("WebServer", f"Auto-capture worker error: {e}")

        # Check queue every 5 seconds (balance between responsiveness and CPU)
        await asyncio.sleep(5)

def _detect_language_simple(text: str) -> str:
    """Detect language for TTS: use langid when available, else simple heuristic (de/en)."""
    if not (text and text.strip()):
        return "en"
    # Prefer langid for reliable detection (e.g. German text -> de)
    try:
        from vaf.vendor import langid
        import re
        # Strip thinking/code so we classify on actual spoken content
        clean = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        if not clean:
            clean = text.strip()
        # Use first ~2k chars to avoid slow classify on huge text
        sample = clean[:2000] if len(clean) > 2000 else clean
        if sample:
            code, _ = langid.classify(sample)
            if code and len(code) >= 2:
                return code[:2].lower()
    except ImportError:
        pass
    except Exception:
        pass
    # Fallback: simple heuristic (de/en)
    t = text.lower()
    de_words = [" das ", " und ", " der ", " die ", " ist ", " nicht ", " ich ", " sie ", " es ", " wie ", " was ", " eine ", " ein ", " mit ", " von ", " für ", " auf ", " sind ", " kann ", " auch ", " dann ", " haben ", " wird "]
    if any(w in t for w in de_words):
        return "de"
    if any(ch in t for ch in ["ä", "ö", "ü", "ß"]):
        return "de"
    # "das " / "der " / "die " at start
    if t.startswith(("das ", "der ", "die ", "den ", "dem ", "ein ", "eine ", "und ", "ist ")):
        return "de"
    return "en"

def _build_artifact_payload(session, session_id: str = None):
    if not session:
        return None
    try:
        state = session.get_provider_state("artifact")
    except Exception:
        state = None
    if not state:
        return None
    payload = {
        "type": "artifact_update",
        "file": state.get("file", ""),
        "code": state.get("code", ""),
        "updatedAt": state.get("updatedAt"),
        "source": state.get("source", "backend")
    }
    if session_id:
        payload["sessionId"] = session_id
    return payload

@app.get("/")
async def root():
    return {"status": "VAF Backend Online", "version": "1.0.0"}

from pydantic import BaseModel

class WorkflowUpdate(BaseModel):
    type: str
    sessionId: Optional[str] = None
    workflowId: Optional[str] = None
    name: Optional[str] = None
    steps: Optional[List] = None
    stepId: Optional[str] = None
    status: Optional[str] = None
    progress: Optional[int] = None
    result: Optional[str] = None
    # document_ready payload (from notify_document_created)
    filePath: Optional[str] = None
    title: Optional[str] = None

class Heartbeat(BaseModel):
    client_id: str
    timestamp: float = 0.0

@app.post("/api/heartbeat")
async def receive_heartbeat(hb: Heartbeat):
    """Receive heartbeat from CLI clients to keep server active."""
    tray_context.register_activity()
    return {"status": "ok", "active": True}

@app.get("/api/heartbeat")
async def healthcheck():
    """Health check endpoint for Docker/monitoring."""
    return {"status": "ok", "healthy": True}

@app.post("/api/workflow/update")
async def receive_workflow_update(update: WorkflowUpdate):
    """Receive workflow updates from external processes (like separate terminals)."""
    data = update.dict(exclude_none=True)
    try:
        if update.sessionId:
            await manager.broadcast_to_session(update.sessionId, data)
        else:
            await manager.broadcast(data)
    except Exception as e:
        append_domain_log("webui", f"[ERROR] broadcast failed in /api/workflow/update: {e}")
    # When a file is created, store its project directory so the agent can edit it later
    if data.get("type") == "file_created" and data.get("filePath") and data.get("sessionId"):
        try:
            from pathlib import Path as _Path
            project_dir = str(_Path(data["filePath"]).parent.resolve())
            loaded = session_mgr.load(data["sessionId"])
            if not getattr(loaded, "runtime_state", None):
                loaded.runtime_state = {}
            loaded.runtime_state["last_project_path"] = project_dir
            # Anchor session workspace on first "real" project creation (VAF_Projects paths only).
            # session.project_path is stable — set once, never overwritten — giving the chat
            # a persistent workspace root independent of which sub-project was last touched.
            if not getattr(loaded, "project_path", ""):
                try:
                    from vaf.core.platform import Platform as _Plat
                    _vaf_root = str(_Plat.documents_dir())
                    if "VAF_Projects" in project_dir and project_dir.startswith(_vaf_root):
                        loaded.project_path = project_dir
                except Exception:
                    pass
            session_mgr.save(loaded, sync_state=False)
        except Exception:
            pass
    return {"status": "ok"}


class SubAgentStreamUpdate(BaseModel):
    """Model for subagent output stream updates from separate processes."""
    type: str
    sessionId: Optional[str] = None
    taskId: Optional[str] = None
    agentType: Optional[str] = None
    agentName: Optional[str] = None
    line: Optional[str] = None
    status: Optional[str] = None
    presence: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    file: Optional[str] = None
    code: Optional[str] = None
    steps: Optional[List] = None
    # Workflow-specific fields
    workflowId: Optional[str] = None
    stepId: Optional[str] = None
    progress: Optional[int] = None
    name: Optional[str] = None

    model_config = {"extra": "allow"}


@app.get("/api/whare_wananga/tool_knowledge/{name}")
async def whare_wananga_tool_knowledge(name: str):
    """Return the stored tool_knowledge record + learned state for a tool (or null)."""
    try:
        from vaf.whare_wananga import load as _ww_load, learned_state as _ww_state
        return {"ok": True, "tool": name, "state": _ww_state(name), "record": _ww_load(name)}
    except Exception as e:
        return {"ok": False, "tool": name, "state": "unlearned", "record": None, "error": str(e)}


@app.post("/api/whare_wananga/train/{name}")
async def whare_wananga_train(name: str):
    """Start a Whare Wananga predict-then-verify training pass for a tool (background job)."""
    try:
        from vaf.core.log_helper import append_domain_log
        append_domain_log("backend", f"[WHARE-WANANGA] train requested: {name}")
    except Exception:
        pass
    agent = manager.agent_instance if manager else None
    if agent is None or not hasattr(agent, "tools"):
        return {"ok": False, "tool": name, "state": "error", "message": "Agent not available"}
    if name not in getattr(agent, "tools", {}):
        return {"ok": False, "tool": name, "state": "error", "message": "Unknown tool"}
    # Precondition: never train a tool whose connection is not configured.
    try:
        from vaf.whare_wananga.preconditions import tool_precondition
        pc = tool_precondition(name)
        if pc.get("requires_config") and not pc.get("configured"):
            return {"ok": False, "tool": name, "state": "skipped", "message": "Connection not configured"}
    except Exception:
        pass
    try:
        from vaf.whare_wananga import jobs
        st = jobs.start_training(agent, name)
        print(f"[WHARE-WANANGA] training started: {name} (validate/refine loop)")
        return {"ok": True, "tool": name, **st}
    except Exception as e:
        return {"ok": False, "tool": name, "state": "error", "message": str(e)}


@app.get("/api/whare_wananga/training_status/{name}")
async def whare_wananga_training_status(name: str):
    """Live status of a Whare Wananga training job (polled by the dashboard)."""
    try:
        from vaf.whare_wananga import jobs
        return {"ok": True, "tool": name, "status": jobs.get_status(name)}
    except Exception as e:
        return {"ok": False, "tool": name, "status": None, "error": str(e)}


@app.post("/api/subagent/stream")
async def receive_subagent_stream(update: SubAgentStreamUpdate):
    """
    Receive subagent output stream updates from external processes.
    This endpoint bridges subprocess output to WebSocket clients.
    """
    data = update.dict(exclude_none=True)
    # We're in an async handler, so we can directly await without checking the loop
    try:
        if update.sessionId:
            await manager.broadcast_to_session(update.sessionId, data)
        else:
            await manager.broadcast(data)
    except Exception as e:
        append_domain_log("webui", f"[ERROR] broadcast failed in /api/subagent/stream: {e}")
    return {"status": "ok"}

@app.get("/api/tools/{name}/source")
async def get_tool_source(name: str):
    """Get the source code of a tool."""
    print(f"[DEBUG] Fetching source for tool: {name}")
    try:
        manager = get_web_interface()
        if not manager.agent_instance:
            print("[DEBUG] Agent instance not found in manager")
            return {"error": "Agent not initialized"}
            
        if not hasattr(manager.agent_instance, 'tools'):
            print("[DEBUG] Agent has no tools attribute")
            return {"error": "Agent has no tools"}
            
        tool = manager.agent_instance.tools.get(name)
        if tool:
             try:
                 # Try to get source of the class
                 src = inspect.getsource(tool.__class__)
                 print(f"[DEBUG] Found source, length: {len(src)}")
                 return {"name": name, "code": src}
             except Exception as e:
                 print(f"[DEBUG] inspect.getsource failed: {e}")
                 # Fallback: Try to read from __file__ if available
                 try:
                     import os
                     file_path = inspect.getfile(tool.__class__)
                     if os.path.exists(file_path):
                         with open(file_path, 'r', encoding='utf-8') as f:
                             src = f.read()
                             return {"name": name, "code": src}
                 except Exception as ex:
                     print(f"[DEBUG] File read fallback failed: {ex}")
                 
                 return {"error": f"Failed to read source: {e}"}
        else:
             print(f"[DEBUG] Tool '{name}' not found in agent.tools")
             print(f"[DEBUG] Available tools: {list(manager.agent_instance.tools.keys())}")
             
    except Exception as e:
        print(f"[DEBUG] General error in get_tool_source: {e}")
        return {"error": str(e)}
    return {"error": "Tool not found"}

# Sounds directory for Web UI completion/notification (vaf/media/sounds)
SOUNDS_DIR = Path(__file__).resolve().parents[1] / "media" / "sounds"
ALLOWED_SOUND_FILES = {"tts01.mp3", "sst.mp3"}  # Only serve known files

@app.get("/api/file")
async def get_file(path: str = Query(..., description="Absolute path to local file")):
    """Serve a local file by path (allowed roots: documents, downloads, data dir). Used by Web UI."""
    from vaf.core.platform import Platform
    import mimetypes
    try:
        target = Platform.normalize_path(path)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    allowed_roots = [
        Platform.documents_dir().resolve(),
        Platform.downloads_dir().resolve(),
        Platform.data_dir().resolve(),
        Platform.get_vaf_output_dir().resolve(),
    ]
    if not any(target.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="Access denied")
    mime_type, _ = mimetypes.guess_type(str(target))
    return FileResponse(
        path=str(target),
        media_type=mime_type or "application/octet-stream",
        filename=target.name,
    )


def _allowed_file_path(path_str: str):
    """Resolve path and check it is under allowed roots. Returns Path or raises HTTPException."""
    from vaf.core.platform import Platform
    try:
        target = Platform.normalize_path(path_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    allowed_roots = [
        Platform.documents_dir().resolve(),
        Platform.downloads_dir().resolve(),
        Platform.data_dir().resolve(),
        Platform.get_vaf_output_dir().resolve(),
    ]
    if not any(target.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="Access denied")
    return target


def _escape_html(s: str) -> str:
    import html
    return html.escape(s, quote=True)


def _office_to_pdf_via_gotenberg(file_path: str, filename: str) -> bytes | None:
    """
    Convert Office docs (DOCX, XLSX, PPTX, ODT, ODS, ODP) to PDF via Gotenberg Docker service.
    Returns PDF bytes or None if unavailable. Gotenberg uses LibreOffice under the hood; MIT license.
    """
    url = (Config.get("document_conversion_docker_url") or "").strip().rstrip("/")
    if not url:
        return None
    try:
        import requests
        api_url = f"{url}/forms/libreoffice/convert"
        with open(file_path, "rb") as f:
            files = {"files": (filename, f, "application/octet-stream")}
            r = requests.post(api_url, files=files, timeout=60)
        if r.status_code == 200 and r.headers.get("content-type", "").lower().startswith("application/pdf"):
            return r.content
    except Exception as e:
        log("WebServer", f"Gotenberg conversion failed for {filename}: {e}")
    return None


def _docx_to_pdf_via_libreoffice(docx_path: str) -> bytes | None:
    """
    Convert DOCX to PDF using LibreOffice headless. Returns PDF bytes or None if unavailable.
    LibreOffice is MPL 2.0 - compatible with MIT projects. Use in Docker: install libreoffice in the image.
    """
    import subprocess
    import shutil
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None
    out_dir = os.path.dirname(docx_path)
    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", out_dir, docx_path],
            capture_output=True,
            timeout=60,
            check=False,
        )
        pdf_path = os.path.splitext(docx_path)[0] + ".pdf"
        if os.path.isfile(pdf_path):
            with open(pdf_path, "rb") as f:
                data = f.read()
            try:
                os.unlink(pdf_path)
            except Exception:
                pass
            return data
    except (subprocess.TimeoutExpired, OSError, Exception):
        pass
    return None


def _docx_to_html(target) -> str:
    from docx import Document
    doc = Document(str(target))
    parts = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style_name = (para.style.name or "").lower()
        if "heading 1" in style_name or para.style.name == "Title":
            parts.append(f"<h1>{_escape_html(text)}</h1>")
        elif "heading 2" in style_name:
            parts.append(f"<h2>{_escape_html(text)}</h2>")
        elif "heading 3" in style_name:
            parts.append(f"<h3>{_escape_html(text)}</h3>")
        else:
            parts.append(f"<p>{_escape_html(text)}</p>")
    for table in doc.tables:
        parts.append("<table border=\"1\" cellpadding=\"4\" cellspacing=\"0\">")
        for row in table.rows:
            parts.append("<tr>")
            for cell in row.cells:
                parts.append(f"<td>{_escape_html(cell.text.strip())}</td>")
            parts.append("</tr>")
        parts.append("</table>")
    return "".join(parts) if parts else "<p></p>"


def _xlsx_to_html(target) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(target, read_only=True, data_only=True)
    parts = []
    for sheet_name in wb.sheetnames[:10]:
        sheet = wb[sheet_name]
        parts.append(f"<h2>Sheet: {_escape_html(sheet_name)}</h2>")
        parts.append("<table border=\"1\" cellpadding=\"4\" cellspacing=\"0\">")
        max_row = min(sheet.max_row, 500)
        max_col = min(sheet.max_column, 30)
        for row in sheet.iter_rows(min_row=1, max_row=max_row, max_col=max_col, values_only=True):
            parts.append("<tr>")
            for cell in row:
                val = "" if cell is None else str(cell)
                parts.append(f"<td>{_escape_html(val)}</td>")
            parts.append("</tr>")
        parts.append("</table>")
    wb.close()
    return "".join(parts) if parts else "<p></p>"


def _pptx_to_html(target) -> str:
    from pptx import Presentation
    prs = Presentation(str(target))
    parts = []
    for i, slide in enumerate(prs.slides[:50], 1):
        parts.append(f"<h2>Slide {i}</h2>")
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                parts.append(f"<p>{_escape_html(shape.text.strip())}</p>")
    return "".join(parts) if parts else "<p></p>"


@app.get("/api/file/as-html")
async def get_file_as_html(path: str = Query(..., description="Path to .docx, .xlsx or .pptx to convert to HTML for editing")):
    """Convert Word (.docx), Excel (.xlsx) or PowerPoint (.pptx) to HTML so the Document Editor can display and edit it."""
    target = _allowed_file_path(path)
    suf = target.suffix.lower()
    if suf not in (".docx", ".xlsx", ".pptx"):
        raise HTTPException(status_code=400, detail="Only .docx, .xlsx and .pptx can be converted to HTML here")
    try:
        if suf == ".docx":
            body = _docx_to_html(target)
        elif suf == ".xlsx":
            body = _xlsx_to_html(target)
        else:
            body = _pptx_to_html(target)
        html = "<!DOCTYPE html><html><head><meta charset=\"utf-8\"/></head><body>" + body + "</body></html>"
        from fastapi.responses import HTMLResponse
        return HTMLResponse(html)
    except ImportError as e:
        raise HTTPException(status_code=503, detail=f"Support not installed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to convert to HTML: {e}")


@app.get("/api/file/docx-model")
async def get_file_as_docx_model(path: str = Query(..., description="Path to .docx to convert to VAF's native DOCX model")):
    """Convert a DOCX file into the native editor model."""
    target = _allowed_file_path(path)
    if target.suffix.lower() != ".docx":
        raise HTTPException(status_code=400, detail="Only .docx files are supported")
    try:
        from vaf.core.docx_import import import_docx_to_native_model

        model = import_docx_to_native_model(target)
        return model.to_dict()
    except ImportError as e:
        raise HTTPException(status_code=503, detail=f"DOCX support not installed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to convert docx to native model: {e}")


class FileSaveRequest(BaseModel):
    """Request body for saving a file."""
    path: str
    content: str


class FileSaveDocxRequest(BaseModel):
    """Request body for saving HTML content back as .docx."""
    path: str
    content: str  # HTML from the editor


class FileSaveDocxNativeRequest(BaseModel):
    """Request body for saving native DOCX model content back as .docx."""
    path: str
    document: dict


def _strip_html_to_text(html_fragment: str) -> str:
    import re
    import html
    t = re.sub(r"<[^>]+>", " ", html_fragment)
    t = html.unescape(t)
    return " ".join(t.split()).strip()


@app.post("/api/file/save-docx")
async def save_file_as_docx(request: FileSaveDocxRequest):
    """Save editor content (HTML) back to a Word (.docx) file. Creates/overwrites the file."""
    from vaf.core.platform import Platform
    import re
    try:
        target = Platform.normalize_path(request.path)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    allowed_roots = [
        Platform.documents_dir().resolve(),
        Platform.downloads_dir().resolve(),
        Platform.data_dir().resolve(),
        Platform.get_vaf_output_dir().resolve(),
    ]
    if not any(target.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="Access denied")
    if target.suffix.lower() != ".docx":
        raise HTTPException(status_code=400, detail="Path must be a .docx file")
    try:
        from docx import Document
        doc = Document()
        html = request.content or ""
        body_match = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL | re.IGNORECASE)
        if body_match:
            html = body_match.group(1)
        # Find all block elements in order: h1, h2, h3, p, table
        pattern = r"<(h[1-3]|p|table)[^>]*>(.*?)</\1>"
        for m in re.finditer(pattern, html, re.DOTALL | re.IGNORECASE):
            tag, inner = m.group(1).lower(), m.group(2)
            text = _strip_html_to_text(inner)
            if tag.startswith("h") and len(tag) == 2:
                level = int(tag[1])
                if text:
                    doc.add_heading(text, level=level)
            elif tag == "p" and text:
                doc.add_paragraph(text)
            elif tag == "table":
                rows_html = re.findall(r"<tr[^>]*>(.*?)</tr>", inner, re.DOTALL | re.IGNORECASE)
                if rows_html:
                    all_cells = [re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.DOTALL | re.IGNORECASE) for r in rows_html]
                    nrows = len(all_cells)
                    ncols = max(len(c) for c in all_cells) if all_cells else 0
                    if ncols > 0:
                        table = doc.add_table(rows=nrows, cols=ncols)
                        for ri, cells in enumerate(all_cells):
                            for ci, cell_html in enumerate(cells):
                                if ci < ncols:
                                    table.rows[ri].cells[ci].text = _strip_html_to_text(cell_html)
        # If no structured blocks found, add whole body as one paragraph
        if len(doc.paragraphs) == 0 and not doc.tables:
            text = _strip_html_to_text(html)
            if text:
                doc.add_paragraph(text)
        doc.save(str(target))
        return {"status": "ok", "path": str(target)}
    except ImportError:
        raise HTTPException(status_code=503, detail="Word support not installed. Run: pip install python-docx")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save docx: {e}")


@app.post("/api/file/save-docx-native")
async def save_file_as_docx_native(request: FileSaveDocxNativeRequest):
    """Save VAF's native DOCX editor model back as a .docx file."""
    target = _allowed_save_path(request.path, ".docx")
    try:
        from vaf.core.docx_export import export_native_docx
        from vaf.core.docx_native_model import NativeDocxDocument

        document = NativeDocxDocument.from_dict(request.document or {})
        saved_path = export_native_docx(document, target)
        return {"status": "ok", "path": str(saved_path)}
    except ImportError as e:
        raise HTTPException(status_code=503, detail=f"DOCX support not installed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save native docx: {e}")


class FileSaveOfficeRequest(BaseModel):
    """Request body for saving HTML content back as .xlsx or .pptx."""
    path: str
    content: str  # HTML from the editor


def _allowed_save_path(path_str: str, required_suffix: str):
    from vaf.core.platform import Platform
    try:
        target = Platform.normalize_path(path_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    allowed_roots = [
        Platform.documents_dir().resolve(),
        Platform.downloads_dir().resolve(),
        Platform.data_dir().resolve(),
        Platform.get_vaf_output_dir().resolve(),
    ]
    if not any(target.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="Access denied")
    if target.suffix.lower() != required_suffix:
        raise HTTPException(status_code=400, detail=f"Path must be a {required_suffix} file")
    return target


@app.post("/api/file/save-xlsx")
async def save_file_as_xlsx(request: FileSaveOfficeRequest):
    """Save editor content (HTML tables) back to an Excel (.xlsx) file. First table = first sheet."""
    import re
    target = _allowed_save_path(request.path, ".xlsx")
    html = request.content or ""
    body_match = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL | re.IGNORECASE)
    if body_match:
        html = body_match.group(1)
    try:
        import openpyxl
        from openpyxl import Workbook
        wb = Workbook()
        wb.remove(wb.active)
        tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.DOTALL | re.IGNORECASE)
        for ti, table_html in enumerate(tables[:20]):
            rows_html = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.DOTALL | re.IGNORECASE)
            if not rows_html:
                continue
            all_cells = [re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.DOTALL | re.IGNORECASE) for r in rows_html]
            ncols = max(len(c) for c in all_cells) if all_cells else 0
            if ncols == 0:
                continue
            sheet_name = f"Sheet{ti + 1}" if ti > 0 else "Sheet1"
            ws = wb.create_sheet(sheet_name, ti)
            for ri, cells in enumerate(all_cells):
                for ci, cell_html in enumerate(cells):
                    if ci < ncols:
                        val = _strip_html_to_text(cell_html)
                        ws.cell(row=ri + 1, column=ci + 1, value=val)
        if not wb.sheetnames:
            ws = wb.create_sheet("Sheet1")
            ws.cell(row=1, column=1, value="")
        wb.save(str(target))
        return {"status": "ok", "path": str(target)}
    except ImportError:
        raise HTTPException(status_code=503, detail="Excel support not installed. Run: pip install openpyxl")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save xlsx: {e}")


@app.post("/api/file/save-pptx")
async def save_file_as_pptx(request: FileSaveOfficeRequest):
    """Save editor content (HTML: h2 = slide title, p = body) back to a PowerPoint (.pptx) file."""
    import re
    target = _allowed_save_path(request.path, ".pptx")
    html = request.content or ""
    body_match = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL | re.IGNORECASE)
    if body_match:
        html = body_match.group(1)
    try:
        from pptx import Presentation
        from pptx.util import Inches, Pt
        prs = Presentation()
        prs.slide_width = Inches(10)
        prs.slide_height = Inches(7.5)
        slide_sections = re.split(r"<h2[^>]*>", html, flags=re.IGNORECASE)
        for i, section in enumerate(slide_sections):
            if i == 0 and not re.search(r"</h2>", section, re.IGNORECASE):
                continue
            parts = re.split(r"</h2>", section, maxsplit=1, flags=re.IGNORECASE)
            title_html = parts[0] if len(parts) > 1 else ""
            body_html = parts[1] if len(parts) > 1 else section
            title = _strip_html_to_text(title_html) or f"Slide {i}"
            body_text = _strip_html_to_text(body_html)
            if not title and not body_text:
                continue
            blank = prs.slide_layouts[6]
            slide = prs.slides.add_slide(blank)
            left = Inches(0.5)
            top = Inches(0.5)
            width = Inches(9)
            height = Inches(0.8)
            tb = slide.shapes.add_textbox(left, top, width, height)
            tf = tb.text_frame
            p = tf.paragraphs[0]
            p.text = title
            p.font.size = Pt(24)
            if body_text:
                tb2 = slide.shapes.add_textbox(left, Inches(1.5), width, Inches(5.5))
                tf2 = tb2.text_frame
                tf2.text = body_text
        if len(prs.slides) == 0:
            blank = prs.slide_layouts[6]
            slide = prs.slides.add_slide(blank)
            slide.shapes.add_textbox(Inches(0.5), Inches(0.5), Inches(9), Inches(1)).text_frame.text = "Untitled"
        prs.save(str(target))
        return {"status": "ok", "path": str(target)}
    except ImportError:
        raise HTTPException(status_code=503, detail="PowerPoint support not installed. Run: pip install python-pptx")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save pptx: {e}")


@app.post("/api/file/save")
async def save_file(request: FileSaveRequest):
    """Save content to a local file (allowed roots: documents, downloads, data dir)."""
    from vaf.core.platform import Platform
    try:
        target = Platform.normalize_path(request.path)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")

    # Security: only allow saving to specific directories
    allowed_roots = [
        Platform.documents_dir().resolve(),
        Platform.downloads_dir().resolve(),
        Platform.data_dir().resolve(),
        Platform.get_vaf_output_dir().resolve(),
    ]
    if not any(target.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="Access denied - file must be in Documents, Downloads, or VAF data directory")

    try:
        # Ensure parent directory exists
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write content
        target.write_text(request.content, encoding='utf-8')
        return {"status": "ok", "path": str(target)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")


@app.get("/sounds/{filename}")
async def get_sound(filename: str):
    """Serve sound files from vaf/media/sounds for Web UI (e.g. answer-complete notification)."""
    if filename not in ALLOWED_SOUND_FILES:
        raise HTTPException(status_code=404, detail="Sound not found")
    path = SOUNDS_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Sound not found")
    import mimetypes
    mime_type, _ = mimetypes.guess_type(filename)
    return FileResponse(
        path=str(path),
        media_type=mime_type or "audio/mpeg",
        filename=filename,
    )


@app.get("/api/download")
async def download_file(request: Request, path: str = Query(..., description="Absolute path to local file")):
    from vaf.core.platform import Platform
    from pathlib import Path
    import mimetypes

    try:
        target = Platform.normalize_path(path)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    allowed_roots = [
        Platform.documents_dir().resolve(),
        Platform.downloads_dir().resolve(),
        Platform.data_dir().resolve(),
        Platform.get_vaf_output_dir().resolve(),
    ]

    if not any(target.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="Access denied")

    mime_type, _ = mimetypes.guess_type(str(target))

    # Local users (127.0.0.1 / ::1) get inline serving — browser opens HTML, PDF, images directly.
    # Remote/LAN users get Content-Disposition: attachment so a download dialog appears.
    client_host = request.client.host if request.client else ""
    is_local = client_host in ("127.0.0.1", "::1", "localhost", "")
    return FileResponse(
        path=str(target),
        media_type=mime_type or "application/octet-stream",
        filename=None if is_local else target.name,
    )


@app.get("/api/notifications")
async def get_notifications_api(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
):
    """Return recent notifications for the current user (thinking, automation, channel replies)."""
    try:
        from vaf.api.config_routes import get_current_user_or_local_admin
        from vaf.core.user_notifications import get_notifications
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Notifications not available: {e}")
    user = get_current_user_or_local_admin(request)
    user_scope_id = user.get("user_scope_id")
    notifications = get_notifications(user_scope_id, limit=limit)
    return {"notifications": notifications}


@app.get("/api/workflows/{wf_id}")
async def get_workflow_details(wf_id: str):
    """Get workflow definition and steps."""
    try:
        from vaf.workflows.templates import WORKFLOW_TEMPLATES
        
        # Try to find by ID first
        wf = WORKFLOW_TEMPLATES.get(wf_id)
        
        # If not found, try by name (fallback)
        if not wf:
            for w in WORKFLOW_TEMPLATES.values():
                if w.get("name") == wf_id:
                    wf = w
                    break
        
        if wf:
            # Format steps for visualization
            steps = []
            for idx, step in enumerate(wf.get("steps", [])):
                # Create a pseudo-code representation of the step
                import json
                step_code = json.dumps(step, indent=2)
                steps.append({
                    "id": str(idx),
                    "name": step.get("description", f"Step {idx+1}"),
                    "type": step.get("tool", "unknown"),
                    "code": step_code
                })
                
            return {
                "id": wf_id,
                "name": wf.get("name"),
                "description": wf.get("description"),
                "triggers": wf.get("triggers", []),
                "steps": steps,
            }
        return {"error": "Workflow not found"}
    except Exception as e:
        return {"error": str(e)}

# Auth cookie name (must match auth_routes) so WebSocket can read JWT when frontend doesn't pass ?token=
VAF_TOKEN_COOKIE = "vaf_token"


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: Optional[str] = Query(None)):
    """
    WebSocket endpoint with optional token authentication.
    
    Token from: query param (?token=<jwt>) or cookie (vaf_token). Cookie is used when
    frontend connects without query (e.g. same-host login); ensures user_scope_id is
    set for RAG/memory so "No memories found" does not happen for logged-in users.
    
    When local_network_enabled=True, non-localhost requires valid JWT.
    When local_network_enabled=False, only localhost is allowed.
    """
    # Use cookie if frontend didn't pass token in URL (e.g. after login from same host)
    if not token and hasattr(websocket, "cookies") and websocket.cookies:
        token = websocket.cookies.get(VAF_TOKEN_COOKIE)
    client_ip = websocket.client.host if websocket.client else "unknown"
    print(f"[WebSocket] Connection attempt from {client_ip}")
    log("API", f"WebSocket connection attempt from {client_ip}")
    
    # First: check if client is localhost or Docker internal network
    try:
        from vaf.network.binding import is_localhost, is_allowed_ip
        import ipaddress
        
        # Check if localhost OR Docker internal network
        is_localhost_client = is_localhost(client_ip)
        
        # In Docker mode, also treat Docker bridge network as "local"
        # This is safe because Docker network is internal to the host
        if not is_localhost_client:
            try:
                ip_obj = ipaddress.ip_address(client_ip)
                docker_network = ipaddress.ip_network("172.16.0.0/12")
                if ip_obj in docker_network:
                    is_localhost_client = True
                    log("API", f"WebSocket: Docker network IP {client_ip} treated as localhost")
            except ValueError:
                pass
                
    except ImportError:
        # Fallback localhost check (including Docker network range 172.16.0.0/12)
        import ipaddress as _ipaddr
        _is_docker = False
        try:
            _is_docker = _ipaddr.ip_address(client_ip) in _ipaddr.ip_network("172.16.0.0/12")
        except ValueError:
            pass
        is_localhost_client = (
            client_ip in ["127.0.0.1", "::1", "localhost"] or _is_docker
        )
    
    # Check local network setting
    local_network_enabled = Config.get("local_network_enabled", False)
    
    # --- CONNECTION TRACKING (Before Auth) ---
    # Register connection immediately to show on map, even if auth fails later
    connection_id = f"ws_{id(websocket)}"
    user_context = None # Will be populated if auth succeeds
    
    try:
        from vaf.network.connection_tracker import (
            get_tracker, ConnectionType, detect_device_type, DeviceType
        )
        tracker = get_tracker()
        
        # Get user agent
        user_agent = None
        if hasattr(websocket, 'headers'):
            user_agent = websocket.headers.get('user-agent', '')
        
        device_type = detect_device_type(user_agent)
        
        tracker.register_connection(
            connection_id=connection_id,
            connection_type=ConnectionType.WEBSOCKET,
            ip=client_ip,
            device_type=device_type,
            user_agent=user_agent,
            username="Guest (Connecting...)", # Temporary status
            metadata={
                "is_localhost": is_localhost_client,
                "status": "handshake"
            }
        )
        log("API", f"Connection tracked (pre-auth): {connection_id}")
    except Exception as e:
        log("API", f"Could not track connection: {e}")

    # If local network is DISABLED, only allow localhost and require auth token.
    if not local_network_enabled:
        if not is_localhost_client:
            log("API", f"WebSocket rejected: Local network disabled, non-localhost IP {client_ip}")
            # Update tracker if possible
            try: get_tracker().unregister_connection(connection_id)
            except: pass
            
            await websocket.close(code=4003, reason="Local network feature is disabled")
            return
        if not token:
            log("API", "WebSocket rejected: Localhost connection without token")
            await websocket.close(code=4001, reason="Authentication required")
            return
        try:
            from vaf.auth.crypto import get_jwt_secret
            import jwt
            secret = get_jwt_secret()
            payload = jwt.decode(token, secret, algorithms=["HS256"])
            user_context = {
                "user_id": payload.get("sub"),
                "user_scope_id": payload.get("user_scope_id"),
                "username": payload.get("username"),
                "role": payload.get("role"),
                "session_id": payload.get("session_id"),
            }
            log("API", f"WebSocket (localhost) authenticated: {user_context.get('username')} (scope: {user_context.get('user_scope_id')})")
        except Exception as e:
            log("API", f"WebSocket rejected: Invalid localhost token ({e})")
            await websocket.close(code=4001, reason="Invalid token")
            return
    else:
        # Local network is ENABLED - authenticate network users
        try:
            from vaf.network.binding import is_allowed_ip
            from vaf.auth.crypto import get_jwt_secret
            import jwt
            
            # Validate IP is from local network
            if not is_allowed_ip(client_ip):
                log("API", f"WebSocket rejected: Non-local IP {client_ip}")
                try: get_tracker().unregister_connection(connection_id)
                except: pass
                await websocket.close(code=4003, reason="Local network only")
                return
            
            # Require token authentication for all network-mode clients (including localhost).
            if not token:
                log("API", f"WebSocket rejected: No token from {client_ip}")
                # Keep tracked as Guest/Unauth for map visibility?
                # Yes, but maybe mark as 'Auth Required'
                try: 
                    tracker = get_tracker()
                    tracker.register_connection(
                        connection_id=connection_id,
                        connection_type=ConnectionType.WEBSOCKET,
                        ip=client_ip,
                        device_type=device_type,
                        username="Unauthenticated Device",
                        metadata={"status": "auth_required"}
                    )
                except: pass
                
                await websocket.close(code=4001, reason="Authentication required")
                return
            
            if token:
                try:
                    secret = get_jwt_secret()
                    payload = jwt.decode(token, secret, algorithms=["HS256"])
                    
                    # Check 2FA status
                    require_2fa = Config.get("local_network_require_2fa", True)
                    if require_2fa and not payload.get("is_2fa_verified", False):
                        if not is_localhost_client or payload.get("role") != "admin":
                            log("API", f"WebSocket rejected: 2FA not verified for {payload.get('username')}")
                            await websocket.close(code=4003, reason="2FA verification required")
                            return
                    
                    user_context = {
                        "user_id": payload.get("sub"),
                        "user_scope_id": payload.get("user_scope_id"),
                        "username": payload.get("username"),
                        "role": payload.get("role"),
                        "session_id": payload.get("session_id"),
                    }
                    log("API", f"WebSocket authenticated: {user_context.get('username')}")
                    
                except jwt.ExpiredSignatureError:
                    log("API", f"WebSocket rejected: Expired token from {client_ip}")
                    await websocket.close(code=4001, reason="Token expired")
                    return
                except jwt.InvalidTokenError:
                    log("API", f"WebSocket rejected: Invalid token from {client_ip}")
                    await websocket.close(code=4001, reason="Invalid token")
                    return
        except ImportError:
            # Auth modules not available - still block non-localhost when disabled
            if not local_network_enabled and not is_localhost_client:
                log("API", f"WebSocket rejected: Local network disabled (auth module not available)")
                await websocket.close(code=4003, reason="Local network feature is disabled")
                return
        except Exception as e:
            log("API", f"WebSocket auth error: {e}")
            # Block non-localhost on error when local network is disabled
            if not local_network_enabled and not is_localhost_client:
                await websocket.close(code=4003, reason="Local network feature is disabled")
                return
    
    try:
        await manager.connect(websocket)
        # Store user_scope_id for RAG/task metadata (memory_save, memory search). Fallback to user_id if no scope in token.
        if user_context and (user_context.get("user_scope_id") or user_context.get("user_id")):
            manager.set_connection_user(
                websocket,
                user_context.get("user_scope_id") or user_context.get("user_id"),
                username=user_context.get("username"),
                role=user_context.get("role"),
            )
        os.environ["VAF_WEBUI_ACTIVE"] = "1"
        print(f"[WebSocket] Connected! Active: {len(manager.active_connections)}")
        log("API", f"WebSocket connected! Active: {len(manager.active_connections)}")
        tray_context.set_websocket_count(len(manager.active_connections)) # Update active count
        log("API", f"WebSocket count updated: {tray_context.active_websockets}")
        
        # Update connection tracker with verified info
        try:
            tracker = get_tracker()
            tracker.register_connection(
                connection_id=connection_id,
                connection_type=ConnectionType.WEBSOCKET,
                ip=client_ip,
                device_type=device_type,
                user_agent=user_agent,
                username=user_context.get("username") if user_context else "Guest",
                service_name="WebUI",
                metadata={
                    "role": user_context.get("role") if user_context else "guest",
                    "is_localhost": is_localhost_client,
                    "status": "connected"
                }
            )
            log("API", f"Connection updated: {connection_id}")
        except Exception as e:
            log("API", f"Could not track connection: {e}")

        # Count Web UI open as activity so thinking mode does not start immediately (idle timer starts after connect)
        try:
            from vaf.core.last_interaction import update_last_interaction
            scope_id = (user_context.get("user_scope_id") or user_context.get("user_id")) if user_context else None
            update_last_interaction(user_scope_id=scope_id, source="web", preview="")
        except Exception:
            pass
        
        try:
            provider = Config.get("provider", "local")
            await websocket.send_json({
                "type": "model_state",
                "loaded": tray_context.model_loaded,
                "persistent": tray_context.is_persistent(),
                "provider": provider
            })
            # Send user context if authenticated
            if user_context:
                await websocket.send_json({
                    "type": "auth_state",
                    "authenticated": True,
                    "username": user_context.get("username"),
                    "role": user_context.get("role"),
                })
        except Exception:
            pass
    except Exception as e:
        log("API", f"WebSocket handshake failed: {e}")
        raise e
    try:
        # Get user scope for filtering sessions
        user_scope_id = manager.get_connection_user(websocket)
        
        # Send initial session list (only Web UI chats; channel sessions appear in their dashboards)
        sessions = session_mgr.list(limit=SESSION_LIST_LIMIT, user_scope_id=user_scope_id)
        web_sessions = _web_ui_sessions(sessions)
        await websocket.send_json({
            "type": "session_list",
            "sessions": [
                {"id": s["id"], "title": s["name"], "date": s["updated_at"], "messageCount": s["message_count"], "source": (s.get("metadata") or {}).get("source")}
                for s in web_sessions
            ]
        })
        # Send cached stats if available, otherwise send defaults
        stats_to_send = manager.last_stats
        if not stats_to_send:
            # Default stats until agent provides real values
            stats_to_send = {"used": 0, "total": 8192, "percent": 0.0, "api": False}
        await websocket.send_json({
            "type": "stats",
            "stats": stats_to_send
        })
        # Send tools list (cached or live) so UI has correct count
        try:
            agent = manager.agent_instance
            if agent and hasattr(agent, "tools"):
                tools_list = [
                    {
                        "name": name,
                        "description": getattr(tool, "description", "No description"),
                        "category": getattr(tool, "category", "general")
                    }
                    for name, tool in agent.tools.items()
                ]
            elif manager.tools_cache:
                tools_list = manager.tools_cache
            else:
                tools_list = _scan_tool_modules()
            await websocket.send_json({
                "type": "tools_list",
                "tools": tools_list or []
            })
        except Exception:
            pass
        # Auto-load latest Web UI session so WebUI gets a valid sessionId immediately (not a channel chat)
        if web_sessions:
            sid = web_sessions[0]["id"]
        else:
            # Create a new default session for the user if they have none
            new_sess = session_mgr.new(user_scope_id=user_scope_id)
            session_mgr.save(new_sess)
            sid = new_sess.id
            # Refresh the list for the client so they see the new session
            sessions = session_mgr.list(limit=SESSION_LIST_LIMIT, user_scope_id=user_scope_id)
            web_sessions = _web_ui_sessions(sessions)
            await websocket.send_json({
                "type": "session_list",
                "sessions": [
                    {"id": s["id"], "title": s["name"], "date": s["updated_at"], "messageCount": s["message_count"], "source": (s.get("metadata") or {}).get("source")}
                    for s in web_sessions
                ]
            })

        try:
            # Subscribe this connection to the session for scoped updates
            manager.subscribe_to_session(websocket, sid)

            try:
                loaded = session_mgr.load(sid)
            except FileNotFoundError:
                # Should not happen for newly created session, but safety first
                loaded = Session(id=sid, name="New Chat")

            import re
            def clean_history_text(text):
                if not text: return ""
                text = re.sub(r'\[dim\]', '<think>', text, flags=re.IGNORECASE)
                text = re.sub(r'\[white dim\]', '<think>', text, flags=re.IGNORECASE)
                text = re.sub(r'\[.*?dim.*?\]', '<think>', text, flags=re.IGNORECASE)
                text = text.replace('[/dim]', '</think>')
                if '<think>' in text and '</think>' not in text:
                    text = text.replace('[/]', '</think>')
                text = re.sub(r'\[\/?[^\]]+\]', '', text)
                return text

            frontend_messages = []
            for msg in loaded.messages:
                role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "user")
                content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
                timestamp = msg.get("timestamp") if isinstance(msg, dict) else getattr(msg, "timestamp", None)
                meta = msg.get("metadata") if isinstance(msg, dict) else getattr(msg, "metadata", None) or {}
                if role == "assistant":
                    content = clean_history_text(content)
                entry = {"role": role, "content": content, "timestamp": timestamp}
                if role == "user" and meta and meta.get("images"):
                    entry["images"] = [
                        {
                            "url": f"data:{img.get('mime_type', 'image/jpeg')};base64,{img.get('data', '')}",
                            "name": img.get("name", "image"),
                        }
                        for img in meta["images"]
                        if img.get("data")
                    ]
                if role == "tool":
                    # Try metadata dict first, then fall back to top-level keys
                    # (backend stores tool info as top-level: name, tool_call_id)
                    tool_name = (meta.get("toolName") if meta else None) or msg.get("name")
                    tool_id = (meta.get("toolId") if meta else None) or msg.get("tool_call_id")
                    tool_status = (meta.get("toolStatus") if meta else None)
                    if tool_name is not None:
                        entry["toolName"] = tool_name
                    if tool_id is not None:
                        entry["toolId"] = tool_id
                    # If tool has content (result), it completed successfully
                    entry["toolStatus"] = tool_status or ("completed" if content else "running")
                frontend_messages.append(entry)

            # If this is a thinking session and we have a stored user reply, append it once
            if sid and str(sid).startswith("thinking_"):
                try:
                    from vaf.core.thinking_mode import pop_user_reply_for_session
                    reply_data = pop_user_reply_for_session(sid)
                    if reply_data:
                        preview = (reply_data.get("reply") or "").strip()
                        if preview:
                            frontend_messages.append({
                                "role": "user",
                                "content": f"User replied: {preview}",
                                "timestamp": reply_data.get("at"),
                            })
                except Exception:
                    pass

            is_active = False
            try:
                if (manager.agent_instance and
                    getattr(manager.agent_instance, '_session_id', None) == sid and
                    manager.latest_state.get("status") != "idle"):
                    is_active = True
            except: pass

            await websocket.send_json({
                "type": "history_update",
                "messages": frontend_messages,
                "sessionId": sid,
                "isActive": is_active,
                "currentStatus": manager.latest_state.get("status", "idle") if is_active else "idle"
            })
            artifact_payload = _build_artifact_payload(loaded, sid)
            if artifact_payload:
                await websocket.send_json(artifact_payload)
            # Restore sidebar documents from session on connect (so UI recovers after page refresh)
            try:
                _saved_sidebar = (getattr(loaded, "runtime_state", None) or {}).get("sidebar_documents") or []
                if _saved_sidebar:
                    _slim_sidebar = [{k: v for k, v in d.items() if k != "data"} for d in _saved_sidebar]
                    await websocket.send_json({
                        "type": "sidebar_documents_restored",
                        "contents": _slim_sidebar,
                        "sessionId": sid,
                    })
            except Exception:
                pass
        except Exception as e:
            log("WebServer", f"Auto-load session failed: {e}")

        while True:
            # Listen for client commands
            data_str = await websocket.receive_text()
            tray_context.register_websocket_activity()
            try:
                cmd = json.loads(data_str)
                # Several handlers below read the incoming message via `data`; keep it as an alias of
                # `cmd` so they work on a fresh connection. (Handlers that fetch over HTTP locally
                # rebind `data = resp.json()`, which is unaffected by this alias.)
                data = cmd
                type = cmd.get("type")
                
                # --- SESSION MANAGEMENT ---
                
                if type == "get_sessions":
                    # user_scope_id is required for correct RAG scope and filtered session listing
                    user_scope_id = manager.get_connection_user(websocket)
                    sessions = session_mgr.list(limit=SESSION_LIST_LIMIT, user_scope_id=user_scope_id)
                    web_sessions = _web_ui_sessions(sessions)
                    await websocket.send_json({
                        "type": "session_list",
                        "sessions": [
                            {"id": s["id"], "title": s["name"], "date": s["updated_at"], "messageCount": s["message_count"], "source": (s.get("metadata") or {}).get("source")}
                            for s in web_sessions
                        ]
                    })                
                elif type == "load_session":
                    sid = cmd.get("id")
                    user_scope_id = manager.get_connection_user(websocket)
                    try:
                        # 1. Load from disk (to check ownership before subscribing)
                        loaded = session_mgr.load(sid)
                        
                        # Verify ownership: matches OR session has no scope (legacy) OR user is local admin
                        session_scope = (loaded.metadata or {}).get("user_scope_id")
                        from vaf.core.config import get_local_admin_scope_id
                        local_admin_scope = get_local_admin_scope_id()
                        
                        is_owner = not session_scope or str(session_scope) == str(user_scope_id)
                        is_admin = str(user_scope_id) == str(local_admin_scope)
                        
                        if not is_owner and not is_admin:
                            log("API", f"Access denied: Session {sid} (scope {session_scope}) does not belong to user {user_scope_id}")
                            await websocket.send_json({"type": "error", "message": "Access denied"})
                            continue

                        # Subscribe this connection to the session for scoped updates
                        manager.subscribe_to_session(websocket, sid)
                        
                        # Push command to main loop to switch session
                        from vaf.core.task_queue import TaskQueue
                        tq = TaskQueue()
                        tq.add(
                            session_id="system",
                            input_text=f"__CMD__:LOAD_SESSION:{sid}",
                            source="web",
                            metadata={"task_class": "background"},
                        )
                        
                        # Helper to clean historical messages (same logic as run.py)
                        import re
                        def clean_history_text(text):
                            if not text: return ""
                            # Convert rich dim to think tags
                            text = re.sub(r'\[dim\]', '<think>', text, flags=re.IGNORECASE)
                            text = re.sub(r'\[white dim\]', '<think>', text, flags=re.IGNORECASE)
                            text = re.sub(r'\[.*?dim.*?\]', '<think>', text, flags=re.IGNORECASE)
                            text = text.replace('[/dim]', '</think>')
                            if '<think>' in text and '</think>' not in text:
                                text = text.replace('[/]', '</think>')
                            # Strip remaining tags
                            text = re.sub(r'\[\/?[^\]]+\]', '', text)
                            return text
                        
                        # 3. Send history to Frontend
                        # Serialize messages for JSON (include tool metadata so UI shows tool names, not "Unknown Tool")
                        frontend_messages = []
                        for msg in loaded.messages:
                            role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "user")
                            content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
                            timestamp = msg.get("timestamp") if isinstance(msg, dict) else getattr(msg, "timestamp", None)
                            meta = msg.get("metadata") if isinstance(msg, dict) else getattr(msg, "metadata", None) or {}
                            # Clean content if it's from assistant (remove legacy artifacts)
                            if role == "assistant":
                                content = clean_history_text(content)
                            entry = {"role": role, "content": content, "timestamp": timestamp}
                            if role == "tool":
                                # Try metadata dict first, then fall back to top-level keys
                                # (backend stores tool info as top-level: name, tool_call_id)
                                tool_name = (meta.get("toolName") if meta else None) or msg.get("name")
                                tool_id = (meta.get("toolId") if meta else None) or msg.get("tool_call_id")
                                tool_status = (meta.get("toolStatus") if meta else None)
                                if tool_name is not None:
                                    entry["toolName"] = tool_name
                                if tool_id is not None:
                                    entry["toolId"] = tool_id
                                # If tool has content (result), it completed successfully
                                entry["toolStatus"] = tool_status or ("completed" if content else "running")
                            frontend_messages.append(entry)

                        # If this is a thinking session and we have a stored user reply, append it once
                        if sid and str(sid).startswith("thinking_"):
                            try:
                                from vaf.core.thinking_mode import pop_user_reply_for_session
                                reply_data = pop_user_reply_for_session(sid)
                                if reply_data:
                                    preview = (reply_data.get("reply") or "").strip()
                                    if preview:
                                        frontend_messages.append({
                                            "role": "user",
                                            "content": f"User replied: {preview}",
                                            "timestamp": reply_data.get("at"),
                                        })
                            except Exception:
                                pass

                        # Check if this session is currently active in the agent
                        is_active = False
                        try:
                            # If agent is working on this session ID right now
                            # We use internal _session_id which we synced in agent.py
                            if (manager.agent_instance and 
                                getattr(manager.agent_instance, '_session_id', None) == sid and
                                manager.latest_state.get("status") != "idle"):
                                is_active = True
                        except: pass

                        await websocket.send_json({
                            "type": "history_update",
                            "messages": frontend_messages,
                            "sessionId": sid,
                            "isActive": is_active,
                            "currentStatus": manager.latest_state.get("status", "idle") if is_active else "idle"
                        })
                        artifact_payload = _build_artifact_payload(loaded, sid)
                        if artifact_payload:
                            await websocket.send_json(artifact_payload)

                        # Restore sidebar documents from session — without base64 data (too large).
                        # The frontend can re-show attached document names from the saved slim entries.
                        try:
                            _saved_sidebar = (getattr(loaded, "runtime_state", None) or {}).get("sidebar_documents") or []
                            if _saved_sidebar:
                                # Strip data field to keep the WS response small
                                _slim_sidebar = [{k: v for k, v in d.items() if k != "data"} for d in _saved_sidebar]
                                await websocket.send_json({
                                    "type": "sidebar_documents_restored",
                                    "contents": _slim_sidebar,
                                    "sessionId": sid,
                                })
                        except Exception:
                            pass

                        # 4. Send initial estimated stats (so UI is not empty)
                        try:
                            # Estimate based on loaded history
                            total_chars = sum(len(str(m.get("content", ""))) for m in frontend_messages)
                            est_tokens = total_chars // 3  # Rough estimate

                            # Get max context from config
                            cfg = Config.load()
                            max_ctx = cfg.get("n_ctx", 8192)
                            is_api = cfg.get("provider", "local") != "local"

                            if is_api and max_ctx <= 16384:
                                max_ctx = 128000

                            stats = {
                                "used": est_tokens,
                                "total": max_ctx,
                                "percent": round((est_tokens / max_ctx) * 100, 1) if max_ctx else 0.0,
                                "api": is_api
                            }
                            await websocket.send_json({
                                "type": "stats",
                                "stats": stats
                            })
                        except Exception as e:
                            print(f"[WebServer] Stats estimation error: {e}")

                        # 5. Send context_status with persisted user_turn_count so the
                        #    compaction progress bar is correct immediately after load/restart.
                        try:
                            from vaf.core.config import Config as _Cfg
                            _compaction_interval = int(_Cfg.get("memory_compaction_interval", 15))
                            _runtime = getattr(loaded, "runtime_state", None) or {}
                            _user_turn_count = _runtime.get("user_turn_count", 0)
                            if _user_turn_count == 0:
                                # Fallback: count from saved messages
                                _user_turn_count = sum(
                                    1 for m in loaded.messages
                                    if (m.get("role") if isinstance(m, dict) else getattr(m, "role", None)) == "user"
                                )
                            await websocket.send_json({
                                "type": "context_status",
                                "sessionId": sid,
                                "stats": {
                                    "user_turn_count": _user_turn_count,
                                    "compaction_interval": _compaction_interval,
                                    "compaction_progress": round((_user_turn_count % _compaction_interval) / _compaction_interval * 100) if _compaction_interval else 0,
                                    "message_count": len(loaded.messages),
                                }
                            })
                        except Exception as e:
                            print(f"[WebServer] context_status on load error: {e}")

                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        print(f"Load error: {e}")

                elif type == "delete_session":
                    sid = cmd.get("id")
                    user_scope_id = manager.get_connection_user(websocket)
                    session_mgr.delete(sid)
                    # Broadcast update ONLY to this user's connections
                    sessions = session_mgr.list(limit=SESSION_LIST_LIMIT, user_scope_id=user_scope_id)
                    web_sessions = _web_ui_sessions(sessions)
                    await manager.broadcast_to_user(user_scope_id, {
                        "type": "session_list",
                        "sessions": [
                            {"id": s["id"], "title": s["name"], "date": s["updated_at"], "messageCount": s["message_count"], "source": (s.get("metadata") or {}).get("source")}
                            for s in web_sessions
                        ]
                    })

                elif type == "hide_session":
                    sid = cmd.get("id")
                    user_scope_id = manager.get_connection_user(websocket)
                    if sid and session_mgr.hide(sid):
                        sessions = session_mgr.list(limit=SESSION_LIST_LIMIT, user_scope_id=user_scope_id)
                        web_sessions = _web_ui_sessions(sessions)
                        await manager.broadcast_to_user(user_scope_id, {
                            "type": "session_list",
                            "sessions": [
                                {"id": s["id"], "title": s["name"], "date": s["updated_at"], "messageCount": s["message_count"], "source": (s.get("metadata") or {}).get("source")}
                                for s in web_sessions
                            ]
                        })

                elif type == "new_session":
                    user_scope_id = manager.get_connection_user(websocket)
                    # Push command to main loop to create new session
                    from vaf.core.task_queue import TaskQueue
                    tq = TaskQueue()
                    tq.add(
                        session_id="system",
                        input_text="__CMD__:NEW_SESSION",
                        source="web",
                        metadata={"user_scope_id": user_scope_id, "task_class": "background"},
                    )
                    
                    # Create new session object AND SAVE IT IMMEDIATELY (temp, main loop will take over)
                    new_sess = session_mgr.new(user_scope_id=user_scope_id)
                    session_mgr.save(new_sess)
                    
                    # Subscribe this connection to the new session for scoped updates
                    manager.subscribe_to_session(websocket, new_sess.id)
                    
                    # Refresh list
                    sessions = session_mgr.list(limit=SESSION_LIST_LIMIT, user_scope_id=user_scope_id)
                    web_sessions = _web_ui_sessions(sessions)
                    await websocket.send_json({
                        "type": "session_list",
                        "sessions": [
                            {"id": s["id"], "title": s["name"], "date": s["updated_at"], "messageCount": s["message_count"], "source": (s.get("metadata") or {}).get("source")}
                            for s in web_sessions
                        ]
                    })
                    # Clear frontend chat
                    await websocket.send_json({
                        "type": "history_update",
                        "messages": [],
                        "sessionId": new_sess.id
                    })

                elif type == "rename_session":
                    sid = cmd.get("id")
                    new_name = cmd.get("newName")
                    user_scope_id = manager.get_connection_user(websocket)
                    if sid and new_name:
                        session_mgr.rename(sid, new_name)
                        # Notify Main Loop to update in-memory object
                        from vaf.core.task_queue import TaskQueue
                        tq = TaskQueue()
                        tq.add(
                            session_id="system",
                            input_text=f"__CMD__:RENAME_SESSION:{sid}:{new_name}",
                            source="web",
                            metadata={"task_class": "background"},
                        )
                        
                        # Broadcast update ONLY to this user's connections
                        sessions = session_mgr.list(limit=SESSION_LIST_LIMIT, user_scope_id=user_scope_id)
                        web_sessions = _web_ui_sessions(sessions)
                        await manager.broadcast_to_user(user_scope_id, {
                            "type": "session_list",
                            "sessions": [
                                {"id": s["id"], "title": s["name"], "date": s["updated_at"], "messageCount": s["message_count"], "source": (s.get("metadata") or {}).get("source")}
                                for s in web_sessions
                            ]
                        })

                elif type == "get_config":
                    # Send config to frontend; non-admins get scoped view (only their own connections)
                    from vaf.core.config import get_local_admin_scope_id
                    user_scope_id = manager.get_connection_user(websocket) if manager else None
                    stored_role = manager.get_connection_user_role(websocket) if manager else None
                    # Admin if stored role says "admin" OR scope matches local admin scope
                    local_admin_scope = get_local_admin_scope_id()
                    is_admin = stored_role == "admin" or (user_scope_id is not None and str(user_scope_id) == str(local_admin_scope))
                    role = "admin" if is_admin else "user"
                    full_cfg = Config.load()
                    cfg = Config.config_for_user(full_cfg, str(user_scope_id) if user_scope_id else None, role)
                    await websocket.send_json({
                        "type": "config_update",
                        "config": cfg
                    })

                elif type == "get_models":
                    # Scan models directory for .gguf files
                    # Use absolute path based on project root (parent of 'vaf' package)
                    # core/web_server.py -> core/ -> vaf/ -> VAF/ -> VAF/models
                    project_root = Path(__file__).parent.parent.parent
                    models_dir = project_root / "models"
                    # print(f"[DEBUG] Looking for models in: {models_dir}")
                    models = []
                    if models_dir.exists():
                        models = [f.name for f in models_dir.glob("*.gguf")]
                        # print(f"[DEBUG] Found models: {models}")
                    else:
                        # print(f"[DEBUG] Models directory not found at {models_dir}")
                        pass
                    
                    await websocket.send_json({
                        "type": "models_list",
                        "models": models
                    })

                elif type == "get_model_preview":
                    repo_id = (cmd.get("repo_id") or "").strip()
                    if not repo_id:
                        await websocket.send_json({
                            "type": "model_preview",
                            "repo_id": "",
                            "error": "repo_id is required (e.g. Nanbeige/Nanbeige4.1-3B)"
                        })
                    else:
                        try:
                            from huggingface_hub import HfApi
                            try:
                                from huggingface_hub import ModelCard
                            except ImportError:
                                ModelCard = None
                            api = HfApi()
                            model_info = api.model_info(repo_id=repo_id, files_metadata=True)
                            siblings = getattr(model_info, "siblings", [])
                            gguf_files = []
                            for f in siblings:
                                rfilename = getattr(f, "rfilename", f) if not isinstance(f, str) else f
                                if isinstance(rfilename, str) and rfilename.endswith(".gguf"):
                                    size_bytes = getattr(f, "size", None) or 0
                                    gguf_files.append({"filename": rfilename, "size_bytes": size_bytes})
                            gguf_files.sort(key=lambda x: x["size_bytes"])
                            card_content = None
                            if ModelCard is not None:
                                try:
                                    card = ModelCard.load(repo_id)
                                    card_content = getattr(card, "content", None) or getattr(card, "text", None) or ""
                                except Exception:
                                    pass
                            if not gguf_files:
                                try:
                                    all_files = api.list_repo_files(repo_id=repo_id)
                                    gguf_from_list = [p for p in all_files if isinstance(p, str) and p.endswith(".gguf")]
                                    if gguf_from_list:
                                        gguf_files = [{"filename": p, "size_bytes": 0} for p in gguf_from_list]
                                except Exception:
                                    pass
                                if not gguf_files:
                                    await websocket.send_json({
                                        "type": "model_preview",
                                        "repo_id": repo_id,
                                        "error": f"No GGUF files found in {repo_id}. This repo may be a base model (safetensors only). Try a GGUF repo instead, e.g. search on Hugging Face for \"<model name> GGUF\".",
                                        "gguf_files": []
                                    })
                                else:
                                    await websocket.send_json({
                                        "type": "model_preview",
                                        "repo_id": repo_id,
                                        "card_content": card_content,
                                        "gguf_files": gguf_files
                                    })
                            else:
                                await websocket.send_json({
                                    "type": "model_preview",
                                    "repo_id": repo_id,
                                    "card_content": card_content,
                                    "gguf_files": gguf_files
                                })
                        except Exception as e:
                            await websocket.send_json({
                                "type": "model_preview",
                                "repo_id": repo_id,
                                "error": str(e),
                                "gguf_files": []
                            })

                elif type == "download_model":
                    repo_id = (cmd.get("repo_id") or "").strip()
                    filename = (cmd.get("filename") or "").strip() or None
                    if not repo_id:
                        await websocket.send_json({
                            "type": "model_download_done",
                            "success": False,
                            "error": "repo_id is required (e.g. Nanbeige/Nanbeige4.1-3B)",
                            "models": []
                        })
                    else:
                        project_root = Path(__file__).parent.parent.parent
                        models_dir = project_root / "models"
                        progress_queue: queue.Queue = queue.Queue()
                        cancel_event = threading.Event()
                        ws_id = id(websocket)
                        _active_model_download_cancels[ws_id] = cancel_event

                        def make_progress_tqdm(pq: queue.Queue, ce: threading.Event):
                            class ProgressTqdm:
                                def __init__(self, total=None, **kwargs):
                                    self.n = 0
                                    self.total = total or 0
                                    self._pq = pq
                                    self._ce = ce
                                    self._start = time.time()
                                def update(self, n=1):
                                    self.n += n
                                    if self._ce.is_set():
                                        raise InterruptedError("Download cancelled")
                                    if self.total and self._pq is not None:
                                        pct = 100.0 * self.n / self.total
                                        elapsed = time.time() - self._start
                                        speed = self.n / elapsed if elapsed > 0 else 0
                                        if speed >= 1024 * 1024:
                                            speed_str = f"{speed / (1024 * 1024):.2f} MB/s"
                                        else:
                                            speed_str = f"{speed / 1024:.2f} KB/s"
                                        try:
                                            self._pq.put_nowait({
                                                "bytes_done": self.n, "bytes_total": self.total,
                                                "progress_pct": round(pct, 2), "speed_str": speed_str
                                            })
                                        except queue.Full:
                                            pass
                                def close(self): pass
                                def __enter__(self): return self
                                def __exit__(self, *a): return None
                            return ProgressTqdm

                        def _do_download() -> tuple[bool, str | None, list]:
                            try:
                                from huggingface_hub import HfApi, hf_hub_download
                                models_dir.mkdir(parents=True, exist_ok=True)
                                api = HfApi()
                                tqdm_class = make_progress_tqdm(progress_queue, cancel_event)
                                if not filename:
                                    try:
                                        model_info = api.model_info(repo_id=repo_id, files_metadata=True)
                                        siblings = getattr(model_info, "siblings", [])
                                    except Exception:
                                        siblings = []
                                        for f in api.list_repo_files(repo_id=repo_id):
                                            class _F:
                                                rfilename = f
                                                size = 0
                                            siblings.append(_F())
                                    gguf = [f for f in siblings if (getattr(f, "rfilename", f) if not isinstance(f, str) else f).endswith(".gguf")]
                                    if not gguf:
                                        return False, f"No GGUF files in {repo_id}", []
                                    gguf.sort(key=lambda x: getattr(x, "size", 0) or 0)
                                    chosen = gguf[0]
                                    fname = getattr(chosen, "rfilename", chosen) if not isinstance(chosen, str) else chosen
                                    hf_hub_download(repo_id=repo_id, filename=fname, local_dir=str(models_dir), tqdm_class=tqdm_class)
                                else:
                                    hf_hub_download(repo_id=repo_id, filename=filename, local_dir=str(models_dir), tqdm_class=tqdm_class)
                                new_models = [f.name for f in models_dir.glob("*.gguf")]
                                return True, None, new_models
                            except InterruptedError:
                                return False, "Download cancelled", []
                            except Exception as e:
                                return False, str(e), []

                        async def run_download():
                            nonlocal progress_queue, ws_id
                            download_task = asyncio.create_task(asyncio.to_thread(_do_download))

                            async def drain_progress():
                                while not download_task.done():
                                    await asyncio.sleep(0.2)
                                    while True:
                                        try:
                                            msg = progress_queue.get_nowait()
                                            await websocket.send_json({
                                                "type": "model_download_progress",
                                                "progress_pct": msg.get("progress_pct"),
                                                "bytes_done": msg.get("bytes_done"),
                                                "bytes_total": msg.get("bytes_total"),
                                                "speed_str": msg.get("speed_str")
                                            })
                                        except queue.Empty:
                                            break
                                # Final drain
                                while True:
                                    try:
                                        msg = progress_queue.get_nowait()
                                        await websocket.send_json({
                                            "type": "model_download_progress",
                                            "progress_pct": msg.get("progress_pct"),
                                            "bytes_done": msg.get("bytes_done"),
                                            "bytes_total": msg.get("bytes_total"),
                                            "speed_str": msg.get("speed_str")
                                        })
                                    except queue.Empty:
                                        break

                            drain_task = asyncio.create_task(drain_progress())
                            try:
                                success, err, new_models = await download_task
                            except Exception as e:
                                success, err, new_models = False, str(e), []
                            finally:
                                _active_model_download_cancels.pop(ws_id, None)
                                drain_task.cancel()
                                try:
                                    await drain_task
                                except asyncio.CancelledError:
                                    pass
                            try:
                                await websocket.send_json({
                                    "type": "model_download_done",
                                    "success": success,
                                    "error": err,
                                    "models": new_models
                                })
                            except Exception:
                                pass

                        asyncio.create_task(run_download())

                elif type == "cancel_model_download":
                    ws_id = id(websocket)
                    if ws_id in _active_model_download_cancels:
                        _active_model_download_cancels[ws_id].set()

                elif type == "get_api_models":
                    # Fetch available models from API providers
                    provider = cmd.get("provider", "openai")
                    api_key = cmd.get("api_key", "")
                    models = []
                    
                    try:
                        if provider == "openai" and api_key:
                            import httpx
                            async with httpx.AsyncClient() as client:
                                resp = await client.get(
                                    "https://api.openai.com/v1/models",
                                    headers={"Authorization": f"Bearer {api_key}"},
                                    timeout=10.0
                                )
                                if resp.status_code == 200:
                                    data = resp.json()
                                    # Filter to chat models only
                                    models = sorted([
                                        m["id"] for m in data.get("data", [])
                                        if "gpt" in m["id"] or "o1" in m["id"] or "o3" in m["id"]
                                    ])
                        elif provider == "anthropic":
                            # Anthropic models list (requires API key)
                            if api_key:
                                import httpx
                                async with httpx.AsyncClient() as client:
                                    resp = await client.get(
                                        "https://api.anthropic.com/v1/models",
                                        headers={
                                            "X-Api-Key": api_key,
                                            "anthropic-version": "2023-06-01"
                                        },
                                        timeout=10.0
                                    )
                                    if resp.status_code == 200:
                                        data = resp.json()
                                        models = [m["id"] for m in data.get("data", []) if m.get("id")]
                        elif provider == "deepseek" and api_key:
                            import httpx
                            async with httpx.AsyncClient() as client:
                                resp = await client.get(
                                    "https://api.deepseek.com/models",
                                    headers={"Authorization": f"Bearer {api_key}"},
                                    timeout=10.0
                                )
                                if resp.status_code == 200:
                                    data = resp.json()
                                    models = [m["id"] for m in data.get("data", [])]
                        elif provider == "google":
                            if api_key:
                                import httpx
                                async with httpx.AsyncClient() as client:
                                    resp = await client.get(
                                        "https://generativelanguage.googleapis.com/v1beta/models",
                                        params={"key": api_key, "pageSize": 1000},
                                        timeout=10.0
                                    )
                                    if resp.status_code == 200:
                                        data = resp.json()
                                        raw_models = data.get("models", [])
                                        unique_models = []
                                        for model in raw_models:
                                            methods = model.get("supportedGenerationMethods", [])
                                            if "generateContent" not in methods:
                                                continue
                                            model_id = model.get("baseModelId") or model.get("name", "")
                                            if model_id.startswith("models/"):
                                                model_id = model_id.split("/", 1)[1]
                                            if model_id and model_id not in unique_models:
                                                unique_models.append(model_id)
                                        models = sorted(unique_models)
                        elif provider == "openrouter" and api_key:
                            import httpx
                            async with httpx.AsyncClient() as client:
                                resp = await client.get(
                                    "https://openrouter.ai/api/v1/models",
                                    headers={"Authorization": f"Bearer {api_key}"},
                                    timeout=10.0
                                )
                                if resp.status_code == 200:
                                    data = resp.json()
                                    models = [m["id"] for m in data.get("data", [])][:50]  # Limit
                    except Exception as e:
                        log("WebServer", f"Failed to fetch models for {provider}: {e}")
                    
                    await websocket.send_json({
                        "type": "api_models_list",
                        "provider": provider,
                        "models": models
                    })

                elif type == "save_config":
                    new_config = cmd.get("config")
                    if new_config:
                        from vaf.core.config import get_local_admin_scope_id
                        user_scope_id = manager.get_connection_user(websocket) if manager else None
                        stored_role = manager.get_connection_user_role(websocket) if manager else None
                        local_admin_scope = get_local_admin_scope_id()
                        is_admin = stored_role == "admin" or (user_scope_id is not None and str(user_scope_id) == str(local_admin_scope))
                        existing = Config.load()
                        if not is_admin:
                            new_filtered, scope_toggles = Config.extract_connection_toggles_for_scope(new_config, str(user_scope_id) if user_scope_id else None)
                            new_config = Config.filter_for_non_admin(new_filtered)
                            if scope_toggles:
                                by_scope = existing.get("connection_enabled_by_scope") or {}
                                if not isinstance(by_scope, dict):
                                    by_scope = {}
                                for scope_id, toggles in scope_toggles.items():
                                    by_scope[scope_id] = {**(by_scope.get(scope_id) or {}), **toggles}
                                existing["connection_enabled_by_scope"] = by_scope
                        merged = Config.merge_preserving_nonempty_sensitive(existing, new_config)
                        Config.save(merged)
                        provider_changed = existing.get("provider") != merged.get("provider")

                        try:
                            if "tray_autostart" in merged:
                                from vaf.core.platform import Platform
                                Platform.set_tray_autostart(bool(merged.get("tray_autostart")))
                        except Exception as e:
                            log("WebServer", f"Tray autostart update failed: {e}")

                        # Use TaskQueue for commands (headless_runner only reads from TaskQueue)
                        # Priority 1 so RELOAD_CONFIG is processed before any pending chat (priority 10)
                        from vaf.core.task_queue import TaskQueue
                        tq = TaskQueue()
                        tq.add(
                            session_id="system",
                            input_text="__CMD__:RELOAD_CONFIG",
                            source="web",
                            priority=1,
                            metadata={"task_class": "background"},
                        )
                        await websocket.send_json({
                            "type": "config_saved",
                            "status": "success",
                            "requires_refresh": provider_changed
                        })

                elif type == "get_autosuggest":
                    text = cmd.get("text", "")
                    if text:
                        # Use the internal _get_best_suggestion method
                        suggestion = get_autosuggest()._get_best_suggestion(text)
                        await websocket.send_json({
                            "type": "autosuggest_result",
                            "suggestion": suggestion
                        })
                
                elif type == "artifact_edit":
                    session_id = cmd.get("sessionId") or manager.get_session_for_connection(websocket)
                    user_scope_id = manager.get_connection_user(websocket)
                    if not session_id:
                        # Use user-scoped default to prevent crosstalk
                        safe_scope = str(user_scope_id or "default").replace("-", "")[:8]
                        session_id = f"web-default-{safe_scope}"
                    file = cmd.get("file", "")
                    code = cmd.get("code", "")
                    source = cmd.get("source", "web")
                    try:
                        from datetime import datetime
                        updated_at = datetime.now().isoformat()
                        loaded = session_mgr.load(session_id)
                        loaded.update_runtime_state("artifact", {
                            "file": file,
                            "code": code,
                            "updatedAt": updated_at,
                            "source": source
                        })
                        session_mgr.save(loaded, sync_state=False)
                        await manager.broadcast_to_session(session_id, {
                            "type": "artifact_update",
                            "file": file,
                            "code": code,
                            "updatedAt": updated_at,
                            "source": source
                        })
                    except Exception as e:
                        log("WebServer", f"Artifact update failed: {e}")

                elif type == "contact_reply_decision":
                    reply_id = cmd.get("replyId")
                    decision = cmd.get("decision")
                    if not reply_id or decision not in ("approve", "reject"):
                        await websocket.send_json({"type": "contact_reply_result", "ok": False, "error": "missing replyId or decision", "replyId": reply_id or ""})
                    else:
                        try:
                            from vaf.core.contact_reply_pending import get_and_remove
                            payload = get_and_remove(reply_id)
                            if not payload:
                                await websocket.send_json({"type": "contact_reply_result", "ok": False, "error": "expired", "replyId": reply_id})
                            elif decision == "approve":
                                src = payload.get("source")
                                text = payload.get("text") or "[No reply text]"
                                if src == "telegram":
                                    from vaf.core.telegram_reply import send_telegram_reply
                                    send_telegram_reply(str(payload["chat_id_or_jid"]), text)
                                    await websocket.send_json({"type": "contact_reply_result", "ok": True, "decision": "approve", "replyId": reply_id})
                                elif src == "whatsapp":
                                    from vaf.core.whatsapp_reply import send_whatsapp_reply
                                    send_whatsapp_reply(payload.get("username") or "admin", str(payload["chat_id_or_jid"]), text)
                                    await websocket.send_json({"type": "contact_reply_result", "ok": True, "decision": "approve", "replyId": reply_id})
                                else:
                                    await websocket.send_json({"type": "contact_reply_result", "ok": False, "error": "unknown source", "replyId": reply_id})
                            else:
                                await websocket.send_json({"type": "contact_reply_result", "ok": True, "decision": "reject", "replyId": reply_id})
                        except Exception as e:
                            log("WebServer", f"contact_reply_decision failed: {e}")
                            await websocket.send_json({"type": "contact_reply_result", "ok": False, "error": str(e)[:200], "replyId": reply_id})

                elif type == "gate_response":
                    # User clicked Allow Once / Allow Always / Cancel in the trust gate dialog.
                    decision = cmd.get("decision")  # "allow_once" | "allow_always" | "cancel"
                    if decision in ("allow_once", "allow_always", "cancel"):
                        _gate_session = manager.get_session_for_connection(websocket)
                        from vaf.core.web_interface import get_web_interface as _gwi
                        _gwi().resolve_gate(_gate_session or "", decision)

                elif type == "chat":
                    content = cmd.get("content")
                    files = cmd.get("files", [])  # List of file objects with {name, data, mimeType}
                    sidebar_docs_payload = cmd.get("sidebarDocuments") or []  # Document Viewer docs to inject into this turn

                    if content or files:
                        tray_context.register_activity()
                        # Learn from user input
                        if content:
                            get_autosuggest().learn(content)

                        # Separate image files from document/text files
                        image_files = [f for f in files if (f.get("mimeType") or "").startswith("image/")]
                        text_files  = [f for f in files if not (f.get("mimeType") or "").startswith("image/")]

                        # Process text/document files (extract content as before)
                        if text_files:
                            print(f"[WebUI] Processing {len(text_files)} attached file(s)...")
                            file_contents = await process_uploaded_files(text_files)
                            if file_contents:
                                content = content + "\n\n" + file_contents if content else file_contents

                        # Build raw image list for vision providers (base64, no data-URI prefix)
                        attached_images = []
                        if image_files:
                            import base64 as _b64
                            print(f"[WebUI] Passing {len(image_files)} image(s) to vision pipeline...")
                            for img_f in image_files:
                                raw = img_f.get("data", "")
                                if raw.startswith("data:"):
                                    raw = raw.split(",", 1)[1] if "," in raw else raw
                                attached_images.append({
                                    "data": raw,
                                    "mime_type": img_f.get("mimeType", "image/jpeg"),
                                    "name": img_f.get("name", "image"),
                                })
                        
                        # Get session ID: prefer explicit message field, then safe connection session, then fallback.
                        requested_session_id = cmd.get("sessionId")
                        connection_session_id = manager.get_session_for_connection(websocket)
                        session_id = requested_session_id or connection_session_id
                        user_scope_id = manager.get_connection_user(websocket)
                        if (
                            not requested_session_id
                            and isinstance(connection_session_id, str)
                            and connection_session_id.startswith(("telegram_", "discord_", "whatsapp_"))
                        ):
                            # Defensive guard: never route WebUI chat into channel sessions implicitly.
                            session_id = None
                        if not session_id:
                            # Use user-scoped default to prevent crosstalk
                            safe_scope = str(user_scope_id or "default").replace("-", "")[:8]
                            session_id = f"web-default-{safe_scope}"

                        # Ensure this connection is subscribed to the session we're queueing for,
                        # so streaming (agent_message_update) reaches this client (fixes non-admin/LAN).
                        manager.subscribe_to_session(websocket, session_id)

                        # Editor document: prepend to this turn so agent has current editor content (like Document Viewer)
                        editor_doc = cmd.get("editorDocument")
                        if editor_doc and isinstance(editor_doc, dict):
                            name = editor_doc.get("name") or "Document"
                            ed_content = editor_doc.get("content") or ""
                            if ed_content:
                                block = f"--- CURRENT DOCUMENT (Editor): {name} ---\n{ed_content}\n----------------\n\n"
                                content = (block + content) if content else block
                            try:
                                loaded = session_mgr.load(session_id)
                            except FileNotFoundError:
                                loaded = Session(
                                    id=session_id,
                                    name=f"Session {session_id}",
                                    runtime_state={"editor_document": editor_doc},
                                )
                                session_mgr.save(loaded, sync_state=False)
                            else:
                                if not getattr(loaded, "runtime_state", None):
                                    loaded.runtime_state = {}
                                loaded.runtime_state["editor_document"] = editor_doc
                                session_mgr.save(loaded, sync_state=False)
                        else:
                            try:
                                loaded = session_mgr.load(session_id)
                                if getattr(loaded, "runtime_state", None) and "editor_document" in loaded.runtime_state:
                                    loaded.runtime_state["editor_document"] = {}
                                    session_mgr.save(loaded, sync_state=False)
                            except Exception:
                                pass

                        # Code viewer file: store in runtime_state so headless_runner injects it per-turn.
                        # Do NOT prepend to content — that would store the full file in message history.
                        code_viewer_file = cmd.get("codeViewerFile")
                        if code_viewer_file and isinstance(code_viewer_file, dict) and code_viewer_file.get("content"):
                            try:
                                loaded = session_mgr.load(session_id)
                            except FileNotFoundError:
                                loaded = Session(id=session_id, name=f"Session {session_id}", runtime_state={})
                                session_mgr.save(loaded, sync_state=False)
                            if not getattr(loaded, "runtime_state", None):
                                loaded.runtime_state = {}
                            loaded.runtime_state["code_viewer_file"] = code_viewer_file
                            session_mgr.save(loaded, sync_state=False)
                        else:
                            try:
                                loaded = session_mgr.load(session_id)
                                if getattr(loaded, "runtime_state", None) and "code_viewer_file" in loaded.runtime_state:
                                    del loaded.runtime_state["code_viewer_file"]
                                    session_mgr.save(loaded, sync_state=False)
                            except Exception:
                                pass

                        # Editor selections: store for replace_editor_selection tool (start/end per index)
                        editor_selections = cmd.get("editorSelections")
                        if isinstance(editor_selections, list) and editor_selections:
                            try:
                                loaded = session_mgr.load(session_id)
                            except FileNotFoundError:
                                loaded = Session(
                                    id=session_id,
                                    name=f"Session {session_id}",
                                    runtime_state={"editor_selections": editor_selections},
                                )
                                session_mgr.save(loaded, sync_state=False)
                            else:
                                if not getattr(loaded, "runtime_state", None):
                                    loaded.runtime_state = {}
                                loaded.runtime_state["editor_selections"] = editor_selections
                                session_mgr.save(loaded, sync_state=False)
                        else:
                            try:
                                loaded = session_mgr.load(session_id)
                                if getattr(loaded, "runtime_state", None) and "editor_selections" in loaded.runtime_state:
                                    loaded.runtime_state["editor_selections"] = []
                                    session_mgr.save(loaded, sync_state=False)
                            except Exception:
                                pass

                        # Ensure sidebar documents are in session before queueing (so headless has context).
                        # NOTE: the frontend now sends only name+mimeType (no base64) so we skip
                        # process_files_to_sidebar_list unless a payload entry actually has file data.
                        # Documents are already in the session from the earlier set_sidebar_documents WS call.
                        if sidebar_docs_payload:
                            try:
                                has_data = any(doc.get("data") for doc in sidebar_docs_payload)
                                if has_data:
                                    contents = await process_files_to_sidebar_list(sidebar_docs_payload)
                                    if contents:
                                        _slim = [{k: v for k, v in doc.items() if k != "data"} for doc in contents]
                                        try:
                                            loaded = session_mgr.load(session_id)
                                        except FileNotFoundError:
                                            loaded = Session(
                                                id=session_id,
                                                name=f"Session {session_id}",
                                                runtime_state={"sidebar_documents": _slim},
                                            )
                                            session_mgr.save(loaded, sync_state=False)
                                        else:
                                            if not getattr(loaded, "runtime_state", None):
                                                loaded.runtime_state = {}
                                            loaded.runtime_state["sidebar_documents"] = _slim
                                            session_mgr.save(loaded, sync_state=False)
                                        log("WebServer", f"Injected {len(contents)} sidebar doc(s) for session {session_id} before chat")
                                        if bool(Config.get("attachment_rag_enabled", False)):
                                            try:
                                                from vaf.memory.attachment_rag import index_session_attachments_async
                                                await index_session_attachments_async(
                                                    session_id=session_id,
                                                    user_scope_id=user_scope_id,
                                                    documents=contents,
                                                )
                                            except Exception as e:
                                                log("WebServer", f"attachment index (chat inject) failed: {e}")
                                # else: no file data — session already has documents from set_sidebar_documents
                            except Exception as e:
                                log("WebServer", f"sidebar_documents on chat failed: {e}")

                        # Use TaskQueue for serialized execution
                        from vaf.core.task_queue import TaskQueue
                        tq = TaskQueue()
                        # user_scope_id is required for correct RAG scope (Auto-Recall and memory_save)
                        user_scope_id = manager.get_connection_user(websocket)
                        username = manager.get_connection_username(websocket)
                        user_role = manager.get_connection_user_role(websocket)
                        metadata = {}
                        if user_scope_id:
                            metadata["user_scope_id"] = user_scope_id
                        if username:
                            metadata["username"] = username
                        if user_role:
                            metadata["role"] = user_role
                        metadata["origin_channel"] = "web"
                        metadata["enqueue_session_id"] = str(session_id)
                        if user_scope_id:
                            metadata["enqueue_user_scope_id"] = str(user_scope_id)
                        metadata["task_class"] = "interactive"
                        log("WebServer", f"Chat message from user_scope_id={user_scope_id}, username={username}")
                        # Mark user activity for thinking mode (idle detection)
                        try:
                            from vaf.core.last_interaction import update_last_interaction
                            update_last_interaction(
                                user_scope_id=user_scope_id,
                                source="web",
                                preview=(content or "")[:80],
                            )
                        except Exception:
                            pass
                        # If thinking mode sent a question via the Web UI and is waiting for
                        # this user's reply, deliver the reply to the thinking mode now.
                        try:
                            from vaf.core.thinking_mode import get_waiting_for_reply, clear_waiting_for_reply
                            if get_waiting_for_reply(user_scope_id):
                                clear_waiting_for_reply(user_scope_id, content)
                        except Exception:
                            pass
                        if attached_images:
                            metadata["images"] = attached_images
                        tq.add(session_id=session_id, input_text=content, source="web", metadata=metadata)
                        try:
                            if is_debug_logging_enabled():
                                from datetime import datetime as _dt
                                qpath = get_dated_log_path("queue", "log")
                                qpath.parent.mkdir(parents=True, exist_ok=True)
                                qsize = tq.get_queue_size()
                                with open(qpath, "a", encoding="utf-8") as f:
                                    f.write(f"{_dt.now().isoformat()} QUEUE_ADD session_id={session_id} preview={repr((content or '')[:60])} queue_size_after={qsize}\n")
                        except Exception:
                            pass
                        # Ack to console only; do not push to chat UI (avoids duplicating user message as system "Queued input...")
                        file_info = f" [{len(files)} file(s)]" if files else ""
                        print(f"[WebUI] Queued input{file_info} for session {session_id}: {content[:50]}...")

                elif type == "set_sidebar_documents":
                    session_id = cmd.get("sessionId") or manager.get_session_for_connection(websocket)
                    user_scope_id = manager.get_connection_user(websocket)
                    if not session_id:
                        # Use user-scoped default to prevent crosstalk
                        safe_scope = str(user_scope_id or "default").replace("-", "")[:8]
                        session_id = f"web-default-{safe_scope}"
                    documents = cmd.get("documents") or []
                    from vaf.core.log_helper import log_attachment
                    log_attachment("WS_RECEIVED", session=session_id, doc_count=len(documents),
                        doc_names=[d.get("name","?") for d in documents[:5]])
                    contents = []
                    try:
                        if not documents:
                            log_attachment("CLEAR_PATH", session=session_id)
                            loaded = session_mgr.load(session_id)
                            if not getattr(loaded, "runtime_state", None):
                                loaded.runtime_state = {}
                            loaded.runtime_state["sidebar_documents"] = []
                            session_mgr.save(loaded, sync_state=False)
                            if bool(Config.get("attachment_rag_enabled", False)):
                                try:
                                    from vaf.memory.attachment_rag import clear_session_attachments_async
                                    await clear_session_attachments_async(session_id=session_id, user_scope_id=user_scope_id)
                                except Exception as e:
                                    log("WebServer", f"attachment clear failed: {e}")
                            await websocket.send_json({
                                "type": "sidebar_documents_set",
                                "contents": [],
                                "sessionId": session_id
                            })
                        else:
                            log_attachment("PROCESS_START", session=session_id, doc_count=len(documents))
                            contents = await process_files_to_sidebar_list(documents)
                            log_attachment("PROCESS_DONE", session=session_id, results=len(contents),
                                content_lens=[len(c.get("content","")) for c in contents],
                                content_types=[
                                    "ERROR" if "[ERROR]" in (c.get("content",""))[:80]
                                    else "SCANNED" if "[Scanned PDF" in (c.get("content",""))[:200]
                                    else "OK"
                                    for c in contents
                                ])
                            for _dc in contents:
                                _dc_name = _dc.get("name", "?")
                                _dc_content = _dc.get("content", "")
                                log("WebServer", f"set_sidebar_documents: doc={_dc_name!r} content_len={len(_dc_content)} preview={repr(_dc_content[:120])}")
                            try:
                                loaded = session_mgr.load(session_id)
                            except Exception as _load_err:
                                # Corrupted or 0-byte session file (pre-atomic-write crash).
                                # Create a minimal in-memory session so sidebar_documents can be saved.
                                log_attachment("SESSION_LOAD_FAILED", session=session_id, error=str(_load_err))
                                log("WebServer", f"set_sidebar_documents: session load failed ({_load_err!r}), creating minimal session for {session_id}")
                                loaded = Session(id=session_id, name=f"Session {session_id}")
                                loaded.metadata["user_scope_id"] = str(user_scope_id or "")
                            if not getattr(loaded, "runtime_state", None):
                                loaded.runtime_state = {}
                            # Strip base64 data before persisting to session runtime_state.
                            # The 'data' field can be 10-25 MB for large PDFs, bloating the
                            # session JSON file and slowing every subsequent session read/write.
                            # The frontend already received 'data' in the WS response below;
                            # the headless_runner only needs 'content' (extracted text).
                            def _sanitize_str(s):
                                """Strip lone Unicode surrogates (lone surrogates from PDF emoji)  
                                that break UTF-8 JSON serialization in session.save()."""
                                if not isinstance(s, str):
                                    return s
                                return s.encode("utf-8", errors="replace").decode("utf-8")
                            _slim = [
                                {k: (_sanitize_str(v) if k == "content" else v)
                                 for k, v in doc.items() if k != "data"}
                                for doc in contents
                            ]
                            loaded.runtime_state["sidebar_documents"] = _slim
                            session_mgr.save(loaded, sync_state=False)
                            log_attachment("SAVE_OK", session=session_id, docs=len(_slim),
                                names=[d.get("name","?") for d in _slim])
                            if bool(Config.get("attachment_rag_enabled", False)):
                                await _notify_attachment_index(manager, session_id, "attachment_indexing", count=len(contents))
                                try:
                                    from vaf.memory.attachment_rag import index_session_attachments_async
                                    await index_session_attachments_async(
                                        session_id=session_id,
                                        user_scope_id=user_scope_id,
                                        documents=contents,
                                    )
                                    await _notify_attachment_index(manager, session_id, "attachment_indexed", count=len(contents))
                                except Exception as e:
                                    log("WebServer", f"attachment index failed: {e}")
                                    await _notify_attachment_index(manager, session_id, "attachment_index_error")
                            await websocket.send_json({
                                "type": "sidebar_documents_set",
                                "contents": contents,
                                "sessionId": session_id
                            })
                    except FileNotFoundError:
                        # Session file missing — still do RAG ops but do NOT write a new
                        # empty session to disk. Creating Session(id=session_id, messages=[])
                        # and saving it would overwrite a valid session that was being written
                        # concurrently, destroying the user's chat history.
                        # The session is always created by the WS connection handler; sidebar
                        # state is transient and will be re-synced on the next attachment.
                        if not documents:
                            if bool(Config.get("attachment_rag_enabled", False)):
                                try:
                                    from vaf.memory.attachment_rag import clear_session_attachments_async
                                    await clear_session_attachments_async(session_id=session_id, user_scope_id=user_scope_id)
                                except Exception as e:
                                    log("WebServer", f"attachment clear failed (new session): {e}")
                        else:
                            if bool(Config.get("attachment_rag_enabled", False)):
                                await _notify_attachment_index(manager, session_id, "attachment_indexing", count=len(contents))
                                try:
                                    from vaf.memory.attachment_rag import index_session_attachments_async
                                    await index_session_attachments_async(
                                        session_id=session_id,
                                        user_scope_id=user_scope_id,
                                        documents=contents,
                                    )
                                    await _notify_attachment_index(manager, session_id, "attachment_indexed", count=len(contents))
                                except Exception as e:
                                    log("WebServer", f"attachment index failed (new session): {e}")
                                    await _notify_attachment_index(manager, session_id, "attachment_index_error")
                        await websocket.send_json({
                            "type": "sidebar_documents_set",
                            "contents": contents,
                            "sessionId": session_id
                        })
                    except Exception as e:
                        import traceback as _tb
                        _tb_str = _tb.format_exc()
                        log("WebServer", f"set_sidebar_documents FAILED: {e}\n{_tb_str}")
                        log_attachment("SAVE_FAILED", session=session_id, error=str(e),
                            tb=_tb_str[-400:])
                        # Use whatever contents were computed before the failure.
                        # Sending [] here would wipe the user's attached documents from
                        # the viewer even when file extraction succeeded but session save
                        # raised (e.g. stale corrupt JSON from before the atomic-write fix).
                        await websocket.send_json({
                            "type": "sidebar_documents_set",
                            "contents": contents,
                            "sessionId": session_id,
                            "error": str(e)
                        })

                elif type == "get_tools":
                    # Return list of available tools, filtered by the requesting
                    # user's role and custom-tool visibility permissions.
                    try:
                        from vaf.core.config import get_local_admin_scope_id
                        from vaf.core.custom_tools_registry import (
                            get_all_custom_tool_names,
                            get_visible_tool_names_for_user,
                            get_tool_manifest_entry,
                        )

                        _gt_scope = manager.get_connection_user(websocket) if manager else None
                        _gt_role  = manager.get_connection_user_role(websocket) if manager else None
                        _gt_local_admin = get_local_admin_scope_id()
                        _gt_is_admin = (
                            _gt_role == "admin"
                            or (
                                _gt_scope is not None
                                and str(_gt_scope) == str(_gt_local_admin)
                            )
                        )

                        # Admins pass None so get_visible_tool_names_for_user returns ALL
                        _gt_filter_scope = None if _gt_is_admin else _gt_scope

                        all_custom_names   = set(get_all_custom_tool_names())
                        visible_custom     = set(get_visible_tool_names_for_user(_gt_filter_scope))

                        agent = manager.agent_instance
                        if agent and hasattr(agent, "tools"):
                            tools_list = []
                            for name, tool in agent.tools.items():
                                is_custom = name in all_custom_names
                                # Non-admins: skip custom tools they can't see
                                if is_custom and not _gt_is_admin and name not in visible_custom:
                                    continue
                                entry = {
                                    "name":        name,
                                    "description": getattr(tool, "description", "No description"),
                                    "category":    getattr(tool, "category", "general"),
                                    # Frontend uses these two flags to render management controls
                                    "is_custom":   is_custom,
                                    "can_manage":  _gt_is_admin,
                                }
                                if is_custom:
                                    meta = get_tool_manifest_entry(name)
                                    if meta:
                                        entry["shared_with"]  = meta.get("shared_with", ["*"])
                                        entry["created_by"]   = meta.get("created_by", "")
                                        entry["updated_at"]   = meta.get("updated_at", "")
                                tools_list.append(entry)

                            _attach_learned_states(tools_list)
                            manager.tools_cache = tools_list
                            await websocket.send_json({
                                "type": "tools_list",
                                "tools": tools_list,
                            })
                        elif manager.tools_cache:
                            await websocket.send_json({
                                "type": "tools_list",
                                "tools": manager.tools_cache,
                            })
                        else:
                            await websocket.send_json({
                                "type": "tools_list",
                                "tools": _scan_tool_modules(),
                            })
                    except Exception as e:
                        await websocket.send_json({
                            "type": "tools_list",
                            "tools": [],
                            "error": str(e),
                        })

                # ── Custom Tool Management (admin-only) ───────────────────────────
                # All four handlers share the same admin-check pattern used
                # throughout the WS loop (see line ~2192 for the reference pattern).

                elif type == "create_custom_tool":
                    # Upload or write a new custom tool from the WebUI editor.
                    # Payload: { name: str, code: str, shared_with: list[str] }
                    try:
                        from vaf.core.config import get_local_admin_scope_id
                        from vaf.core import custom_tools_registry as _ctr

                        _ct_scope = manager.get_connection_user(websocket) if manager else None
                        _ct_role  = manager.get_connection_user_role(websocket) if manager else None
                        _ct_local_admin = get_local_admin_scope_id()
                        _ct_is_admin = (
                            _ct_role == "admin"
                            or (_ct_scope is not None and str(_ct_scope) == str(_ct_local_admin))
                        )
                        if not _ct_is_admin:
                            await websocket.send_json({
                                "type": "custom_tool_error",
                                "error": "Admin permission required to create tools.",
                            })
                        else:
                            _ct_name       = (data.get("name") or "").strip()
                            _ct_code       = data.get("code", "")
                            _ct_shared     = data.get("shared_with", ["*"])
                            _ct_username   = manager.get_connection_username(websocket) or "admin"

                            # Basic name validation: snake_case identifiers only
                            import re as _re
                            if not _re.match(r'^[a-z][a-z0-9_]*$', _ct_name):
                                raise ValueError(
                                    "Tool name must be lowercase snake_case (e.g. my_tool)."
                                )

                            # Write file + validate BaseTool subclass, then register
                            _ct_filename = f"{_ct_name}.py"
                            _ctr.save_tool_file(_ct_filename, _ct_code)
                            # register_tool also validates via load_custom_tool_class
                            _ctr.register_tool(
                                tool_name=_ct_name,
                                filename=_ct_filename,
                                created_by=_ct_username,
                                shared_with=_ct_shared,
                            )

                            # Hot-reload so the live agent immediately has the new tool
                            agent = manager.agent_instance
                            if agent and hasattr(agent, "reload_custom_tools"):
                                agent.reload_custom_tools()

                            await websocket.send_json({
                                "type": "custom_tool_created",
                                "name": _ct_name,
                            })
                            # Broadcast updated tool list to all connected clients
                            # so other open tabs / admin panels refresh automatically.
                            await _broadcast_tools_update(manager)

                    except Exception as e:
                        await websocket.send_json({
                            "type": "custom_tool_error",
                            "error": str(e),
                        })

                elif type == "update_custom_tool":
                    # Edit the source code of an existing custom tool.
                    # Payload: { name: str, code: str }
                    try:
                        from vaf.core.config import get_local_admin_scope_id
                        from vaf.core import custom_tools_registry as _ctr

                        _ut_scope = manager.get_connection_user(websocket) if manager else None
                        _ut_role  = manager.get_connection_user_role(websocket) if manager else None
                        _ut_is_admin = (
                            _ut_role == "admin"
                            or (_ut_scope is not None and str(_ut_scope) == str(get_local_admin_scope_id()))
                        )
                        if not _ut_is_admin:
                            await websocket.send_json({
                                "type": "custom_tool_error",
                                "error": "Admin permission required to edit tools.",
                            })
                        else:
                            _ut_name     = (data.get("name") or "").strip()
                            _ut_code     = data.get("code", "")
                            _ut_username = manager.get_connection_username(websocket) or "admin"

                            # update_tool_source validates BaseTool presence before overwriting
                            _ctr.update_tool_source(_ut_name, _ut_code, _ut_username)

                            agent = manager.agent_instance
                            if agent and hasattr(agent, "reload_custom_tools"):
                                agent.reload_custom_tools()

                            await websocket.send_json({
                                "type": "custom_tool_updated",
                                "name": _ut_name,
                            })
                            await _broadcast_tools_update(manager)

                    except Exception as e:
                        await websocket.send_json({
                            "type": "custom_tool_error",
                            "error": str(e),
                        })

                elif type == "delete_custom_tool":
                    # Remove a custom tool permanently.
                    # Payload: { name: str }
                    try:
                        from vaf.core.config import get_local_admin_scope_id
                        from vaf.core import custom_tools_registry as _ctr

                        _dt_scope = manager.get_connection_user(websocket) if manager else None
                        _dt_role  = manager.get_connection_user_role(websocket) if manager else None
                        _dt_is_admin = (
                            _dt_role == "admin"
                            or (_dt_scope is not None and str(_dt_scope) == str(get_local_admin_scope_id()))
                        )
                        if not _dt_is_admin:
                            await websocket.send_json({
                                "type": "custom_tool_error",
                                "error": "Admin permission required to delete tools.",
                            })
                        else:
                            _dt_name = (data.get("name") or "").strip()
                            _ctr.delete_tool(_dt_name)

                            # Remove from the live agent immediately
                            agent = manager.agent_instance
                            if agent and hasattr(agent, "reload_custom_tools"):
                                agent.reload_custom_tools()

                            await websocket.send_json({
                                "type": "custom_tool_deleted",
                                "name": _dt_name,
                            })
                            await _broadcast_tools_update(manager)

                    except Exception as e:
                        await websocket.send_json({
                            "type": "custom_tool_error",
                            "error": str(e),
                        })

                elif type == "get_mcp_servers":
                    # List configured MCP servers + live connection status for the Settings UI.
                    try:
                        agent = manager.agent_instance if manager else None
                        await websocket.send_json({"type": "mcp_servers", "servers": _mcp_servers_payload(agent)})
                    except Exception as e:
                        await websocket.send_json({"type": "mcp_server_error", "error": str(e)})

                elif type in ("create_mcp_server", "update_mcp_server"):
                    # Add or edit an MCP server in mcp_servers.json, then hot-reload.
                    # Payload: { name, command, transport?, url?, enabled?, permission_level? }
                    try:
                        from vaf.core.config import get_local_admin_scope_id
                        from vaf.core.mcp_registry import load_mcp_manifest, save_mcp_manifest

                        _ms_scope = manager.get_connection_user(websocket) if manager else None
                        _ms_role  = manager.get_connection_user_role(websocket) if manager else None
                        _ms_admin = (
                            _ms_role == "admin"
                            or (_ms_scope is not None and str(_ms_scope) == str(get_local_admin_scope_id()))
                        )
                        if not _ms_admin:
                            await websocket.send_json({"type": "mcp_server_error", "error": "Admin permission required to manage MCP servers."})
                        else:
                            import re as _re
                            _ms_name = (data.get("name") or "").strip()
                            if not _re.match(r'^[A-Za-z][A-Za-z0-9_-]*$', _ms_name):
                                raise ValueError("Server name must start with a letter and use only letters, digits, _ or -.")
                            _ms_transport = (data.get("transport") or "stdio").strip()
                            _ms_cmd = (data.get("command") or "").strip()
                            if _ms_transport == "stdio" and not _ms_cmd:
                                raise ValueError("A command is required for stdio transport.")
                            _ms_perm = (data.get("permission_level") or "write").strip().lower()
                            if _ms_perm not in ("read", "write", "dangerous"):
                                _ms_perm = "write"
                            _manifest = load_mcp_manifest() or {}
                            _srv = _manifest.get("servers")
                            if not isinstance(_srv, dict):
                                _srv = {}
                            _ms_env = data.get("env")
                            if not isinstance(_ms_env, dict):
                                _ms_env = {}
                            _srv[_ms_name] = {
                                "command": _ms_cmd,
                                "transport": _ms_transport,
                                "url": (data.get("url") or "").strip(),
                                "enabled": bool(data.get("enabled", True)),
                                "permission_level": _ms_perm,
                                "env": {str(k): str(v) for k, v in _ms_env.items()},
                            }
                            _manifest["servers"] = _srv
                            save_mcp_manifest(_manifest)
                            agent = manager.agent_instance if manager else None
                            if agent and hasattr(agent, "reload_mcp_tools"):
                                agent.reload_mcp_tools()
                            # Return the refreshed list with the reply so the UI updates without a refetch.
                            await websocket.send_json({"type": "mcp_server_saved", "name": _ms_name, "servers": _mcp_servers_payload(agent)})
                            await _broadcast_tools_update(manager)
                    except Exception as e:
                        await websocket.send_json({"type": "mcp_server_error", "error": str(e)})

                elif type == "delete_mcp_server":
                    # Remove an MCP server from mcp_servers.json, then hot-reload. Payload: { name }
                    try:
                        from vaf.core.config import get_local_admin_scope_id
                        from vaf.core.mcp_registry import load_mcp_manifest, save_mcp_manifest

                        _md_scope = manager.get_connection_user(websocket) if manager else None
                        _md_role  = manager.get_connection_user_role(websocket) if manager else None
                        _md_admin = (
                            _md_role == "admin"
                            or (_md_scope is not None and str(_md_scope) == str(get_local_admin_scope_id()))
                        )
                        if not _md_admin:
                            await websocket.send_json({"type": "mcp_server_error", "error": "Admin permission required to manage MCP servers."})
                        else:
                            _md_name = (data.get("name") or "").strip()
                            _manifest = load_mcp_manifest() or {}
                            _srv = _manifest.get("servers")
                            if isinstance(_srv, dict) and _md_name in _srv:
                                del _srv[_md_name]
                                _manifest["servers"] = _srv
                                save_mcp_manifest(_manifest)
                            agent = manager.agent_instance if manager else None
                            if agent and hasattr(agent, "reload_mcp_tools"):
                                agent.reload_mcp_tools()
                            # Return the refreshed list with the reply so the UI updates without a refetch.
                            await websocket.send_json({"type": "mcp_server_deleted", "name": _md_name, "servers": _mcp_servers_payload(agent)})
                            await _broadcast_tools_update(manager)
                    except Exception as e:
                        await websocket.send_json({"type": "mcp_server_error", "error": str(e)})

                elif type == "test_mcp_server":
                    # Probe a server config (without saving) so the admin can validate it in the
                    # editor. Payload: { command, transport?, url? }. Reply: mcp_server_test_result.
                    try:
                        from vaf.core.config import get_local_admin_scope_id, Config as _CfgMcp
                        from vaf.core.mcp_registry import probe_mcp_server

                        _tm_scope = manager.get_connection_user(websocket) if manager else None
                        _tm_role  = manager.get_connection_user_role(websocket) if manager else None
                        _tm_admin = (
                            _tm_role == "admin"
                            or (_tm_scope is not None and str(_tm_scope) == str(get_local_admin_scope_id()))
                        )
                        if not _tm_admin:
                            await websocket.send_json({"type": "mcp_server_test_result", "connected": False, "tool_count": 0, "tools": [], "error": "Admin permission required."})
                        else:
                            _tm_env = data.get("env") if isinstance(data.get("env"), dict) else {}
                            _tm_cfg = {
                                "command": (data.get("command") or "").strip(),
                                "transport": (data.get("transport") or "stdio").strip(),
                                "url": (data.get("url") or "").strip(),
                                "env": {str(k): str(v) for k, v in _tm_env.items()},
                            }
                            _tm_timeout = float(_CfgMcp.get("mcp_discovery_timeout_seconds", 5) or 5)
                            _tm_res = probe_mcp_server(_tm_cfg, _tm_timeout)
                            await websocket.send_json({"type": "mcp_server_test_result", **_tm_res})
                    except Exception as e:
                        await websocket.send_json({"type": "mcp_server_test_result", "connected": False, "tool_count": 0, "tools": [], "error": str(e)})

                elif type == "update_custom_tool_permissions":
                    # Change which users can see a custom tool.
                    # Payload: { name: str, shared_with: list[str] }
                    #   shared_with: ["*"] → all users
                    #   shared_with: []    → admin only
                    #   shared_with: ["<scope_id>", ...] → specific users
                    try:
                        from vaf.core.config import get_local_admin_scope_id
                        from vaf.core import custom_tools_registry as _ctr

                        _pp_scope = manager.get_connection_user(websocket) if manager else None
                        _pp_role  = manager.get_connection_user_role(websocket) if manager else None
                        _pp_is_admin = (
                            _pp_role == "admin"
                            or (_pp_scope is not None and str(_pp_scope) == str(get_local_admin_scope_id()))
                        )
                        if not _pp_is_admin:
                            await websocket.send_json({
                                "type": "custom_tool_error",
                                "error": "Admin permission required to change tool permissions.",
                            })
                        else:
                            _pp_name       = (data.get("name") or "").strip()
                            _pp_shared     = data.get("shared_with", ["*"])
                            _ctr.update_tool_permissions(_pp_name, _pp_shared)

                            await websocket.send_json({
                                "type": "custom_tool_permissions_updated",
                                "name":        _pp_name,
                                "shared_with": _pp_shared,
                            })
                            # Broadcast so other clients refresh their tool lists
                            await _broadcast_tools_update(manager)

                    except Exception as e:
                        await websocket.send_json({
                            "type": "custom_tool_error",
                            "error": str(e),
                        })

                elif type == "get_custom_tool_source":
                    # Return the Python source code of a custom tool for the editor.
                    # Payload: { name: str }
                    # Admin-only — non-admins must not be able to exfiltrate source.
                    try:
                        from vaf.core.config import get_local_admin_scope_id
                        from vaf.core import custom_tools_registry as _ctr

                        _gs_scope = manager.get_connection_user(websocket) if manager else None
                        _gs_role  = manager.get_connection_user_role(websocket) if manager else None
                        _gs_is_admin = (
                            _gs_role == "admin"
                            or (_gs_scope is not None and str(_gs_scope) == str(get_local_admin_scope_id()))
                        )
                        if not _gs_is_admin:
                            await websocket.send_json({
                                "type": "custom_tool_error",
                                "error": "Admin permission required to view tool source.",
                            })
                        else:
                            _gs_name   = (data.get("name") or "").strip()
                            _gs_source = _ctr.get_tool_source(_gs_name)
                            await websocket.send_json({
                                "type": "custom_tool_source",
                                "name":   _gs_name,
                                "source": _gs_source or "",
                            })

                    except Exception as e:
                        await websocket.send_json({
                            "type": "custom_tool_error",
                            "error": str(e),
                        })
                
                elif type == "get_custom_tool_users":
                    # Return non-admin users for the share picker in CustomToolEditor.
                    # Admin-only: non-admins have no reason to query the user list.
                    try:
                        from vaf.core.config import get_local_admin_scope_id

                        _gu_scope = manager.get_connection_user(websocket) if manager else None
                        _gu_role  = manager.get_connection_user_role(websocket) if manager else None
                        _gu_is_admin = (
                            _gu_role == "admin"
                            or (_gu_scope is not None and str(_gu_scope) == str(get_local_admin_scope_id()))
                        )
                        if not _gu_is_admin:
                            await websocket.send_json({
                                "type": "custom_tool_error",
                                "error": "Admin permission required.",
                            })
                        else:
                            # Reuse the existing /api/users logic by querying the DB directly
                            try:
                                from vaf.auth.database import get_auth_db
                                from vaf.auth.models import LocalUser
                                from sqlalchemy import select as _sa_select
                                async with get_auth_db() as _db:
                                    _result = await _db.execute(
                                        _sa_select(LocalUser).where(LocalUser.is_active == True)
                                    )
                                    _users = _result.scalars().all()
                                    _user_list = [
                                        {
                                            "id":            str(u.id),
                                            "username":      u.username,
                                            "user_scope_id": str(u.user_scope_id),
                                            "role":          u.role,
                                        }
                                        for u in _users
                                        if u.role != "admin"  # admins always see everything; no point including them
                                    ]
                            except Exception:
                                _user_list = []
                            await websocket.send_json({
                                "type":  "custom_tool_users",
                                "users": _user_list,
                            })
                    except Exception as e:
                        await websocket.send_json({
                            "type": "custom_tool_error",
                            "error": str(e),
                        })

                elif type == "get_workflows":
                    # Return list of available workflow templates
                    try:
                        from vaf.workflows.templates import list_templates
                        workflows = list_templates()
                        await websocket.send_json({
                            "type": "workflows_list",
                            "workflows": workflows
                        })
                    except Exception as e:
                        await websocket.send_json({
                            "type": "workflows_list",
                            "workflows": [],
                            "error": str(e)
                        })

                elif type == "create_workflow":
                    from vaf.core.config import get_local_admin_scope_id
                    _wf_scope       = manager.get_connection_user(websocket)
                    _wf_role        = manager.get_connection_user_role(websocket)
                    _wf_local_admin = get_local_admin_scope_id()
                    _wf_is_admin    = (
                        _wf_role == "admin"
                        or (_wf_scope is not None and str(_wf_scope) == str(_wf_local_admin))
                    )
                    if not _wf_is_admin:
                        await websocket.send_json({
                            "type": "workflow_error",
                            "error": "Admin permission required to create workflows.",
                        })
                    else:
                        try:
                            import json as _json_wf
                            import os as _os_wf
                            import re as _re_wf
                            from vaf.workflows.templates import reload_workflows, list_templates

                            _wf_id       = str(cmd.get("workflow_id") or "").strip()
                            _wf_name     = str(cmd.get("name") or "").strip()
                            _wf_desc     = str(cmd.get("description") or "").strip()
                            _wf_triggers = [str(t) for t in (cmd.get("triggers") or []) if str(t).strip()]
                            _wf_steps    = cmd.get("steps") or []

                            if not _re_wf.match(r'^[a-z][a-z0-9_]*$', _wf_id):
                                raise ValueError(f"workflow_id must be lowercase snake_case, got '{_wf_id}'")
                            if not _wf_name:
                                raise ValueError("name is required")
                            if not _wf_steps:
                                raise ValueError("at least one step is required")

                            _user_wf_dir = _os_wf.path.expanduser("~/.vaf/workflows")
                            _os_wf.makedirs(_user_wf_dir, exist_ok=True)
                            _wf_path = _os_wf.path.join(_user_wf_dir, f"{_wf_id}.py")

                            if _os_wf.path.exists(_wf_path):
                                raise ValueError(
                                    f"Workflow '{_wf_id}' already exists. "
                                    "Use update_workflow to modify it."
                                )

                            _wf_dict = {
                                "name": _wf_name,
                                "description": _wf_desc,
                                "triggers": _wf_triggers,
                                "steps": _wf_steps,
                            }
                            _wf_content = (
                                f"# User-created workflow: {_wf_name}\n"
                                f"WORKFLOW = {_json_wf.dumps(_wf_dict, indent=4, ensure_ascii=False)}\n"
                            )
                            _wf_tmp = _wf_path + ".tmp"
                            with open(_wf_tmp, "w", encoding="utf-8") as _f:
                                _f.write(_wf_content)
                            _os_wf.replace(_wf_tmp, _wf_path)

                            reload_workflows()
                            await websocket.send_json({"type": "workflow_created", "workflow_id": _wf_id})
                            await websocket.send_json({"type": "workflows_list", "workflows": list_templates()})
                        except Exception as e:
                            await websocket.send_json({"type": "workflow_error", "error": str(e)})

                elif type == "update_workflow":
                    from vaf.core.config import get_local_admin_scope_id
                    _wf_scope       = manager.get_connection_user(websocket)
                    _wf_role        = manager.get_connection_user_role(websocket)
                    _wf_local_admin = get_local_admin_scope_id()
                    _wf_is_admin    = (
                        _wf_role == "admin"
                        or (_wf_scope is not None and str(_wf_scope) == str(_wf_local_admin))
                    )
                    if not _wf_is_admin:
                        await websocket.send_json({
                            "type": "workflow_error",
                            "error": "Admin permission required to update workflows.",
                        })
                    else:
                        try:
                            import json as _json_wf
                            import os as _os_wf
                            from vaf.workflows.templates import reload_workflows, list_templates

                            _wf_id       = str(cmd.get("workflow_id") or "").strip()
                            _wf_name     = str(cmd.get("name") or "").strip()
                            _wf_desc     = str(cmd.get("description") or "").strip()
                            _wf_triggers = [str(t) for t in (cmd.get("triggers") or []) if str(t).strip()]
                            _wf_steps    = cmd.get("steps") or []

                            if not _wf_name:
                                raise ValueError("name is required")
                            if not _wf_steps:
                                raise ValueError("at least one step is required")

                            _user_wf_dir = _os_wf.path.expanduser("~/.vaf/workflows")
                            _wf_path = _os_wf.path.join(_user_wf_dir, f"{_wf_id}.py")

                            if not _os_wf.path.exists(_wf_path):
                                raise ValueError(
                                    f"Workflow '{_wf_id}' is a built-in workflow and cannot be modified."
                                )

                            _wf_dict = {
                                "name": _wf_name,
                                "description": _wf_desc,
                                "triggers": _wf_triggers,
                                "steps": _wf_steps,
                            }
                            _wf_content = (
                                f"# User-created workflow: {_wf_name}\n"
                                f"WORKFLOW = {_json_wf.dumps(_wf_dict, indent=4, ensure_ascii=False)}\n"
                            )
                            _wf_tmp = _wf_path + ".tmp"
                            with open(_wf_tmp, "w", encoding="utf-8") as _f:
                                _f.write(_wf_content)
                            _os_wf.replace(_wf_tmp, _wf_path)

                            reload_workflows()
                            await websocket.send_json({"type": "workflow_updated", "workflow_id": _wf_id})
                            await websocket.send_json({"type": "workflows_list", "workflows": list_templates()})
                        except Exception as e:
                            await websocket.send_json({"type": "workflow_error", "error": str(e)})

                elif type == "delete_workflow":
                    from vaf.core.config import get_local_admin_scope_id
                    _wf_scope       = manager.get_connection_user(websocket)
                    _wf_role        = manager.get_connection_user_role(websocket)
                    _wf_local_admin = get_local_admin_scope_id()
                    _wf_is_admin    = (
                        _wf_role == "admin"
                        or (_wf_scope is not None and str(_wf_scope) == str(_wf_local_admin))
                    )
                    if not _wf_is_admin:
                        await websocket.send_json({
                            "type": "workflow_error",
                            "error": "Admin permission required to delete workflows.",
                        })
                    else:
                        try:
                            import os as _os_wf
                            from vaf.workflows.templates import reload_workflows, list_templates

                            _wf_id = str(cmd.get("workflow_id") or "").strip()
                            _user_wf_dir = _os_wf.path.expanduser("~/.vaf/workflows")
                            _wf_path = _os_wf.path.join(_user_wf_dir, f"{_wf_id}.py")

                            if not _os_wf.path.exists(_wf_path):
                                raise ValueError(
                                    f"Workflow '{_wf_id}' is a built-in workflow and cannot be deleted."
                                )

                            _os_wf.remove(_wf_path)
                            reload_workflows()
                            await websocket.send_json({"type": "workflow_deleted", "workflow_id": _wf_id})
                            await websocket.send_json({"type": "workflows_list", "workflows": list_templates()})
                        except Exception as e:
                            await websocket.send_json({"type": "workflow_error", "error": str(e)})

                elif type == "get_trusted_sources":
                    try:
                        payload = _get_trusted_sources_for_ui()
                        await websocket.send_json({
                            "type": "trusted_sources_list",
                            **payload
                        })
                    except Exception as e:
                        await websocket.send_json({
                            "type": "trusted_sources_list",
                            "categories": [],
                            "error": str(e)
                        })

                elif type == "add_trusted_source":
                    try:
                        from urllib.parse import urlparse
                        category_id = (cmd.get("category_id") or "").strip() or "custom"
                        name = (cmd.get("name") or "").strip()
                        url = (cmd.get("url") or "").strip()
                        if not name or not url:
                            await websocket.send_json({"type": "trusted_source_updated", "ok": False, "error": "name and url required"})
                        else:
                            try:
                                parsed = urlparse(url if "://" in url else "https://" + url)
                                netloc = parsed.netloc or parsed.path.split("/")[0]
                                domain = netloc.lower().lstrip("www.")
                            except Exception:
                                domain = url.replace("https://", "").replace("http://", "").split("/")[0].lower().lstrip("www.")
                            custom = Config.get("trusted_sources_custom") or {}
                            if category_id not in custom:
                                custom[category_id] = []
                            custom[category_id].append({"name": name, "url": url, "domains": [domain], "trust_score": 6})
                            cfg = Config.load()
                            cfg["trusted_sources_custom"] = custom
                            Config.save(cfg)
                            payload = _get_trusted_sources_for_ui()
                            await websocket.send_json({"type": "trusted_source_updated", "ok": True, "categories": payload.get("categories", [])})
                    except Exception as e:
                        await websocket.send_json({"type": "trusted_source_updated", "ok": False, "error": str(e)})

                elif type == "create_trusted_category":
                    try:
                        name = (cmd.get("name") or "").strip()
                        if not name:
                            await websocket.send_json({"type": "trusted_source_updated", "ok": False, "error": "Category name required"})
                        else:
                            payload = _get_trusted_sources_for_ui()
                            existing_names = { (c.get("name") or c.get("id") or "").strip() for c in payload.get("categories", []) }
                            if name in existing_names:
                                await websocket.send_json({"type": "trusted_source_updated", "ok": False, "error": "Category name already exists"})
                            else:
                                custom = Config.get("trusted_sources_custom") or {}
                                custom[name] = []
                                cfg = Config.load()
                                cfg["trusted_sources_custom"] = custom
                                Config.save(cfg)
                                payload = _get_trusted_sources_for_ui()
                                await websocket.send_json({"type": "trusted_source_updated", "ok": True, "categories": payload.get("categories", [])})
                    except Exception as e:
                        await websocket.send_json({"type": "trusted_source_updated", "ok": False, "error": str(e)})

                elif type == "delete_trusted_category":
                    try:
                        category_id = (cmd.get("category_id") or cmd.get("categoryId") or "").strip()
                        if not category_id:
                            await websocket.send_json({"type": "trusted_source_updated", "ok": False, "error": "category_id required"})
                        else:
                            custom = dict(Config.get("trusted_sources_custom") or {})
                            if category_id not in custom:
                                await websocket.send_json({"type": "trusted_source_updated", "ok": False, "error": "Only custom categories can be deleted"})
                            else:
                                del custom[category_id]
                                cfg = Config.load()
                                cfg["trusted_sources_custom"] = custom
                                Config.save(cfg)
                                payload = _get_trusted_sources_for_ui()
                                await websocket.send_json({"type": "trusted_source_updated", "ok": True, "categories": payload.get("categories", [])})
                    except Exception as e:
                        await websocket.send_json({"type": "trusted_source_updated", "ok": False, "error": str(e)})

                elif type == "remove_trusted_source":
                    try:
                        domain = (cmd.get("domain") or "").strip().lower()
                        is_custom = bool(cmd.get("is_custom"))
                        if not domain:
                            await websocket.send_json({"type": "trusted_source_updated", "ok": False, "error": "domain required"})
                        elif is_custom:
                            custom = Config.get("trusted_sources_custom") or {}
                            for cid in list(custom.keys()):
                                custom[cid] = [s for s in custom[cid] if domain not in (s.get("domains") or [])]
                            cfg = Config.load()
                            cfg["trusted_sources_custom"] = custom
                            Config.save(cfg)
                        else:
                            disabled = list(Config.get("trusted_sources_disabled") or [])
                            if domain not in disabled:
                                disabled.append(domain)
                            cfg = Config.load()
                            cfg["trusted_sources_disabled"] = disabled
                            Config.save(cfg)
                        payload = _get_trusted_sources_for_ui()
                        await websocket.send_json({"type": "trusted_source_updated", "ok": True, "categories": payload.get("categories", [])})
                    except Exception as e:
                        await websocket.send_json({"type": "trusted_source_updated", "ok": False, "error": str(e)})

                elif type == "get_automations":
                    # Return list of saved automations; each user sees only their own (user_scope_id).
                    # Admin (local_admin_scope): also sees root automations (e.g. daily calendar check).
                    try:
                        from vaf.core.automation import AutomationManager
                        from vaf.core.config import get_local_admin_scope_id
                        user_scope_id = manager.get_connection_user(websocket) if manager else None
                        stored_role = manager.get_connection_user_role(websocket) if manager else None
                        local_admin_scope = get_local_admin_scope_id()
                        is_admin = (stored_role == "admin") or (
                            user_scope_id is not None and str(user_scope_id) == str(local_admin_scope)
                        )
                        if is_admin:
                            # Admin: load root + all user dirs, then filter to root + admin-visible scopes.
                            # Some admin JWTs can have a different scope than local_admin_scope.
                            mgr = AutomationManager()
                            all_tasks = list(mgr.list())
                            tasks = [
                                t for t in all_tasks
                                if (
                                    t.user_scope_id is None
                                    or str(t.user_scope_id) == str(user_scope_id)
                                    or str(t.user_scope_id) == str(local_admin_scope)
                                )
                            ]
                        else:
                            mgr = AutomationManager(user_scope_id=user_scope_id) if user_scope_id else AutomationManager()
                            tasks = list(mgr.list())
                        automations_list = [
                            {
                                "id": task.id,
                                "name": task.name,
                                "description": task.description,
                                "prompt": getattr(task, "prompt", "") or task.description,
                                "frequency": task.frequency,
                                "time": task.time,
                                "weekday": task.weekday,
                                "day": task.day,
                                "enabled": task.enabled,
                                "next_run": task.next_run_iso,
                                "last_run": task.last_run
                            }
                            for task in tasks
                        ]
                        await websocket.send_json({
                            "type": "automations_list",
                            "automations": automations_list
                        })
                    except Exception as e:
                        await websocket.send_json({
                            "type": "automations_list",
                            "automations": [],
                            "error": str(e)
                        })

                elif type == "create_automation":
                    try:
                        from vaf.core.automation import AutomationManager, AutomationTask
                        user_scope_id = manager.get_connection_user(websocket) if manager else None
                        mgr = AutomationManager(user_scope_id=user_scope_id) if user_scope_id else AutomationManager()
                        prompt = (cmd.get("prompt") or "").strip()
                        if not prompt:
                            await websocket.send_json({
                                "type": "create_automation_result",
                                "ok": False,
                                "error": "prompt is required"
                            })
                            continue
                        frequency = (cmd.get("frequency") or "daily").lower()
                        time_str = (cmd.get("time") or "06:00").strip()
                        if ":" not in time_str:
                            time_str = f"{int(time_str or 0) % 24:02d}:00"
                        else:
                            parts = time_str.split(":", 1)
                            h = max(0, min(23, int(parts[0] or 0)))
                            m = max(0, min(59, int(parts[1] or 0) if len(parts) > 1 else 0))
                            time_str = f"{h:02d}:{m:02d}"
                        name = (cmd.get("name") or "").strip() or (prompt[:50] + ("..." if len(prompt) > 50 else ""))
                        description = (cmd.get("description") or "").strip() or prompt[:200]
                        weekday = (cmd.get("weekday") or "").strip().lower() or None
                        day = cmd.get("day")
                        if frequency == "weekly" and weekday:
                            pass
                        elif frequency == "monthly" and day is not None:
                            day = max(1, min(31, int(day)))
                        else:
                            if frequency == "weekly":
                                weekday = None
                            if frequency == "monthly":
                                day = None
                        task = AutomationTask(
                            name=name,
                            description=description,
                            prompt=prompt,
                            frequency=frequency,
                            time=time_str,
                            weekday=weekday if frequency == "weekly" else None,
                            day=day if frequency == "monthly" else None,
                            enabled=True,
                            user_scope_id=user_scope_id,
                        )
                        can_create, err_msg = mgr.check_can_create_automation(new_time=time_str, new_frequency=frequency)
                        if not can_create and err_msg:
                            await websocket.send_json({
                                "type": "create_automation_result",
                                "ok": False,
                                "error": err_msg[:500]
                            })
                            continue
                        mgr.create(task)
                        await websocket.send_json({
                            "type": "create_automation_result",
                            "ok": True,
                            "automation": {
                                "id": task.id,
                                "name": task.name,
                                "description": task.description,
                                "frequency": task.frequency,
                                "time": task.time,
                                "enabled": task.enabled,
                                "next_run": task.next_run_iso,
                                "last_run": task.last_run,
                            }
                        })
                    except Exception as e:
                        await websocket.send_json({
                            "type": "create_automation_result",
                            "ok": False,
                            "error": str(e)
                        })

                elif type == "delete_automation":
                    try:
                        from vaf.core.automation import AutomationManager
                        user_scope_id = manager.get_connection_user(websocket) if manager else None
                        mgr = AutomationManager(user_scope_id=user_scope_id) if user_scope_id else AutomationManager()
                        task_id = (cmd.get("task_id") or cmd.get("id") or "").strip()
                        if not task_id:
                            await websocket.send_json({"type": "delete_automation_result", "ok": False, "error": "task_id required"})
                            continue
                        ok = mgr.delete(task_id, permanent=True)
                        if not ok and user_scope_id:
                            root_mgr = AutomationManager()
                            ok = root_mgr.delete(task_id, permanent=True)
                        await websocket.send_json({"type": "delete_automation_result", "ok": ok})
                    except Exception as e:
                        await websocket.send_json({"type": "delete_automation_result", "ok": False, "error": str(e)})

                elif type == "update_automation":
                    try:
                        from vaf.core.automation import AutomationManager
                        user_scope_id = manager.get_connection_user(websocket) if manager else None
                        mgr = AutomationManager(user_scope_id=user_scope_id) if user_scope_id else AutomationManager()
                        task_id = (cmd.get("task_id") or cmd.get("id") or "").strip()
                        if not task_id:
                            await websocket.send_json({"type": "update_automation_result", "ok": False, "error": "task_id required"})
                            continue
                        task = mgr.get(task_id)
                        if not task and user_scope_id:
                            root_mgr = AutomationManager()
                            task = root_mgr.get(task_id)
                            if task:
                                mgr = root_mgr
                        if not task:
                            await websocket.send_json({"type": "update_automation_result", "ok": False, "error": "Automation not found"})
                            continue
                        update_params = {}
                        for key in ("name", "description", "prompt", "frequency", "time", "weekday", "day", "enabled"):
                            if key in cmd and cmd[key] is not None:
                                if key == "enabled":
                                    update_params[key] = bool(cmd[key])
                                elif key == "day":
                                    try:
                                        update_params[key] = max(1, min(31, int(cmd[key])))
                                    except (TypeError, ValueError):
                                        pass
                                elif key == "weekday" and isinstance(cmd.get(key), str):
                                    update_params[key] = (cmd[key] or "").strip().lower() or None
                                else:
                                    update_params[key] = cmd[key]
                        if not update_params:
                            await websocket.send_json({"type": "update_automation_result", "ok": False, "error": "No fields to update"})
                            continue
                        if "time" in update_params:
                            new_time = update_params["time"]
                            if isinstance(new_time, str) and ":" in new_time:
                                parts = new_time.strip().split(":", 1)
                                h = max(0, min(23, int(parts[0] or 0)))
                                m = max(0, min(59, int(parts[1] or 0) if len(parts) > 1 else 0))
                                update_params["time"] = f"{h:02d}:{m:02d}"
                                new_time = update_params["time"]
                            new_frequency = update_params.get("frequency", task.frequency)
                            can_update, err_msg = mgr.check_can_update_automation(task_id=task_id, new_time=str(new_time), new_frequency=new_frequency)
                            if not can_update and err_msg:
                                await websocket.send_json({
                                    "type": "update_automation_result",
                                    "ok": False,
                                    "error": (err_msg[:500] if err_msg else "Time too close to another automation"),
                                })
                                continue
                        updated = mgr.update(task_id, **update_params)
                        if not updated:
                            await websocket.send_json({"type": "update_automation_result", "ok": False, "error": "Update failed"})
                            continue
                        await websocket.send_json({
                            "type": "update_automation_result",
                            "ok": True,
                            "automation": {
                                "id": updated.id,
                                "name": updated.name,
                                "description": updated.description,
                                "frequency": updated.frequency,
                                "time": updated.time,
                                "enabled": updated.enabled,
                                "next_run": updated.next_run_iso,
                                "last_run": updated.last_run,
                            }
                        })
                    except Exception as e:
                        await websocket.send_json({"type": "update_automation_result", "ok": False, "error": str(e)})

                elif type == "get_automation_notes":
                    try:
                        from vaf.core.automation_planner import list_notes
                        user_scope_id = manager.get_connection_user(websocket) if manager else None
                        notes = list_notes(user_scope_id)
                        await websocket.send_json({"type": "automation_notes_list", "notes": notes})
                    except Exception as e:
                        await websocket.send_json({"type": "automation_notes_list", "notes": [], "error": str(e)})

                elif type == "get_automation_todos":
                    try:
                        from vaf.core.automation_planner import list_todos
                        user_scope_id = manager.get_connection_user(websocket) if manager else None
                        todos = list_todos(user_scope_id)
                        await websocket.send_json({"type": "automation_todos_list", "todos": todos})
                    except Exception as e:
                        await websocket.send_json({"type": "automation_todos_list", "todos": [], "error": str(e)})

                elif type == "get_thinking_workspace_handoffs":
                    try:
                        from vaf.core.thinking_workspace import list_pending_handoffs

                        user_scope_id = manager.get_connection_user(websocket) if manager else None
                        handoffs = list_pending_handoffs(user_scope_id)
                        await websocket.send_json({"type": "thinking_workspace_handoffs_list", "handoffs": handoffs})
                    except Exception as e:
                        await websocket.send_json({"type": "thinking_workspace_handoffs_list", "handoffs": [], "error": str(e)})

                elif type == "approve_thinking_workspace_handoff":
                    try:
                        from vaf.core.thinking_workspace import approve_handoff, get_handoff
                        from vaf.core.user_notifications import append_notification

                        user_scope_id = manager.get_connection_user(websocket) if manager else None
                        task_id = (cmd.get("task_id") or "").strip()
                        handoff_id = (cmd.get("handoff_id") or "").strip()
                        if not task_id or not handoff_id:
                            await websocket.send_json({
                                "type": "thinking_workspace_handoff_result",
                                "ok": False,
                                "action": "approve",
                                "error": "task_id and handoff_id required",
                            })
                            continue
                        ok = approve_handoff(user_scope_id, task_id, handoff_id)
                        handoff = get_handoff(user_scope_id, task_id, handoff_id) if ok else None
                        action_result = handoff.get("automation_action_result") if isinstance(handoff, dict) else None
                        action_failed = isinstance(action_result, dict) and (action_result.get("ok") is False)
                        notif_status = "error" if (not ok or action_failed) else "success"
                        summary_parts = [
                            f"Action: approve",
                            f"Task: {task_id}",
                            f"Handoff: {handoff_id}",
                        ]
                        if isinstance(action_result, dict):
                            op = str(action_result.get("operation") or "").strip()
                            ok_txt = "ok" if action_result.get("ok") else "failed"
                            if op:
                                summary_parts.append(f"Automation action: {op} ({ok_txt})")
                            if action_result.get("task_id"):
                                summary_parts.append(f"Automation id: {action_result.get('task_id')}")
                            if action_result.get("error"):
                                summary_parts.append(f"Error: {action_result.get('error')}")
                        append_notification(
                            user_scope_id,
                            kind="system",
                            title="Workspace handoff approved",
                            status=notif_status,
                            summary="\n".join(summary_parts),
                            task_id=task_id,
                            handoff_id=handoff_id,
                            action="approve",
                            automation_action_result=action_result if isinstance(action_result, dict) else None,
                        )
                        await websocket.send_json(
                            {
                                "type": "new_log",
                                "entry": {
                                    "timestamp": time.time(),
                                    "message": (
                                        f"Thinking Workspace handoff approved ({handoff_id})."
                                        + (
                                            " Automation action failed."
                                            if action_failed
                                            else (
                                                " Automation action applied."
                                                if isinstance(action_result, dict) and action_result.get("ok") is True
                                                else ""
                                            )
                                        )
                                    ),
                                    "source": "System",
                                },
                            }
                        )
                        await websocket.send_json({
                            "type": "thinking_workspace_handoff_result",
                            "ok": ok,
                            "action": "approve",
                            "task_id": task_id,
                            "handoff_id": handoff_id,
                            "automation_action_result": action_result,
                            "error": None if ok else "handoff not found",
                        })
                    except Exception as e:
                        await websocket.send_json({
                            "type": "thinking_workspace_handoff_result",
                            "ok": False,
                            "action": "approve",
                            "error": str(e),
                        })

                elif type == "reject_thinking_workspace_handoff":
                    try:
                        from vaf.core.thinking_workspace import reject_handoff
                        from vaf.core.user_notifications import append_notification

                        user_scope_id = manager.get_connection_user(websocket) if manager else None
                        task_id = (cmd.get("task_id") or "").strip()
                        handoff_id = (cmd.get("handoff_id") or "").strip()
                        reason = (cmd.get("reason") or "").strip()
                        if not task_id or not handoff_id:
                            await websocket.send_json({
                                "type": "thinking_workspace_handoff_result",
                                "ok": False,
                                "action": "reject",
                                "error": "task_id and handoff_id required",
                            })
                            continue
                        ok = reject_handoff(user_scope_id, task_id, handoff_id, reason=reason)
                        append_notification(
                            user_scope_id,
                            kind="system",
                            title="Workspace handoff rejected",
                            status="success" if ok else "error",
                            summary="\n".join(
                                [
                                    "Action: reject",
                                    f"Task: {task_id}",
                                    f"Handoff: {handoff_id}",
                                    f"Reason: {(reason or 'n/a')[:500]}",
                                ]
                            ),
                            task_id=task_id,
                            handoff_id=handoff_id,
                            action="reject",
                        )
                        await websocket.send_json(
                            {
                                "type": "new_log",
                                "entry": {
                                    "timestamp": time.time(),
                                    "message": f"Thinking Workspace handoff rejected ({handoff_id}).",
                                    "source": "System",
                                },
                            }
                        )
                        await websocket.send_json({
                            "type": "thinking_workspace_handoff_result",
                            "ok": ok,
                            "action": "reject",
                            "task_id": task_id,
                            "handoff_id": handoff_id,
                            "error": None if ok else "handoff not found",
                        })
                    except Exception as e:
                        await websocket.send_json({
                            "type": "thinking_workspace_handoff_result",
                            "ok": False,
                            "action": "reject",
                            "error": str(e),
                        })

                elif type == "get_notifications":
                    try:
                        from vaf.core.user_notifications import get_notifications
                        user_scope_id = manager.get_connection_user(websocket) if manager else None
                        limit = min(100, max(1, int(cmd.get("limit") or 50)))
                        notifications = get_notifications(user_scope_id, limit=limit)
                        await websocket.send_json({"type": "notifications_list", "notifications": notifications})
                    except Exception as e:
                        await websocket.send_json({"type": "notifications_list", "notifications": [], "error": str(e)})

                elif type == "create_automation_note":
                    try:
                        from vaf.core.automation_planner import add_note
                        user_scope_id = manager.get_connection_user(websocket) if manager else None
                        title = cmd.get("title")
                        content = (cmd.get("content") or "").strip()
                        if not content:
                            await websocket.send_json({"type": "create_automation_note_result", "ok": False, "error": "content required"})
                            continue
                        note = add_note(user_scope_id, content, title=title)
                        await websocket.send_json({"type": "create_automation_note_result", "ok": True, "note": note})
                    except Exception as e:
                        await websocket.send_json({"type": "create_automation_note_result", "ok": False, "error": str(e)})

                elif type == "create_automation_todo":
                    try:
                        from vaf.core.automation_planner import add_todo
                        user_scope_id = manager.get_connection_user(websocket) if manager else None
                        text = (cmd.get("text") or "").strip()
                        if not text:
                            await websocket.send_json({"type": "create_automation_todo_result", "ok": False, "error": "text required"})
                            continue
                        due_at = cmd.get("due_at")
                        todo = add_todo(user_scope_id, text, due_at=due_at)
                        await websocket.send_json({"type": "create_automation_todo_result", "ok": True, "todo": todo})
                    except Exception as e:
                        await websocket.send_json({"type": "create_automation_todo_result", "ok": False, "error": str(e)})

                elif type == "update_automation_todo":
                    try:
                        from vaf.core.automation_planner import update_todo
                        user_scope_id = manager.get_connection_user(websocket) if manager else None
                        todo_id = (cmd.get("id") or "").strip()
                        if not todo_id:
                            await websocket.send_json({"type": "update_automation_todo_result", "ok": False, "error": "id required"})
                            continue
                        text = cmd.get("text")
                        done = cmd.get("done")
                        due_at = cmd.get("due_at")
                        updated = update_todo(user_scope_id, todo_id, text=text, done=done, due_at=due_at)
                        if not updated:
                            await websocket.send_json({"type": "update_automation_todo_result", "ok": False, "error": "Todo not found"})
                            continue
                        await websocket.send_json({"type": "update_automation_todo_result", "ok": True, "todo": updated})
                    except Exception as e:
                        await websocket.send_json({"type": "update_automation_todo_result", "ok": False, "error": str(e)})

                elif type == "delete_automation_note":
                    try:
                        from vaf.core.automation_planner import delete_note
                        user_scope_id = manager.get_connection_user(websocket) if manager else None
                        note_id = (cmd.get("id") or "").strip()
                        if not note_id:
                            await websocket.send_json({"type": "delete_automation_note_result", "ok": False, "error": "id required"})
                            continue
                        ok = delete_note(user_scope_id, note_id)
                        await websocket.send_json({"type": "delete_automation_note_result", "ok": ok, "id": note_id if ok else None})
                    except Exception as e:
                        await websocket.send_json({"type": "delete_automation_note_result", "ok": False, "error": str(e)})

                elif type == "delete_automation_todo":
                    try:
                        from vaf.core.automation_planner import delete_todo
                        user_scope_id = manager.get_connection_user(websocket) if manager else None
                        todo_id = (cmd.get("id") or "").strip()
                        if not todo_id:
                            await websocket.send_json({"type": "delete_automation_todo_result", "ok": False, "error": "id required"})
                            continue
                        ok = delete_todo(user_scope_id, todo_id)
                        await websocket.send_json({"type": "delete_automation_todo_result", "ok": ok, "id": todo_id if ok else None})
                    except Exception as e:
                        await websocket.send_json({"type": "delete_automation_todo_result", "ok": False, "error": str(e)})

                elif type == "process_audio":
                    # Process audio for STT: Docker (HTTP) or local (faster-whisper)
                    import base64
                    import tempfile
                    
                    temp_path = None
                    try:
                        audio_b64 = cmd.get("audio")
                        if not audio_b64:
                            await websocket.send_json({
                                "type": "stt_error",
                                "error": "No audio data provided"
                            })
                            continue

                        audio_data = base64.b64decode(audio_b64)
                        # Use format hint from frontend (wav preferred for Whisper compatibility)
                        audio_format = cmd.get("format", "webm")
                        suffix = ".wav" if audio_format == "wav" else ".webm"
                        mime_type = "audio/wav" if audio_format == "wav" else "audio/webm"

                        with tempfile.NamedTemporaryFile(prefix="vaf_", suffix=suffix, delete=False) as temp_audio:
                            temp_audio.write(audio_data)
                            temp_path = temp_audio.name

                        from vaf.core.speech import SpeechManager
                        sm = SpeechManager.get_instance()
                        if not sm.is_stt_enabled():
                            await websocket.send_json({
                                "type": "stt_error",
                                "error": "STT is disabled in settings"
                            })
                            continue

                        stt_engine = Config.get("speech_stt_engine", "docker")
                        text = ""

                        if stt_engine == "docker":
                            # Docker STT: POST audio file to whisper-asr-webservice (onerahmet image)
                            stt_url = (Config.get("speech_stt_docker_url") or "").strip().rstrip("/") or "http://localhost:5003"
                            try:
                                import requests
                                loop = asyncio.get_running_loop()
                                def _post(endpoint: str):
                                    with open(temp_path, "rb") as f:
                                        # whisper-asr-webservice expects "audio_file" field name
                                        # Add encode=true to let ffmpeg handle the format conversion
                                        # Add output=json to get JSON response
                                        filename = f"audio{suffix}"
                                        return requests.post(
                                            endpoint,
                                            files={"audio_file": (filename, f, mime_type)},
                                            params={"encode": "true", "output": "json"},
                                            timeout=60,
                                        )
                                asr_endpoint = f"{stt_url}/asr"
                                resp = await loop.run_in_executor(None, lambda: _post(asr_endpoint))
                                if resp.status_code == 404:
                                    transcribe_endpoint = f"{stt_url}/transcribe"
                                    resp = await loop.run_in_executor(None, lambda: _post(transcribe_endpoint))

                                # Check for errors before parsing JSON
                                if resp.status_code >= 400:
                                    error_text = resp.text[:200] if resp.text else f"HTTP {resp.status_code}"
                                    raise Exception(f"STT service error: {error_text}")

                                # Try to parse JSON response
                                try:
                                    data = resp.json()
                                except Exception:
                                    # If response is plain text, use it directly
                                    text = resp.text.strip() if resp.text else ""
                                    data = {}

                                if not text:
                                    text = (data.get("text") or data.get("transcript") or "").strip()
                                if not text and isinstance(data.get("results"), list) and data["results"]:
                                    text = (data["results"][0].get("transcript") or "").strip()
                            except Exception as docker_err:
                                await websocket.send_json({
                                    "type": "stt_error",
                                    "error": f"Docker STT failed: {docker_err}. Is the STT container running (e.g. docker compose -f docker-compose.memory.yml up -d)?"
                                })
                                continue
                        else:
                            # Local STT: faster-whisper (SINGLETON)
                            try:
                                model = get_whisper_model()
                                segments, info = model.transcribe(temp_path, beam_size=5)
                                text = " ".join([segment.text for segment in segments])
                            except ImportError:
                                await websocket.send_json({
                                    "type": "stt_error",
                                    "error": "faster-whisper not installed. Use STT engine 'Docker' or: pip install faster-whisper"
                                })
                                continue
                            except Exception as transcribe_error:
                                await websocket.send_json({
                                    "type": "stt_error",
                                    "error": f"Transcription failed: {str(transcribe_error)}"
                                })
                                continue

                        await websocket.send_json({
                            "type": "stt_result",
                            "text": text.strip()
                        })
                    except Exception as e:
                        await websocket.send_json({
                            "type": "stt_error",
                            "error": str(e)
                        })
                    finally:
                        if temp_path and os.path.exists(temp_path):
                            try:
                                os.unlink(temp_path)
                            except Exception:
                                pass
                
                elif type == "speak":
                    # TTS Output: only if Auto TTS is enabled; otherwise skip to avoid loading TTS when disabled.
                    text = cmd.get("text")
                    print(f"[WebSocket] speak request received, text_len={len(text) if text else 0}")
                    if text:
                        tts_enabled = Config.get("speech_tts_enabled", False)
                        print(f"[WebSocket] speech_tts_enabled = {tts_enabled}")
                        if not tts_enabled:
                            print("[WebSocket] TTS disabled, sending stopped state")
                            await websocket.send_json({"type": "tts_state", "status": "stopped"})
                        else:
                            from vaf.core.speech import SpeechManager
                            sm = SpeechManager.get_instance()
                            await websocket.send_json({"type": "tts_state", "status": "loading"})
                            lang = _detect_language_simple(text)
                            print(f"[WebSocket] Detected language: {lang}")
                            import base64
                            # asyncio is already imported at top of file
                            loop = asyncio.get_running_loop()
                            _TTS_SYNTH_TIMEOUT = 35.0  # seconds; avoid blocking WebSocket if Docker TTS hangs
                            try:
                                configured_engine = Config.get("speech_tts_engine", "docker")
                                if configured_engine not in ("docker", "chatterbox"):
                                    configured_engine = "docker"
                                print(f"[WebSocket] Calling synthesize_audio with engine={configured_engine}")
                                audio_bytes = await asyncio.wait_for(
                                    loop.run_in_executor(
                                        None,
                                        lambda: sm.synthesize_audio(text, lang, force_engine=configured_engine),
                                    ),
                                    timeout=_TTS_SYNTH_TIMEOUT,
                                )
                                print(f"[WebSocket] synthesize_audio returned {len(audio_bytes) if audio_bytes else 0} bytes")
                                if audio_bytes:
                                    audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')
                                    print(f"[WebSocket] Sending tts_audio, base64 len={len(audio_b64)}")
                                    await websocket.send_json({
                                        "type": "tts_audio",
                                        "audio": audio_b64,
                                        "format": "wav"
                                    })
                                else:
                                    print("[WebSocket] No audio bytes, sending stopped state")
                                    await websocket.send_json({"type": "tts_state", "status": "stopped"})
                            except asyncio.TimeoutError:
                                log("WebServer", f"TTS synthesize timed out after {_TTS_SYNTH_TIMEOUT}s (WebSocket continues)")
                                await websocket.send_json({"type": "tts_state", "status": "stopped"})
                            except Exception as e:
                                print(f"[WebServer] TTS Error: {e}")
                                import traceback
                                traceback.print_exc()
                                await websocket.send_json({"type": "tts_state", "status": "stopped"})
                
                elif type == "stop_speech":
                    # Stop TTS
                    from vaf.core.speech import SpeechManager
                    sm = SpeechManager.get_instance()
                    sm.stop()

                elif type == "stop_generation":
                    # Stop the current generation by setting a flag
                    from vaf.core.task_queue import TaskQueue
                    from vaf.core.platform import Platform
                    tq = TaskQueue()
                    session_id = cmd.get("sessionId") or manager.get_session_for_connection(websocket)
                    if session_id:
                        tq.request_stop(session_id)
                        dropped = 0
                        try:
                            # Also drop already queued follow-up tasks for this session.
                            # Otherwise users may need to press stop multiple times.
                            dropped = tq.drop_queued_tasks_for_session(str(session_id))
                        except Exception:
                            dropped = 0
                        killed = 0
                        try:
                            # Hard-stop any running sub-agent processes for this chat session.
                            killed = Platform.stop_webui_subagent_processes(str(session_id))
                        except Exception:
                            killed = 0
                        # Also convert active sub-agent tasks into failed results immediately.
                        try:
                            from vaf.core.subagent_ipc import get_ipc
                            ipc = get_ipc()
                            active = ipc.get_active_tasks(session_id=str(session_id))
                            for t in active:
                                try:
                                    ipc.fail_task(t.task_id, "[USER_CANCELLED] Stopped/Cancelled by user via stop button.")
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        log("WebServer", f"Stop requested for session {session_id}; killed_subagents={killed}; dropped_queued={dropped}")
                        await websocket.send_json({"type": "generation_stopped", "sessionId": session_id})

            except json.JSONDecodeError:
                pass
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        tray_context.set_websocket_count(len(manager.active_connections)) # Update active count
        log("API", f"WebSocket disconnected. Active: {tray_context.active_websockets}")
        
        # Unregister from connection tracker
        try:
            from vaf.network.connection_tracker import get_tracker
            tracker = get_tracker()
            tracker.unregister_connection(connection_id)
        except Exception:
            pass
        
        if len(manager.active_connections) == 0:
            os.environ.pop("VAF_WEBUI_ACTIVE", None)
    except Exception as e:
        print(f"WebSocket error: {e}")
        manager.disconnect(websocket)
        tray_context.set_websocket_count(len(manager.active_connections)) # Update active count
        log("API", f"WebSocket error; active now {tray_context.active_websockets}")
        
        # Unregister from connection tracker
        try:
            from vaf.network.connection_tracker import get_tracker
            tracker = get_tracker()
            tracker.unregister_connection(connection_id)
        except Exception:
            pass
        
        if len(manager.active_connections) == 0:
            os.environ.pop("VAF_WEBUI_ACTIVE", None)


async def process_uploaded_files(files: list) -> str:
    """
    Process uploaded files and return their text content.
    Mimics CLI @filename behavior by reading file contents and formatting them.
    
    Args:
        files: List of file objects with {name, data, mimeType}
        
    Returns:
        Formatted string with file contents
    """
    import base64
    import tempfile
    import os
    from pathlib import Path
    
    if not files:
        return ""
    
    results = []
    
    for file_obj in files:
        try:
            filename = file_obj.get("name", "unknown")
            file_data = file_obj.get("data", "")
            mime_type = file_obj.get("mimeType", "")
            
            print(f"[WebUI] Processing file: {filename} ({mime_type})")
            
            # Decode base64 data
            if file_data.startswith("data:"):
                # Remove data URL prefix (e.g., "data:application/pdf;base64,")
                file_data = file_data.split(",", 1)[1] if "," in file_data else file_data
            
            decoded_data = base64.b64decode(file_data)
            
            # Save to temporary file
            file_ext = Path(filename).suffix or ".txt"
            with tempfile.NamedTemporaryFile(prefix="vaf_", suffix=file_ext, delete=False) as temp_file:
                temp_file.write(decoded_data)
                temp_path = temp_file.name

            try:
                # Use Librarian to read file contents
                from vaf.tools.librarian import LibrarianTool
                librarian = LibrarianTool()
                
                # Read file using Librarian's _read_file method
                content = librarian._read_file(Path(temp_path), enable_chunking=True)
                
                # Format like CLI does: --- FILE: name ---\nCONTENTS\n----------------
                formatted = f"\n\n--- FILE: {filename} ---\n{content}\n----------------\n"
                results.append(formatted)
                
                print(f"[WebUI] Successfully processed: {filename}")
                
            finally:
                # Clean up temp file
                try:
                    os.unlink(temp_path)
                except:
                    pass
                    
        except Exception as e:
            error_msg = f"\n\n--- FILE: {filename} ---\n[ERROR] Failed to process file: {str(e)}\n----------------\n"
            results.append(error_msg)
            print(f"[WebUI] Error processing {filename}: {e}")
    
    return "".join(results)


def _is_temp_like_filename(name: str) -> bool:
    """True if name looks like our temp file (vaf_xxxxx.ext), so we should not expose it as document name."""
    if not name or not name.startswith("vaf_"):
        return False
    import re
    return bool(re.match(r"^vaf_[a-zA-Z0-9_]+\.[a-zA-Z0-9]+$", name))


async def _notify_attachment_index(manager, session_id: str, kind: str, **extra) -> None:
    """Fail-safe WS broadcast for the attachment indexing-status indicator in the Web UI.
    kind: 'attachment_indexing' (start) | 'attachment_indexed' (done) | 'attachment_index_error'."""
    try:
        await manager.broadcast_to_session(session_id, {"type": kind, "sessionId": session_id, **extra})
    except Exception:
        pass


async def process_files_to_sidebar_list(files: list) -> list:
    """
    Process uploaded files and return a list of {name, content, data?, mimeType?, path?} for sidebar_documents.
    Uses same Librarian extraction as process_uploaded_files.
    Passes through data (base64) and mimeType for PDF display.
    Uploaded files get path="Hochgeladen über Web-UI (kein lokaler Pfad)" so the agent knows there is no local path.
    """
    import base64
    import tempfile
    import os
    from pathlib import Path

    if not files:
        return []

    from vaf.core.log_helper import log_attachment
    results = []
    for file_obj in files:
        try:
            filename = file_obj.get("name", "unknown")
            file_data = file_obj.get("data", "")
            mime_type = file_obj.get("mimeType", "")

            base64_part = file_data
            if base64_part.startswith("data:"):
                base64_part = base64_part.split(",", 1)[1] if "," in base64_part else base64_part

            decoded_data = base64.b64decode(base64_part)
            file_ext = Path(filename).suffix or ".txt"
            if _is_temp_like_filename(filename):
                filename = f"Dokument{file_ext}"

            log_attachment("FILE_RECEIVED",
                name=filename, mime=mime_type, ext=file_ext,
                base64_len=len(base64_part), decoded_bytes=len(decoded_data))

            with tempfile.NamedTemporaryFile(prefix="vaf_", suffix=file_ext, delete=False) as temp_file:
                temp_file.write(decoded_data)
                temp_path = temp_file.name

            try:
                from vaf.tools.librarian import LibrarianTool
                librarian = LibrarianTool()
                # Run in a thread — PDF parsing (PyPDF2) is CPU-bound and blocks the
                # event loop, which delays WebSocket streaming for the entire duration.
                content = await asyncio.to_thread(librarian._read_file, Path(temp_path), True)
                # Strip lone Unicode surrogates (e.g. \u{DE16} from PDF emoji).
                # They survive json.dumps but crash UTF-8 encoding in WebSocket sends,
                # session file writes, and API request serialization.
                if content:
                    content = content.encode("utf-8", errors="replace").decode("utf-8")

                # Classify content type for diagnostics
                _ctype = "TEXT"
                if "[ERROR]" in (content or "")[:80]:
                    _ctype = "ERROR"
                elif "[Scanned PDF" in (content or "")[:200]:
                    _ctype = "SCANNED_NO_TEXT"
                elif "[INFO]" in (content or "")[:80] and "Auto-Chunk" in (content or "")[:200]:
                    _ctype = "CHUNKED"
                log_attachment("EXTRACT_DONE",
                    name=filename, content_type=_ctype,
                    content_len=len(content or ""),
                    preview=repr((content or "")[:300]))

                entry = {
                    "name": filename,
                    "content": content,
                    "path": "Hochgeladen über Web-UI (kein lokaler Pfad)",
                }
                if base64_part:
                    entry["data"] = base64_part
                if mime_type:
                    entry["mimeType"] = mime_type
                suf = file_ext.lower()
                if suf in (".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"):
                    # Prefer Gotenberg (LibreOffice in Docker): full design fidelity, DOCX/XLSX/PPTX → PDF.
                    pdf_bytes = await asyncio.to_thread(_office_to_pdf_via_gotenberg, temp_path, filename)
                    if not pdf_bytes and suf == ".docx":
                        pdf_bytes = await asyncio.to_thread(_docx_to_pdf_via_libreoffice, temp_path)
                    if pdf_bytes:
                        entry["data"] = base64.b64encode(pdf_bytes).decode("ascii")
                        entry["mimeType"] = "application/pdf"
                    else:
                        try:
                            if suf == ".docx":
                                html_body = _docx_to_html(temp_path)
                            elif suf == ".xlsx":
                                html_body = _xlsx_to_html(temp_path)
                            elif suf == ".pptx":
                                html_body = _pptx_to_html(temp_path)
                            else:
                                html_body = "<p>[ODT/ODS/ODP: Gotenberg nicht erreichbar. Bitte starten Sie den Docker-Stack.]</p>"
                            entry["htmlContent"] = (
                                "<!DOCTYPE html><html><head><meta charset=\"utf-8\"/>"
                                "<style>body{font-family:system-ui,sans-serif;padding:1.5rem;max-width:80ch;}"
                                "table{border-collapse:collapse;}td,th{border:1px solid #ccc;padding:6px;}</style>"
                                "</head><body>" + html_body + "</body></html>"
                            )
                        except Exception as e:
                            log("WebServer", f"Office to HTML failed for {filename}: {e}")
                results.append(entry)
            finally:
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass
        except Exception as e:
            import traceback as _tb2
            err_name = file_obj.get("name", "unknown")
            if _is_temp_like_filename(err_name):
                err_name = "Dokument" + (Path(err_name).suffix or ".txt")
            log_attachment("FILE_ERROR", name=err_name, error=str(e), tb=_tb2.format_exc()[-300:])
            results.append({
                "name": err_name,
                "content": f"[ERROR] Failed to process file: {str(e)}",
                "path": "Hochgeladen über Web-UI (kein lokaler Pfad)",
            })
    return results


def run_server(host="127.0.0.1", port=8001):
    """Run the Uvicorn server. Uses TLS (HTTPS/WSS) when config has cert/key paths set."""
    # Store the loop so the TUI thread can schedule updates
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    manager.set_server_loop(loop)

    tls_enabled = Config.get("local_network_tls_enabled", False)
    ssl_cert = (Config.get("local_network_ssl_cert") or "").strip()
    ssl_key = (Config.get("local_network_ssl_key") or "").strip()

    # Auto-generate certificates if TLS enabled but no valid certs
    if tls_enabled and (not ssl_cert or not ssl_key or not os.path.isfile(ssl_cert) or not os.path.isfile(ssl_key)):
        try:
            from vaf.network.ssl_utils import ensure_ssl_certificates
            ssl_cert, ssl_key = ensure_ssl_certificates()
            if ssl_cert and ssl_key:
                log("WebServer", f"Auto-generated SSL certificate: {ssl_cert}")
        except Exception as e:
            log("WebServer", f"SSL auto-generation failed, falling back to HTTP: {e}")
            ssl_cert, ssl_key = "", ""

    # Allow large PDF/file attachments: base64-encoded image-heavy PDFs can easily exceed
    # uvicorn's default ws_max_size (16 MB), causing the server to close the WebSocket
    # connection mid-upload (frontend shows "Verbindung wird wiederhergestellt").
    ws_max_size = 200 * 1024 * 1024  # 200 MB

    if tls_enabled and ssl_cert and ssl_key and os.path.isfile(ssl_cert) and os.path.isfile(ssl_key):
        config = uvicorn.Config(
            app=app, host=host, port=port, loop="asyncio", log_level="error",
            ssl_certfile=ssl_cert, ssl_keyfile=ssl_key,
            ws_max_size=ws_max_size,
        )
        log("WebServer", f"Starting with TLS (HTTPS/WSS) on {host}:{port}")
    else:
        config = uvicorn.Config(app=app, host=host, port=port, loop="asyncio", log_level="error",
                                ws_max_size=ws_max_size)
        if tls_enabled:
            log("WebServer", f"WARNING: TLS enabled but no certificates available, running HTTP on {host}:{port}")
    server = uvicorn.Server(config)

    # We run this in the thread provided by the caller
    loop.run_until_complete(server.serve())

def start_background_server(host="127.0.0.1", port=8001):
    """Start server in a daemon thread."""
    t = threading.Thread(target=run_server, args=(host, port), daemon=True)
    t.start()
    return t
