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

"""
from typing import Callable, List, Tuple

CONFIG_FORMAT_VERSION = 2


def _v2_lift_lexical_scan_cap(cfg: dict) -> dict:
    """Lift memory_hybrid_lexical_scan_limit from the old default 400 to 2000.

    The old cap predates chunk-at-rest encryption: lexical rows are scanned
    UNORDERED, so on any store larger than the cap the lexical lane saw an
    arbitrary subset (measured 2026-08-19 on a 1017-chunk store: 6 of 26
    golden questions lost, hit@1 12 -> 18 with the cap lifted). Full-config
    saves wrote the old default out explicitly, so a DEFAULTS change alone
    never reaches existing installs. Only the old default is rewritten; a
    deliberate custom value stays. Idempotent: after the rewrite the value
    is no longer 400.
    """
    if cfg.get("memory_hybrid_lexical_scan_limit") == 400:
        cfg["memory_hybrid_lexical_scan_limit"] = 2000
    return cfg


# Applied in order to any config whose stored version is below the target.
CONFIG_MIGRATIONS: List[Tuple[int, Callable[[dict], dict]]] = [
    (2, _v2_lift_lexical_scan_cap),
]


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
