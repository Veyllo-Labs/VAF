# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One turn binding for every lane that moves the shared agent (vaf/core/turn_binding.py).

VAF's runner serves every chat and every account of a worker with ONE agent. MEASURED BEFORE
THE FIX, on the real `Agent`, `load_session_context` and `chat_step` with only the provider
faked: after the owner's turn, another account's turn on the same agent carried the owner's
context summary, decision and goal in its system message, and the tool the owner's chat had
used ("archive_mail") in its tool array; a fresh agent serving the same turn had neither.

MUTATIONS, each turns the named tests red:
- add a `self._carry_probe = 1` store in `chat_step`, or delete one `AGENT_STATE` entry
  -> test_every_agent_attribute_has_a_lifetime;
- drop the `_recent_tools` line from `_reset_chat_state` -> test_recent_tools_belong_to_the_chat
  and test_nothing_carries_into_another_accounts_turn; add a "chat" entry nobody swaps
  (`_pending_user_question`) -> test_the_chat_class_is_swapped_by_the_loader;
- drop any one marker from `_write_markers` -> its test_bind_resets_every_binding_attribute
  parameter; for `_chat_project_dir`, which the bind writes again after the load,
  test_end_turn_clears_what_gates_and_routes instead, and dropping that second write
  -> test_the_prompt_is_built_for_the_bound_turn;
- bind the identity after the conversation, or apply `project_dir` before the load
  -> test_the_prompt_is_built_for_the_bound_turn;
- drop the re-assert after the load, or make it write a None username
  -> test_a_session_binding_reasserts_after_the_load;
- skip `init_chat` for an own conversation, bind its persistence before `init_chat`, or keep
  `current_session_id` -> test_an_own_conversation_carries_nothing_and_loads_nothing;
- drop the account check, or validate after the markers are written
  -> test_a_refused_or_invalid_binding_changes_nothing;
- leave `_room_turn`, `_synthetic_drain_turn` or the allowed set in `end_turn`
  -> test_end_turn_clears_what_gates_and_routes;
- drop the chat switch's context reset (`cm.reset_chat_state()` in `_reset_chat_state`), the
  `_recent_tools` reset, or any marker write -> test_nothing_carries_into_another_accounts_turn.
"""
import ast
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

import vaf
from vaf.core.identity_binding import Identity
from vaf.core.platform import Platform
from vaf.core.turn_binding import AGENT_STATE, TurnBinding, TurnRefused, bind_turn, end_turn

ROOT = Path(__file__).resolve().parents[1]

OWNER_SESSION = "red654321"
BOB_SESSION = "green123456"
BOB_NEW_SESSION = "yellow012345"        # no file yet: a new web chat

OWNER_SCOPE = "6f9619ff-8b86-d011-b42d-00c04fc964ff"
BOB_SCOPE = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"
CAROL_SCOPE = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"

OWNER = Identity(scope=OWNER_SCOPE, username="ownerq7", role="admin")
BOB = Identity(scope=BOB_SCOPE, username="bobq7", role="user")

MARKERS = ("OWNER7QSUMMARY", "OWNER7QDECISION", "OWNER7QGOAL")

# The twelve markers a binding sets beside the identity triple and the source, with the value
# a lane that says nothing means. _ABSENT: the attribute is deleted (the engine reads its
# absence: `_turn_is_human` as a person typing, `_excluded_tools` as nothing hidden).
_ABSENT = object()
NEUTRAL = {
    "_allowed_tools": None,
    "_front_office_mode": False,
    "_front_office_chat": None,
    "_front_office_contact": None,
    "_unattended_turn": False,
    "_turn_is_human": _ABSENT,
    "_turn_wake": None,
    "_room_turn": None,
    "_synthetic_drain_turn": False,
    "_excluded_tools": _ABSENT,
    "_chat_project_dir": "",
    "_turn_decision_note": None,
}
DIRTY = {
    "_allowed_tools": frozenset({"contact_history"}),
    "_front_office_mode": True,
    "_front_office_chat": {"channel": "whatsapp", "chat_id": "x"},
    "_front_office_contact": {"name": "Grace"},
    "_unattended_turn": True,
    "_turn_is_human": False,
    "_turn_wake": ("timer", "Erinnerung"),
    "_room_turn": {"room_id": "room-x", "mode": "autonomous"},
    "_synthetic_drain_turn": True,
    "_excluded_tools": {"web_search"},
    "_chat_project_dir": "/somewhere/else",
    "_turn_decision_note": "[Context: draft 4 was discarded]",
}


def _value(agent, attr):
    return getattr(agent, attr, _ABSENT)


def _seed(session_id, *, scope, username=None, messages=(), metadata=None):
    from vaf.core.session import Session, SessionManager
    session = Session(id=session_id, name=session_id)
    session.metadata["user_scope_id"] = scope
    if username:
        session.metadata["username"] = username
    session.metadata.update(metadata or {})
    for role, content in messages:
        session.add_message(role=role, content=content)
    SessionManager().save(session, sync_state=False)


class _Provider:
    """Records every request, the main call (it carries tools) and every side call alike."""

    def __init__(self, answer="Gern."):
        self.answer, self.texts, self.main, self.tools = answer, [], [], []

    def __call__(self, messages=None, **kw):
        self.texts.append(json.dumps(messages or [], ensure_ascii=False, default=str))
        if kw.get("tools"):
            self.main.append(messages or [])
            self.tools.append(sorted(t["function"]["name"] for t in kw["tools"]))
            yield self.answer
            return
        yield "no"


def _turn(agent, text="Was steht heute an?"):
    provider = _Provider()
    agent.api_backend.chat_completion = provider
    agent.chat_step(user_input=text, raw_user_input=text, stream_callback=lambda t: None)
    return provider


@pytest.fixture
def world(monkeypatch, tmp_path):
    import vaf.core.config as config_module
    import vaf.core.cross_chat as cross_chat
    import vaf.core.session as session_module
    import vaf.core.subagent_ipc as ipc
    from vaf.core.context import ContextManager

    # bind_turn declares the four context variables, which outlive the test on this thread.
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
    monkeypatch.setattr(cross_chat, "_contact_endpoints", lambda *a, **k: set())
    # The owner is the machine's admin.
    monkeypatch.setattr(config_module, "get_local_admin_scope_id", lambda: OWNER_SCOPE)

    _seed(OWNER_SESSION, scope=OWNER_SCOPE, username="ownerq7",
          messages=[("user", "Kuendige bitte den Handyvertrag"), ("assistant", "Erledigt.")])
    _seed(BOB_SESSION, scope=BOB_SCOPE, username="bobq7",
          messages=[("user", "BOB7QHIST Wie ist das Wetter?"), ("assistant", "Sonnig.")])

    def _make():
        from vaf.core.agent import Agent
        agent = Agent(register_signals=False, run_kind="chat",
                      config_overrides={"provider": "deepseek", "api_key_deepseek": "sk-test"})
        agent._current_chat_source = "web"
        monkeypatch.setattr(agent, "_try_workflow", lambda *args, **kw: None)
        agent.init_chat()
        return agent

    yield SimpleNamespace(make=_make, tmp=tmp_path, sessions=sessions)
    for var, token in reversed(tokens):
        var.reset(token)


def _web(identity, session_id, **kw):
    return TurnBinding(identity=identity, source="web", session_id=session_id, **kw)


# ── AC 1: every attribute has a lifetime ────────────────────────────────────────────────────

_LANE_FILES = (
    "vaf/core/headless_runner.py", "vaf/core/identity_binding.py", "vaf/core/turn_binding.py",
    "vaf/core/automation.py", "vaf/core/thinking_mode.py", "vaf/framework.py",
    "vaf/cli/cmd/run.py", "vaf/cli/tui_app/agent_bridge.py", "vaf/whare_wananga/runner.py",
)
# How the files that move an agent name it. A workflow engine (`engine`) and the facade's own
# `self` are other objects.
_AGENT_NAMES = {"agent", "_agent", "agent_instance", "core", "a"}
_AGENT_FIELDS = {"agent", "_agent", "_core", "core"}


def _stores(tree, receiver_ok):
    found = set()
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        elif isinstance(node, ast.Delete):
            targets = node.targets
        for target in targets:
            for t in (target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]):
                if isinstance(t, ast.Attribute) and receiver_ok(t.value):
                    found.add(t.attr)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "setattr" and len(node.args) >= 2
                and receiver_ok(node.args[0]) and isinstance(node.args[1], ast.Constant)):
            found.add(node.args[1].value)
    return found


def _agent_receiver(value):
    if isinstance(value, ast.Name):
        return value.id in _AGENT_NAMES
    return isinstance(value, ast.Attribute) and value.attr in _AGENT_FIELDS


def test_every_agent_attribute_has_a_lifetime():
    tree = ast.parse((ROOT / "vaf/core/agent.py").read_bytes())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Agent")
    stored = _stores(cls, lambda v: isinstance(v, ast.Name) and v.id == "self")
    for rel in _LANE_FILES:
        stored |= _stores(ast.parse((ROOT / rel).read_bytes()), _agent_receiver)

    unclassified = sorted(stored - set(AGENT_STATE))
    assert not unclassified, (
        f"attributes stored on an Agent with no lifetime in turn_binding.AGENT_STATE: "
        f"{unclassified} - decide whether each one is engine, step, chat, account or binding")
    gone = sorted(set(AGENT_STATE) - stored)
    assert not gone, f"AGENT_STATE names attributes nothing stores any more: {gone}"
    assert set(AGENT_STATE.values()) == {"engine", "step", "chat", "account", "binding"}


# ── AC 2: the chat class is swapped by the loader ──────────────────────────────────────────

def _methods(name_set):
    tree = ast.parse((ROOT / "vaf/core/agent.py").read_bytes())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Agent")
    return [f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name in name_set]


def test_the_chat_class_is_swapped_by_the_loader():
    """Every chat attribute is either assigned where a conversation is loaded or started, or
    reset where a conversation's state is dropped."""
    assigned = set()
    for fn in _methods({"load_session_context", "_enter_session", "init_chat",
                        "_bind_session_persistence"}):
        assigned |= _stores(fn, lambda v: isinstance(v, ast.Name) and v.id == "self")
    reset = set()
    for fn in _methods({"_reset_chat_state"}):
        reset |= _stores(fn, lambda v: isinstance(v, ast.Name) and v.id == "self")
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("getattr", "hasattr") and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)):
                reset.add(node.args[1].value)
    chat = {name for name, kind in AGENT_STATE.items() if kind == "chat"}
    unswapped = sorted(chat - assigned - reset)
    assert not unswapped, f"chat state no load or reset touches: {unswapped}"


def test_recent_tools_belong_to_the_chat(world):
    agent = world.make()
    agent.load_session_context(OWNER_SESSION)
    agent._record_tool_used("read_file")
    assert agent._get_recent_tools() == ["read_file"]

    agent.load_session_context(OWNER_SESSION, force=True)      # the same chat, rebuilt
    assert agent._get_recent_tools() == ["read_file"]
    agent.init_chat(keep_chat_state=True)                       # the same chat, a new prompt
    assert agent._get_recent_tools() == ["read_file"]

    agent.load_session_context(BOB_SESSION)                     # another chat
    assert agent._get_recent_tools() == []
    agent._record_tool_used("read_file")
    agent.init_chat()                                           # a fresh conversation
    assert agent._get_recent_tools() == []


# ── AC 3: bind resets every binding attribute ──────────────────────────────────────────────

@pytest.mark.parametrize("attr", sorted(NEUTRAL))
def test_bind_resets_every_binding_attribute(world, attr):
    agent = world.make()
    bind_turn(agent, _web(BOB, BOB_SESSION))
    # The agent is on Bob's chat, so the next load returns early: only the bind can clear it.
    setattr(agent, attr, DIRTY[attr])
    agent._current_chat_source = "whatsapp"

    bind_turn(agent, _web(BOB, BOB_SESSION))

    assert _value(agent, attr) == NEUTRAL[attr], f"{attr} outlived its turn"
    assert agent._current_chat_source == "web"


# ── AC 4: the prompt is built for the bound turn ───────────────────────────────────────────

def test_the_prompt_is_built_for_the_bound_turn(world):
    agent = world.make()
    bind_turn(agent, _web(OWNER, OWNER_SESSION))

    bind_turn(agent, TurnBinding(identity=BOB, source="whatsapp", session_id=BOB_NEW_SESSION,
                                 front_office=True))
    system = agent.history[0]["content"]
    assert "bobq7" in system and "ownerq7" not in system, (
        "the new chat's prompt was built for the identity bound before it")
    assert "## Security (Front Office)" in system, "the Front Office branch is missing"

    proj = world.tmp / "docs" / "VAF_Projects" / "ab12cd34" / "bob-proj"
    proj.mkdir(parents=True)
    bind_turn(agent, _web(BOB, BOB_SESSION, project_dir=str(proj)))
    assert agent._chat_project_dir == str(proj), "the load reset the chat's project folder"


# ── AC 5: a session binding re-asserts the caller after the load ───────────────────────────

def test_a_session_binding_reasserts_after_the_load(world):
    from vaf.core.subagent_ipc import get_current_room_id, get_current_session_id, get_current_turn_id
    _seed("web-carol-chat", scope=CAROL_SCOPE, username="carolq7")
    agent = world.make()

    bind_turn(agent, _web(BOB, "web-carol-chat", turn_id="turn-1", room_id="room-r"))
    assert (agent._current_user_scope_id, agent._current_username, agent._current_user_role) == (
        BOB_SCOPE, "bobq7", "user"), "the session's metadata replaced the caller's identity"
    assert get_current_session_id() == "web-carol-chat"
    assert get_current_turn_id() == "turn-1"
    assert get_current_room_id() == "room-r"

    bind_turn(agent, _web(OWNER, OWNER_SESSION))
    # A caller that names nobody keeps the name the session supplies.
    bind_turn(agent, _web(Identity(scope=CAROL_SCOPE, username=None), "web-carol-chat"))
    assert agent._current_username == "carolq7"
    assert get_current_turn_id() is None and get_current_room_id() is None


# ── AC 6: a conversation of its own ────────────────────────────────────────────────────────

def test_an_own_conversation_carries_nothing_and_loads_nothing(world, monkeypatch):
    from vaf.core.session import SessionManager
    from vaf.core.subagent_ipc import get_current_session_id
    agent = world.make()
    bind_turn(agent, _web(OWNER, OWNER_SESSION))
    agent.context_manager.state.narrative_summary = "OWNER7QSUMMARY"
    assert len(agent.history) > 1

    loads = []
    real_load = SessionManager.load
    monkeypatch.setattr(SessionManager, "load",
                        lambda self, *a, **k: loads.append(a) or real_load(self, *a, **k))
    bind_turn(agent, TurnBinding(identity=OWNER, source="web", conversation="room-x.p-abc"))

    assert loads == [], "a conversation of its own read a chat from the store"
    assert agent.current_session_id is None and get_current_session_id() is None
    assert [m["role"] for m in agent.history] == ["system"]
    assert agent._chat_project_dir == ""
    assert Path(agent.main_persistence.context_dir).name == "room-x.p-abc"
    assert agent.prompt_manager.mpm is agent.main_persistence, (
        "the prompt reads another store than the conversation's")
    assert not agent.context_manager.state.narrative_summary


# ── AC 7: refusal and validation change nothing ────────────────────────────────────────────

def _snapshot(agent):
    from vaf.core.subagent_ipc import get_current_session_id
    return ({attr: _value(agent, attr) for attr in list(NEUTRAL) + [
        "_current_user_scope_id", "_current_username", "_current_user_role",
        "_current_chat_source", "current_session_id"]},
            json.dumps(agent.history, default=str), get_current_session_id())


def test_a_refused_or_invalid_binding_changes_nothing(world):
    agent = world.make()
    bind_turn(agent, _web(OWNER, OWNER_SESSION))
    for attr, value in DIRTY.items():
        setattr(agent, attr, value)
    before = _snapshot(agent)

    invalid = [
        dict(identity=BOB, source="web", session_id=BOB_SESSION, conversation="room-x.p-abc"),
        dict(identity=BOB, source="web"),
        dict(identity=BOB, source="", session_id=BOB_SESSION),
        dict(identity=BOB, source="web", conversation=" "),
    ]
    for fields in invalid:
        with pytest.raises(ValueError):
            bind_turn(agent, TurnBinding(**fields))
        assert _snapshot(agent) == before, f"an invalid binding changed the agent: {fields}"
    with pytest.raises(TypeError):
        bind_turn(agent, _web(BOB, BOB_SESSION, allowed_tools="web_search"))
    assert _snapshot(agent) == before

    vaf.revoke_account(BOB_SCOPE)
    try:
        with pytest.raises(TurnRefused):
            bind_turn(agent, _web(BOB, BOB_SESSION))
        assert _snapshot(agent) == before, "a refused turn changed the agent"
    finally:
        vaf.restore_account(BOB_SCOPE)

    # No scope and the machine owner's scope are no account that can be taken away.
    bind_turn(agent, _web(Identity(scope=None, username=None), BOB_SESSION))
    bind_turn(agent, _web(OWNER, OWNER_SESSION))
    assert agent._current_user_scope_id == OWNER_SCOPE


# ── AC 8: end_turn ─────────────────────────────────────────────────────────────────────────

def test_end_turn_clears_what_gates_and_routes(world):
    from vaf.core.subagent_ipc import get_current_room_id
    agent = world.make()
    bind_turn(agent, TurnBinding(
        identity=BOB, source="whatsapp", session_id=BOB_SESSION, room_id="room-r",
        allowed_tools={"contact_history"}, front_office=True,
        front_office_chat={"channel": "whatsapp", "chat_id": "x"},
        front_office_contact={"name": "Grace"}, unattended=True, is_human=False,
        wake=("timer", "Erinnerung"), room_turn={"room_id": "room-r", "mode": "assist"},
        synthetic=True, excluded_tools={"web_search"}, project_dir=str(world.tmp)))
    agent._turn_decision_note = "[Context: draft 4 was discarded]"
    history = json.dumps(agent.history, default=str)

    end_turn(agent)

    for attr, neutral in NEUTRAL.items():
        assert _value(agent, attr) == neutral, f"{attr} outlived the turn"
    assert get_current_room_id() is None
    assert (agent._current_user_scope_id, agent._current_username, agent._current_user_role) == (
        BOB_SCOPE, "bobq7", "user")
    assert agent._current_chat_source == "whatsapp"
    assert agent.current_session_id == BOB_SESSION
    assert json.dumps(agent.history, default=str) == history


# ── AC 9: nothing carries from one account's turn into the next ────────────────────────────

def _task(session_id, identity, text, source="web", **meta):
    from vaf.core.task_queue import AgentTask
    metadata = {"user_scope_id": identity.scope, "username": identity.username,
                "role": identity.role, "enqueue_session_id": session_id,
                "task_class": "interactive", **meta}
    return AgentTask(session_id=session_id, input_text=text, source=source, metadata=metadata)


def _chat_state(agent):
    """The chat-class attributes as comparable values; timestamps excluded."""
    def _drop_times(d):
        return {k: v for k, v in d.items() if k not in ("last_updated", "captured_at")}
    cm = agent.context_manager
    return {
        "history": json.dumps(agent.history[1:], default=str, sort_keys=True),
        "current_session_id": agent.current_session_id,
        "main_persistence": str(agent.main_persistence.context_dir),
        "prompt_manager": str(agent.prompt_manager.mpm.context_dir),
        "context_manager": (_drop_times(asdict(cm.state)), _drop_times(asdict(cm.intent))),
        "_is_new_session": _value(agent, "_is_new_session"),
        "_recent_tools": dict(agent._recent_tools),
    }


def test_nothing_carries_into_another_accounts_turn(world):
    from vaf.core.headless_runner import _task_turn_binding
    from vaf.core.session import SessionManager
    session_mgr = SessionManager()
    text = "Was steht heute an?"

    shared = world.make()
    bind_turn(shared, _task_turn_binding(_task(OWNER_SESSION, OWNER, "Archiviere die Mail"),
                                         session_mgr))
    # What the owner's turn left on the agent: its context state, a tool it used, and every
    # marker a lane can set.
    cm = shared.context_manager
    cm.state.narrative_summary = MARKERS[0]
    cm.state.key_decisions = [MARKERS[1]]
    cm.intent.primary_goal = MARKERS[2]
    shared._record_tool_used("archive_mail")
    assert "archive_mail" in shared._get_recent_tools()
    for attr, value in DIRTY.items():
        setattr(shared, attr, value)

    bind_turn(shared, _task_turn_binding(_task(BOB_SESSION, BOB, text), session_mgr))
    carried = _turn(shared, text)

    fresh = world.make()
    bind_turn(fresh, _task_turn_binding(_task(BOB_SESSION, BOB, text), session_mgr))
    clean = _turn(fresh, text)

    requests = "\n".join(carried.texts)
    assert not [m for m in MARKERS if m in requests], "the owner's context reached Bob's turn"
    assert "BOB7QHIST" in requests, "the probe does not see Bob's own chat"
    assert carried.tools and carried.tools == clean.tools, (
        "Bob was offered another tool set than a fresh agent offers him")
    binding = sorted(n for n, kind in AGENT_STATE.items() if kind == "binding")
    assert {a: _value(shared, a) for a in binding} == {a: _value(fresh, a) for a in binding}
    assert _chat_state(shared) == _chat_state(fresh)


# ── AC 15: not on the facade ───────────────────────────────────────────────────────────────

def test_the_turn_binding_is_not_on_the_facade():
    for name in ("bind_turn", "end_turn", "TurnBinding", "TurnRefused"):
        assert not hasattr(vaf, name), f"vaf.{name} is exported; the binding is internal"
        assert name not in getattr(vaf, "__all__", ())
