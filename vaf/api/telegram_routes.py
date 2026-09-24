# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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
from typing import Optional, Any, Dict

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel

from vaf.core.config import Config, get_local_admin_scope_id, get_local_admin_username
from vaf.api.user_routes import caller_is_admin, require_admin
from vaf.core.channel_secrets import channel_secret, has_channel_secret
from vaf.core.messaging_connections import channel_enabled_for_scope
from vaf.core.security_events import log_security_event

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
        "user_scope_id": get_local_admin_scope_id(),
        "username": get_local_admin_username(),
    }


class StartVerificationRequest(BaseModel):
    bot_token: str
    verification_code: str


class WhitelistAddRequest(BaseModel):
    telegram_user_id: str
    telegram_username: Optional[str] = None


@router.post("/start-verification")
async def start_verification(request: StartVerificationRequest, _: Dict[str, Any] = Depends(require_admin)):
    """
    Start the Telegram bot and wait for verification code from user (DM to bot).
    On success, verification state contains telegram_user_id and telegram_username.

    Admin only, like the rest of the bot's setup: it runs a bot on whatever token it is
    given, and its state is one per process, so any signed-in account could start a
    verification, read another's result, or reset it.
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
                    # Stop run_polling() from within the handler (graceful, thread-safe).
                    application.stop_running()

            application.add_handler(
                MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, handle_message)
            )

            try:
                # run_polling() normally installs SIGINT/SIGTERM handlers, which only works on
                # the main thread. This bot runs in a background thread, so disable them with
                # stop_signals=None and stop it via application.stop_running() instead.
                application.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=None)
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
async def get_verification_status(_: Dict[str, Any] = Depends(require_admin)):
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
    _: Dict[str, Any] = Depends(require_admin),
):
    """
    Add one whitelist entry linking a Telegram user to the current VAF user.
    user_scope_id and username come from request (auth or local_admin).

    Admin only. The Telegram id comes from the request body and an entry with the same id
    is replaced whoever it belonged to, so any signed-in account could send the admin's id
    and become the owner of the admin's Telegram chat: their agent answered it and their
    window read it. The id is proven by the setup wizard's verification, which is an
    admin's step as well.
    """
    from vaf.core.channel_pairing import pair_telegram_account
    # An admin may move a Telegram account that another account had paired: they could
    # edit the list anyway, and the wizard's verification proved who holds it.
    pair_telegram_account(body.telegram_user_id, body.telegram_username,
                          user_scope_id=current_user["user_scope_id"], username=current_user["username"],
                          may_take_over=True)
    telegram_config = Config.get("telegram_config") or {}
    whitelist = list(telegram_config.get("whitelist") or []) if isinstance(telegram_config, dict) else []
    return {"status": "ok", "whitelist_count": len(whitelist)}


def _pairing_caller(request: Request) -> Dict[str, str]:
    """The account a pairing is for. Strict where `get_current_vaf_user` is lenient: a
    signed-in account without a scope would otherwise be read as the local admin, and a
    pairing is exactly what must never land on somebody else's account."""
    state_user = getattr(request.state, "user", None)
    if isinstance(state_user, dict) and not str(state_user.get("user_scope_id") or "").strip():
        raise HTTPException(status_code=400, detail="This account has no scope to pair a Telegram account with.")
    return get_current_vaf_user(request)


@router.post("/pair")
async def start_pairing(request: Request):
    """A one-time code that pairs the caller's OWN Telegram account with the running bot.

    Any signed-in account may ask; the code is theirs alone. They send it to the bot as
    `/start <code>` (the returned t.me link fills it in), and the bot links the Telegram
    account that sent it to the account that asked (vaf/core/channel_pairing.py). The
    setup wizard's route is an admin's, because it needs the bot token and runs a bot of
    its own; this one needs neither."""
    from vaf.api.telegram_bridge import is_bridge_running
    from vaf.core.channel_pairing import PAIRING_TTL_SECONDS, issue_pairing_code

    caller = _pairing_caller(request)
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict) or not telegram_config.get("verified") or not has_channel_secret("telegram"):
        raise HTTPException(status_code=409, detail="Telegram is not set up on this installation.")
    if not is_bridge_running():
        raise HTTPException(status_code=409, detail="The Telegram bot is not running. An admin switches it on in Settings, Connections.")
    code = issue_pairing_code("telegram", caller["user_scope_id"], caller["username"])
    bot_username = await asyncio.to_thread(_get_bot_username)
    return {
        "code": code,
        "command": f"/start {code}",
        "bot_username": bot_username,
        "link": f"https://t.me/{bot_username}?start={code}" if bot_username else None,
        "expires_in": PAIRING_TTL_SECONDS,
    }


@router.get("/pair")
async def pairing_status(request: Request):
    """Whether the caller's own Telegram account is paired, and whether a code is out."""
    from vaf.core.channel_pairing import pairing_pending

    caller = _pairing_caller(request)
    scope = str(caller["user_scope_id"]).strip()
    telegram_config = Config.get("telegram_config") or {}
    whitelist = list(telegram_config.get("whitelist") or []) if isinstance(telegram_config, dict) else []
    return {
        "paired": any(isinstance(e, dict) and str(e.get("user_scope_id") or "").strip() == scope for e in whitelist),
        "pending": pairing_pending("telegram", scope),
    }


@router.get("/status")
async def get_telegram_status(request: Request):
    """Return per-user Telegram status with strict scope isolation."""
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        telegram_config = {}
    whitelist = telegram_config.get("whitelist") or []
    relay_whitelist = telegram_config.get("relay_whitelist") or []
    running = False
    try:
        from vaf.api.telegram_bridge import is_bridge_running
        running = is_bridge_running()
    except Exception:
        pass
    current_user = get_current_vaf_user(request)
    scope_str = str(current_user.get("user_scope_id") or "").strip()
    is_admin = caller_is_admin(request)

    # The caller's lane, by the one rule the bridge answers by (an admin rides the bot).
    enabled = channel_enabled_for_scope("telegram", scope_str or None, admin=is_admin)
    if is_admin:
        visible_whitelist = list(whitelist)
    else:
        visible_whitelist = [
            e
            for e in list(whitelist) + list(relay_whitelist)
            if isinstance(e, dict) and str(e.get("user_scope_id") or "").strip() == scope_str
        ]

    configured = bool(telegram_config.get("verified") and len(visible_whitelist) > 0 and has_channel_secret("telegram"))
    return {
        "configured": configured,
        "enabled": enabled,
        "running": bool(running and enabled and configured),
        # The shared bot on its own, apart from this caller's switch: the Connections card
        # shows "Connected" from it and the switch as it stands on the page, which may be
        # a change not saved yet (`running` reads the saved one).
        "bridge_running": bool(running),
        "whitelist_count": len(visible_whitelist),
        # Whether the caller's OWN Telegram account is paired (an owner entry of theirs, not
        # a relay contact they added): what the card tells them.
        "paired": any(isinstance(e, dict) and str(e.get("user_scope_id") or "").strip() == scope_str
                      for e in whitelist),
    }


def _get_bot_username() -> Optional[str]:
    """Return bot username from Telegram getMe (cached in config or fetched). No token in response."""
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        return None
    cached = telegram_config.get("bot_username")
    if cached:
        return cached
    token = channel_secret("telegram")
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
async def get_telegram_dashboard(request: Request):
    """
    Data for the Telegram settings dashboard: bot link, sessions (chats for this bot only),
    admin whitelist, relay whitelist, activity. No sensitive data (no tokens).
    Non-admins see only their own whitelist entry and session(s).
    """
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        telegram_config = {}
    # to_thread: _get_bot_username does a BLOCKING requests.get to the Telegram API;
    # calling it directly from this async handler stalls the whole uvicorn event loop.
    bot_username = await asyncio.to_thread(_get_bot_username)
    bot_link = f"https://t.me/{bot_username}" if bot_username else None
    admin_whitelist_raw = list(telegram_config.get("whitelist") or [])
    relay_whitelist = list(telegram_config.get("relay_whitelist") or [])
    current_user = get_current_vaf_user(request)
    user_scope_id = current_user.get("user_scope_id")
    is_admin = caller_is_admin(request)

    if is_admin:
        admin_whitelist = admin_whitelist_raw
    else:
        admin_whitelist = [e for e in admin_whitelist_raw if isinstance(e, dict) and str(e.get("user_scope_id")) == str(user_scope_id)]
        relay_whitelist = [e for e in relay_whitelist if isinstance(e, dict) and str(e.get("user_scope_id")) == str(user_scope_id)]

    activity_raw = list(telegram_config.get("chat_activity") or [])[-100:]
    if is_admin:
        activity = activity_raw
    else:
        my_chat_ids = {str(e.get("telegram_user_id") or "") for e in admin_whitelist + relay_whitelist}
        activity = [a for a in activity_raw if str(a.get("chat_id") or "") in my_chat_ids]

    # Sessions: one per chat (our bot only = whitelist + relay), then the message store's
    # rows: the count, the newest message and the person's own state (unread, waits, done)
    # come from the one overview vaf/core/inbox.py reads. The activity log only seeds a
    # chat the store never saw and feeds the chart.
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
    def _store_rows() -> list:
        from vaf.core.channel_message_store import chat_overview, store_exists
        from vaf.core.contacts_store import message_channel_username
        row_user = message_channel_username("telegram", current_user.get("username"))
        if not store_exists(row_user, user_scope_id):
            return []
        return chat_overview(row_user, user_scope_id=user_scope_id, channel="telegram", limit=500)

    try:
        from vaf.core.inbox import chat_state
        # SQLite off the event loop, as the mail and inbox routes do.
        for row in await asyncio.to_thread(_store_rows):
            cid = str(row.get("chat_id") or "")
            if not cid:
                continue
            rec = sessions_by_chat.setdefault(cid, {
                "chat_id": cid, "telegram_user_id": cid, "telegram_username": None,
                "vaf_username": None, "type": "unknown",
            })
            state = chat_state(row)
            rec["last_ts"] = max(rec.get("last_ts") or 0, int(row.get("last_ts") or 0))
            rec.update({
                "name": (row.get("chat_name") or "").strip() or None,
                "message_count": int(row.get("message_count") or 0),
                "last_preview": row.get("last_body") or "",
                "last_direction": row.get("last_direction") or "",
                "preview_from": state["preview_from"],
                "unread": state["unread"],
                "waits": state["waits"],
                "waits_reason": state["waits_reason"],
                "answered_by_agent": state["answered_by_agent"],
                "done": state["done"],
            })
    except Exception:
        pass
    for rec in sessions_by_chat.values():
        rec.setdefault("last_ts", 0)
        rec.setdefault("message_count", 0)
        rec.setdefault("name", None)
        rec.setdefault("last_preview", "")
        rec.setdefault("last_direction", "")
        rec.setdefault("preview_from", "")
        rec.setdefault("unread", 0)
        rec.setdefault("waits", False)
        rec.setdefault("waits_reason", "")
        rec.setdefault("answered_by_agent", False)
        rec.setdefault("done", False)
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


def _pairing_changed(previous: Optional[Dict[str, Any]], entry: Dict[str, Any]) -> bool:
    """Whether a whitelist write moved a Telegram id's access: no entry before, or the
    entry belonged to another account. A display name is not access."""
    if not previous:
        return True
    return (str(previous.get("user_scope_id") or ""), str(previous.get("vaf_username") or "")) != \
        (str(entry.get("user_scope_id") or ""), str(entry.get("vaf_username") or ""))


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
    previous = next((e for e in (telegram_config.get("relay_whitelist") or [])
                     if isinstance(e, dict) and str(e.get("telegram_user_id")) == telegram_user_id), None)
    # One relay entry per Telegram id, and it belongs to the account that added it. The list
    # below drops the old entry whoever owned it, so without this another account could take
    # a contact over by adding the same id. An admin may move it, as they may edit the list.
    if (previous is not None and not caller_is_admin(request)
            and str(previous.get("user_scope_id") or "").strip() != str(current_user["user_scope_id"]).strip()):
        raise HTTPException(status_code=409, detail="This Telegram account is already a relay contact of another account.")
    relay_whitelist = [e for e in (telegram_config.get("relay_whitelist") or []) if str(e.get("telegram_user_id")) != telegram_user_id]
    entry = {
        "telegram_user_id": telegram_user_id,
        "telegram_username": (body.telegram_username or "").strip() or None,
        "user_scope_id": current_user["user_scope_id"],
        "vaf_username": current_user["username"],
    }
    relay_whitelist.append(entry)
    telegram_config["relay_whitelist"] = relay_whitelist
    config["telegram_config"] = telegram_config
    Config.save(config)
    if _pairing_changed(previous, entry):
        log_security_event("channel_paired", channel="telegram", username=str(current_user.get("username") or ""),
                           path=telegram_user_id, detail=f"relay {telegram_user_id}")
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
async def get_telegram_session_history(session_id: str, request: Request):
    """Return message history and compaction stats for a Telegram session (session_id must start with 'telegram_')."""
    if not session_id.startswith("telegram_"):
        raise HTTPException(status_code=400, detail="Invalid session id")
    current_user = get_current_vaf_user(request)
    user_scope_id = str(current_user.get("user_scope_id") or "").strip()
    is_admin = caller_is_admin(request)
    chat_id = session_id[len("telegram_") :]
    # A relay contact is answered by nobody and never compacts: the pane shows no
    # Memory Learning counter for that chat (the counter would count turns that never learn).
    _cfg = Config.get("telegram_config") or {}
    _relay_ids = {str(e.get("telegram_user_id") or "").strip()
                  for e in (list(_cfg.get("relay_whitelist") or []) if isinstance(_cfg, dict) else [])
                  if isinstance(e, dict)}
    learns = chat_id not in _relay_ids
    if not is_admin:
        telegram_config = Config.get("telegram_config") or {}
        if not isinstance(telegram_config, dict):
            telegram_config = {}
        whitelist = list(telegram_config.get("whitelist") or [])
        relay_whitelist = list(telegram_config.get("relay_whitelist") or [])
        allowed_chat_ids = {
            str(e.get("telegram_user_id") or "").strip()
            for e in whitelist + relay_whitelist
            if isinstance(e, dict) and str(e.get("user_scope_id") or "").strip() == user_scope_id
        }
        if chat_id not in allowed_chat_ids:
            raise HTTPException(status_code=403, detail="Access denied")
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
        out = {"session_id": session_id, "messages": messages}
        if learns:
            out.update({
                "user_turn_count": user_turn_count,
                "compaction_interval": compaction_interval,
                "last_compaction_at_turn": last_compaction_at_turn,
            })
        return out
    except FileNotFoundError:
        last_compaction_at_turn, compaction_interval = _get_compaction_info(session_id)
        if not learns:
            return {"session_id": session_id, "messages": []}
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
    current_user = get_current_vaf_user(request)
    user_scope_id = str(current_user.get("user_scope_id") or "").strip()
    is_admin = caller_is_admin(request)
    telegram_user_id = (body.telegram_user_id or "").strip()
    if not telegram_user_id:
        raise HTTPException(status_code=400, detail="telegram_user_id required")
    config = Config.load()
    telegram_config = config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        telegram_config = {}
    before = len(telegram_config.get("relay_whitelist") or [])
    relay_whitelist = []
    for e in (telegram_config.get("relay_whitelist") or []):
        if str(e.get("telegram_user_id") or "").strip() != telegram_user_id:
            relay_whitelist.append(e)
            continue
        if is_admin:
            # Admin may remove any entry.
            continue
        if str(e.get("user_scope_id") or "").strip() == user_scope_id:
            # Non-admin may remove only own relay entry.
            continue
        relay_whitelist.append(e)
    telegram_config["relay_whitelist"] = relay_whitelist
    config["telegram_config"] = telegram_config
    Config.save(config)
    if len(relay_whitelist) < before:
        log_security_event("channel_unpaired", channel="telegram", username=str(current_user.get("username") or ""),
                           path=telegram_user_id, detail=f"relay {telegram_user_id}")
    return {"status": "ok", "relay_whitelist_count": len(relay_whitelist)}


@router.post("/start")
async def start_telegram_bridge(_: Dict[str, Any] = Depends(require_admin)):
    """Start the Telegram bridge with saved configuration. Admin only: it is the one bot of
    the whole instance, and another user's switch is their own lane, stored per scope."""
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        telegram_config = {}
    if not telegram_config.get("verified"):
        raise HTTPException(status_code=400, detail="Telegram not configured. Please complete setup first.")
    if not has_channel_secret("telegram"):
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
async def stop_telegram_bridge(_: Dict[str, Any] = Depends(require_admin)):
    """Stop the Telegram bridge. Admin only, like /start: stopping it stops it for everybody."""
    try:
        from vaf.api.telegram_bridge import stop_bridge
        stop_bridge()
    except Exception as e:
        logger.exception("Failed to stop Telegram bridge: %s", e)
    return {"status": "stopped", "message": "Telegram bridge stopped."}
