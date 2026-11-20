# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An account that ends leaves no WhatsApp behind, and a new account inherits none.

THE DEFECT, measured on the code before this change with fake processes: `revoke_account` of
an account left its WhatsApp process registered and alive (the bridge registered no revocation
listener); a deleted "carol" left `users/carol/whatsapp/creds.json`, and a NEW account "carol"
(new scope, own switch on) got that folder run for it by `_get_users_to_run`, `linked` true and
the deleted account's number as `linked_phone`.

THE RULE: a deactivation or deletion stops the account's process, its pending start and its
link flow (a demotion or a narrowed tool list stops none of them); a deletion also removes the
login, its picture cache and the name's cached data once no Node runs on the folder; a new
ordinary account finds any login left under its name moved aside before its row exists.

Fake processes, a scratch APP_DIR, the account directory set by the test, real SQLite for the
routes (`tests/auth_harness.py`). No Node, no keyring, no real store.
"""
import asyncio
import io
import json
import logging
import os
import subprocess
import sys

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth_harness import AsyncSessionShim, Harness
from vaf.api import whatsapp_bridge as wa
from vaf.api import whatsapp_routes as routes
from vaf.auth import user_admin
from vaf.auth.models import LocalUser
from vaf.auth.user_admin import UserAdminError
from vaf.core import revocation, tool_dispatch
from vaf.core.config import Config
from vaf.core.platform import Platform
from vaf.core.whatsapp_auth import get_linked_phone, whatsapp_auth_exists

ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000aa"
ALICE_SCOPE = "ab12cd34-0000-4000-8000-000000000001"
BOB_SCOPE = "ab12cd34-0000-4000-8000-000000000002"
PASSWORD = "correct-horse-battery-9"


class FakeProc:
    """A Node double: alive until terminated or killed, unless it survives both."""

    def __init__(self, survive=False):
        self.returncode = None
        self.stdin = io.StringIO()
        self.survive = survive
        self.terminated = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        if not self.survive:
            self.returncode = -15

    def kill(self):
        if not self.survive:
            self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("node", timeout)
        return self.returncode


@pytest.fixture
def world(monkeypatch, tmp_path):
    state = {"local_admin_scope_id": ADMIN_SCOPE, "local_admin_username": "admin",
             "whatsapp_config": {"enabled": True}}
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Config, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    for name in ("_processes", "_scope_by_user", "_pending_starts", "_self_phone", "_chat_lists",
                 "_chat_list_events", "_lid_mappings", "_lid_mappings_events", "_contact_names",
                 "_contact_names_events", "_connection_status", "_connection_events", "_contact_sync_last"):
        monkeypatch.setattr(wa, name, {})
    monkeypatch.setattr(wa, "_spawned_nodes", [])
    monkeypatch.setattr(wa, "TERMINATE_WAIT", 0.05)
    for name in ("_qr_state", "_qr_procs", "_qr_flows"):
        monkeypatch.setattr(routes, name, {})
    rows = [{"username": "alice", "user_scope_id": ALICE_SCOPE, "active": True},
            {"username": "bob", "user_scope_id": BOB_SCOPE, "active": True}]
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver", lambda: list(rows))
    # stop_account_work's other halves read the real queue and process registry.
    from vaf.core import processes, task_queue
    monkeypatch.setattr(task_queue.TaskQueue, "sessions_for_scope", lambda self, key: set())
    monkeypatch.setattr(processes, "list_for_scope", lambda key: [])
    world = type("World", (), {})()
    world.rows, world.state, world.tmp = rows, state, tmp_path
    yield world
    for scope in (ADMIN_SCOPE, ALICE_SCOPE, BOB_SCOPE):
        revocation.restore_account(scope)


def _register(name, scope, proc=None):
    proc = proc or FakeProc()
    wa._processes[name] = proc
    wa._scope_by_user[name] = scope
    return proc


def _login(name, phone="491700000099"):
    d = Config.APP_DIR / "users" / name / "whatsapp"
    d.mkdir(parents=True, exist_ok=True)
    (d / "creds.json").write_text(json.dumps({"me": {"id": f"{phone}:7@s.whatsapp.net"}}), encoding="utf-8")
    (d.parent / "whatsapp_avatars").mkdir(exist_ok=True)
    (d.parent / "whatsapp_avatars" / "4917_lid.jpg").write_bytes(b"JPEG")
    return d


def _live_node_on(folder):
    """A Node VAF started (`_spawned_nodes`) that runs on this login folder."""
    proc = FakeProc(survive=True)
    proc._vaf_login = wa._login_identity(folder)
    wa._spawned_nodes.append(proc)
    return proc


# -- AC3: the account's end stops its WhatsApp --------------------------------------------------

@pytest.mark.parametrize("how", ["deactivate", "delete"])
def test_a_revoked_account_loses_its_process(world, how):
    """The real `revocation.revoke_account`, as the admin routes call it after the commit.
    MUTATION: the bridge's listener not registered - red."""
    alice, admin = _register("alice", ALICE_SCOPE), _register("admin", ADMIN_SCOPE)
    if how == "deactivate":
        world.rows[0]["active"] = False
    else:
        del world.rows[0]
    revocation.revoke_account(ALICE_SCOPE)
    assert alice.returncode is not None and "alice" not in wa._processes
    assert admin.returncode is None and wa._processes["admin"] is admin


def test_a_narrowing_stops_nothing(world):
    """A narrowed tool list or a demotion stops the account's work without the mark, and the
    account still stands. MUTATION: the listener's condition without `is_revoked` /
    `account_stands` - red."""
    alice = _register("alice", ALICE_SCOPE)
    revocation.stop_account_work(ALICE_SCOPE)
    assert alice.returncode is None and wa._processes["alice"] is alice
    flow = object()
    routes._qr_flows[ALICE_SCOPE] = flow
    revocation.stop_account_work(ALICE_SCOPE)
    assert routes._qr_flows[ALICE_SCOPE] is flow


def test_a_pending_start_of_a_revoked_account_is_cancelled(world):
    pending = wa._PendingStart(ALICE_SCOPE)
    wa._pending_starts["alice"] = pending
    revocation.revoke_account(ALICE_SCOPE)
    assert pending.cancelled and pending.done.is_set() and pending.message == wa.START_CANCELLED


def test_no_other_accounts_process_is_touched(world):
    """The revoked account's pending start sits under "alice" while a process of ANOTHER scope
    is registered under the same name (a deleted account's name given to a new one). MUTATION:
    `stop_scope` stops by name without the scope - red."""
    pending = wa._PendingStart(ALICE_SCOPE)
    wa._pending_starts["alice"] = pending
    other = _register("alice", BOB_SCOPE)
    revocation.revoke_account(ALICE_SCOPE)
    assert pending.cancelled
    assert other.returncode is None and wa._processes["alice"] is other


def test_a_revoked_accounts_link_flow_ends(world):
    """MUTATION: the link listener not registered - red."""
    flow, node = object(), FakeProc()
    routes._qr_flows[ALICE_SCOPE] = flow
    routes._qr_procs[ALICE_SCOPE] = node
    routes._qr_state[ALICE_SCOPE] = {"qr_png": "x", "ts": 1}
    bob_flow = object()
    routes._qr_flows[BOB_SCOPE] = bob_flow
    revocation.revoke_account(ALICE_SCOPE)
    assert ALICE_SCOPE not in routes._qr_flows and ALICE_SCOPE not in routes._qr_state
    assert node.returncode is not None
    assert routes._qr_flows[BOB_SCOPE] is bob_flow


def test_the_listeners_are_registered_on_import():
    assert wa._on_account_revoked in revocation._listeners
    assert routes._on_link_account_revoked in revocation._listeners


# -- AC4: deletion removes the login ------------------------------------------------------------

@pytest.fixture
def accounts(world, monkeypatch):
    """The real admin routes over the harness store, the account directory read from it."""
    h = Harness(monkeypatch)
    admin_id, _ = h.add_user("dana", PASSWORD, role="admin", no_second_factor=True)
    bob_id, _ = h.add_user("bob", PASSWORD, no_second_factor=True)

    def directory():
        with Session(h.engine) as s:
            return [{"username": u.username, "user_scope_id": str(u.user_scope_id), "active": u.is_active,
                     "role": u.role} for u in s.execute(select(LocalUser)).scalars().all()]
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver", directory)
    h.bob_id, h.bob_scope = bob_id, str(h.user("bob").user_scope_id)
    h.admin = {"user_id": admin_id, "role": "admin", "user_scope_id": str(h.user("dana").user_scope_id)}
    return h


def _delete(h, user_id):
    from vaf.api import user_routes
    return asyncio.run(user_routes.delete_user(user_id, admin=h.admin))


def _fill_caches(name):
    wa._chat_lists[name] = [{"jid": "491700000042@s.whatsapp.net"}]
    wa._lid_mappings[name] = [{"lid": "1@lid", "e164": "+491700000042"}]
    wa._contact_names[name] = {"491700000042": {"name": "Bob's friend"}}
    wa._self_phone[name] = "+491700000099"
    wa._connection_status[name] = True
    wa._contact_sync_last[name] = 1.0


def test_deleting_removes_the_login_and_the_caches(accounts):
    """MUTATION: skip the cache drop - red; skip `forget_account_login` in the route - red."""
    folder = _login("bob")
    proc = _register("bob", accounts.bob_scope)
    _fill_caches("bob")
    assert _delete(accounts, accounts.bob_id) == {"message": "User deleted successfully"}
    assert proc.returncode is not None and "bob" not in wa._processes
    assert not folder.exists() and not (folder.parent / "whatsapp_avatars").exists()
    for cache in (wa._chat_lists, wa._lid_mappings, wa._contact_names, wa._self_phone,
                  wa._connection_status, wa._contact_sync_last):
        assert "bob" not in cache
    assert folder.parent.is_dir(), "the rest of the account folder is not this feature's"


def test_a_login_still_in_use_is_kept(accounts, caplog):
    """A Node that survived its kill still writes into the folder: nothing is removed, a
    warning says why, and the deletion itself stands. MUTATION: skip the `login_in_use`
    check - red."""
    folder = _login("bob")
    _live_node_on(folder)
    caplog.set_level(logging.WARNING, logger="vaf.api.whatsapp_bridge")
    assert _delete(accounts, accounts.bob_id) == {"message": "User deleted successfully"}
    assert (folder / "creds.json").is_file()
    assert any("still runs on it" in r.getMessage() for r in caplog.records)
    with Session(accounts.engine) as s:
        assert s.execute(select(LocalUser).where(LocalUser.username == "bob")).first() is None


@pytest.mark.skipif(sys.platform == "win32", reason="a folder link needs a privilege on Windows")
def test_a_link_in_place_of_the_folder_is_not_followed(accounts):
    """MUTATION: remove what the link points at (follow it) - red."""
    outside = Config.APP_DIR.parent / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not the account's", encoding="utf-8")
    (Config.APP_DIR / "users" / "bob").mkdir(parents=True)
    os.symlink(outside, Config.APP_DIR / "users" / "bob" / "whatsapp")
    _delete(accounts, accounts.bob_id)
    assert not os.path.lexists(Config.APP_DIR / "users" / "bob" / "whatsapp")
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "not the account's"


def test_a_folder_another_account_opens_is_kept(world, monkeypatch, caplog):
    """An ordinary account literally named "admin", created before the name was reserved,
    is deleted while the local admin (no row of its own, scope from the config) is called
    "admin" too: the folder is the admin's login as well, and stays."""
    folder = _login("admin")
    caplog.set_level(logging.WARNING, logger="vaf.api.whatsapp_bridge")
    assert wa.forget_account_login("admin", BOB_SCOPE) is False
    assert (folder / "creds.json").is_file()
    assert any("is the folder of the account" in r.getMessage() for r in caplog.records)


# -- AC5: a new account inherits no login -------------------------------------------------------

@pytest.fixture
def creating(world, monkeypatch):
    """`create_local_user` on the harness store; every linked folder whose name resolves to an
    account would run (the switch is on for everybody)."""
    h = Harness(monkeypatch)

    async def _cheap(p):
        return f"hash:{p}"
    monkeypatch.setattr(user_admin, "hash_password_async", _cheap)

    def directory():
        with Session(h.engine) as s:
            return [{"username": u.username, "user_scope_id": str(u.user_scope_id), "active": u.is_active}
                    for u in s.execute(select(LocalUser)).scalars().all()]
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver", directory)
    monkeypatch.setattr(wa, "channel_enabled_for_scope", lambda channel, scope: True)
    return h


def _create(h, name, **kw):
    async def go(db):
        return await user_admin.create_local_user(db, username=name, password=PASSWORD, **kw)
    return h.run_db(go)


@pytest.mark.parametrize("left_under", ["carol", "Carol"])
def test_a_new_account_does_not_inherit_a_leftover_login(creating, monkeypatch, caplog, left_under):
    """The folder a deleted "carol" (or "Carol", which the bridge resolves to the new "carol"
    by casefold) left behind. `_get_users_to_run` is asked from inside the insert's commit, so
    a move after the row exists is caught too. MUTATION: no set-aside - red; the set-aside
    after the insert - red."""
    old = _login(left_under, phone="491700000777")
    seen = []
    real_commit = AsyncSessionShim.commit

    async def commit(self):
        await real_commit(self)
        seen.append([entry[1] for entry in wa._get_users_to_run()])
    monkeypatch.setattr(AsyncSessionShim, "commit", commit)
    caplog.set_level(logging.WARNING, logger="vaf.auth.user_admin")

    _create(creating, "carol")

    assert seen and all(left_under not in names for names in seen), seen
    assert not whatsapp_auth_exists("carol") and get_linked_phone("carol") is None
    assert [e[1] for e in wa._get_users_to_run()] == []
    moved = sorted(old.parent.glob("whatsapp.previous-*"))
    assert len(moved) == 1 and (moved[0] / "creds.json").is_file()
    assert not (old.parent / "whatsapp_avatars").exists()
    assert any("was moved to whatsapp.previous-" in r.getMessage() for r in caplog.records)


def test_an_unmovable_leftover_refuses_the_account(creating, monkeypatch):
    """MUTATION: swallow the OSError - red."""
    _login("carol")
    import vaf.core.whatsapp_auth as auth

    def refuse(src, dst):
        raise PermissionError(13, "Permission denied", str(src))
    monkeypatch.setattr(auth.os, "rename", refuse)
    with pytest.raises(UserAdminError) as refused:
        _create(creating, "carol")
    assert refused.value.code == "login_leftover" and refused.value.http_status == 500
    with Session(creating.engine) as s:
        assert s.execute(select(LocalUser)).first() is None


def test_a_second_leftover_gets_its_own_name(creating, monkeypatch):
    import vaf.core.whatsapp_auth as auth
    monkeypatch.setattr(auth.time, "strftime", lambda fmt, t=None: "20261120T101500Z")
    first = _login("carol")
    (first.parent / "whatsapp.previous-20261120T101500Z").mkdir()
    _create(creating, "carol")
    assert (first.parent / "whatsapp.previous-20261120T101500Z-2" / "creds.json").is_file()


def test_the_first_admin_keeps_its_folder(creating):
    """The owner set up again after a lost database is the same person. MUTATION: set aside
    for the first admin too - red."""
    folder = _login("dora")
    creating_state = Config.get("local_admin_username")
    assert creating_state == "admin"
    _create(creating, "dora", role="admin", first_admin=True)
    assert (folder / "creds.json").is_file()
    assert not list(folder.parent.glob("whatsapp.previous-*"))
