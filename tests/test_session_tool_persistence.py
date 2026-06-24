"""Regression tests: the agent must stay aware of its own tool calls (and their
errors) across turns and session reloads.

Two mechanisms are covered:
  * the turn-end squash now records each tool's OUTCOME (OK/FAILED + snippet) in a
    "[Context: ...]" summary instead of just tool names (summarize_tool_turn), and
  * that summary (or the raw tool scaffolding, if not yet squashed) is persisted
    and restored across reloads.
"""
import re
import tempfile

from vaf.core.session import (
    Message,
    SessionManager,
    turn_context_messages_since_last_user,
)
from vaf.core.context import summarize_tool_turn, TURN_CONTEXT_PREFIX


# ---------------------------------------------------------------------------
# Message data model
# ---------------------------------------------------------------------------
def test_message_preserves_tool_fields_roundtrip():
    m = Message(role="tool", content="result", tool_call_id="c1", name="python_exec")
    d = m.to_dict()
    assert d["tool_call_id"] == "c1"
    assert d["name"] == "python_exec"
    restored = Message.from_dict(d)
    assert restored.tool_call_id == "c1"
    assert restored.name == "python_exec"


def test_from_dict_ignores_unknown_keys():
    m = Message.from_dict({"role": "user", "content": "hi", "legacy_unknown": 123})
    assert m.role == "user" and m.content == "hi"


def test_message_kind_roundtrips():
    """The proactive-bubble `kind` tag (drives the avatar animation) must survive persistence so the
    animation re-plays after a reload / chat-switch; it is omitted when None (backward-compatible)."""
    m = Message(role="assistant", content="Hey, noch da?", kind="nudge")
    d = m.to_dict()
    assert d["kind"] == "nudge"
    assert Message.from_dict(d).kind == "nudge"
    # None kind is omitted from the serialized dict and legacy messages load with kind=None.
    assert "kind" not in Message(role="assistant", content="x").to_dict()
    assert Message.from_dict({"role": "assistant", "content": "x"}).kind is None


# ---------------------------------------------------------------------------
# summarize_tool_turn: outcome + error snippet, not just names
# ---------------------------------------------------------------------------
def _squashable_steps():
    return [
        {"role": "assistant", "content": "<think>plan</think>",
         "tool_calls": [{"id": "c1", "function": {"name": "python_exec", "arguments": "{}"}}]},
        {"role": "tool", "name": "python_exec",
         "content": "Error executing tool: Object of type PosixPath is not JSON serializable"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c2", "function": {"name": "browser_agent", "arguments": "{}"}}]},
        {"role": "tool", "name": "browser_agent",
         "content": "No transcript or captions found for ZeLVTV6X1Jg."},
    ]


def test_summary_includes_outcomes_and_error():
    s = summarize_tool_turn(_squashable_steps())
    assert s is not None and s.startswith(TURN_CONTEXT_PREFIX)
    assert "python_exec" in s and "FAILED" in s
    assert "PosixPath is not JSON serializable" in s  # actual error preserved
    assert "browser_agent" in s and "OK" in s
    assert "reasoning: 1 steps" in s  # one <think> block counted


def test_summary_snippet_is_bounded_and_single_line():
    big = [{"role": "tool", "name": "x", "content": "line1\n" + ("A" * 5000)}]
    s = summarize_tool_turn(big, snippet_limit=200)
    assert "\n- x → " in s  # the tool line itself
    # the snippet portion must be collapsed + truncated
    tool_line = [l for l in s.splitlines() if l.startswith("- x")][0]
    assert len(tool_line) < 260 and "…" in tool_line


def test_summary_none_when_nothing_meaningful():
    assert summarize_tool_turn([{"role": "assistant", "content": "plain text"}]) is None
    assert summarize_tool_turn([]) is None


# ---------------------------------------------------------------------------
# turn_context_messages_since_last_user: captures raw tools AND the summary
# ---------------------------------------------------------------------------
def test_helper_captures_raw_tool_scaffolding():
    history = [
        {"role": "user", "content": "transcribe video"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "function": {"name": "python_exec", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "python_exec", "content": "boom"},
        {"role": "assistant", "content": "done"},  # plain text -> skipped
    ]
    out = turn_context_messages_since_last_user(history, "transcribe video")
    assert len(out) == 2
    assert out[0].get("tool_calls") and out[1]["role"] == "tool"


def test_helper_captures_squash_summary():
    history = [
        {"role": "user", "content": "transcribe video"},
        {"role": "system", "content": "[Context: tools used this turn]\n- python_exec → FAILED: boom"},
        {"role": "assistant", "content": "done"},
    ]
    out = turn_context_messages_since_last_user(history, "transcribe video")
    assert len(out) == 1
    assert out[0]["role"] == "system" and out[0]["content"].startswith("[Context:")


def test_helper_falls_back_to_latest_user_and_empty():
    history = [{"role": "user", "content": "x"},
               {"role": "system", "content": "[Context: tools used this turn]\n- t → OK: r"}]
    assert len(turn_context_messages_since_last_user(history, "nomatch")) == 1
    assert turn_context_messages_since_last_user([], "x") == []


# ---------------------------------------------------------------------------
# Persistence round-trip: raw tool linkage survives save -> load
# ---------------------------------------------------------------------------
def test_session_roundtrip_preserves_tool_linkage():
    with tempfile.TemporaryDirectory() as tmp:
        sm = SessionManager(storage_dir=tmp)
        sess = sm.new(name="rt")
        sess.add_message(role="user", content="transcribe video")
        sess.add_message(role="assistant", content="",
                         tool_calls=[{"id": "c1", "function": {"name": "python_exec", "arguments": "{}"}}])
        sess.add_message(role="tool", content="boom", tool_call_id="c1", name="python_exec")
        sess.add_message(role="assistant", content="done")
        sm.save(sess)

        loaded = sm.load(sess.id)
        asst = next(m for m in loaded.messages if m.role == "assistant" and m.tool_calls)
        tool = next(m for m in loaded.messages if m.role == "tool")
        assert asst.tool_calls[0]["id"] == "c1"
        assert tool.tool_call_id == "c1" and tool.name == "python_exec"


# ---------------------------------------------------------------------------
# The "[Context: ...]" summary survives save -> load -> reload-filter
# (mirrors load_session_context's system-message filter, which now whitelists it)
# ---------------------------------------------------------------------------
IGNORE_PATTERNS = ["System:", "Info:", "Step ", "Router:", "Queued input",
                   "Initializing Standalone Server", "Starting chat_step",
                   "Generation stopped", "Empty response detected"]


def _kept_by_reload_filter(role, content):
    if role != "system":
        return True
    is_turn_context = content.lstrip().startswith("[Context:")
    if any(p in content for p in IGNORE_PATTERNS) and "## PROJECT CONTEXT" not in content and not is_turn_context:
        return False
    return True


def test_summary_survives_persistence_and_reload_filter():
    # A summary whose snippet contains an ignored substring ("Step ") must still
    # be kept on reload thanks to the [Context: whitelist.
    summary = "[Context: tools used this turn]\n- python_exec → FAILED: Step 3 failed: boom"
    with tempfile.TemporaryDirectory() as tmp:
        sm = SessionManager(storage_dir=tmp)
        sess = sm.new(name="ctx")
        sess.add_message(role="user", content="transcribe video")
        sess.add_message(role="system", content=summary)
        sess.add_message(role="assistant", content="done")
        sm.save(sess)
        loaded = sm.load(sess.id)

    restored = [m for m in loaded.messages if _kept_by_reload_filter(m.role, m.content)]
    sys_summary = next((m for m in restored if m.role == "system"), None)
    assert sys_summary is not None, "[Context: summary must survive the reload filter"
    assert "PosixPath" not in sys_summary.content  # sanity: it's our summary
    assert sys_summary.content.startswith("[Context:")
