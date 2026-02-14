# VAF Connections

Connect external apps and services to interact with your VAF agent.

## Available Integrations

### Communication

| Platform | Status | Description |
|----------|--------|-------------|
| **Discord** | ✅ Available | Chat with your agent via Discord DMs or channels |
| **Telegram** | ✅ Available | Use VAF from Telegram; VAF can reach you there (whitelist, per-user) |
| **Email** | ✅ Available | OAuth2 (Google, Microsoft, Apple) or IMAP/SMTP; read and send email via agent |
| Slack | 🔜 Coming Soon | Integrate VAF into your Slack workspace |
| **WhatsApp** | ✅ Available | Chat with your agent on WhatsApp (QR link, per-user isolation) |

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
| Google Drive | ✅ Available | Browse, read, download, and sync files; OAuth2; full Drive access |
| Microsoft OneDrive | ✅ Available | Browse and sync files via Microsoft Graph; OAuth2 |
| Apple iCloud | 🔜 Coming Soon | Access iCloud Drive files on macOS |
| Dropbox | 🔜 Coming Soon | Sync and access Dropbox files |
| Nextcloud | 🔜 Coming Soon | Connect to self-hosted Nextcloud via WebDAV |

## Discord Integration

### Features

- **Real-time chat**: Send messages to your VAF agent via Discord DMs
- **Proactive messaging**: The agent can send you messages via Discord (e.g. "send me the result via Discord") using the `send_discord` tool
- **Admin verification**: Only verified admins can control the bot
- **Persistent bridge**: Messages are routed through the headless agent; replies are sent back to Discord automatically
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
- Ensure Message Content Intent is enabled in Discord Developer Portal
- Verify the Discord bridge is running (Settings → Connections → Discord toggle)
- Send your message as a **Direct Message** to the bot (DMs are supported; server channels require additional setup)

**"Can't find the bot" / "Bot not found"**
- You **must invite the bot to a server first**. Discord does not let you DM a bot unless you share a server with it.
- Use OAuth2 → URL Generator (scope: bot, permissions: Send Messages, Read Message History, View Channels), open the URL, and add the bot to your server.
- Then: Right-click the bot in your server → Message → send the verification code.

**"Verification failed"**
- Click **Start Verification** first, then go to Discord and send the code.
- Send the code via **Direct Message** (right-click bot → Message), not in a server channel.
- Make sure you're sending the exact code (no extra spaces).

## Telegram Integration

### Features

- **Same pipeline as Web UI**: Messages are enqueued on the same task queue; the same agent, tools, RAG, and user scope apply.
- **Whitelist**: Only whitelisted Telegram users can use the bot; each entry maps one Telegram user to one VAF user (user_scope_id, username).
- **Local storage**: Bot token and whitelist are stored in your local VAF config only.

### Setup

1. Go to **Settings → Connections**
2. Click **Connect** on Telegram
3. Follow the setup wizard:
   - Create a bot with [BotFather](https://t.me/BotFather) (`/newbot`)
   - Paste your bot token
   - Verify: send the 6-digit code to your bot in a Telegram DM
   - **Whitelist**: Add your own Telegram (username or account). Enter only your own number or username.
4. Turn the connection **on**; the bridge runs in the same process as the Web server and starts automatically on VAF restart when enabled.

### Message handling

- **Debouncing**: Incoming messages are buffered per chat. After each message, the bridge waits a short period (default 5 seconds, configurable). If another message arrives in that period, the timer resets and the new text is appended. When no further message arrives for the full period, the combined text is sent as a single prompt. This avoids multiple separate requests when the user sends several short messages in a row (e.g. "Hello" then "how are you").
- **Replies**: You receive only the agent’s final reply or an error message. No intermediate "processing" notification. Internal reasoning blocks (e.g. `<think>...</think>`) are removed from replies so you see plain answer text only.
- **Idle and model**: If the model is idle and a Telegram message is received, the model is loaded. It stays loaded for a configurable time after the last Telegram activity when there are no active Web connections (see `telegram_idle_timeout` in config).

### Whitelist and multi-user

- Each whitelist entry links one Telegram user to one VAF user (user_scope_id and username from the Web UI session when that user was added).
- Only whitelisted users receive replies; others see an authorization message.
- RAG, memories, and user identity are scoped per user, same as in the Web UI.

### Session storage and memory compaction (15-message rule)

- **Verlauf:** Telegram chat history is stored in the same place as Web UI sessions: `~/.vaf/sessions/`. Each Telegram user has one session file: `telegram_<user_id>.json`. The dashboard “Session-Verlauf” popup reads from this.
- **Nach-15-Nachrichten-Regel:** The same **session compaction** as in the Web UI applies: every N **main-user** turns (default 15, configurable via `memory_compaction_interval`), the model is prompted to write durable memories into RAG. The prompt includes only **user and assistant messages** (no system or tool content). The count is **cumulative** (e.g. 4 today + 5 tomorrow = 9; at 15 total, compaction runs). Only role **user** (main user of that session) is counted; other participants (relay contacts, other bot users) have separate sessions. Memories are stored under the whitelist user’s `user_scope_id`, so they appear in the same Memory graph and are used in later Web and Telegram chats. Reply length: `memory_compaction_max_tokens` (default 4000). See [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md#session-compaction-background).

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

Global options (top-level in config):

| Option | Default | Description |
|--------|--------|-------------|
| `telegram_idle_timeout` | 120 | Seconds to keep the model loaded after the last Telegram prompt when there are no Web connections. |
| `telegram_debounce_seconds` | 5 | Seconds to wait for follow-up messages in the same chat before sending the combined text as one prompt. Minimum 1. |

### Requirements

- Python package: `python-telegram-bot` (v21+)
- Install via: `pip install python-telegram-bot`

### Troubleshooting

- **"Not authorized"**: Add your Telegram in the wizard whitelist step (your own account only).
- **No reply**: Ensure the bridge is running (Settings → Connections, Telegram toggle on). After a VAF restart, the bridge auto-starts if Telegram is enabled.

## WhatsApp Integration

### Features

- **Per-user isolation**: Each VAF user has their own WhatsApp session. Credentials are stored in `~/.vaf/users/<username>/whatsapp/`. Other users cannot see or use your WhatsApp.
- **QR link**: Scan a QR code with WhatsApp (Linked Devices) to link your phone.
- **Whitelist**: Only configured phone numbers (E.164) can send messages **and** receive replies. Each whitelist entry maps a phone number to a VAF user.
- **Read-only for everyone else**: The bot replies only to numbers in your whitelist. It does not message other contacts or react to messages from non-whitelisted numbers.
- **Node.js required**: Uses Baileys via a Node subprocess. Run `npm install` in `vaf/whatsapp_node/` before first use.
- **Agent tools** (like email): `whatsapp_inbox`, `find_whatsapp_messages`, `read_whatsapp_chat` list chats, search messages, and read a chat. Messages are stored as they arrive. `send_whatsapp` supports optional `voice_lang` (e.g. `"de"`, `"en"`) for voice messages. `whatsapp_call` is a placeholder (not implemented).
- **Voice (TTS/STT)**: Incoming voice messages are downloaded, transcribed via Whisper STT (speech_stt_docker_url, default localhost:5003), and passed as text to the agent. When the user sends a voice message, replies can automatically be sent as voice (TTS) in the detected language. The agent can also explicitly send voice via `send_whatsapp(voice_lang="de")` or `send_telegram(voice_lang="de")`.

#### Agent WhatsApp tools (whatsapp_inbox, find_whatsapp_messages, read_whatsapp_chat, send_whatsapp)

| Tool | Purpose | When to use |
|------|---------|-------------|
| `whatsapp_inbox` | List WhatsApp chats. Returns chat_id, name, last_ts. Use `find_whatsapp_messages` to search; `read_whatsapp_chat` to read a chat. | User asks "list WhatsApp chats" or "show my WhatsApp conversations". |
| `find_whatsapp_messages` | Search messages by query (matches body, chat name, sender). Optional `chat_id` to limit to one chat. | User asks "find messages from Anne" or "what did X say in WhatsApp" → find_whatsapp_messages(query="Anne"). |
| `read_whatsapp_chat` | Read messages from a chat (`chat_id`, `limit`). Use chat_id from whatsapp_inbox or find_whatsapp_messages. | read_whatsapp_chat(chat_id="+49...") for full thread. |
| `send_whatsapp` | Send text or voice message. Pass `voice_lang` (e.g. `"de"`) to send as voice message instead of text. | User asks to receive something via WhatsApp; use voice_lang when they prefer voice. |

### Setup

1. Install Node.js (>= 18) and run `npm install` in `vaf/whatsapp_node/`.
2. Go to **Settings → Connections**
3. Click **Connect** on WhatsApp
4. Scan the QR code with WhatsApp on your phone (Linked Devices)
5. Your phone number is automatically added to the whitelist from the linked WhatsApp account.
6. Turn the connection **on**; the bridge starts automatically when enabled.

### WhatsApp Dashboard

The WhatsApp dashboard (Settings → Connections → Dashboard) shows:

- **Connection status** (indicator next to "Chats"): Green = WhatsApp connected, amber = bridge running but not connected, gray = bridge not started.
- **Refresh (↻)**: Refreshes chat list and re-checks connection status (ping/pong with the Node bridge).
- **Restart bridge**: When "Bridge running, WhatsApp not connected" appears, use the "Restart bridge" button to stop and restart the bridge. Wait 20–30 seconds, then refresh.
- **send_whatsapp** now verifies delivery: The tool waits for confirmation from the Node bridge. If the message fails (e.g. "WhatsApp not connected"), the agent receives an error instead of a fake success.

### Configuration

```json
{
  "whatsapp_config": {
    "enabled": true,
    "whitelist": [
      {
        "phone_number": "+49123456789",
        "user_scope_id": "uuid-from-auth",
        "vaf_username": "your_vaf_username"
      }
    ]
  }
}
```

### Troubleshooting

- **QR/Link debugging**: Wa-bridge stderr (including all `connection.update` events) is logged to `logs/whatsapp_qr.log`. After QR scan: WhatsApp disconnects with 515/516 → wa-bridge creates a new socket with stored credentials → `open`. If „logging in“ stays stuck on the phone, it's often the computer (network/firewall).
- **"Node.js not found"**: Install Node.js 18+ and ensure it is in your PATH.
- **"wa-bridge.js not found"**: Run `npm install` in `vaf/whatsapp_node/`.
- **Black terminal / no QR code**: Install Node.js 18+, run `npm install` in `vaf/whatsapp_node/`, restart VAF.
- **No reply**: Ensure the bridge is running (Settings → Connections, WhatsApp toggle on) and your phone number is in the whitelist.
- **Bridge running, WhatsApp not connected**: The Node process is alive but the Baileys socket is not connected. Use "Restart bridge" in the WhatsApp Dashboard, or Settings → Connections → Stop, then Start. Ensure your phone has internet and WhatsApp is open. If it still fails, use Reset and scan a new QR code.
- **Auto-disconnect on session expiry**: When the bridge needs a new QR (session invalid) but cannot display it, VAF stops the bridge and sets the toggle to OFF. Message: "Session expired. Log in again: Reset, scan QR, turn ON." OpenClaw has the same constraint (Baileys session can expire); they use `clawdbot channels login` to re-pair. We use Reset in the UI.
- **Restart doesn't help**: If "Restart bridge" keeps showing amber (not connected) after 20–30 seconds:
  - **1. Reset and new QR**: The session may be invalid. Settings → Connections → WhatsApp → Reset & get new QR code. Scan with your phone; wait for "Linked".
  - **2. Check logs**: Open `logs/whatsapp_qr.log`. Look for `connection.update: connection=close` and the status code (401, 515, etc.). 401/device_removed → Reset; 515/516 → wait, Baileys auto-reconnects.
  - **3. Network**: Test [web.whatsapp.com](https://web.whatsapp.com) in a browser on the same PC. If that fails, the issue is network/firewall. Disable VPN; try a different network.
  - **4. VPS/server**: Some server IPs are blocked by WhatsApp. A home/office PC often works better.
- **send_whatsapp reports success but no message on phone**:
  - Check `logs/whatsapp_reply.log`: Look for `SENDER ok` (message was sent to Node) or `DROPPED process_not_running` / `ERROR` (send failed).
  - **Phone number format**: Whitelist must use E.164 (e.g. `+491761234567`), not `0176...`. Wrong format → wrong JID → message may not reach you.
  - **Bridge/Process**: Settings → Connections → WhatsApp → Stop, then Start. Ensure "Linked" and QR was scanned successfully.
- **Self-chat (messaging yourself): Bot doesn't respond**:
  - Check `logs/whatsapp_inbound.log`: Look for `ACCEPT`, `ENQUEUED`, `HEADLESS processing` (message was received and processed) or `SKIP`, `REJECT` (message was filtered).
  - Bridge must be running; your number in whitelist. If using a newer WhatsApp account with LID format, the bridge resolves it automatically.
- **Few or no chats visible**: VAF uses WhatsApp as a linked device (like WhatsApp Web). WhatsApp decides how many chats to sync – often few or none. The chat list fills when someone messages you (`chats.upsert`). **Diagnosis**: `GET /api/whatsapp/dashboard/debug` shows `raw_chats_count`; if 0, Baileys has no chat list. **Tips**: Restart bridge, wait 30–60 s, open dashboard, click Refresh; check wa-bridge stderr for `messaging-history.set: X chats`.
- **Code 515 ("restart required")**: Common shortly after QR scan. Baileys reconnects automatically – wait 10–20 seconds, no reset needed.
- **Loading ~30 seconds, then error**: The problem is on the **VAF machine** (not the phone). The machine cannot establish a stable connection to WhatsApp servers. Test: Open [web.whatsapp.com](https://web.whatsapp.com) in a browser on the same PC. If that also fails, the issue is network/firewall.
- **401 / device_removed**: Often the issue is on the **VAF machine**:
  - **VPN**: Disable VPN on the PC and try again.
  - **Network**: VPS/server IPs can be blocked by WhatsApp. A home/office PC with normal internet often works better.
  - **Wait**: After several failures, wait 24 hours, then try again.
  - **Other network**: Test VAF on a different machine/network.
  - After each failure: Click "Reset & get new QR code" before scanning again.
- **Verification timeout**: Send the exact 6-digit code to the bot in a **private chat** (DM).

## Proactive messaging

When you have one or more messaging connections (e.g. Telegram, Discord), the agent can **send you proactive messages**—for example when you ask it to "send me the result via Telegram" or "tell me how full my desktop is and send that to me".

- **System prompt**: The agent is informed which channels are available for the current user and whether a preferred channel (`main_messenger`) is set. This is stored in User Identity (see [USER_IDENTITY.md](USER_IDENTITY.md)).
- **Tool availability**: Only tools for **configured** connections are exposed to the agent: `send_telegram` when Telegram is connected, `send_discord` when Discord is connected, `send_slack` for Slack (when supported), and `send_whatsapp` when WhatsApp is linked. The agent never sees a send tool for a channel you do not have.
- **First time**: If you have not set a preferred channel, the agent will ask once (e.g. "Should I send it via Discord, Telegram or Slack?") and store your answer in User Identity as `main_messenger` (via the `update_user_identity` tool).
- **Sending**: The agent uses the matching tool (`send_telegram`, `send_discord`, `send_slack`, or `send_whatsapp`) to deliver the content. For **Telegram**, the agent can only send to you after you have sent at least one message from Telegram (so VAF can associate your chat ID). For **WhatsApp**, the whitelist phone number is used. Chat IDs / endpoints are stored in `messaging_endpoints.json` under the platform data directory.
- **Discord**: Proactive send to Discord is planned for a later phase; Telegram is supported first.

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
1. Telegram bridge receives messages, looks up the sender in the whitelist (user_scope_id, username)
2. Messages are debounced per chat (wait for follow-up messages, then combine into one prompt)
3. A single task is enqueued on the same TaskQueue as the Web UI, with metadata (user_scope_id, username, telegram_chat_id)
4. Headless runner processes the task; RAG, tools, and user identity are scoped to that user
5. The agent reply (without internal reasoning blocks) is sent back to the Telegram chat via the bridge

## Email Integration

### Features

- **Multiple accounts**: Connect **Gmail** (OAuth2 + Gmail API), **Microsoft Outlook** (OAuth2 + Microsoft Graph Mail), or any provider via **IMAP/SMTP**. **iCloud Mail** has no OAuth mail API; use IMAP with an app-specific password (see Apple / iCloud below).
- **Secure storage**: OAuth tokens and IMAP passwords are stored in the OS keyring (Windows Credential Manager, macOS Keychain, Linux Secret Service). If the keyring is unavailable, credentials are stored in an AES-256-GCM encrypted file under the platform data directory. No passwords or tokens are stored in `config.json`.
- **Agent tools**: When at least one email account is connected, the agent can use `mail_inbox`, `find_mail`, `read_mail`, `mark_mail_answered`, and `send_mail`. Credentials are never passed to the agent; the transport layer resolves them by `account_id`. Access tokens are refreshed automatically when expired.

#### Agent email tools (mail_inbox, find_mail, read_mail, mark_mail_answered, send_mail)

| Tool | Purpose | When to use |
|------|---------|-------------|
| `mail_inbox` | List messages in a folder (inbox or other). **Omit `account_id`** to list from **all connected accounts** (e.g. "do we have mails about CVEs?"); returns From, Date, Subject, and per message `account_id`, `message_id`, `provider_message_id`. | User asks to check email, show inbox, or search across all mails (no need to ask for an account). |
| `find_mail` | Search the synced mailbox by subject or sender (`query`, optional `folder`, `limit`). Returns matches with `account_id`, `message_id`, `provider_message_id`; if exactly one match, returns the full body. | User asks "what does the X mail say?" or "details about the Postman/Twitch/… email" → use find_mail(query="X"); if result includes full body use it, else call read_mail with first match's IDs. |
| `read_mail` | Return the full body of one message as plain text. Parameters: `account_id`, `message_id`, `folder` (default INBOX), optional `provider_message_id`. Use IDs from find_mail or mail_inbox. | When you have account_id and message_id (e.g. from find_mail); do not ask the user for these. |
| `mark_mail_answered` | Mark a message as answered by the agent (`account_id`, `message_id`, `folder`). Sets a timestamp so the Mail UI shows an answered indicator and the message is not handled twice. | After the agent has processed or replied to an email. |
| `send_mail` | Send an email (`account_id`, `to`, `subject`, `body`; optional `attachment_paths` for documents). Paths support folder aliases (Downloads, Desktop, Documents). | User asks to send or reply to an email; for documents pass `attachment_paths`. |

Message bodies are always returned as plain text: HTML and MIME structure are stripped, and the same cleaned text is used in the Mail dashboard and for the agent. This keeps context size low and avoids raw markup.

### Setup

1. Go to **Settings → Connections** and click **Connect** on Email.
2. Choose a provider:
   - **Google (Gmail)** / **Microsoft (Outlook)**: If an admin has set the OAuth client (see below), **Sign in with Google** or **Sign in with Microsoft** works with one click for all users. Otherwise use **Other (IMAP/SMTP)** (e.g. Gmail with app password). Mail is read/sent via Gmail API / Microsoft Graph when OAuth is used, or via IMAP/SMTP otherwise.
   - **Admin setup (once per instance)**: In the mail wizard, expand **For admins: OAuth client**. Enter Google and/or Microsoft Client ID and Client secret (from a Web application OAuth app), then Save. After that, everyone on this VAF instance gets one-click sign-in. Redirect URI: `http://127.0.0.1:8001/api/email/oauth/callback`. Do not commit the secret to source code. See “Gmail: Desktop vs Web OAuth client” below for creating the OAuth app.
   - **Apple (iCloud Mail)**: No OAuth mail API. The wizard shows a notice: use **Other (IMAP/SMTP)** and enter your iCloud email and an app-specific password (Apple ID → Sign-In and Security → App-Specific Passwords).
   - **Other (IMAP/SMTP)**: Enter your email and password (or app password if 2FA). Optionally set IMAP/SMTP host/port; defaults are used for known domains (Gmail, Outlook, Yahoo, iCloud).
3. Manage accounts in the wizard: list, verify connection, add another, or remove (credentials are deleted and the account is removed from config).

### OAuth2 (Google, Microsoft)

- **Good UX (like other local apps)**: The app can ship a **default** OAuth client ID so users never open Google Cloud Console. Distributors/packagers set env vars: `VAF_EMAIL_OAUTH_GOOGLE_CLIENT_ID`, `VAF_EMAIL_OAUTH_MICROSOFT_CLIENT_ID` (and optionally `VAF_EMAIL_OAUTH_GOOGLE_CLIENT_SECRET`, `VAF_EMAIL_OAUTH_MICROSOFT_CLIENT_SECRET`). User override in Settings (config) takes precedence over env.
- **Flow**: Authorization Code with PKCE; redirect to the **local** VAF backend (e.g. `http://127.0.0.1:8001/api/email/oauth/callback`). VAF is a **desktop/local** app—no public web app; the browser redirects to your machine’s backend, which exchanges the code for tokens.

#### Gmail: Desktop vs Web OAuth client (Google)

- **Desktop client** (type “Desktop” in Google Cloud): No client secret; PKCE only. Per [Google’s docs](https://developers.google.com/identity/protocols/oauth2/native-app), `client_secret` is optional for installed apps. In practice, when the **token exchange** is done by a **server** (your VAF backend), Google’s token endpoint may still return **“client_secret is missing”**. If you see that error, use a **Web application** client (below).
- **Web application client** (recommended if you get “client_secret is missing”): In [Google Cloud Console → APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials), create an OAuth 2.0 Client ID of type **Web application**. Add authorized redirect URI: `http://127.0.0.1:8001/api/email/oauth/callback` (and optionally `http://localhost:8001/api/email/oauth/callback` if your backend is reachable as localhost). Enable Gmail API for the project. Set **Client ID** and **Client secret** in VAF: env vars `VAF_EMAIL_OAUTH_GOOGLE_CLIENT_ID` and `VAF_EMAIL_OAUTH_GOOGLE_CLIENT_SECRET`, or Settings → Connections → Email OAuth. This is how many local/desktop apps that use a local callback server work (e.g. VS Code, Slack): they use a Web client and keep the secret only on the deployer’s machine.
- **Redirect URI**: Must match exactly (including port). Default is `http://127.0.0.1:<local_network_port>/api/email/oauth/callback` (see config `email_oauth_callback_base_url` or backend port).
- **Scopes**: **Gmail**: `gmail.readonly`, `gmail.send`, `userinfo.email`. **Microsoft**: `User.Read`, `Mail.Read`, `Mail.Send`, `offline_access`, `openid`.
- **Token storage**: Access and refresh tokens are stored only in the keyring (or encrypted fallback file). Config holds only account metadata (`email_config.accounts`: `account_id`, `provider`, `email`, `enabled`, `last_verified_at`, optional server fields for IMAP).
- **Transport**: Gmail uses the Gmail REST API (messages list/get/send). Microsoft uses Microsoft Graph (`/me/messages`, `/me/sendMail`). Tokens are refreshed automatically when expired.
- **Verify**: Use **Verify** in the wizard to re-test the connection (Gmail: profile; Microsoft: GET /me; IMAP: NOOP). Success updates `last_verified_at`.
- **Revocation**: When you remove an account in the UI, tokens are deleted locally. Optionally revoke the grant in the provider’s security settings (Google Account, Microsoft Account).

### Apple / iCloud Mail

- **Sign in with Apple** provides identity only; it does **not** grant mail read/send access. iCloud Mail does not expose a public OAuth Mail API. To connect iCloud Mail, use **Other (IMAP/SMTP)** and enter your iCloud email and an **app-specific password** (Apple ID → Sign-In and Security → App-Specific Passwords). Defaults: `imap.mail.me.com` / `smtp.mail.me.com`.

### IMAP/SMTP fallback

- For providers that do not support OAuth (or if you choose “Other mail provider”), use email + password or app password.
- **Gmail with 2FA**: Prefer **Google (Gmail)** OAuth in the wizard. If you use IMAP with Gmail and 2FA, you need an [App Password](https://myaccount.google.com/apppasswords) (create one there).
- **Microsoft/Outlook.com**: Outlook.com no longer supports IMAP with password (Microsoft retired Basic auth in 2024). Use **Sign in with Microsoft** (OAuth) in the wizard; an admin must configure the OAuth client first.
- **TLS**: IMAP and SMTP use TLS; certificate verification is enabled.
- **Defaults**: Known domains (gmail.com, outlook.com, yahoo.com, icloud.com, me.com, etc.) get default IMAP/SMTP host/port; you can override in the advanced options.

### Configuration (metadata only)

In `config.json`, `email_config` contains only:

```json
{
  "email_config": {
    "accounts": [
      {
        "account_id": "user@gmail.com",
        "provider": "gmail",
        "email": "user@gmail.com",
        "enabled": true
      },
      {
        "account_id": "other@example.com",
        "provider": "imap",
        "email": "other@example.com",
        "enabled": true,
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "smtp_host": "smtp.example.com",
        "smtp_port": 587
      }
    ]
  }
}
```

You can optionally add **sender rules** so that messages from certain senders are auto-labelled (e.g. Social or Promotions). Rules apply to **new** syncs and can be **re-applied to existing** messages (backfill).

- **Config key**: `sender_category_rules` inside `email_config` (single-user) or inside each user’s object in `email_config_by_user` (multi-user).
- **Format**: Array of `{ "pattern": "substring", "category": "social" }`. The pattern is matched case-insensitively against the full **From** header (e.g. `Twitch <no-reply@twitch.tv>`). First match wins. Category is normalized (e.g. `social`, `promotions`, `primary`, or a custom label).
- **Example** (mark Twitch and similar as Social):

```json
"email_config": {
  "accounts": [ ... ],
  "sender_category_rules": [
    { "pattern": "twitch.tv", "category": "social" },
    { "pattern": "newsletter@", "category": "promotions" }
  ]
}
```

- **New syncs**: When mail is fetched (Gmail, Microsoft, IMAP), each message’s category is set from the provider (Gmail labels) or from sender rules. So new mails get the right label automatically.
- **Auto-sync every 30 min**: If the user enables "Auto sync every 30 min" for an account in Settings → Connections → Email, the backend runs a periodic task that syncs all such accounts every 30 minutes. The first run is 60 seconds after server startup; mail is updated even when the Mail dashboard or browser is closed, as long as the VAF server (web or headless) is running.
- **Label in UI**: When the user changes a message's label in the Mail dashboard (Primary, Social, Promotions, or custom), the backend automatically adds a sender rule for that message's From address and applies it to all synced messages from that sender (existing and future). No extra action is required.
- **Manual backfill**: If you edit `sender_category_rules` in config by hand, call **POST** `/api/email/messages/apply-sender-rules` (with auth) to re-apply rules to all synced messages. Response: `{ "ok": true, "updated": 42 }`.

### Mail sync store (SQLite)

- **Path**: By default the DB file is `email_sync.db` in the platform data dir (e.g. `%LOCALAPPDATA%\\vaf` on Windows). To use a Docker volume or custom path, set the environment variable **`VAF_EMAIL_SYNC_DB`** to the full path of the SQLite file (e.g. `/data/vaf/email_sync.db`). The parent directory is created if missing. **Best practice (Docker):** If you run VAF in Docker, mount a volume (e.g. at `/data/vaf`) and set `VAF_EMAIL_SYNC_DB=/data/vaf/email_sync.db` so the Mail DB lives in the volume and is persistent.
- **Version control**: Database files (`*.db`, `*.db-wal`, `*.db-shm`) are listed in `.gitignore`; synced mail data is never committed to the repository.
- **Per-user DB (network mode)**: When multiple users are enabled (network / login), **each user gets their own SQLite file**: `data_dir/users/{username}/email_sync.db`. So User A and User B never share mail data; each user's Mail dashboard and agent tools only see that user's synced mails.
- **Retention**: Messages older than **90 days** (by message date, or by sync date if the message date cannot be parsed) are **deleted automatically** on each sync. This keeps the store size bounded.
- **Answered flag**: When the agent has processed or replied to a message, it can call the **`mark_mail_answered`** tool so the message is marked with a timestamp. The Mail UI then shows an answered timestamp and an "Answered" badge in the list, so the same mail is not handled twice.

OAuth client IDs (optional) at top level: `email_oauth_google_client_id`, `email_oauth_google_client_secret`, and similarly for Microsoft and Apple. Credentials (tokens, passwords) are never stored in config.

### Secure credential storage (OS-independent)

- **Per-account credentials** (OAuth access/refresh tokens, IMAP/SMTP passwords) are **never** stored in `config.json`. They are stored in:
  - **Keyring** when available: Windows Credential Manager, macOS Keychain, or Linux Secret Service (via the same API), so behaviour is OS-independent.
  - **Fallback**: If keyring is unavailable, an AES-256-GCM encrypted file under the platform data directory (see `Platform.data_dir()`) is used; the encryption key is stored in config. Paths and behaviour are cross-platform.
- **OAuth client ID and client secret** (app-level, set by admin) are stored in config when provided. Do **not** commit config that contains client secrets to source control. Where the OS allows, use restrictive file permissions on the config file (e.g. `chmod 600` on Unix; on Windows, ensure only the running user can read the file).

### Multi-user (network) mode

When **local network** is enabled and users log in (e.g. Max and Susan), email data is **scoped per user**:

- **Account list**: Each user only sees and manages their own email accounts. Max’s accounts are stored under his user; Susan’s under hers. The backend uses `email_config` for the local admin (single-user) and `email_config_by_user[username]` for named users.
- **Credentials**: Stored with a user-scoped key (e.g. keyring entry includes the username). So Max’s and Susan’s credentials are isolated.
- **Synced messages**: The email sync store (SQLite) has a `username` column. List/sync only reads and writes rows for the current user. **Susan cannot see Max’s synced emails, and Max cannot see Susan’s.**

Single-user (no login or local admin) continues to use the legacy `email_config` and unscoped credentials/sync store.

## Cloud Storage Integration

### Features

- **Browse full cloud**: Navigate entire Google Drive or OneDrive (not limited to a single folder). OAuth scopes include `drive.readonly` for Google.
- **Read without local copy**: Extract document content (PDF, Word, Google Docs, etc.) via API; no permanent download required.
- **Download**: Save files from cloud to the user's Downloads folder by `file_id`.
- **Upload (VAF Sync)**: Copy local files into the VAF Sync folder; they are uploaded on the next sync.
- **Sync**: Optional bi-directional sync of the "VAF Sync" folder between local storage and cloud.
- **Agent tools**: The `cloud_storage` tool is available to the main agent and to the Librarian sub-agent. Use for browse, download, read, save, list, and status.

### Setup

1. Go to **Settings → Connections** and open the Cloud section.
2. Click **Add account** and choose **Google Drive** or **OneDrive**.
3. **Google Drive**: Sign in with Google. If OAuth is not configured, an admin must set Client ID and Secret first (see below).
4. **OneDrive**: Sign in with Microsoft.
5. After connection, open the **Cloud Dashboard** (gear icon) to browse your drive and trigger sync.

### OAuth setup (admin, once per instance)

For **Google Drive**, create an OAuth 2.0 Client (Web application) in [Google Cloud Console](https://console.cloud.google.com/apis/credentials):

1. Enable **Google Drive API** for the project.
2. Add redirect URIs: `http://127.0.0.1:8001/api/cloud/oauth/callback` and `http://localhost:8001/api/cloud/oauth/callback` (adjust port if needed).
3. Set `cloud_oauth_google_client_id` and `cloud_oauth_google_client_secret` in config or env: `VAF_CLOUD_OAUTH_GOOGLE_CLIENT_ID`, `VAF_CLOUD_OAUTH_GOOGLE_CLIENT_SECRET`.

**Scopes**: `drive.readonly` (browse full Drive), `drive.file` (read/write app-created files, e.g. VAF Sync), `userinfo.email`.

**OneDrive** uses Microsoft Graph; scopes: `Files.ReadWrite`, `User.Read`, `offline_access`. Configure `cloud_oauth_microsoft_client_id` and `cloud_oauth_microsoft_client_secret`.

### Agent cloud_storage tool

| Action | Parameters | Purpose |
|--------|------------|---------|
| `search` | `query` (required), `mime_type` (optional) | Search entire cloud by filename in one call; returns matches with `file_id` (preferred for finding files) |
| `browse` | `folder_id` (default `root`) | List folders and files in cloud; returns `file_id` for navigation |
| `read` | `file_id` | Download to temp, extract text with Librarian, return content, delete temp |
| `download` | `file_id` | Download file to user's Downloads folder |
| `save` | `file_path`, `remote_path` | Copy local file into VAF Sync folder for next sync |
| `list` | — | List files in local VAF Sync folder |
| `retrieve` | `file_path` | Copy file from local sync folder to Downloads |
| `status` | — | Check connection and last sync time |

**Typical flow for "find and read document"**: `search` with `query` (e.g. "approval", "report") → `read` or `download` with `file_id` from results. Use `browse` only when listing folder contents.

### VAF Sync folder

- A "VAF Sync" folder is created in the cloud root (Google Drive, OneDrive, etc.).
- Optional bi-directional sync keeps this folder in sync with a local directory.
- The agent can browse the **entire** drive; sync is optional for specific use cases.

### Credential storage

OAuth tokens are stored in the OS keyring (or encrypted fallback file), same pattern as Email. Config holds only account metadata (`cloud_config.accounts`: `account_id`, `provider`, `display_name`, `sync_enabled`).

## Future Integrations

We're working on additional integrations:

- **Slack**: OAuth-based workspace integration
- **WhatsApp**: Via WhatsApp Business API
