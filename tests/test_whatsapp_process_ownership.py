# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A request acts only through its own account's WhatsApp process.

THE DEFECT, measured on the code before this change: twelve places in the bridge fell back to
"the only running process" when the requested account had none, whoever that process belonged
to. With only the admin linked, an account without a WhatsApp of its own sent from the
admin's agent number (`POST /api/whatsapp/send`, the agent's `send_whatsapp`), listed the
admin's chats, loaded their history and asked the admin's phone for avatars and names.

THE RULE: one helper, `whatsapp_bridge._process_for`, answers the account's own process, or
the only process when it was started for the SAME scope (the same account under another
spelling of its name), and nothing else. Fake processes only; no Node, no network.
"""
import io
import json
import queue
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.api import whatsapp_bridge as wa
from vaf.core.config import Config
from vaf.core.platform import Platform

ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000aa"
ALICE_SCOPE = "ab12cd34-0000-4000-8000-000000000001"
# "Admin" is the admin account under another spelling: the account directory is case-blind.
SCOPES = {"admin": ADMIN_SCOPE, "Admin": ADMIN_SCOPE, "alice": ALICE_SCOPE}


@pytest.fixture
def admin_only(monkeypatch, tmp_path):
    """Only the admin's Node process runs; returns what it was asked."""
    state = {"local_admin_scope_id": ADMIN_SCOPE, "local_admin_username": "admin",
             "whatsapp_config": {"enabled": True}}
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Config, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    monkeypatch.setattr("vaf.core.config.scope_id_for_username", lambda name: SCOPES.get(str(name or "")))
    written = io.StringIO()
    monkeypatch.setattr(wa, "_processes", {"admin": SimpleNamespace(stdin=written, poll=lambda: None)})
    monkeypatch.setattr(wa, "_scope_by_user", {"admin": ADMIN_SCOPE})
    monkeypatch.setattr(wa, "is_bridge_running", lambda: True)
    monkeypatch.setattr(wa, "_chat_lists", {"admin": [{"jid": "491700000042@s.whatsapp.net", "name": "Admin's friend"}]})
    monkeypatch.setattr(wa, "_lid_mappings", {"admin": [{"lid": "1@lid", "e164": "+491700000042"}]})
    monkeypatch.setattr(wa, "_contact_names", {"admin": {"491700000042": {"name": "Admin's friend"}}})
    monkeypatch.setattr(wa, "_connection_status", {"admin": True})
    monkeypatch.setattr(wa, "_append_chat_activity", lambda *a, **k: None)
    monkeypatch.setattr(wa, "NAMES_PASS_INTERVAL", 0.0)
    return written


def _asked(written):
    return [json.loads(line) for line in written.getvalue().splitlines()]


def _store_one(username, scope):
    from vaf.core import channel_message_store as store
    store.append_message(username, "+491700000042", "old", direction="in", user_scope_id=scope, ts=10.0,
                         message_id="A")


# Every lane, called for an account; each returns what its caller would see.
def _lanes(name, scope):
    return {
        "has_process_for_user": lambda: wa.has_process_for_user(name),
        "get_whatsapp_chats": lambda: wa.get_whatsapp_chats(name, wait_timeout=0.05),
        "sync_whatsapp_chats": lambda: wa.sync_whatsapp_chats(name, wait_timeout=0.05),
        "fetch_older_messages": lambda: (_store_one(name, scope),
                                         wa.fetch_older_messages(name, "491700000042@s.whatsapp.net", "+491700000042",
                                                                 scope, wait_timeout=0.05))[1],
        "get_avatar": lambda: wa.get_avatar(name, "491700000042@s.whatsapp.net", wait_timeout=0.05),
        "resync_contacts": lambda: wa.resync_contacts(name, wait_timeout=0.05, user_scope_id=scope),
        "get_connection_status": lambda: wa.get_connection_status(name, wait_timeout=0.05),
        "get_lid_mappings": lambda: wa.get_lid_mappings(name, wait_timeout=0.05),
        "get_contact_names": lambda: wa.get_contact_names(name, wait_timeout=0.05),
        "names_pass": lambda: (_store_one(name, scope), wa._fetch_names_per_chat(name, scope))[1],
    }


LANES = sorted(_lanes("x", "y"))


@pytest.mark.parametrize("lane", LANES)
def test_another_account_never_reaches_the_admins_process(admin_only, lane):
    """MUTATION: any one call site back to the old fallback, or the helper's scope comparison
    removed: the lane writes to the admin's process."""
    out = _lanes("alice", ALICE_SCOPE)[lane]()
    assert _asked(admin_only) == [], f"{lane} wrote to another account's process"
    assert out in (False, None, [], {}, 0, (False, "WhatsApp bridge is not running.")) or (
        isinstance(out, dict) and out.get("ok") is False), (lane, out)
    assert "Admin's friend" not in json.dumps(out, default=str)


@pytest.mark.parametrize("lane", ["has_process_for_user", "get_avatar", "get_connection_status", "names_pass",
                                  "fetch_older_messages", "get_whatsapp_chats"])
def test_the_same_account_under_another_spelling_still_gets_it(admin_only, lane):
    """The case the fallback exists for; stays green when the scope comparison is removed."""
    _lanes("Admin", ADMIN_SCOPE)[lane]()
    if lane == "has_process_for_user":
        assert wa.has_process_for_user("Admin") is True
    else:
        assert _asked(admin_only), f"{lane} did not reach the account's own process"


def test_the_sender_loop_refuses_another_accounts_item(admin_only, monkeypatch):
    """A scoped item of another account finds no process of its own; a scopeless item naming
    another account is refused before any process is asked (`messenger_lane_identity`), and a
    scopeless item under an admin name is the local admin's."""
    results = {}
    monkeypatch.setattr(wa, "_complete_send_request",
                        lambda req_id, ok, err=None, **k: results.__setitem__(req_id, (ok, err)))
    q = queue.Queue()
    q.put(("alice", "491700000042@s.whatsapp.net", "from alice", None, "r1", None, None, None, ALICE_SCOPE))
    q.put(("Admin", "491700000042@s.whatsapp.net", "from the admin", None, "r2"))
    q.put(("alice", "491700000042@s.whatsapp.net", "from alice, no scope", None, "r3"))
    q.put(None)
    monkeypatch.setattr(wa, "_outgoing_queue", q)
    wa._sender_loop()
    assert [c["text"] for c in _asked(admin_only)] == ["from the admin"]
    assert results["r1"] == (False, wa.NO_PROCESS_FOR_ACCOUNT)
    assert results["r3"] == (False, wa.NO_ACCOUNT_FOR_REQUEST)


def test_a_send_for_an_account_without_a_process_never_rides_another(admin_only, monkeypatch):
    """`send_whatsapp_with_confirmation`, the route's and the tool's lane: in-process it does not
    take the admin's process, it hands the request to the sender loop, which refuses it. The
    same send without a scope is refused at the entry: no queue item, no request file."""
    taken = []
    monkeypatch.setattr(wa, "_outgoing_queue", queue.Queue())
    monkeypatch.setattr(wa, "_read_bridge_state", lambda: {"running": True, "usernames": ["admin"]})
    monkeypatch.setattr(wa, "_write_json_atomic", lambda path, data: taken.append(data))
    monkeypatch.setattr(wa, "_wait_for_external_send_result", lambda *a, **k: "WhatsApp could not deliver")
    out = wa.send_whatsapp_with_confirmation("alice", "491700000042@s.whatsapp.net", "hi", allow_contact_send=True,
                                             user_scope_id=ALICE_SCOPE)
    assert wa._outgoing_queue.empty(), "queued for the admin's process"
    assert [t["username"] for t in taken] == ["alice"] and "could not" in out
    assert [t["user_scope_id"] for t in taken] == [ALICE_SCOPE]
    taken.clear()
    out = wa.send_whatsapp_with_confirmation("alice", "491700000042@s.whatsapp.net", "hi", allow_contact_send=True)
    assert out == wa.NO_ACCOUNT_FOR_REQUEST
    assert wa._outgoing_queue.empty() and taken == []
    assert _asked(admin_only) == []


def test_the_fallback_lives_in_one_place():
    """The twelve hand copies are one helper: a thirteenth copy is the next cross-account lane."""
    src = Path(wa.__file__).read_text(encoding="utf-8")
    assert len(re.findall(r"len\(_processes\)\s*==\s*1", src)) == 1
    helper = src[src.index("def _process_for("):src.index("\ndef ", src.index("def _process_for(") + 1)]
    assert "len(_processes) == 1" in helper
    # The second read is the spawn gate's (`_registered_live`: which live process is registered
    # under exactly this name?), which acts on no process.
    gate = src[src.index("def _registered_live("):src.index("\ndef ", src.index("def _registered_live(") + 1)]
    assert helper.count("_processes.get(") == 1 and gate.count("_processes.get(") == 1
    assert src.count("_processes.get(") == 2, "a lane picks a process by name outside the helper"
