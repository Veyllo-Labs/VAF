# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The browser cookie store under at-rest encryption.

Live site cookies are auth tokens; ENCRYPTION_AT_REST.md carried this store on
its measured still-plaintext list with the constraint spelled out: browser_use
is handed a PATH it reads itself and auto-saves onto mid-run, so the agent lane
needs decrypt-to-temp around the run rather than a read/write swap. These tests
pin both lanes and the migration of pre-existing plaintext files.
"""
import json
import os
import sys
import time

import pytest

from vaf.core import data_files
import vaf.core.browser_interactive as bi
import vaf.tools.browser_agent as ba

SECRET_VALUE = "session-token-apfel-banane"


@pytest.fixture(autouse=True)
def _sandboxed_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("VAF_USER_SCOPE_ID", raising=False)
    data_files.reset_key_cache()
    yield
    data_files.reset_key_cache()


def _cookie(value=SECRET_VALUE):
    return {"name": "auth", "value": value, "domain": "bank.example", "path": "/",
            "expires": 4102444800.0, "httpOnly": True, "secure": True, "sameSite": "Strict"}


# ── interactive lane ──────────────────────────────────────────────────────

def test_exported_cookies_are_not_readable_on_disk():
    """MUTATION: write the plaintext json.dump again, and this goes red."""
    path = bi.browser_storage_state_path("scope-a", "default")
    bi._export_storage_cookies(path, [_cookie()])
    raw = open(path, "rb").read()
    assert raw.startswith(b"VAFENC1:")
    assert SECRET_VALUE.encode() not in raw
    assert b"bank.example" not in raw
    if sys.platform != "win32":
        assert oct(os.stat(path).st_mode)[-3:] == "600"
    loaded = bi._load_storage_cookies(path)
    assert [c["value"] for c in loaded] == [SECRET_VALUE]


def test_a_plaintext_legacy_store_still_loads():
    path = bi.browser_storage_state_path("scope-a", "default")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"cookies": [_cookie("legacy")], "origins": []}, f)
    assert [c["value"] for c in bi._load_storage_cookies(path)] == ["legacy"]


# ── agent lane: decrypt-to-temp around the run ────────────────────────────

def test_the_run_is_staged_on_a_temp_never_the_real_store():
    path = bi.browser_storage_state_path("scope-a", "job")
    bi._export_storage_cookies(path, [_cookie()])
    tmp = ba._decrypt_session_to_tmp(path)
    assert tmp is not None and os.path.exists(tmp)
    assert os.path.basename(tmp).startswith(".run-") and tmp.endswith(".tmp.json")
    assert os.path.dirname(tmp) == os.path.dirname(path)
    if sys.platform != "win32":
        assert oct(os.stat(tmp).st_mode)[-3:] == "600"
    # The temp is what browser_use may read: plaintext, the real store is not.
    state = json.load(open(tmp, encoding="utf-8"))
    assert state["cookies"][0]["value"] == SECRET_VALUE
    os.unlink(tmp)


def test_a_first_login_gets_a_temp_path_without_a_file():
    path = bi.browser_storage_state_path("scope-a", "fresh")
    assert not os.path.exists(path)
    tmp = ba._decrypt_session_to_tmp(path)
    assert tmp is not None and not os.path.exists(tmp)


def test_an_unreadable_store_refuses_instead_of_starting_fresh(monkeypatch):
    """A run that silently starts 'fresh' over a store it failed to decrypt
    would export an empty state over the person's real logins at its end."""
    path = bi.browser_storage_state_path("scope-a", "job")
    bi._export_storage_cookies(path, [_cookie()])
    monkeypatch.setattr(data_files, "read_bytes",
                        lambda p: (_ for _ in ()).throw(RuntimeError("no key")))
    assert ba._decrypt_session_to_tmp(path) is None


def test_the_fold_back_encrypts_and_removes_the_temp():
    path = bi.browser_storage_state_path("scope-a", "job")
    tmp = os.path.join(os.path.dirname(path), ".run-deadbeef.tmp.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"cookies": [_cookie()], "origins": []}, f)
    ba._fold_session_tmp_back(tmp, path)
    assert not os.path.exists(tmp)
    raw = open(path, "rb").read()
    assert raw.startswith(b"VAFENC1:")
    assert [c["value"] for c in bi._load_storage_cookies(path)] == [SECRET_VALUE]


def test_stale_run_temps_are_swept_fresh_ones_kept():
    path = bi.browser_storage_state_path("scope-a", "default")
    d = os.path.dirname(path)
    stale = os.path.join(d, ".run-old.tmp.json")
    fresh = os.path.join(d, ".run-new.tmp.json")
    for p in (stale, fresh):
        open(p, "w").write("{}")
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    ba._sweep_stale_session_tmp(d)
    assert not os.path.exists(stale)
    assert os.path.exists(fresh)


# ── migration of pre-existing plaintext stores ────────────────────────────

def test_migration_encrypts_the_legacy_store_and_skips_run_temps(monkeypatch):
    path = bi.browser_storage_state_path("scope-a", "default")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"cookies": [_cookie()], "origins": []}, f)
    tmp = os.path.join(os.path.dirname(path), ".run-live.tmp.json")
    open(tmp, "w").write("{}")

    from vaf.core import at_rest_migration
    # The enforcement flip after a clean pass writes into the PROCESS-cached
    # Config and would poison every later plaintext-legacy test in the run;
    # this test measures the browser tree's migration, not the flip.
    monkeypatch.setattr(at_rest_migration, "_any_plaintext_left", lambda trees: True)
    report = at_rest_migration.run_once(force=True)
    assert report.get("browser_sessions", {}).get("migrated", 0) >= 1
    assert open(path, "rb").read().startswith(b"VAFENC1:")
    # The dot-prefixed staging file is deliberately outside the glob.
    assert open(tmp, "rb").read() == b"{}"
    # And the reader still answers after the migration.
    assert [c["value"] for c in bi._load_storage_cookies(path)] == [SECRET_VALUE]
