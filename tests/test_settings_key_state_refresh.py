# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Settings key-state line has to be refetched, and there is no test runner to prove it.

WHAT HAPPENED (live, owner-found, immediately after the state line shipped). A key was
entered and saved; the line underneath kept reading "No key stored". The key was in fact
stored - measured in the real encrypted store - so the write path was fine and the DISPLAY
was stale. `SettingsModal` is rendered unconditionally by the page, so closing it does not
unmount the component: the fetched state survived a close, and an effect keyed on the active
tab alone never ran again. The wrong answer was therefore permanent until a tab switch or a
page reload, not momentary.

Note what this is: the same failure the round had just written into tests/README.md - "when
a change moves a data source, does the SURFACE still show the same thing" - hit one layer
further in, in the very component built to answer it. The first version proved the endpoint
reports the state and never that the component asks again.

WHY THIS FILE IS WEAKER THAN IT SHOULD BE, said plainly rather than dressed up. `web/` has no
test runner at all: no jest, no vitest, no `test` script, and the one `__tests__` file present
is not executed by anything. A React effect's dependencies cannot be measured here. What is
left is a structural assertion over the source, which is a downgrade from a behavioural test
and is recorded as such - it can show the wiring EXISTS and can never show it WORKS. It reads
the parsed shape rather than a substring, because the neighbouring failure class in
tests/README.md is a guard that matched prose.

If a runner is ever added to `web/`, this file should be replaced by a real one: mount the
component, save, and assert the fetch happened.
"""
import re
from pathlib import Path

import pytest

COMPONENT = Path(__file__).resolve().parents[1] / "web" / "components" / "SettingsModal.tsx"
SOURCE = COMPONENT.read_text(encoding="utf-8")


def _code_lines(text: str) -> str:
    """Drop comment LINES so prose can neither satisfy nor break an assertion.

    Line-level on purpose. The first version deleted `/* ... */` REGIONS with a non-greedy
    regex and removed 35,000 characters of this file, including the fetch it was meant to
    find - a `/*` inside a string paired with a distant `*/`. It was red on correct source,
    which is the one failure a guard cannot have: it measures nothing in either direction.
    Stripping whole lines that begin a comment cannot run away like that, and it still keeps
    the explanatory comments in this component from satisfying an assertion about its code.
    """
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
            continue
        kept.append(line)
    return "\n".join(kept)


CODE = _code_lines(SOURCE)


def test_the_key_state_is_fetched_from_the_endpoint_that_reports_it():
    """The state line has a source at all - the floor for everything below.

    Scoped to the READER rather than to the file. A bare search for the path was satisfied by
    the DELETE call, which names the same endpoint, so removing the GET entirely left this
    green - the assertion was measuring that the revocation button exists.
    """
    reader = re.search(r"const refreshStoredKeys = useCallback\([\s\S]*?\n    \}, \[\]\);", CODE)
    assert reader, "the stored-key reader is gone or no longer recognisable"
    assert re.search(r"fetch\(\s*['\"`]/api/config/api-keys['\"`]", reader.group(0)), (
        "Settings no longer asks which providers have a key; the fields cannot show state"
    )


@pytest.mark.parametrize("trigger", ["isOpen", "activeTab"])
def test_the_refresh_effect_reruns_on_both_triggers(trigger):
    """`isOpen` is the one that was missing, and its absence made the staleness permanent.

    The component stays mounted while closed, so without it the fetched state outlives every
    close and the effect never fires again.
    """
    effect = re.search(r"useEffect\(\(\) => \{[^}]*?refreshStoredKeys\(\);\s*\}, \[([^\]]*)\]", CODE)
    assert effect, "the stored-key refresh effect is gone or no longer recognisable"
    deps = [d.strip() for d in effect.group(1).split(",")]
    assert trigger in deps, f"the refresh effect does not re-run on {trigger}: deps are {deps}"


def test_saving_refreshes_the_key_state():
    """A save changes the stored keys and nothing else in this component can notice.

    The keys go into the encrypted store, so the config that comes back after a save carries
    no trace of them - there is no state change to react to except this explicit call.
    """
    handler = re.search(r"const handleSave = \(\) => \{[\s\S]*?\n    \};", CODE)
    assert handler, "handleSave is gone or no longer recognisable"
    assert "refreshStoredKeys()" in handler.group(0), (
        "a save does not refresh the key state, so the line under the field keeps reporting "
        "what was true before the save"
    )


def test_a_filled_field_is_never_labelled_as_having_no_key():
    """"No key stored" under a box the user just typed into is untrue in the other direction.

    The whole state line exists because a page was telling people they had configured
    nothing; replacing one false claim with its mirror image would not be an improvement.
    """
    assert "keyPendingSave" in CODE, "there is no state for a typed-but-unsaved key"
    assert re.search(r"const pending = !isSet && ", CODE), (
        "the pending state is not derived from the local field, so it cannot mean "
        "'typed but not stored'"
    )
