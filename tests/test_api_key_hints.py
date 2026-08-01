# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A key hint answers "which key is this" and must never become "here is the key".

The owner's question was concrete: the state line says a key is stored, but WHICH one -
`vaf_live_q0D...`? A boolean cannot answer that, so the listing gained lossy display
hints: start, bullets, tail. These tests hold the lossy half, because the pull on this
feature only ever goes one direction - towards showing more. The hint travels as a
placeholder, never as a form value; a value would be echoed by the next save and stored
as the key, which is the loop that poisoned a real store entry the same day this was
built (see tests/test_config_emit_no_secrets.py).
"""
import base64

from vaf.core.api_keys import delete_api_key, stored_key_hints, store_api_key
from vaf.core.config import Config


def test_a_hint_shows_the_start_and_tail_and_never_the_key():
    key = "vaf_live_q0DemoDemoDemoDemoDemo1234Ab4d"
    store_api_key("veyllo", key)

    hint = stored_key_hints()["veyllo"]

    assert hint.startswith("vaf_live_q"), "the recognisable start is the point of the hint"
    assert hint.endswith("Ab4d")
    assert key not in hint, "the hint IS the key - that is the leak, reopened"
    assert "•" in hint, "nothing marks the hint as partial"


def test_the_reveal_is_bounded():
    """At most 14 characters of a long key, ever. The bound is the promise; the exact
    split (10 + 4) is layout."""
    key = "sk-" + "x" * 60
    store_api_key("openai", key)

    hint = stored_key_hints()["openai"]
    revealed = [c for c in hint if c != "•"]

    assert len(revealed) <= 14, f"the hint reveals {len(revealed)} characters of the key"


def test_a_short_key_reveals_nothing():
    """A 7-character secret with 4 shown is half given away; below 8 the hint is bullets."""
    store_api_key("openai", "short7c")

    assert set(stored_key_hints()["openai"]) == {"•"}


def test_an_estate_only_key_is_hinted_from_its_plaintext(monkeypatch):
    """The upgrading user again: base64 in config.json, nothing in the store yet.

    A hint built from the RAW estate value would show `c2st...` - the base64 shell - which
    answers "which key is this" with a string the user has never seen. The hint comes from
    the decoded form, and building it must not be the thing that migrates the key.
    """
    config = dict(Config.load())
    config["api_key_google"] = base64.b64encode(b"AIzaSyDemoDemoDemoDemoDemo12").decode()
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: dict(config)))

    hint = stored_key_hints()["google"]

    assert hint.startswith("AIzaSyDemo"), "the hint shows the base64 shell, not the key"


def test_a_revoked_key_loses_its_hint():
    store_api_key("openrouter", "sk-or-DemoDemoDemoDemoDemo12")
    assert "openrouter" in stored_key_hints()

    delete_api_key("openrouter")

    assert "openrouter" not in stored_key_hints()


def test_an_unreadable_store_yields_no_hints_rather_than_an_error():
    """The listing route already 503s on an unreadable store; the hints are decoration on
    top of it and must not turn that honest error into a crash of the whole response."""
    import vaf.core.api_keys as api_keys
    from vaf.core.secure_store import SecureStoreUnreadable

    class _Broken:
        def load_strict(self):
            raise SecureStoreUnreadable("payload damaged")

    original = api_keys._store
    api_keys._store = lambda: _Broken()
    try:
        assert stored_key_hints() == {}
    finally:
        api_keys._store = original
