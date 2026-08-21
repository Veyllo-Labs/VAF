# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The room door on the wire: who gets in, and what the route must never inherit.

Every omission in the route is a decision, and each one has a test here, because the
tempting thing to do was to reuse the WebUI socket's plumbing wholesale - which would
have filed a room peer in the browser identity table, fanned WebUI state out to it, and
handed the machine owner's seat to anything presenting a token that decodes to nothing.
"""
import ast
import re
from pathlib import Path

import pytest

import vaf.core.a2a.store as store_mod
from vaf.core.a2a.room import Room, derive_peer_id, participant_key
from vaf.core.a2a.wire import HandshakeRefused, admit, open_room, resolve_account

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    room = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-wire")
    room.join(display="Owner", scope_id=None, peer_id="p-owner")
    return room


# A fixed signing secret for every test in this module.
#
# NOT a convenience. The real one is resolved from the machine keyring, which RAISES in
# a scratch home, so decode_token returns None and every credential is refused as
# "not valid" - and a test asserting a refusal then passes no matter what the code
# does. That was live here: the token tests only worked because an earlier test in the
# same file happened to create a room, which minted the keyring as a side effect, so the
# whole file was order-dependent and would have gone quietly useless when run alone.
_TEST_SECRET = "test-secret-for-a2a-handshakes"


@pytest.fixture(autouse=True)
def _pinned_jwt_secret(monkeypatch):
    import vaf.auth.crypto as crypto
    import vaf.core.a2a.wire as wire_mod
    monkeypatch.setattr(crypto, "_get_jwt_secret", lambda: _TEST_SECRET)

    # The verifier the harness registers, reproduced here rather than imported, so this
    # file tests the framework's own rules and not vaf/main's import order. The wiring
    # itself has its own test below.
    def _verify(credential):
        payload = crypto.decode_token(credential)
        if not payload or payload.get("type") != "access":
            return None
        return payload

    monkeypatch.setattr(wire_mod, "_verifier", _verify)
    return _TEST_SECRET


def _token(**claims):
    import jwt
    payload = {"type": "access", "sub": "u-1", "user_scope_id": "scope-a",
               "username": "alice", "role": "user"}
    payload.update(claims)
    return jwt.encode(payload, _TEST_SECRET, algorithm="HS256")


def test_the_tokens_in_this_file_really_decode():
    """The guard against the failure above: if this goes red, every refusal test in
    this module is passing because nothing decodes, not because anything was checked."""
    from vaf.auth.crypto import decode_token

    payload = decode_token(_token())
    assert payload is not None and payload.get("user_scope_id") == "scope-a"


# ── the credential ─────────────────────────────────────────────────────────

def test_no_credential_means_no_connection(rooms):
    """MUTATION: fall back to the local admin when nothing was presented.

    The WebUI socket has that fallback so the desktop is not locked out of its own
    admin-owned chats. A room has no such problem, and copying it would hand the machine
    owner's seat to anything that reaches the port.
    """
    with pytest.raises(HandshakeRefused) as refusal:
        admit(rooms, "")
    assert "no credential" in str(refusal.value), (
        "refused, but for some other reason - the empty case is not actually covered")


def test_a_refresh_token_cannot_open_a_socket():
    """MUTATION: drop the type check.

    A refresh token renews a session; it does not open one. The HTTP middleware has
    always demanded type == "access" and the socket lane never did, so the same
    credential that is refused over HTTP used to open a WebSocket.
    """
    with pytest.raises(HandshakeRefused):
        resolve_account(_token(type="refresh"))


def test_a_token_without_an_account_is_refused(rooms):
    """MUTATION: admit it as a guest with no scope.

    Two gates in this tree read "no scope" as unrestricted, so an unscoped connection is
    the most dangerous shape there is - worse than no connection at all.

    The claims are OMITTED rather than set to null, and that detail is the test. A
    payload carrying sub=None fails PyJWT's own validation, so decode returns nothing
    and the refusal comes from the first check instead of the one under test - it looked
    green while proving nothing, which is the second time this file caught itself doing
    that.
    """
    import jwt

    naked = jwt.encode({"type": "access", "username": "nobody"},
                       _TEST_SECRET, algorithm="HS256")
    from vaf.auth.crypto import decode_token
    assert decode_token(naked) is not None, "the token must decode, or this proves nothing"

    with pytest.raises(HandshakeRefused) as refusal:
        resolve_account(naked)
    assert "no account" in str(refusal.value)


def test_rubbish_is_refused_rather_than_crashing(rooms):
    with pytest.raises(HandshakeRefused):
        admit(rooms, "not-a-token-at-all")


def test_an_account_token_joins_in_the_remote_lane(rooms):
    """MUTATION: derive the handle from the agent or cli lane.

    A peer on the wire must never land on the local agent's seat: its words would then
    appear where the owner's own agent speaks from, and every reader would attribute
    them to it.
    """
    identity, _seat = admit(rooms, _token())

    assert identity.peer_id == derive_peer_id(
        participant_key("remote", "scope-a"), "room-wire")
    for lane in ("agent", "cli"):
        assert identity.peer_id != derive_peer_id(
            participant_key(lane, "scope-a"), "room-wire")


def test_reconnecting_lands_on_the_same_seat(rooms):
    first, _ = admit(rooms, _token())
    second, _ = admit(rooms, _token())
    assert first.peer_id == second.peer_id
    assert len([p for p in rooms.roles()]) == 2, "a reconnect created a second member"


def test_a_ticket_opens_exactly_the_room_it_was_minted_for(rooms, tmp_path):
    """MUTATION: accept a ticket for any room.

    Room-binding is the whole reason an invitation is safe to paste into a chat window.
    """
    from vaf.core.a2a.room import Identity

    owner = Identity("p-owner", "Owner", None, "peer")
    ticket = rooms.mint_ticket(owner, display="Codex")

    other = Room.create(kind="round", owner_scope=None, base=tmp_path, room_id="room-other")
    other.join(display="Owner", scope_id=None, peer_id="p-owner2")
    other.store.put_ticket(ticket, rooms.store.ticket(ticket))

    with pytest.raises(HandshakeRefused):
        admit(other, ticket)

    guest, _ = admit(rooms, ticket)
    assert rooms.role_of(guest.peer_id) == "peer"


def test_an_unknown_room_and_a_traversing_one_refuse_differently(rooms):
    with pytest.raises(HandshakeRefused) as unknown:
        open_room("room-ghost")
    with pytest.raises(HandshakeRefused) as unsafe:
        open_room("../../etc")
    assert unknown.value.code != unsafe.value.code, (
        "a caller cannot tell a wrong id from a missing room")


# ── what the route must never inherit ──────────────────────────────────────

def _route_source() -> str:
    source = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    return source.split("async def a2a_room_endpoint")[1].split("\nasync def ")[0]


def test_the_route_never_files_a_peer_in_the_browser_identity_table():
    """MUTATION: call manager.set_connection_user for a room peer.

    That table is what every ownership check reads. A room peer filed there is
    indistinguishable from a browser to _ws_session_owner_ok and everything like it.
    """
    body = _route_source()
    assert "set_connection_user" not in body
    assert "manager.connect" not in body, (
        "manager.connect would also append the peer to active_connections, so every "
        "WebUI broadcast would fan out to an agent, and the tray would count it as a "
        "browser")
    assert "websocket.accept()" in body


def test_the_route_has_no_local_admin_fallback():
    """MUTATION: copy the WebUI fallback into the room route."""
    body = _route_source()
    for forbidden in ("get_local_admin_scope_id", "local_admin", '"role": "admin"'):
        assert forbidden not in body, f"the route reaches for {forbidden!r}"


def test_the_route_resolves_the_client_through_the_shared_resolver():
    """A forwarding hop must remove trust, never grant it - the same resolver every
    other lane uses, rather than reading client.host directly."""
    body = _route_source()
    assert "_ws_client_ip(websocket)" in body
    assert "websocket.client.host" not in body


def test_the_route_reads_the_credential_from_the_query_string():
    """The proxy strips Authorization headers and subprotocols on the relayed leg,
    silently. The query string is the only carrier that survives it."""
    body = _route_source()
    assert "token: Optional[str] = Query(None)" in _full_route_signature()
    assert "VAF_TOKEN_COOKIE" in body, "the same-origin desktop cookie is not read"


def _full_route_signature() -> str:
    source = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    start = source.index('@app.websocket("/ws/a2a/{room_id}")')
    return source[start:start + 400]


# ── the proxy, which is where this silently fails ──────────────────────────

def test_the_proxy_registers_the_room_route():
    """MUTATION: register the route on the server only.

    Starlette's catch-all matches HTTP scopes only, so without an explicit
    WebSocketRoute a room socket is answered with HTTP 403 at the handshake and never
    reaches the backend - while working perfectly on the desktop, which bypasses the
    proxy entirely. Green locally, dead on the LAN.
    """
    source = (ROOT / "vaf" / "network" / "https_proxy.py").read_text(encoding="utf-8")
    assert 'WebSocketRoute("/ws/a2a/{room_id}"' in source
    assert 'WebSocketRoute("/ws", endpoint=_ws_handler)' in source, "the WebUI route moved"


def test_the_proxy_carries_the_path_to_the_backend():
    """MUTATION: hardcode the backend path again.

    With a hardcoded /ws the new route relays every room to the WebUI handler: the
    socket opens, frames flow, the wrong handler answers. Everything looks healthy,
    which is what makes it the worst shape of all.

    Pinned as the INVARIANT rather than as one literal line: the backend ORIGIN
    later became a single constant shared with the HTTP half (it used to be a
    second hardcoded copy), and a guard that matched the old literal would have
    failed on that improvement while a re-hardcoded PATH - the thing that
    actually breaks rooms - slipped through unnoticed.
    """
    import re as _re
    source = (ROOT / "vaf" / "network" / "https_proxy.py").read_text(encoding="utf-8")
    assert _re.search(r"backend_uri\s*=\s*BACKEND_ORIGIN[^\n]*\+\s*path", source), (
        "the backend websocket URI must be built from BACKEND_ORIGIN with the CLIENT's "
        "path appended; a literal path here relays every socket to the WebUI handler"
    )
    assert 'backend_uri = "ws://127.0.0.1:8005/ws"' not in source
    assert '+ "/ws"' not in source


@pytest.mark.parametrize("path,allowed", [
    ("/ws", True),
    ("/ws/a2a/room-0864b74b2c14", True),
    ("/ws/a2a/room_a.b-c", True),
    ("/ws/", False),
    ("/ws/a2a", False),
    ("/ws/a2a/", False),
    ("/ws/a2a/../../etc", False),
    ("/ws/a2a/a/b", False),
    ("/ws/anything-else", False),
    ("/ws/a2a/" + "x" * 70, False),
])
def test_the_proxy_allowlist_is_a_list_and_not_a_wildcard(path, allowed):
    """MUTATION: relay /ws/{rest:path} generically.

    A wildcard makes every future websocket endpoint reachable from the LAN the moment
    somebody adds one, and nobody would ever make that decision on purpose. The room id
    shape is the store's own, so a path that would be refused as a directory name never
    reaches the backend either.
    """
    from vaf.network.https_proxy import _WS_ALLOWED

    assert bool(_WS_ALLOWED.match(path)) is allowed, path


def test_the_allowlist_agrees_with_the_stores_own_name_rule():
    """Two places decide what a room id may look like. They must not drift: a name the
    store would refuse as a directory must not be relayable, and one it accepts must
    not be unreachable."""
    from vaf.core.a2a.store import _SAFE_COMPONENT
    from vaf.network.https_proxy import _WS_ALLOWED

    for name in ("room-abc", "a", "A1_b.c-d", "x" * 64):
        assert bool(_SAFE_COMPONENT.match(name)) is True, name
        assert bool(_WS_ALLOWED.match(f"/ws/a2a/{name}")) is True, name
    for name in ("", ".", "..", "-lead", "a/b", "x" * 65):
        assert bool(_SAFE_COMPONENT.match(name)) is False, name
        assert bool(_WS_ALLOWED.match(f"/ws/a2a/{name}")) is False, name


# ── the older copies, recorded rather than changed ─────────────────────────

def test_the_webui_handshake_is_documented_as_it_stands():
    """A WATCHMAN, not a fix. Two properties of the WebUI socket were measured and left
    alone by decision, because changing them changes how the running web interface
    authenticates and that is its own round:

      - it decodes a token without demanding type == "access", so a refresh token opens
        a socket that the same credential could not open over HTTP;
      - it treats the whole of 172.16.0.0/12 as "Docker", which is a real RFC1918 range
        that home and office networks use, and a client classified that way can reach
        the local-admin fallback.

    This test pins BOTH as they are today. If somebody later tightens them, it goes red
    and this docstring is the note explaining what changed and why it was left. If
    somebody widens them further, it also goes red.
    """
    source = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    handshake = source.split("async def websocket_endpoint")[1].split("\n@app.")[0]

    assert 'payload = jwt.decode(token, secret, algorithms=["HS256"])' in handshake
    assert handshake.count('payload.get("type")') == 0, (
        "the WebUI handshake now checks the token type - update this watchman and say so")
    assert 'ipaddress.ip_network("172.16.0.0/12")' in handshake, (
        "the docker window changed - update this watchman and say so")

    a2a = _route_source()
    assert "172.16" not in a2a and "jwt.decode" not in a2a, (
        "the room route grew its own copy of the WebUI handshake")


# ── the dependency points the right way ────────────────────────────────────

def test_the_framework_does_not_import_the_auth_layer():
    """MUTATION: import vaf.auth.crypto in wire.py again.

    A framework module reaching into the harness's auth layer points the dependency the
    wrong way round, and this tree has paid for it once already: the tool dispatcher
    imported the harness's permissions directly and had to be unpicked into a registered
    resolver afterwards. There is a guard for this class (test_framework_auth_layering),
    and this test says the same thing at the point of use.
    """
    source = (ROOT / "vaf" / "core" / "a2a" / "wire.py").read_text(encoding="utf-8")
    assert "vaf.auth" not in source
    assert "decode_token" not in source
    assert "set_credential_verifier" in source


def test_an_unregistered_verifier_refuses_rather_than_admits(monkeypatch):
    """MUTATION: treat "no verifier" as "no check needed".

    A door with no way to check a credential does not open. Degrading to "let them in"
    is the exact opposite of what a missing check means, and it is the shape a
    half-wired embedder would hit first.
    """
    import vaf.core.a2a.wire as wire_mod
    monkeypatch.setattr(wire_mod, "_verifier", None)

    with pytest.raises(HandshakeRefused) as refusal:
        resolve_account("anything at all")
    assert "verifier" in str(refusal.value)


def test_a_verifier_that_crashes_is_not_a_verifier_that_approved(monkeypatch):
    import vaf.core.a2a.wire as wire_mod

    def _boom(_credential):
        raise RuntimeError("the account database is down")

    monkeypatch.setattr(wire_mod, "_verifier", _boom)
    with pytest.raises(HandshakeRefused):
        resolve_account(_token())


def test_the_harness_registers_a_verifier_that_demands_an_access_token():
    """MUTATION: register a verifier without the type check, or forget to register.

    The wiring is asserted at the source, next to the other resolver registrations,
    because a resolver that is never registered fails closed and would look like a
    working door that simply refuses everyone.
    """
    source = (ROOT / "vaf" / "main.py").read_text(encoding="utf-8")
    assert "_set_credential_verifier(_a2a_credential_verifier)" in source
    verifier = source.split("def _a2a_credential_verifier")[1].split("\n\n")[0]
    assert 'payload.get("type") != "access"' in verifier
    assert "return None" in verifier


def test_an_invitation_keeps_the_name_it_was_minted_with(rooms):
    """MUTATION: pass a default display into redeem_ticket.

    A default there is not a fallback, it is an override: the ticket already carries the
    name the inviter chose, and "vaf a2a invite --display Codex" produced a member
    called "guest". Seen in a live room log, where two different guests were both
    "guest" and nobody could tell them apart.
    """
    from vaf.core.a2a.room import Identity

    owner = Identity("p-owner", "Owner", None, "peer")
    ticket = rooms.mint_ticket(owner, display="Codex")

    guest, _ = admit(rooms, ticket)

    assert guest.display == "Codex"
    assert rooms.members()[guest.peer_id]["display"] == "Codex"


def test_a_refused_room_handshake_reaches_the_security_log():
    """MUTATION: log it only to the API log.

    A room is the one door a stranger can knock on from another machine, so a refusal
    here belongs on the security dashboard more than one on the browser socket does,
    not less. The WebUI socket has mirrored its refusals there all along; this one did
    not, which is how a rejected room join stayed invisible to the person watching.
    """
    body = _route_source()
    assert "_emit_sec_ws(" in body, "a refused room handshake is not a security event"
    assert body.count("_emit_sec_ws(") >= 2, "only one of the two refusal paths reports"


def test_the_route_answers_a_renew_before_the_store():
    """MUTATION: let a `renew` fall through into hub.submit.

    `renew` is transport, never a frame: fallen through, the frame screen would
    refuse it as malformed - which is exactly what a host too old to know the
    verb does, and what left a held session with no way to keep its lease. The
    intercept must sit BEFORE the submit so the verb never touches the store,
    and a lapsed lease answers not_writer instead of being silently re-taken.
    """
    source = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    start = source.index('@app.websocket("/ws/a2a/{room_id}")')
    route = source[start:start + 12000]
    assert '== "renew"' in route and "hub.renew" in route
    assert '"status": "renewed"' in route
    assert route.index('== "renew"') < route.index("hub.submit"), \
        "the renew intercept must come before the store"


def test_the_route_cannot_leak_a_lease_between_attach_and_the_loop():
    """MUTATION: move the accept or the welcome send back above the protective try.

    A client that vanished between attach and the welcome (a timed-out dialer
    hanging up) skipped the detach, and its dead lease refused its own
    reconnects for the full 90 second TTL - each half-successful retry
    re-arming another dead lease. Measured live as a room gone permanently
    mute behind "another connection is writing", while the endpoint printed an
    unhandled traceback for every one of them.
    """
    source = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    start = source.index('@app.websocket("/ws/a2a/{room_id}")')
    route = source[start:start + 14000]
    attach_at = route.index("hub.attach")
    accept_at = route.index("websocket.accept()")
    detach_at = route.index("hub.detach")
    guard_try = route.rindex("try:", attach_at, accept_at)
    assert attach_at < guard_try < accept_at < detach_at, \
        "accept and welcome must sit inside the try whose finally detaches"
    # And the handshake's store work stays off the shared event loop: a remote
    # client's connect storm froze the WebUI socket beside it.
    assert "to_thread(open_room" in route
    assert "to_thread(admit" in route
    assert "to_thread(hub.attach" in route
    assert "to_thread(room.welcome" in route
