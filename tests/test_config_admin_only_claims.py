# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Every "Admin-only" the schema doc claims must be one the code enforces.

A config key's NAME decides who may write it (`Config.is_global_config_key`,
consulted by the HTTP and WebSocket saves through `filter_for_non_admin`). The
documentation is where that decision is announced - and a row that says
"Admin-only" while the key is on neither list documents a protection that does
not exist, which is worse than saying nothing.

That is not hypothetical: `voice_semantic_endpoint_enabled` shipped with the
doc row ending in "Admin-only/global." while any non-admin LAN account could
flip it, arming a server-side microphone stream and a model download for the
whole instance. The doc was the only thing enforcing it.

This guard covers the CLASS (33 claims at the time of writing) rather than that
one key, because the next key will be added by someone reading the doc, not
this test.
"""
import pathlib
import re

import pytest

from vaf.core.config import Config

_SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "docs" / "setup" / "CONFIG_SCHEMA.md"
_ROW = re.compile(r"^\|\s*`([a-z0-9_]+)`\s*\|(.*)$", re.M)


def _admin_only_claims():
    doc = _SCHEMA.read_text(encoding="utf-8")
    return [(key, rest) for key, rest in _ROW.findall(doc)
            if re.search(r"admin[- ]only", rest, re.I)]


def test_the_doc_actually_makes_admin_only_claims():
    """A parser that silently matches nothing would make this file pass while
    checking nothing - the failure mode of every source-scanning guard."""
    claims = _admin_only_claims()
    assert len(claims) >= 25, f"only {len(claims)} admin-only rows parsed; did the table format change?"


@pytest.mark.parametrize("key", [k for k, _ in _admin_only_claims()])
def test_an_admin_only_claim_is_enforced_in_code(key):
    """MUTATION: remove a key from GLOBAL_CONFIG_KEYS (or drop its prefix) and
    its case goes red while the doc still promises the protection."""
    assert Config.is_global_config_key(key), (
        f"CONFIG_SCHEMA.md calls `{key}` admin-only, but "
        f"Config.is_global_config_key says any non-admin may write it")
    assert key not in Config.filter_for_non_admin({key: True, "theme": "dark"}), (
        f"`{key}` survives the non-admin save filter")


@pytest.mark.parametrize("key", [k for k, _ in _admin_only_claims()])
def test_an_admin_only_key_is_a_real_key(key):
    """A typo in the doc would otherwise document a protection for nothing."""
    assert key in Config.DEFAULTS, f"`{key}` is documented but is not a config key"
