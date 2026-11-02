# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The model-health score in a real `chat_step`: one re-run, a forced call, an honest note.

On the real `Agent` with only the provider faked (the API lane's `chat_completion`, or the
local server's HTTP post), a turn whose model announces work and calls nothing is re-run once:
the reply stays where the person read it, the history gets that reply and a note naming the
person's words and the task tools, and the next request forces a call of exactly those tools
where the lane can. At high after the re-run the reply gets the note in the chat's language.
Nothing of it happens on a turn that is not the user speaking in their own chat.

MUTATIONS, each turns the named test red:
- keep the riders in the narrowed array; call `_retract_streamed_reply` in the re-run; drop
  the note -> test_mid_forces_one_narrowed_call;
- send plain "required" on the allowed-tools provider -> test_allowed_tools_provider_gets_required_mode;
- narrow the array for a thinking node -> test_thinking_forced_node_unchanged;
- set `forces_tool_calls=True` on anthropic -> test_non_forcing_lane_retries_unforced;
- reset `_model_health_retried` inside the loop -> test_one_health_retry_per_turn;
- run the grounding correction before the re-evaluation, or spend `_result_grounding_retries`
  -> test_ungrounded_lift_replaces_grounding_correction;
- append the note once per generation -> test_high_appends_one_stuck_note_per_turn;
- read `_turn_is_from_a_person` instead of `_turn_is_from_the_user` ->
  test_scope_excludes_non_user_turns[allowed_set];
- remove the `[MODEL_HEALTH]` log call -> test_log_line_written;
- reset the ledger only after a tool that succeeded -> test_a_failed_tool_resets_the_ledger;
- drop the written-call check in front of the score -> test_a_call_written_as_text_is_not_stuck.
"""
import json

import pytest
import requests

import vaf
import vaf.core.agent as agent_mod
from vaf.core import model_health as mh
from vaf.core.platform import Platform

SESSION = "green123456"
ASK = "Bitte installiere mir die zehn Pakete aus der Liste"
SAY = "Ich installiere jetzt die zehn Pakete mit install_packages und melde mich gleich."
TASK = "install_packages"


class _Install(vaf.BaseTool):
    """The turn's task tool: records that it ran, does nothing else."""
    name = TASK
    description = "Install Python packages."
    permission_level = "read"
    parameters = {"type": "object", "properties": {}}

    def __init__(self):
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return "10/10 installed"


class _Buffer:
    """The web lane's stream callback: callable, with a clear()."""

    def __init__(self):
        self.chunks = []

    def __call__(self, text):
        self.chunks.append(str(text))

    def clear(self):
        self.chunks = []

    @property
    def text(self):
        return "".join(self.chunks)


def _setup(agent, monkeypatch):
    monkeypatch.setattr(agent, "_try_workflow", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "_route_tools", lambda text: [TASK])
    monkeypatch.setattr(agent, "get_token_usage", lambda **kw: (1000, 32768))
    monkeypatch.setattr(agent, "manage_context", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "analyze_intent", lambda *a, **kw: 0.7)
    agent.tools[TASK] = _Install()
    agent.retractions = []
    monkeypatch.setattr(agent, "_clear_last_assistant_ui", lambda sid: agent.retractions.append(sid))
    agent.grounding = []
    monkeypatch.setattr(agent, "_detect_ungrounded_result_claim",
                        lambda text, res: (agent.grounding.append(text), (False, None))[1])
    agent.current_session_id = SESSION
    agent._bind_session_persistence(SESSION)
    mh.reset_metrics()
    return agent


@pytest.fixture
def make(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    monkeypatch.delenv("VAF_THINKING_MODE", raising=False)
    monkeypatch.delenv("VAF_IN_AUTOMATION", raising=False)
    from vaf.core.agent import Agent

    def _make(provider="openai"):
        a = Agent(register_signals=False, run_kind="chat",
                  config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
        if provider == "library":
            a.api_backend = None
            a.use_server = False
        elif provider == "server":
            a.api_backend = None
            a.use_server = True
        else:
            a.api_backend.provider_name = provider
        return _setup(a, monkeypatch)
    return _make


def _ledger(agent, score, language="de"):
    agent.main_persistence.save_model_health(mh.to_payload(mh.State(score, language)))


class _Runaway(BaseException):
    """A turn that keeps calling the model; BaseException so no retry handler eats it."""


class _Model:
    """The API provider: each main call (the one with tools) answers from the script; a text
    entry is a reply, a tuple is a tool call. Side calls get a short answer. More than eight
    main calls in one turn is a runaway and ends the test."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, messages=None, **kw):
        if not kw.get("tools"):
            yield "ok"
            return
        self.calls.append(kw)
        if len(self.calls) > 8:
            raise _Runaway(f"{len(self.calls)} main calls in one turn")
        step = self.script.pop(0) if self.script else "Fertig."
        if isinstance(step, str):
            yield step
            return
        yield "Einen Moment."
        yield json.dumps({"tool_calls": [{"index": 0, "id": f"c{len(self.calls)}", "type": "function",
                                          "function": {"name": step[0], "arguments": "{}"}}]})
        yield json.dumps({"finish_reason": "tool_calls"})


def _names(tools):
    return [t["function"]["name"] for t in (tools or [])]


def _turn(agent, *script, text=ASK):
    model = _Model(*script)
    agent.api_backend.chat_completion = model
    buf = _Buffer()
    agent.chat_step(user_input=text, raw_user_input=text, stream_callback=buf)
    return model, buf


def _notes(agent):
    return [str(m["content"]) for m in agent.history
            if m.get("role") == "system" and str(m.get("content", "")).startswith("No tool ran this turn")]


# ── the local server lane: the forced call, narrowed ─────────────────────────────────────

class _Stream:
    status_code = 200
    text = ""

    def __init__(self, deltas):
        self._lines = [b"data: " + json.dumps({"choices": [{"delta": d}]}).encode() for d in deltas]
        self._lines.append(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}')
        self._lines.append(b"data: [DONE]")

    def iter_lines(self):
        return iter(self._lines)

    def close(self):
        pass


class _Refused:
    status_code = 500
    text = ""

    def json(self):
        return {}


def test_mid_forces_one_narrowed_call(make, monkeypatch):
    agent = make("server")
    _ledger(agent, 5.0)
    script = [
        [{"content": SAY}],
        [{"tool_calls": [{"index": 0, "id": "c1", "function": {"name": TASK, "arguments": "{}"}}]}],
        [{"content": "Alle zehn Pakete sind installiert."}],
    ]
    payloads = []

    def post(url, json=None, **kw):
        if isinstance(json, dict) and json.get("stream") and "messages" in json:
            payloads.append(json)
            return _Stream(script[min(len(payloads), len(script)) - 1])
        return _Refused()
    monkeypatch.setattr(requests, "post", post)
    buf = _Buffer()
    agent.chat_step(user_input=ASK, raw_user_input=ASK, stream_callback=buf)

    assert payloads[0]["tool_choice"] == "auto"
    assert "ask_user" in _names(payloads[0]["tools"]), "control: the riders are offered normally"
    assert payloads[1]["tool_choice"] == "required"
    assert _names(payloads[1]["tools"]) == [TASK], "forced: the task tools only, no rider"
    assert payloads[2]["tool_choice"] == "auto", "one forced generation, then auto again"
    assert agent.tools[TASK].calls, "the forced call ran"

    assert agent.retractions == [], "the re-run retracted the reply the user read"
    assert SAY in buf.text, "the first reply left the screen after the tool round's clear"
    roles = [(m.get("role"), str(m.get("content") or "")) for m in agent.history]
    i = next(k for k, (r, c) in enumerate(roles) if r == "assistant" and c == SAY)
    assert roles[i + 1][0] == "system" and roles[i + 1][1].startswith("No tool ran this turn")
    assert ASK in roles[i + 1][1] and TASK in roles[i + 1][1]
    assert mh.metrics()["forced"] == 1


# ── the API lanes ────────────────────────────────────────────────────────────────────────

def test_allowed_tools_provider_gets_required_mode(make):
    agent = make("openai")
    _ledger(agent, 5.0)
    model, _ = _turn(agent, SAY, SAY)
    first, forced = model.calls[0], model.calls[1]
    assert forced["tool_choice"] == {"type": "allowed_tools", "allowed_tools": {
        "mode": "required", "tools": [{"type": "function", "function": {"name": TASK}}]}}
    assert _names(forced["tools"]) == _names(first["tools"]), "the resident array changed"


def test_thinking_forced_node_unchanged(make):
    """A thinking node's force (no `_forced_tool_names`) keeps today's request byte for byte."""
    agent = make("openai")
    tools = [{"type": "function", "function": {"name": n}} for n in ("web_search", "thinking_done")]
    for choice in ("auto", {"type": "allowed_tools", "allowed_tools": {"mode": "auto", "tools": []}}):
        agent._force_tool_choice, agent._force_tool_choice_used = "required", False
        agent._forced_tool_names = None
        out_tools, out_choice = agent._forced_request(tools, choice)
        assert out_tools is tools and out_choice == "required"
        assert agent._forcing_this_generation() is True
    agent._force_tool_choice_used = True
    assert agent._forced_request(tools, "auto") == (tools, "auto")


@pytest.mark.parametrize("provider", ["anthropic", "deepseek", "google", "openrouter", "veyllo"])
def test_non_forcing_lane_retries_unforced(make, provider):
    agent = make(provider)
    assert agent._can_force_tool_call() is False
    _ledger(agent, 5.0)
    model, _ = _turn(agent, SAY, SAY)
    assert len(model.calls) == 2, "the re-run ran"
    assert model.calls[1]["tool_choice"] == "auto"
    assert _names(model.calls[1]["tools"]) == _names(model.calls[0]["tools"])
    assert len(_notes(agent)) == 1 and TASK in _notes(agent)[0]


def test_the_forcing_capability_per_lane(make):
    assert make("openai")._can_force_tool_call() is True
    assert make("server")._can_force_tool_call() is True
    assert make("library")._can_force_tool_call() is False


def test_library_lane_retries_unforced(make):
    agent = make("library")
    _ledger(agent, 5.0)
    calls = []

    class _LLM:
        def create_chat_completion(self, **kw):
            if not kw.get("tools"):       # a side call (a classifier), not the turn
                return {"choices": [{"message": {"content": "no"}}]}
            calls.append(kw)
            return iter([{"choices": [{"delta": {"content": SAY}}]}])
    agent.llm = _LLM()
    agent.chat_step(user_input=ASK, raw_user_input=ASK, stream_callback=_Buffer())
    assert len(calls) == 2
    assert calls[1]["tool_choice"] == "auto" and _names(calls[1]["tools"]) == _names(calls[0]["tools"])
    assert len(_notes(agent)) == 1


def test_no_task_tool_means_no_force(make, monkeypatch):
    """The router picked only riders: nothing is the task, the re-run is unforced and its note
    names no tool."""
    agent = make("openai")
    monkeypatch.setattr(agent, "_route_tools", lambda text: ["memory_search"])
    _ledger(agent, 5.0)
    model, _ = _turn(agent, SAY + " Ich nutze memory_search.", SAY)
    assert len(model.calls) == 2
    choice = model.calls[1]["tool_choice"]
    assert choice == "auto" or (isinstance(choice, dict) and choice["allowed_tools"]["mode"] == "auto")
    assert "one of" not in _notes(agent)[0]


def test_one_health_retry_per_turn(make):
    agent = make("openai")
    _ledger(agent, 5.0)
    model, _ = _turn(agent, SAY, SAY, SAY, SAY)
    assert len(model.calls) == 2, "a second re-run in the same turn"
    assert len(_notes(agent)) == 1

    agent = make("openai")
    _ledger(agent, 5.0)
    model, _ = _turn(agent, SAY, (TASK,), "Fertig: alle zehn Pakete sind installiert.")
    after = model.calls[2]["tool_choice"]
    assert after == "auto" or after["allowed_tools"]["mode"] == "auto", after


# ── the order with result grounding ──────────────────────────────────────────────────────

def test_ungrounded_lift_replaces_grounding_correction(make, monkeypatch):
    agent = make("openai")
    _ledger(agent, 2.0)
    seen = []

    def judge(text, res):
        seen.append(agent._result_grounding_retries)
        return (True, "Die Pakete sind installiert.") if len(seen) == 1 else (False, None)
    monkeypatch.setattr(agent, "_detect_ungrounded_result_claim", judge)
    calm = "Die Pakete sind installiert, du kannst sie jetzt benutzen."
    model, _ = _turn(agent, calm, calm)
    assert len(model.calls) == 2
    assert model.calls[1]["tool_choice"]["allowed_tools"]["mode"] == "required"
    assert not [m for m in agent.history if "CORRECTION NEEDED" in str(m.get("content"))]
    assert len(_notes(agent)) == 1
    assert seen == [0, 0], "the re-run spent a grounding correction"


def test_below_mid_grounding_corrects_as_before(make, monkeypatch):
    agent = make("openai")
    verdicts = iter([(True, "Die Pakete sind installiert."), (False, None)])
    monkeypatch.setattr(agent, "_detect_ungrounded_result_claim", lambda text, res: next(verdicts))
    calm = "Die Pakete sind installiert, du kannst sie jetzt benutzen."
    model, _ = _turn(agent, calm, "Korrektur: das ist nicht belegt.")
    assert [m for m in agent.history if "CORRECTION NEEDED" in str(m.get("content"))]
    assert _notes(agent) == []


# ── high: the honest note ────────────────────────────────────────────────────────────────

def test_high_appends_one_stuck_note(make):
    agent = make("openai")
    _ledger(agent, 10.0, "de")
    model, buf = _turn(agent, SAY, SAY)
    note = mh.STUCK_NOTE["de"]
    assert buf.text.count(note) == 1
    assert note in agent.history[-1]["content"], "the stored reply lost the note"
    assert agent.grounding == [], "grounding judged a reply that already carries the note"
    assert mh.metrics()["notes"] == 1


def test_high_appends_one_stuck_note_per_turn(make, monkeypatch):
    agent = make("openai")
    _ledger(agent, 10.0, "de")
    checks = []
    monkeypatch.setattr(agent, "_validate_final_answer",
                        lambda content, intent: (checks.append(1), len(checks) > 1)[1])
    model, buf = _turn(agent, SAY, SAY, SAY)
    assert len(model.calls) == 3, "the bounce gave the turn a third generation"
    assert buf.text.count(mh.STUCK_NOTE["de"]) == 1


# ── scope ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", ["drain", "not_human", "allowed_set", "background", "subagent",
                                  "key_off"])
def test_scope_excludes_non_user_turns(make, monkeypatch, case):
    agent = make("openai")
    _ledger(agent, 10.0)
    lines = []
    monkeypatch.setattr(agent_mod, "append_domain_log",
                        lambda domain, msg, *a, **kw: lines.append(str(msg)))
    if case == "drain":
        agent._synthetic_drain_turn = True
    elif case == "not_human":
        agent._turn_is_human = False
    elif case == "allowed_set":
        agent.set_allowed_tools({TASK})
    elif case == "background":
        agent._background_run = True
    elif case == "subagent":
        monkeypatch.setenv("VAF_IN_SUBAGENT_TERMINAL", "1")
    elif case == "key_off":
        from vaf.core.config import Config
        real = Config.get
        monkeypatch.setattr(Config, "get", staticmethod(
            lambda key, default=None: False if key == "model_health_guard_enabled" else real(key, default)))
    model, buf = _turn(agent, SAY, SAY)
    assert len(model.calls) == 1, f"{case}: re-run"
    assert _notes(agent) == [] and mh.STUCK_NOTE["de"] not in buf.text
    assert not [ln for ln in lines if ln.startswith("[MODEL_HEALTH]")], case


def test_a_thinking_turn_is_not_scored(make):
    agent = make("openai")
    agent._model_health_begin(ASK, False, True, ASK)
    assert agent._model_health_turn is None
    agent._model_health_begin(ASK, False, False, ASK)
    assert agent._model_health_turn is not None, "control"


# ── what is written down ─────────────────────────────────────────────────────────────────

def test_log_line_written(make, monkeypatch):
    agent = make("openai")
    lines = []
    monkeypatch.setattr(agent_mod, "append_domain_log",
                        lambda domain, msg, *a, **kw: lines.append(str(msg)))
    _turn(agent, "Gern, hier ist die Liste der Pakete, die ich kenne.")
    health = [ln for ln in lines if ln.startswith("[MODEL_HEALTH]")]
    assert health == ["[MODEL_HEALTH] score=0 level=none signals=- reaction=none"], health


def test_the_ledger_is_kept_per_chat_and_holds_no_text(make):
    agent = make("openai")
    _turn(agent, SAY, SAY, text=ASK + " QZXWV")
    payload = agent.main_persistence.get_model_health()
    assert set(payload) == {"format", "score", "language", "updated_at"}
    assert payload["format"] == mh.LEDGER_FORMAT and payload["language"] == "de"
    assert "QZXWV" not in json.dumps(payload) and "install" not in json.dumps(payload)
    agent._bind_session_persistence("red654321")
    assert agent.main_persistence.get_model_health() == {}


def test_a_progress_tool_resets_the_ledger(make):
    agent = make("openai")
    _ledger(agent, 2.0)
    _turn(agent, (TASK,), "Alle zehn Pakete sind installiert.")
    assert mh.from_payload(agent.main_persistence.get_model_health()).score == 0.0


def test_a_failed_tool_resets_the_ledger(make, monkeypatch):
    """A tool that RAN and failed is the normal error the score must not count: the measured
    600-second sandbox timeout in the middle of the stuck series."""
    agent = make("openai")
    _ledger(agent, 6.0)
    monkeypatch.setattr(agent.tools[TASK], "run",
                        lambda **kw: "Error: the sandbox timed out after 600 s")
    _turn(agent, (TASK,), "Die Installation ist nach 600 Sekunden abgebrochen.")
    assert mh.from_payload(agent.main_persistence.get_model_health()).score == 0.0


def test_a_call_written_as_text_is_not_stuck(make):
    """A call the model wrote as text is about to run through the fallback parsers: re-running
    the turn would drop it."""
    agent = make("openai")
    _ledger(agent, 10.0)
    leaked = 'Ich installiere jetzt.\n<tool_call>{"name": "install_packages", "arguments": {}}</tool_call>'
    model, _ = _turn(agent, leaked, "Alle zehn Pakete sind installiert.")
    assert _notes(agent) == []
    assert agent.tools[TASK].calls, "the written call did not run"
