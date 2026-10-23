# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A chat switch carries no other chat's context state.

One agent serves every chat of a runner worker, and its context manager - the intent and the
state behind the compressed summary: the summary itself, key decisions, the goal and its
sub-goals, the files, errors and tools noted - is ONE object for all of them. MEASURED BEFORE
THE FIX, on the real `load_session_context` and `chat_step` with only the provider faked: a
switch into a chat with nothing saved (a contact's first WhatsApp message, another account's
new web chat, a chat whose file holds only a sidebar document) kept the previous chat's state.
Its summary, decisions, goal, files and errors went into the new chat's system prompt, its
sub-goals into the router's side call, and the runner's save right after the load wrote all of
it into the new chat's file as that chat's own. The loader also built the new chat's system
message for the chat it was leaving: that chat's workspace folder and, when the caller had not
declared the session first, its working memory.

MUTATIONS, each turns the named tests red:
- M0 `ContextManager.reset_chat_state` does nothing -> the web, sidebar, contact, account,
  init_chat and clear tests;
- M1 drop the reset in `load_session_context` -> the web and sidebar tests and the account
  test's first half (the contact's no-file branch runs a plain `init_chat()` as well, so it
  stays green: two independent resets cover it);
- M2 reset only when the loaded `runtime_state` is empty -> the sidebar test;
- M3 drop the reset from `init_chat` -> the init_chat and clear tests;
- M4 plain `init_chat()` in the loader's try branch -> the account test's second half and the
  keep-site guard;
- M9 the loader without its fail-closed branch, or a half of it (the context variable, the
  working memory, the caller's identity) -> the failed-switch test;
- M10 reset on a rebuild of the same chat too -> the same-chat test;
- M11 restore the saved snapshot on a rebuild of the same chat -> the newer-live-state test;
- M12 the runner runs the turn after a failed load, does not answer it, or keeps a stop
  request meant for it -> the two failed-load tests;
- M13 a turn on an empty history without starting a conversation, or the fail-closed
  branch without the reset -> the failed-switch test;
- M14 the drain's notice to the agent's current session, or out without one -> the
  sub-agent-notice test;
- M5 reset after `SessionManager.load` -> the account test's second half;
- M6 declare the session after `init_chat` again -> the system message test;
- M7 reset by building a new `ContextManager` -> the init_chat test;
- M8 `reload_local_model` calls a plain `init_chat()` -> the keep-site guard (and
  tests/test_reload_local_model.py).
"""
import ast
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.core.front_office_tools import FRONT_OFFICE_ALLOWED_TOOLS
from vaf.core.platform import Platform

_REPO = Path(__file__).resolve().parents[1]

OWNER_SESSION = "red654321"
OTHER_NEW_SESSION = "green123456"       # another account's new web chat, as `new_session` writes it
OTHER_OWN_SESSION = "yellow012345"      # another account's chat with a saved state of its own
CONTACT_SESSION = "whatsapp_4915100000001"
DOC_ONLY_SESSION = "web-doc-only"

OWNER_SCOPE = "6f9619ff-8b86-d011-b42d-00c04fc964ff"
OTHER_SCOPE = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"
CALLER_SCOPE = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"

OWNER_TAG = "OWNER7Q"
OTHER_TAG = "OTHER7Q"
OWNER_NOTE = "OWNER7QNOTE the phone contract ends in March"


def _markers(tag):
    return [f"{tag}{kind}" for kind in
            ("SUMMARY", "DECISION", "GOAL", "SUBGOAL", "FILE", "ERROR", "TOOL")]


def _context_runtime_state(tag):
    """A `runtime_state` as the runner's save writes it, one unique marker per field."""
    return {
        "timestamp": "2026-10-01T10:00:00",
        "schema_version": "1.0",
        "providers": {"context": {
            "version": "1.0",
            "captured_at": "2026-10-01T10:00:00",
            "state": {
                "intent": {"primary_goal": f"{tag}GOAL", "sub_goals": [f"{tag}SUBGOAL"],
                           "constraints": [], "keywords": [], "last_updated": "2026-10-01T10:00:00"},
                "state": {"files_created": [[f"notes/{tag}FILE.md", 1]], "files_read": [],
                          "files_modified": [], "errors_encountered": [f"{tag}ERROR"],
                          "tools_used": [f"{tag}TOOL"], "key_decisions": [f"{tag}DECISION"],
                          "narrative_summary": f"{tag}SUMMARY",
                          "last_updated": "2026-10-01T10:00:00"},
                "max_tokens": 45000,
                "trigger_threshold": 0.85,
            },
        }},
    }


def _seed(session_id, *, scope, runtime_state=None, messages=()):
    from vaf.core.session import Session, SessionManager
    session = Session(id=session_id, name=session_id)
    session.metadata["user_scope_id"] = scope
    for role, content in messages:
        session.add_message(role=role, content=content)
    if runtime_state is not None:
        session.runtime_state = runtime_state
    SessionManager().save(session, sync_state=False)


def _empty(cm) -> bool:
    i, s = cm.intent, cm.state
    return not any((i.primary_goal, i.sub_goals, i.constraints, i.keywords,
                    s.narrative_summary, s.key_decisions, s.errors_encountered,
                    s.files_created, s.files_read, s.files_modified, s.tools_used,
                    s.code_snippets))


def _carried(text, tag=OWNER_TAG):
    return [m for m in _markers(tag) if m in text]


class _Provider:
    """Records every request, the main call (it carries tools) and every side call alike."""

    def __init__(self, answer="Gern."):
        self.answer, self.texts, self.main = answer, [], []

    def __call__(self, messages=None, **kw):
        self.texts.append(json.dumps(messages or [], ensure_ascii=False, default=str))
        if kw.get("tools"):
            self.main.append(messages or [])
            yield self.answer
            return
        yield "no"


def _turn(agent, text="Was hatten wir zuletzt besprochen?"):
    provider = _Provider()
    agent.api_backend.chat_completion = provider
    agent.chat_step(user_input=text, raw_user_input=text, stream_callback=lambda t: None)
    return provider


def _runner_save(agent, session_id, task=None):
    """What the runner does with a chat after its load and at the turn's end: load it with
    the agent's registry (or start it for a task without a file) and save it the same way."""
    from vaf.core.headless_runner import new_session_for_task
    from vaf.core.session import SessionManager
    sm = SessionManager(state_registry=agent.state_registry)
    try:
        session = sm.load(session_id)
    except FileNotFoundError:
        session = new_session_for_task(task)
    sm.save(session)
    return json.dumps(sm._read_session_file(sm.storage_dir / f"{session_id}.json"),
                      ensure_ascii=False)


@pytest.fixture
def make_agent(monkeypatch, tmp_path):
    import vaf.core.session as session_module
    import vaf.core.subagent_ipc as ipc
    from vaf.core.context import ContextManager

    # The loader declares the session in the context variable, which outlives the test on
    # this thread; a later test resolving "the current session" would read ours.
    monkeypatch.delenv("VAF_SESSION_ID", raising=False)
    token = ipc._session_ctx.set(ipc._UNSET)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path / "vaf"))
    monkeypatch.setattr(Platform, "documents_dir", staticmethod(lambda: tmp_path / "docs"))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(session_module, "default_sessions_dir", lambda: sessions)
    monkeypatch.setattr(session_module, "_manager", None)
    monkeypatch.setattr(ContextManager, "ARCHIVE_DIR", tmp_path / "context_archive")

    _seed(OWNER_SESSION, scope=OWNER_SCOPE, runtime_state=_context_runtime_state(OWNER_TAG),
          messages=[("user", "Kuendige bitte den Handyvertrag"), ("assistant", "Erledigt.")])

    def _make():
        from vaf.core.agent import Agent
        agent = Agent(register_signals=False, run_kind="chat",
                      config_overrides={"provider": "deepseek", "api_key_deepseek": "sk-test"})
        agent._current_chat_source = "web"
        monkeypatch.setattr(agent, "_try_workflow", lambda *args, **kw: None)
        agent.init_chat()
        return agent
    yield _make
    ipc._session_ctx.reset(token)


def _on_the_owners_chat(agent):
    """The agent served the owner's chat and ran a turn there, as a worker does."""
    agent.load_session_context(OWNER_SESSION)
    assert _carried(json.dumps(asdict(agent.context_manager.state)) +
                    json.dumps(asdict(agent.context_manager.intent))), \
        "the fixture did not restore the owner's state"
    _turn(agent, "Danke")


def test_a_new_web_chat_starts_without_the_last_chats_state(make_agent):
    _seed(OTHER_NEW_SESSION, scope=OTHER_SCOPE)
    agent = make_agent()
    _on_the_owners_chat(agent)

    agent.load_session_context(OTHER_NEW_SESSION)

    assert _empty(agent.context_manager), "the new chat took over the previous chat's state"
    provider = _turn(agent)
    assert not _carried("\n".join(provider.texts)), "a request carried the previous chat's state"
    assert not _carried(_runner_save(agent, OTHER_NEW_SESSION)), \
        "the runner's save wrote the previous chat's state into the new chat's file"


def test_a_chat_with_only_a_sidebar_document_starts_clean(make_agent):
    _seed(DOC_ONLY_SESSION, scope=OTHER_SCOPE,
          runtime_state={"sidebar_documents": [{"name": "brief.pdf", "content": "Angebot"}]})
    agent = make_agent()
    _on_the_owners_chat(agent)

    agent.load_session_context(DOC_ONLY_SESSION)

    assert _empty(agent.context_manager)
    provider = _turn(agent)
    assert not _carried("\n".join(provider.texts))


def test_a_contacts_first_message_carries_nothing_of_the_owners_chat(make_agent):
    agent = make_agent()
    _on_the_owners_chat(agent)

    # What the runner sets for a contact's turn: the owner's identity, the Front Office mode
    # and its allowed set, the channel as source; the chat has no file yet.
    agent._current_chat_source = "whatsapp"
    agent._front_office_mode = True
    agent._turn_is_human = True
    agent.set_allowed_tools(FRONT_OFFICE_ALLOWED_TOOLS)
    agent.load_session_context(CONTACT_SESSION)
    agent._current_user_scope_id = OWNER_SCOPE
    agent._current_username = "alice"
    agent._current_user_role = "admin"

    assert _empty(agent.context_manager)
    provider = _turn(agent, "Guten Tag, hat Herr Muster naechste Woche Zeit?")
    assert not _carried("\n".join(provider.texts)), "the contact's request carried the owner's state"
    task = SimpleNamespace(session_id=CONTACT_SESSION,
                           metadata={"user_scope_id": OWNER_SCOPE, "username": "alice",
                                     "whatsapp_chat_jid": "4915100000001@s.whatsapp.net"})
    assert not _carried(_runner_save(agent, CONTACT_SESSION, task)), \
        "the turn-end save wrote the owner's state into the contact's chat"


def test_another_account_on_the_same_agent_gets_none_and_keeps_its_own(make_agent):
    _seed(OTHER_NEW_SESSION, scope=OTHER_SCOPE)
    _seed(OTHER_OWN_SESSION, scope=OTHER_SCOPE, runtime_state=_context_runtime_state(OTHER_TAG),
          messages=[("user", "Plane die Reise"), ("assistant", "Gern.")])
    agent = make_agent()
    _on_the_owners_chat(agent)

    agent.load_session_context(OTHER_NEW_SESSION)
    agent._current_user_scope_id = OTHER_SCOPE
    agent._current_user_role = "user"
    provider = _turn(agent)
    assert not _carried("\n".join(provider.texts))
    assert not _carried(_runner_save(agent, OTHER_NEW_SESSION))

    agent._current_user_role = "admin"
    _on_the_owners_chat(agent)
    # The context broadcast re-loads the session with the registry and restores the stored
    # snapshot a second time; held off here, so what is pinned is the loader's own restore.
    agent._broadcast_context_status = lambda: None
    agent.load_session_context(OTHER_OWN_SESSION)
    agent._current_user_role = "user"

    live = json.dumps(asdict(agent.context_manager.state)) + json.dumps(asdict(agent.context_manager.intent))
    assert not _carried(live), "the other account's chat holds the owner's state"
    assert _carried(live, OTHER_TAG) == _markers(OTHER_TAG), "the chat's own saved state was not restored"
    provider = _turn(agent)
    requests = "\n".join(provider.texts)
    assert f"{OTHER_TAG}SUMMARY" in requests, "the chat's own summary did not reach the model"
    assert not _carried(requests)


def test_the_loaders_system_message_is_built_for_the_new_chat(make_agent):
    from vaf.core.main_persistence import MainPersistenceManager
    from vaf.core.session import get_session_workspace_dir

    _seed(OTHER_NEW_SESSION, scope=OTHER_SCOPE)
    owner_ws = get_session_workspace_dir(OWNER_SESSION, create=True, user_scope_id=OWNER_SCOPE)
    other_ws = get_session_workspace_dir(OTHER_NEW_SESSION, create=True, user_scope_id=OTHER_SCOPE)
    MainPersistenceManager(str(Path.cwd()), session_id=OWNER_SESSION).update_working_memory(
        add_notes=[OWNER_NOTE])
    agent = make_agent()
    agent.load_session_context(OWNER_SESSION)
    assert OWNER_NOTE in "\n".join(_turn(agent, "Danke").texts), \
        "the fixture's note never reached the owner's own prompt"

    # The drain shape: nobody declared the session before the load.
    agent.load_session_context(OTHER_NEW_SESSION)

    system = agent.history[0]["content"]
    assert str(owner_ws) not in system, "the new chat's prompt names the previous chat's workspace"
    assert OWNER_NOTE not in system, "the new chat's prompt carries the previous chat's working memory"
    assert str(other_ws) in system


def test_a_switch_that_fails_holds_no_chat(make_agent, monkeypatch):
    """The loader declares the target before it loads. A load that dies after that (here
    the prompt build fails in both branches) must not leave the agent claiming the new
    chat while it holds the previous chat's history: every later load of that chat would
    return early on the claim, and a turn would run on another account's messages."""
    from vaf.core.identity_binding import Identity, bind_identity
    from vaf.core.subagent_ipc import get_current_session_id

    _seed(OTHER_NEW_SESSION, scope=OTHER_SCOPE)
    _seed(OTHER_OWN_SESSION, scope=OTHER_SCOPE, runtime_state=_context_runtime_state(OTHER_TAG),
          messages=[("user", "Plane die Reise"), ("assistant", "Gern.")])
    agent = make_agent()
    agent.load_session_context(OWNER_SESSION)
    assert any("Handyvertrag" in str(m.get("content")) for m in agent.history)
    real_init_chat = agent.init_chat

    def broken(**kw):
        raise RuntimeError("the prompt could not be built")

    # The caller binds the turn's identity before the load, as the runner does from the task.
    caller = Identity(scope=CALLER_SCOPE, username="carol", role="user")
    bind_identity(agent, caller)
    monkeypatch.setattr(agent, "init_chat", broken)
    with pytest.raises(RuntimeError):
        # The target has a saved state, which the load restores before the prompt fails.
        agent.load_session_context(OTHER_OWN_SESSION)

    assert agent.history == [], "the failed switch kept the previous chat's history"
    assert _empty(agent.context_manager), "the failed target's restored state survived"
    assert agent.current_session_id is None, "the failed switch still claims the new chat"
    assert get_current_session_id() is None and agent._dispatch_session_id() is None, \
        "a tool dispatched now would run under the chat that failed to load"
    assert agent.main_persistence is None, "the failed chat's working memory is still bound"
    assert (agent._current_user_scope_id, agent._current_username) == (CALLER_SCOPE, "carol"), \
        "the identity is not the one the caller bound"

    # A caller that catches the failure and goes on (the thinking run, the terminal app's
    # switch) still sends its turn with a system prompt, and with nothing of either chat.
    monkeypatch.setattr(agent, "init_chat", real_init_chat)
    provider = _turn(agent)
    first = provider.main[0][0]
    assert first["role"] == "system" and "| home:" in first["content"], \
        "the turn after a failed load went out without the system prompt"
    assert not _carried("\n".join(provider.texts)) and not _carried("\n".join(provider.texts),
                                                                       OTHER_TAG)

    agent.load_session_context(OTHER_NEW_SESSION)
    assert agent.current_session_id == OTHER_NEW_SESSION
    assert agent.history[0]["role"] == "system"
    assert not any("Handyvertrag" in str(m.get("content")) for m in agent.history)


def test_a_rebuild_of_the_same_chat_keeps_its_state(make_agent):
    """`force=True` and a background append rebuild the chat the agent is on from its file.
    That is the same conversation, and a terminal chat stores no snapshot: a reset there
    would lose its summary to every thinking-mode question or automation result."""
    from vaf.core.session import SessionManager

    _seed(DOC_ONLY_SESSION, scope=OWNER_SCOPE,
          messages=[("user", "Kuendige bitte den Handyvertrag"), ("assistant", "Erledigt.")])
    agent = make_agent()
    agent.load_session_context(DOC_ONLY_SESSION)
    agent.state_registry.get_provider("context").restore_state(
        _context_runtime_state(OWNER_TAG)["providers"]["context"]["state"])

    agent.load_session_context(DOC_ONLY_SESSION, force=True)
    assert agent.context_manager.state.narrative_summary == f"{OWNER_TAG}SUMMARY"

    SessionManager().append_background_message(DOC_ONLY_SESSION, "Erinnerung: Termin um neun",
                                               kind="automation")
    agent.load_session_context(DOC_ONLY_SESSION)
    assert any("Termin um neun" in str(m.get("content")) for m in agent.history), \
        "the appended message was not read in"
    assert agent.context_manager.state.narrative_summary == f"{OWNER_TAG}SUMMARY"


def test_a_rebuild_keeps_a_newer_live_state_over_the_chats_saved_one(make_agent):
    """The chat's file holds a snapshot older than what the agent noted since. A forced
    rebuild of the same chat must not put the older one back; a switch away and back does
    restore the saved one. The context broadcast that ends every load restores the saved
    snapshot on its own and is held off, so this pins the loader."""
    agent = make_agent()
    agent._broadcast_context_status = lambda: None
    agent.load_session_context(OWNER_SESSION)
    agent.context_manager.state.narrative_summary = "LIVE7QSUMMARY newer than the file"
    agent.context_manager.state.key_decisions.append("LIVE7QDECISION")

    agent.load_session_context(OWNER_SESSION, force=True)

    assert agent.context_manager.state.narrative_summary == "LIVE7QSUMMARY newer than the file"
    assert "LIVE7QDECISION" in agent.context_manager.state.key_decisions

    _seed(OTHER_NEW_SESSION, scope=OTHER_SCOPE)
    agent.load_session_context(OTHER_NEW_SESSION)
    agent.load_session_context(OWNER_SESSION)
    assert agent.context_manager.state.narrative_summary == f"{OWNER_TAG}SUMMARY"


def test_a_chat_that_did_not_load_is_answered_as_failed(monkeypatch):
    """The runner does not run a turn on a chat that did not load: the agent holds no chat
    then, and storing the exchange could replace a chat file that did not load because it
    could not be read. It answers like a turn that raised: the error in the web UI's
    timeline, the turn closed, and the usual failure reply on a messenger."""
    import vaf.core.headless_runner as hr

    seen = []

    class _Web:
        def log(self, text, **kw):
            seen.append(("log", kw.get("level"), kw.get("session_id")))

        def emit_message_complete(self, content="", session_id=None):
            seen.append(("complete", content, session_id))

    monkeypatch.setattr(hr, "get_web_interface", lambda: _Web())
    monkeypatch.setattr(hr, "_send_turn_error_reply",
                        lambda task, error, contact=None: seen.append(("reply", task.session_id)))
    task = SimpleNamespace(session_id=CONTACT_SESSION, source="whatsapp", input_text="Hallo",
                           metadata={"from_contact": True})

    hr.answer_failed_load(task, RuntimeError("the chat could not be read"))

    assert seen == [("log", "error", CONTACT_SESSION), ("complete", "", CONTACT_SESSION),
                    ("reply", CONTACT_SESSION)]


def test_the_runner_runs_no_turn_on_a_chat_that_did_not_load():
    """MUTATION: drop the `continue`, or the answer, from the load's except branch - red."""
    import inspect

    import vaf.core.headless_runner as hr

    loop = inspect.getsource(hr.run_headless_agent).replace("\r\n", "\n")
    failed = loop.split('print(f"[Headless] Failed to load session context: {e}")', 1)[1]
    branch = failed.split("# Process Input", 1)[0]
    assert "answer_failed_load(task, e)" in branch
    after = branch.split("answer_failed_load(task, e)", 1)[1]
    assert "continue" in after
    # task_done keeps stop requests; one meant for this turn would stop the next one.
    assert after.index("tq.clear_stop(task.session_id)") < after.index("continue")


def test_a_sub_agent_result_that_cannot_be_summarized_tells_only_its_own_chat(monkeypatch):
    """After a load that failed closed the agent names no current session, and an emit
    without one goes to every connection. MUTATION: send the notice to the agent's current
    session again, or let the notice out without a session - red."""
    import inspect

    import vaf.core.headless_runner as hr

    sent = []

    class _Web:
        def emit_agent_message(self, role, content, session_id=None):
            sent.append(("message", session_id))

        def emit_message_complete(self, content="", session_id=None):
            sent.append(("complete", session_id))

    monkeypatch.setattr(hr, "get_web_interface", lambda: _Web())
    hr.drain_failure_notice(None, RuntimeError("load failed"))
    assert sent == [], "a notice without a chat went to every connection"
    hr.drain_failure_notice(OTHER_NEW_SESSION, RuntimeError("load failed"))
    assert sent == [("message", OTHER_NEW_SESSION), ("complete", OTHER_NEW_SESSION)]

    loop = inspect.getsource(hr.run_headless_agent).replace("\r\n", "\n")
    assert "drain_failure_notice(_drain_session, e)" in loop
    assert ('session_id = result_task.session_id or getattr(agent, "current_session_id", None)\n'
            '                                _drain_session = session_id\n') in loop


def test_init_chat_starts_a_fresh_conversation_unless_told_to_keep_it(make_agent):
    agent = make_agent()
    agent.load_session_context(OWNER_SESSION)
    cm = agent.context_manager
    provider = agent.state_registry.get_provider("context")
    limits = (cm.max_tokens, cm.trigger_threshold, cm.recent_memory_size)
    cm.archive.append("an archived snapshot")
    archives, created = cm.archive, list(cm.created_archives)

    agent.init_chat()

    assert agent.context_manager is cm and provider.context_manager is cm, \
        "the reset replaced the manager the state registry serializes"
    assert _empty(cm)
    assert (cm.max_tokens, cm.trigger_threshold, cm.recent_memory_size) == limits
    assert cm.archive is archives and cm.archive == ["an archived snapshot"]
    assert cm.created_archives == created
    assert [m["role"] for m in agent.history] == ["system"]
    snapshot = agent.state_registry.capture_snapshot().providers["context"]["state"]
    assert snapshot["intent"] == asdict(cm.intent)
    assert snapshot["state"]["narrative_summary"] == ""

    agent.load_session_context(OWNER_SESSION, force=True)
    agent.init_chat(keep_chat_state=True)

    assert agent.context_manager.state.narrative_summary == f"{OWNER_TAG}SUMMARY"
    assert [m["role"] for m in agent.history] == ["system"]


def test_clear_forgets_the_cleared_conversations_summary(make_agent):
    """`clear` in the terminal apps is `init_chat()` (CLI /clear, the TUI's clear). Their
    session managers carry no state registry, so a terminal chat's context state lives in
    memory only: seeded there, as a compression leaves it."""
    _seed(DOC_ONLY_SESSION, scope=OWNER_SCOPE,
          messages=[("user", "Kuendige bitte den Handyvertrag"), ("assistant", "Erledigt.")])
    agent = make_agent()
    agent.load_session_context(DOC_ONLY_SESSION)
    agent.state_registry.get_provider("context").restore_state(
        _context_runtime_state(OWNER_TAG)["providers"]["context"]["state"])
    assert _carried("\n".join(_turn(agent, "Weiter").texts)), "the fixture's summary was never sent"

    agent.init_chat()

    provider = _turn(agent)
    assert not _carried("\n".join(provider.texts)), "the cleared conversation's summary is still sent"


def _keep_sites():
    """Every call in vaf/ that passes `keep_chat_state` as anything but False, spelled as a
    keyword or inside a `**{...}` literal, as (file, innermost function)."""
    found = []

    def _keeps(kw):
        if kw.arg == "keep_chat_state":
            return not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
        return kw.arg is None and isinstance(kw.value, ast.Dict) and any(
            isinstance(k, ast.Constant) and k.value == "keep_chat_state" for k in kw.value.keys)

    class _Visitor(ast.NodeVisitor):
        def __init__(self, rel):
            self.rel, self.stack = rel, []

        def _func(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = visit_AsyncFunctionDef = _func

        def visit_Call(self, node):
            if any(_keeps(kw) for kw in node.keywords):
                found.append((self.rel, self.stack[-1] if self.stack else "<module>"))
            self.generic_visit(node)

    for path in sorted((_REPO / "vaf").rglob("*.py")):
        source = path.read_bytes().decode("utf-8")
        if "keep_chat_state" in source:
            _Visitor(path.relative_to(_REPO).as_posix()).visit(ast.parse(source))
    return found


def test_only_the_same_conversation_keeps_its_state():
    """Exactly two engine paths rebuild the prompt of the SAME conversation: the session
    loader (its body, `_enter_session`) after it restored the chat's own state, and the model
    swap. Every other `init_chat()` starts a fresh one, `clear` in both terminal apps
    included."""
    assert sorted(_keep_sites()) == [("vaf/core/agent.py", "_enter_session"),
                                     ("vaf/core/agent.py", "reload_local_model")]
