# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A mail to an address no mail server can take is refused before it is queued.

``alice@ｅxample.com`` (a full-width e) is no valid internationalised domain name: strict
IDNA2008 refuses it, where a mapping codec would fold it into ``example.com``, another domain.
Refusing in the one funnel (`MailService.queue_send`) means no draft and no card exist for
such a send, in a web turn and in a request of the Veyllo App's agent alike, and the compose
window gets a 400 with the refusal's code. A look-alike that IS a valid domain
(``alice@exаmple.com``, a Cyrillic а) is delivered, and its card marks it.
tests/test_mail_international_address.py covers the deliverable side.

MUTATIONS, each turns the named test red:
- let `envelope_address` pass a domain IDNA2008 refuses (its raw punycode instead of the
  refusal) -> test_queue_send_refuses_an_undeliverable_address,
  test_no_draft_and_no_card_in_a_web_turn_or_a_request, test_the_compose_route_answers_400;
- remove the UndeliverableAddress mapping in the route (the plain 400 has no code)
  -> test_the_compose_route_answers_400;
- remove every ValueError mapping in the route (it then answers 500)
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
INVALID = "alice@ｅxample.com"


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
def test_queue_send_refuses_an_undeliverable_address(svc, field):
    before = _ops(svc)
    args = {"to": "anna@example.org", "cc": "", "bcc": ""}
    args[field] = f"Alice <{INVALID}>" if field != "to" else INVALID
    from vaf.mail.service import UndeliverableAddress
    with pytest.raises(UndeliverableAddress) as caught:
        svc.queue_send("bob@example.com", args["to"], "Hallo", "Text", cc=args["cc"],
                       bcc=args["bcc"], hold=True)
    assert caught.value.code == "domain"
    message = str(caught.value)
    assert "alice@<U+FF45>xample.com [xn--xample-hy68a.com]" in message and "Nothing was queued" in message
    # What the model relays carries a next step, not the protocol's name.
    assert "Check the address with the person" in message and "IDNA" not in message
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
    out = agent.execute_tool("send_mail", {"to": INVALID, "subject": "Angebot", "body": "Hallo"})
    assert out.startswith("Failed to send email"), out
    assert not out.startswith(outbound_hold.HELD_PREFIX)
    assert outbound_hold.pending(BOB.username, BOB.scope) == []
    assert agent._held_this_round == [] and agent._turn_closing is None
    assert sent == []
    # A look-alike that is a valid domain is parked like any draft, and its card marks it.
    out = agent.execute_tool("send_mail", {"to": CONFUSABLE, "subject": "Angebot",
                                           "body": "Hallo"})
    assert out.startswith(outbound_hold.HELD_PREFIX), out
    rows = outbound_hold.pending(BOB.username, BOB.scope)
    assert len(rows) == 1
    assert rows[0]["to_shown"] == "alice@ex\u27e8U+0430\u27e9mple.com [xn--exmple-4nf.com]", rows[0]


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
    res = client.post("/api/mail/send", json={"account_id": "bob@example.com", "to": INVALID,
                                              "subject": "Hallo", "body": "Text", "undo_seconds": 60})
    assert res.status_code == 400, res.text
    from vaf.core.shown_text import mail_address
    from vaf.mail.service import UndeliverableAddress
    body = res.json()
    assert body["detail"] == str(UndeliverableAddress("domain", INVALID))
    assert "Nothing was queued" in body["detail"]
    assert body["code"] == "domain" and body["address"] == str(mail_address(INVALID))
    store = MailStore(SCOPE)
    try:
        assert int(store._conn().execute("SELECT COUNT(*) FROM ops").fetchone()[0]) == 0
    finally:
        store.close()


def test_a_front_office_refusal_logs_no_address(monkeypatch):
    """MUTATION: log ``{e}`` again in the runner's REPLY FAILED line.

    The refusal's text carries the inbound sender's whole address; the lane's own success line
    masks it (``to=...[:3]***``), so the failure line names only the kind of refusal, by its
    code."""
    import vaf.core.headless_runner as hr
    import vaf.mail.service as svc_mod
    from vaf.mail.service import UndeliverableAddress

    class _Svc:
        def __init__(self, scope):
            self.store = self

        def reply_prefill(self, pk):
            return {"to": INVALID, "subject": "Re: x", "body": ""}

        def account_pk(self, account_id):
            return None

        def queue_send(self, *a, **k):
            raise UndeliverableAddress("domain", INVALID)

    lines = []
    import vaf.core.log_helper as log_helper
    monkeypatch.setattr(svc_mod, "MailService", _Svc)
    monkeypatch.setattr(log_helper, "append_lane_log", lambda lane, text, **k: lines.append(text))
    hr._deliver_email_reply(None, {"email_message_pk": 7, "email_account_id": "bob@example.com",
                                   "user_scope_id": SCOPE, "username": "bob"}, "Antwort")
    failed = [line for line in lines if "REPLY FAILED" in line]
    assert failed and "xample" not in failed[0] and "alice" not in failed[0], lines
    assert "FF45" not in failed[0] and "xn--" not in failed[0], lines
    assert "not a valid internationalised domain name" in failed[0]
