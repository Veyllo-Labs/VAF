# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A messaging channel's credentials, kept in the encrypted key ring.

A channel's login token used to be a field of its config block (`telegram_config.bot_token`,
`discord_config.bot_token`), and that put it in two places it does not belong. It sat in
plaintext in config.json. And because the config API blanks only secret-named TOP-LEVEL keys,
the whole block, token included, travelled to every admin's browser on `GET /api/config`,
where the setup wizard even pre-filled it. The config API's own rule is that secrets are
write-only; this block was the exception nobody had listed.

The channel registry (vaf/core/channels.py) now says which fields of a channel's block are
credentials, and this module is the only place they are read or written:

- `channel_secret` answers from the ring. A value found in config.json wins and is MOVED
  into the ring on that read, then blanked there: pasting a token into config.json by hand
  is still a documented way to set a channel up, and it is also how an older release left
  it. The move is idempotent, so a bridge that wrote an old copy of its block back simply
  has it moved again. Nothing here ever invents a value: a missing token is "not
  configured", never a fresh random string.
- `absorb_channel_secrets` is the write side, called from `api_keys.absorb_config_keys`,
  which both save paths (the settings API and the WebSocket config update) already run. It
  takes the credential fields out of an incoming block and stores a non-empty value; an
  empty one means "not re-sent", because the admin view no longer sends the value and a
  save that echoes the block must not wipe the token.
- `redact_channel_secrets` blanks the fields in whatever is about to reach a browser.
- `clear_channel_secrets` is the explicit removal a disconnect needs; an empty field never
  removes anything.

The ring is `vaf.core.data_keyring`, which already holds the JWT signing secret and the
cache password. Whoever holds the recovery key AND a copy of data_keys.enc can open it,
tokens included - the same protection as every other secret in it.

Engine-internal, like the ring itself: not on the facade. A stranger's channel is a named
boundary in vaf/core/channels.py.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from vaf.core.channels import CHANNEL_SECRETS

logger = logging.getLogger("vaf.core.channel_secrets")


def ring_name(channel: str, field: str) -> str:
    """The key ring entry that holds one credential field of one channel."""
    return f"channel_{channel}_{field}"


def _fields(channel: str) -> Tuple[str, ...]:
    return CHANNEL_SECRETS.get(channel, ())


def _field(channel: str, field: Optional[str]) -> str:
    fields = _fields(channel)
    if not fields:
        raise ValueError(f"{channel!r} declares no credential fields in vaf/core/channels.py")
    chosen = field or fields[0]
    if chosen not in fields:
        raise ValueError(f"{chosen!r} is not a credential field of {channel!r}: {fields}")
    return chosen


def _config_value(channel: str, field: str) -> str:
    from vaf.core.config import Config
    block = Config.get(f"{channel}_config")
    if not isinstance(block, dict):
        return ""
    return str(block.get(field) or "").strip()


def _drop_from_config(channel: str, field: str) -> None:
    """Remove the field from the stored block, under the config file lock."""
    from vaf.core.config import Config
    with Config._locked():
        config = Config.load()
        block = config.get(f"{channel}_config")
        if isinstance(block, dict) and field in block:
            config[f"{channel}_config"] = {k: v for k, v in block.items() if k != field}
            Config.save(config)


def _move_into_ring(channel: str, field: str, value: str) -> bool:
    """Store the value in the ring and, once it reads back, take it out of config.json."""
    from vaf.core import data_keyring
    name = ring_name(channel, field)
    # Deliberately NO pre-keyring config backup here. That backup exists for data keys,
    # whose loss orphans ciphertext on a downgrade; it is a plaintext copy of config.json
    # and cancels the protection for as long as it exists. A bot token is recoverable from
    # the platform that issued it, so a downgrade costs one re-entry of the token, and no
    # second plaintext copy of it is written to take its place.
    data_keyring.set_data_secret(name, value)
    if data_keyring.peek_data_secret(name) != value:
        # Never blank the only copy on the strength of a write that did not stick.
        raise RuntimeError(f"{name} did not read back from the key ring")
    _drop_from_config(channel, field)
    logger.info("Moved %s_config.%s out of config.json into the key ring", channel, field)
    return True


def channel_secret(channel: str, field: Optional[str] = None) -> str:
    """The credential, or "" when the channel is not configured (or the ring is unreadable).

    A value in config.json wins and moves into the ring on this read. When the move fails,
    the value is still returned: the channel keeps working and the next read tries again,
    rather than the token being dropped or the bridge refusing to start."""
    field = _field(channel, field)
    legacy = _config_value(channel, field)
    if legacy:
        try:
            _move_into_ring(channel, field, legacy)
        except Exception as e:  # noqa: BLE001 - a failed move must not cost the channel its login
            logger.error("Could not move %s_config.%s into the key ring: %s", channel, field, e)
        return legacy
    from vaf.core import data_keyring
    try:
        return data_keyring.peek_data_secret(ring_name(channel, field))
    except Exception as e:  # noqa: BLE001 - an unreadable ring reads as "not configured"
        logger.error("The key ring cannot be read for %s_config.%s: %s", channel, field, e)
        return ""


def has_channel_secret(channel: str, field: Optional[str] = None) -> bool:
    return bool(channel_secret(channel, field))


def set_channel_secret(channel: str, field: str, value: str) -> None:
    """Store a new value in the ring (and make sure config.json no longer holds one)."""
    field = _field(channel, field)
    value = str(value or "").strip()
    if not value:
        raise ValueError("an empty value is not a credential; use clear_channel_secrets to remove one")
    from vaf.core import data_keyring
    name = ring_name(channel, field)
    data_keyring.set_data_secret(name, value)
    if data_keyring.peek_data_secret(name) != value:
        raise RuntimeError(f"{name} did not read back from the key ring")
    _drop_from_config(channel, field)


def clear_channel_secrets(channel: str) -> Dict[str, bool]:
    """Remove every credential of the channel, from the ring and from config.json.

    The one way a token goes away: a disconnect. Returns which fields held a value."""
    from vaf.core import data_keyring
    removed: Dict[str, bool] = {}
    for field in _fields(channel):
        had_config = bool(_config_value(channel, field))
        _drop_from_config(channel, field)
        removed[field] = data_keyring.delete_data_secret(ring_name(channel, field)) or had_config
    return removed


def absorb_channel_secrets(payload: Any) -> Any:
    """Take credential fields out of an incoming config payload and into the ring.

    Before a block is replaced, whatever config.json still holds for it is moved into the
    ring first: the admin view no longer sends the token, so the first save after an
    upgrade would otherwise replace the block with one that has none and the token would be
    gone without ever having been moved.

    That move RAISES when it fails, unlike the forgiving read in `channel_secret`, and the
    difference is the point. A read that cannot move the token still has it in config.json
    and hands it out; a save that cannot move it is about to overwrite the block that holds
    the only copy. Aborting the save keeps config.json as it was and tells the caller."""
    if not isinstance(payload, dict):
        return payload
    cleaned = dict(payload)
    for channel, fields in CHANNEL_SECRETS.items():
        key = f"{channel}_config"
        block = cleaned.get(key)
        if not isinstance(block, dict):
            continue
        block = dict(block)
        for field in fields:
            legacy = _config_value(channel, field)
            if legacy:
                _move_into_ring(channel, field, legacy)
            value = block.pop(field, None)
            if isinstance(value, str) and value.strip():
                set_channel_secret(channel, field, value)
        cleaned[key] = block
    return cleaned


def redact_channel_secrets(config: Any) -> Any:
    """A copy with every credential field blanked, for anything a browser will see.

    Blanked rather than removed, the same choice the config API makes for its top-level
    secrets, so client code reading `config.telegram_config.bot_token` keeps getting a
    defined empty string."""
    if not isinstance(config, dict):
        return config
    out = dict(config)
    for channel, fields in CHANNEL_SECRETS.items():
        key = f"{channel}_config"
        block = out.get(key)
        if isinstance(block, dict) and any(f in block for f in fields):
            out[key] = {k: ("" if k in fields else v) for k, v in block.items()}
    return out
