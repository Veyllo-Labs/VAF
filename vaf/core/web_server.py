from vaf.startup_logger import log
log("WebServer", "Module load started")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi import HTTPException, Query
import asyncio
import uvicorn
import threading
import inspect
import html
import os
log("WebServer", "Basic imports done")

from vaf.core.web_interface import get_web_interface
from vaf.core.session import SessionManager, Session
from vaf.cli.autosuggest import SmartAutoSuggest
import json
from vaf.core.config import Config
from vaf.core.log_helper import append_domain_log, is_debug_logging_enabled
from pathlib import Path
from typing import Optional, List
import logging
from vaf.core.tray_context import TrayContext
log("WebServer", "VAF imports done")

log_uvicorn = logging.getLogger("uvicorn")

app = FastAPI(title="VAF Local Server")


@app.exception_handler(Exception)
async def json_exception_handler(request, exc):
    """Ensure unhandled exceptions return JSON (for API clients) instead of HTML."""
    from fastapi.responses import JSONResponse
    # HTTPException: return JSON; other exceptions: 500 with error message
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "error": str(exc.detail)})
    log_uvicorn.exception("Unhandled exception: %s", exc)
    return JSONResponse(status_code=500, content={"ok": False, "error": str(exc), "detail": str(exc)})


# CORS: explicit origins required when frontend sends credentials (cookies).
# Regex + credentials can fail in some browsers; list is reliable.
def _build_cors_origins() -> list[str]:
    """Build CORS allow-list including localhost AND local-network IPs when enabled."""
    origins = [
        "http://localhost",
        "http://127.0.0.1",
    ] + [
        f"http://localhost:{p}" for p in range(3000, 3012)
    ] + [
        f"http://127.0.0.1:{p}" for p in range(3000, 3012)
    ]

    # When TLS is enabled, add https:// variants
    tls_on = Config.get("local_network_tls_enabled", False)
    if tls_on:
        origins += [
            "https://localhost",
            "https://127.0.0.1",
        ] + [
            f"https://localhost:{p}" for p in range(3000, 3012)
        ] + [
            f"https://127.0.0.1:{p}" for p in range(3000, 3012)
        ]

    # When local network is enabled, add private-IP origins
    if Config.get("local_network_enabled", False):
        try:
            from vaf.network.binding import get_all_local_ips
            for _iface, ip in get_all_local_ips():
                for port in [3000, 8001]:
                    origins.append(f"http://{ip}:{port}")
                    origins.append(f"http://{ip}")
                    if tls_on:
                        origins.append(f"https://{ip}:{port}")
                        origins.append(f"https://{ip}")
        except Exception:
            pass  # IP middleware validates anyway

    return list(set(origins))  # deduplicate

_CORS_ORIGINS = _build_cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
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
            port = Config.get("local_network_port", 8001)
            port_frontend = Config.get("local_network_port_frontend", 3000)
            
            success = setup_firewall(port, port_frontend)
            if success:
                register_cleanup_on_exit()
                log("WebServer", f"Firewall rules created for ports {port}, {port_frontend}")
            else:
                log("WebServer", "Firewall setup failed - may need elevated privileges")
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

    # In Docker mode, start the headless agent runner
    # This handles task processing (chat, tools, etc.) within the container
    if Config.is_docker_mode():
        log("WebServer", "Docker mode detected - starting headless agent runner...")
        try:
            from vaf.core.headless_runner import run_headless_agent
            import threading
            agent_thread = threading.Thread(target=run_headless_agent, daemon=True, name="HeadlessAgent")
            agent_thread.start()
            log("WebServer", "Headless agent runner started in background thread")
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
        import langid
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
    # We're in an async handler, so we can directly await without checking the loop
    # The server loop check was causing silent failures when _server_loop wasn't set
    try:
        if update.sessionId:
            await manager.broadcast_to_session(update.sessionId, data)
        else:
            await manager.broadcast(data)
    except Exception as e:
        append_domain_log("webui", f"[ERROR] broadcast failed in /api/workflow/update: {e}")
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


class FileSaveRequest(BaseModel):
    """Request body for saving a file."""
    path: str
    content: str


class FileSaveDocxRequest(BaseModel):
    """Request body for saving HTML content back as .docx."""
    path: str
    content: str  # HTML from the editor


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
async def download_file(path: str = Query(..., description="Absolute path to local file")):
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
    ]

    if not any(target.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="Access denied")

    mime_type, _ = mimetypes.guess_type(str(target))
    return FileResponse(
        path=str(target),
        media_type=mime_type or "application/octet-stream",
        filename=target.name,
    )

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
                "steps": steps
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

    # If local network is DISABLED, only allow localhost
    if not local_network_enabled:
        if not is_localhost_client:
            log("API", f"WebSocket rejected: Local network disabled, non-localhost IP {client_ip}")
            # Update tracker if possible
            try: get_tracker().unregister_connection(connection_id)
            except: pass
            
            await websocket.close(code=4003, reason="Local network feature is disabled")
            return
        # Localhost - allow; if JWT cookie present, decode so we get user_scope_id for RAG
        # Use fixed local_admin_scope_id + local_admin_username so WebSocket and HTTP API use same user (user_identity, RAG)
        from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
        local_admin_scope = get_local_admin_scope_id()
        local_admin_username = get_local_admin_username()
        user_context = {"username": local_admin_username, "role": "admin", "user_scope_id": local_admin_scope}
        if token:
            try:
                from vaf.auth.crypto import get_jwt_secret
                import jwt
                secret = get_jwt_secret()
                payload = jwt.decode(token, secret, algorithms=["HS256"])
                user_context = {
                    "user_id": payload.get("sub"),
                    "user_scope_id": payload.get("user_scope_id") or local_admin_scope,
                    "username": payload.get("username", local_admin_username),
                    "role": payload.get("role", "admin"),
                }
                log("API", f"WebSocket (localhost) authenticated: {user_context.get('username')} (scope: {user_context.get('user_scope_id')})")
            except Exception as e:
                log("API", f"WebSocket (localhost) token decode failed: {e}, using Local Admin scope")
                pass  # Keep Local Admin fallback with scope
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
            
            # Non-localhost requires token authentication
            if not is_localhost_client and not token:
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
            elif is_localhost_client:
                # Localhost without token: use fixed scope + username so RAG and user_identity match HTTP API
                from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
                local_admin_scope = get_local_admin_scope_id()
                local_admin_username = get_local_admin_username()
                user_context = {"username": local_admin_username, "role": "admin", "user_scope_id": local_admin_scope}
            
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
        # Send initial session list (only Web UI chats; channel sessions appear in their dashboards)
        sessions = session_mgr.list(limit=SESSION_LIST_LIMIT)
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
            try:
                # Subscribe this connection to the session for scoped updates
                manager.subscribe_to_session(websocket, sid)

                # Load from disk and send history update
                loaded = session_mgr.load(sid)

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
                    if role == "tool" and meta:
                        if meta.get("toolName") is not None:
                            entry["toolName"] = meta["toolName"]
                        if meta.get("toolId") is not None:
                            entry["toolId"] = meta["toolId"]
                        if meta.get("toolStatus") is not None:
                            entry["toolStatus"] = meta["toolStatus"]
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
            except Exception as e:
                log("WebServer", f"Auto-load session failed: {e}")

        while True:
            # Listen for client commands
            data_str = await websocket.receive_text()
            tray_context.register_websocket_activity()
            try:
                cmd = json.loads(data_str)
                type = cmd.get("type")
                
                # --- SESSION MANAGEMENT ---
                
                if type == "get_sessions":
                    sessions = session_mgr.list(limit=SESSION_LIST_LIMIT)
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
                    try:
                        # Subscribe this connection to the session for scoped updates
                        manager.subscribe_to_session(websocket, sid)
                        
                        # Push command to main loop to switch session
                        from vaf.core.task_queue import TaskQueue
                        tq = TaskQueue()
                        tq.add(session_id="system", input_text=f"__CMD__:LOAD_SESSION:{sid}", source="web")
                        
                        # 1. Load from disk (just to send history to frontend immediately)
                        loaded = session_mgr.load(sid)
                        
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
                            if role == "tool" and meta:
                                if meta.get("toolName") is not None:
                                    entry["toolName"] = meta["toolName"]
                                if meta.get("toolId") is not None:
                                    entry["toolId"] = meta["toolId"]
                                if meta.get("toolStatus") is not None:
                                    entry["toolStatus"] = meta["toolStatus"]
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
                                "percent": (est_tokens / max_ctx) if max_ctx else 0.0,
                                "api": is_api
                            }
                            await websocket.send_json({
                                "type": "stats",
                                "stats": stats
                            })
                        except Exception as e:
                            print(f"[WebServer] Stats estimation error: {e}")

                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        print(f"Load error: {e}")

                elif type == "delete_session":
                    sid = cmd.get("id")
                    session_mgr.delete(sid)
                    # Broadcast update
                    sessions = session_mgr.list(limit=SESSION_LIST_LIMIT)
                    web_sessions = _web_ui_sessions(sessions)
                    await manager.broadcast({
                        "type": "session_list",
                        "sessions": [
                            {"id": s["id"], "title": s["name"], "date": s["updated_at"], "messageCount": s["message_count"], "source": (s.get("metadata") or {}).get("source")}
                            for s in web_sessions
                        ]
                    })

                elif type == "hide_session":
                    sid = cmd.get("id")
                    if sid and session_mgr.hide(sid):
                        sessions = session_mgr.list(limit=SESSION_LIST_LIMIT)
                        web_sessions = _web_ui_sessions(sessions)
                        await manager.broadcast({
                            "type": "session_list",
                            "sessions": [
                                {"id": s["id"], "title": s["name"], "date": s["updated_at"], "messageCount": s["message_count"], "source": (s.get("metadata") or {}).get("source")}
                                for s in web_sessions
                            ]
                        })

                elif type == "new_session":
                    # Push command to main loop to create new session
                    from vaf.core.task_queue import TaskQueue
                    tq = TaskQueue()
                    tq.add(session_id="system", input_text="__CMD__:NEW_SESSION", source="web")
                    
                    # Create new session object AND SAVE IT IMMEDIATELY (temp, main loop will take over)
                    new_sess = session_mgr.new()
                    session_mgr.save(new_sess)
                    
                    # Subscribe this connection to the new session for scoped updates
                    manager.subscribe_to_session(websocket, new_sess.id)
                    
                    # Refresh list
                    sessions = session_mgr.list(limit=SESSION_LIST_LIMIT)
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
                    if sid and new_name:
                        session_mgr.rename(sid, new_name)
                        # Notify Main Loop to update in-memory object
                        from vaf.core.task_queue import TaskQueue
                        tq = TaskQueue()
                        tq.add(session_id="system", input_text=f"__CMD__:RENAME_SESSION:{sid}:{new_name}", source="web")
                        
                        # Broadcast update
                        sessions = session_mgr.list(limit=SESSION_LIST_LIMIT)
                        web_sessions = _web_ui_sessions(sessions)
                        await manager.broadcast({
                            "type": "session_list",
                            "sessions": [
                                {"id": s["id"], "title": s["name"], "date": s["updated_at"], "messageCount": s["message_count"], "source": (s.get("metadata") or {}).get("source")}
                                for s in web_sessions
                            ]
                        })

                elif type == "get_config":
                     # Send current config to frontend
                     cfg = Config.load()
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
                        existing = Config.load()
                        # Save config (Config.save will notify observers if critical keys changed)
                        Config.save(new_config)
                        provider_changed = existing.get("provider") != new_config.get("provider")

                        try:
                            if "tray_autostart" in new_config:
                                from vaf.core.platform import Platform
                                Platform.set_tray_autostart(bool(new_config.get("tray_autostart")))
                        except Exception as e:
                            log("WebServer", f"Tray autostart update failed: {e}")

                        # Use TaskQueue for commands (headless_runner only reads from TaskQueue)
                        # Priority 1 so RELOAD_CONFIG is processed before any pending chat (priority 10)
                        from vaf.core.task_queue import TaskQueue
                        tq = TaskQueue()
                        tq.add(session_id="system", input_text="__CMD__:RELOAD_CONFIG", source="web", priority=1)
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
                    if not session_id:
                        session_id = "web-default"
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

                elif type == "chat":
                    content = cmd.get("content")
                    files = cmd.get("files", [])  # List of file objects with {name, data, mimeType}
                    sidebar_docs_payload = cmd.get("sidebarDocuments") or []  # Document Viewer docs to inject into this turn

                    if content or files:
                        tray_context.register_activity()
                        # Learn from user input
                        if content:
                            get_autosuggest().learn(content)
                        
                        # Process files if attached
                        if files:
                            print(f"[WebUI] Processing {len(files)} attached file(s)...")
                            file_contents = await process_uploaded_files(files)
                            if file_contents:
                                # Append file contents to message (like CLI @filename behavior)
                                content = content + "\n\n" + file_contents if content else file_contents
                        
                        # Get session ID: prefer from message, then connection, then fallback
                        session_id = cmd.get("sessionId") or manager.get_session_for_connection(websocket)
                        if not session_id:
                            session_id = "web-default"

                        # Editor document: prepend to this turn so agent has current editor content (like Document Viewer)
                        editor_doc = cmd.get("editorDocument")
                        if editor_doc and isinstance(editor_doc, dict):
                            name = editor_doc.get("name") or "Document"
                            ed_content = editor_doc.get("content") or ""
                            if ed_content:
                                block = f"--- CURRENT DOCUMENT (Editor): {name} ---\n{ed_content}\n----------------\n\n"
                                content = (block + content) if content else block

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

                        # Ensure sidebar documents are in session before queueing (so headless has context)
                        if sidebar_docs_payload:
                            try:
                                contents = await process_files_to_sidebar_list(sidebar_docs_payload)
                                if contents:
                                    try:
                                        loaded = session_mgr.load(session_id)
                                    except FileNotFoundError:
                                        loaded = Session(
                                            id=session_id,
                                            name=f"Session {session_id}",
                                            runtime_state={"sidebar_documents": contents},
                                        )
                                        session_mgr.save(loaded, sync_state=False)
                                    else:
                                        if not getattr(loaded, "runtime_state", None):
                                            loaded.runtime_state = {}
                                        loaded.runtime_state["sidebar_documents"] = contents
                                        session_mgr.save(loaded, sync_state=False)
                                    log("WebServer", f"Injected {len(contents)} sidebar doc(s) for session {session_id} before chat")
                            except Exception as e:
                                log("WebServer", f"sidebar_documents on chat failed: {e}")

                        # Use TaskQueue for serialized execution
                        from vaf.core.task_queue import TaskQueue
                        tq = TaskQueue()
                        # user_scope_id is required for correct RAG scope (Auto-Recall and memory_save)
                        user_scope_id = manager.get_connection_user(websocket)
                        username = manager.get_connection_username(websocket)
                        metadata = {}
                        if user_scope_id:
                            metadata["user_scope_id"] = user_scope_id
                        if username:
                            metadata["username"] = username
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
                        try:
                            from vaf.core.thinking_mode import clear_waiting_for_reply, get_waiting_for_reply
                            reply_text = (content or "").strip() if get_waiting_for_reply(user_scope_id) else None
                            clear_waiting_for_reply(user_scope_id, user_reply_text=reply_text)
                        except Exception:
                            pass
                        tq.add(session_id=session_id, input_text=content, source="web", metadata=metadata)
                        try:
                            if is_debug_logging_enabled():
                                from datetime import datetime as _dt
                                qlog_dir = Path(os.environ.get("VAF_LOG_DIR", str(Path(__file__).resolve().parents[2] / "logs")))
                                qlog_dir.mkdir(parents=True, exist_ok=True)
                                qsize = tq.get_queue_size()
                                with open(qlog_dir / "queue.log", "a", encoding="utf-8") as f:
                                    f.write(f"{_dt.now().isoformat()} QUEUE_ADD session_id={session_id} preview={repr((content or '')[:60])} queue_size_after={qsize}\n")
                        except Exception:
                            pass
                        # Ack to console
                        file_info = f" [{len(files)} file(s)]" if files else ""
                        print(f"[WebUI] Queued input{file_info} for session {session_id}: {content[:50]}...")
                        try:
                            manager.log(
                                f"Queued input{file_info} for session {session_id}: {content[:50]}...",
                                level="info",
                                source="System",
                                session_id=session_id
                            )
                        except Exception:
                            pass

                elif type == "set_sidebar_documents":
                    session_id = cmd.get("sessionId") or manager.get_session_for_connection(websocket)
                    if not session_id:
                        session_id = "web-default"
                    documents = cmd.get("documents") or []
                    try:
                        if not documents:
                            loaded = session_mgr.load(session_id)
                            if not getattr(loaded, "runtime_state", None):
                                loaded.runtime_state = {}
                            loaded.runtime_state["sidebar_documents"] = []
                            session_mgr.save(loaded, sync_state=False)
                            await websocket.send_json({
                                "type": "sidebar_documents_set",
                                "contents": [],
                                "sessionId": session_id
                            })
                        else:
                            contents = await process_files_to_sidebar_list(documents)
                            loaded = session_mgr.load(session_id)
                            if not getattr(loaded, "runtime_state", None):
                                loaded.runtime_state = {}
                            loaded.runtime_state["sidebar_documents"] = contents
                            session_mgr.save(loaded, sync_state=False)
                            await websocket.send_json({
                                "type": "sidebar_documents_set",
                                "contents": contents,
                                "sessionId": session_id
                            })
                    except FileNotFoundError:
                        new_sess = Session(
                            id=session_id,
                            name=f"Session {session_id}",
                            runtime_state={"sidebar_documents": contents}
                        )
                        session_mgr.save(new_sess, sync_state=False)
                        await websocket.send_json({
                            "type": "sidebar_documents_set",
                            "contents": contents,
                            "sessionId": session_id
                        })
                    except Exception as e:
                        log("WebServer", f"set_sidebar_documents failed: {e}")
                        await websocket.send_json({
                            "type": "sidebar_documents_set",
                            "contents": [],
                            "sessionId": session_id,
                            "error": str(e)
                        })

                elif type == "get_tools":
                    # Return list of available tools from agent
                    try:
                        # Use registered agent instance from manager
                        agent = manager.agent_instance
                        if agent and hasattr(agent, 'tools'):
                            tools_list = [
                                {
                                    "name": name,
                                    "description": getattr(tool, 'description', 'No description'),
                                    "category": getattr(tool, 'category', 'general')
                                }
                                for name, tool in agent.tools.items()
                            ]
                            # Update cache
                            manager.tools_cache = tools_list
                            await websocket.send_json({
                                "type": "tools_list",
                                "tools": tools_list
                            })
                        elif manager.tools_cache:
                            await websocket.send_json({
                                "type": "tools_list",
                                "tools": manager.tools_cache
                            })
                        else:
                            await websocket.send_json({
                                "type": "tools_list",
                                "tools": _scan_tool_modules()
                            })
                    except Exception as e:
                        await websocket.send_json({
                            "type": "tools_list",
                            "tools": [],
                            "error": str(e)
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
                    # Return list of saved automations (user-scoped when user_scope_id present)
                    try:
                        from vaf.core.automation import AutomationManager
                        user_scope_id = manager.get_connection_user(websocket) if manager else None
                        mgr = AutomationManager(user_scope_id=user_scope_id) if user_scope_id else AutomationManager()
                        tasks = list(mgr.list())
                        # Always merge root automations so UI matches what the agent sees (list_automations uses root).
                        # E.g. "Daily calendar check" lives in root; without this, non-local-admin or wrong scope showed empty.
                        if user_scope_id:
                            root_mgr = AutomationManager()
                            root_tasks = root_mgr.list()
                            seen = {t.id for t in tasks}
                            for t in root_tasks:
                                if t.id not in seen:
                                    tasks.append(t)
                                    seen.add(t.id)
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
                    tq = TaskQueue()
                    session_id = cmd.get("sessionId") or manager.get_session_for_connection(websocket)
                    if session_id:
                        tq.request_stop(session_id)
                        log("WebServer", f"Stop requested for session {session_id}")
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
            with tempfile.NamedTemporaryFile(prefix="vaf_", suffix=file_ext, delete=False) as temp_file:
                temp_file.write(decoded_data)
                temp_path = temp_file.name

            try:
                from vaf.tools.librarian import LibrarianTool
                librarian = LibrarianTool()
                content = librarian._read_file(Path(temp_path), enable_chunking=True)
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
            err_name = file_obj.get("name", "unknown")
            if _is_temp_like_filename(err_name):
                err_name = "Dokument" + (Path(err_name).suffix or ".txt")
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

    if tls_enabled and ssl_cert and ssl_key and os.path.isfile(ssl_cert) and os.path.isfile(ssl_key):
        config = uvicorn.Config(
            app=app, host=host, port=port, loop="asyncio", log_level="error",
            ssl_certfile=ssl_cert, ssl_keyfile=ssl_key
        )
        log("WebServer", f"Starting with TLS (HTTPS/WSS) on {host}:{port}")
    else:
        config = uvicorn.Config(app=app, host=host, port=port, loop="asyncio", log_level="error")
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
