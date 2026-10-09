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
    start = source.index("const addOwn")
    add = source[start:source.index("return (", start)]
    guard = add.find("if (busy) return;")
    call = add.find("save(")
    assert guard != -1 and guard < call



def test_a_failed_load_keeps_the_section_and_offers_a_retry():
    """Only "not yours" (401/403) hides the section; any other failure keeps the last state
    and says so with a retry, instead of the panel vanishing without a word.
    MUTATION: blank the data on every non-OK answer again - red."""
    source = SECTION.read_text(encoding="utf-8")
    load = source[source.index("const load = useCallback"):source.index("useEffect(")]
    assert "res.status === 401 || res.status === 403" in load
    assert "if (!res.ok) { setLoadFailed(true); return; }" in load
    assert "t('retry')" in source and "t('loadFailed')" in source


def test_a_confirmed_add_empties_the_field_it_came_from():
    """An entry whose save needed "Save anyway" stayed in the field and could be sent again.
    MUTATION: drop the draft from the lockout - red."""
    source = SECTION.read_text(encoding="utf-8")
    assert "save([...data.allowed, entry], data.vpn_only, false, entry)" in source
    assert "draft: draftEntry" in source
    confirm = source[source.index("const typed = lockout.draft;"):]
    assert "setDraft(current => (current.trim() === typed ? '' : current))" in confirm[:400]
