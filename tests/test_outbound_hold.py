# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A send the agent prepares on the person's own chat turn waits for that person.

Live incident: a mail was asked for in the web chat, and it was gone in the
same turn. Nothing stopped it and nothing could: `send_mail` declares `permission_level="write"`
so the confirmation gate (which fires on "dangerous") never sees it, it is not in RISKY_TOOLS,
and it queues with `undo_seconds=0`, so there was no undo window either. Its own
`confirm_high_risk` flag is not a second opinion: the refusal text tells the MODEL to call again
with the flag set.

What is pinned here is the decision and the park, because both are easy to get subtly wrong:

- WHO. Only a call that can reach somebody other than the person is worth a click.
  `send_telegram` and `send_discord` have no recipient parameter at all and resolve the account
  owner's own endpoint inside the tool, so holding one would park a message addressed to the
  very person who would click Approve.
- WHEN. A POSITIVE test on the web chat source. The workflow engine dispatches send steps
  through the same tool funnel with no chat source and nobody watching, so a rule of the shape
  "hold unless this looks like a background run" would park them forever.
- WHAT IS WRITTEN. The parked call is its own row, never a `channel_messages` row: an outbound
  message row opens the channel's reply window, and a message nobody has agreed to send must
  not admit an answer from the recipient.
- WHAT THE MODEL IS TOLD. The result must not read as a delivery, because the automation lane
  counts a delivery by the literal phrase "sent to the user via" and the Front Office lane
  records "the agent asked the owner" for any send result that is not an error.
"""
import json
import time

import pytest

from vaf.core import channel_message_store as store
from vaf.core import outbound_hold
from vaf.core.platform import Platform

SCOPE = "11111111-2222-3333-4444-555555555555"
USER = "alice"


@pytest.fixture
def scratch(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    import vaf.core.config as cfg_mod
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: default))
    monkeypatch.setattr(store, "_local_admin", lambda: "admin")
    monkeypatch.setattr(store, "_local_admin_scope_id", lambda: "admin-scope")
    frames = []
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: frames.append(scope))
    store._reset_announce_state()
    return frames


class _FakeTool:
    """A send tool: records the call, answers like the real one."""

    def __init__(self, answer="Message sent via WhatsApp."):
        self.answer = answer
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


# ---- who is held --------------------------------------------------------------

def test_only_a_call_that_can_reach_somebody_else_is_held():
    """MUTATION: add send_telegram or send_discord to the held tools.

    Their parameter schemas carry no recipient (send_telegram.py, send_discord.py), so the
    message goes to the account owner's own endpoint. Holding one asks the person to approve a
    message to themselves, and it would do that on every proactive notification.
    """
    web = dict(source="web", config_get=lambda k, d: True)
    assert outbound_hold.holds_outward_send("send_mail", {"to": "a@b.c"}, **web) is True
    assert outbound_hold.holds_outward_send("reply_mail", {"message_id": 3}, **web) is True
    assert outbound_hold.holds_outward_send("forward_mail", {"to": "a@b.c"}, **web) is True
    assert outbound_hold.holds_outward_send("send_whatsapp", {"to_phone": "+491700000000",
                                                              "message": "hi"}, **web) is True

    assert outbound_hold.holds_outward_send("send_whatsapp", {"message": "hi"}, **web) is False
    assert outbound_hold.holds_outward_send("send_telegram", {"message": "hi"}, **web) is False
    assert outbound_hold.holds_outward_send("send_discord", {"message": "hi"}, **web) is False
    assert outbound_hold.holds_outward_send("send_slack", {"message": "hi"}, **web) is False
    assert outbound_hold.holds_outward_send("send_to_user", {"message": "hi"}, **web) is False
    assert outbound_hold.holds_outward_send("memory_save", {"content": "x"}, **web) is False


def test_only_the_web_chat_holds():
    """MUTATION: turn the source test into "not a background run".

    The workflow engine dispatches its send steps through the same tool funnel with no chat
    source at all, so a negative test parks them and nobody is there to click. Channel turns
    and the Front Office lane answer strangers; a voice call is a person, but not a person
    looking at a card.
    """
    args = {"to": "a@b.c", "subject": "s", "body": "b"}
    for source in ("telegram", "whatsapp", "discord", "email", "voice_call", "", None, "automation"):
        assert outbound_hold.holds_outward_send(
            "send_mail", args, source=source, config_get=lambda k, d: True) is False, source
    assert outbound_hold.holds_outward_send(
        "send_mail", args, source="WEB", config_get=lambda k, d: True) is True


def test_the_switch_turns_it_off():
    args = {"to": "a@b.c"}
    assert outbound_hold.holds_outward_send(
        "send_mail", args, source="web", config_get=lambda k, d: False) is False
    # A config that raises is not a reason to hold or to send: the default decides.
    def _boom(key, default):
        raise RuntimeError("no config")
    assert outbound_hold.holds_outward_send(
        "send_mail", args, source="web", config_get=_boom) is True


# ---- what the model is told ---------------------------------------------------

def test_the_result_does_not_read_as_a_delivery():
    """MUTATION: phrase the held result like a send ("Message sent to ...").

    vaf/core/automation.py counts a delivery by the literal substring "sent to the user via"
    and by the "Message and document" prefix, and vaf/core/agent.py records the agent's
    question to the owner for any send result that is not an error. A result that reads as a
    send would make an automation suppress its own push and a Front Office chat claim the owner
    was asked.
    """
    text = outbound_hold.held_result("send_whatsapp", {"to_phone": "+49170", "message": "hi"},
                                     entry_id=7)
    assert text.startswith(outbound_hold.HELD_PREFIX)
    assert "sent to the user via" not in text
    assert not text.startswith("Message and document")
    assert "+49170" in text and "7" in text
    assert "do NOT claim it was sent" in text
    # Not an error either: the model did its work, the person decides.
    from vaf.core.context import tool_result_is_error
    assert tool_result_is_error(text) is False


def test_the_preview_is_what_the_person_reads():
    p = outbound_hold.preview_of("send_whatsapp", {"to_phone": "+4917", "message": "Hallo",
                                                   "voice_lang": "de", "file_path": "/x.pdf"})
    assert p == {"recipient": "+4917", "subject": "voice message, attachment", "body": "Hallo"}
    p = outbound_hold.preview_of("send_mail", {"to": "a@b.c", "subject": "Angebot", "body": "Text"})
    assert p == {"recipient": "a@b.c", "subject": "Angebot", "body": "Text"}


# ---- the park -----------------------------------------------------------------

def test_a_parked_call_is_not_a_message(scratch):
    """MUTATION: park the call by writing an outbound row into channel_messages.

    `last_message_ts` reads direction='out' rows whatever their content type, and that is what
    opens the 72 hour reply window. A message nobody has agreed to send must not admit an
    answer from the recipient, so the park is its own table.
    """
    args = {"to_phone": "+491700000000", "message": "Hallo Uwe"}
    entry_id = outbound_hold.park_messenger_call("send_whatsapp", args, username=USER,
                                                 user_scope_id=SCOPE, session_id="s1")
    assert entry_id > 0
    assert store.last_message_ts(USER, "+491700000000", user_scope_id=SCOPE) is None
    assert store.get_chat_messages(USER, "+491700000000", user_scope_id=SCOPE) == []
    assert scratch, "the person's browsers are told a draft is waiting"


def test_pending_lists_the_park_newest_first(scratch):
    outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+1", "message": "one"},
                                      username=USER, user_scope_id=SCOPE)
    second = outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+2", "message": "two"},
                                               username=USER, user_scope_id=SCOPE)
    rows = outbound_hold.pending(USER, SCOPE)
    assert [r["recipient"] for r in rows] == ["+2", "+1"]
    assert rows[0]["id"] == second
    assert rows[0]["kind"] == "call" and rows[0]["channel"] == "whatsapp"
    assert rows[0]["preview"] == "two"
    # Another identity in the same store file sees none of it.
    assert outbound_hold.pending("bob", SCOPE) == []


def test_approving_runs_the_call_with_the_approvers_identity(scratch):
    """MUTATION: pass the parked row's identity through instead of the approver's.

    A tool that declares `file_access` installs its jail from user_scope_id and user_role, so a
    stale pair opens the wrong jail or none. An approval must also not be able to carry a
    privilege the approving session does not have.
    """
    args = {"to_phone": "+491700000000", "message": "Hallo", "user_scope_id": "STALE",
            "username": "mallory", "user_role": "admin"}
    entry_id = outbound_hold.park_messenger_call("send_whatsapp", args, username=USER,
                                                 user_scope_id=SCOPE)
    tool = _FakeTool()
    out = outbound_hold.approve_call(entry_id, username=USER, user_scope_id=SCOPE,
                                     user_role="user", tools={"send_whatsapp": tool})
    assert out["ok"] is True
    assert len(tool.calls) == 1
    call = tool.calls[0]
    assert call["to_phone"] == "+491700000000" and call["message"] == "Hallo"
    assert call["user_scope_id"] == SCOPE and call["username"] == USER and call["user_role"] == "user"
    assert outbound_hold.pending(USER, SCOPE) == []


def test_a_second_click_cannot_send_twice(scratch):
    """MUTATION: settle the row after the send instead of claiming it before.

    Two clicks on one card, or a click racing a CLI approval, must not put the same message on
    the wire twice. The claim is the mail outbox's own shape: the state guard is in the WHERE.
    """
    entry_id = outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+1", "message": "x"},
                                                 username=USER, user_scope_id=SCOPE)
    tool = _FakeTool()
    first = outbound_hold.approve_call(entry_id, username=USER, user_scope_id=SCOPE,
                                       user_role="user", tools={"send_whatsapp": tool})
    second = outbound_hold.approve_call(entry_id, username=USER, user_scope_id=SCOPE,
                                        user_role="user", tools={"send_whatsapp": tool})
    assert first["ok"] is True and second["ok"] is False
    assert len(tool.calls) == 1
    assert "not waiting" in second["result"]


def test_a_send_that_did_not_say_it_left_keeps_the_draft(scratch):
    """The draft is consumed only on a result that SAYS the message left.

    MUTATION: classify by failure prose again (`not result.startswith(("failed", "error", ...))`)
    and every line of this test goes red, because not one of these real refusals begins with
    such a word. They are the WhatsApp tool's actual returns, and the first shape of this rule
    called all of them a success: the bridge was down, the card reported "sent", and the draft
    was gone with the message never written.
    """
    for answer in ("WhatsApp bridge is not running. Start it in Settings.",
                   "WhatsApp could not deliver the message: timeout",
                   "No delivery confirmation from the WhatsApp bridge within the time limit.",
                   "Message was blocked (contained internal system content).",
                   "Access denied: outside your own data",
                   "[TOOL BLOCKED] You are handling a contact's message.",
                   RuntimeError("boom")):
        entry_id = outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+1", "message": "x"},
                                                     username=USER, user_scope_id=SCOPE)
        tool = _FakeTool(answer=answer)
        out = outbound_hold.approve_call(entry_id, username=USER, user_scope_id=SCOPE,
                                         user_role="user", tools={"send_whatsapp": tool})
        assert out["ok"] is False, answer
        row = store.held_send(entry_id, USER, SCOPE)
        assert row["state"] == "failed", answer
        assert str(row["error"]).strip(), "the reason rides on the draft"
        # It is listed for the person with the reason, and it is still theirs to send or drop.
        listed = [r for r in outbound_hold.pending(USER, SCOPE) if r["id"] == entry_id]
        assert listed and listed[0]["state"] == "failed" and listed[0]["error"], answer
        assert outbound_hold.discard_call(entry_id, username=USER, user_scope_id=SCOPE), answer

    # A second attempt on a failed draft is allowed, and a real delivery ends it.
    entry_id = outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+1", "message": "x"},
                                                 username=USER, user_scope_id=SCOPE)
    outbound_hold.approve_call(entry_id, username=USER, user_scope_id=SCOPE, user_role="user",
                               tools={"send_whatsapp": _FakeTool(answer="WhatsApp bridge is not running.")})
    again = outbound_hold.approve_call(entry_id, username=USER, user_scope_id=SCOPE, user_role="user",
                                       tools={"send_whatsapp": _FakeTool(answer="Message sent via WhatsApp.")})
    assert again["ok"] is True
    assert store.held_send(entry_id, USER, SCOPE)["state"] == "sent"
    assert outbound_hold.pending(USER, SCOPE) == [], "a sent draft leaves the list"


def test_a_draft_stranded_mid_send_is_never_repeated_by_a_click(scratch):
    """A worker killed between the claim and the answer left the row in `sending`: invisible
    to the listing, unreachable by discard, and silently stuck for good.

    It comes back as AMBIGUOUS, not failed: the message was handed to the bridge and nobody
    wrote down what happened, so it may have arrived. A messenger send has no idempotency key,
    so the one thing the person must not be offered is a one-click repeat.

    MUTATION: drop the reclaim call from `pending()` and the listing assertion goes red; park
    the row as "failed" instead and the send assertion goes red, because a failed draft is
    sendable again by design.
    """
    entry_id = outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+1", "message": "x"},
                                                 username=USER, user_scope_id=SCOPE)
    assert store.settle_held_send(entry_id, USER, "sending", SCOPE), "claimed by an approval"
    assert outbound_hold.pending(USER, SCOPE) == [], "a claim in flight is nobody's to touch"
    # Still nothing while the lease runs; the clock is an argument, so no test sleeps.
    assert store.reclaim_stranded_held_sends(USER, SCOPE, lease_seconds=300) == 0
    assert store.reclaim_stranded_held_sends(USER, SCOPE, lease_seconds=300,
                                             now=time.time() + 301) == 1
    row = store.held_send(entry_id, USER, SCOPE)
    assert row["state"] == "ambiguous" and "may already have been sent" in row["error"]
    listed = outbound_hold.pending(USER, SCOPE)
    assert len(listed) == 1 and listed[0]["id"] == entry_id and listed[0]["state"] == "ambiguous"

    # Send is refused with the reason, and the tool is never touched.
    tool = _FakeTool(answer="Message sent via WhatsApp.")
    out = outbound_hold.approve_call(entry_id, username=USER, user_scope_id=SCOPE,
                                     user_role="user", tools={"send_whatsapp": tool})
    assert out["ok"] is False and "may already have been sent" in out["result"]
    assert tool.calls == [], "a second delivery is not the machine's call to make"
    assert store.held_send(entry_id, USER, SCOPE)["state"] == "ambiguous"
    # Dropping it is the person's own answer, and it works.
    assert outbound_hold.discard_call(entry_id, username=USER, user_scope_id=SCOPE)
    assert outbound_hold.pending(USER, SCOPE) == []


def test_discarding_drops_it_once(scratch):
    entry_id = outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+1", "message": "x"},
                                                 username=USER, user_scope_id=SCOPE)
    assert outbound_hold.discard_call(entry_id, username=USER, user_scope_id=SCOPE) is True
    assert outbound_hold.discard_call(entry_id, username=USER, user_scope_id=SCOPE) is False
    assert outbound_hold.pending(USER, SCOPE) == []
    # A discarded draft cannot be sent afterwards.
    tool = _FakeTool()
    out = outbound_hold.approve_call(entry_id, username=USER, user_scope_id=SCOPE,
                                     user_role="user", tools={"send_whatsapp": tool})
    assert out["ok"] is False and tool.calls == []


def test_an_unresolvable_tool_does_not_consume_the_draft(scratch):
    """A draft parked by a version that had a tool this one does not: the row must survive the
    click, not disappear into an approval that could never run."""
    entry_id = store.park_held_send(USER, "pigeon", "send_pigeon", '{"to": "x"}',
                                    user_scope_id=SCOPE, recipient="x", preview="hi")
    out = outbound_hold.approve_call(entry_id, username=USER, user_scope_id=SCOPE,
                                     user_role="user", tools={})
    assert out["ok"] is False and "not available" in out["result"]
    assert store.held_send(entry_id, USER, SCOPE)["state"] == "held"


def test_the_resolver_opens_no_back_door(scratch):
    """MUTATION: let `resolve_tool` import any tool by name.

    The approval surfaces have no Agent, so the tool is resolved by name - which is a dispatch
    path reachable from an HTTP route. Only what this module can park is resolvable, so a
    parked row can never name its way into another tool.
    """
    assert outbound_hold.resolve_tool("send_whatsapp") is not None
    for name in ("host_bash", "python_exec", "write_file", "send_mail", "", "vaf.tools.host_bash"):
        assert outbound_hold.resolve_tool(name) is None, name


def test_the_parked_arguments_survive_verbatim(scratch):
    args = {"to_phone": "+49170", "message": "Zeile 1\nZeile 2 mit Umlauten: äöü",
            "voice_lang": "de"}
    entry_id = outbound_hold.park_messenger_call("send_whatsapp", dict(args), username=USER,
                                                 user_scope_id=SCOPE)
    row = store.held_send(entry_id, USER, SCOPE)
    assert json.loads(row["args"]) == args


# ---- the two lanes in one listing ---------------------------------------------

def test_a_held_mail_and_a_parked_call_sort_into_one_listing(scratch, monkeypatch):
    """MUTATION: read the mail draft's `created_at` as a float.

    The two halves stamp different clocks: a parked call writes `time.time()`, the mail outbox
    writes an ISO string (vaf/mail/store._now). Reading one as the other raises, and because
    the listing is fail-open per source, the whole mail half would vanish from the card with
    nothing said - the exact silence this round exists to remove.
    """
    from datetime import datetime, timezone

    class _Svc:
        def __init__(self, scope):
            self.scope = scope

        def list_drafts(self):
            return [{"op_id": 12, "to": "uwe@example.com", "cc": "cc@example.com", "bcc": "bcc@example.com",
                     "subject": "Angebot", "body": "Guten Tag", "thread_id": None,
                     "attachments": ["report.pdf"], "state": "failed", "error": "wire refused",
                     "created_at": datetime.now(timezone.utc).isoformat()}]

    import vaf.mail.service as svc_mod
    monkeypatch.setattr(svc_mod, "MailService", _Svc)

    outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+1", "message": "later",
                                                        "file_path": "/home/user/out/voice note.ogg"},
                                      username=USER, user_scope_id=SCOPE)
    rows = outbound_hold.pending(USER, SCOPE)
    kinds = {r["kind"] for r in rows}
    assert kinds == {"call", "mail"}, rows
    mail = next(r for r in rows if r["kind"] == "mail")
    assert mail["id"] == 12 and mail["channel"] == "mail" and mail["subject"] == "Angebot"
    assert mail["created_ts"] > 0
    # A threadless draft is in the listing: it is exactly what send_mail parks, and the two
    # older draft projections drop it (they keep only drafts with a thread_id).
    assert mail["recipient"] == "uwe@example.com"
    # Every address and every file ride on the row, and so does the draft's own state: what
    # the person approves is what leaves, and a mail that did not leave says why.
    # MUTATION: drop cc, bcc, attachments or state from either row shape and this goes red.
    assert (mail["cc"], mail["bcc"], mail["attachments"]) == ("cc@example.com", "bcc@example.com", ["report.pdf"])
    assert (mail["state"], mail["error"]) == ("failed", "wire refused")
    call = next(r for r in rows if r["kind"] == "call")
    assert (call["cc"], call["bcc"], call["attachments"]) == ("", "", ["voice note.ogg"]), "the name, never the path"


def test_the_epoch_helper_takes_both_clocks():
    from vaf.core.outbound_hold import _epoch

    assert _epoch(1758200000.0) == 1758200000.0
    assert _epoch("2026-09-18T16:29:45.956952+00:00") > 0
    assert _epoch("") == 0.0
    assert _epoch(None) == 0.0
    assert _epoch("not a date") == 0.0


# ---- nobody is at the screen ---------------------------------------------------

def test_a_scheduled_turn_sends_even_though_its_source_is_the_web():
    """MUTATION: drop the `unattended` condition.

    A timer the person set in the browser fires later, on a clock, and the queue carries the
    source of the chat it belongs to - "web". Judged by the source alone, a message they
    SCHEDULED for eight o'clock would sit as a draft until they happened to look, which is the
    opposite of what they asked for. Compaction turns are marked the same way.
    """
    args = {"to": "uwe@example.com", "subject": "Reminder", "body": "text"}
    assert outbound_hold.holds_outward_send(
        "send_mail", args, source="web", unattended=True, config_get=lambda k, d: True) is False
    assert outbound_hold.holds_outward_send(
        "send_whatsapp", {"to_phone": "+1", "message": "x"},
        source="web", unattended=True, config_get=lambda k, d: True) is False
    # The person's own live turn is untouched by that exception.
    assert outbound_hold.holds_outward_send(
        "send_mail", args, source="web", unattended=False, config_get=lambda k, d: True) is True


def test_the_runner_marks_the_unattended_turn_where_it_names_the_source():
    """MUTATION: mark the turn somewhere else, or not at all.

    The runner is the only place that holds the task and the agent at the same moment, and the
    two facts belong together: what the turn's surface is, and whether anybody is on it.
    """
    from pathlib import Path

    runner = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")
    region = runner.split('agent._current_chat_source = getattr(task, "source", "web")', 1)[1][:900]
    # Only a TIMER is unattended - the person's own scheduled order. A finished background
    # command is not an order to send, so what follows it stays behind the hold.
    assert 'agent._unattended_turn = bool(_wake_kind(_meta) == "timer" or _meta.get("compaction"))' in region


# ---- one chat, one card --------------------------------------------------------

def test_a_draft_belongs_to_the_chat_that_asked_for_it(scratch):
    """MUTATION: drop the session filter in `pending`.

    A person who switches chats while the agent is still writing must not find that message
    hanging in the conversation they moved to. The card asks for its own chat; the chat that
    holds the draft carries the sidebar's red dot until they go back to it.
    """
    here = outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+1", "message": "here"},
                                             username=USER, user_scope_id=SCOPE, session_id="chat-a")
    there = outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+2", "message": "there"},
                                              username=USER, user_scope_id=SCOPE, session_id="chat-b")
    a = outbound_hold.pending(USER, SCOPE, session_id="chat-a")
    b = outbound_hold.pending(USER, SCOPE, session_id="chat-b")
    assert [r["id"] for r in a] == [here]
    assert [r["id"] for r in b] == [there]
    # A chat with nothing waiting shows nothing, and the whole list is still the whole list
    # (the CLI and any overview want that one).
    assert outbound_hold.pending(USER, SCOPE, session_id="chat-c") == []
    assert len(outbound_hold.pending(USER, SCOPE)) == 2


def test_a_draft_that_belongs_to_no_chat_stays_out_of_every_chat(scratch, monkeypatch):
    """A Front Office answer is held in the same outbox but was never ordered in a
    conversation. It belongs to the inbox and the mail window; showing it inside somebody's
    chat would be the same mix-up in the other direction."""
    from datetime import datetime, timezone

    class _Svc:
        def __init__(self, scope):
            pass

        def list_drafts(self):
            return [{"op_id": 5, "to": "stranger@example.com", "subject": "Re: Anfrage",
                     "body": "Danke", "thread_id": 3, "chat_session_id": "",
                     "created_at": datetime.now(timezone.utc).isoformat()},
                    {"op_id": 6, "to": "uwe@example.com", "subject": "Angebot", "body": "Text",
                     "thread_id": None, "chat_session_id": "chat-a",
                     "created_at": datetime.now(timezone.utc).isoformat()}]

    import vaf.mail.service as svc_mod
    monkeypatch.setattr(svc_mod, "MailService", _Svc)
    in_chat = outbound_hold.pending(USER, SCOPE, session_id="chat-a")
    assert [r["id"] for r in in_chat] == [6]
    assert len(outbound_hold.pending(USER, SCOPE)) == 2, "the inbox still sees both"


# ---- the draft has a name, new words, a successor and an outcome ---------------

def test_the_result_names_its_draft():
    """MUTATION: drop the ref from `held_result`.

    The chat finds the card's place under the turn that wrote it by this ref, and the next
    turn's note finds the draft again by it. The two lanes number independently, so the ref
    carries the lane: a bare "draft 7" is two different drafts.
    """
    mail = outbound_hold.held_result("send_mail", {"to": "a@b.c"}, entry_id=12)
    call = outbound_hold.held_result("send_whatsapp", {"to_phone": "+49170", "message": "x"}, entry_id=7)
    assert mail.startswith(f"{outbound_hold.HELD_PREFIX} Draft mail:12 ")
    assert call.startswith(f"{outbound_hold.HELD_PREFIX} Draft call:7 ")
    assert outbound_hold.created_refs(mail + "\n" + call) == ["mail:12", "call:7"]
    assert "Your turn ends here" in call, "the model is told the turn is over, not to announce it"
    assert outbound_hold.parse_ref("mail:12") == ("mail", 12)
    assert outbound_hold.parse_ref("pigeon:1") is None and outbound_hold.parse_ref("call:x") is None


def test_a_parked_call_takes_new_words_until_it_is_sent(scratch):
    """MUTATION: write the new text into the preview only (or the args only).

    What the person reads and what the tool is called with are the same text, or the edit is
    a card that lies: the approval replays the parked ARGUMENTS.
    """
    entry_id = outbound_hold.park_messenger_call(
        "send_whatsapp", {"to_phone": "+491700000000", "message": "Hallo", "voice_lang": "de"},
        username=USER, user_scope_id=SCOPE, session_id="chat-a")
    out = outbound_hold.revise_draft("call", entry_id, username=USER, user_scope_id=SCOPE,
                                     body="Hallo Uwe, bis morgen!")
    assert out == {"ok": True, "error": ""}
    row = store.held_send(entry_id, USER, SCOPE)
    assert row["preview"] == "Hallo Uwe, bis morgen!" and row["edited"] == 1
    assert json.loads(row["args"]) == {"to_phone": "+491700000000", "message": "Hallo Uwe, bis morgen!",
                                       "voice_lang": "de"}, "only the text changes"
    assert outbound_hold.revise_draft("call", entry_id, username=USER, user_scope_id=SCOPE,
                                      body="   ") == {"ok": False, "error": "empty"}
    assert outbound_hold.revise_draft("call", entry_id, username="bob", user_scope_id=SCOPE,
                                      body="x")["ok"] is False, "somebody else's draft"

    tool = _FakeTool()
    sent = outbound_hold.send_draft("call", entry_id, username=USER, user_scope_id=SCOPE,
                                    tools={"send_whatsapp": tool}, wake=False)
    assert sent["ok"] is True and tool.calls[0]["message"] == "Hallo Uwe, bis morgen!"
    assert outbound_hold.revise_draft("call", entry_id, username=USER, user_scope_id=SCOPE,
                                      body="zu spät") == {"ok": False, "error": "not waiting"}


def test_a_newer_draft_to_the_same_person_replaces_the_waiting_one(scratch):
    """MUTATION: drop the recipient comparison, or the `keep` of the round.

    Asked in words to change a draft, the agent writes it again; the old card must not stay
    beside the new one, sendable. Everything else stays: another person, another channel,
    another chat, a draft of the SAME round (two messages meant together) and one whose send
    may already have arrived.
    """
    def park(to, msg, session="chat-a"):
        return outbound_hold.park_messenger_call(
            "send_whatsapp", {"to_phone": to, "message": msg},
            username=USER, user_scope_id=SCOPE, session_id=session)

    old = park("+49 170 0000000", "Erste Fassung")
    other_person = park("+491711111111", "Andere Person")
    other_chat = park("+491700000000", "Anderer Chat", session="chat-b")
    same_round = park("+491700000000", "Zweite Nachricht derselben Runde")
    stranded = park("+491700000000", "Vielleicht schon draussen")
    store.settle_held_send(stranded, USER, "ambiguous", SCOPE)
    new = park("+491700000000", "Neue Fassung")

    replaced = outbound_hold.replace_older_drafts(
        f"call:{new}", session_id="chat-a", username=USER, user_scope_id=SCOPE,
        keep=[f"call:{same_round}", f"call:{new}"])
    assert replaced == [f"call:{old}"]
    row = store.held_send(old, USER, SCOPE)
    assert (row["state"], row["replaced_by"]) == ("replaced", f"call:{new}")
    for kept in (other_person, other_chat, same_round, new):
        assert store.held_send(kept, USER, SCOPE)["state"] == "held", kept
    assert store.held_send(stranded, USER, SCOPE)["state"] == "ambiguous"
    # A replaced draft cannot be sent any more, and it is not in the terminal's list.
    tool = _FakeTool()
    out = outbound_hold.send_draft("call", old, username=USER, user_scope_id=SCOPE,
                                   tools={"send_whatsapp": tool}, wake=False)
    assert out["ok"] is False and tool.calls == []
    assert old not in [r["id"] for r in outbound_hold.pending(USER, SCOPE)]


def test_the_chat_listing_keeps_what_was_decided(scratch):
    """The card stays in the conversation after the decision, as the record of what happened
    to the draft. The terminal and the inbox list only what still waits."""
    waiting = outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+1", "message": "a"},
                                                username=USER, user_scope_id=SCOPE, session_id="chat-a")
    dropped = outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+2", "message": "b"},
                                                username=USER, user_scope_id=SCOPE, session_id="chat-a")
    outbound_hold.discard_draft("call", dropped, username=USER, user_scope_id=SCOPE)
    outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+3", "message": "c"},
                                      username=USER, user_scope_id=SCOPE, session_id="chat-b")
    rows = outbound_hold.chat_drafts(USER, SCOPE, "chat-a")
    assert [(r["ref"], r["state"]) for r in rows] == [(f"call:{dropped}", "discarded"),
                                                      (f"call:{waiting}", "held")]
    assert [r["id"] for r in outbound_hold.pending(USER, SCOPE, session_id="chat-a")] == [waiting]
    assert outbound_hold.chat_drafts(USER, SCOPE, "") == []


def test_a_decided_draft_is_reported_once(scratch):
    """MUTATION: report every created draft (drop the `reported` set), or report one still
    waiting.

    The history is the ledger: a draft the agent created there and nothing later reports is
    looked up, and the note carries the very shape that marks it reported, so the next turn
    stays quiet about it.
    """
    sent = outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+1", "message": "a"},
                                             username=USER, user_scope_id=SCOPE, session_id="chat-a")
    dropped = outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+2", "message": "b"},
                                                username=USER, user_scope_id=SCOPE, session_id="chat-a")
    waiting = outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+3", "message": "c"},
                                                username=USER, user_scope_id=SCOPE, session_id="chat-a")
    history = [outbound_hold.held_result("send_whatsapp", {"to_phone": p}, entry_id=i)
               for p, i in (("+1", sent), ("+2", dropped), ("+3", waiting))]
    outbound_hold.send_draft("call", sent, username=USER, user_scope_id=SCOPE,
                             tools={"send_whatsapp": _FakeTool()}, wake=False)
    outbound_hold.discard_draft("call", dropped, username=USER, user_scope_id=SCOPE)

    note = outbound_hold.decision_notes(history, username=USER, user_scope_id=SCOPE)
    assert note.startswith("[Context:")
    assert f"Draft call:{sent} was SENT by the user" in note
    assert f"Draft call:{dropped} was DISCARDED by the user" in note
    assert f"call:{waiting}" not in note, "a draft still waiting is not news"
    assert outbound_hold.decision_notes(history + [note], username=USER, user_scope_id=SCOPE) == ""
    assert outbound_hold.decision_notes(["no drafts here"], username=USER, user_scope_id=SCOPE) == ""


def test_a_send_wakes_the_chat_once_nothing_else_waits(scratch, monkeypatch):
    """MUTATION: wake on every send, or on a discard.

    Two drafts from one turn wake the chat once, after the second decision. A discard wakes
    nothing (the person said stop). The terminal passes wake=False: its process has no queue
    anybody drains."""
    import vaf.core.task_queue as tq
    woken = []
    monkeypatch.setattr(tq, "enqueue_wake_turn", lambda **kw: woken.append(kw))
    first = outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+1", "message": "a"},
                                              username=USER, user_scope_id=SCOPE, session_id="chat-a")
    second = outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+2", "message": "b"},
                                               username=USER, user_scope_id=SCOPE, session_id="chat-a")
    tools = {"send_whatsapp": _FakeTool()}
    outbound_hold.send_draft("call", first, username=USER, user_scope_id=SCOPE, user_role="user", tools=tools)
    assert woken == [], "the other draft of the chat still waits"
    outbound_hold.revise_draft("call", second, username=USER, user_scope_id=SCOPE, body="b, geändert")
    outbound_hold.send_draft("call", second, username=USER, user_scope_id=SCOPE, user_role="user", tools=tools)
    assert len(woken) == 1
    wake = woken[0]
    assert (wake["kind"], wake["session_id"], wake["source"]) == ("draft", "chat-a", "web")
    assert (wake["username"], wake["user_scope_id"], wake["role"]) == (USER, SCOPE, "user")
    assert wake["text"].startswith(outbound_hold.DRAFT_WAKE_PREFIX)
    assert f"Draft call:{second} was SENT by the user after changing its text" in wake["text"]
    assert "b, geändert" in wake["text"], "the agent learns what actually left"
    assert wake["extra"] == {"draft": f"call:{second}"}

    third = outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+3", "message": "c"},
                                              username=USER, user_scope_id=SCOPE, session_id="chat-a")
    outbound_hold.discard_draft("call", third, username=USER, user_scope_id=SCOPE)
    fourth = outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+4", "message": "d"},
                                               username=USER, user_scope_id=SCOPE, session_id="chat-a")
    outbound_hold.send_draft("call", fourth, username=USER, user_scope_id=SCOPE, tools=tools, wake=False)
    assert len(woken) == 1, "a discard and a terminal send wake nothing"


def test_the_recipient_is_named_from_the_contact_book(scratch, monkeypatch):
    """The card and the agent name the person, not only the number: the contact book first,
    then a mail's own display name. A name is a convenience, so a failing lookup is ""."""
    from vaf.core import contacts_store
    monkeypatch.setattr(contacts_store, "get_contact_name_by_phone",
                        lambda phone, username=None, user_scope_id=None: "Uwe Berg" if phone == "+49170" else None)
    monkeypatch.setattr(contacts_store, "find_contact_by_channel",
                        lambda ch, value, username=None, user_scope_id=None:
                        {"name": "Anna Berg"} if value == "anna@example.com" else None)
    assert outbound_hold.recipient_name("whatsapp", "+49170", USER, SCOPE) == "Uwe Berg"
    assert outbound_hold.recipient_name("whatsapp", "+49999", USER, SCOPE) == ""
    assert outbound_hold.recipient_name("mail", "anna@example.com, b@example.com", USER, SCOPE) == "Anna Berg"
    assert outbound_hold.recipient_name("mail", "Carl Kranz <carl@example.com>", USER, SCOPE) == "Carl Kranz"
    outbound_hold.park_messenger_call("send_whatsapp", {"to_phone": "+49170", "message": "x"},
                                      username=USER, user_scope_id=SCOPE, session_id="chat-a")
    assert outbound_hold.chat_drafts(USER, SCOPE, "chat-a")[0]["recipient_name"] == "Uwe Berg"

    def _boom(*a, **k):
        raise RuntimeError("contacts locked")
    monkeypatch.setattr(contacts_store, "get_contact_name_by_phone", _boom)
    assert outbound_hold.recipient_name("whatsapp", "+49170", USER, SCOPE) == ""
