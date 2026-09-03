# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The stores that sat open beside the ones that did not.

Measured 2026-09-03: ~/.vaf/sessions was 0700 with 0600 files, while ~/.vaf/automations,
~/.vaf/logs and ~/.vaf/automation_planner next to it were 0755 with 0644 files. An
automation's prompt is user-authored natural language - the same content class as a chat
message - so a second local account could read what the first one had asked its agent to
do every morning, and when.

Modes are the half that can be applied today. Encryption is NOT applied to these stores,
deliberately: their writers use plain json.load/json.dump rather than the encrypted seam,
so encrypting them would lock their own loaders out. Bringing them under it means
converting those writers first, which is its own change. This file pins the half that is
here and the reason the other half is not, so the next reader does not mistake the gap
for an oversight.
"""
import json
import os
import stat
import sys

import pytest

from vaf.core import at_rest_migration
from vaf.core.platform import Platform

pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="POSIX modes; Windows hardening is a documented no-op")

STORES = ("automations", "automation_planner", "reminders", "logs",
          "user_profile_cache", "thinking_workspace")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A scratch home, and the enforcement branch held open.

    An empty scratch home looks to `run_once` like a machine where every store is already
    ciphertext, so it flips `allow_plaintext_at_rest` to False in the PROCESS-WIDE config
    and every later test that reads a plaintext legacy file fails. The same trap is
    already handled the same way in tests/test_browser_session_at_rest.py; this is that
    pattern, not a second one.
    """
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(at_rest_migration, "_done", False)
    monkeypatch.setattr(at_rest_migration, "_any_plaintext_left", lambda trees: True)
    return tmp_path


def _world_readable(path):
    return bool(stat.S_IMODE(path.stat().st_mode) & (stat.S_IRWXG | stat.S_IRWXO))


def test_a_scheduled_store_is_closed_to_a_second_local_account(home):
    """MUTATION: leave the scheduled stores out of the sweep.

    The prompt of an automation says what the person asked for and the record says when
    it runs. Both were readable by any account on the machine.
    """
    store = home / "automations" / "scope-a"
    store.mkdir(parents=True)
    os.chmod(home / "automations", 0o755)
    task = store / "a1.json"
    task.write_text(json.dumps({"id": "a1", "prompt": "read my mail and summarise it"}),
                    encoding="utf-8")
    os.chmod(task, 0o644)
    assert _world_readable(task), "the fixture did not reproduce the open state"

    at_rest_migration.run_once(force=True)

    assert not _world_readable(home / "automations"), "the directory is still listable"
    assert not _world_readable(store), "the per-user subdirectory is still listable"
    assert not _world_readable(task), "the task file is still readable"
    assert json.loads(task.read_text(encoding="utf-8"))["id"] == "a1", (
        "the file was altered; modes are the only change here")


@pytest.mark.parametrize("name", STORES)
def test_every_named_store_is_swept(home, name):
    """MUTATION: drop one store from the list.

    Each of these holds user-authored content or a record of when the user was active,
    and each is written by a lane that does not read through the encrypted seam.
    """
    root = home / name
    root.mkdir(parents=True)
    os.chmod(root, 0o755)

    at_rest_migration.run_once(force=True)

    assert not _world_readable(root), f"{name} was left open"


def test_a_store_that_is_not_there_is_not_an_error(home):
    """Most installations have never used most of these. A missing directory is the
    ordinary case, not a failure to report."""
    report = at_rest_migration.run_once(force=True)
    assert isinstance(report, dict)
    assert "hardened" not in report or report["hardened"] == []


def test_the_sweep_says_which_stores_it_closed(home):
    for name in ("automations", "logs"):
        (home / name).mkdir(parents=True)
    report = at_rest_migration.run_once(force=True)
    assert set(report.get("hardened", [])) == {"automations", "logs"}


def test_these_stores_are_deliberately_not_encrypted_and_the_code_says_why():
    """MUTATION: add them to the `trees` dict instead.

    That dict encrypts as well as hardens. These writers use plain json.load, so
    encrypting them would make an automation unreadable to the scheduler that has to
    run it - a silent break of the feature, on the next start, for everybody.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "vaf" / "core"
              / "at_rest_migration.py").read_text(encoding="utf-8")
    trees = source.split("trees = {", 1)[1].split("}", 1)[0]
    for name in STORES:
        assert f'"{name}"' not in trees, (
            f"{name} joined the encrypting table; its loader does not read ciphertext")
    assert "do not read through the encrypted seam" in source, "the reason is not written down"
