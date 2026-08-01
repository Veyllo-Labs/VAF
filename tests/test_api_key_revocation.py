# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Deleting an API key is a REVOCATION, and it has to actually revoke.

WHY THE FRAMING MATTERS TO THE TESTS. People delete a provider key because it leaked. That
makes the dangerous failure not "the delete button did nothing" but "the delete button said
it worked" - the operator stops rotating upstream while the installation keeps
authenticating with a key someone else now has. So the assertions below are about the state
AFTER, never about the call returning.

THE TRAP THIS FILE EXISTS FOR, and it is invisible to a single-resolution test. The estate
in `config.json` is still read, and reading it MIGRATES it back into the encrypted store.
Delete from the store alone and the first resolution afterwards finds the estate copy,
returns it, and rewrites the store - the deletion repairs itself, silently, through the
repair path. A test that resolves once sees the key gone if it deleted from the store, so it
passes on the broken order. Every deletion test here therefore resolves TWICE.

The blank-value side is the other half: an empty field must never delete anything. That is
not politeness, it is the guard that keeps a partially filled Settings form from wiping a
key it simply did not re-send, and the reason deletion is a separate explicit call rather
than a meaning attached to emptiness.
"""
import pytest

from vaf.core.api_keys import (
    ApiKeyRevocationFailed,
    absorb_config_keys,
    configured_providers,
    delete_api_key,
    resolve_api_key,
    store_api_key,
)
from vaf.core.config import Config
from vaf.core.secure_store import SecureStoreUnreadable


def _estate_value(provider: str) -> str:
    return str(Config.load().get(f"api_key_{provider}", "") or "")


# ── a blank value never deletes ─────────────────────────────────────────────────────

def test_an_empty_field_does_not_delete_a_stored_key():
    """"The form did not re-send it" must never be read as "remove it"."""
    store_api_key("veyllo", "sk-still-wanted")

    absorb_config_keys({"api_key_veyllo": "", "provider": "veyllo"})

    assert resolve_api_key("veyllo") == "sk-still-wanted"


def test_an_empty_field_does_not_delete_an_estate_key():
    """Same guarantee one source down, where `merge_preserving_nonempty_sensitive` holds it."""
    merged = Config.merge_preserving_nonempty_sensitive(
        {"api_key_openai": "c2stb2xk"}, {"api_key_openai": ""}
    )
    assert merged["api_key_openai"] == "c2stb2xk"


def test_deletion_is_not_reachable_through_the_ordinary_save_path():
    """The distinction the web UI is NOT asked to make.

    Blank means untouched on every save path, so no arrangement of the Settings form can
    revoke a key by accident - and equally, none can revoke one on purpose. That is the
    trade this design makes deliberately: the layer that rebuilds payloads field by field
    has silently dropped a field twice, and here the loss would be a key outliving its
    revocation.
    """
    store_api_key("deepseek", "sk-present")
    for payload in ({"api_key_deepseek": ""}, {"api_key_deepseek": "   "}, {}):
        absorb_config_keys(dict(payload))
        assert resolve_api_key("deepseek") == "sk-present"


# ── an explicit deletion actually revokes ───────────────────────────────────────────

def test_an_explicit_deletion_removes_the_key_and_it_stays_gone():
    """THE test. The second resolution is the one that catches the self-healing order."""
    store_api_key("veyllo", "sk-leaked")
    assert resolve_api_key("veyllo") == "sk-leaked"

    delete_api_key("veyllo")

    assert resolve_api_key("veyllo") == ""
    assert resolve_api_key("veyllo") == ""       # <- the migration path had a second chance


def test_a_key_in_BOTH_places_does_not_come_back(monkeypatch):
    """The self-healing case, and the one the other deletion tests cannot see.

    This is the ordinary shape after an upgrade: base64 still in `config.json`, a migrated
    copy in the encrypted store, because reading the estate writes it forward. Delete the
    store entry alone and the next resolution reads the estate, hands the key back, and
    calls `_migrate_into_store` on the way - the key is restored by the repair path.

    The test above stores the key ONLY in the store, so it stays green even when the estate
    is never touched; it measures the store write, not the revocation. Without this one, the
    suite would sign off a deletion that quietly restores a leaked key on the very next use.
    """
    import base64

    config = dict(Config.load())
    config["api_key_veyllo"] = base64.b64encode(b"sk-leaked-everywhere").decode()
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: dict(config)))
    monkeypatch.setattr(Config, "save", classmethod(lambda cls, cfg: config.update(cfg)))
    store_api_key("veyllo", "sk-leaked-everywhere")          # both places hold it

    delete_api_key("veyllo")

    assert resolve_api_key("veyllo") == ""
    assert resolve_api_key("veyllo") == "", "the estate copy was migrated back in"
    assert not str(config.get("api_key_veyllo") or "")


def test_a_key_that_only_exists_in_the_estate_is_revoked_too(monkeypatch):
    """The upgrading user: their key is still base64 in `config.json` and nowhere else.

    Deleting only from the encrypted store would be a no-op here and would report success,
    which is the exact failure mode this whole file is about - and the one an installation
    that has not been read from yet would hit.
    """
    import base64

    config = dict(Config.load())
    config["api_key_openai"] = base64.b64encode(b"sk-legacy").decode()
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: dict(config)))
    saved = {}
    monkeypatch.setattr(Config, "save", classmethod(lambda cls, cfg: (config.update(cfg), saved.update(cfg))))

    assert resolve_api_key("openai") == "sk-legacy"
    delete_api_key("openai")

    assert resolve_api_key("openai") == ""
    assert resolve_api_key("openai") == ""
    assert not str(config.get("api_key_openai") or "")


def test_the_estate_is_cleared_before_the_store(monkeypatch):
    """Order, asserted as order rather than inferred from the outcome.

    Both orders leave the key gone when nothing fails, so a result-only test cannot tell
    them apart - and the wrong one only bites when the second write fails, which is exactly
    when nobody is watching.
    """
    import vaf.core.api_keys as api_keys

    store_api_key("google", "sk-x")
    seen = []
    real_clear = api_keys.clear_estate_entry
    real_store = api_keys._store

    monkeypatch.setattr(api_keys, "clear_estate_entry", lambda p: (seen.append("estate"), real_clear(p))[1])

    class _Watched:
        def __init__(self, inner): self._inner = inner
        def __getattr__(self, item):
            if item == "update":
                seen.append("store")
            return getattr(self._inner, item)

    monkeypatch.setattr(api_keys, "_store", lambda: _Watched(real_store()))
    delete_api_key("google")

    assert seen[:2] == ["estate", "store"], f"wrong revocation order: {seen}"


def test_a_failed_revocation_raises_instead_of_reporting_success(monkeypatch):
    """A key that survives must never look revoked - the operator stops rotating it."""
    import vaf.core.api_keys as api_keys

    store_api_key("anthropic", "sk-survivor")
    monkeypatch.setattr(api_keys, "resolve_api_key", lambda *a, **k: "sk-survivor")

    with pytest.raises(ApiKeyRevocationFailed) as excinfo:
        delete_api_key("anthropic")
    assert "rotate it at the provider" in str(excinfo.value)


def test_a_deletion_tells_the_running_agents(monkeypatch):
    """A revoked key must stop being used now, not after a restart.

    Same lesson as the reload defect one lane over: the value being gone from disk is not
    the same as the process having stopped using it.
    """
    import vaf.core.api_keys as api_keys

    store_api_key("veyllo", "sk-leaked")
    announced = []
    monkeypatch.setattr(Config, "notify_observers", classmethod(lambda cls, k, v, o=None: announced.append(k)))

    delete_api_key("veyllo")

    assert "api_key_veyllo" in announced


# ── the listing: state without the secret ───────────────────────────────────────────

def test_the_listing_reports_which_providers_have_a_key_and_never_the_key():
    """Settings lost the ability to see a configured key when keys left `config.json`."""
    store_api_key("veyllo", "sk-abcdefghijklmnop")

    listed = configured_providers()

    assert listed.get("veyllo") is True
    assert "sk-abcdefghijklmnop" not in repr(listed)
    assert all(v is True for v in listed.values()), "the listing carries values, not just state"


def test_the_listing_drops_a_provider_after_it_is_revoked():
    store_api_key("openrouter", "sk-y")
    assert configured_providers().get("openrouter") is True

    delete_api_key("openrouter")

    assert "openrouter" not in configured_providers()


def test_an_unreadable_store_is_not_reported_as_nothing_configured(monkeypatch):
    """"Nothing is set up" and "I cannot tell you" must not render as the same screen."""
    import vaf.core.api_keys as api_keys

    class _Broken:
        def load_strict(self):
            raise SecureStoreUnreadable("payload damaged")

    monkeypatch.setattr(api_keys, "_store", lambda: _Broken())
    with pytest.raises(SecureStoreUnreadable):
        configured_providers()
