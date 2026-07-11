# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""WW B-track for ASYNC sub-agent failures (blue378604 audit, Fix 6).

Sub-agent failures never trigger the sync reactive lane: the tool result is only
the "[!] TASK DELEGATED" marker, the real error arrives later via the IPC drain.
The drain's failure message now carries the failed tool's know-how (relaxed gate,
UNVERIFIED-tagged) via the shared runtime.async_failure_hint, in BOTH drains
(Agent._process_subagent_result and the CLI TUI drain in vaf/cli/cmd/run.py).
The pitfall matcher also strips filesystem paths before matching, so path-laden
errors like "[Errno 17] File exists: '/x/y.html'" can reach the 0.6 overlap.
"""
import re
import types
from pathlib import Path

import pytest

import vaf.core.platform as platform_mod
from vaf.core.agent import Agent
from vaf.whare_wananga import delivery, runtime, store


@pytest.fixture(autouse=True)
def ww_home(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_mod.Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    delivery._load_classified.cache_clear()
    return tmp_path


def _save(tool, *, status="confirmed", challenge=True, mode="probe",
          pitfalls=(), procedure=("Provide project_path only for existing projects.",)):
    rec = store.new_record(tool, tool_schema_hash="h1")
    rec["status"] = status
    rec["challenge_passed"] = challenge
    rec["learn_mode"] = mode
    rec["tuatea"]["pitfalls"] = [{"text": p, "source": "whare_wananga", "seen": 1} for p in pitfalls]
    rec["tuarua"]["procedure"] = list(procedure)
    store.save(rec)
    return rec


# ── Matcher: path-stripping ──────────────────────────────────────────────────

def test_path_laden_error_matches_pitfall():
    _save("m1", pitfalls=("os.makedirs fails with errno 17 when a file exists at the target.",))
    err = "[Errno 17] File exists: '/some/other/VAF_Projects/chat9/page.html'"
    assert delivery.known_pitfall_hit("m1", err) is True


def test_pathless_matching_unchanged():
    _save("m2", pitfalls=("Empty content field yields an error.",))
    assert delivery.known_pitfall_hit("m2", "error: empty content field yields") is True
    assert delivery.known_pitfall_hit("m2", "some entirely unrelated failure") is False


def test_url_noise_tolerated():
    _save("m3", pitfalls=("Fetch fails with timeout after connect on slow hosts.",))
    err = "timeout after connect fetching https://example.com/some/long/path"
    assert delivery.known_pitfall_hit("m3", err) is True


# ── Shared helper ────────────────────────────────────────────────────────────

def test_async_failure_hint_delivers_unverified(monkeypatch):
    _save("t_async", status="draft", challenge=False)
    relearn_calls = []
    monkeypatch.setattr(runtime, "maybe_relearn",
                        lambda agent, tool, args, error: relearn_calls.append(tool))
    hint = runtime.async_failure_hint(object(), "t_async", "Some novel error text")
    assert hint is not None and "UNVERIFIED" in hint
    # novel error (no pitfall match) -> re-learn triggered even though a hint exists
    assert relearn_calls == ["t_async"]


def test_async_failure_hint_relearns_even_without_deliverable_knowhow(monkeypatch):
    # Confirmed record whose baskets yield no blocks: hint is None, but the
    # surprise must still feed the re-learn (review finding on the sync-style nesting).
    _save("t_empty", pitfalls=(), procedure=())
    relearn_calls = []
    monkeypatch.setattr(runtime, "maybe_relearn",
                        lambda agent, tool, args, error: relearn_calls.append(tool))
    hint = runtime.async_failure_hint(object(), "t_empty", "Another novel error")
    assert hint is None
    assert relearn_calls == ["t_empty"]


def test_async_failure_hint_never_raises(monkeypatch):
    monkeypatch.setattr(runtime, "maybe_relearn",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert runtime.async_failure_hint(object(), "no_record_tool", "err") is None


# ── Drain enrichment (Agent._process_subagent_result, unbound) ───────────────

class _FakeIPC:
    def __init__(self):
        self.consumed = []

    def consume_result(self, task_id):
        self.consumed.append(task_id)


def _fake_agent():
    a = types.SimpleNamespace()
    a.history = []
    a._async_subagent_tasks = {}
    a.main_persistence = None
    a.current_session_id = "s1"
    return a


def _failed_task(desc=None, error="Tool Error: something broke"):
    return types.SimpleNamespace(
        status="failed", error=error, agent_type="t_drain",
        task_id="task42", task_description=desc, result=None,
    )


@pytest.fixture
def fake_ipc(monkeypatch):
    ipc = _FakeIPC()
    import vaf.core.subagent_ipc as ipc_mod
    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: ipc)
    return ipc


def test_drained_failure_carries_knowhow(fake_ipc, monkeypatch):
    monkeypatch.setattr(runtime, "async_failure_hint",
                        lambda agent, tool, err: "Learned tool know-how (UNVERIFIED - draft record). Pitfalls: x")
    agent = _fake_agent()
    Agent._process_subagent_result(agent, _failed_task(desc="Build the page"))
    assert len(agent.history) == 1, "exactly ONE system message (adjacency)"
    msg = agent.history[0]["content"]
    assert "UNVERIFIED" in msg
    assert "Original task: Build the page" in msg
    assert fake_ipc.consumed == ["task42"], "result consumed exactly once"


def test_none_task_description_is_coerced(fake_ipc, monkeypatch):
    # Rule 4.7: from_dict passes persisted JSON through uncoerced - None must not
    # TypeError the drain (which would leave the result unconsumed -> duplicates).
    monkeypatch.setattr(runtime, "async_failure_hint", lambda *a: None)
    agent = _fake_agent()
    Agent._process_subagent_result(agent, _failed_task(desc=None))
    assert len(agent.history) == 1
    assert "Original task:" not in agent.history[0]["content"]
    assert fake_ipc.consumed == ["task42"]


def test_drain_survives_knowhow_crash(fake_ipc, monkeypatch):
    monkeypatch.setattr(runtime, "async_failure_hint",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    agent = _fake_agent()
    Agent._process_subagent_result(agent, _failed_task())
    assert len(agent.history) == 1, "failure message still delivered"
    assert fake_ipc.consumed == ["task42"], "exactly-once delivery survives a WW crash"


def test_task_description_is_truncated(fake_ipc, monkeypatch):
    monkeypatch.setattr(runtime, "async_failure_hint", lambda *a: None)
    agent = _fake_agent()
    Agent._process_subagent_result(agent, _failed_task(desc="x" * 1000))
    line = [l for l in agent.history[0]["content"].splitlines() if l.startswith("Original task:")][0]
    assert len(line) <= 320


def test_cancelled_tasks_get_no_knowhow(fake_ipc, monkeypatch):
    called = []
    monkeypatch.setattr(runtime, "async_failure_hint",
                        lambda *a: called.append(1) or None)
    agent = _fake_agent()
    task = _failed_task(error="[user_cancelled] stopped/cancelled by user via stop button")
    Agent._process_subagent_result(agent, task)
    assert called == [], "user cancel is not a failure to learn from"


# ── CLI TUI drain wiring guard ───────────────────────────────────────────────

def test_run_py_drain_calls_shared_helper():
    # The CLI TUI drain consumes results BEFORE the runner drain; losing this call
    # silently reopens the gap for `vaf run` users.
    import vaf.cli.cmd.run as run_mod
    src = Path(run_mod.__file__).read_text(encoding="utf-8")
    assert "async_failure_hint" in src, (
        "vaf/cli/cmd/run.py no longer attaches WW know-how to drained sub-agent failures"
    )
