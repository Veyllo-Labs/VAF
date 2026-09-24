# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The mail answering lane (vaf/mail/inbound.py, FRONT_OFFICE.md "Mail"): the cursor arms
on the first run and nothing older than the switch is judged, machine and unverified mail
is ignored, a verified stranger through the open channel gets a case, a contact record and a
Front Office task with the thread fenced, an opted-out contact is left alone, a reply into a
case the agent wrote in comes through a closed channel, the caps park the rest of the day
with one event, and the runner hands the answer to the outbox held or sent by the reply
mode. Plus the harness: the draft routes, the Inbound window's mail row and the source
guards. Isolated: tmp data dir, in-memory config, pinned key and case secret.

MUTATION: drop the opened_at filter from new_inbox_messages and the backlog test goes red;
drop the enrolment from the lane and the contact test goes red; let _deliver_email_reply pass
hold=False for draft mode and the held test goes red."""
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import vaf.mail.crypto as mail_crypto
import vaf.mail.sender as sender
from vaf.core import contacts_store
from vaf.core.platform import Platform
from vaf.mail import case_token, inbound
from vaf.mail.parser import parse_message
from vaf.mail.service import MailService
from vaf.mail.store import MailStore

REPO = Path(__file__).resolve().parents[1]
SCOPE = "11111111-2222-3333-4444-555555555555"
ACCOUNT = "alice@example.com"
OPENED = 1_700_000_000


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    old = mail_crypto._cached_key
    mail_crypto._cached_key = os.urandom(32)
    monkeypatch.setattr(case_token, "_root_secret", lambda: "unit-test-root-secret-of-thirty-two-bytes")
    from vaf.core.channel_ingress_policy import set_front_office
    state = {
        "local_admin_scope_id": SCOPE,
        "local_admin_username": "alice",
        "channel_ingress_policy": set_front_office(None, True, "email", now=OPENED),
        "email_config_by_scope": {SCOPE: {"accounts": [
            {"account_id": ACCOUNT, "email": ACCOUNT, "provider": "imap", "enabled": True,
             "trusted_authserv_id": "mx.google.com"},
        ]}},
    }
    import vaf.core.config as cfg_mod
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    monkeypatch.setattr(cfg_mod.Config, "load", classmethod(lambda cls: json.loads(json.dumps(state))))
    monkeypatch.setattr(cfg_mod.Config, "save", classmethod(lambda cls, cfg: state.update(cfg)))
    events = []
    import vaf.core.security_events as sec
    monkeypatch.setattr(sec, "log_security_event", lambda kind, **f: events.append((kind, f)))
    lane_log = []
    import vaf.core.log_helper as lh
    monkeypatch.setattr(lh, "log_channel_inbound", lambda ch, msg, always=False: lane_log.append((ch, msg)))
    monkeypatch.setattr(sender, "send", lambda msg: sender.SendResult(True, "ok"))
    yield {"state": state, "events": events, "lane_log": lane_log}
    mail_crypto._cached_key = old


def _raw(mid, sender_addr="Lena <lena@example.org>", subject="Vertrag", date="Mon, 20 Nov 2023 10:00:00 +0000",
         auth="mx.google.com; dkim=pass header.i=@example.org; spf=pass smtp.mailfrom=lena@example.org; dmarc=pass header.from=example.org",
         extra="", body="Koennen wir telefonieren?"):
    head = f"Authentication-Results: {auth}\n" if auth else ""
    return (f"{head}From: {sender_addr}\nTo: {ACCOUNT}\nSubject: {subject}\nDate: {date}\nMessage-ID: {mid}\n{extra}\n{body}\n").encode()


def _store():
    s = MailStore(SCOPE)
    apk = s.upsert_account(ACCOUNT, "imap", ACCOUNT)
    fpk = s.upsert_folder(apk, "INBOX", special_use="\\Inbox", sync_tier="eager")
    return s, apk, fpk


def _policy():
    from vaf.mail.verification import auth_policy_for_account
    return auth_policy_for_account({"account_id": ACCOUNT, "email": ACCOUNT, "trusted_authserv_id": "mx.google.com"})


def _ingest(s, apk, fpk, uid, raw):
    return s.ingest_message(apk, fpk, uid, parse_message(raw), raw=raw, auth_policy=_policy())


def _run(queue):
    return inbound.process_account(SCOPE, ACCOUNT, now=datetime(2023, 11, 20, 12, 0, tzinfo=timezone.utc),
                                   enqueue=lambda sid, text, meta: queue.append((sid, text, meta)))


def test_the_cursor_arms_first_and_the_backlog_is_never_judged(world):
    s, apk, fpk = _store()
    _ingest(s, apk, fpk, 1, _raw("<old@example.org>", date="Mon, 13 Nov 2023 10:00:00 +0000"))
    s.close()
    queue = []
    first = _run(queue)
    assert first["skipped"] == "cursor armed" and queue == []
    s = MailStore(SCOPE)
    _ingest(s, apk, fpk, 2, _raw("<older@example.org>", date="Tue, 14 Nov 2023 10:00:00 +0000"))
    _ingest(s, apk, fpk, 3, _raw("<new@example.org>"))
    s.close()
    out = _run(queue)
    assert out["judged"] == 1 and out["draft"] == 1 and len(queue) == 1, "only the mail sent after the switch"
    assert _run(queue)["judged"] == 0, "the cursor moved; nothing is judged twice"


def test_a_full_page_moves_the_cursor_to_its_last_row_and_the_rest_is_judged_next_run(world, monkeypatch):
    """A sync can ingest more inbox rows than one run reads (the store query is capped at
    INBOX_PAGE_LIMIT); the cursor then stops at the last row read, so the remainder is judged
    on the next run instead of vanishing behind max_message_pk.
    MUTATION: advance the cursor to max_message_pk on a full page and the cursor assertion goes
    red (the second run judges nothing); drop the limit argument from the query and the first
    run judges all three rows."""
    monkeypatch.setattr(inbound, "INBOX_PAGE_LIMIT", 2)
    s, apk, fpk = _store()
    s.set_account_state(apk, inbound_cursor=0)
    pks = [_ingest(s, apk, fpk, i, _raw(f"<m{i}@example.org>", sender_addr=f"Pia {i} <pia{i}@example.org>"))
           for i in (1, 2, 3)]
    s.close()
    queue = []
    assert _run(queue)["judged"] == 2, "one page"
    s = MailStore(SCOPE)
    try:
        assert s.account_state(apk)["inbound_cursor"] == pks[1], "the cursor stops at the last row read"
    finally:
        s.close()
    assert _run(queue)["judged"] == 1 and len(queue) == 3, "the third row is judged on the next run, never skipped"
    assert _run(queue)["judged"] == 0


def test_a_closed_channel_without_a_case_judges_nothing_but_arms_the_cursor(world):
    from vaf.core.channel_ingress_policy import set_front_office
    world["state"]["channel_ingress_policy"] = set_front_office(None, False, "email")
    s, apk, fpk = _store()
    _ingest(s, apk, fpk, 1, _raw("<meanwhile@example.org>"))
    s.close()
    assert _run([])["skipped"] == "closed"
    s = MailStore(SCOPE)
    try:
        assert s.account_state(apk)["inbound_cursor"] == 1, "what arrived while the channel was off is behind the cursor"
    finally:
        s.close()


def test_a_closed_channel_still_answers_a_contact_the_owner_allowed(world):
    """The lane's open/closed question is not the policy's alone: a person the owner ALLOWED
    is answered on every channel they have, so a closed mail channel with such a contact in
    the book is still a lane that judges.

    MUTATION: drop the `front_office_endpoints` branch from `inbound._lane_open` and this goes
    red with skipped == "closed" - the allowed contact would be silently unanswerable, which is
    exactly the permission the owner gave by hand.
    """
    from vaf.core.channel_ingress_policy import set_front_office
    world["state"]["channel_ingress_policy"] = set_front_office(None, False, "email")
    # Nobody allowed yet: the lane does not even look.
    s, apk, fpk = _store()
    s.set_account_state(apk, inbound_cursor=0)
    _ingest(s, apk, fpk, 1, _raw("<lena@example.org>"))
    s.close()
    assert _run([])["skipped"] == "closed"

    contacts_store.create_contact("Lena", "alice", user_scope_id=SCOPE, email="lena@example.org",
                                  assistant_access="allowed")
    s = MailStore(SCOPE)
    s.set_account_state(apk, inbound_cursor=0)
    s.close()
    queue = []
    out = _run(queue)
    assert out["skipped"] == "" and out["draft"] == 1, "her own permission opened the lane"
    assert queue[0][2]["ingress_reason"] == "contact_allowed" and queue[0][2]["email_trust"] == "T3"


def test_a_verified_stranger_gets_a_case_a_contact_and_a_fenced_turn(world):
    s, apk, fpk = _store()
    s.set_account_state(apk, inbound_cursor=0)
    pk = _ingest(s, apk, fpk, 1, _raw("<new@example.org>"))
    s.close()
    queue = []
    out = _run(queue)
    assert out["draft"] == 1
    session_id, text, meta = queue[0]
    assert session_id == "email_alice_lena_example_org"
    assert meta["from_contact"] is True and meta["origin_channel"] == "email" and meta["email_reply_mode"] == "draft"
    assert meta["email_message_pk"] == pk and meta["email_from"] == "lena@example.org" and meta["email_trust"] == "T2"
    assert meta["ingress_reason"] == "front_office_open" and meta["chat_label"] == "Lena"
    assert "<untrusted_email_thread>" in text and "Koennen wir telefonieren?" in text and "(NEWEST, answer this one)" in text
    assert "complete e-mail" in text and "No subject line" in text
    s = MailStore(SCOPE)
    try:
        case = s.case_by_id(apk, meta["email_case_id"])
        assert case and case["correspondent"] == "lena@example.org" and case["status"] == "open" and case["trust_max"] == "T2"
        assert s.case_message(pk)["decision"] == "draft" and s.case_message(pk)["outcome"] == "new"
    finally:
        s.close()
    rec = contacts_store.find_contact_by_channel("email", "lena@example.org", "alice", SCOPE)
    assert rec and rec["source"] == "front_office" and rec["name"] == "Lena"
    assert contacts_store.contact_access(rec) is None, \
        "the open channel answered her; the record carries no standing permission"
    assert [e for e in world["events"] if e[0] == "contact_access_changed"][0][1]["channel"] == "email"
    assert any("DRAFT from=len*** trust=T2" in m for _c, m in world["lane_log"])


def test_send_mode_answers_and_machine_or_unverified_mail_is_ignored(world):
    from vaf.core.channel_ingress_policy import set_email_reply_mode
    world["state"]["channel_ingress_policy"] = set_email_reply_mode(world["state"]["channel_ingress_policy"], "send")
    s, apk, fpk = _store()
    s.set_account_state(apk, inbound_cursor=0)
    _ingest(s, apk, fpk, 1, _raw("<p@example.org>"))
    _ingest(s, apk, fpk, 2, _raw("<a@example.org>", extra="Auto-Submitted: auto-replied\n"))
    _ingest(s, apk, fpk, 3, _raw("<u@example.org>", auth=""))
    _ingest(s, apk, fpk, 4, _raw("<v@example.org>", auth="mx.google.com; dkim=pass header.d=other.example; spf=none"))
    _ingest(s, apk, fpk, 5, _raw("<n@example.org>", sender_addr="News <newsletter@example.org>", extra="List-Id: <news.example.org>\n"))
    s.close()
    queue = []
    out = _run(queue)
    assert out["answer"] == 1 and out["ignore"] == 4 and queue[0][2]["email_reply_mode"] == "send"
    reasons = sorted(m.split("reason=")[1].split(" ")[0] for _c, m in world["lane_log"])
    assert reasons == ["machine:auto_reply", "machine:list", "ok", "unverified", "via"]


def test_a_denied_contact_is_left_alone_and_an_allowed_one_is_t3(world):
    """The lane read the old bool here, so every contact the mail sync had ever created
    counted as an opt-out. MUTATION: read `allow_as_assistant_user` again in process_account
    and the first assertion goes red: Lena has a record and no decision, so the open channel
    must answer her."""
    contacts_store.create_contact("Lena", "alice", user_scope_id=SCOPE, email="lena@example.org")
    s, apk, fpk = _store()
    s.set_account_state(apk, inbound_cursor=0)
    _ingest(s, apk, fpk, 1, _raw("<x@example.org>"))
    s.close()
    queue = []
    assert _run(queue)["draft"] == 1 and queue[0][2]["ingress_reason"] == "front_office_open"
    rec = contacts_store.find_contact_by_channel("email", "lena@example.org", "alice", SCOPE)
    contacts_store.update_contact(rec["id"], "alice", user_scope_id=SCOPE, assistant_access="denied")
    s = MailStore(SCOPE)
    _ingest(s, apk, fpk, 2, _raw("<y@example.org>"))
    s.close()
    queue.clear()
    assert _run(queue)["ignore"] == 1 and queue == []
    assert any("reason=contact_denied" in m for _c, m in world["lane_log"])
    contacts_store.update_contact(rec["id"], "alice", user_scope_id=SCOPE, assistant_access="allowed")
    s = MailStore(SCOPE)
    _ingest(s, apk, fpk, 3, _raw("<z@example.org>"))
    s.close()
    assert _run(queue)["draft"] == 1 and queue[0][2]["email_trust"] == "T3"
    assert queue[0][2]["ingress_reason"] == "contact_allowed"


def test_a_reply_into_a_case_the_agent_wrote_in_comes_through_a_closed_channel(world):
    from vaf.core.channel_ingress_policy import set_front_office
    s, apk, fpk = _store()
    s.set_account_state(apk, inbound_cursor=0)
    first = _ingest(s, apk, fpk, 1, _raw("<q@example.org>"))
    s.close()
    queue = []
    assert _run(queue)["draft"] == 1
    case_id = queue[0][2]["email_case_id"]
    svc = MailService(SCOPE)
    anchor = svc.queue_send(ACCOUNT, "lena@example.org", "Re: Vertrag", "Gern.", in_reply_to="<q@example.org>",
                            undo_seconds=0, case_id=case_id, sent_by="front_office", agent_written=True, reply_to_pk=first)["message_id"]
    svc.store.close()
    world["state"]["channel_ingress_policy"] = set_front_office(world["state"]["channel_ingress_policy"], False, "email")
    s = MailStore(SCOPE)
    _ingest(s, apk, fpk, 2, _raw("<r@example.org>", subject="Re: Vertrag", extra=f"In-Reply-To: {anchor}\nReferences: <q@example.org> {anchor}\n"))
    s.close()
    queue.clear()
    out = _run(queue)
    assert out["draft"] == 1 and queue[0][2]["email_trust"] == "T4" and queue[0][2]["email_case_id"] == case_id
    assert queue[0][2]["ingress_reason"] == "open_conversation"
    s = MailStore(SCOPE)
    try:
        cm = s.case_message(s.pk_by_message_id("<r@example.org>"))
        assert cm["signal"] == "own_id" and cm["certainty"] == "certain" and cm["outcome"] == "case"
    finally:
        s.close()


def test_the_caps_park_the_address_with_one_event_a_day(world):
    s, apk, fpk = _store()
    s.set_account_state(apk, inbound_cursor=0)
    svc = MailService(SCOPE)
    for i in range(3):
        svc.queue_send(ACCOUNT, "lena@example.org", "s", "b", undo_seconds=0, sent_by="front_office")
    for i in range(2):
        _ingest(svc.store, apk, fpk, i + 1, _raw(f"<c{i}@example.org>"))
    svc.store.close()
    queue = []
    out = _run(queue)
    assert out["ignore"] == 2 and queue == []
    capped = [e for e in world["events"] if e[0] == "mail_auto_reply_capped"]
    assert len(capped) == 1, "once per address and day"


def test_a_foreign_anchor_is_a_security_event_and_handle_new_mail_never_raises(world, monkeypatch):
    s, apk, fpk = _store()
    s.set_account_state(apk, inbound_cursor=0)
    foreign = case_token.mint_message_id("00000000-0000-0000-0000-000000000000", ACCOUNT, case_token.mint_case_id(), "example.com")
    _ingest(s, apk, fpk, 1, _raw("<f@example.org>", extra=f"In-Reply-To: {foreign}\n"))
    s.close()
    assert _run([])["draft"] == 1, "the mail is still answered as new; the token is reported, never trusted"
    assert [e for e in world["events"] if e[0] == "mail_case_token_misuse"]
    monkeypatch.setattr(inbound, "process_account", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    inbound.handle_new_mail(SCOPE, ACCOUNT, {"new": 1})


# ── the runner hands the answer to the outbox ─────────────────────────────────────────

def _runner_meta(pk, thread_id, case_id, mode):
    return {"user_scope_id": SCOPE, "username": "alice", "from_contact": True, "email_account_id": ACCOUNT,
            "email_message_pk": pk, "email_thread_id": thread_id, "email_case_id": case_id, "email_reply_mode": mode,
            "email_from": "lena@example.org"}


def test_the_runner_holds_the_answer_in_draft_mode_and_sends_it_in_send_mode(world, monkeypatch):
    from types import SimpleNamespace
    from vaf.core import headless_runner as hr
    signals = []
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: signals.append(scope))
    s, apk, fpk = _store()
    pk = _ingest(s, apk, fpk, 1, _raw("<q@example.org>"))
    thread_id = s.get_message(pk)["thread_id"]
    case_id = case_token.mint_case_id()
    s.open_case(apk, case_id, thread_id=thread_id, correspondent="lena@example.org")
    s.close()
    task = SimpleNamespace(session_id="email_alice_lena_example_org")
    hr._deliver_email_reply(task, _runner_meta(pk, thread_id, case_id, "draft"), "Hallo Lena,\n\ngern.\n\nViele Gruesse")
    svc = MailService(SCOPE)
    try:
        drafts = svc.list_drafts(thread_id=thread_id)
        assert len(drafts) == 1 and drafts[0]["sent_by"] == "front_office" and "lena@example.org" in drafts[0]["to"]
        assert drafts[0]["body"].startswith("Hallo Lena,") and "> Koennen wir telefonieren?" in drafts[0]["body"]
        assert drafts[0]["case_id"] == case_id
        assert svc.store.case_by_id(apk, case_id)["status"] == "held" and signals == [SCOPE]
        assert case_token.verify_anchor(SCOPE, ACCOUNT, drafts[0]["message_id"]) == case_id
        assert not svc.store.get_message(pk)["answered_at"], "held is not answered"
        # the inbox and the mail rows say so
        from vaf.core import inbox
        row = [r for r in inbox.list_conversations("alice", SCOPE)["rows"] if r["channel"] == "mail"][0]
        assert row["waits"] and row["waits_reason"] == "draft" and row["draft"]["op_id"] == drafts[0]["op_id"]
        # send mode: the second answer leaves at once
        hr._deliver_email_reply(task, _runner_meta(pk, thread_id, case_id, "send"), "Zweite Antwort")
        assert svc.store.case_by_id(apk, case_id)["status"] == "answered"
        sent = [r for r in svc.store.sent_ids_for_case(apk, case_id) if r["delivery"] == "sent"]
        assert len(sent) == 1 and svc.store.get_message(pk)["answered_at"]
        assert svc.store.get_op(sent[0]["op_id"])["payload"]["references"].split()[0] == drafts[0]["message_id"], "the root anchor rides on every later mail"
        # A send that does not leave (the wire refuses) leaves the case as it was, and so
        # does a missing account: answered means the mail left.
        case2 = case_token.mint_case_id()
        svc.store.open_case(apk, case2, thread_id=thread_id, correspondent="lena@example.org")
        monkeypatch.setattr(sender, "send", lambda msg: sender.SendResult(False, "wire refused"))
        hr._deliver_email_reply(task, _runner_meta(pk, thread_id, case2, "send"), "Dritte Antwort")
        assert svc.store.case_by_id(apk, case2)["status"] == "open", "answered only once the mail left"
        import vaf.core.email_accounts as ea
        monkeypatch.setattr(ea, "get_account", lambda *a, **k: None)
        hr._deliver_email_reply(task, _runner_meta(pk, thread_id, case2, "send"), "Vierte Antwort")
        assert svc.store.case_by_id(apk, case2)["status"] == "open"
    finally:
        svc.store.close()


def test_the_runner_and_the_prompt_know_the_mail_lane():
    src = (REPO / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")
    assert 'elif task_source == "email":' in src and "_deliver_email_reply(task, meta, final_text)" in src
    assert 'task_source_err == "email"' not in src, "an error is never mailed to a stranger"
    assert 'disable_workflows = str(task_source).lower() in CHAT_CHANNELS + ("email",)' in src, \
        "no workflows on a contact's mail, nor on any chat channel"
    assert '{"channel": "mail", "chat_id": str(tid).strip()}' in src
    from vaf.core import headless_runner as hr
    assert hr._front_office_chat_ref({"email_thread_id": 7}, "alice") == {"channel": "mail", "chat_id": "7"}
    from vaf.core.system_prompt import SystemPromptManager
    mgr = SystemPromptManager(tools=[], model_name="Local", agent_instance=None, username="alice")
    assert mgr._format_channel("email") == "E-Mail"
    server = (REPO / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    assert "_on_new_mail(_mail_inbound.handle_new_mail)" in server


# ── the routes ─────────────────────────────────────────────────────────────────────────

USER = {"username": "alice", "user_scope_id": SCOPE, "role": "user"}


def test_a_released_draft_the_sweep_delivers_is_not_a_failure(monkeypatch):
    """`release_held_draft` does two acts, and only the second one puts the mail on the wire.
    When the immediate drain cannot run (no IMAP session, no matching account, a deferred op)
    the op stays `pending` and the sweep takes it: the mail has left the person's hands, so the
    card must not tell them it did not work. `state` keeps the difference.

    The ACCOUNT decides whether the draft may leave the held state at all: nothing but a
    configured account can deliver it, the sweep included, so a draft released for an account
    the config no longer holds would become a pending op nobody ever drains while the person
    was told the next run would take it.

    MUTATION: compare the state with "done" alone and the pending case goes red; approve
    before the account is resolved and the last block goes red (the draft leaves and is lost).
    """
    from types import SimpleNamespace

    import vaf.core.email_accounts as ea
    from vaf.mail.service import NO_ACCOUNT_FOR_DRAFT, release_held_draft

    restored = []
    # Through monkeypatch, never by assignment: a failing assertion below would otherwise leave
    # the stub in place for every later test in the run.
    accounts = {"accounts": [{"account_id": "a@example.com", "email": "a@example.com"}]}
    monkeypatch.setattr(ea, "get_email_config", lambda *a, **k: accounts)

    def _svc(final_state):
        store = SimpleNamespace(get_op=lambda _id: {"kind": "send", "state": "held",
                                                    "payload": {"account_id": "a@example.com"}},
                                mark_op=lambda _id, state, **kw: restored.append((state, kw)) or True)
        return SimpleNamespace(store=store, approve_draft=lambda _id: True,
                               draft_state=lambda op: ("held", ""),
                               send_outcome=lambda _id: {"state": final_state, "error": ""})

    assert release_held_draft("scope", "alice", 1, service=_svc("pending")) == {
        "ok": True, "state": "pending", "delivery": "", "error": ""}
    assert release_held_draft("scope", "alice", 1, service=_svc("done"))["ok"] is True
    for bad in ("failed", "cancelled", "discarded", ""):
        out = release_held_draft("scope", "alice", 1, service=_svc(bad))
        assert out["ok"] is False and out["state"] == bad, bad
    # Only the failed outcome goes back to held; a cancelled or discarded op is not the
    # person's any more and is left alone.
    assert restored == [("held", {"expect_state": "failed"})]
    # And a draft that is not waiting is still "no draft with that id", not a send.
    not_waiting = SimpleNamespace(
        store=SimpleNamespace(get_op=lambda _id: {"kind": "send", "state": "done", "payload": {}}),
        approve_draft=lambda _id: True, draft_state=lambda op: ("held", ""),
        send_outcome=lambda _id: {"state": "done", "error": ""})
    assert release_held_draft("scope", "alice", 1, service=not_waiting)["error"] == "not waiting"

    # No account, no release: the draft stays held and says why, instead of turning into a
    # pending op that nothing will ever drain.
    approved = []
    gone = SimpleNamespace(
        store=SimpleNamespace(get_op=lambda _id: {"kind": "send", "state": "held",
                                                  "payload": {"account_id": "removed@example.com"}}),
        approve_draft=lambda _id: approved.append(_id) or True,
        draft_state=lambda op: ("held", ""), send_outcome=lambda _id: {"state": "pending", "error": ""})
    assert release_held_draft("scope", "alice", 1, service=gone) == {
        "ok": False, "state": "held", "error": NO_ACCOUNT_FOR_DRAFT}
    assert approved == [], "the draft never left the held state"
    # And a config that cannot be read is not permission either.
    def _unreadable(*a, **k):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(ea, "get_email_config", _unreadable)
    assert release_held_draft("scope", "alice", 1, service=_svc("pending"))["state"] == "held"


def test_the_draft_routes_send_and_discard(world, monkeypatch):
    import vaf.api.mail_routes as mr
    signals = []
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: signals.append(scope))
    s, apk, fpk = _store()
    pk = _ingest(s, apk, fpk, 1, _raw("<q@example.org>"))
    thread_id = s.get_message(pk)["thread_id"]
    s.close()
    svc = MailService(SCOPE)
    one = svc.queue_send(ACCOUNT, "lena@example.org", "Re: Vertrag", "A", undo_seconds=0, hold=True, sent_by="front_office", reply_to_pk=pk, thread_id=thread_id)
    two = svc.queue_send(ACCOUNT, "lena@example.org", "Re: Vertrag", "B", undo_seconds=0, hold=True, sent_by="front_office", thread_id=thread_id)
    svc.store.close()
    listed = asyncio.run(mr.list_drafts(thread_id=thread_id, _user=USER))["drafts"]
    assert [d["op_id"] for d in listed] == [two["op_id"], one["op_id"]]
    threads = asyncio.run(mr.list_threads(_user=USER))["threads"]
    assert threads[0]["waits"] and threads[0]["waits_reason"] == "draft" and threads[0]["draft"]["op_id"] == two["op_id"]
    out = asyncio.run(mr.send_draft(one["op_id"], _user=USER))
    assert out["state"] == "done" and out["delivery"] == "sent" and SCOPE in signals
    assert asyncio.run(mr.discard_draft(two["op_id"], _user=USER)) == {"ok": True}
    assert asyncio.run(mr.list_drafts(thread_id=None, _user=USER))["drafts"] == []
    with pytest.raises(mr.HTTPException):
        asyncio.run(mr.send_draft(one["op_id"], _user=USER))
    with pytest.raises(mr.HTTPException):
        asyncio.run(mr.discard_draft(two["op_id"], _user=USER))
    again = MailStore(SCOPE)
    try:
        assert again.get_message(pk)["answered_at"], "the approved answer marked the mail answered"
    finally:
        again.close()


def test_the_front_office_state_and_the_reply_mode_route(world, monkeypatch):
    from types import SimpleNamespace
    from vaf.api import front_office_routes as routes
    monkeypatch.setattr(routes, "log_security_event", lambda kind, **f: world["events"].append((kind, f)))
    req = SimpleNamespace(headers={}, cookies={}, query_params={}, client=None)
    monkeypatch.setattr(routes, "get_current_user_or_local_admin", lambda request: {"username": "alice", "user_scope_id": SCOPE})
    state = asyncio.run(routes.get_front_office(req))
    assert state["channels"]["email"] is True and state["email_reply_mode"] == "draft" and state["channels_connected"]["email"] is True
    out = asyncio.run(routes.put_front_office_mail(routes.FrontOfficeMailUpdate(reply_mode="send"), req, {"username": "alice", "role": "admin"}))
    assert out["email_reply_mode"] == "send"
    assert world["events"][-1] == ("front_office_changed", {"channel": "email", "username": "alice", "detail": "reply mode send"})
    n = len(world["events"])
    asyncio.run(routes.put_front_office_mail(routes.FrontOfficeMailUpdate(reply_mode="send"), req, {"username": "alice", "role": "admin"}))
    assert len(world["events"]) == n, "a write that changes nothing records nothing"
    with pytest.raises(routes.HTTPException):
        asyncio.run(routes.put_front_office_mail(routes.FrontOfficeMailUpdate(reply_mode="later"), req, {"username": "alice", "role": "admin"}))


# ── the web sources ────────────────────────────────────────────────────────────────────

def test_the_inbound_window_switches_mail_and_offers_the_reply_mode():
    src = (REPO / "web" / "components" / "connections" / "FrontOfficeDashboard.tsx").read_text(encoding="utf-8")
    assert "{ id: 'email', label: 'E-Mail', icon: Mail, color: 'bg-amber-500' }" in src
    assert "type FrontOfficeChannel = 'whatsapp' | 'telegram' | 'discord' | 'email';" in src
    assert "api/front-office/mail" in src and "t('mailModeDraft')" in src and "t('mailModeSend')" in src and "t('mailModeHint')" in src
    assert "t('confirmBodyMail'" in src


def test_the_mail_window_and_the_inbox_show_the_held_draft():
    page = (REPO / "web" / "app" / "mail" / "page.tsx").read_text(encoding="utf-8")
    for key in ("draft.title", "draft.hint", "draft.send", "draft.edit", "draft.discard", "draft.sent", "draft.failed"):
        assert f"t('{key}'" in page, key
    assert "api/mail/drafts/${d.op_id}/send" in page and "'DELETE'" in page
    inbox = (REPO / "web" / "components" / "inbox" / "InboxWindow.tsx").read_text(encoding="utf-8")
    for key in ("draftTitle", "draftWaiting", "draftSend", "draftDiscard"):
        assert f"t('{key}')" in inbox, key
    assert "api/mail/drafts/${r.draft.op_id}/send" in inbox
    assert "res.ok && (action === 'discard' || data.state === 'done')" in inbox and "t('draftFailed'" in inbox, \
        "the inbox reloads only when the outbox says the mail left"
    assert "api/mail/drafts?thread_id=${threadId}" in page and "editingDraftRef.current = heldOpId ?? null" in page, \
        "a jumped-to thread fetches its draft by id; an edited draft consumes the held op"
    # MUTATION: rendering the untagged draft again (`activeRow?.draft ?? activeDraft`) turns this red.
    assert "const draft = activeRow?.draft ?? (activeDraft && activeDraft.threadId === activeThread ? activeDraft.draft : null);" in page, \
        "the fetched draft is tagged with its thread: a search hit (no active thread) never shows another thread's draft"
    # MUTATION: leaving the row's draft in place until the reload (`.then(() => { setActiveDraft(null); loadThreads(); }`) turns this red.
    assert "setThreads(prev => prev.map(tr => tr.thread_id === activeThread ? { ...tr, draft: null } : tr));" in page, \
        "an edited held draft leaves the list row at once, not only after the reload"
    shell = (REPO / "web" / "components" / "connections" / "ChannelDashboardShell.tsx").read_text(encoding="utf-8")
    assert "reason === 'draft' ? t('waitsDraft')" in shell
    for path in sorted((REPO / "web" / "messages").glob("*.json")):
        d = json.loads(path.read_text(encoding="utf-8"))
        assert {"mailModeLabel", "mailModeDraft", "mailModeSend", "mailModeHint", "confirmBodyMail"} <= set(d["settings"]["frontOffice"]), path.name
        assert "waitsDraft" in d["settings"]["channelDashboard"], path.name
        assert set(d["mailV2"]["draft"]) == {"title", "hint", "send", "edit", "discard", "sent", "failed"}, path.name
        assert {"draftTitle", "draftWaiting", "draftSend", "draftDiscard", "draftFailed"} <= set(d["inbox"]), path.name
        assert {"ovEvMailSpoof", "ovEvMailToken", "ovEvMailCapped"} <= set(d["notifications"]), path.name


def test_a_held_draft_carries_every_recipient_and_every_file(world):
    """What the person approves is byte for byte what leaves, so the row shows all of it: a
    card with the To line alone let a Bcc or a document go out unseen. The names come from
    the attachment metadata `send_mail` stores beside the op, never the paths.

    MUTATION: drop `bcc` or `attachments` from `list_drafts` or from the unified row and this
    goes red.
    """
    from vaf.core import outbound_hold
    svc = MailService(SCOPE)
    try:
        q = svc.queue_send(ACCOUNT, "lena@example.org", "Angebot", "Guten Tag", cc="cc@example.org",
                           bcc="bcc@example.org", undo_seconds=0, hold=True, sent_by="agent",
                           attachment_meta=[{"path": "/home/user/docs/report.pdf", "filename": "report.pdf"}],
                           chat_session_id="chat-1")
        d = svc.list_drafts()[0]
        assert d["op_id"] == q["op_id"] and d["cc"] == "cc@example.org" and d["bcc"] == "bcc@example.org"
        assert d["attachments"] == ["report.pdf"] and d["state"] == "held" and d["error"] == ""
    finally:
        svc.store.close()
    row = next(r for r in outbound_hold.pending("alice", SCOPE, session_id="chat-1") if r["kind"] == "mail")
    assert row["cc"] == "cc@example.org" and row["bcc"] == "bcc@example.org" and row["attachments"] == ["report.pdf"]
    assert row["state"] == "held" and row["error"] == ""


def test_a_mail_send_that_did_not_leave_comes_back_to_the_person_with_the_reason(world, monkeypatch):
    """The parked-call lane keeps a failed draft on the card with its reason; the mail lane
    parked it as a failed op for the ops API, where nobody was looking, and the card lost it
    with nothing said. `release_held_draft` now reads the outcome by state: `failed` goes back
    to `held` with the reason, and the next approval starts with a fresh attempt budget.

    MUTATION: drop the `mark_op(..., "held")` restore and the second block goes red (the draft
    is gone); drop `attempts=0` from `approve_op` and the last block goes red (the sweep parks
    the draft as "max attempts reached" before trying).
    """
    from vaf.core import outbound_hold
    from vaf.mail.service import release_held_draft
    svc = MailService(SCOPE)
    try:
        q = svc.queue_send(ACCOUNT, "lena@example.org", "Angebot", "Guten Tag", undo_seconds=0, hold=True,
                           sent_by="agent", chat_session_id="chat-1")
        op_id = q["op_id"]
        monkeypatch.setattr(sender, "send", lambda msg: sender.SendResult(False, "permanent", error="wire refused"))
        out = release_held_draft(SCOPE, "alice", op_id, service=svc)
        assert out == {"ok": False, "state": "failed", "delivery": "failed", "error": "wire refused"}
        # Still the person's: held, listed, with the reason, and sendable again.
        assert svc.store.get_op(op_id)["state"] == "held"
        d = svc.list_drafts()[0]
        assert d["op_id"] == op_id and d["state"] == "failed" and d["error"] == "wire refused"
        row = next(r for r in outbound_hold.pending("alice", SCOPE, session_id="chat-1") if r["kind"] == "mail")
        assert row["state"] == "failed" and row["error"] == "wire refused"
        # A fresh attempt budget on every approval: five attempts already burned (the sweep's
        # cap) must not make the person's next Send fail before it is tried.
        conn = svc.store._conn()
        conn.execute("UPDATE ops SET attempts=5 WHERE id=?", (op_id,))
        conn.commit()
        monkeypatch.setattr(sender, "send", lambda msg: sender.SendResult(True, "ok"))
        out = release_held_draft(SCOPE, "alice", op_id, service=svc)
        assert out["ok"] is True and out["state"] == "done" and out["delivery"] == "sent", out
        assert svc.list_drafts() == []
        # The reason the last attempt failed went with that attempt: a released op carrying it
        # reports a stale failure on a send that is already on its way.
        # MUTATION: keep `last_error` in approve_op and this goes red.
        assert "last_error" not in (svc.store.get_op(op_id)["payload"] or {})
    finally:
        svc.store.close()


def test_a_mail_handed_to_the_server_and_never_confirmed_is_never_sent_again(world, monkeypatch):
    """The ledger's `ambiguous` stamp is final: SMTP has no idempotency key, so a draft whose
    last attempt may have delivered it comes back to the person WITHOUT a way to send it
    again, from every surface (the approval itself refuses), and only a discard ends it.

    MUTATION: drop the ambiguous check from `approve_draft` and the sender is called a second
    time.
    """
    from vaf.core import outbound_hold
    from vaf.mail.service import AMBIGUOUS_DRAFT, release_held_draft
    calls = []


    def _handed_off(msg):
        calls.append(msg)
        return sender.SendResult(False, "ambiguous", handed_off=True, error="no reply after DATA")

    svc = MailService(SCOPE)
    try:
        q = svc.queue_send(ACCOUNT, "lena@example.org", "Angebot", "Guten Tag", undo_seconds=0, hold=True,
                           sent_by="agent", chat_session_id="chat-1")
        op_id = q["op_id"]
        monkeypatch.setattr(sender, "send", _handed_off)
        out = release_held_draft(SCOPE, "alice", op_id, service=svc)
        assert out["ok"] is False and out["state"] == "ambiguous" and out["delivery"] == "ambiguous"
        # What the person is told is what to DO, and it reads the same on the second attempt,
        # which refuses before it reaches the transport at all. The transport's own words stay
        # on the op, where the row and the log can still show them.
        assert out["error"] == AMBIGUOUS_DRAFT and len(calls) == 1
        assert svc.store.get_op(op_id)["payload"]["last_error"] == "no reply after DATA"
        assert svc.store.get_op(op_id)["state"] == "held"
        assert svc.list_drafts()[0]["state"] == "ambiguous"
        row = next(r for r in outbound_hold.pending("alice", SCOPE, session_id="chat-1") if r["kind"] == "mail")
        assert row["state"] == "ambiguous"
        # No second delivery from any surface: the release refuses before approving, and the
        # approval itself refuses.
        monkeypatch.setattr(sender, "send", lambda msg: calls.append(msg) or sender.SendResult(True, "ok"))
        again = release_held_draft(SCOPE, "alice", op_id, service=svc)
        assert again == {"ok": False, "state": "ambiguous", "error": AMBIGUOUS_DRAFT} and len(calls) == 1
        assert svc.approve_draft(op_id) is False and len(calls) == 1
        assert svc.store.get_op(op_id)["state"] == "held"
        # Only a discard ends it.
        assert svc.discard_draft(op_id) is True and svc.list_drafts() == []
    finally:
        svc.store.close()


def test_the_mail_windows_send_is_the_one_release(world, monkeypatch):
    """`POST /api/mail/drafts/{id}/send` carried its own copy of the two acts (release, drain)
    and parked a send that did not leave out of sight. It calls `release_held_draft` now, so
    the mail window keeps a failed draft the way the card does, and an interrupted one comes
    back without a way to send it again.

    MUTATION: put the route's own approve-and-drain back and the held assertion goes red.
    """
    import vaf.api.mail_routes as mr
    monkeypatch.setattr(sender, "send", lambda msg: sender.SendResult(False, "permanent", error="wire refused"))
    svc = MailService(SCOPE)
    q = svc.queue_send(ACCOUNT, "lena@example.org", "Angebot", "Guten Tag", undo_seconds=0, hold=True, sent_by="front_office")
    svc.store.close()
    out = asyncio.run(mr.send_draft(q["op_id"], _user=USER))
    assert out == {"ok": False, "state": "failed", "delivery": "failed", "error": "wire refused"}
    listed = asyncio.run(mr.list_drafts(thread_id=None, _user=USER))["drafts"]
    assert [d["op_id"] for d in listed] == [q["op_id"]] and listed[0]["state"] == "failed"
    monkeypatch.setattr(sender, "send", lambda msg: sender.SendResult(True, "ok"))
    out = asyncio.run(mr.send_draft(q["op_id"], _user=USER))
    assert out["ok"] is True and out["state"] == "done" and out["delivery"] == "sent"
    with pytest.raises(mr.HTTPException):
        asyncio.run(mr.send_draft(q["op_id"], _user=USER))
