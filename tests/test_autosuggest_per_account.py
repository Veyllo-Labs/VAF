# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Word suggestions come from what the SAME account typed, and the corpus is encrypted.

MEASURED BEFORE THE FIX: the web server kept one SmartAutoSuggest for the whole process. It
learned every chat message of every account into ~/.vaf/autosuggest.json (plaintext, 0644,
570 KB on a real install) and answered every connection's `get_autosuggest` from it. Whoever
typed "mein passwort ist" could be offered the next word another person had typed there.

MUTATION: hand every account the same suggester and the first test goes red; write the
corpus with plain json again and the second goes red; bring the shared instance back into
the web server and the third goes red.
"""
import os
import sys
from pathlib import Path

import pytest

import vaf.cli.autosuggest as auto_mod
from vaf.core import data_files
from vaf.core.platform import Platform

OWNER = "ab12cd34-owner"
ALICE = "alice-scope-0001"
BOB = "bob-scope-0002"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr("vaf.core.config.get_local_admin_scope_id", lambda: OWNER)
    monkeypatch.setattr(auto_mod, "_per_account", {})
    data_files.reset_key_cache()
    yield tmp_path
    data_files.reset_key_cache()


def test_one_account_never_gets_another_accounts_words():
    alice = auto_mod.autosuggest_for(ALICE)
    alice.learn("mein passwort ist hunter2geheim")
    assert alice.suggest("mein passwort ist ") == "hunter2geheim", "the account's own words still help"
    bob = auto_mod.autosuggest_for(BOB)
    assert bob is not alice
    assert bob.suggest("mein passwort ist ") != "hunter2geheim"
    assert auto_mod.autosuggest_for(None) is None, "no account: no corpus at all"


def test_the_corpus_is_encrypted_and_owner_only(home):
    alice = auto_mod.autosuggest_for(ALICE)
    alice.learn("die adresse lautet lindenstrasse")
    alice.flush()
    path = auto_mod.autosuggest_file(ALICE)
    raw = path.read_bytes()
    assert raw.startswith(b"VAFENC1:") and b"lindenstrasse" not in raw
    if sys.platform != "win32":
        assert oct(os.stat(path).st_mode)[-3:] == "600"
    auto_mod._per_account.clear()
    assert auto_mod.autosuggest_for(ALICE).suggest("die adresse lautet ") == "lindenstrasse"


def test_the_owner_keeps_the_file_the_terminal_lanes_use(home):
    """The terminal lanes are the machine owner; their corpus is the owner's web corpus."""
    assert auto_mod.autosuggest_file(OWNER) == home / "autosuggest.json"
    assert auto_mod.autosuggest_file(ALICE).parent == home / "autosuggest"
    assert auto_mod.autosuggest_file("../evil") is None, "a scope is a file name, never a path"


def test_the_web_server_asks_the_connections_account():
    src = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    assert "SmartAutoSuggest(" not in src, "a process-wide suggester is back in the web server"
    assert src.count("autosuggest_for(manager.get_connection_user(websocket))") == 2
