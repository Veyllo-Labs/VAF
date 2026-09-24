# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The messaging channels VAF knows, declared once.

Every place that has to know "which channels are there" reads the table below: which
sources are chat channels (the tool policy, the system prompt, the workflow and sandbox
gates), which tools send on a channel (the thinking-mode strip set, the automation
dedup, the Front Office allow-list, the workflow engine's scope injection), which values
the owner may pick as their main messenger, what a channel is called on screen, and
which read tool the inbox points at. Those places used to carry their own copy of the
list, about forty-five of them; a new channel meant editing each one, and a missed one
failed OPEN (a tool blocked "on telegram, whatsapp and discord" was allowed on the
fourth channel). `tests/test_channel_registry_sync.py` fails when a hand-written channel
list appears again.

Adding a channel is a row here plus the parts that genuinely differ per channel: the
bridge, its dispatch branch in `messaging_connections.send_to_main_messenger`, its tools
and its window (docs/integrations/CONNECTIONS.md, "Channel model"). A row with
`bridge=False` is known (its send tool exists and says it cannot send yet) but is not a
chat source, not a place to deliver to and not a main messenger; flipping it to True is
what turns all of that on at once.

Pure data, stdlib only: tool classes read it at class-definition time and the
dependency-free ingress policy reads it too, so it may import nothing from VAF.

NAMED BOUNDARY: first-party only, like `vaf.memory`. There is no facade export for
registering a channel of one's own: no embedder has asked for it, and the one thing a
third-party TOOL needs, to be blocked on every chat channel including future ones, is
the `"channel"` sentinel in `channel_restrictions` (docs/EMBEDDING.md), which needs no
list at all. Re-measure when an embedder brings a channel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class Channel:
    name: str
    """The source name bridges tag their tasks with, and the key everywhere else."""
    label: str
    """What the channel is called on screen and in the model's view."""
    bridge: bool
    """A bridge receives and delivers today. Only then is the channel a chat source, a
    place `send_to_user` can deliver to, and a value the owner can pick as main messenger."""
    send_tool: str
    """The per-channel send tool, for an explicit "send it via X"."""
    read_tool: Optional[str] = None
    """The tool that reads one chat of this channel, which the inbox tool points at."""
    front_office: bool = False
    """The Front Office can open this channel to new senders. Separate from `bridge` on
    purpose: a channel that gains a bridge without a contact lane must not become a Front
    Office channel by being routable (vaf/core/channel_ingress_policy.py)."""
    secrets: Tuple[str, ...] = ()
    """The fields of `<name>_config` that are credentials. They never stay in config.json
    and never travel to a browser: vaf/core/channel_secrets.py keeps them in the encrypted
    key ring. A channel whose login is not a field (WhatsApp keeps a session directory)
    declares none."""
    accounts: str = "each"
    """Whose lane the channel is, which decides what `<name>_config.enabled` means and who
    has a switch of their own (messaging_connections.channel_enabled_for_scope):
    "each" - every account links its own connection (WhatsApp: a number and a process per
    account); the global `enabled` is the local admin's own switch, everybody else has one
    under `connection_enabled_by_scope`.
    "shared" - one bot serves every account and accounts pair on it (Telegram); the global
    `enabled` is the BOT's switch, which admins control and ride, and every other account
    has its own switch on top of it.
    "owner" - one bot that serves the local admin alone (Discord); nobody else has a lane."""


CHANNELS: Tuple[Channel, ...] = (
    Channel("whatsapp", "WhatsApp", bridge=True, send_tool="send_whatsapp",
            read_tool="read_whatsapp_chat", front_office=True),
    Channel("telegram", "Telegram", bridge=True, send_tool="send_telegram",
            read_tool="read_telegram_chat", front_office=True, secrets=("bot_token",),
            accounts="shared"),
    Channel("discord", "Discord", bridge=True, send_tool="send_discord",
            read_tool="read_discord_chat", front_office=True, secrets=("bot_token",),
            accounts="owner"),
    # Known, not built: `send_slack` exists and answers that it cannot send yet.
    Channel("slack", "Slack", bridge=False, send_tool="send_slack"),
)

# Every channel VAF knows, bridge or not.
KNOWN_CHANNELS: Tuple[str, ...] = tuple(c.name for c in CHANNELS)
# The channels with a bridge: the chat sources, the places `send_to_user` delivers to, the
# main messengers an owner can pick. Blocking "every chat channel" means these.
CHAT_CHANNELS: Tuple[str, ...] = tuple(c.name for c in CHANNELS if c.bridge)
# The messengers the Front Office can open (mail is the fifth Front Office channel and is
# added by channel_ingress_policy, because it is not a messenger).
FRONT_OFFICE_MESSENGERS: Tuple[str, ...] = tuple(c.name for c in CHANNELS if c.front_office)
# The prefix of a chat channel's session id (`telegram_<chat>`, `whatsapp_<user>_<digits>`,
# `discord_<user>`): a resumed or drained session may carry nothing else that says "chat".
CHAT_SESSION_PREFIXES: Tuple[str, ...] = tuple(f"{c}_" for c in CHAT_CHANNELS)
# What the owner may pick as main messenger: a channel VAF can deliver to. A known channel
# without a bridge is not one, or a proactive message would land in the web UI instead
# while the setting said otherwise.
MAIN_MESSENGERS: Tuple[str, ...] = CHAT_CHANNELS

CHANNEL_SEND_TOOLS: Dict[str, str] = {c.name: c.send_tool for c in CHANNELS}
# Every per-channel send tool, built or not, for the sets that must hold all of them (a
# missing one is an untracked outbound channel in a background run).
ALL_SEND_TOOLS: Tuple[str, ...] = tuple(c.send_tool for c in CHANNELS)
# The send tools that can actually reach somebody today.
CHAT_SEND_TOOLS: Tuple[str, ...] = tuple(c.send_tool for c in CHANNELS if c.bridge)
CHANNEL_READ_TOOLS: Dict[str, str] = {c.name: c.read_tool for c in CHANNELS if c.read_tool}
CHANNEL_LABELS: Dict[str, str] = {c.name: c.label for c in CHANNELS}
# channel -> its credential fields, for the channels that have any.
CHANNEL_SECRETS: Dict[str, Tuple[str, ...]] = {c.name: c.secrets for c in CHANNELS if c.secrets}
# channel -> whose lane it is ("each", "shared" or "owner"; see Channel.accounts).
CHANNEL_ACCOUNTS: Dict[str, str] = {c.name: c.accounts for c in CHANNELS}


def channel_label(name: Optional[str], default: Optional[str] = None) -> str:
    """The on-screen name of a channel; an unknown name comes back as given (or `default`)."""
    key = str(name or "")
    return CHANNEL_LABELS.get(key, key if default is None else default)
