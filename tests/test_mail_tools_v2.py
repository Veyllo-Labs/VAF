# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""P3.3+: agent read tools take the v2 path when mail_engine_v2_enabled is on, and
keep the legacy path when it is off (flag-off instances stay capable until the P7
teardown).

P6.0 hardens the same switch for the default-on step. Two properties are locked
here because both fail SILENTLY - the tool answers, it just answers about the wrong
mailbox or about no mailbox at all:
- isolation: a legacy per-username caller (username set, no scope) must NEVER be
  served from the v2 store, which is scope-keyed and would resolve to the local
  admin's mailbox;
- no blanking: an account the engine does not sync (OAuth awaiting the IMAP
  re-consent) must still be served from the legacy lane.
The flag is now read in ONE place (mail_utils.mail_v2_active), so the tests patch
it there rather than per module.
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


def test_read_mail_legacy_path_when_flag_off(monkeypatch):
    _flag(monkeypatch, False)
    monkeypatch.setattr(rm, "get_account", lambda *a, **k: {"provider": "imap"})
    monkeypatch.setattr(rm, "store_candidates_for_mail", lambda u, s: [(None, "scope-x")])
    seen = {}

    def _legacy(**k):
        seen["legacy"] = True
        return "legacy body"

    monkeypatch.setattr(rm, "get_message_body_plain", _legacy)
    out = rm.ReadMailTool().run(account_id="a@x", message_id="<a1@x>")
    assert out == "legacy body" and seen.get("legacy")


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
    """B1 end to end: the same caller must land on the legacy store, and no v2
    read may happen at all (a v2 read here is a cross-user leak, not a fallback)."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(mi, "list_accounts_for_user", lambda u, user_scope_id=None: ["a@x"])
    monkeypatch.setattr(mi, "get_account", lambda *a, **k: {"provider": "imap"})
    monkeypatch.setattr(mi, "filter_phishing_messages_for_agent", lambda ms: (ms, 0))
    monkeypatch.setattr(mi, "init_store", lambda *a, **k: None)

    def _boom(*a, **k):
        raise AssertionError("v2 store was read for a scope-less username caller")

    monkeypatch.setattr("vaf.mail.tool_bridge.list_messages_merged", _boom)
    monkeypatch.setattr("vaf.mail.service.MailService", _boom)
    monkeypatch.setattr(mi, "store_list_messages", lambda **k: [
        {"account_id": "a@x", "message_id": "<legacy@x>", "from": "Alice",
         "subject": "legacy row", "date": "2026", "provider_message_id": ""}])

    out = mi.MailInboxTool().run(username="bob")
    assert "legacy row" in out


def test_mail_inbox_falls_back_to_live_fetch_for_an_account_v2_never_synced(monkeypatch):
    """B2: an OAuth account still awaiting the IMAP re-consent has no rows (and no
    account row) in mail.db. It must NOT be reported as an empty mailbox, and the
    user must not be told to press Sync - that fails for exactly this account."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(mi, "list_accounts_for_user", lambda u, user_scope_id=None: ["g@x"])
    monkeypatch.setattr(mi, "get_account", lambda *a, **k: {"provider": "gmail"})
    monkeypatch.setattr(mi, "store_candidates_for_mail", lambda u, s: [(None, "scope-x")])
    monkeypatch.setattr(mi, "filter_phishing_messages_for_agent", lambda ms: (ms, 0))
    monkeypatch.setattr("vaf.mail.tool_bridge.list_messages_merged",
                        lambda *a, **k: [])                      # engine knows nothing
    monkeypatch.setattr("vaf.mail.tool_bridge.v2_syncs_account",
                        lambda account_id, scope: False)         # ... and never covered it
    fetched = [{"account_id": "g@x", "message_id": "<live@x>", "from": "Alice",
                "subject": "fetched live", "date": "2026", "provider_message_id": "p1"}]
    stored = []
    monkeypatch.setattr(mi, "fetch_mail", lambda *a, **k: fetched)
    # the live fetch persists into the legacy store and is then read back
    monkeypatch.setattr(mi, "upsert_messages",
                        lambda *a, **k: stored.extend(fetched))
    monkeypatch.setattr(mi, "store_list_messages", lambda **k: list(stored))

    out = mi.MailInboxTool().run(account_id="g@x")
    assert "fetched live" in out           # served, not reported as an empty mailbox
    assert "syncs in the background" not in out  # and not told to press a Sync that would fail


def test_mail_inbox_reports_empty_only_when_v2_owns_the_account(monkeypatch):
    """The counterpart: when the engine DOES sync the account, an empty result is
    genuinely empty and the background-sync hint is the right answer."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(mi, "list_accounts_for_user", lambda u, user_scope_id=None: ["i@x"])
    monkeypatch.setattr(mi, "get_account", lambda *a, **k: {"provider": "imap"})
    monkeypatch.setattr(mi, "store_candidates_for_mail", lambda u, s: [(None, "scope-x")])
    monkeypatch.setattr(mi, "filter_phishing_messages_for_agent", lambda ms: (ms, 0))
    monkeypatch.setattr("vaf.mail.tool_bridge.list_messages_merged", lambda *a, **k: [])
    monkeypatch.setattr("vaf.mail.tool_bridge.v2_syncs_account",
                        lambda account_id, scope: True)

    def _no_live_fetch(*a, **k):
        raise AssertionError("live fetch must not run for a v2-owned account")

    monkeypatch.setattr(mi, "fetch_mail", _no_live_fetch)
    out = mi.MailInboxTool().run(account_id="i@x")
    assert "syncs in the background" in out


def test_read_mail_falls_back_to_legacy_body_when_v2_has_none(monkeypatch):
    """B2 for bodies: a v2 miss (unknown account) must fall through to the legacy
    fetch instead of answering 'could not load the message body'."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(rm, "get_account", lambda *a, **k: {"provider": "gmail"})
    monkeypatch.setattr(rm, "store_candidates_for_mail", lambda u, s: [(None, "scope-x")])

    class FakeSvc:
        def __init__(self, scope):
            pass

        def body_text(self, mid, account_id=None, cred_username=None):
            return None  # not in the engine store

    monkeypatch.setattr("vaf.mail.service.MailService", FakeSvc)
    monkeypatch.setattr(rm, "get_message_body_plain", lambda **k: "legacy body")
    out = rm.ReadMailTool().run(account_id="g@x", message_id="<a1@x>")
    assert out == "legacy body"
