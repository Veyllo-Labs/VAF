# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The outbox routes (vaf/api/outbox_routes.py): the three verbs the card needs.

A send the agent makes on the person's own web chat turn is parked for them
(vaf/core/outbound_hold.py). What is pinned here is the part a route gets wrong first: the
identity. The draft belongs to whoever parked it, so the store call takes its identity from
the auth dependency and never from the path or the body, and an id is only ever looked up in
the caller's own store.

MUTATION: read the username from the request body and the identity test goes red; drop the
kind check and the unknown-kind test goes red; answer 200 for a draft that is not waiting and
the discard test goes red.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import vaf.api.outbox_routes as orr
from vaf.core import channel_message_store as store
from vaf.core.platform import Platform

SCOPE = "11111111-2222-3333-4444-555555555555"
OTHER = "66666666-7777-8888-9999-000000000000"


def _request(scope=SCOPE, username="alice"):
    return SimpleNamespace(state=SimpleNamespace(user={"username": username, "user_scope_id": scope}))


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    import vaf.core.config as cfg_mod
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: default))
    monkeypatch.setattr(store, "_local_admin", lambda: "admin")
    monkeypatch.setattr(store, "_local_admin_scope_id", lambda: "admin-scope")
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: None)
    store._reset_announce_state()

    # No mail in this scratch world, in the shape a real install has it: the store exists and
    # is empty. `MailStore` creates its file on construction, so "no mail account" never
    # raises; it answers with an empty outbox and finds no op.
    closed = []

    class _EmptyMail:
        def __init__(self, scope):
            self.user_scope_id = scope
            self.store = SimpleNamespace(get_op=lambda _id: None, close=lambda: closed.append(scope))

        def list_drafts(self, **kw):
            return []

        def discard_draft(self, _op_id):
            return False
    import vaf.mail.service as svc_mod
    monkeypatch.setattr(svc_mod, "MailService", _EmptyMail)
    monkeypatch.closed_mail_stores = closed
    return monkeypatch


def _park(message="Hallo Uwe", recipient="+491700000000", username="alice", scope=SCOPE):
    from vaf.core.outbound_hold import park_messenger_call
    return park_messenger_call("send_whatsapp", {"to_phone": recipient, "message": message},
                               username=username, user_scope_id=scope)


def test_the_listing_is_the_callers_own(world):
    mine = _park("mine")
    _park("theirs", username="bob")
    out = asyncio.run(orr.list_outbox(_request()))
    assert out["count"] == 1 and out["rows"][0]["id"] == mine
    assert out["rows"][0]["preview"] == "mine"
    assert asyncio.run(orr.list_outbox(_request(username="bob")))["count"] == 1
    assert asyncio.run(orr.list_outbox(_request(username="carol")))["count"] == 0


def test_another_identity_cannot_send_or_drop_my_draft(world):
    entry_id = _park()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(orr.discard_entry("call", entry_id, _request(username="bob")))
    assert exc.value.status_code == 404
    # Mine is untouched.
    assert asyncio.run(orr.list_outbox(_request()))["count"] == 1

    # The send verb, for the same stranger: the tool is stubbed so that a route which DID
    # reach it would be caught by `sent`, and the patch goes on the module the route imports
    # from inside the function (patching a name on `orr` would create an attribute nothing
    # reads and prove nothing).
    sent = []
    import vaf.core.outbound_hold as oh
    world.setattr(oh, "resolve_tool", lambda name: SimpleNamespace(
        run=lambda **kw: sent.append(kw) or "Message sent via WhatsApp."))
    out = asyncio.run(orr.send_entry("call", entry_id, _request(username="bob")))
    assert out["ok"] is False and sent == []


def test_sending_a_call_runs_it_with_the_callers_identity(world):
    entry_id = _park()
    calls = []
    import vaf.core.outbound_hold as oh
    world.setattr(oh, "resolve_tool", lambda name: SimpleNamespace(
        run=lambda **kw: calls.append(kw) or "Message sent via WhatsApp."))
    out = asyncio.run(orr.send_entry("call", entry_id, _request()))
    assert out["ok"] is True
    assert calls and calls[0]["username"] == "alice" and calls[0]["user_scope_id"] == SCOPE
    assert asyncio.run(orr.list_outbox(_request()))["count"] == 0


def test_a_failed_send_answers_with_the_reason_and_keeps_the_draft(world):
    entry_id = _park()
    import vaf.core.outbound_hold as oh
    world.setattr(oh, "resolve_tool", lambda name: SimpleNamespace(
        run=lambda **kw: "Failed to send WhatsApp message: bridge is not running"))
    out = asyncio.run(orr.send_entry("call", entry_id, _request()))
    assert out["ok"] is False and "bridge is not running" in out["error"]
    assert asyncio.run(orr.list_outbox(_request()))["count"] == 1


def test_discarding_is_once_and_then_gone(world):
    entry_id = _park()
    assert asyncio.run(orr.discard_entry("call", entry_id, _request()))["ok"] is True
    with pytest.raises(HTTPException) as exc:
        asyncio.run(orr.discard_entry("call", entry_id, _request()))
    assert exc.value.status_code == 404


def test_an_unknown_kind_is_refused(world):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(orr.send_entry("pigeon", 1, _request()))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        asyncio.run(orr.discard_entry("pigeon", 1, _request()))
    assert exc.value.status_code == 400


def test_a_mail_verb_without_a_mail_store_does_not_pretend(world):
    """A scope with no mail answers 404 rather than reporting a send. And every verb that
    opened the store CLOSES it: the connection is thread-local, the verb runs on a pool
    thread, and a handle left open there outlives the request. MUTATION: drop the `finally`
    around either helper and the count below goes red."""
    with pytest.raises(HTTPException) as exc:
        asyncio.run(orr.discard_entry("mail", 7, _request()))
    assert exc.value.status_code == 404
    assert world.closed_mail_stores == [SCOPE], "discard opened one store and closed it"
    # And the send verb: an empty outbox holds no such op, so the shared release answers
    # "not waiting" and the route turns that into the same 404 rather than a report.
    with pytest.raises(HTTPException) as exc:
        asyncio.run(orr.send_entry("mail", 7, _request()))
    assert exc.value.status_code == 404
    assert world.closed_mail_stores == [SCOPE, SCOPE], "and so did send"
    # The listing too: it reads the mail half through the same store.
    asyncio.run(orr.list_outbox(_request()))
    assert world.closed_mail_stores == [SCOPE, SCOPE, SCOPE]


def test_a_broken_mail_store_is_an_error_and_not_a_missing_account(world, monkeypatch):
    """"No mail account" is an ANSWER, and it has to be true. A store that cannot be opened
    (a permission error, a corrupt database) used to be reported as one, which sends the
    person to the mail settings over a disk problem.

    MUTATION: catch `Exception` in `_mail_service` again and this goes red: the route answers
    "no mail account" instead of letting the failure surface.
    """
    import vaf.mail.service as svc_mod

    class _Broken:
        def __init__(self, scope):
            raise PermissionError("mail.db is not readable")
    monkeypatch.setattr(svc_mod, "MailService", _Broken)
    with pytest.raises(PermissionError):
        asyncio.run(orr.discard_entry("mail", 7, _request()))
    # The fail-closed constructor's own signal stays "no mail lane here", not an error.
    class _NoScope:
        def __init__(self, scope):
            raise ValueError("MailService requires an explicit user_scope_id")
    monkeypatch.setattr(svc_mod, "MailService", _NoScope)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(orr.discard_entry("mail", 7, _request()))
    assert exc.value.status_code == 404


def _json_request(body, scope=SCOPE, username="alice"):
    async def _json():
        if isinstance(body, Exception):
            raise body
        return body
    return SimpleNamespace(state=SimpleNamespace(user={"username": username, "user_scope_id": scope}),
                           json=_json)


def _park_in(session, message="Hallo", username="alice"):
    from vaf.core.outbound_hold import park_messenger_call
    return park_messenger_call("send_whatsapp", {"to_phone": "+491700000000", "message": message},
                               username=username, user_scope_id=SCOPE, session_id=session)


def test_the_card_reads_its_chat_with_the_decided_drafts(world):
    """The card keeps a decided draft in the conversation as its record; the default listing
    (terminal, inbox) stays what still waits. MUTATION: drop the `settled` branch and the
    discarded row vanishes from the card's listing."""
    kept = _park_in("chat-a")
    dropped = _park_in("chat-a", "zwei")
    asyncio.run(orr.discard_entry("call", dropped, _request()))
    _park_in("chat-b")
    card = asyncio.run(orr.list_outbox(_request(), session_id="chat-a", settled=True))
    assert [(r["id"], r["state"]) for r in card["rows"]] == [(dropped, "discarded"), (kept, "held")]
    waiting = asyncio.run(orr.list_outbox(_request(), session_id="chat-a"))
    assert [r["id"] for r in waiting["rows"]] == [kept]
    assert asyncio.run(orr.list_outbox(_request(username="bob"), session_id="chat-a", settled=True))["count"] == 0


def test_editing_is_the_callers_own_and_never_empty(world):
    """MUTATION: read the identity from the body, or accept a blank text."""
    entry_id = _park_in("chat-a")
    out = asyncio.run(orr.revise_entry("call", entry_id, _json_request({"body": "Hallo Uwe"})))
    assert out["ok"] is True
    assert store.held_send(entry_id, "alice", SCOPE)["preview"] == "Hallo Uwe"
    for bad, code in (({"body": "   "}, 400), ({"body": 5}, 400), ({}, 400), (["x"], 400),
                      (ValueError("no json"), 400)):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(orr.revise_entry("call", entry_id, _json_request(bad)))
        assert exc.value.status_code == code, bad
    with pytest.raises(HTTPException) as exc:
        asyncio.run(orr.revise_entry("call", entry_id, _json_request({"body": "fremd"}, username="bob")))
    assert exc.value.status_code == 404
    assert store.held_send(entry_id, "alice", SCOPE)["preview"] == "Hallo Uwe"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(orr.revise_entry("pigeon", entry_id, _json_request({"body": "x"})))
    assert exc.value.status_code == 400
