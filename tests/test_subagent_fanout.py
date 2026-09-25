# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Several reviewers at once: a fan-out of sub-agents in one round, delivered together.

The measured gap: a long build session ran 13 review rounds of 2 to 6 agents side by side, each
looking at the same work with its own focus, and read their findings together. VAF refused the
second sub-agent of a kind while one ran (the anti-re-delegation guard, right for its purpose:
a second coder writing into the same workspace) and delivered every result on its own.

Now the sub-agents of one kind that the model starts in ONE round are a fan-out: the guard lets
up to MAX_FANOUT through, the IPC task records the group (`fanout_id`, set around the dispatch),
and the drain holds a member's result until the last one is in, so the group arrives as one
answer. Only librarian_agent and research_agent (they read and report); only in API mode (local
mode is one llama server, Rule 4.6); a member is not validated alone against the whole request.

MUTATION: drop `hold_open_fanouts` from `_check_subagent_results` and the delivery test goes
red; drop the fan-out exemption in the guard and the second reviewer is refused; drop the
`fanout_scope` around `caller.execute` and the task records no group; drop the provider check
in `_fanout_for` and the local-mode test goes red.
"""
from datetime import datetime, timedelta

import pytest

from vaf.core import subagent_ipc as ipc_mod
from vaf.core.platform import Platform
from vaf.tools.base import BaseTool

SESSION = "green123456"


@pytest.fixture
def ipc(monkeypatch, tmp_path):
    """A SubAgentIPC with its queue files in a scratch folder (the shape
    tests/test_workflow_duplicate_guard.py uses)."""
    q = ipc_mod.SubAgentIPC()
    q.queue_dir = tmp_path
    q.pending_file = tmp_path / "pending_tasks.json"
    q.results_file = tmp_path / "completed_results.json"
    q.active_file = tmp_path / "active_tasks.json"
    q.paused_workflows_file = tmp_path / "paused_workflows.json"
    q.task_payloads_dir = tmp_path / "task_payloads"
    q._mutation_lock_file = tmp_path / ".mutation.lock"
    for f in (q.pending_file, q.results_file, q.active_file, q.paused_workflows_file):
        f.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: q)
    return q


def _task(ipc, agent_type="librarian_agent", fanout=None):
    with ipc_mod.fanout_scope(fanout):
        return ipc.create_task(agent_type, "review", session_id=SESSION)


def test_a_task_records_the_fanout_it_was_created_in(ipc):
    inside = _task(ipc, fanout="r1:librarian_agent")
    outside = _task(ipc)
    pending = {t.task_id: t for t in ipc.get_pending_tasks()}
    assert pending[inside].fanout_id == "r1:librarian_agent"
    assert pending[outside].fanout_id is None
    assert ipc_mod.current_fanout() is None, "the scope ends with its block"


def test_a_fanout_is_delivered_when_its_last_member_is_in(ipc):
    a = _task(ipc, fanout="r1:librarian_agent")
    b = _task(ipc, fanout="r1:librarian_agent")
    lone = _task(ipc)
    for tid in (a, b, lone):
        ipc.mark_task_running(tid)
    ipc.complete_task(a, "security: fine")
    ipc.complete_task(lone, "done")
    held = [t.task_id for t in ipc.hold_open_fanouts(ipc.get_pending_results())]
    assert held == [lone], "a member waits while its sibling still runs; a lone result never waits"
    ipc.complete_task(b, "style: two nits")
    both = sorted(t.task_id for t in ipc.hold_open_fanouts(ipc.get_pending_results()))
    assert both == sorted([a, b, lone])


def test_a_member_that_never_started_stops_holding_after_the_grace(ipc, monkeypatch):
    a = _task(ipc, fanout="r2:research_agent")
    _task(ipc, fanout="r2:research_agent")          # stays pending: its child never came up
    ipc.mark_task_running(a)
    ipc.complete_task(a, "topic one")
    assert ipc.hold_open_fanouts(ipc.get_pending_results()) == []
    later = datetime.now() + timedelta(seconds=ipc_mod.FANOUT_PENDING_GRACE_S + 5)

    class _Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return later

    monkeypatch.setattr(ipc_mod, "datetime", _Later)
    assert [t.task_id for t in ipc.hold_open_fanouts(ipc.get_pending_results())] == [a]


# ── the chat lane ─────────────────────────────────────────────────────────────

@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    from vaf.core.agent import Agent
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
    a.current_session_id = SESSION
    a._subagent_round = "r9"
    a._round_spawns = {}
    return a


def _live(agent, monkeypatch, *rows):
    monkeypatch.setattr(agent, "get_live_session_subagents", lambda: [
        {"task_id": f"t{i}", "agent_type": t, "task_description": "x", "running_seconds": 5,
         "fanout_id": f} for i, (t, f) in enumerate(rows)])


def test_the_guard_lets_a_fanout_through_and_caps_it(agent, monkeypatch):
    _live(agent, monkeypatch, ("librarian_agent", "r9:librarian_agent"))
    assert agent._chat_session_plumbing("librarian_agent", {"task": "security"}) is None
    agent._round_spawns["librarian_agent"] = agent.MAX_FANOUT
    capped = agent._chat_session_plumbing("librarian_agent", {"task": "style"})
    assert capped and "Not started" in capped


def test_one_of_an_earlier_round_still_blocks(agent, monkeypatch):
    _live(agent, monkeypatch, ("librarian_agent", "r1:librarian_agent"))
    refused = agent._chat_session_plumbing("librarian_agent", {"task": "again"})
    assert refused and "ALREADY RUNNING" in refused


def test_the_coder_never_fans_out(agent, monkeypatch):
    assert agent._fanout_for("coding_agent") is None
    _live(agent, monkeypatch, ("coding_agent", None))
    refused = agent._chat_session_plumbing("coding_agent", {"task": "second coder"})
    assert refused and "ALREADY RUNNING" in refused


def test_local_mode_does_not_fan_out(agent, monkeypatch):
    monkeypatch.setattr(agent, "provider", "local")
    assert agent._fanout_for("librarian_agent") is None


def test_the_dispatch_carries_the_group_into_the_spawn(agent, monkeypatch):
    seen = []

    class Reviewer(BaseTool):
        name = "librarian_agent"
        description = "stub"
        parameters = {"type": "object", "properties": {"task": {"type": "string"}}}

        def run(self, **kw):
            seen.append(ipc_mod.current_fanout())
            return "ok"

    agent.tools["librarian_agent"] = Reviewer()
    monkeypatch.setattr(agent, "get_live_session_subagents", lambda: [])
    agent.execute_tool("librarian_agent", {"task": "security"})
    assert seen == ["r9:librarian_agent"]
    assert ipc_mod.current_fanout() is None, "nothing leaks into the next call"


def test_a_member_is_not_validated_alone(agent, ipc, monkeypatch):
    # `ipc`: the result delivery consumes from the queue; this test's own, never the process's
    # (a queue file an earlier test encrypted under its own key is unreadable here).
    seen = {}

    def fake_validate(user_intent, desc, result, agent_type):
        seen["intent"] = user_intent
        return True, None

    monkeypatch.setattr(agent, "_validate_subagent_result_with_llm", fake_validate)

    class _MP:
        def get_subagent_delegation_intent(self, agent_type=None):
            return {"intent": "review the plugin from three angles", "goal": "x"}

        def __getattr__(self, name):
            return lambda *a, **k: None

    agent.main_persistence = _MP()

    def _result(fanout):
        return ipc_mod.SubAgentTask(
            task_id="m1", agent_type="librarian_agent", task_description="security",
            status="completed", created_at=datetime.now().isoformat(), session_id=SESSION,
            result="No injection paths found in the three handlers.", fanout_id=fanout)

    agent._process_subagent_result(_result(None))
    assert seen["intent"] == "review the plugin from three angles", "the control: a lone result is"
    seen.clear()
    agent._process_subagent_result(_result("r9:librarian_agent"))
    assert "intent" in seen and seen["intent"] == ""


def test_the_drain_holds_open_fanouts():
    import inspect
    from vaf.core.agent import Agent
    src = inspect.getsource(Agent._check_subagent_results)
    assert "return ipc.hold_open_fanouts(results)" in src


def test_the_dispatch_carries_the_authorizer_into_the_tool(agent, monkeypatch):
    """The same seam hands the application's authorizer to a tool that runs tools of its own
    in this process (the inline coder). MUTATION: drop authorizer_scope in execute_tool - red."""
    from vaf.core.tool_dispatch import current_authorizer
    seen = []

    def app_rule(req):
        return None

    class Inner(BaseTool):
        name = "probe_tool"
        description = "stub"
        parameters = {"type": "object", "properties": {}}

        def run(self, **kw):
            seen.append(current_authorizer())
            return "ok"

    agent.tools["probe_tool"] = Inner()
    agent.set_tool_authorizer(app_rule)
    agent.execute_tool("probe_tool", {})
    assert seen == [app_rule]
    assert current_authorizer() is None


def test_the_fanout_check_reads_both_queues_in_one_guarded_read(ipc, monkeypatch):
    """A member moving from pending to active between two separate reads would be in
    neither. MUTATION: read the two queues outside the mutation guard - red."""
    from contextlib import contextmanager
    a = _task(ipc, fanout="r3:librarian_agent")
    b = _task(ipc, fanout="r3:librarian_agent")
    ipc.mark_task_running(a)
    ipc.mark_task_running(b)                          # the sibling is still at work
    ipc.complete_task(a, "done")
    inside = []
    real_guard = ipc._mutation_guard

    @contextmanager
    def watched(*args, **kw):
        with real_guard(*args, **kw):
            inside.append(True)
            yield
            inside.pop()

    def reading(real):
        def _read(*args, **kw):
            assert inside, "a queue read outside the guard"
            return real(*args, **kw)
        return _read

    monkeypatch.setattr(ipc, "_mutation_guard", watched)
    monkeypatch.setattr(ipc, "get_active_tasks", reading(ipc.get_active_tasks))
    monkeypatch.setattr(ipc, "get_pending_tasks", reading(ipc.get_pending_tasks))
    # Read under the guard, the sibling is seen and the result waits. A read outside it
    # raises here, and the check fails open - delivering the member alone.
    assert ipc.hold_open_fanouts(ipc.get_pending_results()) == []
