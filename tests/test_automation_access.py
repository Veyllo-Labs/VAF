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
    owner's scope is pinned to LOCAL; nothing reaches the thinking workspace."""
    import vaf.core.config as config_mod
    import vaf.core.thinking_workspace as tw

    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(config_mod, "get_local_admin_scope_id", lambda: LOCAL)
    monkeypatch.setattr(tw, "sync_automation_status_to_workspace", lambda **kw: None)
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

def _ws(monkeypatch, *, role, scope, frames):
    """Send `frames` ([(frame, reply_type), ...]) over one real `/ws` connection opened with a
    signed token; the replies in order. `role=None` signs no role claim (the machine owner's
    tokenless identity carries none)."""
    import jwt
    from starlette.testclient import TestClient

    import vaf.auth.crypto as crypto
    from vaf.core.web_server import app

    monkeypatch.setattr(crypto, "get_jwt_secret", lambda: "s" * 32)
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
                        if msg.get("type") == want:
                            box["replies"].append(msg)
                            break
            except Exception as e:  # noqa: BLE001 - reported by the assert below
                box["error"] = e

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
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
    the caller sees.

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
        if not run_found or not _calls(run.body, "AutomationAccess"):
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
        return AutomationManager().list()

class CallbackOnlyTool(BaseTool):
    name = "callback_automation"
    category = "automations"
    def run(self, **kwargs):
        def never_called():
            return AutomationAccess(kwargs.get("user_scope_id")).list()
        check = lambda: AutomationAccess(None)
        return AutomationManager().list()

class GuardedBase(BaseTool):
    category = "automations"
    def run(self, **kwargs):
        return AutomationAccess(kwargs.get("user_scope_id")).list()

class Left(GuardedBase):
    name = "left_automation"

class Right(GuardedBase):
    name = "right_automation"
    def run(self, **kwargs):
        return AutomationManager().list()

class Diamond(Left, Right):
    name = "diamond_automation"
'''
    assert sorted(_tool_violations(synthetic)[0]) == [
        "AnnotatedTool", "CallbackOnlyTool", "Diamond", "MentionOnlyTool", "PauseAutomationTool",
        "RawTrashTool", "Right"]


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
    monkeypatch.setattr(un, "append_notification", lambda *a, **kw: None)
    task_a = tw.create_task(TENANT_A, "A task", "test")["id"]
    task_b = tw.create_task(TENANT_B, "B task", "test")["id"]
    handoff_b = tw.create_handoff(TENANT_B, task_b, "B handoff", "content")["id"]
    return SimpleNamespace(root=data / "workspaces", task_a=task_a, task_b=task_b,
                           handoff_b=handoff_b, tw=tw)


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
