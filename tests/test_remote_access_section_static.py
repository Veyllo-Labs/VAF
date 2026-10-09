# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The remote-access section's lockout confirmation belongs to one change only.

web/ has no JS test runner, so the rule is pinned at the source: a new, unconfirmed save
drops a confirmation still on screen BEFORE it is sent. Otherwise "Save anyway" stayed
up after a later edit failed (a refused entry, a lost connection) and saved the older
change the person had moved on from.
"""
import re
from pathlib import Path

SECTION = Path(__file__).resolve().parents[1] / "web/components/settings/RemoteAccessSection.tsx"


def test_an_unconfirmed_save_clears_the_pending_confirmation_before_it_is_sent():
    """MUTATION: drop the `if (!confirm) setLockout(null)` line - red."""
    source = SECTION.read_text(encoding="utf-8")
    save = source[source.index("const save = async"):source.index("const copyUrl")]
    clear = re.search(r"if \(!confirm\) setLockout\(null\);", save)
    send = save.find("await fetch(")
    assert clear and send != -1 and clear.start() < send, (
        "an unconfirmed save must drop the pending lockout confirmation before the request")



def test_adding_a_network_waits_for_a_running_save():
    """MUTATION: drop the busy guard from addOwn - red."""
    source = SECTION.read_text(encoding="utf-8")
    add = source[source.index("const addOwn"):source.index("return (")]
    guard = add.find("if (busy) return;")
    call = add.find("save(")
    assert guard != -1 and guard < call
