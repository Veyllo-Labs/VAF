# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The tool-use audit line, written by the funnel instead of by one lane.

`tool_use_<date>.log` exists to answer one question: which session and which user scope
were behind a given tool call. It was written from exactly one place - the chat streaming
loop in `vaf/core/agent.py` - so it answered that question only for chat. Workflow steps,
librarian sub-tools, training samples and every tool an embedder registers through
`add_tool()` ran through the same funnel, were measured by the same funnel for the event
stream, and appeared in the file not at all. The lane that needed the answer least was the
only one producing it.

The move also fixes the placement. The chat loop wrote its line BEFORE dispatching, so a
call the policy or the account allowlist turned away still left a trace - and for a file
about tenant isolation, a rejected cross-tenant attempt is the most valuable line it can
hold. Putting the call next to the `tool_start` event would have looked symmetric and
silently dropped all four refusal paths, so the placement is pinned here rather than left
to whoever next tidies that function.

There was no test for any of this before: not for the line format the Logs window parses,
not for who writes it, not for what the preview contains.
"""
import inspect
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from vaf.core.tool_dispatch import ToolCaller
from vaf.tools.base import BaseTool

SCOPE = "deadbeef-0000-0000-0000-000000000000"   # synthetic; never a real scope UUID
SESSION = "green123456"                          # synthetic; never a real session id


class _Probe(BaseTool):
    name = "probe"
    description = "probe"
    permission_level = "read"
    parameters = {"type": "object", "properties": {}}

    def run(self, **kwargs):
        return "OK"


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VAF_LOG_DIR", str(tmp_path))
    return tmp_path


def _lines(log_dir) -> list:
    path = log_dir / f"tool_use_{datetime.now().strftime('%Y-%m-%d')}.log"
    if not path.exists():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _caller(tool=None, **kw):
    tool = tool if tool is not None else _Probe()
    kw.setdefault("user_scope_id", SCOPE)
    kw.setdefault("user_role", "user")
    kw.setdefault("session_id", SESSION)
    return ToolCaller({tool.name: tool}, **kw)


# ── every lane, not just chat ────────────────────────────────────────────────

def test_a_caller_with_no_agent_at_all_writes_the_audit_line(log_dir):
    """The embedder shape from tests/test_tool_caller.py: a registry and nothing else. This
    is the whole point of the move - the workflow engine, the librarian and a third-party
    `add_tool` all build a ToolCaller like this and produced no line before."""
    assert _caller().execute("probe", {}) == "OK"

    lines = _lines(log_dir)
    assert len(lines) == 1, f"expected exactly one audit line, got {lines}"
    assert "tool=probe" in lines[0]
    assert f"session_id={SESSION}" in lines[0]
    assert f"user_scope_id={SCOPE}" in lines[0]


def test_the_chat_lane_no_longer_writes_its_own_line():
    """Source pin, and it is the ONLY guard for this - deliberately, after measuring.

    The removed call sat in `chat_step`'s streaming loop, which needs a live model stream to
    reach; `Agent.execute_tool` below is a different method and never runs it. A behavioural
    test through `execute_tool` was written first, and reinstating the duplicate left it
    green: it measured the funnel, not the loop the duplicate lived in. Source is what can
    actually see this one.

    House precedent for the shape: tests/test_librarian_shared_dispatch.py.
    """
    import vaf.core.agent as agent_mod

    assert "log_tool_use" not in inspect.getsource(agent_mod), (
        "the chat loop logs tool use again; the funnel already does it for every lane, so "
        "this is a second line per chat call, not a second lane"
    )


def test_the_agents_own_entry_point_reaches_the_funnel_once(log_dir):
    """`Agent.execute_tool` on the duck-typed agent from the dispatch baselines - the wiring
    from the product's entry point into the funnel, not just the funnel in isolation. Says
    the line appears exactly once from there, and (see above) nothing about `chat_step`."""
    from types import SimpleNamespace

    from vaf.core.agent import Agent
    from tests.conftest import bind_chat_stages

    tool = _Probe()
    fake = bind_chat_stages(SimpleNamespace(
        tools={"probe": tool}, _event_sink=None, _allow_once_tools={"probe"},
        _noninteractive=True, _current_turn_thinking_mode=False, _current_chat_source="web",
        current_session_id=SESSION, _current_user_scope_id=SCOPE, _current_user_role="admin",
        _current_username="tenant", _run_kind="chat", _ww_training=False,
        _active_tools=set(), _turn_ran_progress_tool=False, _session_workspace=None,
        history=[], main_persistence=None, _record_tool_used=lambda n: None,
        _plan_gate_decision=lambda n, t, tool_args=None: None,
        _working_memory_note_gate=lambda tool_args: None,
        _proactive_reply_gate_decision=lambda n, t, a: None,
        _ask_first_gate_decision=lambda n, t: None,
        get_live_session_subagents=lambda: [], _extract_subagent_goal=lambda a: "",
        model_display_name="probe",
    ))

    assert Agent.execute_tool(fake, "probe", {}) == "OK"
    assert len(_lines(log_dir)) == 1, (
        "the chat lane writes the audit line twice - once in the loop, once in the funnel"
    )


# ── the placement: refusals are the point ────────────────────────────────────

def test_a_call_the_policy_refused_is_still_recorded(log_dir):
    """An `admin_only` tool reached by a non-admin. The funnel returns before dispatching
    anything and emits NOTHING onto the event stream - deliberately, so no consumer reports
    a blocked tool as run. The audit line is the one artefact that must survive it."""
    tool = _Probe()
    tool.admin_only = True
    result = _caller(tool).execute("probe", {})

    assert result.startswith("Security Error:"), "precondition: the call really was refused"
    assert len(_lines(log_dir)) == 1, (
        "a refused call left no trace. This file exists for tenant-isolation questions, so "
        "the attempt somebody blocked is exactly the line it is opened for"
    )


def test_a_call_the_account_allowlist_refused_is_still_recorded(log_dir):
    """The second refusal path, and the one closest to the file's stated purpose: the tool
    is not enabled for this account."""
    import vaf

    vaf.set_account_allowlist_resolver(lambda scope: frozenset())
    try:
        result = _caller().execute("probe", {})
    finally:
        vaf.set_account_allowlist_resolver(None)

    assert result.startswith("Security Error:"), "precondition: the allowlist really refused"
    assert len(_lines(log_dir)) == 1


def test_an_unknown_tool_is_recorded_too(log_dir):
    """`self.tools.get(name)` returns None and the dispatch reports an unknown tool. A model
    reaching for something that is not there is still a call somebody may want to see."""
    caller = _caller()
    assert caller.execute("no_such_tool", {}).startswith("Error: Unknown tool")
    assert any("tool=no_such_tool" in ln for ln in _lines(log_dir))


# ── the preview ──────────────────────────────────────────────────────────────

def test_the_preview_is_the_sanitised_one(log_dir):
    """Heavy fields arrive as length + digest + bounded excerpt, not as the raw body. The
    transient event already got this treatment; the file that persists and is served over
    HTTP to admins should not be the less careful of the two."""
    tool = _Probe()
    tool.name = "write_file"
    caller = ToolCaller({"write_file": tool}, user_scope_id=SCOPE, user_role="user",
                        session_id=SESSION)
    caller.execute("write_file", {"file_path": "/tmp/x", "content": "S" * 5000})

    line = _lines(log_dir)[0]
    assert "content_sha256" in line or "content_len" in line, (
        f"the raw arguments went into the file instead of the sanitised preview: {line}"
    )


def test_a_path_in_the_arguments_no_longer_loses_the_line(log_dir):
    """The failure this replaces: `json.dumps` raises on a Path, the surrounding
    `except Exception: pass` swallows it, and the call is simply absent from the file with
    nothing to say it should have been there. `make_json_serializable` runs first now."""
    _caller().execute("probe", {"where": Path("/tmp/somewhere")})

    lines = _lines(log_dir)
    assert len(lines) == 1, "a Path argument made the audit line disappear again"
    assert "/tmp/somewhere" in lines[0]


# ── the format is a contract, because something parses it ────────────────────

def test_the_line_still_parses_for_the_logs_window(log_dir):
    """`vaf/api/logs_routes.py` reads this file by substring and a timestamp regex and
    renders the result in the Logs window. Nothing pinned that until now, so a tidier
    format (JSONL, a different separator) would have broken the reader silently."""
    from vaf.api.logs_routes import _read_tool_use_lines

    _caller().execute("probe", {"x": "1"})

    now = datetime.now()
    found = _read_tool_use_lines(log_dir, now.strftime("%Y-%m-%d"), "probe", "",
                                 now - timedelta(minutes=5), now + timedelta(minutes=5))
    assert len(found) == 1, "the Logs window can no longer read what the funnel writes"
    assert found[0]["ts"] and "tool=probe" in found[0]["line"]


def test_one_line_per_call_even_with_a_multiline_argument(log_dir):
    """Every reader of this file - including the one above - assumes one entry per line."""
    _caller().execute("probe", {"x": "a\nb\nc"})
    assert len(_lines(log_dir)) == 1


def test_the_writer_is_silent_when_debug_logging_is_off(log_dir, monkeypatch):
    from vaf.core.config import Config

    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: False if key == "debug_logs_enabled" else default))
    assert _caller().execute("probe", {}) == "OK"
    assert _lines(log_dir) == []


def test_a_broken_log_write_does_not_fail_the_tool_call(log_dir, monkeypatch):
    """Observation is fail-open here, the same as the event sink: a diagnostic must not be
    able to take a tool call down with it."""
    import vaf.core.log_helper as lh

    def _boom(*a, **kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(lh, "log_tool_use", _boom)
    assert _caller().execute("probe", {}) == "OK"


# ── the module stays library-clean ───────────────────────────────────────────

def test_the_audit_import_did_not_move_to_the_top_of_the_module():
    """tests/test_tool_caller.py already asserts the module imports on the slim base; this
    says WHY the log_helper import is written where it is, so the next reader does not
    'clean it up' into the header."""
    src = Path(__file__).resolve().parent.parent / "vaf" / "core" / "tool_dispatch.py"
    header = src.read_text(encoding="utf-8").split("def make_json_serializable", 1)[0]
    assert "log_helper" not in header
