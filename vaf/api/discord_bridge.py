"""
Long-lived Discord bridge: receives messages from admin, enqueues tasks,
and sends replies via discord_reply callback (from headless_runner).
Started/stopped from discord_routes (POST /api/discord/start, /stop).
"""
import logging
import queue
import threading
import time
from typing import Any, Dict, Optional

from vaf.core.config import Config
from vaf.core.task_queue import TaskQueue
from vaf.core.discord_reply import set_discord_reply_callback

logger = logging.getLogger("vaf.api.discord_bridge")

_bridge_thread: Optional[threading.Thread] = None
_sender_thread: Optional[threading.Thread] = None
_bridge_stop = threading.Event()
_bridge_stop_requested = False
_outgoing_queue: Optional[queue.Queue] = None


def _sender_loop(bot_token: str) -> None:
    """Run in a thread: read (channel_id, text) from _outgoing_queue and POST to Discord API."""
    global _outgoing_queue
    try:
        from vaf.core.discord_send import send_discord_message
    except ImportError:
        logger.error("discord_send unavailable")
        return

    while True:
        try:
            item = _outgoing_queue.get(timeout=1.0)
            if item is None:
                break

            channel_id, text = item
            if not channel_id or not text:
                continue

            try:
                ok = send_discord_message(bot_token, channel_id, text, chunk=True)
                if ok:
                    try:
                        from vaf.core.log_helper import log_discord_reply
                        log_discord_reply(f"SENDER ok channel_id={channel_id}")
                    except Exception:
                        pass
                else:
                    logger.warning("Discord send failed for channel %s", channel_id)
            except Exception as e:
                logger.warning("Discord sender error: %s", e)
                try:
                    from vaf.core.log_helper import log_discord_reply
                    log_discord_reply(f"SENDER error {type(e).__name__}: {e}")
                except Exception:
                    pass
        except queue.Empty:
            continue
        except Exception as e:
            logger.exception("Discord sender loop error: %s", e)


def _append_discord_activity(channel_id: str, direction: str = "in") -> None:
    """Append one activity entry for the dashboard timeline (keeps last 20, older are dropped)."""
    try:
        config = Config.load()
        dc = config.get("discord_config") or {}
        if not isinstance(dc, dict):
            return
        activity = list(dc.get("chat_activity") or [])
        activity.append({"channel_id": str(channel_id), "ts": time.time(), "direction": direction})
        dc["chat_activity"] = activity[-20:]
        config["discord_config"] = dc
        Config.save(config)
    except Exception:
        pass


def _enqueue_reply(channel_id: str, text: str) -> None:
    """Enqueue a reply for the sender thread to post to Discord."""
    try:
        _append_discord_activity(channel_id, "out")
    except Exception:
        pass
    try:
        from vaf.core.log_helper import log_discord_reply
        log_discord_reply(f"BRIDGE enqueue channel_id={channel_id} len={len(text)} queue={_outgoing_queue is not None}")
    except Exception:
        pass
    if _outgoing_queue is not None:
        try:
            _outgoing_queue.put((channel_id, text))
        except Exception:
            pass


def _run_bot() -> None:
    """Run the Discord bot in this thread (blocking until stop)."""
    global _bridge_stop_requested

    try:
        import discord
    except ImportError:
        logger.error("discord.py not installed")
        return

    discord_config = Config.get("discord_config") or {}
    if not isinstance(discord_config, dict):
        logger.error("discord_config missing or invalid")
        return
    bot_token = (discord_config.get("bot_token") or "").strip()
    admin_user_id = (discord_config.get("admin_user_id") or "").strip()
    if not bot_token or not admin_user_id:
        logger.error("discord_config bot_token or admin_user_id missing")
        return

    intents = discord.Intents.default()
    intents.message_content = True
    intents.dm_messages = True

    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        logger.info("Discord bridge logged in as %s", client.user)

    @client.event
    async def on_message(message: discord.Message):
        if message.author == client.user:
            return
        # Only accept messages from the verified admin
        if str(message.author.id) != admin_user_id:
            return
        # Only accept DMs for now
        if not isinstance(message.channel, discord.DMChannel):
            return

        text = (message.content or "").strip()
        if not text:
            return

        channel_id = str(message.channel.id)
        session_id = f"discord_{message.author.id}"
        metadata: Dict[str, Any] = {
            "user_scope_id": None,
            "username": "admin",
            "discord_channel_id": channel_id,
            "discord_author_id": str(message.author.id),
        }

        try:
            _append_discord_activity(channel_id, "in")
        except Exception:
            pass
        tq = TaskQueue()
        tq.add(
            session_id=session_id,
            input_text=text,
            source="discord",
            metadata=metadata,
        )
        logger.info("Discord message enqueued from %s", message.author.name)

    set_discord_reply_callback(_enqueue_reply)

    try:
        client.run(bot_token)
    except discord.LoginFailure:
        logger.error("Discord login failed: invalid token")
    except Exception as e:
        logger.exception("Discord bridge error: %s", e)
    finally:
        set_discord_reply_callback(None)


def start_bridge() -> bool:
    """Start the Discord bridge (bot + sender thread). Returns True if started."""
    global _bridge_thread, _sender_thread, _outgoing_queue, _bridge_stop_requested

    discord_config = Config.get("discord_config") or {}
    if not isinstance(discord_config, dict) or not discord_config.get("bot_token"):
        return False
    if not discord_config.get("verified") or not discord_config.get("admin_user_id"):
        return False
    if not discord_config.get("enabled"):
        return False

    if _bridge_thread is not None and _bridge_thread.is_alive():
        return True

    _bridge_stop_requested = False
    _outgoing_queue = queue.Queue()
    bot_token = (discord_config.get("bot_token") or "").strip()

    _sender_thread = threading.Thread(target=_sender_loop, args=(bot_token,), daemon=True)
    _sender_thread.start()

    _bridge_thread = threading.Thread(target=_run_bot, daemon=True)
    _bridge_thread.start()

    logger.info("Discord bridge started")
    return True


def stop_bridge() -> None:
    """Request bridge stop. Sender thread exits; bot closes on next event."""
    global _bridge_stop_requested, _outgoing_queue
    _bridge_stop_requested = True
    if _outgoing_queue is not None:
        try:
            _outgoing_queue.put(None)
        except Exception:
            pass
    logger.info("Discord bridge stop requested")


def is_bridge_running() -> bool:
    return _bridge_thread is not None and _bridge_thread.is_alive()
