# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Cloud credentials are addressed by SCOPE, and a scoped caller can never reach the owner's.

WHAT WAS WRONG. Mail and GitHub key their credentials on the caller's scope; the cloud lane
keyed on a NAME. A name is resolved per lane, so any lane that supplies none collapsed onto
`cloud:<provider>:<account>` - the machine owner's key. Step A (`579431b0`) fixed the TOOL's
resolution, which made the keys diverge for the lanes that HAVE a name, and left the
question answered per caller instead of removed. This removes it.

THE ORDER WAS THE DECISION, and it is asserted rather than described. The hole is a READ:
the providers reach `get_valid_access_token` / `get_cloud_credentials`, and those had no
scope. Changing the WRITE key first would have hidden the credentials from the user who
just connected an account while a tenant carried on reading the owner's. So the providers
carry the scope (base class, five subclasses, one factory that used to be three copies),
and the key format follows.

THE ASSURANCE IS ON THE REFUSING SIDE, and it names the OWNERLESS form rather than
"fallbacks" in general - that form IS the hole. Exactly one legacy probe is allowed, the
caller's own non-empty name, and a hit is re-keyed and DELETED so the branch drains instead
of becoming a second permanent truth.
"""
import pytest

from vaf.cloud import credential_cloud as cc
from vaf.core.credential_store import build_credential_key

SCOPE_A = "ab12cd34-0000-4000-8000-000000000001"
SCOPE_B = "ab12cd34-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _isolated_cloud_store(tmp_path, monkeypatch):
    """A cloud store per test, and no keyring - the real store is never touched."""
    from vaf.core.secure_store import SecureBlobStore

    monkeypatch.setattr(cc, "_store_singleton", SecureBlobStore("cloud", tmp_path / "cloud.enc"))
    monkeypatch.setattr(cc, "keyring_available", lambda: False)
    yield


def _key(account: str, provider: str = "google_drive", **identity) -> str:
    return build_credential_key(account, namespace="cloud", provider=provider, **identity)


def _put(key: str, token: str) -> None:
    import json
    cc._store().update(lambda d: d.__setitem__(key, json.dumps({"type": "oauth", "access_token": token})))


# ── the hole itself ─────────────────────────────────────────────────────────────────

def test_a_scoped_caller_never_reads_the_ownerless_credential():
    """THE assertion. The ownerless key is the owner's cloud account; reaching it from a
    tenant is the whole defect, and every convenience fallback is a way back to it."""
    _put(_key("acct@example.com"), "owner-token")

    assert cc.get_cloud_credentials("acct@example.com", "google_drive",
                                    username="Bob", user_scope_id=SCOPE_A) is None


def test_two_scopes_do_not_see_each_other():
    _put(_key("acct@example.com", user_scope_id=SCOPE_A), "a-token")
    _put(_key("acct@example.com", user_scope_id=SCOPE_B), "b-token")

    got_a = cc.get_cloud_credentials("acct@example.com", "google_drive", user_scope_id=SCOPE_A)
    got_b = cc.get_cloud_credentials("acct@example.com", "google_drive", user_scope_id=SCOPE_B)

    assert got_a["access_token"] == "a-token"
    assert got_b["access_token"] == "b-token"


def test_the_owner_is_unaffected():
    """Gefahrlos auslieferbar: the owner's scope collapses onto the ownerless form in the
    shared builder, so nothing about their existing entries changes."""
    _put(_key("acct@example.com"), "owner-token")

    got = cc.get_cloud_credentials("acct@example.com", "google_drive", username=None, user_scope_id=None)

    assert got["access_token"] == "owner-token"


# ── the one permitted legacy probe ──────────────────────────────────────────────────

def test_a_scoped_caller_finds_their_OWN_named_entry_and_it_is_migrated():
    """The single probe: their own non-empty name, nothing else. On a hit the entry moves
    to the scoped key and the old one is deleted, so the branch drains."""
    old = _key("acct@example.com", username="Bob")
    new = _key("acct@example.com", user_scope_id=SCOPE_A)
    _put(old, "bobs-token")

    got = cc.get_cloud_credentials("acct@example.com", "google_drive",
                                   username="Bob", user_scope_id=SCOPE_A)

    assert got["access_token"] == "bobs-token"
    data = cc._store().load()
    assert new in data, "the credential was not re-keyed onto the scoped form"
    assert old not in data, "the legacy entry survived; the probe becomes a second truth"


def test_the_probe_does_not_reach_ANOTHER_users_named_entry():
    _put(_key("acct@example.com", username="Alice"), "alices-token")

    assert cc.get_cloud_credentials("acct@example.com", "google_drive",
                                    username="Bob", user_scope_id=SCOPE_A) is None


def test_an_empty_name_yields_no_probe_at_all():
    """`_cred_key_username` normalizes empty to None, and a name key with None collapses
    onto the OWNERLESS form - so probing on an empty name would be probing the hole."""
    _put(_key("acct@example.com"), "owner-token")

    for empty in ("", "   ", None):
        assert cc.get_cloud_credentials("acct@example.com", "google_drive",
                                        username=empty, user_scope_id=SCOPE_A) is None


def test_a_second_read_does_not_probe_again():
    """Idempotent by deletion rather than by a marker: after the migration the scoped key
    answers directly, which is what makes the branch finite."""
    _put(_key("acct@example.com", username="Bob"), "bobs-token")
    cc.get_cloud_credentials("acct@example.com", "google_drive", username="Bob", user_scope_id=SCOPE_A)

    again = cc.get_cloud_credentials("acct@example.com", "google_drive", username="Bob", user_scope_id=SCOPE_A)

    assert again["access_token"] == "bobs-token"
    assert len(cc._store().load()) == 1, "the store grew; the migration is not a move"


# ── the write side has to match, or the chain is decoration ─────────────────────────

def test_writing_and_reading_agree_on_the_scoped_key():
    cc.set_cloud_oauth_tokens("acct@example.com", "google_drive", "at", "rt",
                              None, "Bob", user_scope_id=SCOPE_A)

    assert cc.get_cloud_credentials("acct@example.com", "google_drive",
                                    username="Bob", user_scope_id=SCOPE_A)["access_token"] == "at"
    assert cc.get_cloud_credentials("acct@example.com", "google_drive",
                                    username="Bob", user_scope_id=SCOPE_B) is None


def test_deleting_addresses_the_scoped_key():
    cc.set_cloud_oauth_tokens("acct@example.com", "google_drive", "at", "rt",
                              None, "Bob", user_scope_id=SCOPE_A)
    cc.delete_cloud_credentials("acct@example.com", "google_drive", "Bob", user_scope_id=SCOPE_A)

    assert cc.get_cloud_credentials("acct@example.com", "google_drive",
                                    username="Bob", user_scope_id=SCOPE_A) is None


# ── the chain: the providers must actually carry it ─────────────────────────────────

def test_every_provider_carries_the_scope_to_the_credential_lookup():
    """The wiring, not the stage. A subclass that drops the scope in its own `__init__`
    would leave that provider reading the owner's credentials while every unit test of the
    key builder stayed green - the "stage tested, wiring not" shape."""
    from vaf.cloud.base import create_cloud_provider

    for name in ("google_drive", "onedrive", "dropbox", "nextcloud", "icloud"):
        prov = create_cloud_provider(name, "Bob", "acct@example.com", user_scope_id=SCOPE_A)
        assert getattr(prov, "user_scope_id", None) == SCOPE_A, f"{name} dropped the scope"


def test_there_is_exactly_one_provider_factory():
    """It was three byte-identical copies. Three would have meant three hand-applications of
    this change, and a forgotten one does not fail - it silently resolves as the owner."""
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    factories = []
    for path in sorted((repo / "vaf").rglob("*.py")):
        tree = ast.parse(path.read_bytes())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_create_provider":
                # api_backend has an unrelated method of the same name (LLM providers).
                if "cloud" in path.as_posix() or path.name == "cloud_storage.py":
                    factories.append(path.relative_to(repo).as_posix())
    assert not factories, f"a cloud provider factory reappeared: {factories}"
