# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Veyllo App link: a paired phone talks to its account's agent over TLS 1.3 that ends
in this process, carried either by the Veyllo relay (an OUTBOUND connection VAF opens) or by
VAF's own ``wss://.../api/app-link`` on the local network or a VPN. The bytes are the same on
both ways; only the relay adds a 17-byte envelope (docs/integrations/VEYLLO_APP.md).

The pieces, bottom up:
- ``protocol``: the QR, the frames inside TLS, the relay envelope, the message list.
- ``pki``: the app-link server leaf (from the local CA, ``ssl_utils.app_link_server_files``),
  the separate device CA, the CSR check, the device fingerprint, the server context.
- ``devices``: the paired devices, one sealed file; the account of a session comes ONLY from
  the device row its certificate names.
- ``pairing``: the 2FA window, the one-time value K (``channel_pairing``), the web confirm.
- ``session``: one TLS session over memory BIOs and the dispatcher of what the app sends.
- ``bridge``: the link manager: live sessions, the turn feed into ``TaskQueue`` in process,
  streamed replies, the per-device outbox.
- ``relay``: the connector to the relay, idle unless a relay QR is live or a device uses it.
- ``phone_tasks``: tasks for the phone (the ``user_phone`` tool).

Security comes from the inner TLS: the relay and the network see ciphertext, and nothing
here opens a socket to this machine or calls ``/api/*`` (a static guard holds that).

NAMED BOUNDARY: first-party only, like the channel registry (``vaf/core/channels.py``) and
``channel_pairing``: no facade export. The relay is Veyllo's service and the app is closed,
so no embedder can pair an app of their own on it; measured number of embedders asking for
it: 0. The parts a stranger would need (pairing codes with a size, the revocation listener,
``channel_reply.reply_to_chat``, ``TaskQueue.drop_queued_tasks_for_session(where=)``) are
extensions of existing engine modules. Re-measure when an embedder asks.
"""

CHANNEL = "veyllo_app"
CONFIG_KEY = "veyllo_app_config"


_agent_name_resolver = None


def set_agent_name_resolver(resolver) -> None:
    """``resolver(username) -> str``: the name the account's agent goes by, which the QR
    code (``n``) and the app's greeting show. The application registers it (VAF's web server
    registers the persona lookup); without one the app shows "VAF". Display only."""
    global _agent_name_resolver
    _agent_name_resolver = resolver


def agent_name(username: str) -> str:
    resolver = _agent_name_resolver
    if resolver is None or not username:
        return ""
    try:
        return str(resolver(username) or "").strip()
    except Exception:
        return ""


def settings() -> dict:
    """The ``veyllo_app_config`` block with every missing field filled from its default in
    ``Config.DEFAULTS`` (the one place the defaults are written)."""
    from vaf.core.config import Config
    defaults = Config.DEFAULTS.get(CONFIG_KEY)
    defaults = dict(defaults) if isinstance(defaults, dict) else {}
    block = Config.get(CONFIG_KEY)
    block = block if isinstance(block, dict) else {}
    return {**defaults, **block}
