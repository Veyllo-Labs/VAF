# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A linked WhatsApp account's session directory is owner-only.

The directory IS the login: Baileys keeps the Signal-protocol state in it, one file per key.
Measured on a linked install: the directory was 0755 and 379 of its 381 files were 0644,
readable by every account on the machine; only creds.json was 0600. Node wrote them with the
default umask. Now both places that start Node go through one function that fixes what is
there and starts Node with umask 077, so what it writes next is born 0600.
"""
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from vaf.core.whatsapp_auth import harden_auth_dir

REPO = Path(__file__).resolve().parent.parent
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="chmod bits do not exist on Windows")


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


@posix_only
def test_existing_session_files_become_owner_only(tmp_path):
    auth = tmp_path / "whatsapp"
    auth.mkdir(mode=0o755)
    os.chmod(auth, 0o755)
    for name in ("creds.json", "pre-key-1.json", "session-4917.json", "app-state-sync-key-A.json"):
        (auth / name).write_text("{}", encoding="utf-8")
        os.chmod(auth / name, 0o644)
    outside = tmp_path / "elsewhere.json"
    outside.write_text("{}", encoding="utf-8")
    os.chmod(outside, 0o644)
    (auth / "link.json").symlink_to(outside)

    assert harden_auth_dir(auth) == 4
    assert _mode(auth) == 0o700
    assert all(_mode(auth / n) == 0o600 for n in ("creds.json", "pre-key-1.json", "session-4917.json"))
    assert _mode(outside) == 0o644, "a symlink out of the directory is not followed"


def test_a_missing_directory_is_left_alone(tmp_path):
    assert harden_auth_dir(tmp_path / "never-linked") == 0
    assert not (tmp_path / "never-linked").exists()


@posix_only
def test_a_file_the_bridge_writes_is_born_owner_only(tmp_path):
    """Measured, not asserted from the kwargs: a real child started through the one spawn
    function writes a file into the session directory, and the file comes out 0600."""
    from vaf.api.whatsapp_bridge import spawn_node_bridge

    script = tmp_path / "fake-bridge.py"
    script.write_text(
        "import sys, pathlib\n"
        "d = pathlib.Path(sys.argv[sys.argv.index('--auth-dir') + 1])\n"
        "(d / 'session-new.json').write_text('{}')\n"
        "(d / 'sub').mkdir()\n",
        encoding="utf-8",
    )
    auth = tmp_path / "whatsapp"
    old = os.umask(0o022)       # the permissive default the bridge used to inherit
    try:
        proc = spawn_node_bridge(sys.executable, script, auth)
        proc.communicate(timeout=30)
    finally:
        os.umask(old)
    assert proc.returncode == 0
    assert _mode(auth / "session-new.json") == 0o600
    assert _mode(auth / "sub") == 0o700
    assert _mode(auth) == 0o700


def test_node_is_started_in_exactly_one_place():
    """Two hand-written spawns of wa-bridge.js is how the QR link could have kept the old
    umask while the running bridge got the new one."""
    tracked = subprocess.run(["git", "ls-files", "-z", "vaf/"], cwd=REPO, capture_output=True,
                             check=True).stdout.decode("utf-8", "ignore").split("\0")
    spawns = [rel for rel in tracked if rel.endswith(".py") and (REPO / rel).is_file()
              and '"--auth-dir"' in (REPO / rel).read_text(encoding="utf-8")]
    assert spawns == ["vaf/api/whatsapp_bridge.py"], spawns
    src = (REPO / "vaf" / "api" / "whatsapp_bridge.py").read_text(encoding="utf-8")
    assert src.count('"--auth-dir"') == 1
    routes = (REPO / "vaf" / "api" / "whatsapp_routes.py").read_text(encoding="utf-8")
    assert "spawn_node_bridge(node, wa_js, auth_dir)" in routes, "the QR link goes through it too"


def _loose_session(auth: Path) -> None:
    auth.mkdir(parents=True, exist_ok=True)
    os.chmod(auth, 0o755)
    for name in ("creds.json", "session-4917.json", "pre-key-7.json"):
        (auth / name).write_text("{}", encoding="utf-8")
        os.chmod(auth / name, 0o644)


@posix_only
def test_the_qr_flow_protects_the_directory_even_when_it_ends_early(tmp_path, monkeypatch):
    """The flow can end before Node is spawned (no Node, or the dependency install fails),
    and the spawn is the other place the directory is corrected."""
    import vaf.api.whatsapp_routes as routes
    import vaf.core.whatsapp_auth as wa

    auth = tmp_path / "users" / "alice" / "whatsapp"
    _loose_session(auth)
    monkeypatch.setattr(wa, "get_whatsapp_auth_dir", lambda username: auth)
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)     # no Node: the flow returns early
    routes._run_qr_login("alice")
    assert _mode(auth) == 0o700
    assert all(_mode(auth / n) == 0o600 for n in ("creds.json", "session-4917.json", "pre-key-7.json"))


@pytest.mark.parametrize("breakage", ["occupied", "unlistable"])
def test_a_directory_that_cannot_be_prepared_ends_the_qr_flow_with_an_answer(tmp_path, monkeypatch, breakage):
    """The flow runs in a thread: an exception while preparing the directory left nothing in
    the state the setup screen polls, and the screen waited for a QR code that never came.
    It ends with an error the screen shows instead, and Node is never started."""
    import vaf.api.whatsapp_bridge as bridge
    import vaf.api.whatsapp_routes as routes
    import vaf.core.whatsapp_auth as wa

    auth = tmp_path / "users" / "alice" / "whatsapp"
    if breakage == "occupied":
        auth.parent.mkdir(parents=True)
        auth.write_text("not a directory", encoding="utf-8")        # mkdir cannot make it
    else:
        def refuse(path):                                           # another account's directory
            raise PermissionError(13, "Permission denied", str(path))
        monkeypatch.setattr(wa, "harden_auth_dir", refuse)
    monkeypatch.setattr(wa, "get_whatsapp_auth_dir", lambda username: auth)
    spawned = []
    monkeypatch.setattr(bridge, "spawn_node_bridge", lambda *a: spawned.append(a))
    monkeypatch.setattr(routes, "_qr_state", {})

    routes._run_qr_login("alice")

    assert "could not be prepared" in routes._qr_state["alice"]["error"]
    assert spawned == []


@posix_only
def test_every_linked_account_is_protected_at_startup_whatever_the_switch_says(tmp_path, monkeypatch):
    """The bridge only starts Node when WhatsApp is switched on; a linked account that is
    switched off still holds a working login on disk."""
    from vaf.core.config import Config
    from vaf.core.whatsapp_auth import harden_linked_auth_dirs

    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    _loose_session(tmp_path / "users" / "alice" / "whatsapp")
    _loose_session(tmp_path / "users" / "bob" / "whatsapp")
    unlinked = tmp_path / "users" / "carol" / "whatsapp"
    unlinked.mkdir(parents=True)
    (unlinked / "notes.json").write_text("{}", encoding="utf-8")
    os.chmod(unlinked / "notes.json", 0o644)

    assert harden_linked_auth_dirs() == 6
    for who in ("alice", "bob"):
        auth = tmp_path / "users" / who / "whatsapp"
        assert _mode(auth) == 0o700 and _mode(auth / "session-4917.json") == 0o600
    assert _mode(unlinked / "notes.json") == 0o644, "a directory without a login is not an account"


def test_the_server_runs_the_startup_pass_before_the_switch_decides():
    src = (REPO / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    sweep = src.index("harden_linked_auth_dirs()")
    gate = src.index('if isinstance(whatsapp_config, dict) and whatsapp_config.get("enabled"):')
    assert sweep < gate, "the pass runs outside the 'switched on' branch"
