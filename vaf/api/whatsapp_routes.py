"""
WhatsApp Integration API Routes

Handles WhatsApp bridge start/stop, QR display for linking, status, and whitelist management.
"""
import json
import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from vaf.core.config import Config

logger = logging.getLogger("vaf.api.whatsapp")

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])

# QR state per username: { "qr": base64_data, "ts": time }
_qr_state: Dict[str, Dict[str, Any]] = {}
# QR process per username (to terminate before starting a new one)
_qr_procs: Dict[str, subprocess.Popen] = {}
_qr_lock = threading.Lock()


def _jid_to_phone(jid: str) -> str:
    """Extract E.164 phone from WhatsApp JID. Skips @lid, validates length 7-15."""
    if not jid or not isinstance(jid, str):
        return ""
    if "@lid" in jid or jid.endswith("@broadcast") or jid.endswith("@status"):
        return ""
    part = jid.split("@")[0].split(":")[0].strip()
    if not part or not part.isdigit() or len(part) < 7 or len(part) > 15:
        return ""
    return f"+{part}"


def _normalize_phone(phone: str) -> str:
    """Normalize phone to digits only for comparison."""
    return "".join(c for c in (phone or "") if c.isdigit())


def get_current_vaf_user(request: Request) -> Dict[str, str]:
    """Return user_scope_id and username for the current request."""
    user = getattr(request.state, "user", None)
    if user and user.get("user_scope_id") and user.get("username"):
        return {
            "user_scope_id": str(user["user_scope_id"]),
            "username": user.get("username", "admin"),
        }
    return {
        "user_scope_id": Config.get("local_admin_scope_id", "00000000-0000-0000-0000-000000000001"),
        "username": Config.get("local_admin_username", "admin"),
    }


class WhitelistAddRequest(BaseModel):
    phone_number: str
    vaf_username: Optional[str] = None
    user_scope_id: Optional[str] = None


class ConfigUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    whitelist: Optional[list] = None


@router.get("/dashboard/debug")
async def get_whatsapp_dashboard_debug(request: Request):
    """Debug: raw_chats count from bridge. Helps diagnose empty chat list."""
    from vaf.api.whatsapp_bridge import get_connection_status, get_whatsapp_chats, is_bridge_running

    user_info = get_current_vaf_user(request)
    username = user_info["username"]
    raw_chats = get_whatsapp_chats(username, wait_timeout=3.0)
    return {
        "bridge_running": is_bridge_running(),
        "raw_chats_count": len(raw_chats),
        "username": username,
    }


@router.get("/dashboard")
async def get_whatsapp_dashboard(request: Request):
    """Data for the WhatsApp dashboard: status, sessions, activity, stats, whitelist. No sensitive data."""
    import time as _time
    from typing import Any, Dict

    from vaf.api.whatsapp_bridge import get_connection_status, get_whatsapp_chats, is_bridge_running
    from vaf.core.whatsapp_auth import whatsapp_auth_exists

    user_info = get_current_vaf_user(request)
    username = user_info["username"]
    whatsapp_config = Config.get("whatsapp_config") or {}
    if not isinstance(whatsapp_config, dict):
        whatsapp_config = {}

    whitelist = whatsapp_config.get("whitelist") or []
    whitelist = [e for e in whitelist if isinstance(e, dict) and e.get("phone_number")]

    current_linked = whatsapp_auth_exists(username)
    any_whitelist_linked = any(
        whatsapp_auth_exists((e.get("vaf_username") or "admin").strip())
        for e in whitelist if isinstance(e, dict)
    )
    linked = current_linked or any_whitelist_linked

    activity = list(whatsapp_config.get("chat_activity") or [])[-100:]

    def _phone_to_session_id(phone: str, vaf_username: str) -> str:
        digits = "".join(c for c in phone if c.isdigit())
        uname = (vaf_username or "admin").strip()
        return f"whatsapp_{uname}_{digits}"

    whitelist_by_phone: Dict[str, Dict[str, Any]] = {}
    for e in whitelist:
        phone = (e.get("phone_number") or "").strip()
        if not phone:
            continue
        chat_id = phone if phone.startswith("+") else f"+{phone}"
        whitelist_by_phone[chat_id] = e

    sessions_by_chat: Dict[str, Dict[str, Any]] = {}
    raw_chats = get_whatsapp_chats(username, wait_timeout=3.0)
    for c in raw_chats:
        jid = c.get("jid") or c.get("phone") or ""
        if not jid:
            continue
        is_group = c.get("is_group", False) or "@g.us" in str(jid)
        if is_group:
            chat_id = jid
            phone = jid
        else:
            phone = c.get("phone") or _jid_to_phone(jid)
            chat_id = phone if phone and phone.startswith("+") else _jid_to_phone(jid) if jid else ""
        if not chat_id:
            continue
        vaf_username = username
        wl_entry = whitelist_by_phone.get(chat_id)
        if wl_entry:
            vaf_username = (wl_entry.get("vaf_username") or "admin").strip()
        stype = "admin" if chat_id in whitelist_by_phone else "contact"
        sessions_by_chat[chat_id] = {
            "chat_id": chat_id,
            "phone_number": phone or chat_id,
            "vaf_username": vaf_username,
            "session_id": _phone_to_session_id(phone or chat_id, vaf_username),
            "type": stype,
            "name": c.get("name"),
            "last_ts": int(c.get("last_ts") or 0),
            "message_count": 0,
        }
    for a in activity:
        cid = str(a.get("chat_id") or "")
        if not cid:
            continue
        if cid not in sessions_by_chat:
            digits = "".join(c for c in cid if c.isdigit())
            sessions_by_chat[cid] = {
                "chat_id": cid,
                "phone_number": cid,
                "vaf_username": username,
                "session_id": f"whatsapp_{username}_{digits}",
                "type": "contact",
                "name": None,
                "last_ts": 0,
                "message_count": 0,
            }
        rec = sessions_by_chat[cid]
        ts = a.get("ts") or 0
        rec["last_ts"] = max(rec.get("last_ts") or 0, int(ts))
        rec["message_count"] = rec.get("message_count", 0) + 1
    for e in whitelist:
        phone = (e.get("phone_number") or "").strip()
        if not phone:
            continue
        chat_id = phone if phone.startswith("+") else f"+{phone}"
        if chat_id not in sessions_by_chat:
            vaf_username = (e.get("vaf_username") or "admin").strip()
            sessions_by_chat[chat_id] = {
                "chat_id": chat_id,
                "phone_number": phone,
                "vaf_username": vaf_username,
                "session_id": _phone_to_session_id(phone, vaf_username),
                "type": "admin",
                "name": None,
                "last_ts": 0,
                "message_count": 0,
            }
    for rec in sessions_by_chat.values():
        rec.setdefault("last_ts", 0)
        rec.setdefault("message_count", 0)
    sessions = sorted(sessions_by_chat.values(), key=lambda s: (s.get("last_ts") or 0), reverse=True)

    bucket_seconds = 4 * 3600
    now_ts = int(_time.time())
    cutoff = now_ts - 7 * 24 * 3600
    buckets: Dict[int, int] = {}
    for t in range(int(cutoff // bucket_seconds) * bucket_seconds, now_ts + 1, bucket_seconds):
        buckets[t] = 0
    for a in activity:
        ts = a.get("ts") or 0
        bucket_ts = (int(ts) // bucket_seconds) * bucket_seconds
        if bucket_ts in buckets:
            buckets[bucket_ts] += 1
    stats_4h = [{"bucket_ts": ts, "count": c} for ts, c in sorted(buckets.items())]

    running = is_bridge_running()
    connected = get_connection_status(username, wait_timeout=2.0) if running else False

    try:
        from vaf.core.log_helper import get_app_log_dir
        log_path = str(get_app_log_dir() / "whatsapp_qr.log")
    except Exception:
        log_path = "logs/whatsapp_qr.log"

    return {
        "configured": bool(whitelist) and linked,
        "linked": linked,
        "running": running,
        "connected": connected,
        "enabled": whatsapp_config.get("enabled", False),
        "username": username,
        "sessions": sessions,
        "stats_4h": stats_4h,
        "activity": activity,
        "log_path": log_path,
        "whitelist": [
            {"phone_number": e.get("phone_number", ""), "vaf_username": e.get("vaf_username")}
            for e in whitelist
        ],
    }


@router.get("/status")
async def get_whatsapp_status(request: Request):
    """Get WhatsApp bridge status and per-user linked state."""
    from vaf.api.whatsapp_bridge import is_bridge_running
    from vaf.core.whatsapp_auth import whatsapp_auth_exists

    user_info = get_current_vaf_user(request)
    username = user_info["username"]
    whatsapp_config = Config.get("whatsapp_config") or {}
    if not isinstance(whatsapp_config, dict):
        whatsapp_config = {}

    linked = whatsapp_auth_exists(username)
    running = is_bridge_running()

    return {
        "enabled": whatsapp_config.get("enabled", False),
        "running": running,
        "linked": linked,
        "username": username,
    }


@router.post("/start")
async def start_whatsapp_bridge():
    """Start the WhatsApp bridge."""
    from vaf.api.whatsapp_bridge import is_bridge_running, start_bridge

    whatsapp_config = Config.get("whatsapp_config") or {}
    if not isinstance(whatsapp_config, dict) or not whatsapp_config.get("enabled"):
        raise HTTPException(status_code=400, detail="WhatsApp not enabled. Enable in Settings -> Connections.")

    if is_bridge_running():
        return {"status": "started", "message": "WhatsApp bridge already running."}

    if start_bridge():
        return {"status": "started", "message": "WhatsApp bridge started."}
    raise HTTPException(status_code=500, detail="Failed to start WhatsApp bridge. Ensure Node.js is installed and npm install was run in vaf/whatsapp_node.")


@router.post("/stop")
async def stop_whatsapp_bridge():
    """Stop the WhatsApp bridge."""
    from vaf.api.whatsapp_bridge import is_bridge_running, stop_bridge

    if is_bridge_running():
        stop_bridge()
    return {"status": "stopped", "message": "WhatsApp bridge stopped."}


@router.post("/restart")
async def restart_whatsapp_bridge():
    """Restart the WhatsApp bridge (stop, wait for shutdown, start). Use when 'Restart bridge' doesn't reconnect."""
    from vaf.api.whatsapp_bridge import restart_bridge

    whatsapp_config = Config.get("whatsapp_config") or {}
    if not isinstance(whatsapp_config, dict) or not whatsapp_config.get("enabled"):
        raise HTTPException(status_code=400, detail="WhatsApp not enabled. Enable in Settings -> Connections.")
    if restart_bridge():
        return {"status": "restarted", "message": "WhatsApp bridge restarted. Wait 20-30 s, then refresh."}
    raise HTTPException(status_code=500, detail="Failed to restart bridge. Check Node.js and npm install in vaf/whatsapp_node.")


def _get_whatsapp_compaction_info(session_id: str) -> tuple:
    """Return (last_compaction_at_turn, compaction_interval) for a session."""
    from vaf.core.config import Config

    interval = int(Config.get("memory_compaction_interval", 15))
    try:
        from vaf.core.session import SessionManager
        _sm = SessionManager()
        _session = _sm.load(session_id)
        _runtime = getattr(_session, "runtime_state", None) or {}
        if "last_compaction_at_turn" in _runtime:
            last = int(_runtime["last_compaction_at_turn"])
            return (last, interval)
    except Exception:
        pass
    try:
        compaction_path = Config.APP_DIR / "compaction_state.json"
        if compaction_path.exists():
            with open(compaction_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            v = state.get(session_id)
            if isinstance(v, dict) and "turn" in v:
                return (int(v.get("turn", 0)), interval)
            if isinstance(v, (int, float)):
                return (int(v), interval)
    except Exception:
        pass
    return (0, interval)


@router.get("/session/{session_id}/history")
async def get_whatsapp_session_history(session_id: str):
    """Return message history and compaction stats for a WhatsApp session."""
    if not session_id.startswith("whatsapp_"):
        raise HTTPException(status_code=400, detail="Invalid session id")
    try:
        from vaf.core.session import SessionManager

        session_mgr = SessionManager()
        session = session_mgr.load(session_id)
        messages = [
            {"role": m.role, "content": (m.content or "")[:2000], "timestamp": getattr(m, "timestamp", None)}
            for m in (session.messages or [])
        ]
        runtime_state = getattr(session, "runtime_state", None) or {}
        user_turn_count = runtime_state.get("user_turn_count", 0)
        if user_turn_count == 0 and session.messages:
            user_turn_count = sum(1 for m in (session.messages or []) if getattr(m, "role", None) == "user")
        last_compaction_at_turn, compaction_interval = _get_whatsapp_compaction_info(session_id)
        return {
            "session_id": session_id,
            "messages": messages,
            "user_turn_count": user_turn_count,
            "compaction_interval": compaction_interval,
            "last_compaction_at_turn": last_compaction_at_turn,
        }
    except FileNotFoundError:
        last_compaction_at_turn, compaction_interval = _get_whatsapp_compaction_info(session_id)
        return {
            "session_id": session_id,
            "messages": [],
            "user_turn_count": 0,
            "compaction_interval": compaction_interval,
            "last_compaction_at_turn": last_compaction_at_turn,
        }
    except Exception as e:
        logger.exception("WhatsApp session history error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


def _run_qr_login(username: str) -> None:
    """Spawn Node process for QR login, capture QR to _qr_state."""
    from vaf.core.log_helper import log_whatsapp_qr
    from vaf.core.whatsapp_auth import get_whatsapp_auth_dir
    import shutil
    import json

    log_whatsapp_qr(f"[VAF] QR flow started for user={username}")
    auth_dir = get_whatsapp_auth_dir(username)
    auth_dir.mkdir(parents=True, exist_ok=True)
    node = shutil.which("node")
    wa_js = Path(__file__).resolve().parents[1] / "whatsapp_node" / "wa-bridge.js"
    if not node or not wa_js.exists():
        with _qr_lock:
            _qr_state[username] = {"error": "Node or wa-bridge.js not found. Install Node.js 18+ and run 'npm install' in vaf/whatsapp_node/.", "ts": 0}
        return
    kwargs = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "bufsize": 1,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(
            [node, str(wa_js), "--auth-dir", str(auth_dir.resolve())],
            **kwargs,
        )
        log_whatsapp_qr(f"[VAF] Node process spawned pid={proc.pid}")
        with _qr_lock:
            _qr_procs[username] = proc

        def _log_stderr():
            try:
                from vaf.core.log_helper import log_whatsapp_qr
                for line in (proc.stderr or []):
                    s = (line or "").strip()
                    if s:
                        log_whatsapp_qr(f"[stderr] {s}")
                        logger.warning("[wa-bridge] %s", s)
            except Exception:
                pass

        _stderr_thread = threading.Thread(target=_log_stderr, daemon=True)
        _stderr_thread.start()

        try:
            for line in proc.stdout:
                line = (line or "").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                typ = obj.get("type")
                if typ == "qr":
                    log_whatsapp_qr(f"[VAF] Received: qr (len={len(str(obj.get('qr') or ''))})")
                    qr_data = obj.get("qr", "")
                    with _qr_lock:
                        _qr_state[username] = {"qr": qr_data, "ts": __import__("time").time()}
                elif typ == "connected":
                    self_jid = obj.get("selfJid") or ""
                    phone = _jid_to_phone(self_jid)
                    log_whatsapp_qr(f"[VAF] Received: connected selfJid={self_jid} phone={phone}")
                    with _qr_lock:
                        _qr_state[username] = {"connected": True, "phone": phone, "ts": __import__("time").time()}
                    proc.terminate()
                    return
                elif typ == "error":
                    log_whatsapp_qr(f"[VAF] Received: error msg={obj.get('message', '')}")
                    with _qr_lock:
                        _qr_state[username] = {"error": obj.get("message", "Unknown error"), "ts": __import__("time").time()}
            if proc.poll() is not None:
                with _qr_lock:
                    s = _qr_state.get(username, {})
                    if not s.get("connected") and not s.get("error"):
                        code = proc.returncode or -1
                        log_whatsapp_qr(f"[VAF] Process exited without connected/error code={code}")
                        _qr_state[username] = {"error": f"Process exited (code {code}). Check logs/whatsapp_qr.log for details.", "ts": __import__("time").time()}
        finally:
            with _qr_lock:
                _qr_procs.pop(username, None)
    except Exception as e:
        with _qr_lock:
            _qr_state[username] = {"error": str(e), "ts": 0}
        with _qr_lock:
            _qr_procs.pop(username, None)


@router.get("/qr/log-path")
async def get_qr_log_path(request: Request):
    """Return path to whatsapp_qr.log for debugging."""
    from vaf.core.log_helper import get_app_log_dir
    return {"path": str(get_app_log_dir() / "whatsapp_qr.log")}


@router.get("/qr")
async def get_qr_code(request: Request):
    """Get current QR code for linking (or status). Poll until connected."""
    user_info = get_current_vaf_user(request)
    username = user_info["username"]

    with _qr_lock:
        state = _qr_state.get(username, {})

    if state.get("connected"):
        phone = state.get("phone") or ""
        return {"status": "connected", "message": "WhatsApp linked successfully.", "phone": phone}
    if state.get("error"):
        return {"status": "error", "error": state["error"]}
    if state.get("qr"):
        return {"status": "qr", "qr": state["qr"]}
    return {"status": "waiting", "message": "Start QR flow from Settings -> Connections."}


@router.post("/qr/reset")
async def reset_whatsapp_auth(request: Request):
    """Clear WhatsApp auth for current user. Use when 'Logged out' to allow a fresh QR scan."""
    import shutil

    from vaf.core.whatsapp_auth import get_whatsapp_auth_dir

    user_info = get_current_vaf_user(request)
    username = user_info["username"]
    auth_dir = get_whatsapp_auth_dir(username)
    removed = 0
    if auth_dir.exists():
        for p in auth_dir.iterdir():
            try:
                if p.is_file():
                    p.unlink()
                    removed += 1
                elif p.is_dir():
                    shutil.rmtree(p)
                    removed += 1
            except OSError:
                pass
    return {"status": "reset", "message": "Auth cleared. You can start a new QR flow."}


@router.post("/qr/start")
async def start_qr_flow(request: Request):
    """Start QR login flow for current user. QR will appear in /qr poll."""
    user_info = get_current_vaf_user(request)
    username = user_info["username"]

    with _qr_lock:
        old_proc = _qr_procs.pop(username, None)
        _qr_state.pop(username, None)
    if old_proc is not None and old_proc.poll() is None:
        try:
            old_proc.terminate()
            old_proc.wait(timeout=3)
        except Exception:
            pass

    t = threading.Thread(target=_run_qr_login, args=(username,), daemon=True)
    t.start()
    return {"status": "started", "message": "Scan the QR code in WhatsApp (Linked Devices). Poll GET /api/whatsapp/qr for the QR."}


@router.post("/whitelist/remove")
async def remove_whitelist_entry(request: Request, body: WhitelistAddRequest):
    """Remove a whitelist entry by phone number."""
    phone = (body.phone_number or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone_number required")
    config = Config.load()
    wc = config.get("whatsapp_config") or {}
    if not isinstance(wc, dict):
        wc = {}
    whitelist = [e for e in (wc.get("whitelist") or []) if isinstance(e, dict) and str(e.get("phone_number", "")).strip() != phone]
    wc["whitelist"] = whitelist
    config["whatsapp_config"] = wc
    Config.save(config)
    return {"status": "removed", "message": "Whitelist entry removed.", "whitelist_count": len(whitelist)}


@router.post("/whitelist/add")
async def add_whitelist_entry(request: Request, body: WhitelistAddRequest):
    """Add a whitelist entry for WhatsApp (phone_number -> user)."""
    user_info = get_current_vaf_user(request)
    phone = (body.phone_number or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone_number required")
    vaf_username = (body.vaf_username or user_info["username"]).strip()
    user_scope_id = body.user_scope_id or user_info["user_scope_id"]

    config = Config.load()
    wc = config.get("whatsapp_config") or {}
    if not isinstance(wc, dict):
        wc = {"enabled": wc.get("enabled", False) if isinstance(wc, dict) else False, "whitelist": []}
    whitelist = list(wc.get("whitelist") or [])
    for i, e in enumerate(whitelist):
        if isinstance(e, dict) and (
            str(e.get("user_scope_id")) == str(user_scope_id) or e.get("vaf_username") == vaf_username
        ):
            whitelist[i] = {**e, "phone_number": phone, "user_scope_id": user_scope_id, "vaf_username": vaf_username}
            wc["whitelist"] = whitelist
            config["whatsapp_config"] = wc
            Config.save(config)
            return {"status": "updated", "message": "Whitelist entry updated."}
    whitelist.append({
        "phone_number": phone,
        "user_scope_id": user_scope_id,
        "vaf_username": vaf_username,
    })
    wc["whitelist"] = whitelist
    if "enabled" not in wc:
        wc["enabled"] = True
    config["whatsapp_config"] = wc
    Config.save(config)
    return {"status": "added", "message": "Whitelist entry added."}


@router.get("/config")
async def get_whatsapp_config(request: Request):
    """Get WhatsApp config (for UI)."""
    user_info = get_current_vaf_user(request)
    whatsapp_config = Config.get("whatsapp_config") or {}
    if not isinstance(whatsapp_config, dict):
        whatsapp_config = {}
    return {
        "enabled": whatsapp_config.get("enabled", False),
        "whitelist": whatsapp_config.get("whitelist", []),
    }
