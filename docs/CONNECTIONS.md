# VAF Connections

Connect external apps and services to interact with your VAF agent.

## Available Integrations

### Communication

| Platform | Status | Description |
|----------|--------|-------------|
| **Discord** | ✅ Available | Chat with your agent via Discord DMs or channels |
| **Telegram** | ✅ Available | Use VAF from Telegram; VAF can reach you there (whitelist, per-user) |
| Slack | 🔜 Coming Soon | Integrate VAF into your Slack workspace |
| WhatsApp | 🔜 Coming Soon | Chat with your agent on WhatsApp |
| Email | 🔜 Coming Soon | Receive and respond to emails automatically |

### Calendar

| Platform | Status | Description |
|----------|--------|-------------|
| Google Calendar | 🔜 Coming Soon | Sync events, create reminders, manage your calendar |
| Microsoft Outlook | 🔜 Coming Soon | Connect to Outlook/Microsoft 365 calendar |
| Apple Calendar | 🔜 Coming Soon | Sync with iCloud Calendar on macOS |
| CalDAV (Local) | 🔜 Coming Soon | Connect to any CalDAV server (Nextcloud, etc.) |

### Cloud Storage

| Platform | Status | Description |
|----------|--------|-------------|
| Google Drive | 🔜 Coming Soon | Access and manage files on Google Drive |
| Microsoft OneDrive | 🔜 Coming Soon | Sync files with OneDrive / SharePoint |
| Apple iCloud | 🔜 Coming Soon | Access iCloud Drive files on macOS |
| Dropbox | 🔜 Coming Soon | Sync and access Dropbox files |
| Nextcloud | 🔜 Coming Soon | Connect to self-hosted Nextcloud instance |

## Discord Integration

### Features

- **Real-time chat**: Send messages to your VAF agent via Discord
- **Admin verification**: Only verified admins can control the bot
- **Secure**: Token stored locally, never sent to external servers

### Setup

1. Go to **Settings → Connections**
2. Click **Connect** on Discord
3. Follow the setup wizard:
   - Create a Discord bot on the [Developer Portal](https://discord.com/developers/applications)
   - Copy your bot token
   - Verify your identity by sending a code via DM

### Creating a Discord Bot

1. Visit [discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **"New Application"** and name it (e.g., "VAF Agent")
3. Go to **"Bot"** in the sidebar
4. Click **"Add Bot"**
5. Enable **Privileged Gateway Intents**:
   - ✅ Message Content Intent
   - ✅ Server Members Intent (optional)
6. Click **"Reset Token"** and copy the token

### Inviting the Bot to Your Server

1. Go to **OAuth2 → URL Generator**
2. Select scopes: `bot`
3. Select permissions:
   - Send Messages
   - Read Message History
   - View Channels
4. Copy the generated URL and open it to invite the bot

### Admin Verification

For security, only verified admins can control the bot:

1. After entering your bot token, you'll receive a 6-digit verification code
2. Send this code as a **Direct Message (DM)** to your bot on Discord
3. Once verified, your Discord user becomes the authorized admin
4. The bot will only respond to messages from the verified admin

### Configuration

The Discord configuration is stored locally in your VAF config:

```json
{
  "discord_config": {
    "bot_token": "your-bot-token",
    "admin_user_id": "123456789",
    "admin_username": "YourUsername",
    "verified": true,
    "enabled": true
  }
}
```

### Requirements

- Python package: `discord.py`
- Install via: `pip install discord.py`

### Troubleshooting

**"Invalid bot token"**
- Make sure you copied the complete token
- Try resetting the token in Discord Developer Portal

**"Bot not responding"**
- Ensure Message Content Intent is enabled
- Check that the bot has been invited to your server
- Verify the bot is online (green status in Discord)

**"Verification failed"**
- Send the code via **Direct Message**, not in a server channel
- Make sure you're sending the exact code (no extra spaces)

## Telegram Integration

### Features

- **Real-time chat**: Use VAF from Telegram like the Web UI; VAF can also reach you there
- **Whitelist**: Only whitelisted Telegram users can use the bot; each maps to one VAF user (same isolation as Web UI)
- **Secure**: Token stored locally; whitelist links Telegram identity to your VAF user (user_scope_id / username)

### Setup (all in English)

1. Go to **Settings → Connections**
2. Click **Connect** on Telegram
3. Follow the setup wizard:
   - Create a bot with [BotFather](https://t.me/BotFather) (`/newbot`)
   - Paste your bot token
   - Verify: send the 6-digit code to your bot in a Telegram DM
   - **Whitelist**: Add your own Telegram (username or account). **Please enter your own number or username, not someone else's!**
4. Turn the connection **on**; the bridge runs in the same process as the Web server

### Whitelist and multi-user

- Each whitelist entry links one Telegram user to one VAF user (user_scope_id and username from the current Web UI session when you add yourself).
- Only whitelisted users get a response; others see: "You are not authorized…"
- RAG, memories, and user identity are scoped per user, same as in the Web UI.

### Configuration

Stored in your VAF config (locally):

```json
{
  "telegram_config": {
    "bot_token": "your-bot-token",
    "verified": true,
    "enabled": true,
    "whitelist": [
      {
        "telegram_user_id": "123456789",
        "telegram_username": "your_username",
        "user_scope_id": "uuid-from-auth",
        "vaf_username": "your_vaf_username"
      }
    ]
  }
}
```

### Requirements

- Python package: `python-telegram-bot` (v21+)
- Install via: `pip install python-telegram-bot`

### Troubleshooting

- **"Not authorized"**: Add your Telegram in the wizard whitelist step (your own account only).
- **Bot not replying**: Ensure the bridge is started (Settings → Connections, toggle Telegram on).
- **Verification timeout**: Send the exact 6-digit code to the bot in a **private chat** (DM).

## Architecture

```
┌──────────────┐     WebSocket      ┌──────────────┐
│   Discord    │◄──────────────────►│  VAF Gateway │
│   (Users)    │                    │   (ws://)    │
└──────────────┘                    └──────────────┘
       │                                   │
       │                                   │
       ▼                                   ▼
┌──────────────┐                    ┌──────────────┐
│ Discord Bot  │                    │  VAF Agent   │
│  (Bridge)    │                    │   (Core)     │
└──────────────┘                    └──────────────┘
```

The Discord bridge:
1. Receives messages from Discord users
2. Forwards them to the VAF Gateway via WebSocket
3. Receives responses from the agent
4. Sends responses back to Discord

Telegram uses the same pipeline as the Web UI:
1. Telegram bridge receives a message, looks up the sender in the whitelist (user_scope_id, username)
2. Enqueues a task on the same TaskQueue as the Web UI, with metadata (user_scope_id, username, telegram_chat_id)
3. Headless runner processes the task; RAG and user identity are scoped to that user
4. When the agent responds, the reply is sent back to the Telegram chat via the bridge

## Future Integrations

We're working on additional integrations:

- **Slack**: OAuth-based workspace integration
- **WhatsApp**: Via WhatsApp Business API
- **Email**: IMAP/SMTP configuration for email automation
