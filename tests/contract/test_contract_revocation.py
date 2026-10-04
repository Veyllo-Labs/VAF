# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: taking access away and standing grants (docs/EMBEDDING.md, "Taking access
away: `vaf.revoke_account`" and "Headless safety: tool confirmation").

What an embedder's admin panel is built on: a revoked account runs nothing through
`ToolCaller`, whatever role its caller carries; restoring lifts that; stopping an account's
work does NOT revoke it; listeners hear the scope; `stop_session` answers with its documented
keys and never raises. And the standing grants: given per account, listed back, taken back.
Error strings are pinned by prefix only.
"""
from pathlib import Path

import pytest

import vaf

SCOPE = "deadbeef-0000-0000-0000-0000000000a1"
OTHER = "deadbeef-0000-0000-0000-0000000000b2"


class _ReadNote:
    name = "read_note"
    description = "returns a fixed note"
    parameters = {"type": "object", "properties": {}}
    identity_kwargs = ()

    def run(self, **kwargs):
        return "NOTE: ok"


@pytest.fixture(autouse=True)
def _restore():
    yield
    vaf.restore_account(SCOPE)
    vaf.restore_account(OTHER)


def _caller(scope, role):
    return vaf.ToolCaller({"read_note": _ReadNote()}, user_scope_id=scope, user_role=role)


def test_a_revoked_account_runs_nothing_whatever_role_its_caller_carries():
    vaf.revoke_account(SCOPE)
    assert _caller(SCOPE, "admin").execute("read_note", {}).startswith("Security Error:")
    assert _caller(SCOPE, "user").execute("read_note", {}).startswith("Security Error:")
    assert _caller(OTHER, "user").execute("read_note", {}) == "NOTE: ok"


def test_restoring_lifts_the_mark():
    vaf.revoke_account(SCOPE)
    vaf.restore_account(SCOPE)
    assert _caller(SCOPE, "user").execute("read_note", {}) == "NOTE: ok"


def test_stopping_an_accounts_work_does_not_revoke_it():
    vaf.stop_account_work(SCOPE)
    assert _caller(SCOPE, "user").execute("read_note", {}) == "NOTE: ok"


def test_a_listener_hears_the_scope_until_it_is_removed():
    heard = []
    vaf.add_revocation_listener(heard.append)
    try:
        vaf.stop_account_work(SCOPE)
        assert heard == [SCOPE]
    finally:
        vaf.remove_revocation_listener(heard.append)
    vaf.stop_account_work(SCOPE)
    assert heard == [SCOPE]


def test_stop_session_answers_with_its_documented_keys():
    result = vaf.stop_session("contract-chat-without-work")
    assert {"dropped", "killed", "subagents_kept", "gate_cancelled"} <= set(result)
    assert vaf.stop_session("") == {"dropped": 0, "killed": 0, "subagents_kept": False,
                                    "gate_cancelled": False}


def test_standing_grants_are_per_account_and_can_be_taken_back(tmp_path):
    folder = Path(tmp_path) / "project"
    folder.mkdir()
    vaf.set_tool_policy("read_note", "allow", SCOPE)
    vaf.mark_trusted_dir(folder, SCOPE)

    grants = vaf.list_standing_grants(SCOPE)
    assert grants["tools"]["read_note"]["always"] is True
    assert len(grants["dirs"]) == 1
    assert "read_note" not in vaf.list_standing_grants(OTHER)["tools"]

    removed = vaf.revoke_standing_grants(SCOPE, everything=True)
    assert removed["tools"] == ["read_note"] and len(removed["dirs"]) == 1
    assert vaf.list_standing_grants(SCOPE) == {"tools": {}, "dirs": []}
