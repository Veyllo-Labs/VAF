# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One builder for the WhatsApp session id (vaf/core/messaging_connections.py).

`whatsapp_<user>_<digits>` is the session AND the memory namespace of a chat. The recipe was
hand-rolled at eight sites; the Composer derives the namespace key from the dashboard's chat
id while the bridge derives it from the resolved number, and a drifted copy means the Composer
looks under a name the bridge never wrote, with no error. So the spelling exists once and the
guard below keeps it that way.

MUTATION: re-inline an f-string at any converted site and the spelling guard goes red.
"""
import re
from pathlib import Path

from vaf.core.messaging_connections import whatsapp_session_id

_VAF = Path(__file__).resolve().parent.parent / "vaf"


def test_every_shape_of_an_endpoint_yields_the_bridges_id():
    assert whatsapp_session_id("alice", "+49 170 0000042") == "whatsapp_alice_491700000042"
    assert whatsapp_session_id("alice", "491700000042@s.whatsapp.net") == "whatsapp_alice_491700000042"
    assert whatsapp_session_id("alice", "491700000042:7@s.whatsapp.net") == "whatsapp_alice_491700000042"
    assert whatsapp_session_id("alice", "12345@lid") == "whatsapp_alice_12345"
    assert whatsapp_session_id("alice", "491700000042") == "whatsapp_alice_491700000042"
    assert whatsapp_session_id(" alice ", "+491700000042") == "whatsapp_alice_491700000042"


def test_the_fallback_names_the_owner_or_nothing():
    assert whatsapp_session_id("alice", "") == "whatsapp_alice_self"
    assert whatsapp_session_id("alice", "", fallback="unknown") == "whatsapp_alice_unknown"
    assert whatsapp_session_id("alice", "group@g.us", fallback="") == ""
    assert whatsapp_session_id(None, "+491700000042") == "whatsapp_admin_491700000042"
    assert whatsapp_session_id("", "+491700000042") == "whatsapp_admin_491700000042"


def test_the_spelling_exists_once():
    """The builder, and the sidebar's ownership prefix (a prefix, not an id) - nothing else."""
    hits = []
    for path in _VAF.rglob("*.py"):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'\b(?:[fF][rR]?|[rR][fF])["\']{1,3}whatsapp_\{', line):
                hits.append(f"{path.relative_to(_VAF.parent).as_posix()}:{no}")
    assert sorted(hits) == sorted([
        "vaf/core/messaging_connections.py:" + next(
            str(no) for no, line in enumerate(
                (_VAF / "core" / "messaging_connections.py").read_text(encoding="utf-8").splitlines(), 1)
            if 'return f"whatsapp_{uname}_{key}"' in line),
        "vaf/api/whatsapp_routes.py:" + next(
            str(no) for no, line in enumerate(
                (_VAF / "api" / "whatsapp_routes.py").read_text(encoding="utf-8").splitlines(), 1)
            if "prefix = f\"whatsapp_{" in line),
    ]), f"the session-id recipe is spelled by hand again: {hits}"
