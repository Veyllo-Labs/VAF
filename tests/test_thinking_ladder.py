# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The proactive ladder must always be able to reach its last rung.

A thinking run walks a ladder of rungs; each one that finds nothing calls `thinking_done`, and
the post-step keep-alive re-enters the loop at the next. The last rung is the fact-free
get-to-know question, and reaching it is the whole "a run never ends in silence" guarantee.

Two ways that guarantee breaks, both cheap to pin and both invisible in a normal test run:

1. The keep-alive compares `_proactive_step` against a bare number. Insert a rung and the number
   silently stops matching the end of the ladder, so a run that skipped the new rungs spins to
   the turn limit and never asks anything.
2. The turn budget stops covering the ladder. Every rung costs one turn on top of turn 0's
   gather, so a budget sized for a shorter ladder starves the last rung."""
import re
from pathlib import Path

import vaf.core.thinking_mode as tm
from vaf.core.config import Config

_SRC = Path(tm.__file__).read_text(encoding="utf-8")


def test_ladder_constants_are_ordered():
    assert tm._STEP_GROUNDED < tm._STEP_GETTO < tm._STEP_DONE


def test_keep_alive_bound_is_the_constant_not_a_literal():
    """The one line that decides whether the loop re-enters the ladder must name the end of the
    ladder, not a number that happens to equal it today."""
    assert "_proactive_step < _STEP_DONE" in _SRC
    assert not re.search(r"_proactive_step\s*<\s*\d", _SRC), \
        "the keep-alive bound is a bare number again - inserting a rung will silently strand the last one"


def test_no_rung_assignment_uses_a_bare_number():
    assert not re.search(r"_proactive_step\s*=\s*\d", _SRC), \
        "a rung sets _proactive_step to a literal; use a _STEP_* constant so the ladder stays consistent"


def test_turn_budget_covers_the_whole_ladder():
    """Turn 0 gathers, then each rung needs a turn. Reproduces the loop's own clamping so the
    assertion is about what actually runs, not about the raw config value."""
    max_turns = int(Config.get("thinking_max_turns", 8) or 8)
    max_turns = max(1, min(max_turns, 10))
    progress_threshold = max(2, int(Config.get("thinking_no_progress_turns", 5) or 5))
    max_turns = min(10, max(max_turns, progress_threshold + 2))

    rungs_needing_a_turn = tm._STEP_DONE          # every step value below DONE is a rung
    assert max_turns >= 1 + rungs_needing_a_turn, (
        f"turn budget {max_turns} cannot fit turn 0 plus {rungs_needing_a_turn} rungs - "
        "the get-to-know question would be unreachable"
    )
