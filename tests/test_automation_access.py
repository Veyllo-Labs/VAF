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
    assert access.sees(_T(TENANT_A)) and access.sees(_T(f" {TENANT_A} "))
    for other in (None, "", LOCAL, ADMIN2, TENANT_B, "  "):
        assert not access.sees(_T(other)), other


def test_admin_sees_root_own_and_local_only(store):
    access = AutomationAccess(ADMIN2, "admin")
    for seen in (None, "", "  ", ADMIN2, LOCAL):
        assert access.sees(_T(seen)), seen
    for other in (TENANT_A, TENANT_B):
        assert not access.sees(_T(other)), other


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
    return misses


def test_access_fuzz_invariants(store):
    """Seeded: three invariants of the classifier. F440_FUZZ_DRAWS raises the count outside
    the suite. MUTATION: `is_admin = not self.own`."""
    draws = int(os.environ.get("F440_FUZZ_DRAWS", "2000"))
    assert _fuzz(draws) == []


# ── AC8, AC9: the WebSocket branches ask the rule ─────────────────────────────

_PLANNER = ("automation_note", "automation_todo")


def _ws_branch_violations(source: str) -> tuple:
    """(violations, branch literals) for every `type == "..."` branch of
    `websocket_endpoint` whose literal names an automation."""
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "websocket_endpoint")
    bad, seen = [], []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        left, comps = node.test.left, node.test.comparators
        if not (isinstance(left, ast.Name) and left.id == "type" and len(comps) == 1
                and isinstance(comps[0], ast.Constant) and isinstance(comps[0].value, str)):
            continue
        literal = comps[0].value
        if "automation" not in literal:
            continue
        seen.append(literal)
        names = set()
        for stmt in node.body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
                elif isinstance(sub, ast.ImportFrom):
                    names.update(a.asname or a.name for a in sub.names)
        if any(p in literal for p in _PLANNER):
            if names & {"AutomationManager", "AutomationAccess"}:
                bad.append(f"{literal}: a planner command touches the task store")
        elif literal == "create_automation":
            if "AutomationAccess" in names:
                bad.append(f"{literal}: unexpected")
        elif "AutomationAccess" not in names or "AutomationManager" in names:
            bad.append(f"{literal}: does not go through AutomationAccess")
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "AutomationManager"
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
    """The guard itself: a new automation command reaching the global store fails it."""
    synthetic = '''
async def websocket_endpoint(websocket, token=None):
    while True:
        if type == "get_automations":
            tasks = AutomationAccess(scope, role).list()
        elif type == "pause_automation":
            mgr = AutomationManager()
            mgr.get(cmd["id"])
        elif type == "get_automation_notes":
            notes = list_notes(scope)
'''
    bad, seen = _ws_branch_violations(synthetic)
    assert any("pause_automation" in b for b in bad), bad
    assert any("without user_scope_id" in b for b in bad), bad
    assert not any("get_automations" in b or "notes" in b for b in bad), bad


# ── AC10: the agent tools ask the rule ────────────────────────────────────────

def _tool_violations(source: str) -> tuple:
    tree = ast.parse(source)
    bad, names = [], []
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        if not any(isinstance(b, ast.Name) and b.id == "BaseTool" for b in cls.bases):
            continue
        attrs = {}
        for stmt in cls.body:
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
                for t in stmt.targets:
                    if isinstance(t, ast.Name):
                        attrs[t.id] = stmt.value.value
        if attrs.get("category") != "automations":
            continue
        name = attrs.get("name")
        names.append(name)
        if name == "create_automation":
            continue
        run = next((s for s in cls.body if isinstance(s, ast.FunctionDef) and s.name == "run"), None)
        refs = {n.id for n in ast.walk(run) if isinstance(n, ast.Name)} if run else set()
        if "AutomationAccess" not in refs:
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
class PauseAutomationTool(BaseTool):
    name = "pause_automation"
    category = "automations"
    def run(self, **kwargs):
        manager, _ = _manager_for_scope(kwargs.get("user_scope_id"))
        return manager.get(kwargs["task_id"])
'''
    assert _tool_violations(synthetic)[0] == ["pause_automation"]


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
