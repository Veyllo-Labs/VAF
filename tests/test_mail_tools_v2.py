# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The agent read tools serve from the v2 engine store.

The legacy read lane they used to fall back on is gone (P7.3 deleted the transport
fetch/body functions), so what is guarded here is the ISOLATION rule that decides
who may be served at all - it fails SILENTLY: the tool answers, it just answers
about the wrong mailbox. A legacy per-username caller (username set, no scope) has
no store of its own, and the v2 store is scope-keyed, so serving it would resolve
to the LOCAL ADMIN's mailbox. The rule lives in one place
(mail_utils.mail_v2_active), which is where these tests patch the flag.
"""
import vaf.tools.find_mail as fm
import vaf.tools.label_mail as lm
import vaf.tools.inbox as ib
import vaf.tools.mail_utils as mu
import vaf.tools.mark_mail_answered as ma
import vaf.tools.read_mail as rm


def test_find_mail_v2_searches_and_loads_body(monkeypatch):
    monkeypatch.setattr(fm, "store_candidates_for_mail", lambda u, s: [(None, "scope-x")])
    monkeypatch.setattr(fm, "filter_phishing_messages_for_agent", lambda ms: (ms, 0))
    monkeypatch.setattr(
        "vaf.mail.tool_bridge.search_messages_merged",
        lambda q, folder, limit, username, scope: [
            {"account_id": "a@x", "message_id": "<m@x>", "provider_message_id": "",
             "from": "Alice", "date": "2026", "subject": "hello vaf"}])

    class FakeSvc:
        def __init__(self, scope):
            pass

        def body_text(self, mid, account_id=None, cred_username=None):
            return "the full body"

    monkeypatch.setattr("vaf.mail.service.MailService", FakeSvc)
    out = fm.FindMailTool().run(query="hello")
    assert "hello vaf" in out and "the full body" in out  # single match -> body appended


def test_inbox_mail_lane_lists_from_engine_store(monkeypatch):
    """The inbox tool's mail rows come from the shared inbox primitive over the v2 store."""
    row = {"key": "mail:7", "channel": "mail", "id": "7", "name": "Alice", "subject": "hi there",
           "preview": "hello", "preview_from": "them", "last_ts": 1_700_000_000.0, "message_count": 1,
           "unread": 1, "waits": True, "waits_reason": "unanswered", "answered_by_agent": False,
           "done": False, "is_group": False, "mode": "mail", "reply_window_until": None,
           "can_compose": False, "session_id": "",
           "jump": {"channel": "mail", "thread_id": "7", "account_id": "a@x", "folder": "INBOX", "message_id": "<m@x>"}}
    monkeypatch.setattr("vaf.core.inbox.list_conversations",
                        lambda *a, **k: {"rows": [row], "counts": {"all": 1, "waits": 1, "unread": 1, "agent": 0}, "channels": ["mail"]})
    monkeypatch.setattr("vaf.tools.mail_utils.filter_phishing_messages_for_agent", lambda ms: (ms, 0))
    out = ib.InboxTool().run(channel="mail", user_scope_id="scope-x")
    assert "hi there" in out and "read_mail" in out and "message_id='<m@x>'" in out


def test_mark_answered_v2_uses_mailservice(monkeypatch):
    monkeypatch.setattr(ma, "get_account", lambda *a, **k: {"provider": "imap"})
    monkeypatch.setattr(ma, "store_candidates_for_mail", lambda u, s: [(None, "scope-x")])

    class FakeSvc:
        def __init__(self, scope):
            pass

        def mark_answered(self, account_id, message_id, at=None):
            return True

    monkeypatch.setattr("vaf.mail.service.MailService", FakeSvc)
    out = ma.MarkMailAnsweredTool().run(account_id="a@x", message_id="<m@x>")
    assert "marked as answered" in out and "Beantwortet" in out  # typo fixed too


def test_label_mail_v2_sets_category_and_rule(monkeypatch):
    monkeypatch.setattr(lm, "get_account", lambda *a, **k: {"provider": "imap"})
    monkeypatch.setattr(lm, "store_candidates_for_mail", lambda u, s: [(None, "scope-x")])
    monkeypatch.setattr(lm, "_add_sender_rule", lambda scope, pattern, category: None)

    class FakeSvc:
        def __init__(self, scope):
            pass

        def set_category(self, account_id, message_id, category):
            return True

        def message_from_addr(self, account_id, message_id):
            return "Alice <alice@example.com>"

    monkeypatch.setattr("vaf.mail.service.MailService", FakeSvc)
    out = lm.LabelMailTool().run(account_id="a@x", message_id="<m@x>", category="social")
    assert "Label set to 'social'" in out and "sender rule" in out


def test_read_mail_v2_uses_mailservice_body_text(monkeypatch):
    monkeypatch.setattr(rm, "get_account", lambda *a, **k: {"provider": "imap", "email": "bob@example.com"})
    monkeypatch.setattr(rm, "store_candidates_for_mail", lambda u, s: [(None, "scope-x")])

    class FakeSvc:
        def __init__(self, scope):
            self.scope = scope

        def body_text(self, mid, account_id=None, cred_username=None):
            return "the message body"

    monkeypatch.setattr("vaf.mail.service.MailService", FakeSvc)
    out = rm.ReadMailTool().run(account_id="bob@example.com", message_id="<a1@x>")
    assert out == "the message body"


# ── P6.0 blocker guards ────────────────────────────────────────────────────────

def test_v2_is_refused_for_a_scopeless_username_caller():
    """The isolation rule, which outlived the engine flag: a username without a
    scope has no store of its own, and the store is scope-keyed - serving that
    caller would resolve to the LOCAL ADMIN's mailbox. email_sync_store and the
    sync supervisor refuse the same mapping."""
    assert mu.mail_v2_active("", None) is True            # local admin: allowed
    assert mu.mail_v2_active("", "scope-x") is True       # scoped: allowed
    assert mu.mail_v2_active("bob", "scope-x") is True    # scoped user: allowed
    assert mu.mail_v2_active("bob", None) is False        # legacy per-username: REFUSED
    assert mu.mail_v2_active("bob", "  ") is False        # blank scope counts as none


def test_scopeless_username_caller_never_touches_the_v2_store(monkeypatch):
    """B1 end to end: NO v2 read may happen for that caller. Reading the engine
    store here would resolve to the admin's mailbox - a cross-user leak, not a
    fallback. The tool answers empty-handed instead, which is the honest result."""
    def _boom(*a, **k):
        raise AssertionError("v2 store was read for a scope-less username caller")

    monkeypatch.setattr("vaf.mail.service.MailService", _boom)
    monkeypatch.setattr("vaf.mail.store.MailStore.exists", staticmethod(_boom))

    out = ib.InboxTool().run(username="bob", channel="mail", account_id="a@x")
    assert "syncs in the background" in out          # no rows, and crucially no v2 access


def test_inbox_reports_an_empty_mail_store_neutrally(monkeypatch):
    """With no live-fetch lane left, an empty store gets ONE honest answer. It must
    not tell the user to press Sync: that fails for exactly the account class that
    can land here (one not connected for the engine yet)."""
    monkeypatch.setattr("vaf.core.inbox.list_conversations",
                        lambda *a, **k: {"rows": [], "counts": {"all": 0, "waits": 0, "unread": 0, "agent": 0}, "channels": ["mail"]})
    out = ib.InboxTool().run(channel="mail", account_id="i@x", user_scope_id="scope-x")
    assert "syncs in the background" in out
    assert "click Sync" not in out          # would fail for this account class
