# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The agent's `inbox` tool (vaf/tools/inbox.py): one listing across every channel, the same
rows the person sees, with the next-step hint a weak model needs and the mail IDs read_mail
needs. It replaced whatsapp_inbox, telegram_inbox, discord_inbox and mail_inbox.

MUTATION: drop the hint and the first test goes red; skip the phishing filter and the mail
test goes red; ask the bridge and the last test goes red.
"""
from pathlib import Path

import pytest

from vaf.tools.inbox import InboxTool

TOOLS = Path(__file__).resolve().parent.parent / "vaf" / "tools"


def _row(channel, id_, **over):
    row = {"key": f"{channel}:{id_}", "channel": channel, "id": id_, "name": f"name-{id_}", "preview": "hello",
           "preview_from": "them", "last_ts": 1_700_000_000.0, "message_count": 3, "unread": 1, "waits": True,
           "waits_reason": "unanswered", "answered_by_agent": False, "done": False, "is_group": False,
           "mode": "readonly", "reply_window_until": None, "can_compose": False, "session_id": "",
           "jump": {"channel": channel, "chat_id": id_}}
    row.update(over)
    return row


@pytest.fixture
def listing(monkeypatch):
    seen = {}

    def fake(username, user_scope_id, **kw):
        seen["username"], seen["scope"], seen["kw"] = username, user_scope_id, kw
        rows = seen.get("rows", [])
        # The core narrows the mail lane at the source (the store's thread-level folder rule);
        # the fake approximates it on its rows by the newest message's folder.
        if kw.get("mail_account_id"):
            rows = [r for r in rows if r["channel"] != "mail" or (r.get("jump") or {}).get("account_id") == kw["mail_account_id"]]
        if kw.get("mail_folder"):
            rows = [r for r in rows if r["channel"] != "mail" or (r.get("jump") or {}).get("folder") == kw["mail_folder"]]
        stored = {c: sum(1 for r in seen.get("rows", []) if r["channel"] == c) for c in ("whatsapp", "telegram", "discord", "mail", "room")}
        if kw.get("channels"):
            rows = [r for r in rows if r["channel"] in kw["channels"]]
        if kw.get("view") == "waits":
            rows = [r for r in rows if r["waits"]]
        return {"rows": rows[: kw.get("limit") or 200],
                "counts": {"all": len(rows), "waits": sum(1 for r in rows if r["waits"]),
                           "unread": sum(r["unread"] for r in rows), "agent": 0, "stored_per_channel": stored,
                           "bulk_hidden": 0 if kw.get("include_bulk") else int(seen.get("bulk_hidden", 0))},
                "channels": kw.get("channels")}
    monkeypatch.setattr("vaf.core.inbox.list_conversations", fake)
    monkeypatch.setattr("vaf.tools.mail_utils.filter_phishing_messages_for_agent", lambda ms: (ms, 0))
    monkeypatch.setattr("vaf.core.telegram_history.sync_telegram_history", lambda *a, **k: 0)
    monkeypatch.setattr("vaf.core.discord_history.sync_discord_history", lambda *a, **k: 0)
    return seen


def test_every_lane_renders_and_names_its_read_tool(listing):
    listing["rows"] = [
        _row("whatsapp", "+491700000042", mode="contact"),
        _row("telegram", "7", mode="owner", unread=0, waits=False, answered_by_agent=True, preview_from="agent"),
        _row("mail", "9", subject="Vertrag Q4", name="Lena <lena@example.com>",
             jump={"channel": "mail", "thread_id": "9", "account_id": "a@x", "folder": "INBOX", "message_id": "<q@x>",
                   "provider_message_id": "18f2"}),
        _row("room", "r1", name="Phoenix", is_group=True, mode="room", waits_reason="invitation"),
    ]
    out = InboxTool().run(username="alice", user_scope_id="s")
    head = out.split("\n\n", 1)[0]
    for tool in ("read_whatsapp_chat", "read_telegram_chat", "read_discord_chat", "read_mail", "room_read", "find_mail"):
        assert tool in head, tool
    assert "Do NOT call inbox again" in head
    assert "Inbox: 4 conversations, 3 wait for you, 3 unread" in out
    assert "[WhatsApp] name-+491700000042 | chat_id=+491700000042" in out and "WAITS FOR YOU (unanswered)" in out
    assert "Front Office" in out and "1 unread" in out
    assert "[Telegram] name-7 | chat_id=7" in out and "agent answered" in out and "your own chat" in out
    assert "[Mail] Lena <lena@example.com> | subject: Vertrag Q4" in out
    assert "  3: account_id=a@x message_id='<q@x>' provider_message_id=18f2 folder=INBOX" in out
    assert "[Room] Phoenix | room_id=r1" in out and "WAITS FOR YOU (invitation)" in out
    assert "Discord: no stored chats yet" in out, "an empty lane says so instead of vanishing"
    assert listing["kw"]["channels"] == ["whatsapp", "telegram", "discord", "mail", "room"]


def test_channel_view_and_toggles_pass_through_and_mail_narrows_by_account_and_folder(listing):
    listing["rows"] = [
        _row("mail", "1", jump={"channel": "mail", "thread_id": "1", "account_id": "a@x", "folder": "INBOX"}),
        _row("mail", "2", jump={"channel": "mail", "thread_id": "2", "account_id": "b@x", "folder": "Sent"}),
    ]
    out = InboxTool().run(username="alice", user_scope_id="s", channel="mail", view="waits", max_chats="15",
                          query="vertrag", include_groups=False, include_done=True, account_id="a@x")
    assert listing["kw"] == {"channels": ["mail"], "view": "waits", "include_groups": False, "include_done": True,
                             "include_bulk": False, "query": "vertrag", "limit": 15, "mail_account_id": "a@x", "mail_folder": None}
    InboxTool().run(username="alice", user_scope_id="s", channel="mail", include_bulk=True)
    assert listing["kw"]["include_bulk"] is True, "bulk mail only when asked"
    listing["bulk_hidden"] = 3
    assert "(3 bulk mail thread(s) hidden" in InboxTool().run(username="alice", user_scope_id="s", channel="mail"), "the agent hears what the list dropped"
    assert "bulk mail thread(s) hidden" not in InboxTool().run(username="alice", user_scope_id="s", channel="mail", include_bulk=True)
    assert "name-1" in out and "name-2" not in out
    out = InboxTool().run(username="alice", user_scope_id="s", channel="mail", folder="Sent")
    assert "name-2" in out and "name-1" not in out
    assert listing["kw"]["mail_folder"] == "Sent" and listing["kw"]["mail_account_id"] is None
    assert "Unknown channel" in InboxTool().run(channel="fax")


def test_a_lane_hidden_by_a_view_or_the_cut_is_not_called_empty(listing):
    listing["rows"] = [_row("whatsapp", "+491700000042", waits=False, unread=0),
                       _row("telegram", "7", waits=False, unread=0), _row("telegram", "8", waits=False, unread=0)]
    out = InboxTool().run(username="alice", user_scope_id="s", channel="whatsapp", view="waits")
    assert "WhatsApp: no stored chats yet" not in out, "the view hid the chat; the lane is not empty"
    assert "Inbox: 0 conversations" in out and "view=waits" in out
    out = InboxTool().run(username="alice", user_scope_id="s", channel="telegram", max_chats="1")
    assert "Telegram: no stored chats yet" not in out and out.count("[Telegram]") == 1


def test_suspicious_mail_is_hidden_and_said(listing, monkeypatch):
    listing["rows"] = [_row("mail", "1", name="bad@evil.example", subject="urgent wire",
                            jump={"channel": "mail", "thread_id": "1", "account_id": "a@x", "folder": "INBOX"}),
                       _row("mail", "2", name="Lena", subject="lunch",
                            jump={"channel": "mail", "thread_id": "2", "account_id": "a@x", "folder": "INBOX"})]
    monkeypatch.setattr("vaf.tools.mail_utils.filter_phishing_messages_for_agent",
                        lambda ms: ([m for m in ms if m["subject"] != "urgent wire"], 1))
    out = InboxTool().run(username="alice", user_scope_id="s", channel="mail")
    assert "lunch" in out and "urgent wire" not in out
    assert "Hidden 1 suspicious mail thread(s)" in out


def test_the_tool_never_asks_a_bridge_and_the_four_it_replaced_are_gone():
    src = (TOOLS / "inbox.py").read_text(encoding="utf-8")
    assert "whatsapp_bridge" not in src and "is_bridge_running" not in src
    for old in ("whatsapp_inbox", "telegram_inbox", "discord_inbox", "mail_inbox"):
        assert not (TOOLS / f"{old}.py").exists(), old
    assert InboxTool.identity_kwargs == ("user_scope_id", "username") and InboxTool.permission_level == "read"
