"""
Telegram Integration API Routes

Handles Telegram bot setup, verification, whitelist (per-user), and bridge management.
All responses and user-facing messages in English.
"""
import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Optional, Any, Dict, List

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel

from vaf.core.config import Config

logger = logging.getLogger("vaf.api.telegram")

router = APIRouter(prefix="/api/telegram", tags=["telegram"])

# Global state for verification process (bot token check + code verification)
_verification_state: Dict[str, Any] = {
    "pending_code": None,
    "verified": False,
    "telegram_user_id": None,
    "telegram_username": None,
    "error": None,
    "bot_running": False,
    "bot_thread": None,
}


def get_current_vaf_user(request: Request) -> Dict[str, str]:
    """Return user_scope_id and username for the current request (auth or local admin)."""
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


class StartVerificationRequest(BaseModel):
    bot_token: str
    verification_code: str


class WhitelistAddRequest(BaseModel):
    telegram_user_id: str
    telegram_username: Optional[str] = None


@router.post("/start-verification")
async def start_verification(request: StartVerificationRequest):
    """
    Start the Telegram bot and wait for verification code from user (DM to bot).
    On success, verification state contains telegram_user_id and telegram_username.
    """
    global _verification_state

    _verification_state = {
        "pending_code": request.verification_code,
        "verified": False,
        "telegram_user_id": None,
        "telegram_username": None,
        "error": None,
        "bot_running": False,
        "bot_thread": None,
    }

    try:
        try:
            from telegram import Update
            from telegram.ext import Application, ContextTypes, MessageHandler, filters
        except ImportError:
            raise HTTPException(
                status_code=500,
                detail="python-telegram-bot not installed. Run: pip install python-telegram-bot",
            )

        token = request.bot_token.strip()
        code = request.verification_code.strip()

        # Reject if they pasted the verification instructions instead of the bot token
        if not token or len(token) > 100 or "Waiting for verification" in token or "Send the code" in token or "to your bot" in token:
            raise HTTPException(
                status_code=400,
                detail="That looks like the verification instructions, not your bot token. In step 2 (Enter Token), paste only the token from BotFather (e.g. 123456789:ABC...). Do not paste the text from this step.",
            )

        def run_verification_bot():
            global _verification_state
            application = Application.builder().token(token).build()

            async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
                global _verification_state
                if not update.message or not update.message.text:
                    return
                if update.message.text.strip() != _verification_state.get("pending_code"):
                    await update.message.reply_text("Invalid verification code. Please check and try again.")
                    return
                user = update.effective_user
                if user:
                    _verification_state["verified"] = True
                    _verification_state["telegram_user_id"] = str(user.id)
                    _verification_state["telegram_username"] = (user.username or user.first_name or "").strip() or f"user_{user.id}"
                    await update.message.reply_text(
                        "Verification successful! You can now add this Telegram to the whitelist in the wizard."
                    )
                    await application.stop()

            application.add_handler(
                MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, handle_message)
            )

            try:
                application.run_polling(allowed_updates=Update.ALL_TYPES)
            except Exception as e:
                err_msg = str(e)
                if "rejected" in err_msg.lower() or "invalid token" in err_msg.lower():
                    _verification_state["error"] = (
                        "Invalid bot token. Paste only the token from BotFather in step 2 (Enter Token), "
                        "not the verification instructions from this page."
                    )
                else:
                    _verification_state["error"] = err_msg
                logger.exception("Telegram verification bot error")

        _verification_state["bot_running"] = True
        thread = threading.Thread(target=run_verification_bot, daemon=True)
        thread.start()
        _verification_state["bot_thread"] = thread

        await asyncio.sleep(2)
        if _verification_state.get("error"):
            raise HTTPException(status_code=400, detail=_verification_state["error"])

        return {"status": "waiting", "message": "Bot started. Send the verification code to the bot in Telegram (DM)."}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to start Telegram verification")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/verification-status")
async def get_verification_status():
    """Return current verification state (verified, telegram_user_id, telegram_username, error)."""
    return {
        "verified": _verification_state.get("verified", False),
        "telegram_user_id": _verification_state.get("telegram_user_id"),
        "telegram_username": _verification_state.get("telegram_username"),
        "error": _verification_state.get("error"),
        "bot_running": _verification_state.get("bot_running", False),
    }


@router.post("/whitelist-add")
async def whitelist_add(
    body: WhitelistAddRequest,
    request: Request,
    current_user: Dict[str, str] = Depends(get_current_vaf_user),
):
    """
    Add one whitelist entry linking a Telegram user to the current VAF user.
    user_scope_id and username come from request (auth or local_admin).
    """
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        telegram_config = {}
    whitelist: List[Dict[str, Any]] = list(telegram_config.get("whitelist") or [])

    # Avoid duplicate telegram_user_id
    telegram_user_id = body.telegram_user_id.strip()
    whitelist = [e for e in whitelist if str(e.get("telegram_user_id")) != telegram_user_id]

    entry = {
        "telegram_user_id": telegram_user_id,
        "telegram_username": (body.telegram_username or "").strip() or None,
        "user_scope_id": current_user["user_scope_id"],
        "vaf_username": current_user["username"],
    }
    whitelist.append(entry)

    config = Config.load()
    if "telegram_config" not in config or not isinstance(config["telegram_config"], dict):
        config["telegram_config"] = {}
    config["telegram_config"]["whitelist"] = whitelist
    Config.save(config)

    return {"status": "ok", "whitelist_count": len(whitelist)}


@router.get("/status")
async def get_telegram_status():
    """Return configured, enabled, running, and whitelist count (no sensitive data)."""
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        telegram_config = {}
    whitelist = telegram_config.get("whitelist") or []
    running = False
    try:
        from vaf.api.telegram_bridge import is_bridge_running
        running = is_bridge_running()
    except Exception:
        pass
    return {
        "configured": bool(telegram_config.get("bot_token") and telegram_config.get("verified")),
        "enabled": bool(telegram_config.get("enabled")),
        "running": running,
        "whitelist_count": len(whitelist),
    }


def _get_bot_username() -> Optional[str]:
    """Return bot username from Telegram getMe (cached in config or fetched). No token in response."""
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        return None
    cached = telegram_config.get("bot_username")
    if cached:
        return cached
    token = (telegram_config.get("bot_token") or "").strip()
    if not token:
        return None
    try:
        import requests
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
        if r.ok:
            data = r.json()
            username = (data.get("result") or {}).get("username")
            if username:
                config = Config.load()
                if "telegram_config" not in config or not isinstance(config["telegram_config"], dict):
                    config["telegram_config"] = {}
                config["telegram_config"]["bot_username"] = username
                Config.save(config)
                return username
    except Exception:
        pass
    return None


@router.get("/dashboard")
async def get_telegram_dashboard():
    """
    Data for the Telegram settings dashboard: bot link, sessions (chats for this bot only),
    admin whitelist, relay whitelist, activity. No sensitive data (no tokens).
    Sessions = one per Telegram user who can chat with our bot (from whitelist + relay), with last_ts and count.
    """
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        telegram_config = {}
    bot_username = _get_bot_username()
    bot_link = f"https://t.me/{bot_username}" if bot_username else None
    admin_whitelist = list(telegram_config.get("whitelist") or [])
    relay_whitelist = list(telegram_config.get("relay_whitelist") or [])
    activity = list(telegram_config.get("chat_activity") or [])[-100:]

    # Sessions: one per chat (our bot only = whitelist + relay). Enrich with last_ts and message_count from activity.
    sessions_by_chat: Dict[str, Dict[str, Any]] = {}
    for e in admin_whitelist:
        uid = str(e.get("telegram_user_id") or "")
        if not uid:
            continue
        # Private chat: chat_id == telegram_user_id
        sessions_by_chat[uid] = {
            "chat_id": uid,
            "telegram_user_id": uid,
            "telegram_username": e.get("telegram_username"),
            "vaf_username": e.get("vaf_username"),
            "type": "admin",
        }
    for e in relay_whitelist:
        uid = str(e.get("telegram_user_id") or "")
        if not uid:
            continue
        if uid not in sessions_by_chat:
            sessions_by_chat[uid] = {
                "chat_id": uid,
                "telegram_user_id": uid,
                "telegram_username": e.get("telegram_username"),
                "vaf_username": e.get("vaf_username"),
                "type": "relay",
            }
    for a in activity:
        cid = str(a.get("chat_id") or "")
        if not cid:
            continue
        if cid not in sessions_by_chat:
            sessions_by_chat[cid] = {
                "chat_id": cid,
                "telegram_user_id": cid,
                "telegram_username": None,
                "vaf_username": None,
                "type": "unknown",
            }
        rec = sessions_by_chat[cid]
        ts = a.get("ts") or 0
        rec["last_ts"] = max(rec.get("last_ts") or 0, ts)
        rec["message_count"] = rec.get("message_count", 0) + 1
    for rec in sessions_by_chat.values():
        rec.setdefault("last_ts", 0)
        rec.setdefault("message_count", 0)
    sessions = sorted(sessions_by_chat.values(), key=lambda s: (s.get("last_ts") or 0), reverse=True)

    # Stats: messages per 4-hour bucket (last 7 days). Bucket key = floor(ts / 14400) * 14400 (4h = 14400s).
    import time as _time
    bucket_seconds = 4 * 3600
    now_ts = int(_time.time())
    buckets: Dict[int, int] = {}
    for i in range(42):  # 7 days * 6 buckets per day
        bucket_ts = ((now_ts - (41 - i) * bucket_seconds) // bucket_seconds) * bucket_seconds
        buckets[bucket_ts] = 0
    for a in activity:
        ts = a.get("ts") or 0
        bucket_ts = (int(ts) // bucket_seconds) * bucket_seconds
        if bucket_ts in buckets:
            buckets[bucket_ts] += 1
    stats_4h = [{"bucket_ts": ts, "count": c} for ts, c in sorted(buckets.items())]

    return {
        "bot_username": bot_username,
        "bot_link": bot_link,
        "sessions": sessions,
        "stats_4h": stats_4h,
        "admin_whitelist": [{"telegram_user_id": e.get("telegram_user_id"), "telegram_username": e.get("telegram_username"), "vaf_username": e.get("vaf_username")} for e in admin_whitelist],
        "relay_whitelist": [{"telegram_user_id": e.get("telegram_user_id"), "telegram_username": e.get("telegram_username"), "vaf_username": e.get("vaf_username")} for e in relay_whitelist],
        "activity": activity,
    }


class RelayWhitelistAddRequest(BaseModel):
    telegram_user_id: str
    telegram_username: Optional[str] = None


@router.post("/relay-whitelist-add")
async def relay_whitelist_add(request: Request, body: RelayWhitelistAddRequest):
    """Add a contact who can only relay messages to the main user (no tools, safe replies only)."""
    current_user = get_current_vaf_user(request)
    telegram_user_id = (body.telegram_user_id or "").strip()
    if not telegram_user_id:
        raise HTTPException(status_code=400, detail="telegram_user_id required")
    config = Config.load()
    telegram_config = config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        telegram_config = {}
    relay_whitelist = [e for e in (telegram_config.get("relay_whitelist") or []) if str(e.get("telegram_user_id")) != telegram_user_id]
    relay_whitelist.append({
        "telegram_user_id": telegram_user_id,
        "telegram_username": (body.telegram_username or "").strip() or None,
        "user_scope_id": current_user["user_scope_id"],
        "vaf_username": current_user["username"],
    })
    telegram_config["relay_whitelist"] = relay_whitelist
    config["telegram_config"] = telegram_config
    Config.save(config)
    return {"status": "ok", "relay_whitelist_count": len(relay_whitelist)}


def _get_compaction_info(session_id: str) -> tuple:
    """Return (last_compaction_at_turn, compaction_interval) for session."""
    interval = int(Config.get("memory_compaction_interval", 15))
    last = 0

    # First try session.runtime_state (preferred, persistent)
    try:
        from vaf.core.session import SessionManager
        _sm = SessionManager()
        _session = _sm.load(session_id)
        _runtime = getattr(_session, 'runtime_state', None) or {}
        if "last_compaction_at_turn" in _runtime:
            last = int(_runtime["last_compaction_at_turn"])
            return (last, interval)
    except Exception:
        pass

    # Fallback to compaction_state.json
    try:
        path = Path(Config.APP_DIR) / "compaction_state.json"
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            v = state.get(session_id)
            if isinstance(v, dict) and "turn" in v:
                last = int(v.get("turn", 0))
            elif isinstance(v, (int, float)):
                last = int(v)
    except Exception:
        pass
    return (last, interval)


@router.get("/session/{session_id}/history")
async def get_telegram_session_history(session_id: str):
    """Return message history and compaction stats for a Telegram session (session_id must start with 'telegram_')."""
    if not session_id.startswith("telegram_"):
        raise HTTPException(status_code=400, detail="Invalid session id")
    try:
        from vaf.core.session import SessionManager
        session_mgr = SessionManager()
        session = session_mgr.load(session_id)
        messages = [{"role": m.role, "content": (m.content or "")[:2000], "timestamp": getattr(m, "timestamp", None)} for m in (session.messages or [])]
        # Use PERSISTENT user_turn_count from runtime_state (not from compressed messages)
        runtime_state = getattr(session, 'runtime_state', None) or {}
        user_turn_count = runtime_state.get("user_turn_count", 0)
        # Fallback: if runtime_state has no count, compute from messages (for old sessions)
        if user_turn_count == 0 and session.messages:
            user_turn_count = sum(1 for m in (session.messages or []) if getattr(m, "role", None) == "user")
        last_compaction_at_turn, compaction_interval = _get_compaction_info(session_id)
        return {
            "session_id": session_id,
            "messages": messages,
            "user_turn_count": user_turn_count,
            "compaction_interval": compaction_interval,
            "last_compaction_at_turn": last_compaction_at_turn,
        }
    except FileNotFoundError:
        last_compaction_at_turn, compaction_interval = _get_compaction_info(session_id)
        return {
            "session_id": session_id,
            "messages": [],
            "user_turn_count": 0,
            "compaction_interval": compaction_interval,
            "last_compaction_at_turn": last_compaction_at_turn,
        }
    except Exception as e:
        logger.exception("Session history error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/relay-whitelist-remove")
async def relay_whitelist_remove(request: Request, body: WhitelistAddRequest):
    """Remove a contact from the relay whitelist."""
    telegram_user_id = (body.telegram_user_id or "").strip()
    if not telegram_user_id:
        raise HTTPException(status_code=400, detail="telegram_user_id required")
    config = Config.load()
    telegram_config = config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        telegram_config = {}
    relay_whitelist = [e for e in (telegram_config.get("relay_whitelist") or []) if str(e.get("telegram_user_id")) != telegram_user_id]
    telegram_config["relay_whitelist"] = relay_whitelist
    config["telegram_config"] = telegram_config
    Config.save(config)
    return {"status": "ok", "relay_whitelist_count": len(relay_whitelist)}


@router.post("/start")
async def start_telegram_bridge():
    """Start the Telegram bridge with saved configuration."""
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        telegram_config = {}
    if not telegram_config.get("verified"):
        raise HTTPException(status_code=400, detail="Telegram not configured. Please complete setup first.")
    if not telegram_config.get("bot_token"):
        raise HTTPException(status_code=400, detail="Bot token missing.")

    try:
        from vaf.api.telegram_bridge import start_bridge
        if start_bridge():
            return {"status": "started", "message": "Telegram bridge started."}
    except Exception as e:
        logger.exception("Failed to start Telegram bridge")
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "error", "message": "Failed to start bridge."}


@router.post("/stop")
async def stop_telegram_bridge():
    """Stop the Telegram bridge."""
    try:
        from vaf.api.telegram_bridge import stop_bridge
        stop_bridge()
    except Exception as e:
        logger.exception("Failed to stop Telegram bridge: %s", e)
    return {"status": "stopped", "message": "Telegram bridge stopped."}
