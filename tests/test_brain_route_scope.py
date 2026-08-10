# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Context Window panel answers for ONE chat, and only for its owner.

Three defects were stacked here, and each hid the next.

The working memory is stored per session, with a shared legacy directory as the
fallback for lanes that have no session id - a scheduled automation declares
exactly that. The route resolved the session from process state, which inside an
HTTP request is always "no session", so the panel served that shared directory:
a chat displayed an automation's goal next to an unrelated lane's plan. The two
values the user saw came from two different lanes at once, which is what a
last-writer-wins bucket looks like.

Underneath it, the route had no auth dependency at all, so the agent's goal,
plan and tasks were readable by anyone who could reach the port. Adding the
session id to the request without an ownership check would have upgraded a
wrong-lane display bug into a deliberate cross-user read, so the two halves
belong in one change.

The ownership rule itself is `SessionManager.may_access` - the same one the
WebSocket commands use - so a session id cannot mean one thing over a socket and
another over HTTP.
"""
import pytest

from vaf.api import brain_routes

ALICE = "aaaa1111-2222-3333-4444-555555555555"
BOB = "bbbb1111-2222-3333-4444-555555555555"


def _user(scope, role="user"):
    return {"username": "u", "role": role, "user_scope_id": scope}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A session store plus a per-session working memory, both under tmp."""
    from vaf.core.session import Session, SessionManager

    manager = SessionManager(storage_dir=str(tmp_path / "sessions"))
    monkeypatch.setattr("vaf.core.session.get_manager", lambda: manager)

    for sid, scope in (("alice_chat", ALICE), ("bob_chat", BOB)):
        session = Session(id=sid, name=sid)
        session.metadata["user_scope_id"] = scope
        manager.save(session)

    monkeypatch.chdir(tmp_path)
    return manager


def _write_memory(session_id, *, intent, plan, tasks):
    from vaf.core.main_persistence import MainPersistenceManager
    import os

    mpm = MainPersistenceManager(os.getcwd(), session_id=session_id)
    mpm.update_user_intent(intent)
    mpm.update_working_memory(plan=plan, tasks=tasks)


def _write_legacy_global(*, intent):
    """What a session-less lane (a scheduled automation) leaves behind."""
    from vaf.core.main_persistence import MainPersistenceManager
    import os

    MainPersistenceManager(os.getcwd(), session_id=None).update_user_intent(intent)


def test_the_panel_answers_for_the_session_it_was_asked_about(store):
    _write_memory("alice_chat", intent="clone the repo and check the docs",
                  plan=["read the docs"], tasks=[{"text": "clone", "status": "pending"}])
    _write_legacy_global(intent="you are the morning weather assistant")

    out = brain_routes.get_brain(session_id="alice_chat", user=_user(ALICE))

    assert out["intent"] == "clone the repo and check the docs"
    assert "weather" not in out["intent"], "the legacy shared store leaked into a chat"
    assert [t["text"] for t in out["tasks"]] == ["clone"]


def test_without_a_session_it_answers_empty_not_the_shared_store(store):
    """MUTATION: fall back to the global store and this goes red.

    That fallback is what put an automation's goal into a chat, and an empty
    panel is the honest answer to "which chat?" when nobody said.
    """
    _write_legacy_global(intent="you are the morning weather assistant")

    out = brain_routes.get_brain(session_id=None, user=_user(ALICE))

    assert out == {"intent": "", "notes": [], "plan": [], "tasks": [], "agents": []}


def test_another_users_chat_is_refused(store):
    from fastapi import HTTPException

    _write_memory("bob_chat", intent="my tax situation", plan=[], tasks=[])

    with pytest.raises(HTTPException) as excinfo:
        brain_routes.get_brain(session_id="bob_chat", user=_user(ALICE))

    assert excinfo.value.status_code == 403


def test_the_admin_reaches_a_legacy_session_but_a_stranger_does_not(store):
    """A session with no recorded scope predates isolation: admin-only, not open."""
    from fastapi import HTTPException
    from vaf.core.session import Session

    store.save(Session(id="legacy_chat", name="legacy"))
    _write_memory("legacy_chat", intent="from before scopes", plan=[], tasks=[])

    assert brain_routes.get_brain(
        session_id="legacy_chat", user=_user(None, role="admin"))["intent"] == "from before scopes"

    with pytest.raises(HTTPException):
        brain_routes.get_brain(session_id="legacy_chat", user=_user(BOB))


def test_the_route_declares_an_auth_dependency():
    """It had none: the whole working memory was readable with no cookie at all.

    Asserted on the signature rather than through a client, because the defect
    was a MISSING Depends - a behavioural test would have to know which caller
    is unauthenticated to catch it, and the honest statement is simply that the
    route must not be reachable without the identity dependency.
    """
    import inspect

    param = inspect.signature(brain_routes.get_brain).parameters["user"]

    assert param.default is not inspect.Parameter.empty, "no auth dependency on the brain route"
    assert "Depends" in type(param.default).__name__ or callable(
        getattr(param.default, "dependency", None))
