"""
Telegram Integration API Routes

Handles Telegram bot setup, verification, whitelist (per-user), and bridge management.
All responses and user-facing messages in English.
"""
import asyncio
import logging
import threading
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
