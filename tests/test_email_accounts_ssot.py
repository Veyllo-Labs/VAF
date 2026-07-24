# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Phase 1 of the mail v2-only port: account-config get/save moved to the
route-independent SSOT vaf/core/email_accounts.py. These tests pin the Rule-2
anti-drift guarantee (every historical name IS the one SSOT object, so the copies
can never diverge again) and the unchanged scope-isolation behavior."""
import vaf.api.calendar_routes as calendar_routes
import vaf.api.email_routes as email_routes
import vaf.core.calendar_client as calendar_client
import vaf.core.email_accounts as ea
import vaf.core.email_transport as email_transport


def test_all_historical_names_are_the_one_ssot_object():
    # Rule 2: two drifting copies collapsed into one. Identity (not just equality)
    # so a future edit to one call site cannot silently reintroduce a fork.
    assert email_routes._get_email_config is ea.get_email_config
    assert email_routes._save_email_config is ea.save_email_config
    assert email_transport._get_email_config is ea.get_email_config
    assert calendar_client._get_email_config is ea.get_email_config
    assert calendar_routes._get_email_config is ea.get_email_config


def test_account_and_sender_rule_helpers_are_the_one_ssot_object():
    # P3.1: get_account + the sender-category-rule readers relocated to the SSOT;
    # email_transport (and its importers) re-export them. Identity, not a fork.
    assert email_transport.get_account is ea.get_account
    assert email_transport.apply_sender_rules_to_category is ea.apply_sender_rules_to_category
    assert email_transport._email_config_candidates is ea._email_config_candidates
    assert email_transport.get_sender_rules is ea.get_sender_rules
    assert email_transport._get_sender_rules is ea.get_sender_rules  # historical private alias
    assert email_routes.apply_sender_rules_to_category is ea.apply_sender_rules_to_category


def test_get_account_and_sender_rules_behavior(monkeypatch):
    monkeypatch.setattr(ea, "get_local_admin_scope_id", lambda: "admin-scope")
    monkeypatch.setattr(ea, "get_local_admin_username", lambda: "admin")
    ec = {"accounts": [{"account_id": "a@x", "email": "a@x", "provider": "imap"}],
          "sender_category_rules": [{"pattern": "twitch.tv", "category": "social"}]}
    monkeypatch.setattr(ea.Config, "get", staticmethod(lambda k, d=None: ec if k == "email_config" else d))
    assert ea.get_account("a@x")["provider"] == "imap"
    assert ea.get_account("nope@x") is None
    assert ea.apply_sender_rules_to_category("News <n@twitch.tv>", "primary") == "social"
    assert ea.apply_sender_rules_to_category("n@other.com", "primary") == "primary"


def test_list_accounts_with_labels_reads_the_ssot(monkeypatch):
    # P3.5: mail_utils.list_accounts_with_labels_for_user resolves via the SSOT
    # get_email_config instead of duplicating the scope branches.
    import vaf.tools.mail_utils as mu
    monkeypatch.setattr(ea, "get_local_admin_scope_id", lambda: "admin-scope")
    monkeypatch.setattr(ea, "get_local_admin_username", lambda: "admin")
    ec = {"accounts": [{"email": "a@x", "label": "Work"}, {"account_id": "b@y"}]}
    monkeypatch.setattr(ea.Config, "get", staticmethod(lambda k, d=None: ec if k == "email_config" else d))
    assert mu.list_accounts_with_labels_for_user() == [
        {"email": "a@x", "label": "Work"}, {"email": "b@y", "label": ""}]


def test_imap_presets_and_probe_are_the_one_ssot_object():
    # P4.1: IMAP presets + the login probe relocated to the SSOT, re-exported.
    assert email_routes.IMAP_SMTP_DEFAULTS is ea.IMAP_SMTP_DEFAULTS
    assert email_routes._test_imap_login is ea.test_imap_login


def test_account_crud_and_mail_enabled_marker(monkeypatch):
    # P4.1: add/patch/remove + the mail_enabled marker (calendar-safe delete keeps
    # the entry but hides it from the mail list).
    store = {"email_config": {"accounts": [{"account_id": "a@x", "email": "a@x", "provider": "imap"}]}}
    monkeypatch.setattr(ea, "get_local_admin_username", lambda: "admin")
    monkeypatch.setattr(ea, "get_local_admin_scope_id", lambda: "s")
    monkeypatch.setattr(ea.Config, "get", staticmethod(lambda k, d=None: store.get(k, d)))
    monkeypatch.setattr(ea.Config, "load", staticmethod(lambda: dict(store)))
    monkeypatch.setattr(ea.Config, "save", staticmethod(lambda cfg: store.update(cfg)))

    ea.add_account({"account_id": "b@y", "email": "b@y", "provider": "gmail"})
    assert [a["account_id"] for a in store["email_config"]["accounts"]] == ["a@x", "b@y"]
    assert ea.patch_account("b@y", {"label": "Work"}) and not ea.patch_account("nope@z", {"label": "x"})
    ea.set_mail_enabled("a@x", False)  # calendar-only leftover: keep entry, hide from mail
    assert [a["account_id"] for a in ea.list_mail_accounts()] == ["b@y"]
    assert ea.remove_account("b@y") and not ea.remove_account("b@y")
    assert [a["account_id"] for a in store["email_config"]["accounts"]] == ["a@x"]


def _patch_config(monkeypatch, *, email_config, by_scope, by_user,
                  admin_scope="admin-scope", admin_user="admin"):
    monkeypatch.setattr(ea, "get_local_admin_scope_id", lambda: admin_scope)
    monkeypatch.setattr(ea, "get_local_admin_username", lambda: admin_user)
    store = {
        "email_config": email_config,
        "email_config_by_scope": by_scope,
        "email_config_by_user": by_user,
    }
    monkeypatch.setattr(ea.Config, "get", staticmethod(lambda key, default=None: store.get(key, default)))


def test_scope_isolation_reads(monkeypatch):
    admin = {"accounts": [{"account_id": "admin@x"}]}
    a = {"accounts": [{"account_id": "a@x"}]}
    b = {"accounts": [{"account_id": "b@x"}]}
    alice = {"accounts": [{"account_id": "alice@x"}]}
    _patch_config(monkeypatch, email_config=admin,
                  by_scope={"scopeA": a, "scopeB": b},
                  by_user={"alice": alice})

    # local admin (no username, no scope) -> legacy email_config
    assert ea.get_email_config() == admin
    assert ea.get_email_config(user_scope_id="admin-scope") == admin
    # per-scope, no cross-scope bleed
    assert ea.get_email_config(user_scope_id="scopeA") == a
    assert ea.get_email_config(user_scope_id="scopeB") == b
    # non-admin username -> own by_user config
    assert ea.get_email_config("alice") == alice
    # scope miss for a non-admin user falls back to THAT user's config, never scopeA/B
    got = ea.get_email_config("alice", user_scope_id="ghost-scope")
    assert got == alice and got not in (a, b)


def test_save_routes_to_the_right_bucket(monkeypatch):
    captured = {}
    monkeypatch.setattr(ea, "get_local_admin_scope_id", lambda: "admin-scope")
    monkeypatch.setattr(ea, "get_local_admin_username", lambda: "admin")
    monkeypatch.setattr(ea.Config, "load", staticmethod(lambda: {}))
    monkeypatch.setattr(ea.Config, "save", staticmethod(lambda cfg: captured.update(cfg)))

    ea.save_email_config({"accounts": ["S"]}, user_scope_id="scopeX")
    assert captured["email_config_by_scope"]["scopeX"] == {"accounts": ["S"]}

    captured.clear()
    ea.save_email_config({"accounts": ["ADMIN"]})            # admin -> legacy blob
    assert captured["email_config"] == {"accounts": ["ADMIN"]}

    captured.clear()
    ea.save_email_config({"accounts": ["BOB"]}, username="bob")
    assert captured["email_config_by_user"]["bob"] == {"accounts": ["BOB"]}
