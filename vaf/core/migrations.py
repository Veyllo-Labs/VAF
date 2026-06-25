# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Ordered, additive config migrations.

A config file carries `config_format_version` (default 1). When the config format
needs a breaking change, bump ``CONFIG_FORMAT_VERSION`` and append an entry to
``CONFIG_MIGRATIONS``. Migrations let an old on-disk config be upgraded in place
when a user updates VAF.

Rules for a migration function ``fn(config: dict) -> dict``:
  - **additive only**: it may ADD keys; it must NOT remove or rename a key that an
    older VAF still reads. A user can roll back after updating (see `vaf update`),
    and the old code must still understand the (now newer) config. Old code simply
    ignores keys it does not know, so adding is always safe; removing/renaming is not.
  - **pure and idempotent**: running it twice must be a no-op the second time.

v1 ships with no migrations — this is the seam, ready for the first format change.
"""
from typing import Callable, List, Tuple

CONFIG_FORMAT_VERSION = 1

# Applied in order to any config whose stored version is below the target.
# Example for a future change:
#   def _v1_to_v2(cfg): cfg.setdefault("new_key", "default"); return cfg
#   CONFIG_MIGRATIONS = [(2, _v1_to_v2)]
CONFIG_MIGRATIONS: List[Tuple[int, Callable[[dict], dict]]] = []


def run_config_migrations(config: dict, stored_version: int):
    """Apply migrations whose target version is greater than ``stored_version``.

    Returns ``(config, applied)`` where ``applied`` is the list of target versions run.
    """
    applied: List[int] = []
    for target, fn in CONFIG_MIGRATIONS:
        if target > stored_version:
            config = fn(config)
            applied.append(target)
    return config, applied
