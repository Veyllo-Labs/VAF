# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Discord Integration API Routes

Handles Discord bot setup, verification, and management.
"""
import asyncio
import logging
import threading
from typing import Optional
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger("vaf.api.discord")

router = APIRouter(prefix="/api/discord", tags=["discord"])

# Global state for verification process
_verification_state = {
    "pending_code": None,
    "verified": False,
    "admin_user_id": None,
    "admin_username": None,
    "error": None,
    "bot_running": False,
    "bot_thread": None,
}


def _is_discord_admin(user: dict) -> bool:
    """Return True if current request user is local admin or role=admin."""
    from vaf.core.config import get_local_admin_scope_id
    role = (user or {}).get("role")
    scope = (user or {}).get("user_scope_id")
    return (str(role or "").lower() == "admin") or (scope is not None and str(scope) == str(get_local_admin_scope_id()))


class StartVerificationRequest(BaseModel):
    bot_token: str
    verification_code: str


class DiscordConfig(BaseModel):
    bot_token: str
    admin_user_id: Optional[str] = None
    admin_username: Optional[str] = None
    verified: bool = False
    enabled: bool = False


@router.post("/start-verification")
async def start_verification(request: StartVerificationRequest):
    """
    Start the Discord bot and wait for verification code from user.
    """
    global _verification_state
    
    # Reset state
    _verification_state = {
        "pending_code": request.verification_code,
        "verified": False,
        "admin_user_id": None,
        "admin_username": None,
        "error": None,
        "bot_running": False,
        "bot_thread": None,
    }
    
    try:
        # Try to import discord
        try:
            import discord
        except ImportError:
            raise HTTPException(
                status_code=500, 
                detail="Discord.py not installed. Run: pip install discord.py"
            )
        
        # Start bot in background thread
        def run_verification_bot():
            global _verification_state
            
            try:
                intents = discord.Intents.default()
                intents.message_content = True
                intents.dm_messages = True
                
                client = discord.Client(intents=intents)
                
                @client.event
                async def on_ready():
                    logger.info(f"Verification bot logged in as {client.user}")
                    _verification_state["bot_running"] = True
                
                @client.event
                async def on_message(message):
                    global _verification_state
                    
                    # Ignore bot's own messages
                    if message.author == client.user:
                        return
                    
                    # Only accept DMs for verification
                    if not isinstance(message.channel, discord.DMChannel):
                        return
                    
                    # Check if message matches verification code
                    if message.content.strip() == _verification_state["pending_code"]:
                        _verification_state["verified"] = True
                        _verification_state["admin_user_id"] = str(message.author.id)
                        _verification_state["admin_username"] = message.author.name
                        
                        # Send confirmation
                        await message.channel.send(
                            f"✅ **Verification successful!**\n\n"
                            f"You are now the admin for this VAF bot.\n"
                            f"User ID: `{message.author.id}`"
                        )
                        
                        logger.info(f"Discord admin verified: {message.author.name} ({message.author.id})")
                        
                        # Stop the bot after verification
                        await client.close()
                    else:
                        await message.channel.send(
                            "❌ Invalid verification code. Please check and try again."
                        )
                
                @client.event
                async def on_error(event, *args, **kwargs):
                    global _verification_state
                    import traceback
                    error_msg = traceback.format_exc()
                    logger.error(f"Discord error: {error_msg}")
                    _verification_state["error"] = str(error_msg)
                
                # Run the bot
                client.run(request.bot_token)
                
            except discord.LoginFailure:
                _verification_state["error"] = "Invalid bot token. Please check and try again."
                logger.error("Discord login failed: Invalid token")
            except Exception as e:
                _verification_state["error"] = str(e)
                logger.error(f"Discord bot error: {e}")
        
        # Start bot in background
        thread = threading.Thread(target=run_verification_bot, daemon=True)
        thread.start()
        _verification_state["bot_thread"] = thread
        
        # Wait a bit for bot to start
        await asyncio.sleep(2)
        
        if _verification_state.get("error"):
            raise HTTPException(status_code=400, detail=_verification_state["error"])
        
        return {"status": "waiting", "message": "Bot started. Send verification code via Discord DM."}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to start verification: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/verification-status")
async def get_verification_status():
    """
    Check the current verification status.
    """
    return {
        "verified": _verification_state.get("verified", False),
        "admin_user_id": _verification_state.get("admin_user_id"),
        "admin_username": _verification_state.get("admin_username"),
        "error": _verification_state.get("error"),
        "bot_running": _verification_state.get("bot_running", False),
    }


@router.get("/dashboard")
async def get_discord_dashboard(request: Request):
    """
    Data for the Discord settings dashboard: status, admin info, activity. No sensitive data (no tokens).
    """
    from vaf.core.config import Config
    from vaf.api.discord_bridge import is_bridge_running

    from vaf.api.config_routes import get_current_user_or_local_admin
    user = get_current_user_or_local_admin(request)
    if not _is_discord_admin(user):
        # Discord is single-tenant admin integration. Non-admin users must not see admin metadata/activity.
        return {
            "configured": False,
            "running": False,
            "admin_username": None,
            "admin_user_id": None,
            "enabled": False,
            "activity": [],
        }

    discord_config = Config.get("discord_config") or {}
    if not isinstance(discord_config, dict):
        discord_config = {}

    # Activity: max 20 events, newest first
    raw_activity = list(discord_config.get("chat_activity") or [])[-20:]
    activity = [
        {
            "chat_id": str(a.get("channel_id", a.get("chat_id", ""))),
            "ts": a.get("ts", 0),
            "direction": a.get("direction", "in"),
        }
        for a in raw_activity
    ]

    return {
        "configured": bool(discord_config.get("verified") and discord_config.get("admin_user_id")),
        "running": is_bridge_running(),
        "admin_username": discord_config.get("admin_username"),
        "admin_user_id": discord_config.get("admin_user_id"),
        "enabled": discord_config.get("enabled", False),
        "activity": activity,
    }


@router.get("/status")
async def get_discord_status(request: Request):
    """Get the current Discord bridge status — scoped per user."""
    from vaf.core.config import Config
    from vaf.api.discord_bridge import is_bridge_running
    from vaf.api.config_routes import get_current_user_or_local_admin

    user = get_current_user_or_local_admin(request)
    if not _is_discord_admin(user):
        # Discord is single-tenant (admin-only). Non-admin never has Discord.
        return {"configured": False, "enabled": False, "running": False, "admin_username": None}

    discord_config = Config.get("discord_config", {})
    return {
        "configured": discord_config.get("verified", False),
        "enabled": discord_config.get("enabled", False),
        "running": is_bridge_running(),
        "admin_username": discord_config.get("admin_username"),
    }


@router.post("/start")
async def start_discord_bridge():
    """
    Start the Discord bridge with saved configuration.
    """
    from vaf.core.config import Config
    from vaf.api.discord_bridge import start_bridge, is_bridge_running

    discord_config = Config.get("discord_config", {})

    if not discord_config.get("verified"):
        raise HTTPException(status_code=400, detail="Discord not configured. Please complete setup first.")

    if not discord_config.get("bot_token"):
        raise HTTPException(status_code=400, detail="Bot token missing.")

    if not discord_config.get("admin_user_id"):
        raise HTTPException(status_code=400, detail="Admin user ID missing. Complete verification first.")

    if is_bridge_running():
        return {"status": "started", "message": "Discord bridge already running."}

    if start_bridge():
        return {"status": "started", "message": "Discord bridge started."}
    raise HTTPException(status_code=500, detail="Failed to start Discord bridge.")


@router.get("/session/{session_id}/history")
async def get_discord_session_history(session_id: str, request: Request):
    """Return message history for a Discord session (session_id must start with 'discord_')."""
    if not session_id.startswith("discord_"):
        raise HTTPException(status_code=400, detail="Invalid session id")
    try:
        from vaf.api.config_routes import get_current_user_or_local_admin
        user = get_current_user_or_local_admin(request)
        if not _is_discord_admin(user):
            raise HTTPException(status_code=403, detail="Access denied")
        from vaf.core.config import Config
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
        interval = int(Config.get("memory_compaction_interval", 15))
        last_compaction = int(runtime_state.get("last_compaction_at_turn", 0))
        return {
            "session_id": session_id,
            "messages": messages,
            "user_turn_count": user_turn_count,
            "compaction_interval": interval,
            "last_compaction_at_turn": last_compaction,
        }
    except FileNotFoundError:
        from vaf.core.config import Config
        interval = int(Config.get("memory_compaction_interval", 15))
        return {
            "session_id": session_id,
            "messages": [],
            "user_turn_count": 0,
            "compaction_interval": interval,
            "last_compaction_at_turn": 0,
        }
    except Exception as e:
        logger.exception("Discord session history error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/stop")
async def stop_discord_bridge():
    """
    Stop the Discord bridge.
    """
    from vaf.api.discord_bridge import stop_bridge, is_bridge_running

    if is_bridge_running():
        stop_bridge()
    return {"status": "stopped", "message": "Discord bridge stopped."}
