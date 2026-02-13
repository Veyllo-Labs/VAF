"""
WhatsApp Integration API Routes

Handles WhatsApp bridge start/stop, QR display for linking, status, and whitelist management.
"""
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
                    log_whatsapp_qr(f"[VAF] Received: connected selfJid={obj.get('selfJid', '')}")
                    with _qr_lock:
                        _qr_state[username] = {"connected": True, "ts": __import__("time").time()}
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
        return {"status": "connected", "message": "WhatsApp linked successfully."}
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
