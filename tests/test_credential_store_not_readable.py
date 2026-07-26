# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: the file tools must never open VAF's own data directory.

``~/.vaf`` is the credential store. On a live instance it holds config.json with every
``api_key_*`` and the JWT secret (plus ``.bak`` copies of the same), ``secrets/``, ``ssl/``
private keys, ``browser_sessions/`` with live logged-in cookies, ``speaker_profiles/`` with
voice biometrics, and ``sessions/`` with every conversation.

``is_safe_path`` blocked ``.ssh``, ``.env`` and ``id_rsa`` from the start - the instinct was
there - but ``.vaf`` was never on the list, so ``read_file`` opened the key store. That is not
a per-user permission question: a key extracted through a chat prompt keeps working outside
VAF and outlives any access this instance could revoke. It is therefore denied for EVERYONE,
the machine owner included.

The polarity is deny-by-default with one explicit exception, so a folder added under ``~/.vaf``
tomorrow is protected without anyone remembering to list it. The exception is ``skills/``,
because ``use_skill`` hands the model absolute paths to a skill's bundled files and tells it to
open them with ``read_file`` (vaf/tools/use_skill.py:33 and :92) - blocking that would break
every skill shipping reference material.
"""
from pathlib import Path

import pytest

from vaf.core.platform import Platform
from vaf.tools.filesystem import is_safe_path

VAF_DIR = Path(Platform.vaf_dir())


@pytest.mark.parametrize("relative", [
    "config.json",                    # every api_key_*, the JWT secret
    "config.json.bak-rls-cutover",    # a real backup seen on a live instance: same keys
    "secrets",
    "secrets/anything.enc",
    "ssl/key.pem",
    "browser_sessions/session.json",  # live logged-in cookies
    "speaker_profiles/owner.npy",     # voice biometrics
    "sessions/some-session.json",     # conversations
    "logs/queue.log",
])
def test_credential_store_is_denied(relative):
    ok, _ = is_safe_path(str(VAF_DIR / relative))
    assert ok is False, f"{relative} under the VAF data dir must not be readable"


def test_the_data_dir_itself_is_denied():
    ok, _ = is_safe_path(str(VAF_DIR))
    assert ok is False


def test_a_new_folder_under_the_data_dir_is_denied_without_being_listed():
    """The point of deny-by-default: nobody has to remember to add it."""
    ok, _ = is_safe_path(str(VAF_DIR / "some_future_feature" / "tokens.json"))
    assert ok is False


def test_user_created_content_stays_reachable():
    """Skills and workflows are user/agent-created content, not credentials. They are the
    same category as the custom tools in data_dir/custom_tools, which live outside ~/.vaf and
    were never affected - so this block must not treat them differently just because of where
    they happen to sit. Access to them is governed by the per-user jail like anything else.

    Concretely: use_skill hands the model absolute paths to a skill's bundled files and tells
    it to open them with read_file (use_skill.py:33, :92).
    """
    for p in (
        VAF_DIR / "skills", VAF_DIR / "skills" / "demo" / "reference.md",
        VAF_DIR / "workflows", VAF_DIR / "workflows" / "my_flow.py",
    ):
        ok, _ = is_safe_path(str(p))
        assert ok is True, f"{p} is user content and must stay reachable"


def test_ordinary_paths_are_unaffected():
    """The block must not spill over: this is about one directory, not about home."""
    home = Path.home()
    for p in (home / "Documents", home / "Downloads", home / "Documents" / "VAF_Projects"):
        ok, _ = is_safe_path(str(p))
        assert ok is True, f"{p} must remain reachable"


def test_a_dot_vaf_folder_inside_a_user_project_is_also_denied():
    """Component match, deliberately: a stray .vaf directory anywhere is treated as a data
    dir rather than risking a miss. Fail-closed is the right side to err on here."""
    ok, _ = is_safe_path(str(Path.home() / "Documents" / "myproject" / ".vaf" / "config.json"))
    assert ok is False
