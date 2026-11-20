# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An account touches only the automations it can see: one rule, `AutomationAccess`.

THE DEFECT. The WebSocket `update_automation` and `delete_automation` first tried the
caller's scoped store and, on a miss, retried on `AutomationManager()`, which loads the
root directory and every account's directory. Measured: an ordinary account rewrote the
prompt of the machine owner's automation (which runs as the machine owner) and deleted
another account's, knowing only the 8-character id; an unknown id answered differently,
so the lane was also an existence oracle. The agent tools let a second admin read and
rewrite any account's task by id, and five readers answered "which automations are this
caller's" by hand, with three different answers.

THE RULE. `vaf.core.automation.AutomationAccess(scope, role)`: an admin (role claim or the
machine owner's scope, or a nameless caller) sees root tasks, its own and the machine
owner's; anyone else sees its own scope only; a blank scope sees nothing. Every lane asks
it, and a foreign id answers exactly like a missing one.

Synthetic scopes and a scratch store only (`Platform.vaf_dir` points into tmp_path); the
WebSocket tests open a real `/ws` with a signed token.
"""
import ast
import asyncio
import json
import os
import random
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.core import automation as auto_mod
from vaf.core.automation import AutomationAccess, AutomationManager, AutomationTask
from vaf.core.platform import Platform

LOCAL = "ab12cd34-0000-4000-8000-0000000000a0"     # the machine owner's scope
ADMIN2 = "ab12cd34-0000-4000-8000-0000000000a2"    # a second admin account
TENANT_A = "ab12cd34-0000-4000-8000-0000000000b1"
TENANT_B = "ab12cd34-0000-4000-8000-0000000000b2"

ROOT_ID, LOCAL_ID, ADM2_ID = "r00t0001", "10ca0001", "ad200001"
A_ID, B_ID, B2_ID = "aaaa0001", "bbbb0001", "bbbb0002"
UNKNOWN_ID = "ffff0000"

SEED = (  # (id, scope, time); None = a root task in automations/
    (ROOT_ID, None, "07:00"),
    (LOCAL_ID, LOCAL, "08:00"),
    (ADM2_ID, ADMIN2, "09:00"),
    (A_ID, TENANT_A, "10:00"),
    (B_ID, TENANT_B, "11:00"),
    (B2_ID, TENANT_B, "12:00"),
)

_REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A scratch automation store seeded with one task per kind of owner; the machine
    owner's scope is pinned to LOCAL; nothing reaches the thinking workspace.

    A tenant task's next run reads its account's time zone, and the account name behind a
    scope comes from the account directory (`_resolve_username_for_scope`), which in a process
    that registered VAF's resolver asks the auth store. These tests are about who sees which
    task, not about that lookup, so it answers what it answers for an unknown account
    (`scope_<8 hex>`) without asking any directory; the lookup itself is pinned in
    tests/test_scope_name_directory_lookup.py."""
    import vaf.core.config as config_mod
    import vaf.core.thinking_mode as thinking_mode
    import vaf.core.thinking_workspace as tw

    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(config_mod, "get_local_admin_scope_id", lambda: LOCAL)
    monkeypatch.setattr(tw, "sync_automation_status_to_workspace", lambda **kw: None)
    monkeypatch.setattr(thinking_mode, "_resolve_username_for_scope",
                        lambda scope: "scope_" + str(scope).replace("-", "")[:8])
    root = tmp_path / "automations"
    root.mkdir()
    for task_id, scope, at in SEED:
        _write(root, AutomationTask(id=task_id, name=f"Task {task_id}", prompt=f"prompt {task_id}",
                                    frequency="daily", time=at, enabled=True, user_scope_id=scope))
    return root


def _write(root: Path, task: AutomationTask, *, trash: bool = False) -> Path:
    folder = root / task.user_scope_id if task.user_scope_id else root
    if trash:
        folder = root / "trash"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{task.id}.json"
    path.write_text(json.dumps(task.to_dict()), encoding="utf-8")
    return path


def _path(root: Path, task_id: str) -> Path:
    for tid, scope, _at in SEED:
        if tid == task_id:
            return (root / scope if scope else root) / f"{task_id}.json"
    raise KeyError(task_id)


def _snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*.json"))}


# ── a real /ws connection ─────────────────────────────────────────────────────

def _ws(monkeypatch, *, role, scope, frames, seen=None, probe=None):
    """Send `frames` ([(frame, reply_type), ...]) over one real `/ws` connection opened with a
    signed token; the replies in order. `role=None` signs no role claim (the machine owner's
    tokenless identity carries none). `seen`, a list, receives every message read. `probe`
    is called with the session while the frames are being answered.

    The account-standing check of the handshake answers what it answers with the auth store
    unreachable (the token stands) without asking it: it dialed the store four times per
    connection, about eight seconds on a Windows runner, and these tests are about who
    reaches which automation, not about account standing (tests/test_account_revocation.py)."""
    import jwt
    from starlette.testclient import TestClient

    import vaf.auth.crypto as crypto
    import vaf.core.web_server as web_server
    from vaf.core.web_server import app

    async def stands(*_a, **_kw):
        return True

    monkeypatch.setattr(crypto, "get_jwt_secret", lambda: "s" * 32)
    monkeypatch.setattr(web_server, "token_account_stands", stands)
    claims = {"sub": "7", "user_scope_id": scope, "username": "alice"}
    if role is not None:
        claims["role"] = role
    token = jwt.encode(claims, "s" * 32, algorithm="HS256")
    box = {"replies": []}
    with TestClient(app, client=("127.0.0.1", 40000)).websocket_connect(f"/ws?token={token}") as ws:

        def pump():
            try:
                for frame, want in frames:
                    ws.send_text(json.dumps(frame))
                    for _ in range(200):
                        msg = ws.receive_json()
                        if seen is not None:
                            seen.append(msg)
                        if msg.get("type") == want:
                            box["replies"].append(msg)
                            break
            except Exception as e:  # noqa: BLE001 - reported by the assert below
                box["error"] = e

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        if probe is not None:
            probe(ws)
        reader.join(30)
    assert len(box["replies"]) == len(frames), f"missing replies ({box.get('error')!r})"
    return box["replies"]


def _upd(task_id, **fields):
    return ({"type": "update_automation", "task_id": task_id, **fields}, "update_automation_result")


def _del(task_id):
    return ({"type": "delete_automation", "task_id": task_id}, "delete_automation_result")


_LIST = ({"type": "get_automations"}, "automations_list")
_NOT_FOUND = {"type": "update_automation_result", "ok": False, "error": "Automation not found"}
_NOT_DELETED = {"type": "delete_automation_result", "ok": False}


# ── AC1, AC2: a foreign id is a missing id ────────────────────────────────────

def test_tenant_cannot_update_or_delete_foreign_automation(store, monkeypatch):
    """MUTATIONS: restore the global fallback in the `update_automation` branch; separately,
    in the `delete_automation` branch."""
    before = _snapshot(store)
    foreign = (ROOT_ID, LOCAL_ID, ADM2_ID, B_ID)
    frames = [_upd(UNKNOWN_ID, prompt="PWNED by A")]
    frames += [_upd(tid, prompt="PWNED by A") for tid in foreign]
    frames += [_del(UNKNOWN_ID)] + [_del(tid) for tid in foreign]
    replies = _ws(monkeypatch, role="user", scope=TENANT_A, frames=frames)
    updates, deletes = replies[:5], replies[5:]
    assert updates[0] == _NOT_FOUND
    assert deletes[0] == _NOT_DELETED
    for tid, reply in zip(foreign, updates[1:]):
        assert reply == updates[0], f"update of {tid} answered {reply!r}"
    for tid, reply in zip(foreign, deletes[1:]):
        assert reply == deletes[0], f"delete of {tid} answered {reply!r}"
    assert _snapshot(store) == before, "a foreign task file changed"


# ── AC3: the own automation still works ───────────────────────────────────────

def test_tenant_updates_and_deletes_own(store, monkeypatch):
    """MUTATION: the non-admin branch of `AutomationAccess.sees` returns False."""
    replies = _ws(monkeypatch, role="user", scope=TENANT_A,
                  frames=[_upd(A_ID, prompt="new prompt A"), _del(A_ID)])
    assert replies[0]["ok"] is True, replies[0]
    assert replies[0]["automation"]["id"] == A_ID
    assert replies[1] == {"type": "delete_automation_result", "ok": True}
    assert not _path(store, A_ID).exists()


def test_tenant_update_is_stored(store, monkeypatch):
    """The change reaches the file, not just the reply."""
    replies = _ws(monkeypatch, role="user", scope=TENANT_A, frames=[_upd(A_ID, prompt="stored A")])
    assert replies[0]["ok"] is True, replies[0]
    assert json.loads(_path(store, A_ID).read_text(encoding="utf-8"))["prompt"] == "stored A"


# ── AC4: an admin acts on what its list shows ─────────────────────────────────

@pytest.mark.parametrize("role,scope,own_id", [("admin", ADMIN2, ADM2_ID), (None, LOCAL, LOCAL_ID)],
                         ids=["second-admin", "local-admin-without-role"])
def test_admin_reaches_root_local_and_own_not_tenant(store, monkeypatch, role, scope, own_id):
    """MUTATIONS: `sees` admin branch returns True for every scope; `is_admin` asks the role
    only (drop the scope half: the local admin without a role claim is then a tenant)."""
    tenant_file = _path(store, A_ID)
    tenant_before = tenant_file.read_bytes()
    frames = [_upd(tid, name=f"renamed {tid}") for tid in (ROOT_ID, LOCAL_ID, own_id, A_ID)]
    replies = _ws(monkeypatch, role=role, scope=scope, frames=frames)
    for tid, reply in zip((ROOT_ID, LOCAL_ID, own_id), replies[:3]):
        assert reply["ok"] is True, (tid, reply)
        assert json.loads(_path(store, tid).read_text(encoding="utf-8"))["name"] == f"renamed {tid}"
    assert replies[3] == _NOT_FOUND
    replies = _ws(monkeypatch, role=role, scope=scope,
                  frames=[_del(A_ID), _del(ROOT_ID), _del(own_id)])
    assert replies[0] == _NOT_DELETED
    assert tenant_file.read_bytes() == tenant_before
    assert replies[1]["ok"] is True and not _path(store, ROOT_ID).exists()
    assert replies[2]["ok"] is True and not _path(store, own_id).exists()


# ── AC5: the list, also as the first frame ────────────────────────────────────

@pytest.mark.parametrize("role,scope,expected", [
    ("admin", ADMIN2, {ROOT_ID, LOCAL_ID, ADM2_ID}),
    (None, LOCAL, {ROOT_ID, LOCAL_ID}),
    ("user", TENANT_A, {A_ID}),
    ("user", TENANT_B, {B_ID, B2_ID}),
], ids=["second-admin", "local-admin", "tenant-a", "tenant-b"])
def test_get_automations_first_frame_no_error(store, monkeypatch, role, scope, expected):
    """MUTATIONS: re-add a function-local `from vaf.core.config import is_admin_identity` in
    the `save_config` branch with the old hand filter in place; `list()` without the `sees`
    filter for an admin."""
    reply = _ws(monkeypatch, role=role, scope=scope, frames=[_LIST])[0]
    assert "error" not in reply, reply
    assert {a["id"] for a in reply["automations"]} == expected


# ── AC6: the agent tools ──────────────────────────────────────────────────────

_ADMINS = [("admin", ADMIN2), (None, LOCAL)]
_ADMIN_IDS = ["second-admin", "local-admin"]


@pytest.mark.parametrize("role,scope", _ADMINS, ids=_ADMIN_IDS)
def test_read_tool_refuses_a_foreign_task(store, role, scope):
    """MUTATION: remove the `sees` check from `ReadAutomationTool.run`."""
    from vaf.tools.automation import ReadAutomationTool

    out = ReadAutomationTool().run(task_id=A_ID, user_scope_id=scope, user_role=role)
    assert "not found" in out and f"prompt {A_ID}" not in out, out
    own = ReadAutomationTool().run(task_id=ROOT_ID, user_scope_id=scope, user_role=role)
    assert f"prompt {ROOT_ID}" in own


@pytest.mark.parametrize("role,scope", _ADMINS, ids=_ADMIN_IDS)
def test_update_tool_refuses_a_foreign_task(store, role, scope):
    """MUTATION: remove the `sees` check from `UpdateAutomationTool.run`."""
    from vaf.tools.automation import UpdateAutomationTool

    before = _path(store, A_ID).read_bytes()
    out = UpdateAutomationTool().run(task_id=A_ID, name="taken over", user_scope_id=scope,
                                     user_role=role)
    assert "not found" in out, out
    assert _path(store, A_ID).read_bytes() == before
    own = UpdateAutomationTool().run(task_id=ROOT_ID, name="renamed root", user_scope_id=scope,
                                     user_role=role)
    assert "Updated" in own, own


def test_delete_tool_refuses_a_foreign_task(store):
    """The local admin's tool store is the global one, so the id is found there and only the
    rule keeps it out. MUTATION: remove the `sees` check from `DeleteAutomationTool.run`."""
    from vaf.tools.automation import DeleteAutomationTool

    out = DeleteAutomationTool().run(task_id=A_ID, user_scope_id=LOCAL, user_role=None)
    missing = DeleteAutomationTool().run(task_id=UNKNOWN_ID, user_scope_id=LOCAL, user_role=None)
    assert out == missing.replace(UNKNOWN_ID, A_ID), out
    assert _path(store, A_ID).exists()
    assert not (store / "trash" / f"{A_ID}.json").exists()


def test_trash_tools_refuse_a_foreign_task(store):
    """A tenant task that an earlier global delete put into the root trash is neither listed
    nor restorable for the machine owner's tools; a root task there still is. MUTATIONS:
    remove the `sees` check from `RestoreAutomationTool.run`; separately, from
    `ListTrashTool.run`."""
    from vaf.tools.automation import ListTrashTool, RestoreAutomationTool

    foreign = _write(store, AutomationTask(id="bbbb0009", name="Trashed B", prompt="b",
                                           user_scope_id=TENANT_B), trash=True)
    _write(store, AutomationTask(id="r00t0009", name="Trashed root", prompt="r"), trash=True)
    listing = ListTrashTool().run(user_scope_id=LOCAL, user_role=None)
    assert "r00t0009" in listing and "bbbb0009" not in listing, listing
    out = RestoreAutomationTool().run(task_id="bbbb0009", user_scope_id=LOCAL, user_role=None)
    assert "not found in trash" in out, out
    assert foreign.exists() and not (store / TENANT_B / "bbbb0009.json").exists()
    back = RestoreAutomationTool().run(task_id="r00t0009", user_scope_id=LOCAL, user_role=None)
    assert "Restored" in back, back


def test_second_admin_deletes_lists_and_restores_a_root_task(store):
    """A second admin acts on the root task its list shows: delete moves it to the root
    trash, list_trash shows it, restore brings it back; its own earlier trash (in its own
    scope) stays listable. A tenant does none of that. MUTATIONS: choose the delete store
    without the role again; separately, list the trash of the scoped store only."""
    from vaf.tools.automation import DeleteAutomationTool, ListTrashTool, RestoreAutomationTool

    admin = {"user_scope_id": ADMIN2, "user_role": "admin"}
    tenant = {"user_scope_id": TENANT_A, "user_role": "user"}
    root_file = _path(store, ROOT_ID)
    missing = DeleteAutomationTool().run(task_id=UNKNOWN_ID, **tenant)
    assert DeleteAutomationTool().run(task_id=ROOT_ID, **tenant) == missing.replace(UNKNOWN_ID,
                                                                                    ROOT_ID)
    assert root_file.exists()

    out = DeleteAutomationTool().run(task_id=ROOT_ID, **admin)
    assert "Deleted (Moved to Trash)" in out, out
    assert not root_file.exists() and (store / "trash" / f"{ROOT_ID}.json").exists()
    _write(store, AutomationTask(id="ad200009", name="Old admin", prompt="o",
                                 user_scope_id=ADMIN2), trash=False)
    (store / ADMIN2 / "trash").mkdir(parents=True, exist_ok=True)
    (store / ADMIN2 / "ad200009.json").replace(store / ADMIN2 / "trash" / "ad200009.json")

    listing = ListTrashTool().run(**admin)
    assert ROOT_ID in listing and "ad200009" in listing, listing
    tenant_listing = ListTrashTool().run(**tenant)
    assert ROOT_ID not in tenant_listing and "ad200009" not in tenant_listing
    assert "not found in trash" in RestoreAutomationTool().run(task_id=ROOT_ID, **tenant)

    assert "Restored" in RestoreAutomationTool().run(task_id=ROOT_ID, **admin)
    assert root_file.exists()
    assert "Restored" in RestoreAutomationTool().run(task_id="ad200009", **admin)
    assert (store / ADMIN2 / "ad200009.json").exists()


def test_admin_restores_a_task_the_machine_owner_deleted(store):
    """The machine owner's own delete lands in its scoped trash. A second admin saw the task
    while it was live, so it lists and restores it from there too; a tenant does not.
    MUTATION: drop the machine owner's store from `AutomationAccess._stores`."""
    from vaf.tools.automation import DeleteAutomationTool, ListTrashTool, RestoreAutomationTool

    owner = {"user_scope_id": LOCAL, "user_role": None}
    admin = {"user_scope_id": ADMIN2, "user_role": "admin"}
    tenant = {"user_scope_id": TENANT_A, "user_role": "user"}
    assert "Deleted (Moved to Trash)" in DeleteAutomationTool().run(task_id=LOCAL_ID, **owner)
    assert (store / LOCAL / "trash" / f"{LOCAL_ID}.json").exists()
    assert LOCAL_ID not in ListTrashTool().run(**tenant)
    assert "not found in trash" in RestoreAutomationTool().run(task_id=LOCAL_ID, **tenant)
    assert LOCAL_ID in ListTrashTool().run(**admin)
    assert "Restored" in RestoreAutomationTool().run(task_id=LOCAL_ID, **admin)
    assert _path(store, LOCAL_ID).exists()


def test_tenant_trash_round_trip_unchanged(store):
    """A tenant's own trash, including a record written without a scope, still lists and
    restores. MUTATION: drop the scope stamp in `AutomationManager.list_trash`."""
    from vaf.tools.automation import DeleteAutomationTool, ListTrashTool, RestoreAutomationTool

    _write(store, AutomationTask(id="aaaa0009", name="Old A", prompt="a"), trash=False)
    legacy = store / "aaaa0009.json"
    (store / TENANT_A / "trash").mkdir(parents=True, exist_ok=True)
    legacy.replace(store / TENANT_A / "trash" / "aaaa0009.json")
    assert "Deleted" in DeleteAutomationTool().run(task_id=A_ID, user_scope_id=TENANT_A,
                                                   user_role="user")
    listing = ListTrashTool().run(user_scope_id=TENANT_A, user_role="user")
    assert A_ID in listing and "aaaa0009" in listing, listing
    out = RestoreAutomationTool().run(task_id="aaaa0009", user_scope_id=TENANT_A, user_role="user")
    assert "Restored" in out, out


@pytest.mark.parametrize("role,scope,expected", [
    ("admin", ADMIN2, {ROOT_ID, LOCAL_ID, ADM2_ID}),
    (None, LOCAL, {ROOT_ID, LOCAL_ID}),
    ("user", TENANT_A, {A_ID}),
], ids=["second-admin", "local-admin", "tenant"])
def test_list_tool_shows_what_the_rule_sees(store, role, scope, expected):
    """MUTATION: `list()` without the `sees` filter for an admin."""
    from vaf.tools.automation import ListAutomationsTool

    out = ListAutomationsTool().run(user_scope_id=scope, user_role=role)
    shown = {tid for tid, _s, _a in SEED if f"({tid})" in out}
    assert shown == expected, out


def test_create_tool_quotes_no_foreign_task(store, monkeypatch):
    """The duplicate check reads only what the caller sees: a tenant's task of the same name
    is neither quoted nor holds the machine owner's create back. Weekly, so nothing runs at
    once; the scheduler is not started. MUTATION: iterate `manager.list()` again."""
    from vaf.tools.automation import AutomationTool

    monkeypatch.setattr(auto_mod, "ensure_scheduler_started", lambda **kw: (None, False))
    out = AutomationTool().run(name=f"Task {B_ID}", prompt=f"prompt {B_ID}", frequency="weekly",
                               weekday="monday", time="11:30", user_scope_id=LOCAL, user_role=None)
    assert f"({B_ID})" not in out and f"'{B_ID}'" not in out, out
    assert "Created" in out, out


# ── AC7: the truth table ──────────────────────────────────────────────────────

class _T:
    def __init__(self, scope):
        self.user_scope_id = scope


@pytest.mark.parametrize("scope,role,admin", [
    (None, None, True), ("", None, True), ("", "user", True),
    ("   ", None, False), ("  ", "admin", False), ("\t\n", "ADMIN", False),
    (ADMIN2, "admin", True), (ADMIN2, " Admin ", True), (ADMIN2, "ADMIN", True),
    (LOCAL, None, True), (f" {LOCAL} ", None, True),
    (TENANT_A, None, False), (TENANT_A, "user", False), (TENANT_A, "administrator", False),
])
def test_access_truth_table(store, scope, role, admin):
    """MUTATION: `is_admin = not self.own` (a blank scope fails toward admin)."""
    access = AutomationAccess(scope, role)
    assert access.is_admin is admin


def test_tenant_sees_only_its_own_scope(store):
    access = AutomationAccess(TENANT_A, "user")
    assert access.sees(_T(TENANT_A)) and AutomationAccess(f" {TENANT_A} ", "user").sees(_T(TENANT_A))
    for other in (None, "", LOCAL, ADMIN2, TENANT_B, "  ", f" {TENANT_A} "):
        assert not access.sees(_T(other)), other


def test_admin_sees_root_own_and_local_only(store):
    access = AutomationAccess(ADMIN2, "admin")
    for seen in (None, "", ADMIN2, LOCAL):
        assert access.sees(_T(seen)), seen
    for other in (TENANT_A, TENANT_B):
        assert not access.sees(_T(other)), other


@pytest.mark.parametrize("raw", ["\xa0", " ", "\t", f" {LOCAL}", f"{LOCAL} ", f" {ADMIN2}\n",
                                 f" {TENANT_A}"])
def test_padded_or_blank_record_scope_is_seen_by_nobody(store, raw):
    """A stored scope this rule would read stripped while `_path_for_task` and the scheduler
    use it raw: a blank one would pass as root, a padded one as its owner, and a change would
    land in a directory named by the raw string. MUTATION: drop the raw-scope check in
    `AutomationAccess.sees`."""
    callers = [AutomationAccess(None), AutomationAccess(LOCAL), AutomationAccess(ADMIN2, "admin"),
               AutomationAccess(TENANT_A, "user")]
    for access in callers:
        assert not access.sees(_T(raw)), (access.own, raw)
    task = AutomationTask(id="0dd00001", name="odd", prompt="p", time="13:30", user_scope_id=raw)
    (store / "0dd00001.json").write_text(json.dumps(task.to_dict()), encoding="utf-8")
    for access in callers:
        assert "0dd00001" not in {t.id for t in access.list()}
        assert access.get("0dd00001") == (None, None)


def test_blank_scope_sees_nothing_and_builds_no_manager(store, monkeypatch):
    """`AutomationManager(user_scope_id="")` is the GLOBAL store, so a blank caller must not
    construct one. MUTATIONS: `is_admin = not self.own`; drop the early `return []`."""
    def boom(self, *a, **kw):
        raise AssertionError("a blank scope constructed a manager")

    monkeypatch.setattr(AutomationManager, "__init__", boom)
    access = AutomationAccess("   ", "admin")
    assert access.list() == []
    assert access.get(ROOT_ID) == (None, None)
    for scope in (None, "", "   ", LOCAL, TENANT_A):
        assert not access.sees(_T(scope)), scope


def test_get_answers_foreign_like_missing(store):
    """MUTATION: return the task without asking `sees` in `AutomationAccess.get`."""
    tenant = AutomationAccess(TENANT_A, "user")
    for tid in (ROOT_ID, LOCAL_ID, ADM2_ID, B_ID, UNKNOWN_ID):
        assert tenant.get(tid) == (None, None), tid
    mgr, task = tenant.get(A_ID)
    assert task.id == A_ID and mgr.user_scope_id == TENANT_A
    admin = AutomationAccess(ADMIN2, "admin")
    for tid in (A_ID, B_ID, UNKNOWN_ID):
        assert admin.get(tid) == (None, None), tid
    for tid in (ROOT_ID, LOCAL_ID, ADM2_ID):
        assert admin.get(tid)[1].id == tid


# ── fuzz: the classifier's three invariants ───────────────────────────────────

def _pool(rng):
    pool = [None, "", " ", "\t", "  \n", LOCAL, LOCAL.upper(), f" {LOCAL} ", f"{LOCAL}\n",
            ADMIN2, TENANT_A, TENANT_B, "ümlaut-scope", " ", " ", "ADMIN", "admin"]
    pool.append(str(uuid.UUID(int=rng.getrandbits(128))))
    pool.append("".join(rng.choice("ab \t-é0 ") for _ in range(rng.randint(0, 6))))
    return pool


def _fuzz(draws: int, seed: int = 440) -> list:
    rng = random.Random(seed)
    roles = [None, "", "admin", "Admin", " ADMIN ", "user", "guest", "administrator", "root"]
    misses = []
    for _ in range(draws):
        pool = _pool(rng)
        scope = rng.choice(pool)
        role = rng.choice(roles + ["".join(rng.choice("admin ") for _ in range(rng.randint(0, 7)))])
        tscope = rng.choice(pool)
        access = AutomationAccess(scope, role)
        own = str(scope or "").strip()
        ts = str(tscope or "").strip()
        seen = access.sees(_T(tscope))
        if seen and not access.is_admin and not (own and ts == own):
            misses.append(("tenant-foreign", scope, role, tscope))
        if seen and access.is_admin and ts not in ("", own, LOCAL):
            misses.append(("admin-foreign", scope, role, tscope))
        if scope is not None and scope != "" and not own and (seen or access.is_admin):
            misses.append(("blank-sees", scope, role, tscope))
        if seen and tscope is not None and str(tscope) != str(tscope).strip():
            misses.append(("padded-record-seen", scope, role, tscope))
    return misses


def test_access_fuzz_invariants(store):
    """Seeded: three invariants of the classifier. F440_FUZZ_DRAWS raises the count outside
    the suite. MUTATION: `is_admin = not self.own`."""
    draws = int(os.environ.get("F440_FUZZ_DRAWS", "2000"))
    assert _fuzz(draws) == []


# ── AC8, AC9: the WebSocket branches ask the rule ─────────────────────────────

_PLANNER = ("automation_note", "automation_todo")


def _branch_literals(test) -> list:
    """The `type` literals an `if` test dispatches on: `type == "x"`, `"x" == type` and
    `type in ("x", "y")`, also inside an `and`/`or` condition."""
    if isinstance(test, ast.BoolOp):
        return [lit for value in test.values for lit in _branch_literals(value)]
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and len(test.comparators) == 1):
        return []
    left, op, right = test.left, test.ops[0], test.comparators[0]

    def is_type(node):
        return isinstance(node, ast.Name) and node.id == "type"

    if isinstance(op, ast.Eq):
        for a, b in ((left, right), (right, left)):
            if is_type(a) and isinstance(b, ast.Constant) and isinstance(b.value, str):
                return [b.value]
    if isinstance(op, ast.In) and is_type(left) and isinstance(right, (ast.Tuple, ast.List, ast.Set)):
        return [e.value for e in right.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _own_path(nodes):
    """Every node of `nodes` that runs on the code's own path: nested functions, lambdas
    and classes are skipped, because a call inside one runs only if that one is called."""
    stack = [node for node in nodes if not isinstance(node, _NESTED_SCOPES)]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(child for child in ast.iter_child_nodes(node)
                     if not isinstance(child, _NESTED_SCOPES))


def _calls(nodes, name: str) -> bool:
    """Whether `name` is CALLED (as `name(...)` or `x.name(...)`) on the code's own path,
    not merely mentioned and not only inside a nested function or lambda."""
    for sub in _own_path(nodes):
        if isinstance(sub, ast.Call):
            f = sub.func
            if (isinstance(f, ast.Name) and f.id == name) or (
                    isinstance(f, ast.Attribute) and f.attr == name):
                return True
    return False


def _mentions(nodes, name: str) -> bool:
    for stmt in nodes:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Name) and sub.id == name:
                return True
            if isinstance(sub, ast.Attribute) and sub.attr == name:
                return True
            if isinstance(sub, ast.ImportFrom) and any(
                    name in (a.name, a.asname) for a in sub.names):
                return True
    return False


def _ws_branch_violations(source: str) -> tuple:
    """(violations, branch literals) for every branch of `websocket_endpoint` that
    dispatches on a `type` literal naming an automation, plus three file-wide checks: no
    `AutomationManager(...)` without `user_scope_id`, no `x.AutomationManager(...)` and no
    aliased import of it (either would hide a call from the first check).

    A store branch must CALL `AutomationAccess(...)` on its own path (a call inside a
    nested function or lambda does not count) and must not name `AutomationManager`.

    Named boundaries, not seen by this guard (the behaviour tests over a real `/ws` cover
    the commands that exist):
    - a helper that builds the global store elsewhere (`get_manager()`,
      `ensure_scheduler_started()`, a function in another module), or a rebind by plain
      assignment (`AM = AutomationManager` outside the branch, then `AM()`), or `getattr`;
    - dispatch not written as a comparison of the name `type`: `cmd.get("type") == ...`,
      `match`/`case`, `type.startswith(...)`;
    - a command whose literal does not contain "automation", such as the thinking-workspace
      handoff commands, which reach the store through `_apply_automation_action` (asks
      `AutomationAccess`; pinned by the handoff tests below)."""
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "websocket_endpoint")
    bad, seen = [], []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        literals = [lit for lit in _branch_literals(node.test) if "automation" in lit]
        if not literals:
            continue
        seen.extend(literals)
        label = "|".join(literals)
        store = [lit for lit in literals if not any(p in lit for p in _PLANNER)]
        if not store:
            if _mentions(node.body, "AutomationManager") or _mentions(node.body, "AutomationAccess"):
                bad.append(f"{label}: a planner command touches the task store")
        elif store == ["create_automation"]:
            if _mentions(node.body, "AutomationAccess"):
                bad.append(f"{label}: unexpected")
        elif not _calls(node.body, "AutomationAccess") or _mentions(node.body, "AutomationManager"):
            bad.append(f"{label}: does not go through AutomationAccess")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "AutomationManager" and a.asname not in (None, "AutomationManager"):
                    bad.append(f"line {node.lineno}: AutomationManager imported as {a.asname}")
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "AutomationManager":
            bad.append(f"line {node.lineno}: AutomationManager called through an attribute")
        elif (isinstance(f, ast.Name) and f.id == "AutomationManager"
                and not any(k.arg == "user_scope_id" for k in node.keywords)):
            bad.append(f"line {node.lineno}: AutomationManager() without user_scope_id")
    return bad, seen


def test_ws_automation_branches_ask_the_rule():
    """MUTATION: drop `AutomationAccess` from the `delete_automation` branch."""
    source = (_REPO / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    bad, seen = _ws_branch_violations(source)
    assert bad == []
    assert {"get_automations", "create_automation", "update_automation",
            "delete_automation"} <= set(seen), seen


def test_ws_guard_rejects_a_new_unruled_branch():
    """The guard itself: a new automation command reaching the global store fails it,
    however the store is spelled, and a branch that only names the rule does not pass."""
    synthetic = '''
from vaf.core.automation import AutomationAccess, AutomationManager as AM
import vaf.core.automation as A

async def websocket_endpoint(websocket, token=None):
    while True:
        if type == "get_automations":
            tasks = AutomationAccess(scope, role).list()
        elif type == "pause_automation":
            mgr = AutomationManager()
            mgr.get(cmd["id"])
        elif type == "rename_automation":
            _rule = AutomationAccess
            mgr = AM(user_scope_id=None)
        elif type in ("archive_automation", "unarchive_automation"):
            mgr = A.AutomationManager(user_scope_id=scope)
        elif "copy_automation" == type:
            mgr = A.AutomationManager(user_scope_id=scope)
        elif type == "move_automation" and cmd.get("id"):
            mgr = load_store(scope)
        elif type == "clone_automation":
            def never_called():
                return AutomationAccess(scope, role).list()
            mgr = load_store(scope)
        elif type == "get_automation_notes":
            notes = list_notes(scope)
'''
    bad, seen = _ws_branch_violations(synthetic)
    for literal in ("pause_automation", "rename_automation",
                    "archive_automation|unarchive_automation", "copy_automation",
                    "move_automation", "clone_automation"):
        assert any(b.startswith(f"{literal}:") for b in bad), (literal, bad)
    assert any("without user_scope_id" in b for b in bad), bad
    assert any("imported as AM" in b for b in bad), bad
    assert any("through an attribute" in b for b in bad), bad
    assert not any(b.startswith(("get_automations", "get_automation_notes")) for b in bad), bad


# ── AC10: the agent tools ask the rule ────────────────────────────────────────

def _tool_violations(source: str) -> tuple:
    """(violations, names) for every tool class in the module whose category is
    `automations`: the `run` Python would call, resolved in method resolution order over the
    module's own classes, must CALL `AutomationAccess(...)` on its own path. Subclasses of
    an existing tool count, a `category` may be annotated, and the base may be spelled
    `base.BaseTool`. `create_automation` is included: its duplicate check reads only what
    the caller sees. A `run` that also builds `AutomationManager(...)` itself is refused even
    next to the rule call: the store is chosen by `_manager_for_scope` or by the rule.

    Named boundaries, not seen by this guard: a base class from another module (its
    attributes and `run` are unknown here), a `run` that calls the rule and then reaches
    the global store through a helper, and a class whose category is computed rather than a
    literal."""
    tree = ast.parse(source)
    classes = {c.name: c for c in tree.body if isinstance(c, ast.ClassDef)}

    def base_name(base):
        if isinstance(base, ast.Name):
            return base.id
        if isinstance(base, ast.Attribute):
            return base.attr
        return None

    def mro(name, depth=0):
        """C3 linearization over the module's classes; any other base is a leaf."""
        cls = classes.get(name)
        if cls is None or depth > 50:
            return [name]
        bases = [b for b in (base_name(b) for b in cls.bases) if b]
        seqs = [mro(b, depth + 1) for b in bases] + [list(bases)]
        out = [name]
        while any(seqs):
            for seq in seqs:
                head = seq[0] if seq else None
                if head is not None and not any(head in other[1:] for other in seqs):
                    break
            else:
                raise ValueError(f"inconsistent bases for {name}")
            out.append(head)
            seqs = [seq[1:] if seq and seq[0] == head else seq for seq in seqs]
        return out

    def own(cls):
        attrs, run = {}, None
        for stmt in cls.body:
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
                for t in stmt.targets:
                    if isinstance(t, ast.Name):
                        attrs[t.id] = stmt.value.value
            elif (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                    and isinstance(stmt.value, ast.Constant)):
                attrs[stmt.target.id] = stmt.value.value
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == "run":
                run = stmt
        return attrs, run

    bad, names = [], []
    for name in classes:
        order = mro(name)
        if "BaseTool" not in order:
            continue
        attrs, run, run_found = {}, None, False
        for klass in reversed([k for k in order if k in classes]):
            k_attrs, _k_run = own(classes[klass])
            attrs.update(k_attrs)
        for klass in (k for k in order if k in classes):
            _a, k_run = own(classes[klass])
            if k_run is not None:
                run, run_found = k_run, True
                break
        if attrs.get("category") != "automations":
            continue
        names.append(attrs.get("name"))
        if (not run_found or not _calls(run.body, "AutomationAccess")
                or _calls(run.body, "AutomationManager")):
            bad.append(name)
    return bad, names


def test_automation_tools_ask_the_rule():
    """MUTATION: remove the `AutomationAccess` reference from `ListTrashTool.run`."""
    source = (_REPO / "vaf" / "tools" / "automation.py").read_text(encoding="utf-8")
    bad, names = _tool_violations(source)
    assert bad == []
    assert {"list_automations", "read_automation", "update_automation", "delete_automation",
            "restore_automation", "list_trash"} <= set(names), names


def test_tool_guard_rejects_a_new_unruled_tool():
    synthetic = '''
class ListTrashTool(BaseTool):
    name = "list_trash"
    category = "automations"
    def run(self, **kwargs):
        return [t for _m, t in AutomationAccess(kwargs.get("user_scope_id")).trash()]

class PauseAutomationTool(BaseTool):
    name = "pause_automation"
    category = "automations"
    def run(self, **kwargs):
        manager, _ = _manager_for_scope(kwargs.get("user_scope_id"))
        return manager.get(kwargs["task_id"])

class MentionOnlyTool(BaseTool):
    name = "mention_automation"
    category = "automations"
    def run(self, **kwargs):
        _rule = AutomationAccess
        return AutomationManager().list()

class RawTrashTool(ListTrashTool):
    name = "raw_trash"
    def run(self, **kwargs):
        return AutomationManager().list_trash()

class InheritedTrashTool(ListTrashTool):
    name = "inherited_trash"

class AnnotatedTool(base.BaseTool):
    name: str = "annotated_automation"
    category: str = "automations"
    def run(self, **kwargs):
        return _manager_for_scope(None)[0].list()

class CallbackOnlyTool(BaseTool):
    name = "callback_automation"
    category = "automations"
    def run(self, **kwargs):
        def never_called():
            return AutomationAccess(kwargs.get("user_scope_id")).list()
        check = lambda: AutomationAccess(None)
        return _manager_for_scope(None)[0].list()

class GuardedBase(BaseTool):
    category = "automations"
    def run(self, **kwargs):
        return AutomationAccess(kwargs.get("user_scope_id")).list()

class Left(GuardedBase):
    name = "left_automation"

class Right(GuardedBase):
    name = "right_automation"
    def run(self, **kwargs):
        return _manager_for_scope(None)[0].list()

class Diamond(Left, Right):
    name = "diamond_automation"

class RuleThenStoreTool(BaseTool):
    name = "rule_then_store_automation"
    category = "automations"
    def run(self, **kwargs):
        AutomationAccess(kwargs.get("user_scope_id")).list()
        return AutomationManager().list()
'''
    assert sorted(_tool_violations(synthetic)[0]) == [
        "AnnotatedTool", "CallbackOnlyTool", "Diamond", "MentionOnlyTool", "PauseAutomationTool",
        "RawTrashTool", "Right", "RuleThenStoreTool"]


# ── AC12: the calendar's daily check ──────────────────────────────────────────

def _ensure_daily_check(monkeypatch, user):
    import vaf.api.calendar_routes as cr

    monkeypatch.setattr(cr, "get_local_admin_scope_id", lambda: LOCAL)
    monkeypatch.setattr(cr, "_calendar_accounts", lambda username, scope: [{"id": "acc"}])
    monkeypatch.setattr(cr.CalendarStore, "exists", staticmethod(lambda scope: False))
    return asyncio.run(cr.ensure_daily_check_automation(_user=user))


def test_calendar_daily_check_ignores_tenant_task(store, monkeypatch):
    """MUTATION: revert the "already exists" lookup to `mgr.list()`."""
    import vaf.api.calendar_routes as cr

    tenant_task = AutomationTask(id="bbbb00dc", name=cr.CALENDAR_DAILY_CHECK_NAME, prompt="b",
                                 time="13:00", user_scope_id=TENANT_B)
    _write(store, tenant_task)
    owner = _ensure_daily_check(monkeypatch, {"user_scope_id": LOCAL, "username": "admin",
                                              "role": "admin"})
    assert owner["created"] is True and owner["task_id"] != "bbbb00dc", owner
    assert (store / LOCAL / f"{owner['task_id']}.json").exists()
    again = _ensure_daily_check(monkeypatch, {"user_scope_id": LOCAL, "username": "admin",
                                              "role": "admin"})
    assert again == {"ok": True, "created": False, "task_id": owner["task_id"]}
    second = _ensure_daily_check(monkeypatch, {"user_scope_id": ADMIN2, "username": "bob",
                                               "role": "admin"})
    assert second["created"] is True and second["task_id"] not in ("bbbb00dc", owner["task_id"])
    tenant = _ensure_daily_check(monkeypatch, {"user_scope_id": TENANT_B, "username": "carol",
                                               "role": "user"})
    assert tenant == {"ok": True, "created": False, "task_id": "bbbb00dc"}


# ── AC13: the next-run lookup ─────────────────────────────────────────────────

def test_next_run_scopeless_ignores_tenants(store):
    """A tenant task two minutes away is always the soonest; neither the machine owner's
    lookup nor the nameless one may answer with it. MUTATION: revert
    `get_next_automation_run_utc` to the global list."""
    soon = (datetime.now() + timedelta(minutes=2)).strftime("%H:%M")
    _write(store, AutomationTask(id="bbbb0003", name="soon", prompt="b", time=soon,
                                 user_scope_id=TENANT_B))
    visible = [t for t in AutomationManager().list(enabled_only=True)
               if t.user_scope_id in (None, LOCAL)]
    expected = min(t.next_run_datetime for t in visible)
    assert auto_mod.get_next_automation_run_utc(None) == expected
    assert auto_mod.get_next_automation_run_utc(LOCAL) == expected
    tenant = AutomationTask(id="x", time=soon).next_run_datetime
    assert auto_mod.get_next_automation_run_utc(TENANT_B) == tenant
    assert auto_mod.get_next_automation_run_utc(TENANT_A) == AutomationManager(
        user_scope_id=TENANT_A).get(A_ID).next_run_datetime


# ── the thinking review ───────────────────────────────────────────────────────

def test_thinking_review_counts_only_the_runs_own(store):
    """MUTATION: drop the `sees` filter in `_automation_review_state`."""
    from vaf.core import thinking_mode as tm

    assert tm._automation_review_state(LOCAL)[0] == 2        # root + the owner's scope
    assert tm._automation_review_state(TENANT_B)[0] == 2
    assert tm._automation_review_state(TENANT_A)[0] == 1


# ── the thinking-workspace handoff commands ───────────────────────────────────
# `approve_thinking_workspace_handoff` and `reject_thinking_workspace_handoff` take a task id
# and a handoff id from the client and used to join both into a path unchecked. A tenant
# pointed the handoff path at another account's automation file, rewrote it (status and
# timestamp added), copied it into its own archive, and read ok against "not found" as an
# existence oracle; a task-id walk approved another tenant's handoff. Both ids are single
# entries now (`safe_entry_name`), and a refused id answers exactly like a missing one.

@pytest.fixture
def workspaces(store, tmp_path, monkeypatch):
    """Thinking workspaces under tmp_path/data: tenant A owns one task, tenant B one task
    with a pending handoff. Notifications are recorded, not stored."""
    import vaf.core.thinking_workspace as tw
    import vaf.core.user_notifications as un

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: data))
    real_notify = un.append_notification
    monkeypatch.setattr(un, "append_notification", lambda *a, **kw: None)
    task_a = tw.create_task(TENANT_A, "A task", "test")["id"]
    task_b = tw.create_task(TENANT_B, "B task", "test")["id"]
    handoff_b = tw.create_handoff(TENANT_B, task_b, "B handoff", "content")["id"]
    return SimpleNamespace(root=data / "workspaces", task_a=task_a, task_b=task_b,
                           handoff_b=handoff_b, tw=tw, real_notify=real_notify)


def _walk_to(ws, task_id: str, target: Path) -> str:
    """A handoff id that, joined as `tasks/<task_id>/handoff/<id>.json` inside tenant A's
    workspace, lands on `target` (a `.json` file)."""
    handoff_dir = ws.root / TENANT_A / "tasks" / task_id / "handoff"
    return os.path.relpath(str(target.with_suffix("")), str(handoff_dir))


def _handoff(action, task_id, handoff_id):
    return ({"type": f"{action}_thinking_workspace_handoff", "task_id": task_id,
             "handoff_id": handoff_id}, "thinking_workspace_handoff_result")


def _without_ids(reply: dict) -> dict:
    return {k: v for k, v in reply.items() if k not in ("task_id", "handoff_id")}


def _all_files(*roots) -> dict:
    out = {}
    for root in roots:
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.name.endswith(".log"):
                out[p.as_posix()] = p.read_bytes()
    return out


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_handoff_id_walk_reaches_no_automation_file(store, workspaces, monkeypatch, action):
    """MUTATION: drop the entry check on the handoff id (`_handoff_paths`)."""
    ws = workspaces
    a_ws = ws.root / TENANT_A
    before = _all_files(store, ws.root / TENANT_B)
    walks = [_walk_to(ws, ws.task_a, _path(store, tid)) for tid in (ROOT_ID, B_ID, LOCAL_ID)]
    frames = [_handoff(action, ws.task_a, "ffff0000")]
    frames += [_handoff(action, ws.task_a, walk) for walk in walks]
    replies = _ws(monkeypatch, role="user", scope=TENANT_A, frames=frames)
    missing = _without_ids(replies[0])
    assert missing["ok"] is False and missing["error"] == "handoff not found", missing
    for walk, reply in zip(walks, replies[1:]):
        assert _without_ids(reply) == missing, (walk, reply)
    assert _all_files(store, ws.root / TENANT_B) == before, "a foreign file changed"
    assert not list((a_ws / "archive").rglob("*.json")), "a foreign file was copied in"


def test_task_id_walk_approves_no_foreign_handoff(store, workspaces, monkeypatch):
    """MUTATION: drop the entry check on the task id (`_handoff_paths`)."""
    ws = workspaces
    walk = f"../../{TENANT_B}/tasks/{ws.task_b}"
    b_handoff = ws.root / TENANT_B / "tasks" / ws.task_b / "handoff" / f"{ws.handoff_b}.json"
    before = b_handoff.read_bytes()
    replies = _ws(monkeypatch, role="user", scope=TENANT_A, frames=[
        _handoff("approve", "ffff0000", ws.handoff_b),
        _handoff("approve", walk, ws.handoff_b),
        _handoff("reject", walk, ws.handoff_b),
    ])
    assert _without_ids(replies[1]) == _without_ids(replies[0]), replies[1]
    assert replies[2]["ok"] is False and replies[2]["error"] == "handoff not found"
    assert b_handoff.read_bytes() == before
    assert json.loads(before)["status"] == "pending"


def test_own_handoff_still_approves_and_applies_only_own(store, workspaces, monkeypatch):
    """An own handoff is approved, and its automation action reaches only what the account
    sees: an update aimed at another account's id is "not found". MUTATION: let
    `_apply_automation_action` update through the global `AutomationManager()`."""
    ws = workspaces
    own = ws.tw.create_handoff(TENANT_A, ws.task_a, "A handoff", "c",
                               automation_action={"operation": "update", "task_id": A_ID,
                                                  "name": "approved name"})["id"]
    foreign = ws.tw.create_handoff(TENANT_A, ws.task_a, "A handoff 2", "c",
                                   automation_action={"operation": "update", "task_id": B_ID,
                                                      "name": "taken over"})["id"]
    b_before = _path(store, B_ID).read_bytes()
    replies = _ws(monkeypatch, role="user", scope=TENANT_A, frames=[
        _handoff("approve", ws.task_a, own), _handoff("approve", ws.task_a, foreign)])
    assert replies[0]["ok"] is True and replies[0]["automation_action_result"]["ok"] is True
    assert json.loads(_path(store, A_ID).read_text(encoding="utf-8"))["name"] == "approved name"
    assert replies[1]["ok"] is True
    assert replies[1]["automation_action_result"] == {"ok": False, "error": "automation not found"}
    assert _path(store, B_ID).read_bytes() == b_before


def test_handoff_action_without_a_scope_is_refused(store, workspaces, monkeypatch):
    """No scope must not mean the global store. MUTATION: restore the
    `... if user_scope_id else AutomationManager()` fallback."""
    def boom(self, *a, **kw):
        raise AssertionError("a scopeless handoff action built a manager")

    monkeypatch.setattr(AutomationManager, "__init__", boom)
    for scope in (None, "", "   "):
        for action in ({"operation": "create", "prompt": "p"},
                       {"operation": "update", "task_id": ROOT_ID, "name": "x"}):
            result = workspaces.tw._apply_automation_action(scope, action)
            assert result["ok"] is False, (scope, action, result)


def test_workspace_file_paths_stay_in_the_workspace(store, workspaces):
    """`../workspaceX/...` starts with the workspace's own path as a string, which is all a
    prefix comparison asked. MUTATION: put the prefix comparison back in place of
    `contained_path`."""
    ws = workspaces
    task_root = ws.root / TENANT_A / "tasks" / ws.task_a
    for rel in ("../workspaceX/evil.txt", "../workspace2/x", "../meta/task.json", "../../x"):
        with pytest.raises(ValueError):
            ws.tw.write_workspace_file(TENANT_A, ws.task_a, rel, "x")
        with pytest.raises((ValueError, FileNotFoundError)):
            ws.tw.read_workspace_file(TENANT_A, ws.task_a, rel)
    assert not (task_root / "workspaceX").exists() and not (task_root / "workspace2").exists()
    ws.tw.write_workspace_file(TENANT_A, ws.task_a, "notes/ok.md", "fine")
    assert ws.tw.read_workspace_file(TENANT_A, ws.task_a, "notes/ok.md") == "fine"


def test_workspace_read_refuses_an_existing_sibling_file(store, workspaces):
    """The read half, with a file that really exists beside the workspace: without the
    containment the read would return it, so a refusal here is the check and not a missing
    file. MUTATION: put the prefix comparison back in `read_workspace_file`."""
    ws = workspaces
    sibling = ws.root / TENANT_A / "tasks" / ws.task_a / "workspaceX"
    sibling.mkdir(parents=True)
    (sibling / "evil.txt").write_text("outside", encoding="utf-8")
    with pytest.raises(ValueError):
        ws.tw.read_workspace_file(TENANT_A, ws.task_a, "../workspaceX/evil.txt")
    ws.tw.write_workspace_file(TENANT_A, ws.task_a, "inside.md", "in")
    assert ws.tw.read_workspace_file(TENANT_A, ws.task_a, "inside.md") == "in"


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_overlong_ids_answer_like_missing_and_leak_no_path(store, workspaces, monkeypatch,
                                                           action):
    """An id longer than any file name used to reach the filesystem, whose error came back
    verbatim with the server's absolute data path. MUTATIONS: drop the length limit in
    `_entry`; separately, answer an `OSError` in the WebSocket branch with `str(e)` again."""
    ws = workspaces
    long_id = "x" * 300
    replies = _ws(monkeypatch, role="user", scope=TENANT_A, frames=[
        _handoff(action, ws.task_a, "ffff0000"),
        _handoff(action, ws.task_a, long_id),
        _handoff(action, long_id, "ffff0000"),
    ])
    missing = _without_ids(replies[0])
    for reply in replies[1:]:
        assert _without_ids(reply) == missing, reply
        text = json.dumps(reply)
        assert str(ws.root) not in text and TENANT_A not in text and "Errno" not in text, text
    # Refused before any filesystem call, not only caught afterwards.
    from vaf.core.path_jail import PathEscape
    assert ws.tw._handoff_paths(TENANT_A, ws.task_a, long_id) is None
    assert ws.tw._handoff_paths(TENANT_A, long_id, "ffff0000") is None
    with pytest.raises(PathEscape):
        ws.tw._task_root(TENANT_A, "y" * 129)
    assert ws.tw._handoff_paths(TENANT_A, ws.task_a, "z" * 100) is not None


def test_handoff_oserror_answers_like_missing(store, workspaces, monkeypatch):
    """Any filesystem error on these two commands answers "handoff not found", never the
    error text (it names the data path). MUTATION: answer an `OSError` with `str(e)`."""
    ws = workspaces

    def boom(*a, **kw):
        raise OSError(36, "File name too long", str(ws.root / TENANT_A / "secret"))

    monkeypatch.setattr(ws.tw, "approve_handoff", boom)
    monkeypatch.setattr(ws.tw, "reject_handoff", boom)
    replies = _ws(monkeypatch, role="user", scope=TENANT_A, frames=[
        _handoff("approve", ws.task_a, "ffff0000"), _handoff("reject", ws.task_a, "ffff0000")])
    for reply, action in zip(replies, ("approve", "reject")):
        assert reply["ok"] is False and reply["error"] == "handoff not found", reply
        assert reply["action"] == action and str(ws.root) not in json.dumps(reply), reply


def test_workspace_task_id_is_one_entry(store, workspaces):
    """A task id that walks is a missing task on every lane that takes one. MUTATION:
    drop `safe_entry_name` in `_task_root`."""
    ws = workspaces
    walk = f"../../{TENANT_B}/tasks/{ws.task_b}"
    assert ws.tw.get_task(TENANT_A, walk) is None
    assert ws.tw.get_handoff(TENANT_A, walk, ws.handoff_b) is None
    with pytest.raises(FileNotFoundError):
        ws.tw.write_workspace_file(TENANT_A, walk, "x.md", "x")
    with pytest.raises(FileNotFoundError):
        ws.tw.read_workspace_file(TENANT_A, walk, "x.md")
    with pytest.raises(FileNotFoundError):
        ws.tw.create_handoff(TENANT_A, walk, "t", "c")
    assert not (ws.root / TENANT_B / "tasks" / ws.task_b / "workspace" / "x.md").exists()


# ── a decision is made once, and what follows it never unmakes the answer ────

def _approve_twice_frames(task_id, handoff_id):
    return [_handoff("approve", task_id, handoff_id), _handoff("approve", task_id, handoff_id),
            _handoff("reject", task_id, handoff_id)]


def test_a_decided_handoff_is_not_applied_again(store, workspaces, monkeypatch):
    """A second approve used to apply the automation action again: a retry created a second
    automation. MUTATION: drop the `pending` check in `_decide`."""
    ws = workspaces
    hid = ws.tw.create_handoff(TENANT_A, ws.task_a, "Create one", "c", automation_action={
        "operation": "create", "prompt": "only once", "time": "13:30"})["id"]
    a_dir = store / TENANT_A
    before = {p.name for p in a_dir.glob("*.json")}
    replies = _ws(monkeypatch, role="user", scope=TENANT_A,
                  frames=_approve_twice_frames(ws.task_a, hid))
    assert replies[0]["ok"] is True and replies[0]["automation_action_result"]["ok"] is True
    created = {p.name for p in a_dir.glob("*.json")} - before
    assert len(created) == 1, created
    for reply in replies[1:]:
        assert reply["ok"] is False and reply["error"] == "handoff already decided", reply
    handoff = ws.tw.get_handoff(TENANT_A, ws.task_a, hid)
    assert handoff["status"] == "approved"


def test_a_failure_after_the_decision_still_answers_applied(store, workspaces, monkeypatch):
    """The bookkeeping after a saved decision (archive copy, task status, event) and the
    notification are best effort: a failure there is logged, the decision is reported as
    applied, and no path reaches the reply. MUTATIONS: drop the `try` in `_after_decision`;
    separately, the `try` around `append_notification` in the approve branch."""
    import vaf.core.user_notifications as un

    ws = workspaces
    secret = str(ws.root / TENANT_A / "secret.json")

    def boom(*a, **kw):
        raise OSError(28, "No space left on device", secret)

    hid = ws.tw.create_handoff(TENANT_A, ws.task_a, "Create", "c", automation_action={
        "operation": "create", "prompt": "after the decision", "time": "14:30"})["id"]
    rid = ws.tw.create_handoff(TENANT_A, ws.task_a, "Reject me", "c")["id"]
    monkeypatch.setattr(ws.tw, "append_event", boom)
    monkeypatch.setattr(un, "append_notification", boom)
    replies = _ws(monkeypatch, role="user", scope=TENANT_A, frames=[
        _handoff("approve", ws.task_a, hid), _handoff("reject", ws.task_a, rid)])
    for reply in replies:
        assert reply["ok"] is True and reply["error"] is None, reply
        assert secret not in json.dumps(reply) and str(ws.root) not in json.dumps(reply)
    assert replies[0]["automation_action_result"]["ok"] is True
    assert ws.tw.get_handoff(TENANT_A, ws.task_a, hid)["status"] == "approved"
    assert ws.tw.get_handoff(TENANT_A, ws.task_a, rid)["status"] == "rejected"


# ── the id limit counts bytes ─────────────────────────────────────────────────

@pytest.mark.parametrize("char,fits", [("é", 64), ("€", 42), ("😀", 32), ("x", 128)])
def test_the_id_limit_counts_bytes(store, workspaces, char, fits):
    """The filesystem's name limit is 255 BYTES; a character count let 100 three-byte
    characters through to the filesystem. MUTATION: count characters in `_entry`."""
    from vaf.core.path_jail import PathEscape

    tw = workspaces.tw
    assert tw._entry(char * fits) == char * fits
    with pytest.raises(PathEscape):
        tw._entry(char * (fits + 1))
    with pytest.raises(PathEscape):
        tw._entry("a\ud800b")


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_multibyte_ids_answer_like_missing(store, workspaces, monkeypatch, action):
    """MUTATION: count characters in `_entry` (the backstop then answers alike, but the
    direct refusal below goes red)."""
    ws = workspaces
    replies = _ws(monkeypatch, role="user", scope=TENANT_A, frames=[
        _handoff(action, ws.task_a, "ffff0000"),
        _handoff(action, ws.task_a, "€" * 84),
        _handoff(action, "😀" * 100, "ffff0000"),
    ])
    for reply in replies[1:]:
        assert _without_ids(reply) == _without_ids(replies[0]), reply
        assert str(ws.root) not in json.dumps(reply) and "Errno" not in json.dumps(reply)
    assert ws.tw._handoff_paths(TENANT_A, ws.task_a, "€" * 84) is None
    assert ws.tw._handoff_paths(TENANT_A, "😀" * 100, "ffff0000") is None


def test_workspace_tools_quote_no_server_path(store, workspaces):
    """The model's three workspace tools answered an OS error with its text, which quotes
    the server's data path. Multibyte ids are refused like missing tasks, and an error the
    OS raises (a file name too long inside the task) gives only its reason. MUTATIONS: answer
    the `except` of the read or the write tool with `str(e)`; make `vaf.describe_error`
    return `str(e)`. (A character count in `_entry` is caught by the byte tests above: this
    `except OSError` is the backstop that keeps such an id pathless here too.)"""
    from vaf.tools.thinking_workspace_handoff import ThinkingWorkspaceHandoffTool
    from vaf.tools.thinking_workspace_read import ThinkingWorkspaceReadTool
    from vaf.tools.thinking_workspace_write import ThinkingWorkspaceWriteTool

    ws = workspaces
    scope = {"user_scope_id": TENANT_A}
    outs = []
    for bad in ("€" * 100, "😀" * 100, "é" * 200):
        outs.append(ThinkingWorkspaceReadTool().run(mode="file", task_id=bad, path="x.md", **scope))
        outs.append(ThinkingWorkspaceWriteTool().run(mode="write_file", task_id=bad, path="x.md",
                                                     content="x", **scope))
        outs.append(ThinkingWorkspaceHandoffTool().run(task_id=bad, title="t", content="c",
                                                       **scope))
    long_name = "n" * 300 + ".md"
    outs.append(ThinkingWorkspaceWriteTool().run(mode="write_file", task_id=ws.task_a,
                                                 path=long_name, content="x", **scope))
    outs.append(ThinkingWorkspaceReadTool().run(mode="file", task_id=ws.task_a, path=long_name,
                                                **scope))
    for out in outs:
        assert out.startswith("Error"), out
        assert str(ws.root) not in out and "Errno" not in out and TENANT_A not in out, out
    assert "File name too long" in outs[-2], outs[-2]
    from vaf.core.path_jail import describe_error
    assert describe_error(FileNotFoundError("Task not found")) == "Task not found"


# ── one decision at a time, and a claimed approval is finished, never repeated ──
# Measured before: two threads approving one handoff with a create action made two
# automations in 30 of 30 trials; an `approved` save that failed after the action left the
# handoff pending and the retry created a second automation; a lone surrogate in a reject
# reason emptied the handoff file and the account's notification list.

class _Crash(BaseException):
    """A process dying at that point: nothing after it runs, no `except Exception` sees it."""


def _create_handoff(ws, prompt="once"):
    return ws.tw.create_handoff(TENANT_A, ws.task_a, "Create", "c", automation_action={
        "operation": "create", "prompt": prompt, "time": "15:30"})["id"]


def _a_tasks(store) -> set:
    return {p.stem for p in (store / TENANT_A).glob("*.json")}


def _record(ws, hid) -> dict:
    path = ws.root / TENANT_A / "tasks" / ws.task_a / "handoff" / f"{hid}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _count_creates(monkeypatch) -> list:
    """Record every `AutomationManager.create` (the real one still runs)."""
    calls = []
    real = AutomationManager.create

    def counting(self, task):
        calls.append(task.id)
        return real(self, task)

    monkeypatch.setattr(AutomationManager, "create", counting)
    return calls


def _race(fns) -> list:
    """Run the callables at the same instant (a barrier); each one's result or exception.
    Daemon workers: a decision that hangs fails the assert below and cannot keep the test
    process alive."""
    barrier = threading.Barrier(len(fns))
    out = [None] * len(fns)

    def run(i, fn):
        barrier.wait()
        try:
            out[i] = fn()
        except BaseException as e:  # noqa: BLE001 - the outcome is what is compared
            out[i] = e

    threads = [threading.Thread(target=run, args=(i, fn), daemon=True)
               for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads), "a decision hung"
    return out


@pytest.mark.parametrize("lock", ["filelock", "no_filelock"])
def test_two_threads_approve_once(store, workspaces, monkeypatch, lock):
    """Two approvals of one handoff at the same instant (the tray's two listeners, two
    sockets) both passed the pending check and both applied the action. MUTATION: replace
    `_decision_lock` with `contextlib.nullcontext()`; for `no_filelock`, make the
    in-process fallback a no-op."""
    import vaf.core.secure_store as secure_store

    ws = workspaces
    if lock == "no_filelock":
        monkeypatch.setattr(secure_store, "_get_filelock_cls", lambda: None)
    for trial in range(10):
        hid = _create_handoff(ws, f"trial {trial}")
        before = _a_tasks(store)
        out = _race([lambda: ws.tw.approve_handoff(TENANT_A, ws.task_a, hid)] * 2)
        assert sorted(type(o).__name__ for o in out) == ["HandoffAlreadyDecided", "bool"], out
        assert True in out, out
        assert len(_a_tasks(store) - before) == 1, (trial, _a_tasks(store) - before)
        assert _record(ws, hid)["status"] == "approved"


def test_a_failed_save_after_the_action_is_finished_once(store, workspaces, monkeypatch):
    """The `approved` save fails after the automation was created: the answer is "not
    saved", the claim stays on the pending record, and the retry finishes it with the same
    task instead of creating another. MUTATIONS: drop the `mgr.get(planned_id)` check in
    `_apply_automation_action` (the retry creates again); keep the claim in memory only,
    without its save before the action (the retry claims a new id and creates a second)."""
    ws = workspaces
    hid = _create_handoff(ws)
    before = _a_tasks(store)
    creates = _count_creates(monkeypatch)
    real_save = ws.tw._save_json
    failed = []

    def save(path, value):
        if not failed and isinstance(value, dict) and value.get("status") == "approved":
            failed.append(path)
            raise OSError(28, "No space left on device", str(path))
        return real_save(path, value)

    monkeypatch.setattr(ws.tw, "_save_json", save)
    with pytest.raises(ws.tw.HandoffNotSaved) as err:
        ws.tw.approve_handoff(TENANT_A, ws.task_a, hid)
    assert str(ws.root) not in str(err.value) and "Errno" not in str(err.value)
    record = _record(ws, hid)
    assert record["status"] == "pending"
    planned = record["claim"]["automation_id"]
    assert _a_tasks(store) - before == {planned}
    assert ws.tw.approve_handoff(TENANT_A, ws.task_a, hid) is True
    assert _a_tasks(store) - before == {planned}
    assert creates == [planned], creates
    record = _record(ws, hid)
    assert record["status"] == "approved" and "claim" not in record
    assert record["automation_action_result"] == {"ok": True, "operation": "create",
                                                  "task_id": planned}


def test_a_crash_after_the_claim_creates_once(store, workspaces, monkeypatch):
    """A crash between the claim and the action: nothing was created, and the retry creates
    exactly one task, with the claimed id. MUTATION: create without `planned_id` (a fresh
    random id)."""
    ws = workspaces
    hid = _create_handoff(ws)
    before = _a_tasks(store)
    real_apply = ws.tw._apply_automation_action
    fired = []

    def apply_once(*a, **kw):
        if not fired:
            fired.append(1)
            raise _Crash()
        return real_apply(*a, **kw)

    monkeypatch.setattr(ws.tw, "_apply_automation_action", apply_once)
    with pytest.raises(_Crash):
        ws.tw.approve_handoff(TENANT_A, ws.task_a, hid)
    record = _record(ws, hid)
    assert record["status"] == "pending" and _a_tasks(store) == before
    planned = record["claim"]["automation_id"]
    assert ws.tw._CLAIM_ID.fullmatch(planned)
    assert ws.tw.approve_handoff(TENANT_A, ws.task_a, hid) is True
    assert _a_tasks(store) - before == {planned}
    assert _record(ws, hid)["status"] == "approved"


def test_a_crash_after_the_create_is_not_repeated(store, workspaces, monkeypatch):
    """A crash after the task was stored and before the decision was saved: the retry
    reports that task and does not create it again (a second create would overwrite the
    task's state, such as its last run). MUTATION: drop the `mgr.get(planned_id)` check."""
    ws = workspaces
    hid = _create_handoff(ws)
    before = _a_tasks(store)
    calls = []
    real = AutomationManager.create

    def create_then_crash(self, task):
        calls.append(task.id)
        made = real(self, task)
        if len(calls) == 1:
            raise _Crash()
        return made

    monkeypatch.setattr(AutomationManager, "create", create_then_crash)
    with pytest.raises(_Crash):
        ws.tw.approve_handoff(TENANT_A, ws.task_a, hid)
    assert len(_a_tasks(store) - before) == 1
    assert _record(ws, hid)["status"] == "pending"
    assert ws.tw.approve_handoff(TENANT_A, ws.task_a, hid) is True
    assert len(_a_tasks(store) - before) == 1 and len(calls) == 1, calls
    record = _record(ws, hid)
    assert record["status"] == "approved" and "claim" not in record
    assert record["automation_action_result"]["task_id"] == calls[0]


def test_a_failed_automation_write_answers_not_saved(store, workspaces, monkeypatch):
    """The automation store fails while the claimed action runs: that is a decision not
    saved (the claim stays and the next approve finishes it), not a missing handoff, and
    no path reaches the caller. MUTATION: let the `OSError` of the action pass through
    `_approve_locked`."""
    ws = workspaces
    hid = _create_handoff(ws)
    before = _a_tasks(store)
    real = AutomationManager._save_task
    failed = []

    def save_task(self, task):
        if not failed:
            failed.append(task.id)
            raise OSError(28, "No space left on device", str(store / TENANT_A / task.id))
        return real(self, task)

    monkeypatch.setattr(AutomationManager, "_save_task", save_task)
    replies = _ws(monkeypatch, role="user", scope=TENANT_A, frames=[
        _handoff("approve", ws.task_a, hid), _handoff("approve", ws.task_a, hid)])
    assert replies[0]["ok"] is False and replies[0]["error"] == "handoff decision not saved"
    assert str(store) not in json.dumps(replies[0])
    assert replies[1]["ok"] is True, replies[1]
    assert _a_tasks(store) - before == set(failed), (failed, _a_tasks(store) - before)
    assert _record(ws, hid)["status"] == "approved"


_HOLD_THE_LOCK = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "import vaf.core.config as config\n"
    "config.get_local_admin_scope_id = lambda: sys.argv[3]\n"
    "from vaf.core.platform import Platform\n"
    "Platform.data_dir = staticmethod(lambda: Path(sys.argv[1]))\n"
    "import vaf.core.thinking_workspace as tw\n"
    "with tw._decision_lock(sys.argv[2]):\n"
    "    print('held', flush=True)\n"
    "    time.sleep(60)\n"
)


def test_a_killed_lock_holder_does_not_block(store, workspaces, monkeypatch):
    """A process that dies while it holds the decision lock must not block the account's
    next decision: the kernel releases the lock with the process. The child takes the lock
    through the module's own `_decision_lock`. MUTATION: an `O_CREAT|O_EXCL` marker removed
    only on a clean exit (it survives the kill, and the approve answers busy)."""
    import subprocess
    import sys

    ws = workspaces
    hid = _create_handoff(ws)
    monkeypatch.setattr(ws.tw, "_DECISION_LOCK_TIMEOUT_S", 1.0)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD_THE_LOCK, str(ws.root.parent), TENANT_A, LOCAL],
        stdout=subprocess.PIPE, cwd=str(_REPO), text=True)
    try:
        # Bounded: a child that stalls before it holds the lock fails here, and the
        # `finally` below kills and reaps it.
        ready = []
        reader = threading.Thread(target=lambda: ready.append(proc.stdout.readline()),
                                  daemon=True)
        reader.start()
        reader.join(30)
        assert ready and ready[0].strip() == "held", ready
        with pytest.raises(ws.tw.HandoffBusy):
            ws.tw.approve_handoff(TENANT_A, ws.task_a, hid)
        proc.kill()
        proc.wait(10)
        # True, not `HandoffBusy`: acquired within the timeout, so the dead holder's lock
        # was released with it.
        assert ws.tw.approve_handoff(TENANT_A, ws.task_a, hid) is True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
        proc.stdout.close()
    assert _record(ws, hid)["status"] == "approved"


def test_a_busy_decision_answers_busy(store, workspaces, monkeypatch):
    """While another decision of the account holds the lock past the timeout, the approve
    answers "handoff is being decided", applies nothing, and sends no log line or
    notification. MUTATION: answer `HandoffBusy` with "handoff already decided" in the
    WebSocket branch."""
    import vaf.core.user_notifications as un

    ws = workspaces
    notes = []
    monkeypatch.setattr(un, "append_notification", lambda *a, **kw: notes.append(kw["title"]))
    hid = _create_handoff(ws)
    before = _a_tasks(store)
    monkeypatch.setattr(ws.tw, "_DECISION_LOCK_TIMEOUT_S", 0.2)
    held, done = threading.Event(), threading.Event()

    def hold():
        with ws.tw._decision_lock(TENANT_A):
            held.set()
            done.wait(20)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        assert held.wait(10)
        seen = []
        replies = _ws(monkeypatch, role="user", scope=TENANT_A, seen=seen, frames=[
            _handoff("approve", ws.task_a, hid), _handoff("reject", ws.task_a, hid)])
    finally:
        done.set()
        holder.join(10)
    for reply in replies:
        assert reply["ok"] is False and reply["error"] == "handoff is being decided", reply
    logs = [m["entry"]["message"] for m in seen if m.get("type") == "new_log"
            and "Thinking Workspace handoff" in m["entry"]["message"]]
    assert logs == [], logs
    assert notes == [], notes
    assert _a_tasks(store) == before and _record(ws, hid)["status"] == "pending"


def _crash_after_claim(ws, monkeypatch, hid) -> str:
    """Approve once and die between the claim and the action; the planned id."""
    real_apply = ws.tw._apply_automation_action

    def crash(*a, **kw):
        raise _Crash()

    monkeypatch.setattr(ws.tw, "_apply_automation_action", crash)
    with pytest.raises(_Crash):
        ws.tw.approve_handoff(TENANT_A, ws.task_a, hid)
    monkeypatch.setattr(ws.tw, "_apply_automation_action", real_apply)
    return _record(ws, hid)["claim"]["automation_id"]


def _crash_after_create(ws, monkeypatch, hid) -> str:
    """Approve once and die after the planned task was stored; its id."""
    real = AutomationManager.create

    def create_then_crash(self, task):
        real(self, task)
        raise _Crash()

    monkeypatch.setattr(AutomationManager, "create", create_then_crash)
    with pytest.raises(_Crash):
        ws.tw.approve_handoff(TENANT_A, ws.task_a, hid)
    monkeypatch.setattr(AutomationManager, "create", real)
    return _record(ws, hid)["claim"]["automation_id"]


def test_a_reject_wins_over_a_claim_that_created_nothing(store, workspaces, monkeypatch):
    """An approval that died between its claim and its action created nothing, so a reject
    still wins: it is stored as rejected and announced like any reject. MUTATION: let every
    claim count as taken effect in `_claim_took_effect` (the reject then creates the
    automation and answers "already approved")."""
    import vaf.core.user_notifications as un

    ws = workspaces
    hid = _create_handoff(ws)
    before = _a_tasks(store)
    _crash_after_claim(ws, monkeypatch, hid)
    notes = []
    monkeypatch.setattr(un, "append_notification", lambda *a, **kw: notes.append(kw["title"]))
    seen = []
    replies = _ws(monkeypatch, role="user", scope=TENANT_A, seen=seen, frames=[
        ({"type": "reject_thinking_workspace_handoff", "task_id": ws.task_a,
          "handoff_id": hid, "reason": "changed my mind"}, "thinking_workspace_handoff_result")])
    assert replies[0]["ok"] is True and replies[0]["error"] is None, replies[0]
    record = _record(ws, hid)
    assert record["status"] == "rejected" and "claim" not in record, record
    assert _a_tasks(store) == before
    logs = [m["entry"]["message"] for m in seen if m.get("type") == "new_log"
            and "Thinking Workspace handoff" in m["entry"]["message"]]
    assert len(logs) == 1 and "rejected" in logs[0], logs
    assert notes == ["Workspace handoff rejected"], notes


def test_a_reject_after_a_claimed_create_finishes_and_announces_it(store, workspaces,
                                                                   monkeypatch):
    """An approval that died after its automation was stored has taken effect: a reject
    finishes it, never stores `rejected` over an automation that exists, answers "handoff
    already approved" with the result, and sends the approval's notification and log line,
    so the account learns what was created. MUTATIONS: let the reject branch answer
    `HandoffAlreadyApproved` like any `HandoffAlreadyDecided`; separately, let
    `reject_handoff` give up every claim."""
    import vaf.core.user_notifications as un

    ws = workspaces
    hid = _create_handoff(ws)
    before = _a_tasks(store)
    planned = _crash_after_create(ws, monkeypatch, hid)
    assert _a_tasks(store) - before == {planned}
    notes = []
    monkeypatch.setattr(un, "append_notification", lambda *a, **kw: notes.append(kw["title"]))
    seen = []
    replies = _ws(monkeypatch, role="user", scope=TENANT_A, seen=seen, frames=[
        _handoff("reject", ws.task_a, hid)])
    reply = replies[0]
    assert reply["ok"] is False and reply["error"] == "handoff already approved", reply
    assert reply["automation_action_result"] == {"ok": True, "operation": "create",
                                                 "task_id": planned}, reply
    record = _record(ws, hid)
    assert record["status"] == "approved" and "claim" not in record, record
    assert _a_tasks(store) - before == {planned}
    logs = [m["entry"]["message"] for m in seen if m.get("type") == "new_log"
            and "Thinking Workspace handoff" in m["entry"]["message"]]
    assert len(logs) == 1 and "approved" in logs[0] and "applied" in logs[0], logs
    assert notes == ["Workspace handoff approved"], notes


def test_a_forged_claim_id_is_not_used_as_a_name(store, workspaces, monkeypatch):
    """A claim this module did not write (an id that is not 8 hex) is replaced, never used
    as an automation file name. MUTATION: accept any string in `_valid_claim`."""
    ws = workspaces
    hid = _create_handoff(ws)
    path = ws.root / TENANT_A / "tasks" / ws.task_a / "handoff" / f"{hid}.json"
    record = _record(ws, hid)
    record["claim"] = {"decision": "approve", "automation_id": f"../{TENANT_B}/{B_ID}"}
    path.write_text(json.dumps(record), encoding="utf-8")
    b_before = _path(store, B_ID).read_bytes()
    before = _a_tasks(store)
    assert ws.tw.approve_handoff(TENANT_A, ws.task_a, hid) is True
    made = _a_tasks(store) - before
    assert len(made) == 1 and ws.tw._CLAIM_ID.fullmatch(next(iter(made))), made
    assert _path(store, B_ID).read_bytes() == b_before


def test_a_lone_surrogate_reason_keeps_the_handoff_and_notifications(store, workspaces,
                                                                     monkeypatch):
    """A reject reason with a lone surrogate (a valid JSON escape) emptied the handoff file
    and cut off the account's notification file, which then read as empty and was
    overwritten. MUTATION: drop the reason coercion in `reject_handoff` and in the
    WebSocket branch (the reject answers not saved and stays pending)."""
    import vaf.core.user_notifications as un

    ws = workspaces
    monkeypatch.setattr(un, "append_notification", ws.real_notify)
    un.append_notification(TENANT_A, kind="system", title="one", summary="first")
    un.append_notification(TENANT_A, kind="system", title="two", summary="second")
    rid = ws.tw.create_handoff(TENANT_A, ws.task_a, "Reject me", "c")["id"]
    frame = {"type": "reject_thinking_workspace_handoff", "task_id": ws.task_a,
             "handoff_id": rid, "reason": "x\ud800"}
    replies = _ws(monkeypatch, role="user", scope=TENANT_A,
                  frames=[(frame, "thinking_workspace_handoff_result")])
    assert replies[0]["ok"] is True and replies[0]["error"] is None, replies[0]
    record = _record(ws, rid)
    assert record["status"] == "rejected" and record["rejected_reason"] == "x?", record
    titles = [n["title"] for n in un.get_notifications(TENANT_A, limit=10)]
    assert {"one", "two", "Workspace handoff rejected"} <= set(titles), titles


def test_append_notification_never_truncates(store, workspaces):
    """MUTATION: `append_notification` back to `open(path, "w")` + `json.dump`."""
    import vaf.core.user_notifications as un

    notify = workspaces.real_notify
    notify(TENANT_A, kind="system", title="one", summary="first")
    notify(TENANT_A, kind="system", title="two", summary="second")
    path = un._file_path(TENANT_A)
    notify(TENANT_A, kind="system", title="three", summary="x\ud800")
    items = json.loads(path.read_text(encoding="utf-8"))
    assert [i["title"] for i in items] == ["one", "two"], items


def test_a_refused_decision_sends_no_log_or_notification(store, workspaces, monkeypatch):
    """A refused decision (already decided, missing, busy, not saved) used to send the
    "handoff approved/rejected" log line and a decision-titled notification all the same.
    MUTATION: drop the `if not ok` gate in the approve branch; separately, in the reject
    branch."""
    import vaf.core.user_notifications as un

    ws = workspaces
    notes = []
    monkeypatch.setattr(un, "append_notification", lambda *a, **kw: notes.append(kw["title"]))
    hid = _create_handoff(ws)
    unsaved = ws.tw.create_handoff(TENANT_A, ws.task_a, "Unsaved", "c")["id"]
    real_save = ws.tw._save_json

    def save(path, value):
        if isinstance(value, dict) and value.get("id") == unsaved:
            raise OSError(28, "No space left on device", str(path))
        return real_save(path, value)

    monkeypatch.setattr(ws.tw, "_save_json", save)
    seen = []
    replies = _ws(monkeypatch, role="user", scope=TENANT_A, seen=seen, frames=[
        _handoff("approve", ws.task_a, hid),
        _handoff("approve", ws.task_a, hid),
        _handoff("reject", ws.task_a, hid),
        _handoff("approve", ws.task_a, "ffff0000"),
        _handoff("reject", ws.task_a, "ffff0000"),
        _handoff("approve", ws.task_a, unsaved),
        _handoff("reject", ws.task_a, unsaved),
    ])
    assert replies[0]["ok"] is True
    errors = [r["error"] for r in replies[1:]]
    assert errors == ["handoff already decided"] * 2 + ["handoff not found"] * 2 + [
        "handoff decision not saved"] * 2, errors
    logs = [m["entry"]["message"] for m in seen if m.get("type") == "new_log"
            and "Thinking Workspace handoff" in m["entry"]["message"]]
    assert len(logs) == 1 and hid in logs[0] and "approved" in logs[0], logs
    assert notes == ["Workspace handoff approved"], notes


# ── seeded fuzz: any interleaving of approve and reject decides once ──────────

def _decision_fuzz(ws, store, monkeypatch, draws: int, seed: int = 4444) -> list:
    """Per draw: 2 to 4 threads behind one barrier, each approving or rejecting one fresh
    handoff, with or without a create action; sometimes an approval first dies after its
    claim (nothing created) or after its create (the automation exists). Returns the draws
    that broke the invariant."""
    rng = random.Random(seed)
    tw = ws.tw
    bad = []
    for draw in range(draws):
        with_create = rng.random() < 0.7
        crash = rng.choice((None, "claim", "create")) if with_create and rng.random() < 0.4 else None
        actions = [rng.choice(("approve", "reject")) for _ in range(rng.randint(2, 4))]
        if with_create:
            hid = _create_handoff(ws, f"fuzz {draw}")
        else:
            hid = tw.create_handoff(TENANT_A, ws.task_a, "Plain", "c")["id"]
        before = _a_tasks(store)
        if crash == "claim":
            _crash_after_claim(ws, monkeypatch, hid)
        elif crash == "create":
            _crash_after_create(ws, monkeypatch, hid)

        def call(action):
            if action == "approve":
                return lambda: tw.approve_handoff(TENANT_A, ws.task_a, hid)
            return lambda: tw.reject_handoff(TENANT_A, ws.task_a, hid, reason="no")

        out = _race([call(a) for a in actions])
        record = _record(ws, hid)
        made = len(_a_tasks(store) - before)
        trues = [o for o in out if o is True]
        others = [o for o in out if o is not True]
        # Whoever locks first decides. Only after a create that took effect does a reject
        # that comes first finish the approval and answer "already approved" (not True).
        if crash == "create" and "reject" in actions:
            allowed = {0, 1} if "approve" in actions else {0}
        else:
            allowed = {1}
        problems = []
        if len(trues) not in allowed:
            problems.append(f"{len(trues)} calls returned True")
        if not all(isinstance(o, tw.HandoffAlreadyDecided) for o in others):
            problems.append(f"outcomes {out!r}")
        if record.get("status") not in ("approved", "rejected") or "claim" in record:
            problems.append(f"record {record.get('status')} claim={'claim' in record}")
        if crash == "create" and record.get("status") != "approved":
            problems.append("an approval in effect was rejected")
        want_made = 1 if (with_create and record.get("status") == "approved") else 0
        if made != want_made:
            problems.append(f"{made} automations created")
        if problems:
            bad.append((draw, actions, with_create, crash, problems))
    return bad


def test_decision_interleavings_fuzz(store, workspaces, monkeypatch):
    """MUTATION: replace `_decision_lock` with `contextlib.nullcontext()`.
    `VAF_FUZZ_DRAWS` raises the draw count for a run outside the suite."""
    draws = int(os.environ.get("VAF_FUZZ_DRAWS", "12"))
    bad = _decision_fuzz(workspaces, store, monkeypatch, draws)
    assert bad == [], bad[:5]


class _SlowFilelock:
    """Stands in for the `filelock` module on its first import: the attribute read waits,
    which is the window in which a second thread asks for the lock class."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        import time

        time.sleep(0.03)
        return getattr(self._real, name)


def _first_probe(monkeypatch):
    """The process state before anything asked for the lock class, with a slow import."""
    import sys

    import filelock

    import vaf.core.secure_store as secure_store

    monkeypatch.setattr(secure_store, "_FILELOCK_CLS", None)
    monkeypatch.setattr(secure_store, "_FILELOCK_PROBED", False)
    monkeypatch.setitem(sys.modules, "filelock", _SlowFilelock(filelock))
    return secure_store, filelock


def test_the_first_lock_class_is_the_same_for_every_thread(monkeypatch):
    """Found by the decision fuzz (the first draw of a fresh process): the accessor set its
    "probed" flag before the class, so a thread asking during the first import got None and
    took the in-process fallback while the first one took the file lock, two locks that do
    not exclude each other. MUTATION: set `_FILELOCK_PROBED` before `_FILELOCK_CLS` again."""
    secure_store, filelock = _first_probe(monkeypatch)
    out = _race([secure_store._get_filelock_cls] * 4)
    assert out == [filelock.FileLock] * 4, out


def test_the_first_decisions_of_a_process_share_one_lock(store, workspaces, monkeypatch):
    """The fuzz mismatch as a fixed row: three approvals as the first decisions of a
    process, while the lock class is still being imported; the action takes long enough
    that the decisions overlap when they hold different locks (measured: two answered
    True, and the second applied the claimed create again). MUTATION: as above."""
    import time

    _first_probe(monkeypatch)
    ws = workspaces
    real_apply = ws.tw._apply_automation_action

    def slow_apply(*a, **kw):
        time.sleep(0.4)
        return real_apply(*a, **kw)

    monkeypatch.setattr(ws.tw, "_apply_automation_action", slow_apply)
    hid = _create_handoff(ws, "first of the process")
    before = _a_tasks(store)
    out = _race([lambda: ws.tw.approve_handoff(TENANT_A, ws.task_a, hid)] * 3)
    assert out.count(True) == 1, out
    assert all(isinstance(o, ws.tw.HandoffAlreadyDecided) for o in out if o is not True), out
    assert len(_a_tasks(store) - before) == 1


# ── round two: no error keeps a handoff pending, the loop never waits, ids before the lock ──

_MISTYPED = [
    {"operation": "create", "prompt": 5},
    {"operation": "create", "prompt": "p", "name": ["a"]},
    {"operation": "create", "prompt": "p", "frequency": "weekly", "weekday": 3},
    {"operation": "update", "task_id": 7, "name": "x"},
    # Raises nowhere: the update would store a number where the scheduler reads a time.
    {"operation": "update", "task_id": A_ID, "time": 5},
]
_MISTYPED_IDS = ["prompt", "name", "weekday", "update-task-id", "update-time"]


def _with_action(ws, action, *, claim=None) -> str:
    """A pending handoff of tenant A whose record carries `action` as it is (written past
    `create_handoff`, as a record from before its check would be), optionally claimed."""
    hid = ws.tw.create_handoff(TENANT_A, ws.task_a, "Stored", "c")["id"]
    path = ws.root / TENANT_A / "tasks" / ws.task_a / "handoff" / f"{hid}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["automation_action"] = action
    if claim is not None:
        record["claim"] = claim
    path.write_text(json.dumps(record), encoding="utf-8")
    return hid


@pytest.mark.parametrize("action", _MISTYPED, ids=_MISTYPED_IDS)
def test_a_mistyped_action_is_decided_and_never_sticks(store, workspaces, action):
    """An action with a field of the wrong type raised on every approve and, once claimed,
    on every reject too: the handoff stayed pending for good. It is now approved with the
    result "invalid automation action" and never claimed, and an update with a mistyped field
    is not applied (it raised nowhere and stored a number as the time). MUTATION: drop the
    type check before the claim in `_approve_locked`."""
    ws = workspaces
    hid = _with_action(ws, action)
    before = _snapshot(store)
    assert ws.tw.approve_handoff(TENANT_A, ws.task_a, hid) is True
    record = _record(ws, hid)
    assert record["status"] == "approved" and "claim" not in record, record
    assert record["automation_action_result"] == {"ok": False,
                                                  "error": "invalid automation action"}
    with pytest.raises(ws.tw.HandoffAlreadyDecided):
        ws.tw.reject_handoff(TENANT_A, ws.task_a, hid)
    assert _snapshot(store) == before


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize("action", _MISTYPED, ids=_MISTYPED_IDS)
def test_a_claimed_mistyped_action_is_decided(store, workspaces, action, decision):
    """A record already claimed with such an action (written by the build before the type
    check): an approve finishes it as "invalid automation action", a reject wins because the
    action never ran; either way the claim is gone. MUTATIONS: drop the type check in
    `_approve_locked` and in `_claim_took_effect`."""
    ws = workspaces
    hid = _with_action(ws, action, claim={"decision": "approve", "automation_id": "abcd0123"})
    if decision == "approve":
        assert ws.tw.approve_handoff(TENANT_A, ws.task_a, hid) is True
        want = "approved"
    else:
        assert ws.tw.reject_handoff(TENANT_A, ws.task_a, hid, reason="no") is True
        want = "rejected"
    record = _record(ws, hid)
    assert record["status"] == want and "claim" not in record, record
    assert "abcd0123" not in _a_tasks(store)


def test_any_error_of_the_action_after_the_claim_ends_the_decision(store, workspaces,
                                                                     monkeypatch):
    """An error the type check does not foresee (here a `RuntimeError`) is finished like a
    refusal, with the claim removed, instead of staying pending for every later decision.
    MUTATION: drop the `except Exception` after the action in `_approve_locked`."""
    ws = workspaces
    hid = _create_handoff(ws)

    def boom(*a, **kw):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(ws.tw, "_apply_automation_action", boom)
    assert ws.tw.approve_handoff(TENANT_A, ws.task_a, hid) is True
    record = _record(ws, hid)
    assert record["status"] == "approved" and "claim" not in record, record
    assert record["automation_action_result"] == {"ok": False,
                                                  "error": "invalid automation action"}


def test_the_handoff_tool_refuses_a_mistyped_action(store, workspaces):
    """The model's proposal with a number as the prompt is refused where it is made, with a
    reason the model can act on, and nothing is stored. MUTATION: drop the type check in
    `create_handoff`."""
    from vaf.tools.thinking_workspace_handoff import ThinkingWorkspaceHandoffTool

    ws = workspaces
    handoff_dir = ws.root / TENANT_A / "tasks" / ws.task_a / "handoff"
    before = sorted(p.name for p in handoff_dir.glob("*")) if handoff_dir.exists() else []
    out = ThinkingWorkspaceHandoffTool().run(
        task_id=ws.task_a, title="t", content="c", user_scope_id=TENANT_A,
        automation_action={"operation": "create", "prompt": 5})
    assert out.startswith("Error") and "invalid automation action" in out, out
    after = sorted(p.name for p in handoff_dir.glob("*")) if handoff_dir.exists() else []
    assert after == before


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_the_websocket_loop_is_free_while_a_decision_waits(store, workspaces, monkeypatch,
                                                           action):
    """A decision waiting for another decision's lock held the event loop that serves every
    socket of the listener. Now the probe runs a coroutine on the session's loop while the
    decision waits, and only then lets the lock go: on a free loop the waiting decision then
    gets the lock and succeeds; on a blocked loop the probe could run only after the decision
    had given up as busy. The lock timeout only bounds the test. MUTATION: call
    `approve_handoff`, separately `reject_handoff`, directly in its WebSocket branch again."""
    import anyio

    ws = workspaces
    hid = _create_handoff(ws)
    monkeypatch.setattr(ws.tw, "_DECISION_LOCK_TIMEOUT_S", 3.0)
    held, done, deciding = threading.Event(), threading.Event(), threading.Event()

    def hold():
        with ws.tw._decision_lock(TENANT_A):
            held.set()
            done.wait(20)

    name = f"{action}_handoff"
    real = getattr(ws.tw, name)

    def deciding_wrapper(*a, **kw):
        deciding.set()
        return real(*a, **kw)

    monkeypatch.setattr(ws.tw, name, deciding_wrapper)
    holder = threading.Thread(target=hold, daemon=True)
    holder.start()

    def probe(session):
        assert deciding.wait(10)   # the decision has started and waits for the lock
        session.portal.call(anyio.sleep, 0)
        done.set()                 # only now may the decision get the lock

    try:
        assert held.wait(10)
        replies = _ws(monkeypatch, role="user", scope=TENANT_A, probe=probe,
                      frames=[_handoff(action, ws.task_a, hid)])
    finally:
        done.set()
        holder.join(10)
    assert replies[0]["ok"] is True and replies[0]["error"] is None, replies[0]


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_an_invalid_id_is_not_found_while_the_lock_is_held(store, workspaces, monkeypatch,
                                                            action):
    """An id that is not one entry answers "not found" at once, also while another decision
    holds the lock, not "busy" after the wait. MUTATION: drop the `_handoff_paths` check
    before the lock."""
    ws = workspaces
    monkeypatch.setattr(ws.tw, "_DECISION_LOCK_TIMEOUT_S", 0.2)
    decide = ws.tw.approve_handoff if action == "approve" else ws.tw.reject_handoff
    held, done = threading.Event(), threading.Event()

    def hold():
        with ws.tw._decision_lock(TENANT_A):
            held.set()
            done.wait(20)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        assert held.wait(10)
        for task_id, handoff_id in ((ws.task_a, "../x"), (ws.task_a, "x" * 300),
                                    ("../" + TENANT_B, "ffff0000")):
            assert decide(TENANT_A, task_id, handoff_id) is False, (task_id, handoff_id)
    finally:
        done.set()
        holder.join(10)


def test_the_fallback_lock_is_one_per_workspace(store, workspaces, monkeypatch):
    """Without filelock, one account's decision made every other account wait; the machine
    owner's aliases still share one lock, as they share one workspace. MUTATIONS: one global
    fallback lock; separately, key the fallback by the raw scope."""
    import vaf.core.secure_store as secure_store

    tw = workspaces.tw
    monkeypatch.setattr(secure_store, "_get_filelock_cls", lambda: None)
    monkeypatch.setattr(tw, "_DECISION_LOCK_TIMEOUT_S", 0.2)

    def held_by(scope, check):
        held, done = threading.Event(), threading.Event()

        def hold():
            with tw._decision_lock(scope):
                held.set()
                done.wait(20)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        try:
            assert held.wait(10)
            check()
        finally:
            done.set()
            holder.join(10)

    def a_is_free():
        with tw._decision_lock(TENANT_A):
            pass

    def owner_aliases_wait():
        for alias in (LOCAL, "default"):
            with pytest.raises(tw.HandoffBusy):
                with tw._decision_lock(alias):
                    pass

    held_by(TENANT_B, a_is_free)
    held_by(None, owner_aliases_wait)


def test_a_lock_file_error_is_not_answered_as_missing(store, workspaces, monkeypatch):
    """The lock file cannot be opened (a permission error): the handoff exists, so the
    answer is "handoff decision not saved", without the path, and nothing is decided.
    MUTATION: drop the `except OSError` around the lock acquisition."""
    import vaf.core.secure_store as secure_store

    ws = workspaces
    hid = _create_handoff(ws)

    class Unopenable:
        def __init__(self, path):
            self.path = path

        def acquire(self):
            raise PermissionError(13, "Permission denied", self.path)

        def release(self):
            pass

    real_cls = secure_store._get_filelock_cls()

    def lock_for(path, timeout=-1, **kw):
        # Only the decision lock fails: the handshake's own stores lock through the same
        # accessor, and a fake for them closes the socket before the command is read.
        if Path(path).name == "handoff.lock":
            return Unopenable(path)
        return real_cls(path, timeout=timeout, **kw)

    monkeypatch.setattr(secure_store, "_get_filelock_cls", lambda: lock_for)
    replies = _ws(monkeypatch, role="user", scope=TENANT_A, frames=[
        _handoff("approve", ws.task_a, hid), _handoff("reject", ws.task_a, hid)])
    for reply in replies:
        assert reply["ok"] is False and reply["error"] == "handoff decision not saved", reply
        assert str(ws.root) not in json.dumps(reply) and "Errno" not in json.dumps(reply)
    assert _record(ws, hid)["status"] == "pending"


def test_save_task_never_truncates(store):
    """A task save that fails half way (here a value that does not encode) emptied the task
    file, which every later load skipped: the automation was gone. MUTATION:
    `AutomationManager._save_task` back to `open(path, "w")` + `json.dump`."""
    mgr = AutomationManager(user_scope_id=TENANT_A)
    path = _path(store, A_ID)
    before = path.read_bytes()
    task = mgr.get(A_ID)
    task.name = "x\ud800"
    with pytest.raises(ValueError):
        mgr._save_task(task)
    assert path.read_bytes() == before
    assert AutomationManager(user_scope_id=TENANT_A).get(A_ID).name == f"Task {A_ID}"
    assert not [p.name for p in path.parent.iterdir() if p.name.startswith(".tmp-")]



# ── the schedule fields have one coercion; recovery reads the store strictly ────

@pytest.mark.parametrize("given,want", [
    ({"day": "15"}, {"day": 15}), ({"day": " 7 "}, {"day": 7}), ({"day": 0}, {"day": 1}),
    ({"day": 99}, {"day": 31}), ({"day": 15.0}, {"day": 15}), ({"day": "x"}, {}),
    ({"day": [3]}, {}), ({"day": None}, {"day": None}),
    ({"enabled": "false"}, {"enabled": False}), ({"enabled": " OFF "}, {"enabled": False}),
    ({"enabled": "0"}, {"enabled": False}), ({"enabled": "no"}, {"enabled": False}),
    ({"enabled": ""}, {"enabled": False}), ({"enabled": "true"}, {"enabled": True}),
    ({"enabled": "Yes"}, {"enabled": True}), ({"enabled": "1"}, {"enabled": True}),
    ({"enabled": "maybe"}, {"enabled": False}), ({"enabled": 0}, {"enabled": False}),
    ({"enabled": "nein"}, {"enabled": False}), ({"enabled": "aus"}, {"enabled": False}),
    ({"enabled": "disabled"}, {"enabled": False}),
    ({"enabled": "\uff46\uff41\uff4c\uff53\uff45"}, {"enabled": False}),
    ({"enabled": "false\u200b"}, {"enabled": False}), ({"enabled": ["x"]}, {"enabled": False}),
    ({"enabled": "ein"}, {"enabled": True}), ({"enabled": "Aktiv"}, {"enabled": True}),
    ({"enabled": "active"}, {"enabled": True}), ({"enabled": float("nan")}, {"enabled": False}),
    ({"enabled": float("inf")}, {"enabled": False}), ({"enabled": "x" * 65}, {"enabled": False}),
    ({"enabled": "ja"}, {"enabled": True}), ({"enabled": "\uff54\uff52\uff55\uff45"}, {"enabled": True}),
    ({"enabled": True}, {"enabled": True}),
    ({"weekday": " Monday "}, {"weekday": "monday"}), ({"weekday": "Montag"}, {"weekday": "monday"}),
    ({"weekday": ""}, {"weekday": None}), ({"weekday": 3}, {}),
    ({"name": "kept", "time": "7:5"}, {"name": "kept", "time": "7:5"}),
])
def test_coerce_schedule_fields_reads_each_value_one_way(given, want):
    """The one coercion every lane goes through, value by value. Text is parsed, never
    `bool()`-ed: "false" is off. MUTATION: `bool(enabled)` for text; separately, keep a day
    that does not parse."""
    assert auto_mod.coerce_schedule_fields(given) == want


def _armed_scheduler(monkeypatch):
    """The process scheduler as `refresh_scheduler_from_disk` sees it running: a global
    manager whose loop thread is alive (a thread that only waits)."""
    stop = threading.Event()
    loop = threading.Thread(target=stop.wait, daemon=True)
    loop.start()
    sched = AutomationManager()
    sched._running = True
    sched._scheduler_thread = loop
    for task in sched.list(enabled_only=True):
        sched._schedule_task(task)
    monkeypatch.setattr(auto_mod, "_scheduler_manager", sched)
    return sched, stop


def test_a_text_day_in_an_approved_update_keeps_every_account_armed(store, workspaces,
                                                                     monkeypatch):
    """An approved update with `day: "15"` stored the text; the monthly clock rule then
    raised in `list()`, so the refresh that follows every update cleared every account's
    clock jobs and armed none, and a scheduler start failed. The day is now coerced where
    every update lands. MUTATION: drop the coercion in `AutomationManager.update`."""
    ws = workspaces
    path = _path(store, A_ID)
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(frequency="monthly", day=3)
    path.write_text(json.dumps(record), encoding="utf-8")
    sched, stop = _armed_scheduler(monkeypatch)
    try:
        assert B_ID in sched._clock_jobs and LOCAL_ID in sched._clock_jobs
        hid = ws.tw.create_handoff(TENANT_A, ws.task_a, "Move it", "c", automation_action={
            "operation": "update", "task_id": A_ID, "day": "15"})["id"]
        assert ws.tw.approve_handoff(TENANT_A, ws.task_a, hid) is True
        assert _record(ws, hid)["automation_action_result"]["ok"] is True
        assert json.loads(path.read_text(encoding="utf-8"))["day"] == 15
        AutomationManager().list()
        assert {B_ID, LOCAL_ID, A_ID} <= set(sched._clock_jobs), sorted(sched._clock_jobs)
    finally:
        stop.set()


def test_a_text_day_already_on_disk_is_read_as_a_number(store):
    """A record written before the coercion still carries `"day": "15"`: the data-model
    boundary reads it as 15, and the global list and its next run work. MUTATION: drop the
    coercion in `AutomationTask.__post_init__`."""
    path = _path(store, B_ID)
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(frequency="monthly", day="15", enabled="false")
    path.write_text(json.dumps(record), encoding="utf-8")
    task = AutomationManager().get(B_ID)
    assert task.day == 15 and task.enabled is False
    assert AutomationManager(user_scope_id=TENANT_B).get(B_ID).calculate_next_run().day == 15


def test_the_next_run_never_raises_on_a_mistyped_field():
    """`calculate_next_run` promises no exception; a day or weekday of the wrong type set
    past the coercion raised `TypeError` or `AttributeError` out of `list()`. MUTATION: catch
    only `ValueError` and `OverflowError` again."""
    monthly = AutomationTask(frequency="monthly", day=5, time="08:00")
    monthly.day = "x"
    weekly = AutomationTask(frequency="weekly", weekday="monday", time="08:00")
    weekly.weekday = 3
    assert monthly.calculate_next_run() is None
    assert weekly.calculate_next_run() is None


def test_enabled_false_as_text_switches_an_automation_off(store, workspaces, monkeypatch):
    """`bool("false")` is True: an update that said "false" kept the automation running.
    Over the WebSocket and through an approved handoff it is now off. MUTATION: `bool()`
    for text in `coerce_schedule_fields`."""
    ws = workspaces
    replies = _ws(monkeypatch, role="user", scope=TENANT_A, frames=[_upd(A_ID, enabled="false")])
    assert replies[0]["ok"] is True, replies[0]
    assert json.loads(_path(store, A_ID).read_text(encoding="utf-8"))["enabled"] is False
    hid = ws.tw.create_handoff(TENANT_B, ws.task_b, "Off", "c", automation_action={
        "operation": "update", "task_id": B_ID, "enabled": "false"})["id"]
    assert ws.tw.approve_handoff(TENANT_B, ws.task_b, hid) is True
    assert json.loads(_path(store, B_ID).read_text(encoding="utf-8"))["enabled"] is False


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_an_unreadable_planned_task_keeps_the_claim(store, workspaces, monkeypatch, decision):
    """The planned task is on disk but cannot be parsed: the loaders skip it, so a lenient
    lookup took it for missing, and a reject stored `rejected` over it (an approve would
    have created it anew). It is now "not saved" and the claim stays. MUTATION: look the
    planned task up through `AutomationManager.get` again."""
    ws = workspaces
    hid = _create_handoff(ws)
    planned = _crash_after_create(ws, monkeypatch, hid)
    planned_path = store / TENANT_A / f"{planned}.json"
    planned_path.write_text("{not json", encoding="utf-8")
    decide = ws.tw.approve_handoff if decision == "approve" else ws.tw.reject_handoff
    with pytest.raises(ws.tw.HandoffNotSaved):
        decide(TENANT_A, ws.task_a, hid)
    record = _record(ws, hid)
    assert record["status"] == "pending" and record["claim"]["automation_id"] == planned
    assert planned_path.read_text(encoding="utf-8") == "{not json"


def test_a_reject_finishes_from_the_observed_create(store, workspaces, monkeypatch):
    """A reject that found the planned task finishes the approval from that observation. A
    second read of the store, through the approval, raced a delete that takes no decision
    lock and then created the task anew. MUTATION: finish through `_approve_locked` again."""
    ws = workspaces
    hid = _create_handoff(ws)
    planned = _crash_after_create(ws, monkeypatch, hid)
    real_exists = ws.tw._planned_task_exists

    def exists_then_deleted(scope, planned_id):
        found = real_exists(scope, planned_id)
        if found:
            (store / TENANT_A / f"{planned_id}.json").unlink()   # another tab deletes it
        return found

    monkeypatch.setattr(ws.tw, "_planned_task_exists", exists_then_deleted)
    creates = _count_creates(monkeypatch)
    with pytest.raises(ws.tw.HandoffAlreadyApproved) as approved:
        ws.tw.reject_handoff(TENANT_A, ws.task_a, hid)
    assert approved.value.action_result == {"ok": True, "operation": "create",
                                            "task_id": planned}
    assert creates == [], creates
    assert _record(ws, hid)["status"] == "approved"


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_a_stored_create_with_a_legacy_value_is_approved_not_refused(store, workspaces,
                                                                      decision):
    """A claimed create with a value an older version accepted (`day: true`) whose task is
    already stored: the planned task is looked up before the type check, so the decision
    is the approval with that task, not "invalid automation action" and not a reject over
    it. MUTATION: run the type check before the planned lookup in `_approve_locked`."""
    ws = workspaces
    hid = _with_action(ws, {"operation": "create", "prompt": "p", "frequency": "monthly",
                            "day": True}, claim={"decision": "approve", "automation_id": "abcd0123"})
    _write(store, AutomationTask(id="abcd0123", name="Stored", prompt="p", frequency="monthly",
                                 day=1, user_scope_id=TENANT_A))
    if decision == "approve":
        assert ws.tw.approve_handoff(TENANT_A, ws.task_a, hid) is True
    else:
        with pytest.raises(ws.tw.HandoffAlreadyApproved):
            ws.tw.reject_handoff(TENANT_A, ws.task_a, hid)
    record = _record(ws, hid)
    assert record["status"] == "approved" and "claim" not in record, record
    assert record["automation_action_result"] == {"ok": True, "operation": "create",
                                                  "task_id": "abcd0123"}


def test_a_create_that_stored_its_task_then_raised_is_approved(store, workspaces, monkeypatch):
    """The catch-all after the claim: the create stored the planned task and then raised an
    unforeseen error. The decision is the approval with that task, not "invalid automation
    action". MUTATION: answer every error there with "invalid automation action"."""
    ws = workspaces
    hid = _create_handoff(ws)
    before = _a_tasks(store)
    real = AutomationManager.create

    def create_then_raise(self, task):
        real(self, task)
        raise RuntimeError("after the save")

    monkeypatch.setattr(AutomationManager, "create", create_then_raise)
    assert ws.tw.approve_handoff(TENANT_A, ws.task_a, hid) is True
    made = _a_tasks(store) - before
    record = _record(ws, hid)
    assert len(made) == 1 and record["status"] == "approved", (made, record)
    assert record["automation_action_result"] == {"ok": True, "operation": "create",
                                                  "task_id": next(iter(made))}


@pytest.mark.parametrize("lock", ["filelock", "no_filelock"])
def test_a_workspace_setup_failure_is_not_saved_not_missing(store, workspaces, monkeypatch,
                                                             lock):
    """Setting up the workspace (a missing `archive` folder that cannot be made) failed
    before the lock's error handling and answered "not found" for a handoff that exists. The
    ids are now checked without the filesystem, and the setup fails inside the lock as "not
    saved". MUTATIONS: check the ids through `_handoff_paths` before the lock again;
    separately, drop the setup from the fallback branch."""
    import vaf.core.secure_store as secure_store

    ws = workspaces
    hid = _create_handoff(ws)
    if lock == "no_filelock":
        monkeypatch.setattr(secure_store, "_get_filelock_cls", lambda: None)

    def unwritable(scope):
        raise PermissionError(13, "Permission denied", str(ws.root / "x"))

    monkeypatch.setattr(ws.tw, "ensure_workspace", unwritable)
    for decide in (ws.tw.approve_handoff, ws.tw.reject_handoff):
        with pytest.raises(ws.tw.HandoffNotSaved):
            decide(TENANT_A, ws.task_a, hid)
    assert ws.tw.approve_handoff(TENANT_A, ws.task_a, "../x") is False


# ── one decision per account on the pool, enabled fails closed, small pins ──────

def test_a_locked_account_does_not_delay_another_account(store, workspaces, monkeypatch):
    """Two accounts whose decision locks are held elsewhere used to hold both workers of the
    decision pool for the whole lock wait, so a third account's free decision waited for
    them (measured 4.97 s). A waiting decision now gives its worker back between attempts:
    the third account's decision finishes while the other two are still waiting. MUTATION:
    let `_decide_handoff` wait for the lock inside the worker (one call, no `lock_timeout`)."""
    import asyncio

    import vaf.core.web_server as web_server

    ws = workspaces
    tw = ws.tw
    monkeypatch.setattr(tw, "_DECISION_LOCK_TIMEOUT_S", 1.0)
    hid_a = _create_handoff(ws)
    task_c = tw.create_task(ADMIN2, "C task", "test")["id"]
    hid_c = tw.create_handoff(ADMIN2, task_c, "C handoff", "c")["id"]
    entered = {TENANT_A: threading.Event(), TENANT_B: threading.Event()}
    real = tw.approve_handoff

    def approve(scope, *a, **kw):
        if scope in entered:
            entered[scope].set()
        return real(scope, *a, **kw)

    done = threading.Event()
    holding = []

    def hold(scope):
        with tw._decision_lock(scope):
            holding.append(scope)
            done.wait(20)

    holders = [threading.Thread(target=hold, args=(s,), daemon=True) for s in (TENANT_A, TENANT_B)]
    for h in holders:
        h.start()

    async def scenario():
        a = asyncio.ensure_future(web_server._decide_handoff(TENANT_A, approve, TENANT_A,
                                                             ws.task_a, hid_a))
        b = asyncio.ensure_future(web_server._decide_handoff(TENANT_B, approve, TENANT_B,
                                                             ws.task_b, ws.handoff_b))
        for _ in range(500):
            if all(e.is_set() for e in entered.values()):
                break
            await asyncio.sleep(0.01)
        c = await web_server._decide_handoff(ADMIN2, approve, ADMIN2, task_c, hid_c)
        before_them = not a.done() and not b.done()
        rest = await asyncio.gather(a, b, return_exceptions=True)
        return c, before_them, rest

    try:
        for _ in range(500):
            if len(holding) == 2:
                break
            threading.Event().wait(0.01)
        c, before_them, rest = asyncio.run(scenario())
    finally:
        done.set()
        for h in holders:
            h.join(10)
    assert c is True and before_them, (c, before_them, rest)
    assert all(isinstance(r, tw.HandoffBusy) for r in rest), rest


def test_one_account_flooding_decisions_does_not_delay_another(store, monkeypatch):
    """One account sending many decisions at once filled the pool's two workers and its
    queue, so another account waited behind all of them (measured 9.49 s for eight). Now an
    account has at most one decision queued or running: the others answer busy at once, and
    the other account's decision runs on the free worker. Also pins the pool itself: the
    decisions run on `vaf-handoff-decision` threads of a `DaemonPool`, not the loop's default
    executor. MUTATION: drop the per-account check in `_decide_handoff`."""
    import asyncio

    import vaf.core.thinking_workspace as tw
    import vaf.core.web_server as web_server
    from vaf.core.bounded_run import DaemonPool

    assert isinstance(web_server._handoff_decision_pool(), DaemonPool)
    release = threading.Event()
    ran_on = []

    def slow(*a, lock_timeout=None):
        ran_on.append(threading.current_thread().name)
        release.wait(5)
        return True

    def fast(*a, lock_timeout=None):
        ran_on.append(threading.current_thread().name)
        return True

    async def scenario():
        floods = [asyncio.ensure_future(web_server._decide_handoff(TENANT_A, slow))
                  for _ in range(8)]
        try:
            await asyncio.sleep(0.1)
            other = await asyncio.wait_for(web_server._decide_handoff(TENANT_B, fast), 3)
        finally:
            release.set()
        return other, await asyncio.gather(*floods, return_exceptions=True)

    other, floods = asyncio.run(scenario())
    assert other is True
    assert floods.count(True) == 1, floods
    assert sum(isinstance(f, tw.HandoffBusy) for f in floods) == 7, floods
    assert ran_on and all(n.startswith("vaf-handoff-decision") for n in ran_on), ran_on


def test_enabled_fails_closed_and_the_proposal_must_say_it(store, workspaces):
    """An `enabled` text the coercion did not know left the automation running. It now
    reads as off, after folding (a full-width "false" and one with a zero-width space are
    "false"), and a proposal with a word that says neither is refused where it is made.
    MUTATIONS: let an unknown text read as on; separately, drop the `enabled` check in
    `_action_type_error`."""
    from vaf.tools.thinking_workspace_handoff import ThinkingWorkspaceHandoffTool

    ws = workspaces
    assert auto_mod.read_enabled("maybe") is None
    assert AutomationTask(enabled="maybe").enabled is False
    refused = ThinkingWorkspaceHandoffTool().run(
        task_id=ws.task_a, title="t", content="c", user_scope_id=TENANT_A,
        automation_action={"operation": "update", "task_id": A_ID, "enabled": "maybe"})
    assert refused.startswith("Error") and "'enabled'" in refused, refused
    hid = ws.tw.create_handoff(TENANT_A, ws.task_a, "Off", "c", automation_action={
        "operation": "update", "task_id": A_ID, "enabled": "\uff46\uff41\uff4c\uff53\uff45"})["id"]
    assert ws.tw.approve_handoff(TENANT_A, ws.task_a, hid) is True
    assert json.loads(_path(store, A_ID).read_text(encoding="utf-8"))["enabled"] is False


def test_a_deeply_nested_planned_task_keeps_the_claim(store, workspaces, monkeypatch):
    """A planned task file nested too deeply for the JSON parser raised `RecursionError`,
    which the strict read did not catch: the WebSocket answered with the raw error text. It
    is unreadable like any other and answers "not saved". MUTATION: catch only `OSError` and
    `ValueError` in `_planned_task_exists`."""
    ws = workspaces
    hid = _create_handoff(ws)
    planned = _crash_after_create(ws, monkeypatch, hid)
    (store / TENANT_A / f"{planned}.json").write_text("[" * 200000, encoding="utf-8")
    with pytest.raises(ws.tw.HandoffNotSaved):
        ws.tw.reject_handoff(TENANT_A, ws.task_a, hid)
    assert _record(ws, hid)["claim"]["automation_id"] == planned


def test_a_german_weekday_is_read_where_every_task_is_built(store, monkeypatch):
    """The German weekday map moved into the shared coercion; a task built with " Freitag "
    (also by the agent's create tool) runs on Fridays, not on Monday, the fallback of an
    unknown name. MUTATION: drop the weekday line of `AutomationTask.__post_init__`."""
    from vaf.tools.automation import AutomationTool

    assert AutomationTask(frequency="weekly", weekday=" Freitag ").weekday == "friday"
    monkeypatch.setattr(auto_mod, "ensure_scheduler_started", lambda **kw: (None, False))
    before = _a_tasks(store)
    out = AutomationTool().run(name="Fridays", prompt="weekly on fridays", frequency="weekly",
                               weekday="Freitag", time="13:30", user_scope_id=TENANT_A,
                               user_role="user")
    assert "Created" in out, out
    made = _a_tasks(store) - before
    assert len(made) == 1, made
    stored = json.loads((store / TENANT_A / f"{next(iter(made))}.json").read_text(encoding="utf-8"))
    assert stored["weekday"] == "friday", stored


def test_a_next_run_that_cannot_be_read_is_logged_by_id(monkeypatch):
    """`calculate_next_run` answers None for a field it cannot use; that is now logged with
    the task id and the error's name only, so a task that is never armed can be found.
    MUTATION: drop the log line."""
    lines = []
    monkeypatch.setattr(auto_mod, "append_domain_log", lambda domain, text: lines.append(text))
    task = AutomationTask(id="abcd0123", frequency="monthly", day=5, time="08:00",
                          prompt="secret prompt")
    task.day = "x"
    assert task.calculate_next_run() is None
    assert len(lines) == 1 and "abcd0123" in lines[0] and "TypeError" in lines[0], lines
    assert "secret" not in lines[0]


def test_a_long_enabled_text_is_never_folded(monkeypatch):
    """Folding works character by character, and an `enabled` of 1 MB held the event loop of
    the WebSocket update for 1.32 s (10 MB: 12.85 s). Text longer than any word it could be
    is not folded and reads as off; a short one still is folded. Counted, not timed, so a
    slow runner cannot flake it. MUTATION: drop the length bound in `read_enabled`."""
    import vaf.core.agent_origin as agent_origin

    calls = []
    real = agent_origin.folded_text

    def counting(text):
        calls.append(len(text))
        return real(text)

    monkeypatch.setattr(agent_origin, "folded_text", counting)
    assert auto_mod.read_enabled("x" * 10**7) is None
    assert auto_mod.coerce_schedule_fields({"enabled": "false" * 10**6}) == {"enabled": False}
    assert AutomationTask(enabled="y" * 10**6).enabled is False
    assert calls == [], calls[:3]
    assert auto_mod.read_enabled("\uff46\uff41\uff4c\uff53\uff45") is False
    assert calls == [5]


def test_nan_and_infinity_are_neither_on_nor_off(store, workspaces):
    """`bool(nan)` is True, so NaN and infinity read as on. They say neither: off when a
    task is built, refused where a proposal is made. MUTATION: drop the finiteness check
    in `read_enabled`."""
    ws = workspaces
    for value in (float("nan"), float("inf"), float("-inf")):
        assert auto_mod.read_enabled(value) is None
        assert AutomationTask(enabled=value).enabled is False
        with pytest.raises(ValueError):
            ws.tw.create_handoff(TENANT_A, ws.task_a, "t", "c", automation_action={
                "operation": "update", "task_id": A_ID, "enabled": value})


def test_a_waiting_decision_retries_every_50_ms(store, monkeypatch):
    """A decision whose lock is held elsewhere tries again every 50 ms on the event loop;
    without the pause it would spin, taking a pool worker and the loop for every attempt.
    About 20 attempts in a one-second wait. MUTATION: drop the `asyncio.sleep` between
    attempts in `_decide_handoff`."""
    import asyncio

    import vaf.core.thinking_workspace as tw
    import vaf.core.web_server as web_server

    monkeypatch.setattr(tw, "_DECISION_LOCK_TIMEOUT_S", 1.0)
    attempts = []

    def always_busy(*a, lock_timeout=None):
        attempts.append(lock_timeout)
        raise tw.HandoffBusy("handoff is being decided")

    with pytest.raises(tw.HandoffBusy):
        asyncio.run(web_server._decide_handoff(TENANT_A, always_busy))
    assert 2 <= len(attempts) <= 25, len(attempts)
    assert set(attempts) == {0}


def test_the_machine_owners_aliases_are_one_account(store, monkeypatch):
    """No scope, "default" and the machine owner's scope share one workspace and one lock,
    so they are one account for the one-decision rule: while a decision for None runs, one
    for the owner's scope answers busy at once. MUTATION: key `_decide_handoff` by the raw
    scope instead of `_scope_key`."""
    import asyncio

    import vaf.core.thinking_workspace as tw
    import vaf.core.web_server as web_server

    running, release = threading.Event(), threading.Event()

    def slow(*a, lock_timeout=None):
        running.set()
        release.wait(5)
        return True

    def fast(*a, lock_timeout=None):
        return True

    async def scenario():
        first = asyncio.ensure_future(web_server._decide_handoff(None, slow))
        try:
            for _ in range(500):
                if running.is_set():
                    break
                await asyncio.sleep(0.01)
            outcomes = []
            for alias in (LOCAL, "default"):
                try:
                    outcomes.append(await asyncio.wait_for(
                        web_server._decide_handoff(alias, fast), 3))
                except tw.HandoffBusy as busy:
                    outcomes.append(busy)
        finally:
            release.set()
        return outcomes, await first

    outcomes, first = asyncio.run(scenario())
    assert first is True
    assert all(isinstance(o, tw.HandoffBusy) for o in outcomes), outcomes

