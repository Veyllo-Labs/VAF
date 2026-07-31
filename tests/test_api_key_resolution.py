# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The refusing side of the key resolver. "The key arrives" proves nothing.

A secret resolver is the shape where the passing direction is worthless as evidence: every
assertion of the form "the key comes back" stays green under a resolver that is far too
willing - one that falls back to a weaker copy, hands out another provider's key, or turns a
broken store into "nothing configured". So what is asserted here is what must NOT happen.

TWO OPPOSITE POLARITIES LIVE ONE FUNCTION APART, and reading either one as the rule is the
expensive mistake this file exists to prevent:

  READING fails HARD.   A payload that exists and cannot be decrypted must not become "".
                        Empty means "not configured" to all fourteen consumers, so a corrupt
                        store would silently drop a user to the local model - and let the
                        base64 estate win, which makes the whole move reversible by accident.
  RE-KEYING fails SOFT. The key was found and is usable; only the WRITE to the new store
                        failed. A read-only data directory or a full disk says nothing about
                        whether a key is valid, and hardening that would lock out a user
                        whose key is fine, on every start, because the cause persists.

Each test below names the mutation that turns it red. They were run.
"""
import pytest

from vaf.core.api_keys import ApiKeyUnavailable, resolve_api_key
from vaf.core.secure_store import SecureBlobStore

PROVIDER = "openai"
OTHER = "anthropic"
KEY = "sk-proj-SYNTHETIC-NOT-REAL"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """The per-test provider-key store, handed back so a test can corrupt it."""
    import vaf.core.api_keys as api_keys

    blob = SecureBlobStore("provider_keys", tmp_path / "provider_keys.enc")
    monkeypatch.setattr(api_keys, "_store_singleton", blob)
    return blob


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A config file of our own - the attribute decides, not the environment.

    `Config.CONFIG_FILE` is computed at import time, so pointing HOME somewhere else moves
    nothing. Establishing the precondition rather than hoping for it.
    """
    from vaf.core.config import Config

    app_dir = tmp_path / "vaf"
    app_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Config, "APP_DIR", app_dir)
    monkeypatch.setattr(Config, "CONFIG_FILE", app_dir / "config.json")
    return Config


# ── the refusing side ────────────────────────────────────────────────────────

def test_an_unreadable_store_raises_instead_of_looking_unconfigured(store, config):
    """THE assurance. `""` and "broken" used to be the same value.

    Mutation that turns this red: have the resolver call `load()` instead of `load_strict()`,
    i.e. go back to swallowing. Every "the key arrives" test stays green through it.
    """
    store.update(lambda data: data.__setitem__(PROVIDER, KEY))
    store.enc_path.write_bytes(b"corrupted-not-a-valid-payload")

    with pytest.raises(ApiKeyUnavailable):
        resolve_api_key(PROVIDER)


def test_an_unreadable_store_does_not_fall_back_to_the_estate(store, config):
    """The half that decides whether the move is real.

    The base64 copy is still on disk during the whole migration window. If a broken store
    quietly fell through to it, the fallback would SUCCEED - the user would notice nothing,
    and the encrypted copy would stay broken forever. That is a downgrade wearing a
    migration's clothes.
    """
    store.update(lambda data: data.__setitem__(PROVIDER, KEY))
    store.enc_path.write_bytes(b"corrupted-not-a-valid-payload")
    cfg = config.load()
    cfg[f"api_key_{PROVIDER}"] = "sk-ESTATE-STILL-THERE"
    config.save(cfg)

    with pytest.raises(ApiKeyUnavailable):
        resolve_api_key(PROVIDER)


def test_a_missing_store_is_not_an_error(store, config):
    """The control, and the boundary of the rule above: nothing stored is a NORMAL state and
    must stay an empty string. Without this, "raises on anything" would also pass the two
    tests above while breaking every fresh installation."""
    assert resolve_api_key(PROVIDER) == ""


def test_one_providers_key_is_never_served_for_another(store, config):
    """No crossover, from any source. A resolver that reached for "some key" would satisfy
    every arriving-side assertion and quietly bill the wrong account."""
    store.update(lambda data: data.__setitem__(PROVIDER, KEY))
    assert resolve_api_key(OTHER) == ""
    assert resolve_api_key(OTHER, {"api_key_" + PROVIDER: KEY}) == ""


def test_the_callers_key_beats_the_store(store, config):
    """Precedence, asserted where it matters rather than where it is easy: a stored key for
    the same provider must lose."""
    store.update(lambda data: data.__setitem__(PROVIDER, "sk-STORED"))
    assert resolve_api_key(PROVIDER, {f"api_key_{PROVIDER}": "sk-CALLER"}) == "sk-CALLER"


# ── the opposite polarity, one function away ─────────────────────────────────

def test_an_unwritable_store_still_returns_the_key(store, config, monkeypatch):
    """RE-KEYING FAILS SOFT, and this is the test that keeps the two rules apart.

    Whoever reads "a decryption error is a hard error" and generalises it will make a full
    disk lock out a user whose key is perfectly fine - every start, because the cause does
    not go away. So a failed migration write is swallowed and retried on the next read.

    Mutation that turns this red: let `_migrate_into_store` propagate.
    """
    cfg = config.load()
    cfg[f"api_key_{PROVIDER}"] = "sk-ESTATE-PLAIN"
    config.save(cfg)

    def _explode(*_args, **_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(store, "update", _explode)
    assert resolve_api_key(PROVIDER) == "sk-ESTATE-PLAIN"


def test_the_migration_keeps_no_done_marker(store, config, monkeypatch):
    """Idempotent by construction: a marker is exactly what turns one transient write
    failure into a permanent one, so the next read must try again."""
    cfg = config.load()
    cfg[f"api_key_{PROVIDER}"] = "sk-ESTATE-PLAIN"
    config.save(cfg)

    failing = {"n": 0}
    real_update = store.update

    def _fail_once(mutator):
        failing["n"] += 1
        if failing["n"] == 1:
            raise OSError("transient")
        real_update(mutator)

    monkeypatch.setattr(store, "update", _fail_once)
    assert resolve_api_key(PROVIDER) == "sk-ESTATE-PLAIN"      # first read: write fails
    assert resolve_api_key(PROVIDER) == "sk-ESTATE-PLAIN"      # second read: retried
    assert store.load().get(PROVIDER) == "sk-ESTATE-PLAIN", (
        "the retry never happened - a failed migration became permanent"
    )


# ── a migration is not a transition ──────────────────────────────────────────

def test_rekeying_an_existing_key_sets_no_stt_provider(store, config):
    """The side effect that must NOT fire, and it would have fired for every upgrading user.

    `Config.apply_veyllo_stt_default` seeds `speech_stt_provider = "veyllo"` the first time
    a Veyllo key appears while no provider was chosen - and "none chosen" is the normal
    state. Once the trigger moved to the store, MOVING an existing key looks exactly like a
    new one: absent, then present. Every migrating user with a Veyllo key would have been
    switched to the metered cloud lane by an upgrade they did not ask for.

    Mutation that turns this red: drop the `is_migration` flag, so the move counts as a
    transition.
    """
    # The estate is written STRAIGHT TO THE FILE, not through `Config.save`. Going through
    # save would itself take the config dict from no-key to key - which is the very
    # transition the legacy seed watches, so the setup would fire the thing under test and
    # the assertion below would fail for the wrong reason. It did, on the first run: a test
    # simulating a precondition through a path that has the side effect it is measuring.
    import json

    config.CONFIG_FILE.write_text(json.dumps({
        "api_key_veyllo": "vaf_estate_key",
        "speech_stt_provider": "",
        "speech_stt_engine": "docker",
    }))

    assert resolve_api_key("veyllo") == "vaf_estate_key"       # migrates on the way past
    assert (config.load().get("speech_stt_provider") or "") == "", (
        "a migration flipped the user to cloud STT; moving a key is not acquiring one"
    )


def test_a_genuinely_new_key_still_seeds(store, config):
    """The control. Without it the assertion above also passes for a seed that never fires
    at all - which would silently retire a product behaviour rather than protect it."""
    from vaf.core.config import Config

    cfg = config.load()
    cfg["speech_stt_provider"] = ""
    cfg["speech_stt_engine"] = "docker"
    config.save(cfg)

    Config.set_api_key("veyllo", "vaf_brand_new_key")
    assert config.load().get("speech_stt_provider") == "veyllo"


# ── a reload must never replace a caller-supplied key ────────────────────────

def test_a_reload_cannot_substitute_the_file_for_a_callers_key(store, config):
    """The fourth assurance, and the one that replaced a GUARD rather than a gap.

    `reload_api_backend` re-applies provider and key from the LIVE on-disk config, so an
    embedded agent whose key exists only in memory could have it swapped for whatever the
    file holds. Counting the embedded-mode check there as three deleted lines was wrong
    twice over: precedence covers the KEY half, but that method also re-reads the PROVIDER,
    which precedence says nothing about - so the check stays and this test covers the half
    precedence does own.

    Asserted through the resolver with a store that disagrees, because that is the substance:
    whatever a rebuild reads, the caller's dict wins.
    """
    store.update(lambda data: data.__setitem__(PROVIDER, "sk-FROM-DISK-SIDE"))
    cfg = config.load()
    cfg[f"api_key_{PROVIDER}"] = "sk-FROM-ESTATE"
    config.save(cfg)

    caller = {f"api_key_{PROVIDER}": "sk-CALLER-OWNS-THIS"}
    assert resolve_api_key(PROVIDER, caller) == "sk-CALLER-OWNS-THIS"


def test_the_embedded_lock_still_guards_the_provider_half(config):
    """The half precedence does NOT cover, pinned so the next reader does not delete it.

    An embedded agent's provider is its caller's choice; `reload_api_backend` reads
    `provider` from the live file. Without the check, a change on disk would move an
    embedded agent to a provider nobody asked for - which no amount of key precedence
    prevents.
    """
    import inspect

    from vaf.core.agent import Agent

    src = inspect.getsource(Agent.reload_api_backend)
    assert "_config_overrides" in src, (
        "the embedded-mode check is gone. Precedence replaces its KEY half only; without it "
        "the on-disk provider reaches into an agent its caller controls."
    )


# ── the running agent must learn about a key change ──────────────────────────

def test_storing_a_changed_key_notifies_the_config_observers(store, config):
    """A LIVE REGRESSION, found by using the product rather than by running the suite.

    `Config.save` notifies observers when one of a list of critical keys differs before and
    after; the tray listens and calls `reload_api_backend`, which is what makes a key change
    reach the RUNNING agent without a restart. Lifting `api_key_*` out of the saved payload
    removed the difference, so the notification stopped: a user changed their key in
    Settings, the UI reported success, and the agent went on using the old one. The exact
    failure this whole change existed to remove, reintroduced one layer up - and no test saw
    it, because every test asks the resolver rather than the running agent.

    Mutation that turns this red: drop the `_announce_change` call.
    """
    from vaf.core.api_keys import store_api_key
    from vaf.core.config import Config

    seen = []
    Config.add_observer(lambda key, value, old=None: seen.append(key))
    try:
        store_api_key(PROVIDER, "sk-FIRST")
        store_api_key(PROVIDER, "sk-SECOND")
    finally:
        Config._observers.clear()

    assert f"api_key_{PROVIDER}" in seen, (
        "a changed key never reached the observers, so the running agent keeps the old one"
    )


def test_an_unchanged_key_and_a_migration_stay_quiet(store, config):
    """The refusing half: only a real change is announced.

    Re-writing the same value, or moving a key from the estate into the store, must not
    trigger a live backend rebuild - a migration happens on the first read after every
    update, and announcing it would rebuild the backend on startup for no reason.
    """
    from vaf.core.api_keys import store_api_key
    from vaf.core.config import Config

    store_api_key(PROVIDER, "sk-SAME")
    seen = []
    Config.add_observer(lambda key, value, old=None: seen.append(key))
    try:
        store_api_key(PROVIDER, "sk-SAME")                       # unchanged
        store_api_key(OTHER, "sk-MOVED", is_migration=True)      # a move
    finally:
        Config._observers.clear()

    assert seen == [], f"a no-op write announced a change: {seen}"
