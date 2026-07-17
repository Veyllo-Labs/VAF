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
    i_plan = body.index("self._plan_gate_decision(name, tool_instance, tool_args=args)")
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


# ── three-way reply classification (live incident: a fresh task message while a
# background question was pending got the full reply framing; the armed gate then
# blocked the user's own workflow request twice, and the turn ended in confusion) ──

INCIDENT_TASK = ("Okay fuehre bitte einen mehrstufige websuche in einem workflow durch, "
                 "du kannst dafuer auch einen temporaeren workflow bauen, suche nach dem "
                 "wetter, dann nach ny, und erstelle ein HTML mit deinen ergebnissen")


def test_classify_short_affirmative_and_negation():
    assert Agent._classify_background_reply("Ja bitte") == "affirmative"
    assert Agent._classify_background_reply("ok mach das") == "affirmative"
    assert Agent._classify_background_reply("nein bitte nicht") == "negation"
    assert Agent._classify_background_reply("vielleicht") == "unclear"
    assert Agent._classify_background_reply("") == "unclear"
    assert Agent._classify_background_reply(None) == "unclear"


def test_classify_long_task_message_is_new_topic():
    """The incident message: starts with 'Okay' but is a 200-char NEW request -
    it must be new_topic, never a confirmation of the pending proposal."""
    assert Agent._classify_background_reply(INCIDENT_TASK) == "new_topic"
    # ... even when it contains negation-shaped words somewhere inside.
    assert Agent._classify_background_reply(
        INCIDENT_TASK + " aber nicht als PDF") == "new_topic"


def test_clear_affirmative_is_length_bounded():
    """A long message that merely OPENS with 'Okay' is not a clear go-ahead:
    treating it as one would carry out the pending proposal the user never
    addressed (the flip side of the incident)."""
    assert Agent._is_clear_affirmative("Okay!") is True
    assert Agent._is_clear_affirmative(INCIDENT_TASK) is False


def test_reply_gate_disarmed_for_new_topic_pickup():
    """new_topic pickups inject only a light note and DISARM the gate: the
    user's own request must not be [CONFIRM REQUIRED]-blocked over an
    unrelated pending question (the incident blocked create_agent_workflow
    twice exactly this way)."""
    a = _bare_agent(_thinking_reply_context="[Context: light new_topic note]",
                    _thinking_reply_user_text=INCIDENT_TASK,
                    _thinking_reply_gate_armed=False)
    assert a._proactive_reply_gate_decision("create_agent_workflow", _tool(), {}) is None
    assert a._proactive_reply_gate_decision("update_automation", _tool(), {}) is None


def test_reply_gate_stays_armed_by_default():
    """Without an explicit disarm the gate keeps its strict pre-fix behavior
    (attribute may not exist on old paths - default must be armed)."""
    a = _bare_agent(_thinking_reply_context="[Context: ...]",
                    _thinking_reply_user_text="vielleicht spaeter?")
    msg = a._proactive_reply_gate_decision("update_automation", _tool(), {})
    assert msg and "[CONFIRM REQUIRED]" in msg


# ── plan gate: workflow launches carry their own plan ────────────────────────
# Live incident: the model committed to execute_workflow(research_and_code),
# the plan gate bounced it, the model set a plan - and then did the steps
# manually, the workflow forgotten. A workflow launch IS the plan the gate
# demands; the gate now seeds working memory from the call and allows it.

def test_derive_plan_seed_from_execute_workflow():
    seed = Agent._derive_workflow_plan_seed(
        "execute_workflow",
        {"workflow_id": "research_and_code",
         "variables": {"query": "Wetter Berlin + New York", "output_file": "w.html"}})
    assert seed and "research_and_code" in seed[0] and "Wetter Berlin" in seed[0]


def test_derive_plan_seed_from_run_temp_steps():
    seed = Agent._derive_workflow_plan_seed(
        "create_agent_workflow",
        {"action": "run_temp", "name": "Wetter Recherche",
         "steps": [{"name": "Wetter Berlin suchen", "tool": "web_search"},
                   {"name": "HTML bauen", "tool": "coding_agent"}]})
    assert seed and "Wetter Recherche" in seed[0]
    assert "Wetter Berlin suchen" in seed[0] and "HTML bauen" in seed[0]


def test_derive_plan_seed_rejects_unusable_args():
    assert Agent._derive_workflow_plan_seed("execute_workflow", {}) is None
    assert Agent._derive_workflow_plan_seed("create_agent_workflow", {"action": "list"}) is None
    assert Agent._derive_workflow_plan_seed("create_agent_workflow", None) is None


def _plan_gate_agent(seeded):
    """Bare agent with no plan; records what gets seeded into working memory."""
    class _Persistence:
        def get_working_memory(self):
            return {"plan": []}

        def update_working_memory(self, **kw):
            seeded.update(kw)

    a = Agent.__new__(Agent)
    a._noninteractive = False
    a._plan_gate_blocks = 0
    a.main_persistence = _Persistence()
    return a


def test_plan_gate_seeds_and_allows_a_workflow_launch(monkeypatch):
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    seeded = {}
    a = _plan_gate_agent(seeded)
    msg = a._plan_gate_decision(
        "execute_workflow", _tool("write"),
        tool_args={"workflow_id": "research_and_code", "variables": {"query": "x"}})
    assert msg is None                       # allowed, no bounce
    assert seeded.get("plan") and "research_and_code" in seeded["plan"][0]


def test_plan_gate_still_bounces_other_write_tools(monkeypatch):
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    seeded = {}
    a = _plan_gate_agent(seeded)
    msg = a._plan_gate_decision("write_file", _tool("write"),
                                tool_args={"path": "x.html", "content": "..."})
    assert msg and "[PLAN REQUIRED]" in msg
    assert not seeded                        # nothing auto-seeded for normal tools


def test_plan_gate_bounces_workflow_launch_without_usable_args(monkeypatch):
    """A create_agent_workflow(action='list') carries no plan - normal bounce."""
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    seeded = {}
    a = _plan_gate_agent(seeded)
    msg = a._plan_gate_decision("create_agent_workflow", _tool("write"),
                                tool_args={"action": "list"})
    assert msg and "[PLAN REQUIRED]" in msg


# ── cross-lane duplicate filter placement (source scan) ──────────────────────

def test_duplicate_filter_covers_fallback_parsed_calls():
    """The dedupe/window checks must run AFTER every parsing lane (streamed AND
    the XML/JSON/paren/recovery fallbacks) and BEFORE the assistant tool_calls
    message is built - a fallback-parsed batch bypassed the old in-loop checks
    and re-ran the same two searches verbatim (live incident,
    streaming_tools=0)."""
    import vaf.core.agent as agent_mod
    body = Path(agent_mod.__file__).read_text(encoding="utf-8")
    i_fallback = body.index("after_regex_fallback tool_calls=")
    i_filter = body.index("Cross-lane duplicate filter")
    i_append = body.index('"tool_calls": tool_calls_detected}')
    assert i_fallback < i_filter < i_append, \
        "duplicate filter must sit between the last parser lane and the history append"


# ── waiting-latch TTL safety net ─────────────────────────────────────────────

def test_waiting_for_reply_ttl_expires_stale_latch(tmp_path, monkeypatch):
    """The 10-min skip only runs when a thinking run fires; a stale latch from
    a crashed/disabled thinking mode must not claim the user's next message
    as a 'reply' days later. Reads past the TTL expire and clear the entry."""
    import time as _time

    import vaf.core.thinking_mode as tm
    from vaf.core.platform import Platform

    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))
    scope = "ab12cd34-0000-4000-8000-000000000001"

    tm.set_waiting_for_reply(scope, username="user", question_text="Frage?")
    assert tm.get_waiting_for_reply(scope) is not None  # fresh -> returned

    # Backdate beyond the TTL: the next read expires and clears it.
    data = tm._load_waiting()
    key = tm._key(scope)
    data[key]["question_sent_at_ts"] = _time.time() - 13 * 3600
    tm._save_waiting(data)
    assert tm.get_waiting_for_reply(scope) is None
    assert key not in tm._load_waiting()  # cleared, not just hidden


def test_waiting_for_reply_ttl_zero_disables(tmp_path, monkeypatch):
    import time as _time

    import vaf.core.thinking_mode as tm
    from vaf.core.config import Config
    from vaf.core.platform import Platform

    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Config, "get",
                        classmethod(lambda cls, k, d=None:
                                    0 if k == "thinking_reply_wait_ttl_hours" else d))
    scope = "ab12cd34-0000-4000-8000-000000000001"
    tm.set_waiting_for_reply(scope, username="user", question_text="Frage?")
    data = tm._load_waiting()
    data[tm._key(scope)]["question_sent_at_ts"] = _time.time() - 100 * 3600
    tm._save_waiting(data)
    assert tm.get_waiting_for_reply(scope) is not None  # TTL off -> kept
