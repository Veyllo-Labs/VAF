# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The expanded event-sink contract (docs/OBSERVABILITY.md):

- llm_start/llm_end wrap every APIBackendManager.chat_completion call when a
  sink is attached (duration_ms, ok, best-effort usage), and the wrapper is a
  no-op passthrough without a sink.
- tool_end carries duration_ms and a dispatch-level ok flag (verified against
  a REAL engine instance, not a stub).
"""
import pytest

from vaf.core.api_backend import APIBackendManager


def _mgr(events=None):
    mgr = APIBackendManager("openai", config={}, api_key="test-key")
    if events is not None:
        mgr.event_sink = events.append
    return mgr


def test_llm_events_wrap_a_successful_stream():
    events = []
    mgr = _mgr(events)
    mgr._chat_completion_impl = lambda *a, **k: iter(["Hel", "lo"])
    out = list(mgr.chat_completion([{"role": "user", "content": "hi"}], model="gpt-4o"))
    assert out == ["Hel", "lo"]
    assert [e["type"] for e in events] == ["llm_start", "llm_end"]
    start, end = events
    assert start["provider"] == "openai" and start["model"] == "gpt-4o"
    assert end["ok"] is True
    assert isinstance(end["duration_ms"], int) and end["duration_ms"] >= 0
    assert isinstance(end["usage"], dict)


def test_llm_end_reports_not_ok_on_error_and_reraises():
    events = []
    mgr = _mgr(events)

    def _boom(*a, **k):
        raise RuntimeError("provider down")
        yield  # pragma: no cover - makes this a generator

    mgr._chat_completion_impl = _boom
    with pytest.raises(RuntimeError, match="provider down"):
        list(mgr.chat_completion([{"role": "user", "content": "hi"}]))
    assert [e["type"] for e in events] == ["llm_start", "llm_end"]
    assert events[1]["ok"] is False


def test_llm_end_reports_not_ok_when_stream_is_abandoned():
    events = []
    mgr = _mgr(events)
    mgr._chat_completion_impl = lambda *a, **k: iter(["a", "b", "c"])
    gen = mgr.chat_completion([{"role": "user", "content": "hi"}])
    assert next(gen) == "a"
    gen.close()  # consumer stops early (user stop)
    assert events[-1]["type"] == "llm_end"
    assert events[-1]["ok"] is False


def test_no_sink_means_pure_passthrough():
    mgr = _mgr(events=None)
    mgr._chat_completion_impl = lambda *a, **k: iter(["x"])
    assert list(mgr.chat_completion([{"role": "user", "content": "hi"}])) == ["x"]


def test_raising_sink_never_breaks_the_call():
    mgr = _mgr()

    def _bad_sink(evt):
        raise RuntimeError("consumer bug")

    mgr.event_sink = _bad_sink
    mgr._chat_completion_impl = lambda *a, **k: iter(["ok"])
    assert list(mgr.chat_completion([{"role": "user", "content": "hi"}])) == ["ok"]


# ── tool_end enrichment, against the real engine ──────────────────────────────


@pytest.fixture(scope="module")
def real_agent():
    import os

    os.environ.setdefault("VAF_NONINTERACTIVE", "1")
    from vaf.core.agent import Agent as CoreAgent

    return CoreAgent(
        verbose=False, register_signals=False, config_overrides={"provider": "local"}
    )


def test_tool_end_carries_duration_and_ok_true(real_agent):
    events = []
    real_agent.set_event_sink(events.append)
    result = real_agent.execute_tool("list_files", {"path": "."})
    assert isinstance(result, str)
    ends = [e for e in events if e["type"] == "tool_end" and e["tool"] == "list_files"]
    assert len(ends) == 1
    assert ends[0]["ok"] is True
    assert isinstance(ends[0]["duration_ms"], int) and ends[0]["duration_ms"] >= 0


def test_tool_end_reports_ok_false_for_unknown_tool(real_agent):
    events = []
    real_agent.set_event_sink(events.append)
    result = real_agent.execute_tool("tool_that_does_not_exist_xyz", {})
    assert "Unknown tool" in result
    end = [e for e in events if e["type"] == "tool_end"][-1]
    assert end["ok"] is False
    assert "duration_ms" in end
