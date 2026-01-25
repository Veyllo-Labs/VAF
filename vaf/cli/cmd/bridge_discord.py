import discord
import asyncio
import websockets
import json
import os
import logging
from vaf.core.protocol import CommandRequest, AgentPromptPayload, EventFrame

# Logging Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vaf.bridge.discord")

class DiscordBridge(discord.Client):
    """
    Translates between Discord and VAF Gateway.
    No logic here, just routing.
    """
    def __init__(self, gateway_url: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gateway_url = gateway_url
        self.ws = None
        self.channel_map = {} # discord_channel_id -> last_msg_id (for context)

    async def on_ready(self):
        logger.info(f"Logged into Discord as {self.user} (ID: {self.user.id})")
        # Connect to VAF Gateway via WebSockets
        try:
            self.ws = await websockets.connect(f"{self.gateway_url}/ws/discord_bridge?client_type=bridge")
            logger.info(f"Connected to VAF Gateway at {self.gateway_url}")
            # Start listening for messages from the Gateway
            self.loop.create_task(self.listen_to_gateway())
        except Exception as e:
            logger.error(f"Failed to connect to Gateway: {e}")

    async def on_message(self, message):
        # Ignore own messages
        if message.author == self.user:
            return

        logger.info(f"Discord Msg from {message.author}: {message.content}")

        # Construct the VAF Protocol Message
        prompt = CommandRequest(
            source=f"discord:{message.author}",
            type="agent.prompt",
            payload=AgentPromptPayload(
                text=message.content,
                context={
                    "channel_id": message.channel.id,
                    "author_id": message.author.id,
                    "platform": "discord"
                }
            ).model_dump()
        )

        if self.ws:
            try:
                await self.ws.send(prompt.model_dump_json())
            except Exception as e:
                logger.error(f"Failed to send to Gateway: {e}")

    async def listen_to_gateway(self):
        """Listens for responses from VAF and posts them to Discord."""
        try:
            async for message in self.ws:
                data = json.loads(message)
                # We expect an EventFrame with type 'response'
                if data.get("type") == "response":
                    payload = data.get("payload", {})
                    text = payload.get("text", "No response text.")
                    
                    # We need to know where to send this. 
                    # In a simple bridge, we might use the context we sent earlier.
                    # For now, we assume the gateway echoed the context back or we use a fixed channel.
                    # Real implementation would route via 'target' or specific payload metadata.
                    
                    # For this prototype: find channel from context or use a default
                    channel_id = data.get("context", {}).get("channel_id") # If gateway provides it back
                    # OR we just send to the first available text channel for the test
                    
                    if not channel_id:
                        # Fallback: Just log it
                        logger.info(f"Agent Response (no channel): {text}")
                        continue
                        
                    channel = self.get_channel(int(channel_id))
                    if channel:
                        await channel.send(text)
        except Exception as e:
            logger.error(f"Gateway connection lost: {e}")
            # Reconnect logic would go here

def run_bridge(token: str, gateway_url: str = "ws://127.0.0.1:8000"):
    intents = discord.Intents.default()
    intents.message_content = True
    
    bridge = DiscordBridge(gateway_url=gateway_url, intents=intents)
    bridge.run(token)

if __name__ == "__main__":
    # Get token from env
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("Error: DISCORD_TOKEN environment variable not set.")
    else:
        run_bridge(token)
