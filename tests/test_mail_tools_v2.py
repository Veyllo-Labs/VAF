# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""P3.3+: agent read tools take the v2 MailService path when mail_engine_v2_enabled
is on, and keep the legacy path when it is off (flag-off / not-yet-synced instances
stay capable until the P7 teardown)."""
import vaf.tools.find_mail as fm
import vaf.tools.mail_inbox as mi
import vaf.tools.read_mail as rm


def _flag(monkeypatch, mod, on):
    monkeypatch.setattr(mod.Config, "get",
                        staticmethod(lambda k, d=None: on if k == "mail_engine_v2_enabled" else d))


def test_find_mail_v2_searches_and_loads_body(monkeypatch):
    _flag(monkeypatch, fm, True)
    monkeypatch.setattr(fm, "store_candidates_for_mail", lambda u, s: [(None, "scope-x")])
    monkeypatch.setattr(fm, "filter_phishing_messages_for_agent", lambda ms: (ms, 0))

    class FakeSvc:
        def __init__(self, scope):
            pass

        def search_for_agent(self, q, limit=50):
            return [{"account_id": "a@x", "message_id": "<m@x>", "provider_message_id": "",
                     "from": "Alice", "date": "2026", "subject": "hello vaf"}]

        def body_text(self, mid, account_id=None, cred_username=None):
            return "the full body"

    monkeypatch.setattr("vaf.mail.service.MailService", FakeSvc)
    out = fm.FindMailTool().run(query="hello")
    assert "hello vaf" in out and "the full body" in out  # single match -> body appended


def test_mail_inbox_v2_lists_from_engine_store(monkeypatch):
    _flag(monkeypatch, mi, True)
    monkeypatch.setattr(mi, "list_accounts_for_user", lambda u, user_scope_id=None: ["a@x"])
    monkeypatch.setattr(mi, "store_candidates_for_mail", lambda u, s: [(None, "scope-x")])
    monkeypatch.setattr(mi, "filter_phishing_messages_for_agent", lambda ms: (ms, 0))

    class FakeSvc:
        def __init__(self, scope):
            pass

        def list_for_agent(self, account_id=None, folder=None, category=None, limit=50):
            return [{"account_id": "a@x", "message_id": "<m@x>", "from": "Alice",
                     "subject": "hi there", "date": "2026", "provider_message_id": ""}]

    monkeypatch.setattr("vaf.mail.service.MailService", FakeSvc)
    out = mi.MailInboxTool().run()
    assert "hi there" in out and "read_mail" in out  # listing + next-step hint


def test_read_mail_v2_uses_mailservice_body_text(monkeypatch):
    _flag(monkeypatch, rm, True)
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
    _flag(monkeypatch, rm, False)
    monkeypatch.setattr(rm, "get_account", lambda *a, **k: {"provider": "imap"})
    monkeypatch.setattr(rm, "store_candidates_for_mail", lambda u, s: [(None, "scope-x")])
    seen = {}

    def _legacy(**k):
        seen["legacy"] = True
        return "legacy body"

    monkeypatch.setattr(rm, "get_message_body_plain", _legacy)
    out = rm.ReadMailTool().run(account_id="a@x", message_id="<a1@x>")
    assert out == "legacy body" and seen.get("legacy")
