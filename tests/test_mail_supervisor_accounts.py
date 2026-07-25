# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""P6.0: which accounts the mail sync supervisor may poll.

Two user intents were silently ignored by the sweep, and both get worse the moment
the engine becomes the default lane:
- `mail_enabled=False` marks an account the user deleted from Mail while the shared
  OAuth token and the config entry stay for Calendar. Re-syncing it re-creates the
  rows the delete just purged, so the delete undoes itself within one sweep.
- `auto_sync_enabled=False` is the per-account toggle in the account panel. Ignoring
  it makes turning auto-sync OFF *increase* the polling rate (30-minute legacy loop
  -> 5-minute sweep plus a permanent IDLE connection).
The send drain must stay wider than the sync set: a queued mail still has to leave
even for an account whose mailbox is no longer polled.
"""
import vaf.core.config as cfg_mod
import vaf.mail.supervisor as sup


def _accounts(monkeypatch, accounts, by_scope=None):
    store = {"email_config": {"accounts": accounts},
             "email_config_by_scope": by_scope or {}}
    monkeypatch.setattr(cfg_mod.Config, "get",
                        staticmethod(lambda k, d=None: store.get(k, d)))
    monkeypatch.setattr(cfg_mod, "get_local_admin_scope_id", lambda: "admin-scope")


def test_wants_sync_honors_all_three_intents():
    assert sup._wants_sync({"account_id": "a@x"}) is True                      # defaults on
    assert sup._wants_sync({"enabled": False}) is False
    assert sup._wants_sync({"mail_enabled": False}) is False                   # calendar-safe delete
    assert sup._wants_sync({"auto_sync_enabled": False}) is False              # panel toggle
    assert sup._wants_sync({"enabled": True, "mail_enabled": True,
                            "auto_sync_enabled": True}) is True


def test_collect_accounts_is_the_send_drain_set_and_stays_wide(monkeypatch):
    # deliberately wider than the sync set: a queued send must never be stranded
    _accounts(monkeypatch, [
        {"account_id": "a@x", "provider": "imap"},
        {"account_id": "b@x", "provider": "imap", "auto_sync_enabled": False},
        {"account_id": "c@x", "provider": "gmail", "mail_enabled": False},
        {"account_id": "d@x", "provider": "imap", "enabled": False},
    ])
    got = [a["account_id"] for _s, _u, a in sup._collect_accounts()]
    assert got == ["a@x", "b@x", "c@x"]   # only `enabled` filters here
    assert "d@x" not in got


def test_sync_set_drops_mail_disabled_and_auto_sync_off(monkeypatch):
    # the sweep's own filter, applied on top of _collect_accounts
    _accounts(monkeypatch, [
        {"account_id": "a@x", "provider": "imap"},
        {"account_id": "b@x", "provider": "imap", "auto_sync_enabled": False},
        {"account_id": "c@x", "provider": "gmail", "imap_ready": True, "mail_enabled": False},
        {"account_id": "e@x", "provider": "gmail"},  # not imap_ready -> no IMAP sync
    ])
    accounts = sup._collect_accounts()
    syncable = [a["account_id"] for _s, _u, a in accounts
                if sup._wants_sync(a)
                and ((a.get("provider") or "imap").lower() == "imap" or a.get("imap_ready"))]
    assert syncable == ["a@x"]


def test_scope_lane_accounts_are_filtered_the_same_way(monkeypatch):
    _accounts(monkeypatch, [], by_scope={
        "scope-1": {"accounts": [
            {"account_id": "s1@x", "provider": "imap"},
            {"account_id": "s2@x", "provider": "imap", "mail_enabled": False},
        ]},
        "admin-scope": {"accounts": [{"account_id": "dup@x", "provider": "imap"}]},
    })
    rows = sup._collect_accounts()
    ids = [a["account_id"] for _s, _u, a in rows]
    assert "s1@x" in ids and "s2@x" in ids          # send drain keeps both
    assert "dup@x" not in ids                        # admin scope is not read twice
    assert all(s == "scope-1" for s, _u, _a in rows)
    syncable = [a["account_id"] for _s, _u, a in rows if sup._wants_sync(a)]
    assert syncable == ["s1@x"]                      # the deleted-from-mail one is not polled
