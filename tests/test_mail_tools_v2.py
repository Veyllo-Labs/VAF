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
import vaf.tools.mail_inbox as mi
import vaf.tools.mail_utils as mu
import vaf.tools.mark_mail_answered as ma
import vaf.tools.read_mail as rm


def _flag(monkeypatch, on):
    """Set mail_engine_v2_enabled at its single reader (mail_utils)."""
    monkeypatch.setattr(mu.Config, "get",
                        staticmethod(lambda k, d=None: on if k == "mail_engine_v2_enabled" else d))


def test_find_mail_v2_searches_and_loads_body(monkeypatch):
    _flag(monkeypatch, True)
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


def test_mail_inbox_v2_lists_from_engine_store(monkeypatch):
    _flag(monkeypatch, True)
    monkeypatch.setattr(mi, "list_accounts_for_user", lambda u, user_scope_id=None: ["a@x"])
    monkeypatch.setattr(mi, "store_candidates_for_mail", lambda u, s: [(None, "scope-x")])
    monkeypatch.setattr(mi, "filter_phishing_messages_for_agent", lambda ms: (ms, 0))
    monkeypatch.setattr(
        "vaf.mail.tool_bridge.list_messages_merged",
        lambda account_id, folder, limit, offset, username, scope, category=None: [
            {"account_id": "a@x", "message_id": "<m@x>", "from": "Alice",
             "subject": "hi there", "date": "2026", "provider_message_id": ""}])
    out = mi.MailInboxTool().run()
    assert "hi there" in out and "read_mail" in out  # listing + next-step hint


def test_mark_answered_v2_uses_mailservice(monkeypatch):
    _flag(monkeypatch, True)
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
    _flag(monkeypatch, True)
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
    _flag(monkeypatch, True)
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

def test_v2_is_refused_for_a_scopeless_username_caller(monkeypatch):
    """B1: username without a scope has no v2 store; serving it from the engine
    would resolve to the LOCAL ADMIN's mailbox. email_sync_store and the sync
    supervisor both refuse this - the tools must too."""
    _flag(monkeypatch, True)
    assert mu.mail_v2_active("", None) is True            # local admin: allowed
    assert mu.mail_v2_active("", "scope-x") is True       # scoped: allowed
    assert mu.mail_v2_active("bob", "scope-x") is True    # scoped user: allowed
    assert mu.mail_v2_active("bob", None) is False        # legacy per-username: REFUSED
    assert mu.mail_v2_active("bob", "  ") is False        # blank scope counts as none
    _flag(monkeypatch, False)
    assert mu.mail_v2_active("", "scope-x") is False      # flag still wins


def test_scopeless_username_caller_never_touches_the_v2_store(monkeypatch):
    """B1 end to end: NO v2 read may happen for that caller. Reading the engine
    store here would resolve to the admin's mailbox - a cross-user leak, not a
    fallback. The tool answers empty-handed instead, which is the honest result."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(mi, "list_accounts_for_user", lambda u, user_scope_id=None: ["a@x"])
    monkeypatch.setattr(mi, "get_account", lambda *a, **k: {"provider": "imap"})
    monkeypatch.setattr(mi, "filter_phishing_messages_for_agent", lambda ms: (ms, 0))

    def _boom(*a, **k):
        raise AssertionError("v2 store was read for a scope-less username caller")

    monkeypatch.setattr("vaf.mail.tool_bridge.list_messages_merged", _boom)
    monkeypatch.setattr("vaf.mail.service.MailService", _boom)

    out = mi.MailInboxTool().run(username="bob", account_id="a@x")
    assert "sync store yet" in out          # no rows, and crucially no v2 access


def test_mail_inbox_reports_an_empty_store_neutrally(monkeypatch):
    """With no live-fetch lane left, an empty store gets ONE honest answer. It must
    not tell the user to press Sync: that fails for exactly the account class that
    can land here (one not connected for the engine yet)."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(mi, "list_accounts_for_user", lambda u, user_scope_id=None: ["i@x"])
    monkeypatch.setattr(mi, "get_account", lambda *a, **k: {"provider": "imap"})
    monkeypatch.setattr(mi, "store_candidates_for_mail", lambda u, s: [(None, "scope-x")])
    monkeypatch.setattr(mi, "filter_phishing_messages_for_agent", lambda ms: (ms, 0))
    monkeypatch.setattr("vaf.mail.tool_bridge.list_messages_merged", lambda *a, **k: [])
    out = mi.MailInboxTool().run(account_id="i@x")
    assert "syncs in the background" in out
    assert "click Sync" not in out          # would fail for this account class
