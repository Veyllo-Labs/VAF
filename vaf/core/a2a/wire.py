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

    A verifier that names a ``client_ip`` parameter (or takes ``**kwargs``) is also told the
    real client's address (the door's own resolver, ``binding.connection_client_ip``),
    because whether a credential may open a session can depend on where it is used: VAF's
    own sessions of the window on the machine itself stand only for that machine. It gets
    the parameter on every call, however it declares it (by position right after the
    credential when it is positional-only), and None there means the door did not know
    the client: the verifier decides what that means (VAF's own then skips its address
    rule), so a door must pass the address it has. So two shapes are refused here with a
    TypeError, because the door could not hand them the parameter (``_client_slot``): a
    verifier whose signature cannot be read (compiled code, a ``__signature__`` that raises
    or is not a Signature), which may take ``client_ip`` unseen, and one whose
    positional-only ``client_ip`` is not right after the credential. Wrap such a verifier
    in a Python function that names ``client_ip``. A verifier with the one argument is
    called with the credential alone and keeps working.

    NAMED BOUNDARY: a ``functools.wraps`` wrapper is read by its own signature when that
    names ``client_ip`` (a decorator that adds the address check), and otherwise through to
    what it wraps. So a wrapper that adds the address must name ``client_ip`` itself: one
    that takes it only through ``**kwargs`` cannot be told apart from a plain logging
    decorator and gets what its wrapped function declares.
    """
    global _verifier
    if verify is not None:
        _client_slot(verify)
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


_WRAP_HINT = ("wrap it in a Python function that names client_ip, e.g. "
              "def verify(credential, client_ip=None): return compiled(credential, client_ip)")


def _client_slot(verify) -> Optional[str]:
    """How `verify` takes the client's address: ``"position"`` (a positional-only
    ``client_ip`` right after the credential), ``"keyword"`` (any other ``client_ip``, or
    ``**kwargs``) or None (it names neither and gets the credential alone).

    The verifier's own signature decides when it names ``client_ip``: a ``functools.wraps``
    decorator that adds the address check to a one-argument verifier reports the wrapped
    signature, and read through it would be called without the address. Otherwise the
    signature it forwards to decides (wrappers, partials, callable objects), so a plain
    ``(*args, **kwargs)`` wrapper is called as its wrapped function asks; a wrapper that
    adds the address must name ``client_ip`` itself (the named boundary in
    ``set_credential_verifier``).

    Raises TypeError when the address could not be handed over: a signature that cannot be
    read may hide a ``client_ip``, and a positional-only ``client_ip`` anywhere else has no
    place for it. Calling either with the credential alone would leave a verifier that takes
    the address without it, and a missing address means "not known" and skips an address
    rule (VAF's own verifier does exactly that): it would admit a client it means to refuse."""
    import inspect
    try:
        own = list(inspect.signature(verify, follow_wrapped=False).parameters.values())
    except Exception:  # no own signature: the one it forwards to decides, or nothing does
        own = []
    if any(p.name == "client_ip" for p in own):
        params = own
    else:
        try:
            params = list(inspect.signature(verify).parameters.values())
        except Exception as e:  # inspect raises TypeError or ValueError; a __signature__ may raise anything
            raise TypeError(f"the credential verifier's signature cannot be read ({e}); "
                            f"{_WRAP_HINT}") from None
    named = next((p for p in params if p.name == "client_ip"), None)
    if named is not None and named.kind is inspect.Parameter.POSITIONAL_ONLY:
        if params.index(named) != 1:
            raise TypeError("the credential verifier's positional-only client_ip is not right "
                            f"after the credential; {_WRAP_HINT}")
        return "position"
    if named is not None or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return "keyword"
    return None


def _call_verifier(verify, credential: str, client_ip: Optional[str]):
    """Call the verifier, handing it the client the way `_client_slot` says it takes it. A
    verifier whose way cannot be told raises here too (one set past `set_credential_verifier`,
    or whose signature changed after it), and the door reads that as a refusal: the address
    is never dropped."""
    # None is handed over like an address: it means the door did not know the client, and a
    # verifier that names the parameter decides what that means. Called with the credential
    # alone instead, it could not tell "not known" from "not asked".
    slot = _client_slot(verify)
    if slot == "position":
        return verify(credential, client_ip)
    if slot == "keyword":
        return verify(credential, client_ip=client_ip)
    return verify(credential)


def resolve_account(credential: str, *, client_ip: Optional[str] = None) -> dict:
    """A verified account from a credential, or a refusal.

    The verification itself belongs to whoever owns the accounts and arrives through
    the registered verifier. What stays here is the framework's own rule, which is
    about rooms rather than about tokens: an identity WITHOUT A SCOPE is refused. Two
    gates in this tree read "no scope" as unrestricted, so an unscoped connection is
    the most dangerous shape there is - worse than no connection at all - and a door
    that let one through would be handing out the thing those gates protect.

    ``client_ip`` is the real client's address as the door resolved it. None means the
    client is not known; a verifier that names the parameter receives that None and decides
    (VAF's own then skips its address rule), so a door must pass the address it has.
    """
    if _verifier is None:
        raise HandshakeRefused("no credential verifier is registered")

    try:
        claims = _call_verifier(_verifier, str(credential or ""), client_ip)
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


def admit(room: Room, credential: str, *, display: str = "",
          client_ip: Optional[str] = None) -> tuple:
    """Turn a credential into a member of THIS room, or refuse.

    Returns ``(identity, seat)``. The seat is a durable way back in, minted exactly
    when a TICKET is redeemed and None on every other path: a ticket is single use
    (rightly - a bearer credential pasted into a chat window must die on first use),
    but a CLI is one process per command, so the invited agent's second connection
    needs something to present. An account credential needs no seat - it can always
    reconnect as itself - and a seat presented back is its own proof.

    The room is passed in rather than looked up from the credential, because a ticket
    is bound to one room and an account token is bound to none: the caller has already
    decided which room this connection asked for, and every credential is checked
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
            identity = room.redeem_ticket(credential, display=display)
            return identity, room.issue_seat(identity)
        except TicketInvalid as e:
            raise HandshakeRefused(str(e), code=4003) from None
        except RoomError as e:
            raise HandshakeRefused(str(e), code=4003) from None

    if str(credential).startswith(Room.SEAT_PREFIX):
        try:
            return room.redeem_seat(credential), None
        except TicketInvalid as e:
            raise HandshakeRefused(str(e), code=4003) from None

    account = resolve_account(credential, client_ip=client_ip)
    key = participant_key("remote", account["user_scope_id"])
    peer_id = derive_peer_id(key, room.room_id)

    existing = room.role_of(peer_id)
    if existing:
        record = room.store.member(peer_id) or {}
        return Identity(peer_id, record.get("display") or display or account["username"],
                        account["user_scope_id"], existing), None
    try:
        return room.join(display=display or account["username"] or "remote",
                         peer_id=peer_id, scope_id=account["user_scope_id"],
                         participant_key=key), None
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
