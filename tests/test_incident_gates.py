# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Confirmation gates from incident 2026-07-13 09:15 (Telegram).

A misattributed reply ("nein bitte nicht") to a background question mutated a
stored automation and delegated a file deletion five times, unconfirmed - the
drain turns even re-delegated AFTER the agent itself had asked "Soll ich die
Datei jetzt direkt loeschen?". Two gates now sit in execute_tool right after
the plan gate: (a) proactive-reply mutation gate, (c) ask-first drain gate.
Both return confirm-style tool RESULTS (invariant 4.1) and have config
kill-switches.
"""
from pathlib import Path
from types import SimpleNamespace

from vaf.core.agent import Agent


def _bare_agent(**attrs):
    a = Agent.__new__(Agent)
    a._thinking_reply_context = None
    a._thinking_reply_user_text = None
    a._synthetic_drain_turn = False
    a._pending_user_question = None
    for k, v in attrs.items():
        setattr(a, k, v)
    return a


# ── clear-affirmative classifier (deterministic) ──────────────────────────────

def test_clear_affirmatives():
    for t in ("ja", "Ja bitte", "ja mach das", "ok", "Okay!", "yes please", "klar", "mach mal", "go"):
        assert Agent._is_clear_affirmative(t), t


def test_not_affirmative():
    for t in ("nein bitte nicht", "nein", "bitte nicht", "vielleicht", "ja aber nicht heute",
              "was ? was machst du ?", "", None, "no", "stop", "warum?"):
        assert not Agent._is_clear_affirmative(t), t


# ── gate (a): proactive-reply mutation gate ───────────────────────────────────

def _tool(perm="write"):
    return SimpleNamespace(permission_level=perm)


def test_reply_gate_blocks_mutation_on_unclear_reply():
    a = _bare_agent(_thinking_reply_context="[Context: ...]",
                    _thinking_reply_user_text="nein bitte nicht")
    msg = a._proactive_reply_gate_decision("update_automation", _tool(), {})
    assert msg and "[CONFIRM REQUIRED]" in msg


def test_reply_gate_blocks_destructive_delegation():
    a = _bare_agent(_thinking_reply_context="[Context: ...]",
                    _thinking_reply_user_text="nein bitte nicht")
    msg = a._proactive_reply_gate_decision(
        "librarian_agent", _tool(),
        {"task": "Loesche die Datei /home/user/Documents/x.html"})
    assert msg and "[CONFIRM REQUIRED]" in msg


def test_reply_gate_allows_clear_yes_and_benign_delegation():
    a = _bare_agent(_thinking_reply_context="[Context: ...]",
                    _thinking_reply_user_text="ja mach das")
    assert a._proactive_reply_gate_decision("update_automation", _tool(), {}) is None
    a2 = _bare_agent(_thinking_reply_context="[Context: ...]",
                     _thinking_reply_user_text="nein bitte nicht")
    assert a2._proactive_reply_gate_decision(
        "librarian_agent", _tool(), {"task": "Liste die Dateien im Ordner"}) is None


def test_reply_gate_inactive_outside_pickup_turns():
    a = _bare_agent(_thinking_reply_context=None,
                    _thinking_reply_user_text="nein bitte nicht")
    assert a._proactive_reply_gate_decision("update_automation", _tool(), {}) is None


# ── gate (c): ask-first drain gate ────────────────────────────────────────────

def test_ask_first_blocks_write_tools_in_drain_turns():
    a = _bare_agent(_synthetic_drain_turn=True,
                    _pending_user_question={"preview": "Soll ich loeschen?"})
    msg = a._ask_first_gate_decision("librarian_agent", _tool("write"))
    assert msg and "[AWAITING USER]" in msg
    assert a._ask_first_gate_decision("update_automation", _tool("write"))


def test_ask_first_allows_read_and_normal_turns():
    a = _bare_agent(_synthetic_drain_turn=True,
                    _pending_user_question={"preview": "?"})
    assert a._ask_first_gate_decision("read_file", _tool("read")) is None
    b = _bare_agent(_synthetic_drain_turn=False,
                    _pending_user_question={"preview": "?"})
    assert b._ask_first_gate_decision("update_automation", _tool("write")) is None
    c = _bare_agent(_synthetic_drain_turn=True, _pending_user_question=None)
    assert c._ask_first_gate_decision("update_automation", _tool("write")) is None


# ── wiring guards (source scans) ──────────────────────────────────────────────

def test_gates_wired_after_plan_gate():
    import vaf.core.agent as agent_mod
    body = Path(agent_mod.__file__).read_text(encoding="utf-8")
    i_plan = body.index("self._plan_gate_decision(name, tool_instance)")
    i_a = body.index("self._proactive_reply_gate_decision(name, tool_instance, args)")
    i_c = body.index("self._ask_first_gate_decision(name, tool_instance)", i_a)
    assert i_plan < i_a < i_c, "gates must run after the plan gate, in order"


def test_drain_sets_and_restores_synthetic_flag():
    import vaf.core.headless_runner as hr
    src = Path(hr.__file__).read_text(encoding="utf-8")
    assert "agent._synthetic_drain_turn = True" in src
    i_set = src.index("agent._synthetic_drain_turn = True")
    i_finally = src.index("finally:", i_set)
    i_unset = src.index("agent._synthetic_drain_turn = False", i_finally)
    assert i_set < i_finally < i_unset, "drain flag must be restored in finally (Rule 4.5)"


def test_drain_retry_respects_pending_question():
    import vaf.core.headless_runner as hr
    src = Path(hr.__file__).read_text(encoding="utf-8")
    assert 'any_needs_retry and getattr(agent, "_pending_user_question", None)' in src, (
        "drain retry lane lost the ask-first branch - it would command re-delegation "
        "while the agent awaits the user's answer"
    )


def test_pending_latch_not_cleared_by_synthetic_turns():
    import vaf.core.agent as agent_mod
    src = Path(agent_mod.__file__).read_text(encoding="utf-8")
    assert ('if user_input and not skip_input and not getattr(self, "_synthetic_drain_turn", False):'
            in src), "pending-question latch must survive synthetic drain turns"


def test_config_kill_switches_exist():
    from vaf.core.config import Config
    assert Config.DEFAULTS.get("proactive_reply_mutation_gate_enabled") is True
    assert Config.DEFAULTS.get("ask_first_drain_gate_enabled") is True
