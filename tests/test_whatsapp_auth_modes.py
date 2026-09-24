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
