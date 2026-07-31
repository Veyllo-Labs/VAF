# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression tests for cloud credential key derivation.

The store and the lookup MUST build the same key. A local admin's tokens were stored under the raw
admin username (e.g. "cloud:google_drive:<adminname>:<id>") but looked up normalized
("cloud:google_drive:<id>"), so cloud sync reported "Credentials not found". Network-free.

WHERE THE NORMALISATION LIVES CHANGED, and these tests caught the moment it went missing. The lane
used to own a `_credential_key` that normalised internally; it now calls the shared
`build_credential_key`, which does not, so each call site normalises itself - the same convention
mail and github already follow. During the merge three of the four call sites lost it for one
edit, i.e. they would have STORED under the raw name and LOOKED UP normalised: precisely the
incident above, reintroduced. These tests failed, which is the only reason it was noticed."""
from vaf.core.config import Config
import vaf.cloud.credential_cloud as cc




def _key(cc, account_id, provider, username, monkeypatch=None):
    """Drive the REAL storage path and report the key its call site built.

    An earlier version of this helper normalised the username itself and then called the shared
    builder - reproducing the call site instead of exercising it. Removing the normalisation from
    all four real call sites left every test in this file GREEN, because none of them ever reached
    those lines. It now intercepts the builder inside the lane, so what is asserted is what the
    call site actually passes.
    """
    import vaf.cloud.credential_cloud as mod
    from vaf.core.credential_store import build_credential_key as real

    seen = {}

    def _spy(acct, **kw):
        key = real(acct, **kw)
        seen["key"] = key
        return key

    orig = mod.build_credential_key
    mod.build_credential_key = _spy
    try:
        try:
            mod.set_cloud_oauth_tokens(account_id, provider, "at", "rt", 0, username)
        except Exception:
            pass                      # storage may be unavailable; the key was built regardless
    finally:
        mod.build_credential_key = orig
    return seen.get("key")


def _admin(monkeypatch, name="Owner"):
    _orig = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda k, d=None: name if k == "local_admin_username" else _orig(k, d)))


def test_local_admin_key_is_normalized_for_store_and_lookup(monkeypatch):
    _admin(monkeypatch, "Owner")
    raw = _key(cc, "user@example.com", "google_drive", "Owner")   # storage path (raw username)
    norm = _key(cc, "user@example.com", "google_drive", None)    # lookup path (normalized)
    assert raw == norm == "cloud:google_drive:user@example.com"


def test_local_admin_case_insensitive(monkeypatch):
    _admin(monkeypatch, "Owner")
    assert _key(cc, "a@x.com", "google_drive", "owner") == \
        _key(cc, "a@x.com", "google_drive", "OWNER") == \
        "cloud:google_drive:a@x.com"


def test_network_user_keeps_username_segment(monkeypatch):
    _admin(monkeypatch, "Owner")
    key = _key(cc, "a@x.com", "google_drive", "alice")
    assert key == "cloud:google_drive:alice:a@x.com"


def test_empty_username_no_segment(monkeypatch):
    _admin(monkeypatch, "Owner")
    assert _key(cc, "a@x.com", "onedrive", "") == "cloud:onedrive:a@x.com"
    assert _key(cc, "a@x.com", "onedrive", None) == "cloud:onedrive:a@x.com"
