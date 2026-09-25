# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A long session's compression keeps what the conversation still needs.

Every compression summarized the removed messages into "2-3 concise sentences" (200 tokens) and
re-rolled the previous summary into three sentences again, next to five tool results cut to 300
characters - sized for an 8k local window, applied to every window. In a long session (measured
in a long build session: four compressions) the user's standing instructions, the decisions and
the file paths were gone after a few rounds.

Now the budget follows the effective limit (ContextManager.summary_budget_tokens /
critical_tool_budget): a small window keeps the short summary and the five results; above it the
summary is structured (requests and instructions in the user's words, decisions, work with paths,
errors and fixes, what is open), merges a previous summary instead of re-rolling it, and more of
the compressed-away tool results survive, with more of each.

MUTATION: return 200 from summary_budget_tokens for every window and the structured-summary
test goes red; put back the fixed `[-5:]` / 300 in compress and the large-window test goes red.
"""
import pytest

from vaf.core.context import ContextManager
from vaf.core.platform import Platform


def test_the_budgets_follow_the_limit():
    assert ContextManager(8192).summary_budget_tokens() == 200
    assert ContextManager(20000).summary_budget_tokens() == 1000
    assert ContextManager(45000).summary_budget_tokens() == 2250
    assert ContextManager(1_000_000).summary_budget_tokens() == 4000
    assert ContextManager(8192).critical_tool_budget() == (5, 300)
    assert ContextManager(45000).critical_tool_budget() == (10, 800)
    assert ContextManager(200_000).critical_tool_budget() == (15, 1200)


def _history(n_tools: int, tool_len: int, n_filler: int):
    h = [{"role": "system", "content": "sys"}]
    for i in range(n_tools):
        h.append({"role": "tool", "name": "write_file", "tool_call_id": f"c{i}",
                  "content": f"Wrote /home/user/proj/file{i}.py " + "x" * tool_len})
    for i in range(n_filler):
        h.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"})
    return h


@pytest.mark.parametrize("limit,count,chars", [(8192, 5, 300), (45000, 10, 800)])
def test_more_tool_results_survive_on_a_larger_window(limit, count, chars, monkeypatch, tmp_path):
    cm = ContextManager(limit)
    monkeypatch.setattr(cm, "_archive_history", lambda h: None)
    history = _history(14, 2000, cm.recent_memory_size + 10)
    out = cm.compress(history)
    kept = [m for m in out if m.get("role") == "tool"]
    assert len(kept) == count
    assert all(len(m["content"]) <= chars + 3 for m in kept)
    assert kept[-1]["content"].startswith("Wrote /home/user/proj/file13.py"), "the newest survive"


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    from vaf.core.agent import Agent
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
    a.use_server = False
    a.llm = None
    return a


def _capture(agent):
    seen = {}

    def fake(messages=None, **kw):
        seen["prompt"] = messages[-1]["content"]
        seen["max_tokens"] = kw.get("max_tokens")
        yield "summary"

    agent.api_backend.chat_completion = fake
    return seen


SEGMENT = [
    {"role": "system", "content": "Previous Summary: The user builds a Paper plugin in /home/user/PlayLog."},
    {"role": "user", "content": "Und bitte immer echte Umlaute in allem, was Spieler sehen."},
    {"role": "assistant", "content": "Verstanden."},
]


def test_a_small_window_keeps_the_short_summary(agent):
    seen = _capture(agent)
    assert agent._generate_summary(SEGMENT, budget_tokens=200) == "summary"
    assert "2-3 concise sentences" in seen["prompt"] and seen["max_tokens"] == 200


def test_a_larger_window_gets_a_structured_merging_summary(agent):
    seen = _capture(agent)
    agent._generate_summary(SEGMENT, budget_tokens=ContextManager(45000).summary_budget_tokens())
    prompt = seen["prompt"]
    assert "Requests and instructions" in prompt and "standing instruction" in prompt
    assert "merge it" in prompt and "full paths" in prompt
    assert "immer echte Umlaute" in prompt, "the removed messages are what it summarizes"
    assert seen["max_tokens"] == 2700


def test_an_oversized_segment_is_cut_in_the_middle(agent):
    seen = _capture(agent)
    big = [{"role": "user", "content": "FIRST REQUEST"}] + [
        {"role": "assistant", "content": "y" * 400} for _ in range(400)] + [
        {"role": "user", "content": "LAST REQUEST"}]
    agent._generate_summary(big, budget_tokens=600)
    prompt = seen["prompt"]
    assert "FIRST REQUEST" in prompt and "LAST REQUEST" in prompt
    assert "[middle of the segment left out]" in prompt and len(prompt) < 25_000


def test_the_compression_hands_the_budget_to_the_summary():
    import inspect
    from vaf.core.agent import Agent
    src = inspect.getsource(Agent)
    assert "messages_for_llm, budget_tokens=cm.summary_budget_tokens())" in src
