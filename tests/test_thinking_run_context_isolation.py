# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A thinking run's flags belong to the run, not to the process.

MEASURED BEFORE, with the real `_run_thinking_for_user` on a thread and only `Agent` faked: the
run wrote six flags into os.environ for its whole duration (VAF_THINKING_MODE,
VAF_THINKING_SCOPE_ID, VAF_THINKING_RUN_ID, VAF_BACKGROUND_PRO and, when configured,
VAF_PROVIDER / VAF_MODEL_OVERRIDE). The web server hosts the chat worker in the same process,
so while one account's run was active another account's chat turn resolved DeepSeek Auto to
the pro model, lost its live status emits, had its own update_intent refused, and spawned its
sub-agents with the thinker's provider. A second run that ended first popped the flags of the
one still running. Model calls on helper threads (router, validation, failover deadline,
complete()) dropped the run's lane and account and were booked as the owner's main chat.

The run now declares itself in its own context (`subagent_ipc.BackgroundRun`) and hands its
provider and model to its own agent. Every test below names the mutation that turns it red.
"""
import asyncio
import concurrent.futures
import contextvars
import os
import re
import threading
import time
from pathlib import Path

import pytest

import vaf.core.agent as agent_mod
import vaf.core.cost as cost
import vaf.core.thinking_mode as tm
from vaf.core.api_backend import APIBackendManager
from vaf.core.bounded_run import call_with_deadline, run_bounded
from vaf.core.platform import Platform
from vaf.core.subagent_ipc import (BackgroundRun, get_current_background_run, in_thinking_run,
                                   set_current_background_run, set_current_session_id)

SCOPE_A = "ab12cd34-0000-4000-8000-00000000000a"
SCOPE_B = "ab12cd34-0000-4000-8000-00000000000b"
REPO = Path(__file__).resolve().parents[1]

# Process-wide flags a background run used to set, or that decide routing on their own.
_FLAGS = ("VAF_THINKING_MODE", "VAF_THINKING_SCOPE_ID", "VAF_THINKING_RUN_ID",
          "VAF_BACKGROUND_PRO", "VAF_BACKGROUND_RUN", "VAF_BACKGROUND_RUN_ID", "VAF_PROVIDER",
          "VAF_MODEL_OVERRIDE", "VAF_IN_WORKFLOW_TERMINAL", "VAF_IN_AUTOMATION",
          "VAF_COMPACTION_IN_PROGRESS")


# ── the provider the routing is read from ─────────────────────────────────────

class _CaptureProvider:
    def __init__(self):
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.last_request_usage = {"input_tokens": 0, "output_tokens": 0}
        self.received = None

    def chat_completion(self, messages, temperature, max_tokens, stream, model, tools, tool_choice):
        self.received = {"model": model}
        return iter([])


def _resolve() -> str:
    """The model a DeepSeek Auto call made HERE reaches the provider with: the real resolver."""
    m = APIBackendManager.__new__(APIBackendManager)
    m.provider_name = "deepseek"
    m.config = {}
    m.provider = _CaptureProvider()
    m.session_usage = {"input_tokens": 0, "output_tokens": 0}
    m.last_request_usage = {"input_tokens": 0, "output_tokens": 0}
    list(m.chat_completion([{"role": "user", "content": "hi"}], model="deepseek-auto"))
    return m.provider.received["model"]


# ── isolation ─────────────────────────────────────────────────────────────────

@pytest.fixture
def iso(monkeypatch, tmp_path):
    """Scratch stores, no routing flag in the environment, the thinker settings a dict away,
    and spawns that capture the child's environment instead of opening a terminal."""
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("VAF_LOG_DIR", str(tmp_path / "logs"))
    for var in _FLAGS:
        monkeypatch.delenv(var, raising=False)

    from vaf.core.config import Config
    settings = {"thinking_provider": "inherit", "thinking_model": None}
    real_get = Config.get.__func__

    def _get(cls, key, default=None):
        if key in settings:
            return settings[key]
        return real_get(cls, key, default)

    monkeypatch.setattr(Config, "get", classmethod(_get))

    spawned = {}

    def _capture(command, extra_env):
        spawned[command] = dict(extra_env)
        raise RuntimeError("captured, no terminal")

    # The real spawn funnel (the suite stubs it out), stopped where it would open a terminal.
    monkeypatch.setattr(Platform, "open_new_terminal", Platform._real_open_new_terminal)
    monkeypatch.setattr(Platform, "is_windows", staticmethod(lambda: False))
    monkeypatch.setattr(Platform, "is_macos", staticmethod(lambda: False))
    monkeypatch.setattr(Platform, "_with_env_prefix", staticmethod(_capture))
    return {"settings": settings, "spawned": spawned, "dir": tmp_path}


def _spawn_env(iso, tag: str) -> dict:
    """The environment a sub-agent spawned HERE would start with (the one spawn funnel)."""
    Platform.open_new_terminal(f"vaf-child-{tag}", extra_env={"VAF_SPAWN_MODE": "terminal"})
    return iso["spawned"][f"vaf-child-{tag}"]


# ── a parked run ──────────────────────────────────────────────────────────────

class _EndRun(Exception):
    pass


class _Slot:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.seen = {}
        self.overrides = "unset"


_SLOTS: dict = {}


def _observe(iso, tag: str) -> dict:
    run = get_current_background_run()
    return {
        "in_run": in_thinking_run(),
        "run_id": run.run_id if run else None,
        "model": _resolve(),
        "bounded": run_bounded(lambda: (in_thinking_run(), _resolve()), timeout=5, label="probe"),
        "deadline": call_with_deadline(lambda: (in_thinking_run(), cost.current_lane()),
                                       timeout=5, label="probe"),
        "emit": agent_mod._emit_to_web_ui(),
        "lane": cost.current_lane(),
        "scope": cost._SCOPE.get(),
        "env": dict(os.environ),
        "child_env": _spawn_env(iso, tag),
    }


def _parked_agent_class(iso):
    class _ParkedAgent:
        """Stands in for the run's Agent: it observes the run from inside, parks until the
        test releases it, observes again and ends the run."""

        def __init__(self, verbose=False, run_kind=None, config_overrides=None, **kw):
            run = get_current_background_run()
            slot = _SLOTS[run.run_id if run else "?"]
            slot.overrides = config_overrides
            slot.seen["start"] = _observe(iso, f"{run.run_id if run else '?'}-start")
            slot.entered.set()
            slot.release.wait(10)
            slot.seen["end"] = _observe(iso, f"{run.run_id if run else '?'}-end")
            raise _EndRun()

    return _ParkedAgent


def _swallow(fn, *args):
    try:
        fn(*args)
    except _EndRun:
        pass


def _start_run(scope: str, run_id: str) -> tuple:
    """The production shape: a daemon thread whose target is `_run_thinking_for_user`."""
    slot = _SLOTS[run_id] = _Slot()
    t = threading.Thread(target=_swallow, args=(tm._run_thinking_for_user, scope, run_id, time.time()),
                         daemon=True)
    t.start()
    assert slot.entered.wait(10), "the run never reached its agent"
    return slot, t


def _on_thread(fn):
    """Run ``fn`` on a fresh thread, the way the chat worker runs beside the run."""
    box = {}

    def _target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(20)
    if "error" in box:
        raise box["error"]
    return box["value"]


@pytest.fixture
def parked(iso, monkeypatch):
    monkeypatch.setattr(agent_mod, "Agent", _parked_agent_class(iso))
    _SLOTS.clear()
    yield iso
    for slot in _SLOTS.values():
        slot.release.set()


# ── 1. a chat turn beside a run keeps its own routing ─────────────────────────

def test_a_chat_turn_beside_a_thinking_run_is_routed_and_prompted_as_a_chat(parked):
    """The defect in one test: the run is active on its thread, another account's chat turn
    runs on another thread of the same process. MUTATION: write
    `os.environ["VAF_BACKGROUND_RUN"] = "thinking"` (or `VAF_BACKGROUND_PRO` with the
    resolver reading it) back into `_thinking_pass` and the chat turn goes pro, quiet and
    intent-less."""
    from vaf.core.main_persistence import MainPersistenceManager
    from vaf.tools.context_tools import UpdateIntentTool

    base = parked["dir"] / "work"
    base.mkdir()
    slot, t = _start_run(SCOPE_A, "runA0001")

    def _chat_turn():
        set_current_session_id("green123456")
        reply = UpdateIntentTool().run(base_dir=str(base), intent="Plan the trip to Lisbon")
        return {
            "in_run": in_thinking_run(),
            "model": _resolve(),
            "emit": agent_mod._emit_to_web_ui(),
            "intent_reply": reply,
            "intent": MainPersistenceManager(str(base), session_id="green123456").get_user_intent(),
            "child_env": _spawn_env(parked, "chat"),
        }

    chat = _on_thread(_chat_turn)
    slot.release.set()
    t.join(10)

    assert chat["in_run"] is False
    assert chat["model"] == "deepseek-flash", "a chat turn was routed as the background run"
    assert chat["emit"] is True, "a chat turn lost its live status emits to the background run"
    assert "intent not changed" not in chat["intent_reply"]
    assert chat["intent"] == "Plan the trip to Lisbon"
    assert "VAF_BACKGROUND_RUN" not in chat["child_env"]

    # ...while the run itself is still a background run, on its thread and in its workers.
    seen = slot.seen["start"]
    assert seen["in_run"] is True and seen["run_id"] == "runA0001"
    assert seen["model"] == "deepseek-v4-pro"
    assert seen["bounded"] == (True, "deepseek-v4-pro")
    assert seen["deadline"] == (True, "thinking")
    assert seen["emit"] is False


def test_the_run_writes_nothing_into_the_process_environment(parked):
    """Neither during nor after the run, with the thinker's provider and model set or not.
    MUTATION: put back any one of the six `os.environ[...] =` writes in `_thinking_pass`."""
    for provider, model in (("inherit", None), ("openai", "gpt-4o-mini")):
        parked["settings"].update(thinking_provider=provider, thinking_model=model)
        before = dict(os.environ)
        rid = f"run-{provider}"
        slot, t = _start_run(SCOPE_A, rid)
        slot.release.set()
        t.join(10)
        assert slot.seen["start"]["env"] == before, f"{provider}: the run changed os.environ"
        assert slot.seen["end"]["env"] == before
        assert dict(os.environ) == before


def test_the_thinker_settings_reach_the_run_agent_and_the_run_children_only(parked):
    """The thinker's provider and model are this run's: its own agent gets them as config
    overrides, a sub-agent it spawns gets them in its child environment, and nothing else in
    the process sees them. MUTATION: drop `config_overrides=` from the Agent construction
    (the overrides read None), or drop the child-env merge in `Platform.open_new_terminal`."""
    parked["settings"].update(thinking_provider="openai", thinking_model="gpt-4o-mini")
    slot, t = _start_run(SCOPE_A, "runP0001")
    chat_child = _on_thread(lambda: _spawn_env(parked, "chat-p"))
    slot.release.set()
    t.join(10)
    assert slot.overrides == {"provider": "openai", "model": "gpt-4o-mini"}
    child = slot.seen["start"]["child_env"]
    assert child["VAF_BACKGROUND_RUN"] == "thinking"
    assert child["VAF_BACKGROUND_RUN_ID"] == "runP0001"
    assert child["VAF_PROVIDER"] == "openai"
    assert child["VAF_MODEL_OVERRIDE"] == "gpt-4o-mini"
    for var in ("VAF_BACKGROUND_RUN", "VAF_PROVIDER", "VAF_MODEL_OVERRIDE"):
        assert var not in chat_child, f"a chat turn's sub-agent got the run's {var}"

    parked["settings"].update(thinking_provider="inherit", thinking_model=None)
    slot, t = _start_run(SCOPE_A, "runP0002")
    slot.release.set()
    t.join(10)
    assert slot.overrides is None
    assert "VAF_PROVIDER" not in slot.seen["start"]["child_env"]


def test_a_sub_agent_provider_of_its_own_beats_the_run_pin():
    """setdefault: the spawn's explicit value wins, as it won over the inherited one before."""
    run = BackgroundRun("thinking", "r1", provider="openai")
    env = {"VAF_PROVIDER": "anthropic"}
    for k, v in run.child_env().items():
        env.setdefault(k, v)
    assert env["VAF_PROVIDER"] == "anthropic"


# ── 2. two overlapping runs ───────────────────────────────────────────────────

def test_a_second_run_ending_first_leaves_the_first_one_its_flags(parked):
    """Run B of another account starts and ends while run A is parked. MUTATION: keep the
    declaration in a module global instead of the ContextVar (A then reads B's run id, or no
    run at all once B is done)."""
    slot_a, t_a = _start_run(SCOPE_A, "runA0001")
    slot_b, t_b = _start_run(SCOPE_B, "runB0002")
    slot_b.release.set()
    t_b.join(10)
    slot_a.release.set()
    t_a.join(10)
    end = slot_a.seen["end"]
    assert end["in_run"] is True
    assert end["run_id"] == "runA0001"
    assert end["model"] == "deepseek-v4-pro"
    assert end["scope"] == SCOPE_A
    assert slot_b.seen["end"]["scope"] == SCOPE_B


# ── 3. the lane and the account of every call ─────────────────────────────────

def test_the_run_books_its_calls_to_its_own_lane_and_account_from_the_start(parked):
    """Before the first chat_step (the reply classification runs there) the lane used to be
    `main` and the account none, so a member's run was booked to the owner's main chat.
    MUTATION: drop `set_usage_context` from `_thinking_pass`."""
    slot, t = _start_run(SCOPE_B, "runL0001")
    slot.release.set()
    t.join(10)
    seen = slot.seen["start"]
    assert seen["lane"] == "thinking"
    assert seen["scope"] == SCOPE_B
    assert seen["deadline"][1] == "thinking"


def test_the_caller_keeps_its_own_context_after_a_run(iso, monkeypatch):
    """Called on the caller's own thread (the run raises at once), nothing of the run stays
    behind there. MUTATION: call `_thinking_pass` directly instead of through
    `contextvars.copy_context().run`."""
    class _Boom:
        def __init__(self, *a, **kw):
            raise _EndRun()

    monkeypatch.setattr(agent_mod, "Agent", _Boom)

    def _caller():
        cost.set_usage_context(lane="main", scope="ab12cd34-0000-4000-8000-0000000000cc")
        with pytest.raises(_EndRun):
            tm._run_thinking_for_user(SCOPE_A, "runC0001", time.time())
        return get_current_background_run(), cost.current_lane(), cost._SCOPE.get()

    assert contextvars.copy_context().run(_caller) == (
        None, "main", "ab12cd34-0000-4000-8000-0000000000cc")


# ── 4. the readers ────────────────────────────────────────────────────────────

def _in_run(fn, run_id="runS0001", scope=SCOPE_A):
    """``fn()`` inside a declared thinking run, in a context of its own."""
    def _body():
        set_current_background_run(BackgroundRun("thinking", run_id))
        cost.set_usage_context(lane="thinking", scope=scope)
        return fn()
    return contextvars.copy_context().run(_body)


def test_the_emit_gate_reads_the_call_context_not_the_environment(iso, monkeypatch):
    """MUTATION: read `VAF_THINKING_MODE` again in `_emit_to_web_ui` (the first assertion goes
    red), or drop its automation half (the last one does)."""
    monkeypatch.setenv("VAF_THINKING_MODE", "1")
    assert agent_mod._emit_to_web_ui() is True
    assert _in_run(agent_mod._emit_to_web_ui) is False
    monkeypatch.delenv("VAF_THINKING_MODE")
    monkeypatch.setenv("VAF_IN_AUTOMATION", "1")
    assert agent_mod._emit_to_web_ui() is False


def test_update_intent_is_a_no_op_only_inside_a_run(iso, monkeypatch):
    """MUTATION: read `VAF_THINKING_MODE` again in `UpdateIntentTool.run`."""
    from vaf.core.main_persistence import MainPersistenceManager
    from vaf.tools.context_tools import UpdateIntentTool

    base = iso["dir"] / "work"
    base.mkdir()
    monkeypatch.setenv("VAF_THINKING_MODE", "1")

    def _write(intent):
        set_current_session_id("green123456")
        return UpdateIntentTool().run(base_dir=str(base), intent=intent)

    assert "intent not changed" not in contextvars.copy_context().run(_write, "Outside")
    assert MainPersistenceManager(str(base), session_id="green123456").get_user_intent() == "Outside"
    assert "intent not changed" in _in_run(lambda: _write("Inside"))
    assert MainPersistenceManager(str(base), session_id="green123456").get_user_intent() == "Outside"


def test_the_working_memory_mirror_runs_only_inside_a_run(iso, monkeypatch):
    """MUTATION: read `VAF_THINKING_MODE` again in `UpdateWorkingMemoryTool.run`."""
    import vaf.core.thinking_workspace as tw
    from vaf.tools.context_tools import UpdateWorkingMemoryTool

    mirrored = []
    monkeypatch.setattr(tw, "mirror_working_memory_snapshot",
                        lambda scope, snap: mirrored.append(scope))
    base = iso["dir"] / "work"
    base.mkdir()
    monkeypatch.setenv("VAF_THINKING_MODE", "1")

    def _note(text):
        set_current_session_id("green123456")
        return UpdateWorkingMemoryTool().run(base_dir=str(base), user_scope_id=SCOPE_A,
                                             add_notes=[text])

    contextvars.copy_context().run(_note, "outside")
    assert mirrored == []
    _in_run(lambda: _note("inside"))
    assert mirrored == [SCOPE_A]


def test_a_request_carries_the_run_id_of_its_own_context(iso, monkeypatch):
    """MUTATION: read `VAF_THINKING_RUN_ID` from the environment again in
    `deliver_tracked_message`."""
    import vaf.core.thinking_requests as tr

    monkeypatch.setattr(tm, "emit_message_to_web_ui", lambda scope, content, session_id=None: None)
    monkeypatch.setenv("VAF_THINKING_RUN_ID", "runX9999")
    contextvars.copy_context().run(
        tm.deliver_tracked_message, "u-out", "Soll ich das erledigen?", source_note_id="n1")
    _in_run(lambda: tm.deliver_tracked_message("u-in", "Soll ich das erledigen?",
                                               source_note_id="n2"), run_id="runR0001")
    assert [r.get("thinking_run_id") for r in tr.list_requests("u-out")] == [None]
    assert [r.get("thinking_run_id") for r in tr.list_requests("u-in")] == ["runR0001"]


def test_a_pro_resolution_names_its_trigger_and_lane(iso, monkeypatch):
    """MUTATION: remove the `append_domain_log` call from the DeepSeek Auto pro branch."""
    import vaf.core.log_helper as lh

    lines = []
    monkeypatch.setattr(lh, "append_domain_log", lambda domain, msg: lines.append((domain, msg)))
    assert _resolve() == "deepseek-flash"
    assert lines == []
    assert _in_run(_resolve) == "deepseek-v4-pro"
    assert lines == [("backend",
                      "[deepseek-auto] pro model=deepseek-v4-pro trigger=thinking_run lane=thinking")]


def test_the_environment_flags_alone_no_longer_make_a_thinking_run(iso, monkeypatch):
    """The names the run used to set mean nothing in this process any more."""
    for var in ("VAF_BACKGROUND_PRO", "VAF_THINKING_MODE", "VAF_THINKING_RUN_ID"):
        monkeypatch.setenv(var, "1")
    assert in_thinking_run() is False
    assert _resolve() == "deepseek-flash"


def test_a_child_of_a_run_reads_the_run_from_its_own_environment(iso, monkeypatch):
    """The process boundary: a sub-agent the run spawned carries the run in its env."""
    monkeypatch.setenv("VAF_BACKGROUND_RUN", "thinking")
    monkeypatch.setenv("VAF_BACKGROUND_RUN_ID", "runK0001")
    assert get_current_background_run() == BackgroundRun("thinking", "runK0001")
    assert _resolve() == "deepseek-v4-pro"
    # ...unless its own context says otherwise.
    assert contextvars.copy_context().run(
        lambda: (set_current_background_run(None), in_thinking_run())[1]) is False


# ── 5. every worker that makes a model call keeps the context ─────────────────

class _ProbeBackend:
    """An API backend that records what the call's own context says."""

    def __init__(self):
        self.seen = []

    def chat_completion(self, messages=None, **kw):
        self.seen.append((in_thinking_run(), cost.current_lane(), _resolve()))
        yield "list_tools"


def test_the_validation_call_keeps_the_run_context(iso):
    """MUTATION: submit `_ask` to a bare `ThreadPoolExecutor` again in `_run_validation_llm`."""
    import types

    backend = _ProbeBackend()
    ns = types.SimpleNamespace(use_server=False, api_backend=backend, llm=None)
    _in_run(lambda: agent_mod.Agent._run_validation_llm(ns, [{"role": "user", "content": "x"}],
                                                        max_tokens=5, timeout_s=5))
    assert backend.seen == [(True, "thinking", "deepseek-v4-pro")]


def test_the_failover_first_chunk_keeps_the_run_context(iso):
    """MUTATION: pull the first chunk through a bare `ThreadPoolExecutor` again."""
    seen = []

    def _gen():
        seen.append((in_thinking_run(), cost.current_lane()))
        yield "hello"

    m = APIBackendManager.__new__(APIBackendManager)
    first, failure = _in_run(lambda: m._first_chunk(_gen(), 5))
    assert (first, failure) == ("hello", None)
    assert seen == [(True, "thinking")]


def test_complete_with_a_timeout_keeps_the_run_context(iso):
    """MUTATION: submit `_run` to a bare `ThreadPoolExecutor` again in `_api_complete`."""
    from vaf.core.completion import complete

    backend = _ProbeBackend()
    assert _in_run(lambda: complete("x", provider="deepseek", backend=backend, timeout=5)) \
        == "list_tools"
    assert backend.seen and backend.seen[0][:2] == (True, "thinking")


def test_the_memory_answer_keeps_the_run_context(iso, monkeypatch):
    """MUTATION: `run_in_executor(None, ...)` again in `RagPipeline._generate_answer`."""
    import vaf.core.completion as completion
    from vaf.memory.rag import RagPipeline

    seen = []
    monkeypatch.setattr(completion, "complete",
                        lambda *a, **kw: seen.append((in_thinking_run(), cost.current_lane())) or "ok")
    p = RagPipeline.__new__(RagPipeline)
    assert _in_run(lambda: asyncio.run(p._generate_answer("q"))) == "ok"
    assert seen == [(True, "thinking")]


def test_the_browser_bridge_keeps_the_run_context(iso, monkeypatch):
    """MUTATION: `loop.run_in_executor(None, self._call_sync, ...)` again in `ainvoke`, or a
    bare `threading.Thread(target=_thread_run)` in `_run_async_in_new_loop`."""
    from vaf.tools import browser_agent as ba

    seen = []
    bridge = ba.VAFLLMBridge("deepseek-auto", "deepseek")
    monkeypatch.setattr(bridge, "_to_dicts", lambda messages: [{"role": "user", "content": "x"}])
    monkeypatch.setattr(bridge, "_call_sync", lambda raw, max_tokens: seen.append(
        (in_thinking_run(), cost.current_lane())) or "done")
    out = _in_run(lambda: ba._run_async_in_new_loop(bridge.ainvoke([])))
    assert getattr(out, "completion", out) == "done"
    assert seen == [(True, "thinking")]


def test_the_tool_router_keeps_the_run_context(iso, monkeypatch, tmp_path):
    """MUTATION: submit `_collect_router_chunks` to a bare `ThreadPoolExecutor` again."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    a = agent_mod.Agent(register_signals=False, run_kind="chat",
                        config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
    a.current_session_id = "green123456"
    backend = _ProbeBackend()
    a.api_backend.chat_completion = backend.chat_completion
    _in_run(lambda: a._route_tools("Was steht heute an?"))
    assert backend.seen and backend.seen[0][:2] == (True, "thinking")


def _probe():
    return (in_thinking_run(), cost.current_lane())


def test_generate_text_keeps_the_run_context(iso):
    """The document agent's in-process calls (`BaseTool.generate_text`). MUTATION: run
    `query_llm` on a bare `threading.Thread` again."""
    import types

    from vaf.tools.base import BaseTool

    iso["settings"]["provider"] = "deepseek"
    seen = []
    ns = types.SimpleNamespace(query_llm=lambda **kw: seen.append(_probe()) or "text")
    out = _in_run(lambda: BaseTool.generate_text(ns, [{"role": "user", "content": "x"}],
                                                 max_tokens=5))
    assert out == "text"
    assert seen == [(True, "thinking")]


def test_the_research_searches_keep_the_run_context(iso):
    """The single bounded search and the parallel fan-out (pool + per-query worker).
    MUTATION: a bare thread in `_bounded_web_search` or `_search_one`, or
    `executor.submit(_search_one, q)` without the context copy."""
    import types

    from vaf.tools import research_agent as ra

    seen = []
    web = types.SimpleNamespace(run=lambda **kw: seen.append(_probe()) or [
        {"href": f"https://example.org/{kw.get('query')}"}])
    assert _in_run(lambda: ra._bounded_web_search(web, 5, None, query="a")) == [
        {"href": "https://example.org/a"}]
    tool = ra.ResearchAgentTool.__new__(ra.ResearchAgentTool)
    merged = _in_run(lambda: tool._search_parallel(web, ["b", "c"], 3, False, 5))
    assert sorted(r["href"] for r in merged) == ["https://example.org/b", "https://example.org/c"]
    assert seen == [(True, "thinking")] * 3


def test_the_research_section_call_keeps_the_run_context(iso):
    """MUTATION: run the section's `query_llm` on a bare `threading.Thread` again."""
    from vaf.tools import research_agent as ra

    iso["settings"]["provider"] = "deepseek"
    seen = []
    tool = ra.ResearchAgentTool.__new__(ra.ResearchAgentTool)
    tool.query_llm = lambda **kw: seen.append(_probe()) or "<h2>T</h2><p>Body text here.</p>"
    results = "A search result long enough to count as one, about the topic at hand. " * 3
    _in_run(lambda: tool._summarize_section_html(topic="t", title="T", web_results=results,
                                                 sources=[], lang="en", min_words_target=150))
    assert seen and seen[0] == (True, "thinking")


def test_the_attachment_transfer_keeps_the_run_context(iso):
    """Inside a running loop the transfer gets a loop of its own on a worker. MUTATION: a bare
    `threading.Thread` in `_drive_transfer` again."""
    from vaf.tools import learn_attached_knowledge as la

    seen = []

    async def _transfer():
        seen.append(_probe())
        return 2

    async def _outer():
        return la._drive_transfer(_transfer)

    assert _in_run(lambda: asyncio.run(_outer())) == 2
    assert seen == [(True, "thinking")]


def test_a_transfer_that_fails_itself_runs_exactly_once(iso):
    """The tool is irreversible: a transfer that stored some documents and then raised a
    RuntimeError of its own (the database layer, the memory guard) must not be started again,
    with or without a running loop. MUTATION: find out whether a loop runs by catching the
    RuntimeError of `asyncio.run(...)` again (the transfer then runs twice outside a loop)."""
    from vaf.tools import learn_attached_knowledge as la

    starts = []

    async def _transfer():
        starts.append(1)  # stands for the documents stored before the failure
        raise RuntimeError("memory guard tripped")

    out = la._drive_transfer(_transfer)
    assert starts == [1]
    assert out == "Error: Failed to transfer attachment knowledge: memory guard tripped"

    starts.clear()

    async def _outer():
        return la._drive_transfer(_transfer)

    out = asyncio.run(_outer())
    assert starts == [1]
    assert out == "Error: Failed to transfer attachment knowledge: memory guard tripped"


def test_the_attachment_rag_calls_keep_the_run_context(iso, monkeypatch):
    """Both lanes of `attachment_rag._run_async`: the persistent vector runner (a job runs in
    the context it was submitted from) and the per-call worker inside a running loop.
    MUTATION: drop `job_ctx.run` in the runner, or a bare thread for the per-call worker."""
    import vaf.memory.attachment_rag as ar

    monkeypatch.setattr(ar, "_trip_memory_killer", lambda origin: False)
    seen = []

    async def _job():
        seen.append(_probe())
        return "ok"

    monkeypatch.setattr(ar, "_safe_mode_enabled", lambda: False)
    assert _in_run(lambda: ar._run_async(_job)) == "ok"
    monkeypatch.setattr(ar, "_safe_mode_enabled", lambda: True)

    async def _outer():
        return ar._run_async(_job)

    assert _in_run(lambda: asyncio.run(_outer())) == "ok"
    assert seen == [(True, "thinking"), (True, "thinking")]


def test_the_loop_bridges_are_the_one_helper():
    """Three byte-near copies of "a coroutine on a new thread and loop" were one helper's
    worth (the learn_document copy had no caller left and is gone). MUTATION: put a hand copy
    back in either module."""
    from vaf.core.bounded_run import run_coroutine_on_new_loop
    from vaf.tools import browser_agent, context_tools, learn_document

    for module in (browser_agent, context_tools):
        assert module._run_async_in_new_loop is run_coroutine_on_new_loop, module.__name__
    assert not hasattr(learn_document, "_run_async_in_new_loop")


def test_the_loop_bridge_keeps_the_context_and_hands_back_a_stop():
    """MUTATION: drop the context copy in `run_on_worker`, or catch only Exception."""
    from vaf.core.bounded_run import run_coroutine_on_new_loop

    async def _ok():
        return _probe()

    async def _stopped():
        raise asyncio.CancelledError()

    assert _in_run(lambda: run_coroutine_on_new_loop(_ok())) == (True, "thinking")
    # Bounded, so a stop that never reaches the caller fails the test instead of hanging it.
    with pytest.raises(asyncio.CancelledError):
        call_with_deadline(run_coroutine_on_new_loop, _stopped(), timeout=10)


def test_a_route_started_learn_books_to_its_own_account(iso, monkeypatch):
    """It runs on a plain thread on purpose (it outlives the request); its calls are booked to
    the account it learns for. MUTATION: drop the `usage_context(scope=...)` in `_run`."""
    from vaf.tools import learn_job as lj

    seen = []
    done = threading.Event()
    monkeypatch.setattr(lj, "_child_generate_fn", lambda: (lambda prompt: ""))

    def _learn(spec, **kw):
        seen.append((cost._SCOPE.get(), in_thinking_run()))
        return lj.LearnOutcome(status="complete", doc_tag=spec.doc_tag, doc_title="")

    monkeypatch.setattr(lj, "_learn_batches", _learn)
    assert _in_run(lambda: lj.start_background_learn(
        lj.LearnJobSpec(path="notes.md", doc_tag="doc-f024"), user_scope_id=SCOPE_B,
        on_done=lambda outcome: done.set()))
    assert done.wait(10)
    assert seen == [(SCOPE_B, False)]


# ── 6. call_with_deadline ─────────────────────────────────────────────────────

_PROBE: "contextvars.ContextVar[str]" = contextvars.ContextVar("f024_probe", default="unset")


def test_call_with_deadline_carries_the_context():
    """MUTATION: run `fn` without `ctx.run` in the worker."""
    def _body():
        _PROBE.set("caller")
        return call_with_deadline(_PROBE.get, timeout=5)
    assert contextvars.copy_context().run(_body) == "caller"


def test_call_with_deadline_does_not_wait_for_a_hung_worker():
    """MUTATION: run the call in `with ThreadPoolExecutor(...)` (it joins the worker)."""
    t0 = time.monotonic()
    with pytest.raises(concurrent.futures.TimeoutError):
        call_with_deadline(time.sleep, 5, timeout=0.2)
    assert time.monotonic() - t0 < 1.5


def test_the_worker_is_a_daemon_thread():
    """An abandoned worker must not hold up the exit of the process (a pool's workers are
    joined at exit). MUTATION: `daemon=False` in `run_on_worker`."""
    from vaf.core.bounded_run import run_on_worker

    with pytest.raises(concurrent.futures.TimeoutError):
        run_on_worker(time.sleep, 3, timeout=0.2, label="f024-daemon")
    workers = [t for t in threading.enumerate() if t.name == "vaf-worker-f024-daemon"]
    assert workers and all(t.daemon for t in workers)


def test_call_with_deadline_re_raises_the_callable_error():
    def _bad():
        raise ValueError("boom")
    with pytest.raises(ValueError, match="boom"):
        call_with_deadline(_bad, timeout=5)
    assert call_with_deadline(lambda: 7, timeout=None) == 7


# ── 7. static guards ──────────────────────────────────────────────────────────

def _src(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_the_retired_flag_names_are_gone_from_vaf():
    """MUTATION: put a `VAF_BACKGROUND_PRO` (or a scope/run-id env) reader back anywhere."""
    hits = []
    for path in sorted((REPO / "vaf").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for name in ("VAF_BACKGROUND_PRO", "VAF_THINKING_SCOPE_ID", "VAF_THINKING_RUN_ID"):
            if name in text:
                hits.append(f"{path.relative_to(REPO).as_posix()}: {name}")
    assert hits == []


def test_the_thinking_lane_writes_no_process_environment():
    """MUTATION: add any `os.environ[...] = ...` or `os.environ.pop(...)` to thinking_mode.py."""
    src = _src("vaf/core/thinking_mode.py")
    assert not re.search(r"environ\s*\[[^\]]+\]\s*=(?!=)", src)
    assert not re.search(r"environ\.(pop|setdefault|update|clear)\(|putenv\(", src)


def test_no_process_writes_the_background_run_into_its_own_environment():
    """The child-env names are data for a CHILD (`BackgroundRun.child_env`); a process that
    wrote them into its own environment would make every context of it a background run."""
    pattern = re.compile(r"environ\s*(\[\s*['\"]VAF_BACKGROUND_RUN|\.setdefault\(\s*['\"]VAF_BACKGROUND_RUN)")
    hits = [path.relative_to(REPO).as_posix() for path in sorted((REPO / "vaf").rglob("*.py"))
            if pattern.search(path.read_text(encoding="utf-8"))]
    assert hits == []


# Every raw thread, pool and executor start left in the code a thinking run's model calls are
# made from. Each one starts with an EMPTY context, so it is either free of model calls and of
# the run's identity, or it carries the context itself; the reason says which. A model call on
# a thread of its own goes through `vaf.core.bounded_run.run_on_worker` (or its wrappers
# `call_with_deadline` / `run_coroutine_on_new_loop`) and does not appear here.
_THREAD_START_ALLOWLIST = {
    ("vaf/core/thinking_mode.py", "_resolve_username_for_scope", "Thread"):
        (1, "username lookup in the account store; no model call"),
    ("vaf/core/thinking_mode.py", "_registered_scope_ids", "Thread"):
        (1, "account id lookup; no model call"),
    ("vaf/core/thinking_mode.py", "maybe_start_thinking_for_user", "Thread"):
        (1, "starts a run whose target runs the pass in a context of its own; an empty start is the point"),
    ("vaf/core/thinking_mode.py", "start_thinking_mode_background", "Thread"):
        (1, "the thinking loop for the life of the process; each run declares its own context"),
    ("vaf/tools/automation.py", "AutomationTool.run", "Thread"):
        (1, "starts an independent automation run in a terminal of its own; it must not inherit "
            "the creator's session, turn or background run"),
    ("vaf/tools/browser_agent.py", "BrowserAgentTool._run_browser", "Thread"):
        (1, "stop watchdog: polls the stop flag; no model call"),
    ("vaf/tools/browser_agent.py", "BrowserAgentTool._run_browser", "run_in_executor"):
        (5, "browser handover, workspace mirror, CDP resolution and idle parking; no model call"),
    ("vaf/tools/coder.py", "CodingAgentTool.run", "Thread"):
        (1, "terminal animation; no model call"),
    ("vaf/tools/learn_job.py", "start_background_learn", "Thread"):
        (1, "a learn started by a route, outliving the request and belonging to no turn or run; "
            "it books under its own account (usage_context in _run)"),
    ("vaf/tools/research_agent.py", "ResearchAgentTool.run._run_research_loop", "Thread"):
        (1, "opens the finished report in a browser; no model call"),
    ("vaf/tools/research_agent.py", "ResearchAgentTool.run", "Thread"):
        (1, "TUI refresher; no model call"),
    ("vaf/tools/research_agent.py", "ResearchAgentTool._search_parallel", "ThreadPoolExecutor"):
        (1, "each query is submitted through contextvars.copy_context().run"),
    ("vaf/tools/workflow_executor.py", "ExecuteWorkflowTool.run", "Thread"):
        (2, "IPC heartbeat and step watchdog; no model call"),
    ("vaf/memory/attachment_rag.py", "_ensure_vector_runner", "Thread"):
        (1, "the persistent vector runner; each job runs in the context it was submitted from"),
    ("vaf/memory/database.py", "check_db_connection_sync", "ThreadPoolExecutor"):
        (1, "database connection probe; no model call"),
    ("vaf/memory/embeddings.py", "EmbeddingService.embed", "run_in_executor"):
        (1, "local embedding compute; no backend model call, nothing booked"),
    ("vaf/memory/embeddings.py", "EmbeddingService.embed_batch", "run_in_executor"):
        (1, "local embedding compute; no backend model call, nothing booked"),
    ("vaf/memory/rag.py", "run_session_compaction_sync._schedule_profile_refresh", "Thread"):
        (1, "memory search and profile cache write; no model call, the scope is passed explicitly"),
    ("vaf/memory/rag.py", "run_session_compaction_sync", "Thread"):
        (1, "compaction ingest: embeddings and database writes; no model call"),
    ("vaf/memory/rag.py", "run_memory_search_sync", "Thread"):
        (1, "memory search: embeddings and database reads; its UI push names the scope explicitly"),
    ("vaf/memory/reembed.py", "ensure_embedding_model_current", "Thread"):
        (1, "startup hook: counts rows and spawns the re-embed process; no model call"),
}


def _thread_starts() -> dict:
    """(file, enclosing qualname, construct) -> count, read from the code with `ast`."""
    import ast
    from collections import Counter

    files = [REPO / "vaf" / "core" / n for n in
             ("agent.py", "api_backend.py", "completion.py", "thinking_mode.py")]
    files += sorted((REPO / "vaf" / "tools").glob("*.py")) + sorted((REPO / "vaf" / "memory").glob("*.py"))
    found: Counter = Counter()

    def _walk(node, stack, rel):
        for child in ast.iter_child_nodes(node):
            inner = stack + [child.name] if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else stack
            if isinstance(child, ast.Call):
                fn = child.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if name in ("Thread", "ThreadPoolExecutor", "run_in_executor"):
                    found[(rel, ".".join(stack) or "<module>", name)] += 1
            _walk(child, inner, rel)

    for path in files:
        _walk(ast.parse(path.read_text(encoding="utf-8")), [], path.relative_to(REPO).as_posix())
    return dict(found)


def test_every_raw_thread_start_on_the_model_call_path_is_accounted_for():
    """A new thread, pool or executor start in this code starts with an empty context, and a
    model call on it loses the run, its lane and its account. MUTATION: start a model call on
    a bare `threading.Thread` again (`BaseTool.generate_text`, a research worker, the
    attachment call thread), or delete an allowlisted start without removing its entry."""
    allowed = {key: count for key, (count, reason) in _THREAD_START_ALLOWLIST.items() if reason}
    assert _thread_starts() == allowed
