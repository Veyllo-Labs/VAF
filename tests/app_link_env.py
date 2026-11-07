# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A home of the test's own for the app link: configuration, ssl directory, device store,
a fresh task queue, the security events captured in memory, and the Veyllo App lane on for
two accounts. And the worlds a request of the app's agent runs in: the real runner with only
the provider faked (``runner_world``, ``_run_app_turn``) and the real `Agent`
(``agent_world``), shared by the tests of requests and of their confirmation cards."""
import contextvars
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest

import vaf
from app_link_client import link, paired_device
from cert_factory import isolated_home
from vaf.core.identity_binding import Identity
from vaf.core.platform import Platform
from vaf.core.turn_binding import TurnBinding, bind_turn


def _devices():
    from vaf.core.app_link import devices
    return devices

SCOPE_A = "0a0b0c0d-0000-4000-8000-0000000000a1"
SCOPE_B = "0a0b0c0d-0000-4000-8000-0000000000b2"
A1 = "0f8fad5b-d9cb-469f-a165-70867728950e"
A2 = "1f8fad5b-d9cb-469f-a165-70867728950e"
A3 = "2f8fad5b-d9cb-469f-a165-70867728950e"
A4 = "3f8fad5b-d9cb-469f-a165-70867728950e"
A5 = "4f8fad5b-d9cb-469f-a165-70867728950e"
ASK_TEXT = "Welche Termine habe ich morgen?"


@dataclass
class AppLinkHome:
    root: Any
    ssl_dir: Any
    events: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)

    def lane(self, scope: str, on: bool) -> None:
        from vaf.core.config import Config
        with Config._locked():
            cfg = Config.load()
            by_scope = dict(cfg.get("connection_enabled_by_scope") or {})
            mine = dict(by_scope.get(scope) or {})
            mine["veyllo_app"] = on
            by_scope[scope] = mine
            cfg["connection_enabled_by_scope"] = by_scope
            Config.save(cfg)

    def set_block(self, **values) -> None:
        from vaf.core.config import Config
        with Config._locked():
            cfg = Config.load()
            block = dict(cfg.get("veyllo_app_config") or {})
            block.update(values)
            cfg["veyllo_app_config"] = block
            Config.save(cfg)

    def kinds(self) -> List[str]:
        return [kind for kind, _ in self.events]


@pytest.fixture
def app_link_home(tmp_path, monkeypatch):
    from vaf.core import security_events
    from vaf.core.app_link import pki
    from vaf.core.platform import Platform
    from vaf.core.task_queue import TaskQueue

    ssl_dir = isolated_home(tmp_path, monkeypatch, veyllo_app_config={
        "enabled": True, "relay_enabled": True, "relay_api_base": "https://relay.test",
        "relay_instance_id": "", "device_idle_days": 90},
        connection_enabled_by_scope={SCOPE_A: {"veyllo_app": True},
                                     SCOPE_B: {"veyllo_app": True}})
    vaf_dir = tmp_path / "vafdir"
    vaf_dir.mkdir()
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: vaf_dir))
    data_dir = tmp_path / "datadir"
    data_dir.mkdir()
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: data_dir))
    home = AppLinkHome(root=tmp_path, ssl_dir=ssl_dir)

    def _capture(kind, **fields):
        home.events.append((kind, fields))

    monkeypatch.setattr(security_events, "log_security_event", _capture)
    TaskQueue._instance = None  # type: ignore[attr-defined]
    pki.reset_context()
    yield home
    pki.reset_context()
    TaskQueue._instance = None  # type: ignore[attr-defined]


# ── the runner and the agent of a request ────────────────────────────────────────────────

class _LoopStop(BaseException):
    pass


class _Queue:
    def __init__(self, tasks):
        self.tasks = list(tasks)

    def get(self, *a, **k):
        return self.tasks.pop(0) if self.tasks else None

    def should_stop(self, session_id):
        return False

    def try_claim_session(self, session_id):
        return True

    def get_queue_stats(self):
        return {}

    def get_queue_size(self):
        return 0

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _Web:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


RUNNER_CHAT = "veyllo_app_k3y"
RUNNER_SCOPE = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"


@pytest.fixture
def runner_world(monkeypatch, tmp_path):
    import vaf.core.config as config_module
    import vaf.core.session as session_module
    import vaf.core.subagent_ipc as ipc
    from vaf.core.context import ContextManager
    monkeypatch.delenv("VAF_SESSION_ID", raising=False)
    tokens = [(var, var.set(ipc._UNSET)) for var in
              (ipc._session_ctx, ipc._turn_ctx, ipc._room_ctx, ipc._conversation_ctx)]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path / "vaf"))
    monkeypatch.setattr(Platform, "documents_dir", staticmethod(lambda: tmp_path / "docs"))
    monkeypatch.setattr(config_module.Config, "APP_DIR", tmp_path / "app", raising=False)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(session_module, "default_sessions_dir", lambda: sessions)
    monkeypatch.setattr(session_module, "_manager", None)
    monkeypatch.setattr(ContextManager, "ARCHIVE_DIR", tmp_path / "context_archive")
    monkeypatch.setattr(config_module, "get_local_admin_scope_id",
                        lambda: "6f9619ff-8b86-d011-b42d-00c04fc964ff")
    session = session_module.Session(id=RUNNER_CHAT, name="Veyllo App")
    session.metadata.update({"user_scope_id": RUNNER_SCOPE, "username": "bobq7",
                             "source": "veyllo_app"})
    session_module.SessionManager().save(session, sync_state=False)
    yield SimpleNamespace(tmp=tmp_path, sessions=sessions)
    for var, token in reversed(tokens):
        var.reset(token)


def run_runner(monkeypatch, agent, tasks):
    """The real runner loop over ``tasks`` on ``agent`` until the queue is empty, with the web UI
    and the process probes stubbed. Every other piece (the binding, the turn, the reply to the
    app) is the real one unless the caller replaced it."""
    import time as _time

    import vaf.core.headless_runner as hr
    import vaf.core.subagent_ipc as ipc_mod
    queue = _Queue(tasks)
    state = {"idle": 0, "error": False}
    logs = []
    monkeypatch.setattr(hr, "Agent", lambda *a, **k: agent)
    monkeypatch.setattr(hr, "TaskQueue", lambda: queue)
    monkeypatch.setattr(hr, "get_web_interface", lambda: _Web())
    monkeypatch.setattr(hr, "_WORKER_THREADS", {})
    monkeypatch.setattr(hr, "append_domain_log_always", lambda domain, msg: logs.append(msg))
    monkeypatch.setattr(Platform, "stop_webui_subagent_processes", staticmethod(lambda **k: 0))
    monkeypatch.setattr("vaf.core.memory_profiler.start_profiler", lambda *a, **k: None)
    monkeypatch.setattr("vaf.core.at_rest_migration.repair_context_copies_once", lambda: None)
    monkeypatch.setattr("vaf.memory.rag.turn_memory_context", lambda *a, **k: "")
    monkeypatch.setattr(ipc_mod, "_ipc_instance", None)

    def _sleep(seconds):
        if seconds >= 1:
            state["error"] = True
            raise _LoopStop()
        if not queue.tasks:
            state["idle"] += 1
            if state["idle"] >= 2:
                raise _LoopStop()

    monkeypatch.setattr(hr, "time", SimpleNamespace(time=_time.time, sleep=_sleep))
    with pytest.raises(_LoopStop):
        contextvars.copy_context().run(hr.run_headless_agent, worker_id=1, total_workers=1)
    assert not state["error"], f"the loop raised: {logs[-5:]}"
    return logs


def _run_app_turn(monkeypatch, world, text, **meta):
    import vaf.core.last_interaction as last_interaction
    from vaf.core.agent import Agent
    from vaf.core.app_link import bridge as app_bridge
    from vaf.core.task_queue import AgentTask
    agent = Agent(register_signals=False, run_kind="chat",
                  config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
    monkeypatch.setattr(agent, "_try_workflow", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_detect_ungrounded_result_claim", lambda text, res: (False, None))
    monkeypatch.setattr(agent, "collect_room_wake", lambda **k: None)
    agent.init_chat()
    seen = []

    def provider(messages=None, **kw):
        seen.append([dict(m) for m in (messages or [])])
        yield "Morgen ist frei."

    agent.api_backend.chat_completion = provider
    sent = {"finished": []}
    monkeypatch.setattr(app_bridge, "turn_started", lambda m: "rid")
    monkeypatch.setattr(app_bridge, "stream_round", lambda *a, **k: None)
    monkeypatch.setattr(app_bridge, "finish_reply",
                        lambda m, text: sent["finished"].append((dict(m), text)) or True)
    recorded = []
    monkeypatch.setattr(last_interaction, "update_last_interaction",
                        lambda **k: recorded.append(k))
    task = AgentTask(session_id=RUNNER_CHAT, input_text=text, source="veyllo_app", metadata={
        "user_scope_id": RUNNER_SCOPE, "username": "bobq7", "origin_channel": "veyllo_app",
        "task_class": "interactive", "app_device_id": "dev-1", **meta})
    run_runner(monkeypatch, agent, [task])
    from vaf.core.session import SessionManager
    stored = SessionManager()._read_session_file(world.sessions / f"{RUNNER_CHAT}.json")
    return SimpleNamespace(stored=stored, seen=seen, sent=sent, recorded=recorded)


BOB = Identity(scope=RUNNER_SCOPE, username="bobq7", role="user")


class _Tool(vaf.BaseTool):
    __module__ = "vaf.tools.notes"
    description = "A stand-in."
    permission_level = "write"
    side_effect_class = "reversible"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, name, *, restrictions=(), level="write", agent_request="allow"):
        self.name = name
        self.channel_restrictions = list(restrictions)
        self.permission_level = level
        self.agent_request = agent_request
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return "ok"


@pytest.fixture
def agent_world(runner_world, monkeypatch):
    from vaf.core.agent import Agent

    def _make():
        agent = Agent(register_signals=False, run_kind="chat",
                      config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
        agent._current_chat_source = "veyllo_app"
        # The plan gate asks for a plan before any write; not what these tests are about.
        agent._plan_gate_decision = lambda *a, **k: None
        agent.init_chat()
        return agent
    return SimpleNamespace(make=_make)


def _ask_turn(agent, asked=True):
    bind_turn(agent, TurnBinding(identity=BOB, source="veyllo_app", session_id=RUNNER_CHAT,
                                 asked_by_agent="veyllo_app" if asked else None))


# ── requests of the app's agent over the link ────────────────────────────────────────────

def _chat(scope=SCOPE_A):
    return _devices().chat_session_id(scope)


def _ask_chat(scope=SCOPE_A):
    """The session the app agent's requests run in, beside the account's chat."""
    return _devices().ask_session_id(scope)


def _queued(scope=SCOPE_A):
    """The queued turns of the account's chat and of its app-agent session."""
    from vaf.core.task_queue import TaskQueue
    queued = TaskQueue()._queued_by_session
    return list(queued.get(_chat(scope), ())) + list(queued.get(_ask_chat(scope), ()))


def _ask(row, text=ASK_TEXT, aid=A1, with_chat=True, **extra):
    message = {"t": "agent_ask", "id": aid, "text": text, **extra}
    if with_chat and "chat" not in extra:
        message["chat"] = _chat(row["user_scope_id"])
    return message


def _task_of(aid, scope=SCOPE_A):
    return [t for t in _queued(scope) if t.metadata.get("app_ask_id") == aid][0]


def _two_devices():
    asker = paired_device(SCOPE_A, model="Phone")
    other = paired_device(SCOPE_A, model="Tablet")
    away = paired_device(SCOPE_A, model="Old phone")
    client, _p1, row = link(SCOPE_A, device=asker)
    other_client, _p2, other_row = link(SCOPE_A, device=other, peer="10.0.0.6", connection="c2")
    return SimpleNamespace(client=client, row=row, other_client=other_client, other_row=other_row,
                           away=away[0], asker=asker)
