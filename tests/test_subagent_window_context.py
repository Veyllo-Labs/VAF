# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An open sub-agent window is context the model gets to know about.

The interactive browser has told the agent for a while that the user is looking
at a page, and that it can take that same browser over. The other specialists
had nothing: someone could open the Coder window, type "fix the failing test"
and the model had no idea which work they meant.

Two things are different from the browser lane and both shape this design:

- The browser's fact is SERVER-side (it holds a CDP lease that can be read at
  send time). These windows exist only in the browser tab, so the client has to
  state it - which is why the value is validated against the known kinds before
  it is stored: it ends up inside a prompt, and an unchecked client string in a
  prompt is an injection lane.
- The block pairs the state with the AFFORDANCE. The browser block carries a
  measured note that naming where the person is, without saying what can be done
  about it, changed nothing about the model's behaviour.
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_RUNNER = _REPO / "vaf" / "core" / "headless_runner.py"
_SERVER = _REPO / "vaf" / "core" / "web_server.py"
_PAGE = _REPO / "web" / "app" / "page.tsx"

_KINDS = ("coder", "research", "document", "librarian")
_TOOLS = {"coder": "coding_agent", "research": "research_agent",
          "document": "document_agent", "librarian": "librarian_agent"}


def _runner_block() -> str:
    """The whole block including its kind table, with the source's own line
    wrapping folded away: the prose is split across adjacent string literals,
    so searching the raw text for a sentence finds nothing."""
    src = _RUNNER.read_bytes().decode("utf-8")
    block = src.split("sw_kind = ((getattr", 1)[1].split("# Image viewer:", 1)[0]
    return re.sub(r'"\s*\n\s*(?:f?")?', "", block)


# ── the prompt block ──────────────────────────────────────────────────────────

def test_every_kind_names_its_own_tool():
    """A block that describes the Coder and then offers research_agent would send
    the model to the wrong specialist - and the mapping is easy to fat-finger."""
    block = _runner_block()
    for kind, tool in _TOOLS.items():
        entry = re.search(rf'"{kind}": \("([^"]+)", "([^"]+)"', block)
        assert entry, f"{kind} has no entry in the window block"
        assert entry.group(2) == tool, \
            f"{kind} points at {entry.group(2)}, should be {tool}"


def test_the_block_says_what_can_be_done_and_not_only_where_the_user_is():
    # The browser block records this as a MEASURED lesson: state alone changed
    # nothing. Its twin must not quietly regress to a status line.
    block = _runner_block()
    assert "call the {_tool} tool" in block, "the block stopped naming the action"
    assert "most likely" in block, "the block stopped saying what the message is about"


def test_the_block_does_not_invite_a_second_run():
    """It coexists with the SUB-AGENT ACTIVE system block, which says the opposite
    ('do not delegate this again'). An open window is not a running one, and the
    text has to hold both truths or the two blocks contradict each other."""
    block = _runner_block()
    assert "does NOT mean a run is going" in block
    assert "do not start a second one" in block or "not start a second one" in block


def test_it_is_injected_for_one_turn_and_cleared():
    src = _RUNNER.read_bytes().decode("utf-8")
    assert 'session_for_sw.runtime_state.pop("subagent_window", None)' in src, \
        "the block would repeat on every later turn"
    assert "effective_input = sw_block + effective_input" in src


# ── the lane that carries it ──────────────────────────────────────────────────

def test_the_server_validates_the_kind_before_it_reaches_a_prompt():
    src = _SERVER.read_bytes().decode("utf-8")
    seg = src.split('_saw_kind = cmd.get("subAgentWindow")', 1)[1][:1400]
    assert '_known_kinds = ("coder", "research", "document", "librarian")' in seg
    assert "_saw_kind in _known_kinds" in seg, \
        "an unvalidated client string would reach the prompt"


def test_closing_the_window_removes_the_fact_by_itself():
    # The delete branch is the whole close mechanism: the client simply stops
    # sending the field, so there is no close event that can be missed.
    src = _SERVER.read_bytes().decode("utf-8")
    seg = src.split('_saw_kind = cmd.get("subAgentWindow")', 1)[1][:1400]
    assert 'del loaded.runtime_state["subagent_window"]' in seg


def test_the_browser_keeps_its_own_richer_lane():
    """The browser is deliberately NOT folded into this: its context carries the
    page, the selection and a screenshot, read server-side from the lease."""
    page = _PAGE.read_bytes().decode("utf-8")
    assert "subAgentState.agentKind !== 'browser'" in page, \
        "the browser fell into the generic window lane"
    src = _SERVER.read_bytes().decode("utf-8")
    assert '"browser_context"' in src, "the browser's own capture disappeared"


def test_the_chip_and_the_turn_agree_on_what_is_open():
    """One derivation feeds both, so what the user is told and what the model is
    told cannot drift apart."""
    page = _PAGE.read_bytes().decode("utf-8")
    assert "const subAgentWindowKind: SubAgentKind | null =" in page
    assert "{ subAgentWindow: subAgentWindowKind }" in page, \
        "the turn stopped carrying the open window"
    assert "title={tMain('subAgentContextChipTitle'" in page, \
        "the composer stopped showing which window is open"


def test_a_hotbar_icon_opens_that_agents_window():
    page = _PAGE.read_bytes().decode("utf-8")
    assert "onClick={() => toggleSubAgentWindow(kind)}" in page
    body = page.split("const toggleSubAgentWindow", 1)[1][:900]
    assert "openSubAgentWindow(true)" in body, "the icon stopped opening the window"
    assert "closeSubAgentWindow(true)" in body, \
        "a second click no longer closes it (and loses the slide-out)"


def test_the_chip_sits_next_to_the_workspace_not_adrift_in_the_middle():
    """The composer row is justify-end, so a single `mr-auto` is what separates
    the left group from the right-aligned rest - it belongs on the LAST chip of
    that group. The browser chip already took it off the workspace chip; the
    sub-agent chip did not, so the workspace kept it and pushed the sub-agent
    chip out into the middle of the row (screenshot-confirmed)."""
    page = _PAGE.read_bytes().decode("utf-8")
    seg = page.split("Workspace chip: leftmost element", 1)[1][:2600]
    assert '!subAgentState.interactive?.active && !subAgentWindowKind && "mr-auto"' in seg, \
        "the workspace chip no longer hands the auto margin to the sub-agent chip"


def test_the_open_agent_is_the_bright_mark_in_the_rail():
    """The globe wears the hover state permanently while its window is open, so
    the rail always shows WHICH window is up. The hotbar icons had no open state
    at all: the coder window could be showing and its icon stayed grey, which
    reads as 'nothing is open' (screenshot-confirmed)."""
    page = _PAGE.read_bytes().decode("utf-8")
    seg = page.split("The hotbar itself", 1)[1][:2600]
    assert "subAgentState.isOpen && subAgentState.agentKind === kind" in seg, \
        "the rail icon no longer knows whether its own window is open"
    assert '"text-gray-900 scale-110"' in seg, \
        "the open rail icon lost the globe's active look"


def test_each_accent_is_the_one_that_agents_own_window_uses():
    """Rule 2: the accent now exists twice - in the specialist's view inside
    SubAgentWindow and on the chip/badge that stand for it elsewhere. Taking the
    hue from the view is the whole point; a chip in a colour its own window never
    shows would teach the wrong association."""
    page = _PAGE.read_bytes().decode("utf-8")
    window = (_REPO / "web" / "components" / "SubAgentWindow.tsx").read_bytes().decode("utf-8")
    table = page.split("const SUBAGENT_ACCENT", 1)[1].split("};", 1)[0]

    # the coder is deliberately hueless - a code editor is black and white
    assert re.search(r"coder:\s*\{ chip: \"text-gray-", table), \
        "the coder grew an accent; its window is deliberately neutral"

    expected_hue = {"research": "violet", "document": "teal",
                    "librarian": "orange", "browser": "sky"}
    view_start = {"research": "agentKind === 'research'", "document": "agentKind === 'document'",
                  "librarian": "agentKind === 'librarian'", "browser": "agentKind === 'browser'"}
    for kind, hue in expected_hue.items():
        row = re.search(rf"{kind}:\s*\{{ chip: \"([^\"]+)\"", table)
        assert row, f"{kind} has no accent"
        assert hue in row.group(1), f"{kind}'s chip is not {hue}: {row.group(1)}"
        # and that hue really is what the view paints with
        seg = window.split(view_start[kind], 1)[1][:6000]
        assert f"-{hue}-" in seg, \
            f"{kind}'s window no longer uses {hue}; the chip and the view drifted apart"
