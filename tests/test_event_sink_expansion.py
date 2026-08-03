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
    mgr = APIBackendManager("openai", config={},
                            caller_config={"api_key_openai": "test-key"})
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


# ── the result payload (added 2026-08-03) ───────────────────────────────────────────

def test_tool_end_carries_the_result_against_the_real_engine(real_agent):
    """The whole point of the field: a consumer can show WHAT a tool returned
    without correlating debug logs. Driven through the real dispatcher, not a
    stub, so a change in where the emit sits fails here."""
    events = []
    real_agent.set_event_sink(events.append)
    result = real_agent.execute_tool("list_files", {"path": "."})

    end = [e for e in events if e["type"] == "tool_end"][-1]
    assert "result" in end, "the sink lost the result payload"
    assert isinstance(end["result"], str)
    # The event carries the same answer the caller got (up to the cap).
    assert end["result"][:80] == str(result)[:80]


def test_tool_end_result_is_present_on_the_error_path(real_agent):
    """ok=False must still say WHY - an empty result on failures would send the
    consumer straight back to the debug logs the field exists to replace."""
    events = []
    real_agent.set_event_sink(events.append)
    real_agent.execute_tool("tool_that_does_not_exist_xyz", {})

    end = [e for e in events if e["type"] == "tool_end"][-1]
    assert end["ok"] is False
    assert "Unknown tool" in end["result"]


def test_event_result_is_always_a_capped_transport_safe_string():
    """The three clauses of the helper, each one a real failure mode."""
    from vaf.core.tool_dispatch import EVENT_RESULT_CHARS, event_result

    # 1. always a string, whatever a tool returned
    assert event_result(None) == ""
    assert event_result(42) == "42"
    assert event_result({"a": 1}) == "{'a': 1}"

    # 2. capped, with the total length still visible
    long = "x" * (EVENT_RESULT_CHARS + 500)
    capped = event_result(long)
    assert len(capped) < len(long)
    assert capped.startswith("x" * 100)
    assert "+500 chars" in capped

    # 3. surrogate-safe against the serialization the LANES use. Note the
    # default json.dumps escapes surrogates and would hide this: every writer
    # of sink events in the repo passes ensure_ascii=False (vaf/main.py,
    # vaf/cli/cmd/run.py stream-json, subagent_debug's events.jsonl), and that
    # combination raises UnicodeEncodeError on the byte encoding.
    import json
    raw = "ok \ud800 tail"
    with pytest.raises(UnicodeEncodeError):
        json.dumps({"result": raw}, ensure_ascii=False).encode("utf-8")

    dirty = event_result(raw)
    json.dumps({"result": dirty}, ensure_ascii=False).encode("utf-8")   # must not raise
    assert "ok" in dirty and "tail" in dirty


def test_the_short_path_is_byte_identical(real_agent):
    """A result under the cap must arrive verbatim - no ellipsis, no rewrap."""
    from vaf.core.tool_dispatch import event_result
    assert event_result("small answer") == "small answer"


def test_all_three_tool_end_emitters_carry_the_field():
    """The dispatcher is not the only emitter: the python_exec fallback and the
    parallel wrapper build their own tool_end. A field added to one of three is
    a consumer that works for most tools and silently not for two."""
    import inspect

    from vaf.core import agent as agent_mod
    from vaf.core import tool_dispatch as td

    sources = inspect.getsource(td) + inspect.getsource(agent_mod)
    starts = sources.count('"type": "tool_end"')
    carried = sources.count('"result": event_result(')
    assert starts >= 3, "an emitter disappeared - re-check this guard"
    assert carried == starts, (
        f"{starts} tool_end emitters but only {carried} carry the result")
