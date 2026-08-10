# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The switches that protect the data are not personal preferences.

A config key's NAME decides who may write it: `Config.is_global_config_key`
answers from `GLOBAL_CONFIG_KEYS` plus a prefix list, and both the HTTP save
(`config_routes.py`) and the WebSocket save run every non-admin body through
`filter_for_non_admin`. A key that is in neither list is writable by any
account on the LAN.

The at-rest round added six keys and put none of them on the list, so a
non-admin could have turned off the encryption of everyone's chats, opened the
tolerant read back up, removed the terminal password gate, or switched on the
log that writes the whole assembled prompt - decrypted memories included - to
disk. Each of those is an instance-wide security decision.

The test enumerates the six by name on purpose. A membership check written as
"every key added by this round" would need the round to be identifiable at
runtime, and a future seventh key would slip past a clever assertion just as
easily as it slipped past review.
"""
import pytest

from vaf.core.config import Config

AT_REST_POLICY_KEYS = (
    "file_encryption_enabled",
    "allow_plaintext_at_rest",
    "cli_password_gate",
    "prompt_log_full_enabled",
    "secure_store_kek_backend",
    "context_archive_max_age_days",
)


@pytest.mark.parametrize("key", AT_REST_POLICY_KEYS)
def test_the_key_is_admin_only(key):
    """MUTATION: drop one from GLOBAL_CONFIG_KEYS and its case goes red."""
    assert Config.is_global_config_key(key), (
        f"{key} decides at-rest protection for the whole instance and would be "
        f"writable by any non-admin account"
    )


@pytest.mark.parametrize("key", AT_REST_POLICY_KEYS)
def test_a_non_admin_save_cannot_carry_the_key(key):
    """The list is only as good as the filter that consults it."""
    body = {key: False, "theme": "dark"}

    filtered = Config.filter_for_non_admin(body)

    assert key not in filtered
    assert filtered.get("theme") == "dark", "the filter must not eat ordinary preferences"


def test_every_at_rest_key_really_exists():
    """A typo here would make the whole file pass while protecting nothing."""
    for key in AT_REST_POLICY_KEYS:
        assert key in Config.DEFAULTS, f"{key} is not a real config key"
