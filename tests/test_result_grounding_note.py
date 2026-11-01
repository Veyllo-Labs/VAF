# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The last unsupported verdict of a reply chain gets a fixed note, not another rewrite.

Live incident on a 4B local model: a reply claimed a result the tools had not delivered, the
guard sent it back twice, and the third reply proceeded silently. The three replies said done,
not done and running, and all three stayed on the screen, because a guard never erases a
streamed reply. With `result_grounding_max_retries` = N, verdicts 1..N-1 still get the model
correction, and verdict N gets the fixed note below the reply, with no further model call.

MUTATIONS, each turns the named tests red:
- drop the note append -> all three note tests;
- the old cap (`> _rg_max`: N corrections, then the note) -> the N=2 test (a third model
  reply);
- the note always in English -> the German test.
"""
import pytest

from vaf.core.agent import _GROUNDING_NOTE
from vaf.core.config import Config
from vaf.core.platform import Platform

REPLIES = ["Alle zehn Pakete sind installiert.", "Die Pakete sind noch nicht installiert.",
           "Die Installation laeuft gerade."]


@pytest.fixture
def agent(monkeypatch, tmp_path):
    from vaf.core.agent import Agent
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "deepseek", "api_key_deepseek": "sk-test"})
    a.current_session_id = "green123456"
    monkeypatch.setattr(a, "_try_workflow", lambda *args, **kw: None)
    monkeypatch.setattr(a, "_detect_ungrounded_result_claim",
                        lambda text, results: (True, "Pakete sind installiert"))
    monkeypatch.setattr(a, "_guard_keeps_answer", lambda text: True)
    a.main_calls = []

    def provider(messages=None, **kw):
        if not kw.get("tools"):
            yield "ok"
            return
        a.main_calls.append(1)
        yield REPLIES[min(len(a.main_calls), len(REPLIES)) - 1]
    a.api_backend.chat_completion = provider
    return a


def _config(monkeypatch, **values):
    real_get = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: values[key] if key in values else real_get(key, default)))


def _turn(agent, text):
    """One turn; returns what was streamed and the stored reply. The return value of
    chat_step is a status, the reply is the stream and the history (docs/CORE_AGENT.md)."""
    streamed = []
    agent.chat_step(user_input=text, raw_user_input=text, stream_callback=streamed.append)
    stored = [m for m in agent.history if m.get("role") == "assistant"]
    return "".join(streamed), stored


def test_two_replies_then_the_note(agent, monkeypatch):
    _config(monkeypatch, result_grounding_enabled=True, result_grounding_max_retries=2)
    streamed, stored = _turn(agent, "Install ten packages please")

    assert len(agent.main_calls) == 2, "the Nth verdict must not ask the model again"
    note = _GROUNDING_NOTE["en"]
    assert REPLIES[0] in streamed and REPLIES[1] in streamed, "a reply the user read is gone"
    assert streamed.rstrip().endswith(note) and REPLIES[2] not in streamed
    assert [m["content"] for m in stored] == [REPLIES[0], REPLIES[1] + "\n\n" + note], \
        "the stored replies lack the note, or lost a reply"


def test_the_note_is_german_for_a_german_user(agent, monkeypatch):
    _config(monkeypatch, result_grounding_enabled=True, result_grounding_max_retries=2)
    streamed, stored = _turn(agent, "Installiere bitte zehn Pakete fuer mich")
    assert agent.prompt_manager.user_language == "de"
    assert streamed.rstrip().endswith(_GROUNDING_NOTE["de"])
    assert stored[-1]["content"].endswith(_GROUNDING_NOTE["de"])


def test_zero_retries_puts_the_note_under_the_first_reply(agent, monkeypatch):
    _config(monkeypatch, result_grounding_enabled=True, result_grounding_max_retries=0)
    streamed, stored = _turn(agent, "Install ten packages please")
    assert len(agent.main_calls) == 1
    assert [m["content"] for m in stored] == [REPLIES[0] + "\n\n" + _GROUNDING_NOTE["en"]]
    assert streamed.rstrip().endswith(_GROUNDING_NOTE["en"])


def test_no_note_when_the_guard_is_off(agent, monkeypatch):
    _config(monkeypatch, result_grounding_enabled=False, result_grounding_max_retries=2)
    streamed, stored = _turn(agent, "Install ten packages please")
    assert len(agent.main_calls) == 1
    assert [m["content"] for m in stored] == [REPLIES[0]]
    assert _GROUNDING_NOTE["en"] not in streamed
