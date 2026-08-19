# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A tool result that IS the deliverable reaches the model whole.

The funnel caps every result (2000 chars by default) to protect the model's context
from unbounded grep output - a good default with one measured failure mode: tools
whose result is a hand-over artifact. A live incident put three of them in one turn:
a loaded skill body cut mid-instruction, the same skill's source cut the same way,
and an invitation briefing cut inside a block the result text itself orders the model
to pass on "unchanged and complete". The agent refused to hand over the torn half
(correctly - the text told it to) and spent the turn hunting the rest in encrypted
stores and capped logs, until the model collapsed.

`BaseTool.result_is_deliverable` is the declared exemption; the flag is a promise in
return that the tool keeps its own output bounded. Both halves are pinned here.
"""
from vaf.core.tool_dispatch import EVENT_RESULT_CHARS, ToolCaller
from vaf.tools.base import BaseTool
from vaf.tools.read_skill import ReadSkillTool
from vaf.tools.room_tools import RoomInviteTool
from vaf.tools.use_skill import SKILL_BODY_BUDGET_CHARS, UseSkillTool

LONG = "x" * 5000


class _Plain(BaseTool):
    name = "plain_probe"
    description = "returns more than the cap"
    permission_level = "read"

    def run(self, **kwargs):
        return LONG


class _Deliverable(_Plain):
    name = "deliverable_probe"
    result_is_deliverable = True


def test_a_declared_deliverable_is_not_cut_and_everything_else_still_is():
    """MUTATION: drop the `result_is_deliverable` clause in `ToolCaller.execute`.

    Both halves in one test on purpose - the exemption without the default would
    re-open the funnel for every 100k grep, and the default without the exemption
    is the incident above.
    """
    caller = ToolCaller({t.name: t for t in (_Plain(), _Deliverable())})

    assert caller.execute("deliverable_probe", {}) == LONG
    capped = caller.execute("plain_probe", {})
    assert "[Output Truncated" in capped and len(capped) < len(LONG)


def test_observation_never_inherits_the_exemption():
    """MUTATION: emit the raw result on the event stream for deliverable tools.

    The exemption is the MODEL's: the tool_end event feeds logs and live views,
    and `event_result` caps it independently so observation stays bounded no
    matter what the funnel hands the model.
    """
    events = []
    caller = ToolCaller({"deliverable_probe": _Deliverable()}, on_event=events.append)
    caller.execute("deliverable_probe", {})

    ends = [e for e in events if e.get("type") == "tool_end"]
    assert ends, "the tool_end event itself must still be emitted"
    assert all(len(e.get("result") or "") <= EVENT_RESULT_CHARS + 40 for e in ends)


def test_the_hand_over_tools_declare_it_and_nothing_declares_it_by_accident():
    """MUTATION: remove the declaration from any one of the three tools.

    The three are exactly the ones the incident measured: the skill body an agent
    is meant to FOLLOW, the skill source it inspects before editing, and the
    invitation briefing it must pass on whole. The default stays False so a new
    tool opts in deliberately or not at all.
    """
    assert BaseTool.result_is_deliverable is False
    for tool_cls in (UseSkillTool, ReadSkillTool, RoomInviteTool):
        assert tool_cls.result_is_deliverable is True, tool_cls.name


def test_read_skill_keeps_its_own_promise(monkeypatch):
    """MUTATION: drop the source budget in read_skill.

    Nothing limits a skill's size at creation, so without its own bound the
    exemption would ride an unbounded source straight into the model's context.
    """
    from vaf.core import skills_registry

    monkeypatch.setattr(skills_registry, "is_skill_visible_to_user", lambda *a: True)
    monkeypatch.setattr(skills_registry, "can_user_edit_skill", lambda *a: True)
    monkeypatch.setattr(skills_registry, "get_skill_md_source",
                        lambda sid: "y" * (SKILL_BODY_BUDGET_CHARS * 3))

    out = ReadSkillTool().run(skill_id="giant")
    assert len(out) < SKILL_BODY_BUDGET_CHARS + 300
    assert "truncated at the skill body budget" in out
