# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A refusal at the room socket has to say WHICH refusal it was.

The room socket is the one door a stranger knocks on from another machine, and the
protocol gives it a vocabulary for turning somebody away: 4001 the credential was
refused, 4003 the ticket or seat does not open this room, 4004 no such room here,
4009 another connection is already writing as this peer. The VAF-free guest client
carries that table and prints the sentence.

None of it could ever fire. `close(code=..., reason=...)` on a socket that was never
ACCEPTED does not carry an application code: the peer sees a plain 1000 with an empty
reason. Measured against the live endpoint, which is how it was found - a wrong
ticket and an empty token both came back as `Close-Code 1000, Grund ''`.

So every refusal looked identical from outside, the guest client's whole table was
unreachable code, and a foreign agent that hit one had to guess which of four things
had happened. It guessed wrong, in the first live use, on the lane the room protocol
exists for.

This is a STATIC guard rather than a live one on purpose. The defect is invisible to
every test that speaks to the endpoint through an in-process client, because the
ASGI test transport hands back the code the handler passed rather than what a socket
would carry; only a real TCP peer sees the difference. What is checkable everywhere
is the ORDER, and the order is the whole fix.
"""
import inspect
import re

import vaf.core.web_server as web_server


def _endpoint_source() -> str:
    """The room socket handler, by name rather than by line number."""
    # Taken from the function object rather than by slicing the file, so moving the
    # endpoint or adding another one below it cannot quietly widen what is checked.
    return inspect.getsource(web_server.a2a_room_endpoint)


def test_the_room_socket_is_accepted_before_any_refusal():
    """MUTATION: move the accept below one of the refusals.

    Everything after it still reads correctly and every in-process test still
    passes; only a peer on a real socket loses the reason it was turned away.
    """
    body = _endpoint_source()
    accept = body.index("await websocket.accept()")
    closes = [m.start() for m in re.finditer(r"await websocket\.close\(", body)]

    assert closes, "the room socket refuses nothing, which cannot be right"
    for position in closes:
        assert accept < position, (
            "a refusal is sent before the socket is accepted, so its close code and "
            "reason are replaced by a bare 1000 and the peer cannot tell which "
            "refusal it was")


def test_the_socket_is_accepted_exactly_once():
    """Accepting twice raises inside Starlette and would take down the happy path,
    which is the obvious way to get this wrong while fixing it."""
    assert _endpoint_source().count("await websocket.accept()") == 1


def test_every_close_code_the_guest_client_explains_is_one_the_server_sends():
    """The guest client's table and the server's codes are two copies of one
    vocabulary. A code the server sends and the client cannot name is a shrug; a
    code the client names and the server never sends is dead prose."""
    from pathlib import Path

    guest = (Path(__file__).resolve().parents[1] / "examples" / "12_a2a_wire_peer.py"
             ).read_text(encoding="utf-8")
    explained = {int(c) for c in re.findall(r"^\s+(4\d{3}):", guest, re.MULTILINE)}
    sent = {int(c) for c in re.findall(r"close\(code=(4\d{3})", _endpoint_source())}

    assert sent, "the endpoint sends no application close codes at all"
    assert sent <= explained, (
        f"the server sends {sorted(sent - explained)}, which the guest client cannot "
        f"turn into a sentence")
