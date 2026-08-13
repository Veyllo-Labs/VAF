# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Who is on the other end of a room connection.

One home for the handshake, because the alternative was measured: the WebUI socket
decodes its token INLINE, twice, once per auth branch, and a third copy for the room
route would have made three hand-rolled handshakes and no primitive. The two older
copies are deliberately left where they are - converting them changes how the running
web interface authenticates, which is its own decision - so this is the first consumer
rather than the only one, and the boundary is named rather than pretended away.

It is also STRICTER than those copies, on purpose, and the difference is the point:

- A refresh token is refused. ``type == "access"`` is required, the same demand the
  HTTP middleware makes. Without it a token minted to renew a session opens a socket
  that the very same credential could not open over HTTP.
- No identity means no connection. There is no local-admin fallback here. The WebUI has
  one so the desktop is not locked out of its own admin-owned chats; a room has no such
  problem, and copying it would hand the machine owner's seat to anything that reaches
  the port with a token that decodes to nothing.
- Every connection lands in the REMOTE participant lane, even one holding a perfectly
  good account token. A peer on the wire must never derive the local agent's or the
  local terminal's room handle: landing on the agent's seat would put a stranger's
  words where the owner's own agent speaks from, and every reader would attribute them
  to it.

Two credentials are accepted and they mean different things. A JWT says "I am an
account on this machine" and can open any room that account may see. A join ticket says
"somebody invited me to exactly this room" and can open nothing else - which is what
makes an invitation safe to paste into a chat window.
"""
from __future__ import annotations

from typing import Any, Optional

from vaf.core.a2a.room import (
    Identity,
    Room,
    RoomError,
    TicketInvalid,
    derive_peer_id,
    participant_key,
)

# How this framework learns whether a credential is real. REGISTERED by the harness
# rather than imported from it: framework code reaching into the auth layer points the
# dependency the wrong way round, and this tree already paid for that once - the tool
# dispatcher imported the harness's permissions directly and had to be unpicked into a
# resolver afterwards. The same shape as set_account_allowlist_resolver, for the same
# reason, and it is what lets an embedder plug in their own accounts.
#
# UNREGISTERED MEANS REFUSE. A door with no way to check a credential does not open;
# degrading to "let them in" would be the exact opposite of what a missing check means.
_verifier = None


def set_credential_verifier(verify) -> None:
    """Register the callable that turns a raw credential into verified claims.

    The contract: given the string a peer presented, return a mapping with at least
    ``user_scope_id`` for a VERIFIED credential that is allowed to open a session, or
    None for anything else. Deciding what "allowed to open a session" means - the token
    type, an expiry, a revoked session - belongs to whoever owns the accounts.
    """
    global _verifier
    _verifier = verify


def credential_verifier():
    """The registered verifier, or None. Exposed so a wiring test can see it."""
    return _verifier

# What a join ticket looks like. Minted in Room.mint_ticket; the prefix is what lets one
# query parameter carry either credential without a second field to keep in step.
TICKET_PREFIX = "t-"


class HandshakeRefused(Exception):
    """The connection may not proceed. Carries the close code the caller should send."""

    def __init__(self, reason: str, *, code: int = 4001) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


def looks_like_ticket(credential: str) -> bool:
    return str(credential or "").startswith(TICKET_PREFIX)


def resolve_account(credential: str) -> dict:
    """A verified account from a credential, or a refusal.

    The verification itself belongs to whoever owns the accounts and arrives through
    the registered verifier. What stays here is the framework's own rule, which is
    about rooms rather than about tokens: an identity WITHOUT A SCOPE is refused. Two
    gates in this tree read "no scope" as unrestricted, so an unscoped connection is
    the most dangerous shape there is - worse than no connection at all - and a door
    that let one through would be handing out the thing those gates protect.
    """
    if _verifier is None:
        raise HandshakeRefused("no credential verifier is registered")

    try:
        claims = _verifier(str(credential or ""))
    except Exception:
        # A verifier that crashed is not a verifier that approved.
        raise HandshakeRefused("the credential could not be checked") from None
    if not claims:
        raise HandshakeRefused("the credential is not valid")

    scope = claims.get("user_scope_id") or claims.get("sub")
    if not scope:
        raise HandshakeRefused("the credential carries no account")
    return {
        "user_scope_id": str(scope),
        "username": claims.get("username") or "",
        "role": (claims.get("role") or "user"),
    }


def admit(room: Room, credential: str, *, display: str = "") -> Identity:
    """Turn a credential into a member of THIS room, or refuse.

    The room is passed in rather than looked up from the credential, because a ticket
    is bound to one room and an account token is bound to none: the caller has already
    decided which room this connection asked for, and both credentials are checked
    against that decision rather than allowed to choose it.
    """
    credential = str(credential or "").strip()
    if not credential:
        raise HandshakeRefused("no credential was presented")

    if looks_like_ticket(credential):
        try:
            # No default here: passing one would OVERRIDE the name the invitation was
            # minted with, so "vaf a2a invite --display Codex" produced a member called
            # "guest". The room falls back on its own if neither carries a name.
            return room.redeem_ticket(credential, display=display)
        except TicketInvalid as e:
            raise HandshakeRefused(str(e), code=4003) from None
        except RoomError as e:
            raise HandshakeRefused(str(e), code=4003) from None

    account = resolve_account(credential)
    key = participant_key("remote", account["user_scope_id"])
    peer_id = derive_peer_id(key, room.room_id)

    existing = room.role_of(peer_id)
    if existing:
        record = room.store.member(peer_id) or {}
        return Identity(peer_id, record.get("display") or display or account["username"],
                        account["user_scope_id"], existing)
    try:
        return room.join(display=display or account["username"] or "remote",
                         peer_id=peer_id, scope_id=account["user_scope_id"])
    except RoomError as e:
        raise HandshakeRefused(str(e), code=4003) from None


def open_room(room_id: str, *, base: Optional[Any] = None) -> Room:
    """The room a connection asked for, or a refusal a caller can close on."""
    from vaf.core.a2a.store import StoreError, UnsafeName

    try:
        return Room.open(str(room_id), base=base)
    except UnsafeName:
        raise HandshakeRefused("that is not a valid room id", code=4003) from None
    except StoreError:
        raise HandshakeRefused("no such room on this machine", code=4004) from None
