# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression tests for cloud credential key derivation.

The store and the lookup MUST build the same key. A local admin's tokens were stored under the raw
admin username (e.g. "cloud:google_drive:mert:<id>") but looked up normalized
("cloud:google_drive:<id>"), so cloud sync reported "Credentials not found". The key builder now
normalizes the username identically for store/lookup/delete. Network-free."""
from vaf.core.config import Config
import vaf.cloud.credential_cloud as cc


def _admin(monkeypatch, name="Mert"):
    _orig = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda k, d=None: name if k == "local_admin_username" else _orig(k, d)))


def test_local_admin_key_is_normalized_for_store_and_lookup(monkeypatch):
    _admin(monkeypatch, "Mert")
    raw = cc._credential_key("user@example.com", "google_drive", "Mert")   # storage path (raw username)
    norm = cc._credential_key("user@example.com", "google_drive", None)    # lookup path (normalized)
    assert raw == norm == "cloud:google_drive:user@example.com"


def test_local_admin_case_insensitive(monkeypatch):
    _admin(monkeypatch, "Mert")
    assert cc._credential_key("a@x.com", "google_drive", "mert") == \
        cc._credential_key("a@x.com", "google_drive", "MERT") == \
        "cloud:google_drive:a@x.com"


def test_network_user_keeps_username_segment(monkeypatch):
    _admin(monkeypatch, "Mert")
    key = cc._credential_key("a@x.com", "google_drive", "alice")
    assert key == "cloud:google_drive:alice:a@x.com"


def test_empty_username_no_segment(monkeypatch):
    _admin(monkeypatch, "Mert")
    assert cc._credential_key("a@x.com", "onedrive", "") == "cloud:onedrive:a@x.com"
    assert cc._credential_key("a@x.com", "onedrive", None) == "cloud:onedrive:a@x.com"
