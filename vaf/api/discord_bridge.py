# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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
from vaf.core.channel_ingress_policy import evaluate_ingress, should_log_unauthorized
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


def _store_discord_message(chat_id, body, direction, content_type="text", message_id=None) -> None:
    """Record a Discord message in the shared channel store (whatsapp_message_store, channel='discord')
    so the agent's read_discord_chat / find_discord_messages tools can read history. Discord is
    admin-only: username='admin', user_scope_id=None. chat_id = the Discord user/author id (==
    get_discord_user_id), so incoming and proactive-outgoing land in the same bucket. Best-effort."""
    try:
        from vaf.core.channel_message_store import append_message
        append_message(
            username="admin", chat_id=str(chat_id or ""), body=str(body or ""),
            direction=direction, content_type=content_type, message_id=message_id,
            channel="discord", user_scope_id=None,
        )
    except Exception:
        pass


# Accumulated RAG documents per Discord session (mirror of telegram_bridge); the RAG index is a
# REPLACE per session, so we keep the full set and re-index on each new document. In-memory.
_discord_session_documents: Dict[str, list] = {}


async def _enqueue_discord_image(message, attachment, session_id, caption) -> bool:
    """Route a Discord image attachment to the vision pipeline (mirror of the Telegram image path):
    download -> persist into the user's attachments folder -> metadata['images'] -> agent turn."""
    import base64 as _b64
    import asyncio as _aio
    try:
        raw = await attachment.read()
    except Exception:
        return False
    mime = (getattr(attachment, "content_type", "") or "image/jpeg") or "image/jpeg"
    name = getattr(attachment, "filename", "") or "discord_image.jpg"
    attached_images = [{"data": _b64.b64encode(raw).decode("ascii"), "mime_type": mime, "name": name}]
    try:
        from vaf.core.web_server import _persist_attached_images_to_files
        attached_images = await _aio.to_thread(_persist_attached_images_to_files, attached_images, session_id, None)
    except Exception as e:
        logger.warning("discord image persist failed, keeping inline: %s", e)
    author_id = str(message.author.id)
    metadata = {
        "user_scope_id": None, "username": "admin",
        "discord_channel_id": str(message.channel.id), "discord_author_id": author_id,
        "origin_channel": "discord", "task_class": "interactive",
        "images": attached_images,  # -> vision pipeline (headless_runner reads metadata['images'])
    }
    user_message = f"[Photo] (User: {caption})" if caption else "[Photo]"
    TaskQueue().add(session_id=session_id, input_text=user_message, source="discord", metadata=metadata)
    _store_discord_message(author_id, user_message, "in", "image", str(message.id))
    logger.info("Discord image enqueued for vision from %s", message.author.name)
    return True


async def _handle_discord_document(message, attachment, session_id, caption) -> bool:
    """Extract text from a Discord document attachment, enqueue it inline, and index it for RAG
    (mirror of the Telegram document path)."""
    import os as _os
    import tempfile as _tf
    import asyncio as _aio
    from pathlib import Path as _Path
    file_name = getattr(attachment, "filename", "") or "document"
    ext = _os.path.splitext(file_name.lower())[1]
    _DOC_EXTS = (".pdf", ".docx", ".xlsx", ".pptx", ".xls", ".txt", ".md", ".csv", ".json", ".xml")
    if ext not in _DOC_EXTS:
        return False
    try:
        raw = await attachment.read()
    except Exception:
        return False
    tmp = None
    extracted = ""
    try:
        with _tf.NamedTemporaryFile(prefix="vaf_disc_", suffix=ext, delete=False) as f:
            f.write(raw)
            tmp = f.name
        from vaf.tools.librarian import LibrarianTool
        extracted = await _aio.to_thread(LibrarianTool()._read_file, _Path(tmp), True)
    except Exception as e:
        logger.warning("discord document extract failed: %s", e)
        return False
    finally:
        if tmp:
            try:
                _os.unlink(tmp)
            except Exception:
                pass
    author_id = str(message.author.id)
    if caption:
        user_message = f"[Document: {file_name}] (User: {caption})\n\n--- Document content ---\n{extracted}"
    else:
        user_message = f"[Document: {file_name}]\n\n--- Document content ---\n{extracted}"
    metadata = {
        "user_scope_id": None, "username": "admin",
        "discord_channel_id": str(message.channel.id), "discord_author_id": author_id,
        "origin_channel": "discord", "task_class": "interactive",
    }
    TaskQueue().add(session_id=session_id, input_text=user_message, source="discord", metadata=metadata)
    _store_discord_message(author_id, f"[Document: {file_name}]" + (f" {caption}" if caption else ""),
                           "in", "document", str(message.id))
    # RAG indexing (additive): accumulate the session's docs and re-index (mirror Telegram).
    try:
        if bool(Config.get("attachment_rag_enabled", False)) and extracted and not extracted.lstrip().startswith("[ERROR]"):
            new_doc = {"name": file_name, "content": extracted,
                       "mimeType": getattr(attachment, "content_type", "") or "", "path": ""}
            docs = [d for d in _discord_session_documents.get(session_id, []) if d.get("name") != file_name]
            docs.append(new_doc)
            _discord_session_documents[session_id] = docs
            from vaf.memory.attachment_rag import index_session_attachments_sync
            await _aio.to_thread(index_session_attachments_sync, session_id, None, list(docs))
    except Exception as e:
        logger.warning("discord document RAG indexing failed: %s", e)
    return True


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
        policy = Config.get("channel_ingress_policy")
        explicit_match = str(message.author.id) == admin_user_id and isinstance(message.channel, discord.DMChannel)
        allowed, reason = evaluate_ingress("discord", policy, explicit_match=explicit_match, contact_match=False)
        if not allowed:
            sender_id = str(message.author.id)
            if should_log_unauthorized("discord", sender_id, policy):
                logger.warning(
                    "Dropped unauthorized Discord message author_id=%s channel_id=%s reason=%s",
                    sender_id,
                    str(getattr(message.channel, "id", "")),
                    reason,
                )
            return

        text = (message.content or "").strip()
        channel_id = str(message.channel.id)
        session_id = f"discord_{message.author.id}"

        # Attachments: route images to the vision pipeline and documents to attachment RAG
        # (mirror of the Telegram bridge). On Discord the message text doubles as the caption.
        attachments = list(getattr(message, "attachments", None) or [])
        if attachments:
            try:
                _append_discord_activity(channel_id, "in")
            except Exception:
                pass
            for att in attachments:
                mime = (getattr(att, "content_type", "") or "").lower()
                fname = (getattr(att, "filename", "") or "").lower()
                ext = fname[fname.rfind("."):] if "." in fname else ""
                try:
                    if mime.startswith("image/") or ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
                        await _enqueue_discord_image(message, att, session_id, text)
                    else:
                        await _handle_discord_document(message, att, session_id, text)
                except Exception as e:
                    logger.warning("Discord attachment handling failed: %s", e)
            return  # the message text is carried as the attachment caption

        if not text:
            return

        metadata: Dict[str, Any] = {
            "user_scope_id": None,
            "username": "admin",
            "discord_channel_id": channel_id,
            "discord_author_id": str(message.author.id),
            "origin_channel": "discord",
            "task_class": "interactive",
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
        # Record in the channel store (canonical chat_id = author.id, matching session_id +
        # get_discord_user_id) so read_discord_chat / find_discord_messages can read history.
        _store_discord_message(str(message.author.id), text, "in", "text", str(message.id))
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
