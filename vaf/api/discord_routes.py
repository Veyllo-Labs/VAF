"""
Discord Integration API Routes

Handles Discord bot setup, verification, and management.
"""
import asyncio
import logging
import threading
from typing import Optional
from fastapi import APIRouter, HTTPException
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


@router.get("/status")
async def get_discord_status():
    """
    Get the current Discord bridge status.
    """
    from vaf.core.config import Config
    
    discord_config = Config.get("discord_config", {})
    
    return {
        "configured": discord_config.get("verified", False),
        "enabled": discord_config.get("enabled", False),
        "running": _verification_state.get("bot_running", False),
        "admin_username": discord_config.get("admin_username"),
    }


@router.post("/start")
async def start_discord_bridge():
    """
    Start the Discord bridge with saved configuration.
    """
    from vaf.core.config import Config
    
    discord_config = Config.get("discord_config", {})
    
    if not discord_config.get("verified"):
        raise HTTPException(status_code=400, detail="Discord not configured. Please complete setup first.")
    
    if not discord_config.get("bot_token"):
        raise HTTPException(status_code=400, detail="Bot token missing.")
    
    # TODO: Implement persistent Discord bridge
    # For now, just mark as running
    _verification_state["bot_running"] = True
    
    return {"status": "started", "message": "Discord bridge started."}


@router.post("/stop")
async def stop_discord_bridge():
    """
    Stop the Discord bridge.
    """
    global _verification_state
    
    _verification_state["bot_running"] = False
    
    # TODO: Actually stop the bridge thread
    
    return {"status": "stopped", "message": "Discord bridge stopped."}
