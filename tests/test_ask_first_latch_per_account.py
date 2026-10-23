# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The ask-first latch belongs to the account it was asked of.

One agent serves every account of a runner worker. As one field on the agent, another
account's message cleared the owner's open question, and one account's question held every
other account's drain turns. The latch is now kept per account (the bound scope, canonical
key `thinking_mode.scope_storage_key`).

MUTATIONS: key the latch on one constant (agent-wide again), on the raw scope without the
canonical key, or on the current chat; let the setter ignore None; read the backing dict
without a guard.
"""
from types import SimpleNamespace

import pytest

from vaf.core.platform import Platform

OWNER_SCOPE = "6f9619ff-8b86-d011-b42d-00c04fc964ff"
BOB_SCOPE = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"
WRITE_TOOL = SimpleNamespace(permission_level="write")


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path / "vaf"))
    from vaf.core.agent import Agent
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "deepseek", "api_key_deepseek": "sk-test"})
    a.current_session_id = "red654321"
    return a


def _as(agent, scope, session="red654321"):
    agent._current_user_scope_id = scope
    agent.current_session_id = session
    return agent


def _drain_gate(agent):
    agent._synthetic_drain_turn = True
    try:
        return agent._ask_first_gate_decision("write_file", WRITE_TOOL)
    finally:
        agent._synthetic_drain_turn = False


def test_another_accounts_message_leaves_the_owners_question_open(agent):
    _as(agent, OWNER_SCOPE)._pending_user_question = {"preview": "Soll ich die Dateien loeschen?"}
    _as(agent, BOB_SCOPE, "green123456")._pending_user_question = None   # Bob's reply
    assert _as(agent, OWNER_SCOPE)._pending_user_question == {"preview": "Soll ich die Dateien loeschen?"}
    assert _drain_gate(agent), "the owner's drain turn was no longer held"


def test_the_owners_question_does_not_hold_another_accounts_background_turns(agent):
    _as(agent, OWNER_SCOPE)._pending_user_question = {"preview": "Soll ich?"}
    _as(agent, BOB_SCOPE, "green123456")
    assert getattr(agent, "_pending_user_question", None) is None
    assert _drain_gate(agent) is None, "Bob's drain turn was held by the owner's question"
    _as(agent, OWNER_SCOPE)
    assert _drain_gate(agent), "control: the owner's own drain turn is held"


def test_one_accounts_other_chat_clears_its_question(agent):
    """Per account, not per chat (named in the property's docstring)."""
    _as(agent, BOB_SCOPE, "green123456")._pending_user_question = {"preview": "Soll ich?"}
    _as(agent, BOB_SCOPE, "yellow012345")._pending_user_question = None
    assert _as(agent, BOB_SCOPE, "green123456")._pending_user_question is None


def test_no_scope_and_the_local_admin_scope_are_one_account(agent, monkeypatch):
    import vaf.core.config as config
    monkeypatch.setattr(config, "get_local_admin_scope_id", lambda: OWNER_SCOPE)
    _as(agent, OWNER_SCOPE)._pending_user_question = {"preview": "Soll ich?"}
    assert _as(agent, None)._pending_user_question == {"preview": "Soll ich?"}
    _as(agent, None)._pending_user_question = None
    assert _as(agent, OWNER_SCOPE)._pending_user_question is None


def test_a_stub_agent_keeps_one_latch_per_account():
    from vaf.core.agent import Agent
    stub = Agent.__new__(Agent)
    assert getattr(stub, "_pending_user_question", None) is None
    stub._current_user_scope_id = OWNER_SCOPE
    stub._pending_user_question = {"preview": "a"}
    stub._current_user_scope_id = BOB_SCOPE
    stub._pending_user_question = {"preview": "b"}
    assert stub._pending_user_question == {"preview": "b"}
    stub._current_user_scope_id = OWNER_SCOPE
    assert stub._pending_user_question == {"preview": "a"}
