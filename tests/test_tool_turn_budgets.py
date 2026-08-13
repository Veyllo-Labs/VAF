# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The loop-protection budgets are configuration, and only the admin holds them.

The 75-turn hard stop was a literal for its whole life, and `tool_loop_unlimited`
was read by the enforcement sites without ever being registered - a ghost key no
schema documented and no Settings surface could show. Both are registered now,
and both are admin-only for the same reason as the room-report keys: a limit its
own subject can raise is not a limit.
"""
from pathlib import Path

from vaf.core.config import Config

_AGENT = Path(__file__).parent.parent / "vaf" / "core" / "agent.py"


def test_the_budget_keys_exist_with_the_historical_defaults():
    assert Config.DEFAULTS["max_tool_turns_per_step"] == 75
    assert Config.DEFAULTS["tool_loop_unlimited"] is False


def test_both_budget_keys_are_admin_only():
    assert Config.is_global_config_key("max_tool_turns_per_step")
    assert Config.is_global_config_key("tool_loop_unlimited")
    assert Config.filter_for_non_admin({"max_tool_turns_per_step": 10_000}) == {}
    assert Config.filter_for_non_admin({"tool_loop_unlimited": True}) == {}


def test_the_agent_loop_reads_the_cap_from_config():
    """MUTATION: hardcode 75 again - this goes red.

    The soft reminder must keep its distance below the cap rather than sit on a
    second literal: min(50, cap-3) lands on the historical 50 at the default 75
    and scales down when an admin tightens the cap.
    """
    source = _AGENT.read_text(encoding="utf-8")
    assert 'int(Config.get("max_tool_turns_per_step", 75) or 75)' in source, (
        "the hard cap is a literal again instead of configuration")
    assert "min(50, MAX_TOOL_TURNS_PER_STEP - 3)" in source, (
        "the soft reminder no longer derives from the cap")


def test_the_soft_reminder_does_not_promise_a_hard_stop_that_cannot_come():
    """MUTATION: drop the _unlimited_loop wording guard - this goes red.

    With the unlimited switch on there is no hard stop, and a reminder that
    counts down to one is the framework lying to the model.
    """
    source = _AGENT.read_text(encoding="utf-8")
    assert "if not _unlimited_loop else" in source and "no hard stop configured" in source, (
        "the soft reminder promises a hard stop even when tool_loop_unlimited is on")
