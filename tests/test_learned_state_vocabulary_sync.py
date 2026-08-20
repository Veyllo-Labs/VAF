# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The learned-state vocabulary is a registry with a copy in the web UI.

`vaf/whare_wananga/store.py` owns the four states; `SettingsModal.tsx` renders a
badge per state and folds them into the header's Learned/Unlearned counters. A
fifth state added in Python without a matching UI branch would be silently
counted as unlearned and rendered without a badge, which is exactly the kind of
drift Rule 2 asks for a test rather than a prose reminder.
"""
import re
from pathlib import Path

import vaf.whare_wananga.store as store

_SETTINGS = Path(__file__).resolve().parents[1] / "web" / "components" / "SettingsModal.tsx"


def _states():
    return {name: value for name, value in vars(store).items()
            if name.startswith("STATE_") and isinstance(value, str)}


def _tsx():
    return _SETTINGS.read_bytes().decode("utf-8")


def test_the_vocabulary_is_the_four_states_the_ui_knows():
    # Pinned as literals, not as the constants' names: a test that reads the
    # constant follows a rename and stays green while the UI breaks.
    assert set(_states().values()) == {"unlearned", "learning", "learned", "stale"}


def test_every_state_the_store_can_return_is_handled_in_the_ui():
    tsx = _tsx()
    missing = [value for value in _states().values() if f"'{value}'" not in tsx]
    assert not missing, (
        f"SettingsModal.tsx has no branch for {missing}. A state the UI does not "
        "know renders without a badge and is counted as unlearned.")


def test_learned_state_derives_from_learned_state_itself():
    # Whatever learned_state() returns for a tool must be what the UI reads;
    # a second field would be a fork of the framework's own answer.
    assert "learned_state" in _tsx()
    assert store.learned_state("a-tool-that-was-never-trained") == store.STATE_UNLEARNED


def test_the_counters_count_learned_strictly_and_take_the_rest_as_unlearned():
    """The fold rule, pinned because the tempting inversion silently under-counts.

    `_attach_learned_states` swallows every failure in one try/except, so the
    field can be ABSENT on every entry rather than defaulted. Counting
    `=== 'unlearned'` would then report zero unlearned tools and a total that
    does not add up; counting learned strictly and taking the remainder is
    correct whatever the field is missing or set to. It also puts `stale` on the
    unlearned side, which is the framework's own wording in invalidate_stale.
    """
    tsx = _tsx()
    assert re.search(r"toolsLearnedCount\s*=\s*tools\.filter\(\(t\) =>\s*"
                     r"t\.learned_state === 'learned'\)\.length", tsx), \
        "the learned counter no longer tests strict equality on 'learned'"
    assert re.search(r"toolsUnlearnedCount\s*=\s*tools\.length\s*-\s*toolsLearnedCount", tsx), \
        "the unlearned counter is no longer the remainder, so the two can stop summing to the total"
    assert "learned_state === 'unlearned'" not in tsx, \
        "counting 'unlearned' directly under-counts whenever the field is absent"


def test_stale_keeps_its_own_badge_even_though_the_counter_folds_it():
    # The fold is a counting decision, not a rendering one: a stale tool still
    # says so per-tool, it just does not count as learned.
    assert "'stale'" in _tsx()
    assert store.STATE_STALE == "stale"
