# Connecting the Veyllo App

The Veyllo App on your phone talks to your own agent on your VAF: you chat with it from
anywhere, it sends you its messages (reminders, automation results, sub-agent results), and
your agent can hand a task to the phone (`user_phone`). This page says how the link works,
what each party can see, what it costs and where its limits are.

The code lives in `vaf/core/app_link/` (its module docstring names the pieces), the routes in
`vaf/api/app_link_routes.py`, the terminal commands in `vaf/cli/cmd/devices.py` and
`vaf/cli/cmd/relay.py`.

## Two ways, one protocol

- **Through the Veyllo relay** (from anywhere). VAF opens ONE outbound connection to the
  relay, and only while it is needed: a relay QR code was scanned, or a device uses the
  relay. Without that there is no connection at all. The relay forwards bytes between the
  app and VAF; nothing reaches VAF through it except the link protocol of paired devices.
  The sign-in, the web UI and the API are never reachable through the relay.
- **Directly** (`wss://<address>:<https port>/api/app-link`). Your phone reaches VAF on the
  local network or through your own VPN on the phone. Offered only while Local Network and
  its TLS are on (the protocol is `wss` only); the address is admitted like every other
  connection ([NETWORK_FEATURES.md](../setup/NETWORK_FEATURES.md)). Nothing is sent to
  Veyllo for these messages and they cost nothing.

Both ways carry the same bytes: a TLS 1.3 session between the app and VAF that ends inside
VAF (ALPN `vaf-link/1`). The relay only adds a 17-byte envelope (`0x01` and a 16-byte
connection id). A relay QR code that also lists direct addresses lets the app switch to the
direct way later without pairing again.

## Pairing

Pairing happens in the web UI, in **Settings, Connections, Veyllo App**
(`web/components/connections/VeylloAppSetupWizard.tsx`):

1. The window first says what the app can do, what Veyllo sees on each way and what each way
   costs, and then asks for **your own** 2FA code. That opens a pairing window for your account
   for ten minutes; nobody can create a QR code for another account, an admin neither.
   With the relay switched off and no direct address either, the window says pairing is not
   possible and asks for no code.
2. VAF shows a QR code, drawn on this machine (the routes answer it as a PNG; no renderer
   elsewhere ever sees it), with the time it is still valid. Below it a switch chooses the way:
   **Veyllo Relay** (from anywhere, one cent per message, Veyllo sees only metadata) or
   **Local** (only on your own network or over your VPN, free, nothing through Veyllo).
   Switching issues a new QR code without a second 2FA code and withdraws the earlier one.
   While an admin has the relay switched off there is no switch, only the Local code. The QR
   code carries a one-time value of 32 random bytes, the fingerprint of this machine's local
   certificate authority, and for the relay a separate relay value. Everything secret sits in
   the URL's fragment, which a browser never sends; the code is valid for ten minutes and is
   neither logged nor stored on disk.
3. The app creates a key in the phone's hardware store, checks VAF's certificate against the
   authority's fingerprint from the QR code, and sends a certificate request with the
   one-time value. VAF checks the request first (a bad one does not use up the code), then
   the code: a code used once is "used", an unknown or expired one "expired", a second
   pairing while one waits for confirmation "busy".
4. The window asks "Is this your device?" and shows the phone's model, platform and app version
   and its key fingerprint (four groups of hex, exactly as the app shows them). Only after you
   confirm, within two minutes, is the device created, its certificate issued by VAF's separate
   device authority, and the Veyllo App switched on for your account. The window then says
   "Connected" with the phone's model; in the app, your VAF is called "Main-Agent (VAF)".

The window asks the server every two seconds while it is open, visible and the pairing is still
running. Closing it (the X, Escape, a click beside it) withdraws a QR code that is still live
and refuses a phone that is waiting for the confirmation, so no pairing nobody watches stays
open. Refused, expired and failed pairings each say what happened and offer a new code.

Every refused pairing is a security event (`app_pairing_refused`), every paired and removed
device too (`channel_paired`, `channel_unpaired`).

The protocol's vectors (the QR code, the frames, the fingerprint) are in
`tests/fixtures/app_link/`, made by `scripts/app_link_vectors.py`.

## What each party sees

- **The relay (Veyllo)** sees that this installation has an instance, how many devices are
  members, their IP addresses, when they connect and how many bytes flow. It cannot read the
  content and cannot act as VAF: the app checks VAF's certificate against the authority from
  the QR code, and VAF knows a device only by its certificate. A relay that changes, replays,
  reorders or cuts bytes ends the session; a relay that moves one device's stream to another
  connection id still gets that device, because the id never decides who is talking. The
  relay's connection goes only to the API's host (`relay_api_base`) or `relay.veyllo.com`,
  at an address VAF resolved and checked as public itself, with TLS checked against the
  name; behind a site proxy the proxy resolves the name, as for every outbound fetch.
  A session that has not said who it is gets 20 seconds and small frames only.
- **The agent in the Veyllo app** uses your VAF as a tool and reads its answers with the app's
  own chat model; there is no switch for that. The pairing window says so in one sentence and
  links Veyllo's privacy policy (https://veyllo.app/legal/privacy), which names it.
- **Google and Apple** see when a push notice reaches the phone (it carries no content).
- **The app is closed source.** The end-to-end promise holds against the relay, not against a
  changed app. When you use Veyllo's models in the app, that content goes to Veyllo.

## Messages, replies and the phone being away

Your message becomes a turn of your agent in your account's one Veyllo App chat; the account
comes from the device, never from the message. The reply streams to the phone and ends with
its final text. A message VAF sends by itself (`send_to_user` with the Veyllo App as your main
messenger, the `send_veyllo_app` tool) and every finished reply are stored once in VAF's chat
history and held per device until that device confirms it: a phone that was away gets them,
in order, when it connects again, a reply that finished meanwhile as one message. Up to 200
entries and seven days are held per device; older ones stay in VAF's chat history, and the
phone gets one notice that they are there. A file sent while a phone is away reaches it as its
text line only.

**Push.** When something waits for a phone that is not connected, VAF waits 30 seconds (the
phone often reconnects), then asks the relay to notify it, and then at most once every five
minutes while something still waits. Push needs the phone to be paired with Veyllo: without
Veyllo there is no push, and on the direct way alone the app learns about new messages only
while it is open. With the relay switched off there are no push notices either.

**Tasks for the phone.** `user_phone` hands the phone's agent a task (a calendar entry, a
reminder) and waits for its answer, up to its timeout. With no phone connected it says so at
once and nothing is sent or kept for later. Its answer reaches your agent marked as data the
phone reported, never as instructions. The phone asks you itself before sensitive actions.

**Tools that need you to confirm** (the shell, Python, SSH, FTP, the sandbox environments, the
browser agent, the builders, timers) stay off in the app chat, as on Telegram: protocol v1 has
no confirmation message. They come once a confirmation type is agreed with the app.

## Cost

The relay is billed to the app user's Veyllo account, not by VAF: one cent per message in
each direction, also for the messages VAF sends by itself (reminders, automations, sub-agent
results), and half a cent per direction for a task for the phone. The direct way is free.

## Managing devices and the relay

- Your paired phones are rows of **Settings, Account, Signed-in devices**, with the way they
  last used (relay or direct), whether one is connected now, when it last connected, when it
  was paired and its fingerprint. **Remove** asks first; its access then ends at once, also
  when its certificate is still valid (a lost phone too). In the terminal: `vaf devices list`,
  `vaf devices remove <id>`.
- An admin sees every account's devices, with the account, in the admin section of the Veyllo
  App window ("Veyllo relay (for every account)") and removes one there.
- Devices also go when their account is deactivated or deleted, after 90 days without use
  (`device_idle_days`), and when the account signs out every other device. A password change
  does not remove them: the device key, not the password, is their credential.
- An admin switches the relay off and on (`vaf relay off`, `vaf relay on`, or the switch in
  the admin section of the Veyllo App window, which also shows the relay's state and how many
  devices are members and use it now): off keeps the instance, its secret and the devices, so
  on again needs no new pairing. `vaf relay rotate` (or "Replace relay secret" in the window,
  after a question) replaces the relay secret. `vaf relay dissolve` (or "Dissolve relay
  instance" in the window, with the admin's own 2FA code) deletes the instance; the devices
  stay for the direct way and need a new pairing for the relay. A failed action says whether
  no relay is set up, the relay could not be reached or it refused (`reason` in the route's
  answer).
- `vaf health` has an `app_link` row, `vaf doctor` reports a relay that carries devices and an
  instance whose secret is missing.

## Named boundaries

- **The local CA's key is the app link's identity.** The app pins this machine's local CA;
  its key (`~/.vaf/ssl/ca-key.pem`) is an owner-only file, not sealed, like the device CA's
  key beside it. Whoever copies `ca-key.pem` can pass as this VAF to every paired app, and
  with the relay in hand read that traffic. The key is in the encrypted backup; replacing it
  (`vaf server ca renew`) means every device pairs again.
- **Renewing the certificate authority** (`vaf server ca renew`) means every device pairs
  again: the app pins that authority. So does an authority VAF had to replace because its
  files were damaged.
- **A server behind 1:1 NAT** (a public address mapped onto a private one) is not detected as
  public; see [NETWORK_FEATURES.md](../setup/NETWORK_FEATURES.md#scope-private-networks-only).
- **A backup restored onto a second running machine** holds the same relay secret: the two
  take the relay connection from each other (VAF waits five minutes each time and says so in
  `vaf relay status`). Rotate the secret on one, or dissolve on one.
- **Post-quantum key exchange** is used only where both TLS stacks offer it; there is no extra
  key from the QR code in every session.
- **Pairing from the terminal** is not offered: the one-time value lives in the web server's
  process and the confirmation is a step of the signed-in account in the web UI.
- **The pairing window learns of a scan by asking**, every two seconds, not by a push over the
  WebSocket: the state lives in the web server's process, and the shortest deadline (two
  minutes for the confirmation) is sixty of those polls long.
