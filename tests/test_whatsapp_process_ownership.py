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
spelling of its name), and nothing else. Every lane names the account's scope; a request
without one gets no process. Measured while the read lanes still asked by name alone: a
deleted "bob"'s process, still registered under "bob", answered the new account "bob" on every
read lane, and with the account directory down an account "Straße" reached the local admin
"strasse"'s process. Fake processes only; no Node, no network.
"""
import ast
import io
import json
import queue
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.api import whatsapp_bridge as wa
from vaf.core import config as _config_module
from vaf.core.config import Config
from vaf.core.platform import Platform

_REAL_SCOPE_ID_FOR_USERNAME = _config_module.scope_id_for_username

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
        "has_process_for_user": lambda: wa.has_process_for_user(name, scope),
        "get_whatsapp_chats": lambda: wa.get_whatsapp_chats(name, wait_timeout=0.05, user_scope_id=scope),
        "sync_whatsapp_chats": lambda: wa.sync_whatsapp_chats(name, wait_timeout=0.05, user_scope_id=scope),
        "fetch_older_messages": lambda: (_store_one(name, scope),
                                         wa.fetch_older_messages(name, "491700000042@s.whatsapp.net", "+491700000042",
                                                                 scope, wait_timeout=0.05))[1],
        "get_avatar": lambda: wa.get_avatar(name, "491700000042@s.whatsapp.net", wait_timeout=0.05,
                                            user_scope_id=scope),
        "resync_contacts": lambda: wa.resync_contacts(name, wait_timeout=0.05, user_scope_id=scope),
        "get_connection_status": lambda: wa.get_connection_status(name, wait_timeout=0.05, user_scope_id=scope),
        "get_lid_mappings": lambda: wa.get_lid_mappings(name, wait_timeout=0.05, user_scope_id=scope),
        "get_contact_names": lambda: wa.get_contact_names(name, wait_timeout=0.05, user_scope_id=scope),
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


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("name", ["admin", "Admin"])
def test_the_account_still_reaches_its_own_process(admin_only, lane, name):
    """Its own name, and the same account under another spelling (the case the fallback exists
    for). MUTATION: any lane passes no scope (it then reaches nothing) - red."""
    out = _lanes(name, ADMIN_SCOPE)[lane]()
    if lane == "has_process_for_user":
        assert out is True
    else:
        assert _asked(admin_only), f"{lane} did not reach the account's own process"


@pytest.mark.parametrize("lane", LANES)
def test_a_reused_name_never_reaches_the_old_process(admin_only, monkeypatch, lane):
    """A deleted "bob"'s process is still registered under "bob" (its stop failed, or it was
    deleted before deletion stopped it); a NEW account "bob" asks with its own scope. The old
    lanes asked by name alone and reached it on every lane. MUTATION: one lane back to asking
    without the scope, with `_process_for` settling a scopeless request by its name - red."""
    old_bob, new_bob = "ab12cd34-0000-4000-8000-0000000000b0", "ab12cd34-0000-4000-8000-0000000000b1"
    written = io.StringIO()
    monkeypatch.setattr(wa, "_processes", {"bob": SimpleNamespace(stdin=written, poll=lambda: None)})
    monkeypatch.setattr(wa, "_scope_by_user", {"bob": old_bob})
    monkeypatch.setattr("vaf.core.config.scope_id_for_username",
                        lambda name: new_bob if str(name or "").casefold() == "bob" else None)
    out = _lanes("bob", new_bob)[lane]()
    assert _asked(written) == [], f"{lane} wrote to the deleted account's process"
    assert out in (False, None, [], {}, 0, (False, "WhatsApp bridge is not running.")) or (
        isinstance(out, dict) and out.get("ok") is False), (lane, out)


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("spelling", ["Straße", "STRASSE"])
def test_a_twin_never_reaches_the_owner_while_the_directory_is_down(admin_only, monkeypatch, lane, spelling):
    """The local admin "strasse" runs alone; an account "Straße", created while names were
    compared with `lower()` only, asks while the account directory answers nothing. The name
    resolved to the admin by casefold and the one-process fallback took the admin's process.
    MUTATION: one lane back to asking without the scope, with `_process_for` settling a
    scopeless request by its name - red."""
    from vaf.core import tool_dispatch
    twin = "ab12cd34-0000-4000-8000-0000000000c5"
    state = {"local_admin_scope_id": ADMIN_SCOPE, "local_admin_username": "strasse",
             "whatsapp_config": {"enabled": True}}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    import vaf.core.config as config_module
    monkeypatch.setattr(config_module, "scope_id_for_username", _REAL_SCOPE_ID_FOR_USERNAME)
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver", lambda: [])    # down: nobody known
    written = io.StringIO()
    monkeypatch.setattr(wa, "_processes", {"strasse": SimpleNamespace(stdin=written, poll=lambda: None)})
    monkeypatch.setattr(wa, "_scope_by_user", {"strasse": ADMIN_SCOPE})
    assert config_module.scope_id_for_username(spelling) == ADMIN_SCOPE, "the world: the name resolves to the admin"
    _lanes(spelling, twin)[lane]()
    assert _asked(written) == [], f"{lane} wrote to the local admin's process"


def test_every_process_for_call_names_a_scope():
    """Every call of `_process_for` in vaf/ passes two arguments and neither is a literal None:
    a lane that forgets the scope is a lane that settles by name. MUTATION: one call back to
    `_process_for(username)` - red."""
    root = Path(wa.__file__).resolve().parents[1]
    calls = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if name == "_process_for":
                calls.append((path.name, node.lineno, node))
    assert len(calls) >= 12, f"the scan found only {len(calls)} calls; it is broken"
    bad = []
    for where, line, node in calls:
        values = list(node.args) + [k.value for k in node.keywords]
        if len(values) < 2 or any(isinstance(v, ast.Constant) and v.value is None for v in values):
            bad.append(f"{where}:{line}")
    assert not bad, "a _process_for call without a scope: " + ", ".join(bad)


def test_a_request_without_a_scope_gets_no_process(admin_only):
    """Even under the process's own name. MUTATION: drop the empty-scope answer - red."""
    assert wa._process_for("admin", None) == (None, None)
    assert wa._process_for("admin", "  ") == (None, None)
    assert wa.has_process_for_user("admin") is False
    assert wa._process_for("admin", ADMIN_SCOPE)[0] == "admin"


def test_the_docstring_no_longer_calls_a_scopeless_read_safe():
    """MUTATION: restore the old named boundary - red."""
    doc = wa._process_for.__doc__ or ""
    assert "a request without a scope is settled by its name alone" not in doc.lower()
    assert "without one gets no process" in doc or "without a scope gets no process" in doc
    assert "Straße" in doc and '"bob"' in doc, "the rule's measurement is gone"


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
    # The second read is the registration's (`_registration`: which process is registered under
    # exactly this name, and for which scope?), asked by the spawn gate (`_registered_live`) and
    # the reconnect worker; it acts on no process.
    gate = src[src.index("def _registration("):src.index("\ndef ", src.index("def _registration(") + 1)]
    assert helper.count("_processes.get(") == 1 and gate.count("_processes.get(") == 1
    assert src.count("_processes.get(") == 2, "a lane picks a process by name outside the helper"
