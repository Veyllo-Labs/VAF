# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A mail to an address outside plain ASCII is refused before it is queued.

Measured: the mail lane never negotiates SMTPUTF8 and smtplib sends ``RCPT TO`` in ASCII, so
``alice@exаmple.com`` (a Cyrillic а) fails permanently at the wire on every provider, and
only after the person approved the card that showed it. Refusing in the one funnel
(`MailService.queue_send`) means no draft and no card exist for such a send, in a web turn
and in a request of the Veyllo App's agent alike, and the compose window gets a 400.

MUTATIONS, each turns the named test red:
- remove the check in `queue_send` -> test_queue_send_refuses_a_non_ascii_address,
  test_no_draft_and_no_card_in_a_web_turn_or_a_request, test_the_compose_route_answers_400;
- remove the ValueError mapping in the route (it then answers 500)
  -> test_the_compose_route_answers_400.
"""
import os

import pytest

import vaf.mail.crypto as mail_crypto
from app_link_env import (_ask_turn, agent_world, BOB, RUNNER_CHAT,  # noqa: F401
                          runner_world)
from vaf.core import outbound_hold
from vaf.core.turn_binding import TurnBinding, bind_turn
from vaf.mail import case_token
from vaf.mail.service import MailService
from vaf.mail.store import MailStore

SCOPE = "12345678-1234-1234-1234-123456789abc"
ACCOUNT = {"provider": "imap", "account_id": "bob@example.com", "email": "bob@example.com"}
CONFUSABLE = "alice@exаmple.com"


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    old = mail_crypto._cached_key
    mail_crypto._cached_key = os.urandom(32)
    monkeypatch.setattr(case_token, "_root_secret", lambda: "unit-test-root-secret-of-thirty-two-bytes")
    yield
    mail_crypto._cached_key = old


@pytest.fixture()
def svc(tmp_path):
    store = MailStore(SCOPE, base_dir=tmp_path)
    s = MailService.__new__(MailService)
    s.user_scope_id = SCOPE
    s.store = store
    store.upsert_account("bob@example.com", "imap", "bob@example.com")
    yield s
    store.close()


def _ops(svc) -> int:
    return int(svc.store._conn().execute("SELECT COUNT(*) FROM ops").fetchone()[0])


@pytest.mark.parametrize("field", ["to", "cc", "bcc"])
def test_queue_send_refuses_a_non_ascii_address(svc, field):
    before = _ops(svc)
    args = {"to": "anna@example.org", "cc": "", "bcc": ""}
    args[field] = f"Alice <{CONFUSABLE}>" if field != "to" else CONFUSABLE
    from vaf.mail.service import UndeliverableAddress
    with pytest.raises(UndeliverableAddress) as caught:
        svc.queue_send("bob@example.com", args["to"], "Hallo", "Text", cc=args["cc"],
                       bcc=args["bcc"], hold=True)
    message = str(caught.value)
    assert "alice@ex<U+0430>mple.com [xn--exmple-4nf.com]" in message and "Nothing was queued" in message
    # What the model relays carries a next step, not the protocol's name.
    assert "Ask for the address in plain letters" in message and "SMTPUTF8" not in message
    message.encode("cp1252")
    assert _ops(svc) == before


def test_a_non_ascii_display_name_still_queues(svc):
    before = _ops(svc)
    out = svc.queue_send("bob@example.com", "Jürgen <j@x.de>, anna@example.org", "Hallo",
                         "Text", hold=True)
    assert out["op_id"] and _ops(svc) == before + 1


def _with_mail_tool(agent, monkeypatch, tmp_path):
    import vaf.tools.send_mail as sm
    monkeypatch.setattr(sm, "list_accounts_for_user", lambda *a, **k: ["bob@example.com"])
    monkeypatch.setattr(sm, "get_account", lambda *a, **k: dict(ACCOUNT))
    sent = []
    import vaf.mail.sender as sender
    monkeypatch.setattr(sender, "send", lambda msg: sent.append(msg) or sender.SendResult(True, "ok"))
    agent.tools["send_mail"] = sm.SendMailTool()
    return sent


@pytest.mark.parametrize("asked", [False, True])
def test_no_draft_and_no_card_in_a_web_turn_or_a_request(agent_world, monkeypatch, asked):
    agent = agent_world.make()
    sent = _with_mail_tool(agent, monkeypatch, None)
    if asked:
        _ask_turn(agent)
    else:
        bind_turn(agent, TurnBinding(identity=BOB, source="web", session_id=RUNNER_CHAT))
    agent._parked_this_round, agent._held_this_round, agent._turn_closing = [], [], None
    out = agent.execute_tool("send_mail", {"to": CONFUSABLE, "subject": "Angebot", "body": "Hallo"})
    assert out.startswith("Failed to send email"), out
    assert not out.startswith(outbound_hold.HELD_PREFIX)
    assert outbound_hold.pending(BOB.username, BOB.scope) == []
    assert agent._held_this_round == [] and agent._turn_closing is None
    assert sent == []
    # The same call to a plain address is parked as before: the refusal is the address.
    out = agent.execute_tool("send_mail", {"to": "alice@example.com", "subject": "Angebot",
                                           "body": "Hallo"})
    assert out.startswith(outbound_hold.HELD_PREFIX), out
    assert len(outbound_hold.pending(BOB.username, BOB.scope)) == 1


def test_the_compose_route_answers_400(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import vaf.api.mail_routes as routes
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(routes, "_account_ctx", lambda user, account_id: (SCOPE, "bob", dict(ACCOUNT)))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._get_current_user] = lambda: {
        "username": "bob", "user_scope_id": SCOPE, "role": "user"}
    client = TestClient(app, raise_server_exceptions=False)
    res = client.post("/api/mail/send", json={"account_id": "bob@example.com", "to": CONFUSABLE,
                                              "subject": "Hallo", "body": "Text", "undo_seconds": 60})
    assert res.status_code == 400, res.text
    assert "Nothing was queued" in res.json()["detail"]
    store = MailStore(SCOPE)
    try:
        assert int(store._conn().execute("SELECT COUNT(*) FROM ops").fetchone()[0]) == 0
    finally:
        store.close()


def test_a_front_office_refusal_logs_no_address(monkeypatch):
    """MUTATION: log ``{e}`` again in the runner's REPLY FAILED line.

    The refusal's text carries the inbound sender's whole address; the lane's own success line
    masks it (``to=...[:3]***``), so the failure line names only the kind of refusal."""
    import vaf.core.headless_runner as hr
    import vaf.mail.service as svc_mod
    from vaf.mail.service import UndeliverableAddress

    class _Svc:
        def __init__(self, scope):
            self.store = self

        def reply_prefill(self, pk):
            return {"to": CONFUSABLE, "subject": "Re: x", "body": ""}

        def account_pk(self, account_id):
            return None

        def queue_send(self, *a, **k):
            raise UndeliverableAddress(f"cannot queue a mail to {CONFUSABLE}: ...")

    lines = []
    import vaf.core.log_helper as log_helper
    monkeypatch.setattr(svc_mod, "MailService", _Svc)
    monkeypatch.setattr(log_helper, "append_lane_log", lambda lane, text, **k: lines.append(text))
    hr._deliver_email_reply(None, {"email_message_pk": 7, "email_account_id": "bob@example.com",
                                   "user_scope_id": SCOPE, "username": "bob"}, "Antwort")
    failed = [line for line in lines if "REPLY FAILED" in line]
    assert failed and "exmple" not in failed[0] and "alice" not in failed[0], lines
    assert "outside plain ASCII" in failed[0]
