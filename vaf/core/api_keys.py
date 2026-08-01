# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Where a provider API key comes from. One answer, asked by the product and by an embedder.

There used to be three answers. `Config.get_api_key` is a classmethod over the on-disk
config, so a dict handed to `Agent(config={...})` could never influence it; the embedder's
key travelled a separate RAW path into one constructor argument instead. Measured: it
reached ONE of thirteen consumers. The failover chain and model discovery call the
file-reading path unconditionally, even in embedded mode, so for an embedder that chain was
structurally dead rather than merely weak - while `docs/EMBEDDING.md` said "pass your key".

SOURCES, IN ORDER. Only the second is ever written to.

  1. the caller's config      `Agent(config={"api_key_<provider>": "..."})`
  2. the encrypted store      the same envelope encryption that already holds mail,
                              GitHub and cloud credentials
  3. `config.json`, base64    the estate, READ ONLY - with exactly one exception, named so
                              it cannot become a quiet contradiction: `delete_api_key`
                              blanks the estate entry, because a revocation that leaves a
                              readable copy behind is not a revocation. Nothing WRITES a key
                              there any more; removal is the opposite operation.

WHAT "ENCRYPTED" MEANS HERE, and it belongs next to the code rather than in a release note.
Without a master passphrase - the default, and the headless case - the KEK is a random key
kept in `config.json` (`secure_store_kek`); `secure_store` says of itself that this is
"equivalent to chmod-only protection". The gain over base64 is real and bounded: the secret
no longer sits in the same file as everything else, it is not readable by eye, and a config
backup or a screenshot no longer carries it. It is NOT "whoever can read the data directory
cannot get the keys" - with no passphrase set, they can. Saying otherwise would be the label
whose present tense nobody measures.

TWO OPPOSITE POLARITIES LIVE IN THIS FILE, and mistaking one for the other is the expensive
mistake:

  READING fails HARD.        A payload that exists and cannot be decrypted must not become
                             an empty string. Empty means "not configured" to all thirteen
                             consumers, so a corrupt store would silently downgrade a user
                             to the local model - and, worse, let the weaker base64 copy win.
  RE-KEYING fails SOFT.      The key was found and is usable; only the WRITE to the new
                             store failed - read-only data dir, full disk, keyring gone,
                             lock not acquirable. Hardening that would lock out a user whose
                             key is perfectly fine, on every single start.

The migration is therefore idempotent and keeps NO "already migrated" marker: a marker is
exactly the state that turns one transient write failure into a permanent one.
"""
import base64
import threading
from typing import Optional

from vaf.core.config import Config
from vaf.core.platform import Platform
from vaf.core.secure_store import SecureBlobStore, SecureStoreUnreadable

_STORE_NAME = "provider_keys"
_store_singleton: Optional[SecureBlobStore] = None
_store_lock = threading.Lock()


class ApiKeyUnavailable(RuntimeError):
    """A key is stored for this provider and could not be read.

    Deliberately NOT the same as "no key configured", which is an empty string and a normal
    state. Raised per provider at the moment it is asked for, never at import or startup: a
    damaged entry for a provider nobody uses must not take the installation down.
    """


class ApiKeyRevocationFailed(RuntimeError):
    """An explicit deletion did not remove the key from everywhere it can be answered from.

    Its own error, and loud, because of WHY people delete a key: usually because it leaked.
    That makes a half-completed deletion the worst outcome on this path - the operator is
    told the key is gone, stops rotating it upstream, and the installation keeps
    authenticating with it. A revocation either completed or it did not; there is no
    best-effort reading of it, and nothing here may swallow a failure the way the migration
    deliberately does.
    """


def _store() -> SecureBlobStore:
    """Lazily-created encrypted store (path resolved on first use).

    Third instance of a store that already exists twice - `credential_store.py` for mail and
    `credential_cloud.py` for cloud. Reusing it is not about the saved lines: it already
    serialises read-modify-write across a process-local lock AND a cross-process file lock
    and writes atomically, which is exactly what concurrent re-keying needs. A fresh
    implementation would have to reproduce that before it was correct.
    """
    global _store_singleton
    if _store_singleton is None:
        with _store_lock:
            if _store_singleton is None:
                _store_singleton = SecureBlobStore(
                    _STORE_NAME, Platform.data_dir() / "provider_keys.enc"
                )
    return _store_singleton


def _decode_estate(raw: str) -> str:
    """Decode a `config.json` value written by any version of VAF.

    Keys written before the encoder existed sit there unencoded, so one name carries two
    encodings and only an exception tells them apart. Both must keep reading - the estate is
    on every installed copy and reaches users through `vaf update`.

    KNOWN HAZARD, recorded rather than repaired: a stored value that is BOTH valid base64
    and valid UTF-8 decodes to something else entirely. Measured across the real shapes
    (`sk-`, `sk-ant-`, `gsk_`, `AIza`) - none of them decodes, so all of them survive. The
    hazard is the shape, not today's data, and it disappears with the estate rather than
    being papered over here.
    """
    try:
        return base64.b64decode(raw.encode()).decode()
    except Exception:
        return raw


def resolve_api_key(provider: str, caller_config: Optional[dict] = None) -> str:
    """The one answer to "what is the API key for this provider".

    Returns "" when no key is configured anywhere - a normal state, and what every consumer
    already reads as "not set up". Raises `ApiKeyUnavailable` when a key IS stored and
    cannot be read, which is the case that used to be indistinguishable from the first.
    """
    name = (provider or "").strip().lower()
    if not name:
        return ""

    # 1. The caller's own config. Beats everything, including a later reload: an embedder
    #    who passed a key must never have it replaced by whatever the file happens to hold.
    if caller_config:
        supplied = caller_config.get(f"api_key_{name}")
        if supplied and str(supplied).strip():
            return str(supplied).strip()

    # 2. The encrypted store. STRICT: an unreadable payload raises instead of looking empty,
    #    because "empty" would send us to source 3 - and the estate is still on disk, so the
    #    fallback would succeed and nobody would learn that the encrypted copy broke.
    try:
        stored = _store().load_strict()
    except SecureStoreUnreadable as exc:
        raise ApiKeyUnavailable(
            f"The stored API key for '{name}' could not be read ({exc}). "
            f"Re-enter it in Settings, or restore the data directory from a backup."
        ) from exc
    value = stored.get(name)
    if value and str(value).strip():
        return str(value).strip()

    # 3. The estate, read-only - and migrated on the way past.
    raw = Config.get(f"api_key_{name}", "") or ""
    if not str(raw).strip():
        return ""
    key = _decode_estate(str(raw))
    if not key:
        return ""
    _migrate_into_store(name, key)
    return key


def _seed_side_effects(name: str) -> None:
    """Run the config side effects that used to hang off a key appearing in `config.json`.

    `Config.save` seeds `speech_stt_provider = "veyllo"` the first time a Veyllo key shows
    up while no STT provider was chosen - and it triggers on the key appearing IN THE CONFIG
    DICT. Moving keys out of that dict would have retired the seed for every write path at
    once; its own docstring says "one place, every write path - so a new key-write path can
    never silently miss it", and a new key-write path did exactly that. A test whose
    docstring has warned about this for years is what caught it.

    The rule itself is NOT reimplemented here. `apply_veyllo_stt_default` stays the single
    definition and is handed a synthetic before/after pair describing the transition that
    now happens in the store. Only its OUTCOME is written back, never the key.
    """
    try:
        config = Config.load()
        key_name = f"api_key_{name}"
        seeded = Config.apply_veyllo_stt_default(
            {**config, key_name: ""},          # before: no key
            {**config, key_name: "present"},   # after: key present (value never stored)
        )
        chosen = str(seeded.get("speech_stt_provider") or "").strip()
        if chosen and chosen != str(config.get("speech_stt_provider") or "").strip():
            Config.set("speech_stt_provider", chosen)
    except Exception:                                       # noqa: BLE001 - best effort, like the original
        pass


def _migrate_into_store(name: str, key: str) -> None:
    """Move one estate key into the encrypted store. SOFT: never raises.

    The key has already been found and is about to be returned; only the write can fail
    here, and a read-only data directory or a full disk says nothing about whether the key
    is valid. Hardening this would lock out a user whose key is perfectly fine - on every
    start, because the cause persists. So a failure is left for the next read to retry, and
    nothing records that a migration was attempted: a marker is precisely what turns one
    transient failure into a permanent one.
    """
    try:
        store_api_key(name, key, is_migration=True)
    except Exception:                                       # noqa: BLE001 - see docstring
        pass


def absorb_config_keys(config: dict) -> dict:
    """Take every `api_key_*` out of an incoming config payload and into the store.

    THE WRITE SIDE, and leaving it out would have been a user-visible bug rather than an
    unfinished sentence. The Settings UI and the WebSocket config update both merge a body
    into `config.json`, and they write API keys RAW - so with only the read side moved, a
    key would migrate into the store on first read while the next "Save" click kept writing
    to a file nobody asks any more. The user changes their key, the UI says saved, and the
    agent goes on using the old one.

    Returns the payload with those entries removed, so the caller saves a config that no
    longer carries secrets. An empty value is dropped rather than stored: the merge helper
    it runs beside treats blank as "keep what you had", and honouring that here is what
    keeps a Settings save from wiping a key the form did not re-send.
    """
    if not isinstance(config, dict):
        return config
    cleaned = dict(config)
    for key in [k for k in cleaned if k.startswith("api_key_")]:
        value = cleaned.pop(key)
        if isinstance(value, str) and value.strip():
            store_api_key(key[len("api_key_"):], value.strip())
    return cleaned


def store_api_key(provider: str, key: str, *, is_migration: bool = False) -> None:
    """Persist a key. Writes ONLY to the encrypted store, never to `config.json`.

    A MIGRATION IS NOT A TRANSITION, and that distinction has to be made explicitly because
    the store cannot see it. Once the seed trigger moves here, re-keying an existing key
    looks exactly like a brand-new one - absent, then present - so every upgrading user who
    already had a Veyllo key would silently have `speech_stt_provider` switched to the
    metered cloud lane. A behaviour change nobody asked for, caused by a move.

    So the seed fires only when the key is new TO THE USER, never when it changes location.
    The refusing side is the assertion that matters here: re-keying sets no STT provider.
    """
    name = (provider or "").strip().lower()
    if not name or not key:
        return
    existing = _store().load()
    previous = existing.get(name)
    _store().update(lambda data: data.__setitem__(name, str(key)))
    if is_migration:
        return                      # a move is neither a new key nor a changed one
    if not previous:
        _seed_side_effects(name)
    if previous != str(key):
        _announce_change(name)


def _announce_change(name: str) -> None:
    """Tell the config observers a key changed, even though it no longer lives there.

    A LIVE REGRESSION THIS REPAIRS, found by running the product rather than the suite.
    `Config.save` compares a list of critical keys before and after and notifies observers on
    a difference; the tray listens and calls `reload_api_backend`, which is what makes a key
    change take effect in the RUNNING agent without a restart. Lifting `api_key_*` out of the
    payload removed the difference, so the notification stopped - and a user who changed
    their key in Settings watched the agent keep using the old one, with the UI reporting
    success. Exactly the shape this change set out to remove, reintroduced one layer up.

    The VALUE is redacted rather than passed. No observer consults it for these keys - the
    only listener branches on the key NAME - and routing a secret through a fan-out that
    ends in logging calls would be a poor trade for an argument nobody reads.
    """
    try:
        Config.notify_observers(f"api_key_{name}", "<changed>", None)
    except Exception:                                       # noqa: BLE001 - never block a write
        pass


def clear_estate_entry(provider: str) -> None:
    """Blank the `config.json` entry after the key lives in the encrypted store.

    NOT called by the migration, and that is deliberate. TWO reasons, and both have to stay
    together - whoever later remembers only the first will turn this on and take the second
    away with it:

      1. It is the only destructive step, and it touches the live file of every user
         through `vaf update`.
      2. It is what makes a DOWNGRADE survivable. A user who rolls back to an older VAF
         finds their keys only in `config.json`; a version without the encrypted store
         cannot read the new location. While the entry stands, the way back is open.

    WHEN THE ESTATE READER MAY FINALLY GO - and this is a RELEASE statement with a version
    floor, not a filesystem check: when no supported update path can still bring a
    `config.json` carrying base64 keys, i.e. when the oldest version `vaf update` leads from
    already writes to the encrypted store. "config.json has no non-empty api_key_* any more"
    is a LOCAL observation - a fresh install satisfies it trivially while every older one in
    the field still carries it, and deleting the branch on that basis takes every existing
    user's keys away on their next update.

    THE TWO ENDINGS ARE THE SAME MOMENT, which neither paragraph shows on its own: the
    condition above is only reachable once the entry is gone from the field, which is only
    reachable by calling this - the very step reason 2 forbids. So the end of the estate
    reader IS the end of the rollback path, and the order is: switch on -> at least one
    release in the field -> only then may the reader fall.
    """
    name = (provider or "").strip().lower()
    if not name:
        return
    key = f"api_key_{name}"
    config = Config.load()
    if config.get(key):
        config[key] = ""
        Config.save(config)


def configured_providers() -> dict:
    """Which providers have a key, WITHOUT revealing one. Never returns a value.

    The Settings page needs this and, since keys left `config.json`, has nothing else to go
    on: `GET /api/config` answers `api_key_<provider>` with the empty default, so a
    perfectly good key reads as "not configured" in the only place a person looks. That was
    a side effect of moving the keys and it is repaired here rather than by handing the
    secret back to a browser.

    STRICT on purpose. An unreadable store raises rather than reporting every provider as
    unset - "nothing is configured" and "I cannot tell you what is configured" must not
    render as the same screen, which is the same honesty rule the dashboard applies to
    absent module data.
    """
    stored = _store().load_strict()          # raises SecureStoreUnreadable; caller converts
    names = {str(k).strip().lower() for k, v in stored.items() if v and str(v).strip()}
    for key, value in (Config.load() or {}).items():
        if key.startswith("api_key_") and isinstance(value, str) and value.strip():
            names.add(key[len("api_key_"):].strip().lower())
    return {name: True for name in sorted(names) if name}


def delete_api_key(provider: str) -> None:
    """Revoke a key everywhere it can be answered from. ESTATE FIRST, store second.

    THE ORDER IS THE WHOLE FUNCTION, and getting it wrong produces a deletion that undoes
    itself while reporting success. Clear the encrypted store first and the estate second,
    and any failure in between leaves `config.json` holding the key - so the very next
    `resolve_api_key` finds it there, returns it, AND writes it back into the store through
    `_migrate_into_store`. The key is fully restored, by the repair path, with nothing
    logged. Estate first cannot do that: whatever fails afterwards, no source remains that
    can re-seed the store, and the failure stays visible instead of healing.

    An empty value never reaches here. Blank means "the form did not re-send it", and
    `merge_preserving_nonempty_sensitive` plus `absorb_config_keys` both keep it that way;
    deleting is a separate, explicit call. The alternative - blank means delete - would have
    rested on the web UI distinguishing "cleared" from "never touched" and transmitting that
    distinction intact, and that layer rebuilds its payloads field by field and has silently
    dropped a field twice (CLAUDE.md Rule 2). The damage there would be a key that outlives
    its revocation.

    THIS IS THE ONE PLACE THAT WRITES `config.json`, and the module header calls source 3
    read-only, so it needs naming rather than an exception being quietly true. Reading is
    still all the estate is used for; this is the opposite operation, removal, and it is
    reached only by an operator asking for it. Note that it does NOT contradict the reasons
    `clear_estate_entry` stays off for the MIGRATION: those protect a key the user still
    wants (the rollback path back to a version that can only read `config.json`). A revoked
    key is not one the user still wants, and preserving a rollback to a version that would
    resurrect it would be the defect, not the feature.

    Raises `ApiKeyRevocationFailed` if the key can still be resolved afterwards. The check
    is a real second resolution rather than an assumption that the two writes worked - which
    is also what makes it catch a future fourth source for free.
    """
    name = (provider or "").strip().lower()
    if not name:
        raise ApiKeyRevocationFailed("No provider given; nothing was revoked.")

    problems = []
    try:
        clear_estate_entry(name)                                # 1. estate, see docstring
    except Exception as exc:                                    # noqa: BLE001 - reported, not swallowed
        problems.append(f"config.json: {exc}")
    try:
        _store().update(lambda data: data.pop(name, None))      # 2. encrypted store
    except Exception as exc:                                    # noqa: BLE001 - reported, not swallowed
        problems.append(f"encrypted store: {exc}")

    # Running agents must stop using it now, not after a restart. The critical-key list in
    # `Config.save` is a hardcoded six, so a seventh provider would notify nobody; announcing
    # by name here is provider-agnostic and reaches the tray's broadcast either way.
    _announce_change(name)

    try:
        leftover = resolve_api_key(name)
    except ApiKeyUnavailable as exc:
        problems.append(f"the store is unreadable, so the key cannot be confirmed gone: {exc}")
        leftover = ""
    if leftover:
        problems.append("the key still resolves after both writes")

    if problems:
        raise ApiKeyRevocationFailed(
            f"The API key for '{name}' was NOT fully revoked ({'; '.join(problems)}). "
            f"Treat it as still live: rotate it at the provider."
        )
