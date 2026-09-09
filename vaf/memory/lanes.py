# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The lanes of the memory store: which rows an ordinary lookup may see.

One `memories` table holds rows that must never mix on retrieval. The long-term lane is
what the user's agent knows. The attachment lane (`source = attachment_ephemeral`) is a
session's uploaded documents, with a lifetime. The chat lanes (`source = chat/<session id>`)
are what the agent learned inside ONE messenger chat with a contact: facts about that person
and what was agreed with them, which belong to that chat and to nothing else. An ordinary
lookup leaves both special lanes out, in SQL, in every lane of the hybrid search, and a
caller that wants one of them names it explicitly.

Why a leaf module: `rag.py` imports `graph.py`, and both apply the same predicates, so the
predicates cannot live in either. And why predicates rather than a `metadata_filter` entry:
that filter reaches the search unfiltered from three public routes, and it runs in Python
over rows the database already ranked, so it can neither isolate nor rank within a lane.

The chat predicate is a prefix match, never an equality, and it keeps rows whose `source`
is NULL: the store predates the `source` field, and excluding what it cannot classify would
silently drop old facts (the same rule `_not_document_memory` follows for `type`). No index
on `meta->>'source'` exists; the predicate joins two JSONB reads that are already on the hot
path, and a partial index waits for a measurement that asks for one.
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional

from sqlalchemy import or_

from vaf.memory.models import Memory

ATTACHMENT_EPHEMERAL_SOURCE = "attachment_ephemeral"
CHAT_SOURCE_PREFIX = "chat/"

_NAMESPACE_KEYS = ("chat_key", "chat_channel", "chat_label")
_CHANNEL_NAMES = {"whatsapp": "WhatsApp", "telegram": "Telegram", "discord": "Discord"}


def chat_source(chat_key: str) -> str:
    """`meta.source` of every memory learned in one messenger chat."""
    return f"{CHAT_SOURCE_PREFIX}{chat_key}"


def is_chat_source(source: Any) -> bool:
    return isinstance(source, str) and source.startswith(CHAT_SOURCE_PREFIX)


def not_attachment_lane():
    """SQL: not the ephemeral attachment lane. Rows without a source pass (legacy store)."""
    return or_(
        Memory.meta["source"].astext.is_(None),
        Memory.meta["source"].astext != ATTACHMENT_EPHEMERAL_SOURCE,
    )


def not_chat_lane():
    """SQL: not learned inside a messenger chat. Prefix match; rows without a source pass."""
    return or_(
        Memory.meta["source"].astext.is_(None),
        Memory.meta["source"].astext.not_like(CHAT_SOURCE_PREFIX + "%"),
    )


def in_chat_lane(chat_key: str):
    """SQL: exactly one chat's namespace, by equality on the full source."""
    return Memory.meta["source"].astext == chat_source(chat_key)


def pin_namespace(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """A memory's lane is set once, at ingest, and an update cannot move it.

    `before` is the stored meta, `after` the merged meta an update wants to write. A row
    inside a chat namespace keeps its `source` and its three namespace keys exactly as they
    were; a row outside one cannot acquire them. Without this a PUT carrying `source` could
    lift a contact's facts into the general lane, or hide an ordinary memory inside a chat,
    and the SQL predicates would honour the forgery."""
    pinned = dict(after)
    if is_chat_source(before.get("source")):
        for key in ("source",) + _NAMESPACE_KEYS:
            if key in before:
                pinned[key] = before[key]
            else:
                pinned.pop(key, None)
        return pinned
    for key in _NAMESPACE_KEYS:
        pinned.pop(key, None)
    if is_chat_source(pinned.get("source")):
        if "source" in before:
            pinned["source"] = before["source"]
        else:
            pinned.pop("source", None)
    return pinned


@dataclass(frozen=True)
class ChatNamespace:
    """One messenger chat's own memory lane.

    `key` IS the session id the bridge built for that chat (`whatsapp_<user>_<digits>`,
    `telegram_<id>`); nothing else names a namespace, so the agent answering in the chat,
    the compaction that learns from it and the Composer drafting for it all derive the same
    lane from the same id. `label` is the person's name as the bridge knew it when the fact
    was learned; the graph shows `display_label`."""

    key: str
    channel: str
    label: str

    @property
    def source(self) -> str:
        return chat_source(self.key)

    @property
    def display_label(self) -> str:
        channel = (self.channel or "").strip().lower()
        name = _CHANNEL_NAMES.get(channel) or (channel.title() if channel else "Chat")
        return f"{name}: {self.label}"

    def as_meta(self) -> Dict[str, str]:
        """The three keys that travel in a queue task and land in `Memory.meta`."""
        return {"chat_key": self.key, "chat_channel": self.channel, "chat_label": self.label}

    @classmethod
    def from_meta(cls, meta: Optional[Dict[str, Any]]) -> Optional["ChatNamespace"]:
        key = str((meta or {}).get("chat_key") or "").strip()
        if not key:
            return None
        channel = str((meta or {}).get("chat_channel") or key.split("_", 1)[0]).strip().lower()
        label = str((meta or {}).get("chat_label") or "").strip() or _endpoint_of(key)
        return cls(key=key, channel=channel, label=label)

    @classmethod
    def from_task(cls, session_id: str, metadata: Optional[Dict[str, Any]]) -> Optional["ChatNamespace"]:
        """The rule "the agent may answer in this chat", read off a queued task.

        A task carrying `from_contact` is a chat the agent answers for someone other than
        the account owner (a Front Office contact, an open conversation): it learns into
        its own namespace. The owner's own chats carry no `from_contact` and stay in the
        general lane. A Telegram relay contact gets no agent answer and no namespace."""
        meta = metadata or {}
        if not meta.get("from_contact") or meta.get("relay"):
            return None
        key = str(session_id or "").strip()
        if not key:
            return None
        channel = str(meta.get("origin_channel") or key.split("_", 1)[0]).strip().lower()
        label = str(meta.get("chat_label") or "").strip() or _endpoint_of(key)
        return cls(key=key, channel=channel, label=label)


def _endpoint_of(session_id: str) -> str:
    """The other side of a channel session id, for a namespace that carries no name."""
    tail = session_id.rsplit("_", 1)[-1]
    return f"+{tail}" if session_id.startswith("whatsapp_") and tail.isdigit() else tail
