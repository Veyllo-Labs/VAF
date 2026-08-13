# First Run - Setup Wizard

The first time you open VAF, no admin account exists yet, so VAF shows a setup wizard
instead of the login screen. This page walks through it. Installation comes first - see
[LINUX_SETUP.md](LINUX_SETUP.md), [MACOS_SETUP.md](MACOS_SETUP.md), or
[WINDOWS_SETUP.md](WINDOWS_SETUP.md).

## Where to open it

- **Desktop mode:** the tray opens the web UI automatically, or browse to
  `http://localhost:3000`.
- **Server mode:** open the LAN HTTPS URL shown by the installer / `vaf status` (the
  certificate is self-signed, so your browser will warn once). See
  [SERVER_MODE.md](SERVER_MODE.md).

The wizard is detected automatically (the backend reports "needs setup" while no admin
account exists); these bootstrap endpoints are reachable without a login, by design - see
[NETWORK_FEATURES.md](NETWORK_FEATURES.md).

## Without a browser: `vaf setup`

The account can also be created in the terminal, which is the shorter path when you are
already in one - and the only path on a machine with no browser at all:

```
vaf setup
```

It asks for a username, a password (twice), a name for your AI agent, and optionally a
Veyllo API key. The database it needs runs in Docker, so the command starts the service
stack itself and waits for it.

Both routes create exactly the same account (`vaf/auth/user_admin.py` is the one place
that does it), so afterwards the web UI shows the normal login instead of this wizard,
and **the first login still walks you through two-factor setup**. What the wizard would
also have done - language, theme, the soul questionnaire - is not asked here; those live
in Settings, and your agent starts with a default soul.

Naming the agent is the one thing the terminal asks and the wizard does not. Without it
your agent picks its own name, something like `Nobel4831SkyBlue`.

**For scripts and AI agents.** Give the password on stdin, never as an argument (an
argument is visible in `ps` to every user on the machine):

```
printf 'your-password\n' | vaf setup --username alice --agent-name Jarvis --password-stdin
```

Exit codes: `0` created, `1` failed, `2` cancelled or bad input, `3` an admin already
exists, `4` the database is unavailable.

If you start `vaf run` on a machine that has no account yet, it offers the same setup
right there before opening the chat.

## The steps

The wizard first asks for your **language**, then confirms you have a phone for two-factor
auth, and then runs a four-step progress bar: **Admin → Soul → Veyllo API → 2FA**.

### Language
Pick the UI language (currently English or German). This changes only the web UI; your
agent's Soul is authored and stored in English regardless of this choice. You can switch the
UI language later in Settings.

### Phone check
The final step (2FA) needs an authenticator app on a phone (e.g. Google Authenticator,
Authy). The wizard confirms you have one before continuing. **Cancel** returns to the
language step.

### 1. Create admin account
Choose an admin **username** and a **password** (confirmed). This is the owner account -
it has full access and isolates your data under its own scope.

### 2. Soul (agent personality)
A short questionnaire that defines how your agent behaves, in four parts:

- **Core Truths** - values and what it's for;
- **Boundaries** - rules and limits;
- **Vibe** - tone and communication style;
- **Continuity** - how it should carry context forward.

Suggestions are offered; every field is editable. The questionnaire UI follows your chosen
language, but the Soul itself is written in English. This becomes the agent's Soul - see
[SOUL_SYSTEM.md](../memory/SOUL_SYSTEM.md). It is saved to `~/.vaf/users/<admin>/soul.md`
and can be changed later in Settings.

### 3. Veyllo API (optional)
Optionally paste a **Veyllo API key** to use the hosted Veyllo models - handy if your
machine has limited GPU/VRAM. The key is verified live; on success VAF sets Veyllo as the
default provider (including vision, and speech-to-text when no STT provider was chosen
yet - it always falls back to the local engine and an explicit later choice overrides it).
**Skip** to keep the local default model. You can add
this or other providers later under **Settings → AI & Model**.

> Discord, Telegram, and Email connections are no longer part of first-run setup - add them
> anytime under **Settings → Connections** (see [CONNECTIONS.md](../integrations/CONNECTIONS.md)).

### 4. Two-factor authentication (2FA)
Scan the displayed QR code with an authenticator app (e.g. Google Authenticator, Authy)
and enter the 6-digit code. When you confirm, the wizard commits everything from the
previous steps (admin account, Soul, any Veyllo key) and logs you in.

> **2FA is the last step and is required.** The earlier steps are saved when 2FA is verified.

## What about the model / provider?

The only provider the wizard offers is **Veyllo** (the optional step 3). If you skip it, VAF
starts with a sensible default (a local model, VRAM-adaptive). To switch to another cloud
provider or a different model - or to set Veyllo up later - open **Settings → AI & Model**
after login, choose the provider, and paste your API key. The available providers and model
switching are covered in the main [README](../../README.md) and
[API_INTEGRATION.md](../llm/API_INTEGRATION.md).

## What gets created

- the admin account (with its 2FA secret), in the local users database;
- a few identity keys in `~/.vaf/config.json` (e.g. the admin scope id and username, and
  an auto-generated network JWT secret) - plus your Veyllo provider/key settings if you
  entered a key in step 3;
- your agent's `soul.md` and identity files under `~/.vaf/users/<admin>/`.

After step 4 you land in the main web UI, logged in, with your agent live.

## If a device suddenly warns about the certificate

Only relevant if you turned network access on and installed `~/.vaf/ssl/ca.pem` on
another device to silence its browser warning.

That authority certificate is replaced ONCE, on the first start after updating, because
the one generated before carried no key identifier and was therefore unverifiable to any
program that checks properly - browsers and `curl` accepted it, correctly written
programs did not. See
[NETWORK_FEATURES.md](NETWORK_FEATURES.md) for the measurement behind that.

What you see: every device that trusted the old file warns once. What to do: copy the new
`~/.vaf/ssl/ca.pem` over the old one on those devices. The log says the same thing at
warning level when it happens, and nothing else about your setup changes - your account,
sessions and settings are untouched.
