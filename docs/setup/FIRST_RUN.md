# First Run — Setup Wizard

The first time you open VAF, no admin account exists yet, so VAF shows a setup wizard
instead of the login screen. This page walks through it. Installation comes first — see
[LINUX_SETUP.md](LINUX_SETUP.md), [MACOS_SETUP.md](MACOS_SETUP.md), or
[WINDOWS_SETUP.md](WINDOWS_SETUP.md).

## Where to open it

- **Desktop mode:** the tray opens the web UI automatically, or browse to
  `http://localhost:3000`.
- **Server mode:** open the LAN HTTPS URL shown by the installer / `vaf status` (the
  certificate is self-signed, so your browser will warn once). See
  [SERVER_MODE.md](SERVER_MODE.md).

The wizard is detected automatically (the backend reports "needs setup" while no admin
account exists); these bootstrap endpoints are reachable without a login, by design — see
[NETWORK_FEATURES.md](NETWORK_FEATURES.md).

## The four steps

The wizard has a progress bar with four steps, in this order:

### 1. Create admin account
Choose an admin **username** and a **password** (confirmed). This is the owner account —
it has full access and isolates your data under its own scope.

### 2. Soul (agent personality)
A short questionnaire that defines how your agent behaves, in four parts:

- **Core Truths** — values and what it's for;
- **Boundaries** — rules and limits;
- **Vibe** — tone and communication style;
- **Continuity** — how it should carry context forward.

Suggestions are offered; every field is editable. This becomes the agent's Soul — see
[SOUL_SYSTEM.md](../memory/SOUL_SYSTEM.md). It is saved to `~/.vaf/users/<admin>/soul.md`
and can be changed later in Settings.

### 3. Connections (optional)
Optionally connect Discord, Telegram, or Email now. All of these are optional and can be
added later under **Settings → Connections** — see
[CONNECTIONS.md](../integrations/CONNECTIONS.md). Skip the ones you don't need.

### 4. Two-factor authentication (2FA)
Scan the displayed QR code with an authenticator app (e.g. Google Authenticator, Authy)
and enter the 6-digit code. When you confirm, the wizard commits everything from the
previous steps (admin account, Soul, any connections) and logs you in.

> **2FA is the last step and is required.** The earlier Soul and Connections steps come
> before it and are saved when 2FA is verified.

## What about the model / provider?

The wizard does **not** ask which LLM to use. VAF starts with a sensible default
(a local model, VRAM-adaptive). To switch to a cloud provider or a different model, open
**Settings → AI & Model** after login, choose the provider, and paste your API key. The
available providers and model switching are covered in the main
[README](../../README.md) and [API_INTEGRATION.md](../llm/API_INTEGRATION.md).

## What gets created

- the admin account (with its 2FA secret), in the local users database;
- a few identity keys in `~/.vaf/config.json` (e.g. the admin scope id and username, and
  an auto-generated network JWT secret);
- your agent's `soul.md` and identity files under `~/.vaf/users/<admin>/`.

After step 4 you land in the main web UI, logged in, with your agent live.
