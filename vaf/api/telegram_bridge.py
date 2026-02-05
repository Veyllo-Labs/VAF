"""
Long-lived Telegram bridge: receives messages from Telegram, enqueues tasks with
user_scope_id/username from whitelist, and sends replies via the telegram_reply hook.
Started/stopped from telegram_routes (POST /api/telegram/start, /stop).
"""
import logging
import queue
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from vaf.core.config import Config
from vaf.core.task_queue import TaskQueue
from vaf.core.telegram_reply import set_telegram_reply_callback
from vaf.core.tray_context import TrayContext

logger = logging.getLogger("vaf.api.telegram_bridge")

_bridge_thread: Optional[threading.Thread] = None
_sender_thread: Optional[threading.Thread] = None
_bridge_stop = threading.Event()
_bridge_stop_requested = False
_outgoing_queue: Optional[queue.Queue] = None


def _whitelist_lookup(telegram_user_id: str) -> Optional[Dict[str, Any]]:
    """Return whitelist entry for this telegram_user_id or None."""
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        return None
    whitelist: List[Dict[str, Any]] = telegram_config.get("whitelist") or []
    for entry in whitelist:
        if str(entry.get("telegram_user_id")) == str(telegram_user_id):
            return entry
    return None


def _sender_loop(bot_token: str):
    """Run in a thread: read (chat_id, text) from _outgoing_queue and POST to Telegram."""
    global _outgoing_queue
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    while True:
        try:
            item = _outgoing_queue.get(timeout=1.0)
            if item is None:
                break
            chat_id, text = item
            if not chat_id or not text:
                continue
            # Send as plain text (no parse_mode) so model output with < & > doesn't break
            payload = {"chat_id": chat_id, "text": text[:4096]}
            resp = requests.post(url, json=payload, timeout=10)
            if not resp.ok:
                logger.warning("Telegram sendMessage failed: %s %s", resp.status_code, resp.text)
                try:
                    from vaf.core.log_helper import log_telegram_reply
                    log_telegram_reply(f"SENDER failed chat_id={chat_id} status={resp.status_code} body={resp.text[:200]}")
                except Exception:
                    pass
            else:
                try:
                    from vaf.core.log_helper import log_telegram_reply
                    log_telegram_reply(f"SENDER ok chat_id={chat_id}")
                except Exception:
                    pass
        except queue.Empty:
            continue
        except Exception as e:
            logger.exception("Telegram sender error: %s", e)
            try:
                from vaf.core.log_helper import log_telegram_reply
                log_telegram_reply(f"SENDER error {type(e).__name__}: {e}")
            except Exception:
                pass


def _enqueue_reply(chat_id: str, text: str) -> None:
    try:
        from vaf.core.log_helper import log_telegram_reply
        log_telegram_reply(f"BRIDGE enqueue chat_id={chat_id} len={len(text)} queue={_outgoing_queue is not None}")
    except Exception:
        pass
    if _outgoing_queue is not None:
        try:
            _outgoing_queue.put((chat_id, text))
        except Exception:
            pass


def _run_bot():
    """Run the Telegram bot in this thread (blocking until stop)."""
    global _bridge_stop, _outgoing_queue
    try:
        from telegram import Update
        from telegram.ext import Application, ContextTypes, MessageHandler, filters
    except ImportError:
        logger.error("python-telegram-bot not installed")
        return

    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict):
        logger.error("telegram_config missing or invalid")
        return
    bot_token = (telegram_config.get("bot_token") or "").strip()
    if not bot_token:
        logger.error("telegram_config.bot_token missing")
        return

    application = Application.builder().token(bot_token).build()

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.message.text:
            return
        user = update.effective_user
        if not user:
            return
        telegram_user_id = str(user.id)
        chat_id = update.effective_chat.id if update.effective_chat else user.id
        entry = _whitelist_lookup(telegram_user_id)
        if not entry:
            await update.message.reply_text(
                "You are not authorized to use this bot. Please add your Telegram in VAF Settings → Connections."
            )
            return
        user_scope_id = entry.get("user_scope_id")
        vaf_username = entry.get("vaf_username") or "admin"
        session_id = f"telegram_{telegram_user_id}"
        tq = TaskQueue()
        tq.add(
            session_id=session_id,
            input_text=update.message.text.strip(),
            source="telegram",
            metadata={
                "user_scope_id": user_scope_id,
                "username": vaf_username,
                "telegram_chat_id": str(chat_id),
            },
        )
        try:
            TrayContext().register_telegram_activity()
        except Exception:
            pass
        # No "Message received. Processing…" – user only gets the model reply or an error message

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Hi! Send a message and I’ll relay it to your VAF agent. "
            "If you get “not authorized”, add your Telegram in VAF Settings → Connections."
        )

    from telegram.ext import CommandHandler
    application.add_handler(CommandHandler("start", cmd_start))

    # Job to check stop request and exit run_polling (if job queue is available)
    async def check_stop(context):
        if _bridge_stop_requested:
            await application.stop()

    try:
        job_queue = application.job_queue
        if job_queue:
            job_queue.run_repeating(check_stop, interval=3, first=5)
    except Exception:
        pass

    set_telegram_reply_callback(_enqueue_reply)

    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except Exception as e:
        logger.exception("Telegram bridge bot error: %s", e)
    finally:
        set_telegram_reply_callback(None)


def start_bridge() -> bool:
    """Start the Telegram bridge (bot + sender thread). Returns True if started."""
    global _bridge_thread, _sender_thread, _outgoing_queue, _bridge_stop_requested
    telegram_config = Config.get("telegram_config") or {}
    if not isinstance(telegram_config, dict) or not telegram_config.get("bot_token"):
        return False
    if _bridge_thread is not None and _bridge_thread.is_alive():
        return True
    _bridge_stop_requested = False
    _outgoing_queue = queue.Queue()
    bot_token = (telegram_config.get("bot_token") or "").strip()
    _sender_thread = threading.Thread(target=_sender_loop, args=(bot_token,), daemon=True)
    _sender_thread.start()
    _bridge_thread = threading.Thread(target=_run_bot, daemon=True)
    _bridge_thread.start()
    logger.info("Telegram bridge started")
    return True


def stop_bridge() -> None:
    """Request bridge stop (sender thread exits; bot may keep running until next job check)."""
    global _bridge_stop_requested, _outgoing_queue
    _bridge_stop_requested = True
    if _outgoing_queue is not None:
        try:
            _outgoing_queue.put(None)
        except Exception:
            pass
    logger.info("Telegram bridge stop requested")


def is_bridge_running() -> bool:
    return _bridge_thread is not None and _bridge_thread.is_alive()
