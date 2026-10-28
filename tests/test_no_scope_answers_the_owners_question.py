# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A turn with no scope is the machine owner's turn, for the whole background question.

No scope means the machine owner: the Discord bridge enqueues the owner's messages with
`user_scope_id=None`, and an agent embedded without `user_scope` acts as the owner. The waiting
record is keyed by `thinking_mode.scope_storage_key`, which names the owner "default" for no scope
and for the configured local-admin scope alike. The request store and the handoff bundle store were
keyed by the raw scope, and every writer files the owner's records under the configured scope.
MEASURED BEFORE THE FIX, on the real `chat_step` with only the provider faked: a reply from a turn
with no scope (the Discord bridge's owner lane, an embedded agent without `user_scope`) read and
deleted the owner's waiting record while the request stayed `asked`, the proposal, the findings and
the automation's handoff never reached the turn, the bundle stayed `open`, and an empty
`thinking_requests/_default` directory appeared. Both stores now resolve the owner's aliases
through `thinking_mode.scope_data_id`.

MUTATIONS, each turns the named tests red:
- M1 `scope_data_id` returns `_key(...)` unchanged -> the table, the Discord answer;
- M2 `thinking_requests._dir` back to raw keying -> the table, the Discord answer, the bare agent;
- M3 `handoff_bundle._dir` back to raw keying -> the table, the handoff;
- M4 `scope_data_id` returns the owner's scope for every input -> the table;
- M5 `_requests_by_key` back to the merge loop -> the dashboard (owner rows twice);
- M6 drop the `_default` skip in `_requests_by_key` -> the dashboard (a phantom user);
- M7 the pickup reads with no scope whatever scope is bound -> another account gains nothing.
"""
import asyncio
import json

import pytest

import vaf
from vaf.core import handoff_bundle as hb
from vaf.core import thinking_mode as tm
from vaf.core import thinking_requests as treq
from vaf.core.front_office_tools import FRONT_OFFICE_ALLOWED_TOOLS
from vaf.core.identity_binding import Identity, bind_identity, identity_from_metadata
from vaf.core.platform import Platform

OWNER = "3f2a9c1e-7b4d-4e8a-9c21-5d6e7f8a9b0c"
OTHER = "8d1e2f3a-4b5c-4d6e-8f70-1a2b3c4d5e6f"
SESSION = "discord_alice_1"
OWNER_SESSION = "red654321"

QUESTION = "Soll ich deinen Zahnarzttermin am Freitag absagen?"
PROPOSAL = "Absage des Zahnarzttermins per Mail an die Praxis Weber"
DETAILS = "Praxis Weber, Freitag 9:30, kostenlose Absage bis Donnerstag 18 Uhr"
STEP = "found three slots at the garage, Tuesday 8:00 is the cheapest"

# What the Discord bridge enqueues for the owner's own messages.
DISCORD_OWNER_META = {"user_scope_id": None, "username": "admin"}


@pytest.fixture
def stores(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path / "vaf"))
    # On the module attribute: both key helpers import it lazily.
    import vaf.core.config as config
    monkeypatch.setattr(config, "get_local_admin_scope_id", lambda: OWNER)
    monkeypatch.setattr(tm, "_latest_web_session_id", lambda scope: None)
    monkeypatch.setattr(tm, "_main_agent_busy", lambda scope: True)   # defer, no live emit
    return tmp_path / "vaf"


@pytest.fixture
def make_agent(stores, monkeypatch):
    def _make(identity: Identity, *, source="discord", front_office=False):
        from vaf.core.agent import Agent
        a = Agent(register_signals=False, run_kind="chat",
                  config_overrides={"provider": "deepseek", "api_key_deepseek": "sk-test"})
        a.current_session_id = SESSION
        bind_identity(a, identity)
        a._current_chat_source = source
        a._turn_is_human = True
        monkeypatch.setattr(a, "_try_workflow", lambda *args, **kw: None)
        if front_office:
            a._front_office_mode = True
            a.set_allowed_tools(FRONT_OFFICE_ALLOWED_TOOLS)
        a.init_chat()
        a._bind_session_persistence(SESSION)
        return a
    return _make


def _discord_owner():
    return identity_from_metadata(DISCORD_OWNER_META)


def _seed_question():
    """The owner's open background question, written the way the writers write it."""
    req = treq.add_request(OWNER, question=QUESTION, run_seq=1,
                           proposed_action=PROPOSAL, details=DETAILS)
    tm.set_waiting_for_reply(OWNER, username="alice", display_name="Alice",
                             question_text=QUESTION, request_id=req["id"],
                             session_id=OWNER_SESSION, channel="discord")
    return req


class _Provider:
    """Records every request, the main call (it carries tools) and every side call alike."""

    def __init__(self, answer="Gut, ich sage den Termin bei der Praxis Weber ab."):
        self.answer, self.texts, self.main = answer, [], []

    def __call__(self, messages=None, **kw):
        self.texts.append(json.dumps(messages or [], ensure_ascii=False, default=str))
        if kw.get("tools"):
            self.main.append(messages or [])
            yield self.answer
            return
        yield "no"


def _turn(agent, text):
    provider = _Provider()
    agent.api_backend.chat_completion = provider
    agent.chat_step(user_input=text, raw_user_input=text, stream_callback=lambda t: None)
    return provider


def _assert_the_owner_answered(req, provider, stores):
    after = treq.get_request(OWNER, req["id"])
    assert after["status"] == "replied", after
    assert after["user_reply"] == "ja", after
    assert (after["main_reply"] or "").strip(), "the agent's own reply was not captured"
    main = json.dumps(provider.main[0], ensure_ascii=False)
    for needed in (QUESTION, PROPOSAL, DETAILS):
        assert needed in main, f"{needed!r} did not reach the turn"
    assert tm.get_waiting_for_reply(OWNER) is None
    assert not (stores / "thinking_requests" / "_default").exists()


# ── AC 1: one owner, every store ─────────────────────────────────────────────

@pytest.mark.parametrize("scope, expected", [
    (None, OWNER),
    ("", ""),
    ("   ", ""),
    ("default", OWNER),
    (OWNER, OWNER),
    (f" {OWNER} ", OWNER),
    (OTHER, OTHER),
])
def test_every_store_names_the_same_owner(stores, scope, expected):
    assert tm.scope_data_id(scope) == expected
    is_owner = tm.scope_storage_key(scope) == "default"
    assert is_owner == (expected == OWNER)
    folder = expected or "_default"
    assert treq._dir(scope) == stores / "thinking_requests" / folder
    assert hb._dir(scope) == stores / "handoff_bundles" / folder


def test_an_unreadable_owner_scope_falls_back_to_no_account(stores, monkeypatch):
    """Reading the configured scope raises: never another account's directory."""
    import vaf.core.config as config

    def _broken():
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(config, "get_local_admin_scope_id", _broken)
    assert tm.scope_data_id(None) == ""
    assert tm.scope_data_id("default") == ""
    assert treq._dir(None) == stores / "thinking_requests" / "_default"
    assert hb._dir(None) == stores / "handoff_bundles" / "_default"
    assert tm.scope_data_id(OTHER) == OTHER


def test_the_stores_keep_accounts_apart(stores):
    others = treq.add_request(OTHER, question="Bob's question", run_seq=1)
    owners = treq.add_request(None, question=QUESTION, run_seq=1)
    assert treq.get_request(None, others["id"]) is None
    assert treq.get_request(OWNER, others["id"]) is None
    assert treq.get_request(OTHER, others["id"])["question"] == "Bob's question"
    assert treq.get_request(OTHER, owners["id"]) is None
    assert treq.get_request(OWNER, owners["id"])["question"] == QUESTION

    bundle = hb.create(OWNER, history=[], summary=DETAILS, question=QUESTION, source="automation")
    assert hb.load(None, bundle["id"]) is not None
    assert hb.load(OTHER, bundle["id"]) is None


# ── AC 2-4: the owner's turns with no scope ──────────────────────────────────

def test_an_answer_on_discord_is_the_owners_answer(make_agent, stores):
    req = _seed_question()
    agent = make_agent(_discord_owner())

    provider = _turn(agent, "ja")

    _assert_the_owner_answered(req, provider, stores)


def test_an_answer_on_discord_opens_the_automations_handoff(make_agent, stores):
    handoff = hb.deliver_handoff(
        None, message=QUESTION, proposed_action=PROPOSAL, details=DETAILS, username="alice",
        history=[{"role": "user", "content": "check the garage"},
                 {"role": "tool", "name": "web_search", "content": STEP}],
    )
    assert handoff is not None
    agent = make_agent(_discord_owner())

    provider = _turn(agent, "ja")

    assert hb.load(OWNER, handoff["bundle_id"])["status"] == "resolved"
    assert treq.get_request(OWNER, handoff["id"])["status"] == "replied"
    sent = "\n".join(provider.texts)
    assert STEP in sent and DETAILS in sent, "the automation's findings never reached the turn"


def test_a_bare_embedded_agent_answers_as_the_owner(stores, monkeypatch):
    # The facade setdefaults this; deleting it first makes monkeypatch restore the absence.
    monkeypatch.delenv("VAF_NONINTERACTIVE", raising=False)
    req = _seed_question()
    facade = vaf.Agent(config={"provider": "deepseek", "api_key_deepseek": "sk-test"})
    core = facade.core
    assert getattr(core, "_current_user_scope_id", None) is None
    provider = _Provider()
    core.api_backend.chat_completion = provider

    facade.run("ja")

    after = treq.get_request(OWNER, req["id"])
    assert after["status"] == "replied" and after["user_reply"] == "ja", after
    assert PROPOSAL in json.dumps(provider.main[0], ensure_ascii=False)


# ── AC 5-6: nobody else gains anything, the owner's own scope still works ────

@pytest.mark.parametrize("who", ["another_account", "contact"])
def test_another_account_and_a_contact_gain_nothing(make_agent, who):
    req = _seed_question()
    before = dict(tm.get_waiting_for_reply(OWNER))
    if who == "another_account":
        agent = make_agent(Identity(scope=OTHER, username="bob", role="user"), source="web")
    else:
        agent = make_agent(_discord_owner(), front_office=True)

    provider = _turn(agent, "ja")

    assert provider.main, "the turn never reached the model"
    after = treq.get_request(OWNER, req["id"])
    assert after["status"] == "asked" and after["user_reply"] is None, after
    assert tm.get_waiting_for_reply(OWNER) == before
    for text in provider.texts:
        assert QUESTION not in text and PROPOSAL not in text and DETAILS not in text


def test_the_owners_own_scope_still_answers(make_agent, stores):
    req = _seed_question()
    agent = make_agent(Identity(scope=OWNER, username="alice", role="admin"), source="web")

    provider = _turn(agent, "ja")

    _assert_the_owner_answered(req, provider, stores)


# ── AC 7: the dashboard ──────────────────────────────────────────────────────

def test_the_dashboard_lists_each_owner_request_once(stores, monkeypatch):
    owners = treq.add_request(OWNER, question=QUESTION, run_seq=1)
    others = treq.add_request(OTHER, question="Soll ich Tests automatisieren?", run_seq=1)
    # An empty legacy no-scope directory (older installs) and an alias directory of the owner.
    (stores / "thinking_requests" / "_default").mkdir(parents=True)
    (stores / "thinking_requests" / "default").mkdir(parents=True)

    from vaf.api.thinking_routes import _requests_by_key, thinking_status
    by_key = _requests_by_key(OWNER)

    assert "_default" not in by_key
    assert [r["id"] for r in by_key["default"]] == [owners["id"]]
    assert [r["id"] for r in by_key[OTHER]] == [others["id"]]

    async def fake_names():
        return {OTHER: "bob"}
    import vaf.api.security_routes as sr
    monkeypatch.setattr(sr, "_scope_username_map", fake_names)
    out = asyncio.run(thinking_status(_={"role": "admin"}))
    assert all(u["scope"] != "_default" for u in out["users"]), out["users"]
    owner_rows = [u for u in out["users"] if u["scope"] == ""]
    assert len(owner_rows) == 1
    assert [r["id"] for r in owner_rows[0]["requests"]] == [owners["id"]]
