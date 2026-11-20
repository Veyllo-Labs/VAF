# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One live Node per WhatsApp login folder, and a link flow that is its account's own.

THE DEFECT, measured on the code before this change with a fake Popen: two calls of
`spawn_node_bridge` on one folder ran two live Nodes on one login (two Baileys sockets on one
credential set evict each other). `/qr/start` spawned a link Node next to the account's running
process; `/qr/reset` deleted the login under it; the link state was keyed by the account NAME,
so a new account under a deleted account's name read that account's flow; a flow replaced
while it installed the bridge's dependencies still spawned; and `connected` was reported while
the link Node still ran.

Fake processes, a scratch APP_DIR, signed-in requests through the real router. No Node.
"""
import io
import json
import os
import random
import subprocess
import sys
import threading
import time

import pytest

from vaf.api import whatsapp_bridge as wa
from vaf.api import whatsapp_routes as routes
from vaf.core.config import Config
from vaf.core.platform import Platform

ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000aa"
ALICE_SCOPE = "ab12cd34-0000-4000-8000-000000000001"
BOB_SCOPE = "ab12cd34-0000-4000-8000-000000000002"
ALICE = {"username": "alice", "role": "user", "user_scope_id": ALICE_SCOPE}
ADMIN = {"username": "admin", "role": "admin", "user_scope_id": ADMIN_SCOPE}
needs_links = pytest.mark.skipif(sys.platform == "win32", reason="a folder link needs a privilege on Windows")


class FakeNode:
    """Stands in for `subprocess.Popen` inside `spawn_node_bridge`: alive until ended."""

    def __init__(self, args, survive=False, **_kw):
        self.args = args
        self.auth_dir = args[args.index("--auth-dir") + 1] if "--auth-dir" in args else ""
        self.returncode = None
        self.pid = 4242
        self.survive = survive
        self.stdin = io.StringIO()
        self.stdout, self.stderr = [], []
        self.seen_folder = None

    def poll(self):
        return self.returncode

    def terminate(self):
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
    monkeypatch.setattr(wa, "_processes", {})
    monkeypatch.setattr(wa, "_scope_by_user", {})
    monkeypatch.setattr(wa, "_pending_starts", {})
    monkeypatch.setattr(wa, "_spawned_nodes", [])
    monkeypatch.setattr(wa, "TERMINATE_WAIT", 0.05)
    for name in ("_qr_state", "_qr_procs", "_qr_flows"):
        monkeypatch.setattr(routes, name, {})
    from vaf.core import node_runtime
    monkeypatch.setattr(node_runtime, "child_env", lambda node: {})
    monkeypatch.setattr(subprocess, "Popen", FakeNode)
    import vaf.api.auth_routes as auth_routes
    monkeypatch.setattr(auth_routes, "qr_png_base64", lambda value: "PNG:" + value)
    return state


def _folder(name):
    d = Config.APP_DIR / "users" / name / "whatsapp"
    d.mkdir(parents=True, exist_ok=True)
    (d / "creds.json").write_text("{}", encoding="utf-8")
    return d


def _spawn(folder):
    return wa.spawn_node_bridge("node", Config.APP_DIR / "wa-bridge.js", folder)


def _client(user):
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    app = FastAPI()

    @app.middleware("http")
    async def signed_in(request, call_next):
        request.state.user = dict(user)
        return await call_next(request)
    app.include_router(routes.router)
    return TestClient(app)


# -- AC8: one Node per login --------------------------------------------------------------------

@pytest.mark.parametrize("second", ["same path", pytest.param("through a link", marks=needs_links)])
def test_a_second_node_on_one_login_is_refused(world, tmp_path, second):
    """MUTATION: remove the check in `spawn_node_bridge` (both red); compare the paths only,
    without the folder's identity (the link row red)."""
    folder = _folder("alice")
    first = _spawn(folder)
    other = folder
    if second == "through a link":
        other = tmp_path / "another-name-for-it"
        os.symlink(folder, other, target_is_directory=True)
    with pytest.raises(wa.LoginInUse):
        _spawn(other)
    assert wa._spawned_nodes == [first]
    assert wa.login_in_use(folder) and wa.login_in_use(other)
    assert not wa.login_in_use(_folder("bob")), "another account's folder is free"
    first.returncode = 0
    assert not wa.login_in_use(folder)
    assert _spawn(other) is not first, "an ended Node frees its login"


def test_qr_start_refuses_a_running_login(world, monkeypatch):
    """The account's own connection runs on the folder: no link Node next to it. MUTATION: drop
    the `login_in_use` check in `/qr/start` - red."""
    _spawn(_folder("alice"))
    started = []
    monkeypatch.setattr(routes, "_run_qr_login", lambda *a: started.append(a))
    client = _client(ALICE)
    res = client.post("/api/whatsapp/qr/start")
    assert res.status_code == 409 and res.json()["detail"] == routes.LOGIN_IN_USE, res.text
    assert client.get("/api/whatsapp/qr").json() == {"status": "error", "error": routes.LOGIN_IN_USE}
    time.sleep(0.05)
    assert started == []


def test_an_account_start_waits_for_a_link_node(world, monkeypatch):
    """A link Node (registered for nobody) runs on Alice's login; her start answers
    PREVIOUS_NOT_ENDED and spawns nothing. MUTATION: drop the `login_in_use` clause of
    `_blocked_by_previous` - red."""
    folder = _folder("alice")
    _spawn(folder)
    monkeypatch.setattr(wa, "_bridge_stop", threading.Event())
    monkeypatch.setattr(wa, "_spawns_open", True)
    monkeypatch.setattr(wa, "_node_path", lambda: "node")
    monkeypatch.setattr(wa, "_wa_bridge_path", lambda: Config.APP_DIR / "users")   # exists
    assert wa._blocked_by_previous("alice", ALICE_SCOPE) is True
    assert wa._start_into_running((ALICE_SCOPE, "alice", folder)) == (False, wa.PREVIOUS_NOT_ENDED)
    assert len(wa._spawned_nodes) == 1 and "alice" not in wa._processes


# -- AC9: the link flow is the account's --------------------------------------------------------

class QRNode(FakeNode):
    def __init__(self, lines):
        super().__init__(["node", "wa-bridge.js"])
        self.stdout = [json.dumps(line) + "\n" for line in lines]


def _run_flow(monkeypatch, name, scope, lines):
    node = QRNode(lines)
    monkeypatch.setattr(wa, "spawn_node_bridge", lambda *a, **k: node)
    monkeypatch.setattr(wa, "ensure_bridge_deps", lambda *a, **k: (True, ""))
    from vaf.core import node_runtime
    monkeypatch.setattr(node_runtime, "find", lambda name: "node")
    flow = object()
    routes._qr_flows[wa._scope_key(scope)] = flow
    routes._run_qr_login(name, scope, flow)
    return node


def test_link_state_is_per_scope(world, monkeypatch):
    """Two sign-ins under one name ("alice", a deleted account's name given to a new one):
    each polls its own flow. MUTATION: key the state by name - red."""
    _run_flow(monkeypatch, "alice", ALICE_SCOPE, [{"type": "qr", "qr": "CODE-A"}])
    mine = _client(ALICE).get("/api/whatsapp/qr").json()
    assert mine == {"status": "qr", "qr_png": "PNG:CODE-A"}
    namesake = {"username": "alice", "role": "user", "user_scope_id": BOB_SCOPE}
    theirs = _client(namesake).get("/api/whatsapp/qr").json()
    assert theirs["status"] == "waiting", theirs


def test_a_replaced_flow_spawns_nothing(world, monkeypatch):
    """The first flow waits inside the dependency install until a second `/qr/start` replaced
    it; released, it spawns nothing and writes nothing. MUTATION: drop the currency check before
    the spawn - red."""
    from vaf.core import node_runtime
    monkeypatch.setattr(node_runtime, "find", lambda name: "node")
    gate, entered = threading.Event(), threading.Event()
    installs = []

    def deps(*a, **k):
        installs.append(1)
        if len(installs) == 1:
            entered.set()
            gate.wait(10)
        return True, ""
    monkeypatch.setattr(wa, "ensure_bridge_deps", deps)
    spawned = []

    def spawn(*a, **k):
        node = QRNode([{"type": "qr", "qr": f"CODE-{len(spawned) + 1}"}])
        node.poll = lambda: None
        spawned.append(node)
        return node
    monkeypatch.setattr(wa, "spawn_node_bridge", spawn)
    threads = []
    real_thread = threading.Thread

    class Recorded(real_thread):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            threads.append(self)
    monkeypatch.setattr(routes.threading, "Thread", Recorded)
    client = _client(ALICE)
    assert client.post("/api/whatsapp/qr/start").status_code == 200
    assert entered.wait(5)
    assert client.post("/api/whatsapp/qr/start").status_code == 200
    deadline = time.monotonic() + 5
    while not spawned and time.monotonic() < deadline:
        time.sleep(0.01)
    gate.set()
    for t in list(threads):
        t.join(5)
    assert len(spawned) == 1, "the replaced flow spawned a Node"
    assert client.get("/api/whatsapp/qr").json() == {"status": "qr", "qr_png": "PNG:CODE-1"}
    assert routes._qr_procs.get(ALICE_SCOPE) in (spawned[0], None)


def test_connected_comes_after_the_link_node_ended(world, monkeypatch):
    """The wizard's next step starts the account's process on this login. MUTATION: write
    `connected` before the wait - red."""
    seen = []

    def ended(proc):
        seen.append(dict(routes._qr_state.get(ALICE_SCOPE, {})))
        proc.returncode = 0
        return True
    monkeypatch.setattr(wa, "_terminate", ended)
    node = _run_flow(monkeypatch, "alice", ALICE_SCOPE,
                     [{"type": "connected", "selfJid": "491700000099:7@s.whatsapp.net"}])
    assert seen and not seen[0].get("connected"), seen
    assert node.returncode == 0
    assert _client(ALICE).get("/api/whatsapp/qr").json()["status"] == "connected"


# -- AC10: Re-link stops the caller's own process first -----------------------------------------

def _account_node(name, scope, folder, survive=False):
    """The account's process as the bridge spawns and registers it."""
    node = _spawn(folder)
    node.survive = survive
    wa._processes[name] = node
    wa._scope_by_user[name] = scope
    return node


def test_reset_stops_the_own_process_first(world):
    """MUTATION: no `stop_account` call - red."""
    folder = _folder("alice")
    mine = _account_node("alice", ALICE_SCOPE, folder)
    admins = _account_node("admin", ADMIN_SCOPE, _folder("admin"))
    stopped_with_the_folder = []
    real_terminate = mine.terminate
    mine.terminate = lambda: (stopped_with_the_folder.append(folder.exists()), real_terminate())
    res = _client(ALICE).post("/api/whatsapp/qr/reset")
    assert res.status_code == 200, res.text
    assert stopped_with_the_folder == [True], "the process ended after its login was removed"
    assert mine.returncode is not None and not folder.exists()
    assert admins.returncode is None and wa._processes["admin"] is admins


@pytest.mark.parametrize("case", ["survivor", "other scope"])
def test_reset_removes_nothing_while_a_node_runs(world, case):
    """MUTATION: remove the login before the checks - red."""
    folder = _folder("alice")
    if case == "survivor":
        _account_node("alice", ALICE_SCOPE, folder, survive=True)
        expected = (500, wa.STOP_FAILED)
    else:
        other = _account_node("alice", BOB_SCOPE, folder)
        expected = (409, wa.PREVIOUS_NOT_ENDED)
    res = _client(ALICE).post("/api/whatsapp/qr/reset")
    assert (res.status_code, res.json()["detail"]) == expected, res.text
    assert (folder / "creds.json").is_file()
    if case == "other scope":
        assert other.returncode is None, "another account's process was stopped"


# -- fuzz: one Node per folder under concurrency --------------------------------------------------

class _FuzzNode(FakeNode):
    lock = threading.Lock()
    seq = 0
    nodes: list = []
    violations: list = []
    rng = random.Random(0)

    def __init__(self, args, **kw):
        super().__init__(args, **kw)
        st = os.stat(self.auth_dir)
        self.ident = (st.st_dev, st.st_ino)
        cls = _FuzzNode
        with cls.lock:
            cls.seq += 1
            self.born, self.died = cls.seq, None
            if any(n.ident == self.ident and n.died is None for n in cls.nodes):
                cls.violations.append(("two live", self.auth_dir))
            cls.nodes.append(self)
        time.sleep(cls.rng.random() * 0.002)

    def poll(self):
        cls = _FuzzNode
        with cls.lock:
            if self.died is None and cls.rng.random() < 0.3:
                cls.seq += 1
                self.died = cls.seq
            return None if self.died is None else 0


def test_fuzz_never_two_nodes_on_one_login(world, monkeypatch, tmp_path):
    """2 to 4 threads spawn on three folders, one also reached through a link; the fake Node
    sleeps 0 to 2 ms in its constructor and dies at random when polled. Oracle, checked in the
    fake under its own lock: never two live Nodes on one folder identity, and every refusal
    overlapped a live Node on that folder. `VAF_FUZZ_DRAWS` raises the round count."""
    rounds = int(os.environ.get("VAF_FUZZ_DRAWS", "200"))
    monkeypatch.setattr(subprocess, "Popen", _FuzzNode)
    folders = [_folder(n) for n in ("alice", "bob", "carol")]
    if sys.platform != "win32":
        link = tmp_path / "alice-again"
        os.symlink(folders[0], link, target_is_directory=True)
        folders.append(link)
    rng = random.Random(453_002)
    _FuzzNode.rng = random.Random(453_003)
    _FuzzNode.nodes, _FuzzNode.violations, _FuzzNode.seq = [], [], 0
    refusals = []

    def worker(picks):
        for folder in picks:
            ident = os.stat(folder)
            ident = (ident.st_dev, ident.st_ino)
            with _FuzzNode.lock:
                before = _FuzzNode.seq
            try:
                _spawn(folder)
            except wa.LoginInUse:
                with _FuzzNode.lock:
                    after = _FuzzNode.seq
                    overlapped = any(n.ident == ident and n.born <= after and (n.died is None or n.died > before)
                                     for n in _FuzzNode.nodes)
                if not overlapped:
                    refusals.append(str(folder))
            time.sleep(rng.random() * 0.002)

    spawned = 0
    for _ in range(rounds):
        # Every Node of the last round ends; the oracle then looks at this round's only, so a
        # long run stays linear in its rounds.
        with _FuzzNode.lock:
            spawned += len(_FuzzNode.nodes)
            _FuzzNode.nodes = []
        wa._spawned_nodes.clear()
        plans = [[rng.choice(folders) for _ in range(3)] for _ in range(rng.randint(2, 4))]
        threads = [threading.Thread(target=worker, args=(plan,)) for plan in plans]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    assert _FuzzNode.violations == [], _FuzzNode.violations[:5]
    assert refusals == [], refusals[:5]
    spawned += len(_FuzzNode.nodes)
    assert spawned > rounds, "the fuzz spawned almost nothing; it is broken"


# -- review round: the link flow under failure and concurrency ---------------------------------

def test_connected_is_not_reported_while_the_link_node_survives(world, monkeypatch):
    """The link Node survives its terminate and kill: the poll says so, and the handle stays
    for the next start or reset. MUTATION: publish `connected` whatever the terminate answered
    - red."""
    monkeypatch.setattr(wa, "_terminate", lambda proc: False)
    node = _run_flow(monkeypatch, "alice", ALICE_SCOPE,
                     [{"type": "connected", "selfJid": "491700000099:7@s.whatsapp.net"}])
    assert _client(ALICE).get("/api/whatsapp/qr").json() == {"status": "error", "error": routes.LINK_NODE_SURVIVED}
    assert routes._qr_procs.get(ALICE_SCOPE) is node


def test_an_ended_flow_keeps_a_surviving_nodes_handle(world):
    """The token goes at once; the handle only once the Node's exit is confirmed, so the next
    try ends it. MUTATION: drop the handle before the terminate - red."""
    node = FakeNode(["node"], survive=True)
    routes._qr_flows[ALICE_SCOPE] = object()
    routes._qr_procs[ALICE_SCOPE] = node
    assert routes._end_link_flow(ALICE_SCOPE) is False
    assert ALICE_SCOPE not in routes._qr_flows and routes._qr_procs.get(ALICE_SCOPE) is node
    node.survive = False
    assert routes._end_link_flow(ALICE_SCOPE) is True
    assert node.returncode is not None and ALICE_SCOPE not in routes._qr_procs


def test_two_starts_at_once_leave_no_orphan(world, monkeypatch):
    """Both starts end the old flow before either installs its token; the first flow's Node is
    up by the time the second looks at the login. The second must end it, not report the login
    in use and leave it running flowless. MUTATION: drop the account's `_flow_lock` from
    `_begin_link_flow` - red."""
    import vaf.core.node_runtime as node_runtime
    monkeypatch.setattr(node_runtime, "find", lambda name: "node")
    monkeypatch.setattr(wa, "ensure_bridge_deps", lambda *a, **k: (True, ""))
    _folder("alice")
    spawned = []

    def spawn(node, wa_js, auth_dir):
        proc = QRNode([{"type": "qr", "qr": f"CODE-{len(spawned) + 1}"}])
        proc.poll = lambda p=proc: p.returncode
        proc.stdout = iter(proc.stdout)      # the Node keeps running after its code
        proc._vaf_login = wa._login_identity(auth_dir)
        with wa._spawned_nodes_lock:
            wa._spawned_nodes.append(proc)
        spawned.append(proc)
        return proc
    monkeypatch.setattr(wa, "spawn_node_bridge", spawn)
    real_end, real_in_use = routes._end_link_flow, wa.login_in_use
    ends = []

    def end(scope):
        # Each start finishes ending the old flow, then waits for the other to have done the
        # same if it can (it cannot while the first holds the account's lock).
        result = real_end(scope)
        ends.append(1)
        deadline = time.monotonic() + 0.3
        while len(ends) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        return result
    looks = []

    def in_use(folder):
        looks.append(1)
        if len(looks) == 2:     # the second start looks once the first flow's Node is up
            deadline = time.monotonic() + 1.0
            while not spawned and time.monotonic() < deadline:
                time.sleep(0.005)
            time.sleep(0.05)
        return real_in_use(folder)
    monkeypatch.setattr(routes, "_end_link_flow", end)
    monkeypatch.setattr(wa, "login_in_use", in_use)
    threads = [threading.Thread(target=routes._begin_link_flow, args=("alice", ALICE_SCOPE)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and _client(ALICE).get("/api/whatsapp/qr").json().get("status") != "qr":
        time.sleep(0.02)
    assert _client(ALICE).get("/api/whatsapp/qr").json()["status"] == "qr"
    live = [p for p in spawned if p.returncode is None]
    assert live == [routes._qr_procs.get(ALICE_SCOPE)], "a link Node runs that no flow owns"


def test_a_link_of_a_revoked_account_spawns_nothing(world, monkeypatch):
    """The flow passed `/qr/start` before the account was deactivated; it reaches its spawn
    after. MUTATION: drop the `_account_may_start` check before the link spawn - red."""
    from vaf.core import revocation
    revocation.revoke_account(ALICE_SCOPE)
    try:
        spawned = []
        monkeypatch.setattr(wa, "spawn_node_bridge", lambda *a, **k: spawned.append(a) or QRNode([]))
        monkeypatch.setattr(wa, "ensure_bridge_deps", lambda *a, **k: (True, ""))
        from vaf.core import node_runtime
        monkeypatch.setattr(node_runtime, "find", lambda name: "node")
        flow = object()
        routes._qr_flows[ALICE_SCOPE] = flow
        routes._run_qr_login("alice", ALICE_SCOPE, flow)
        assert spawned == [] and ALICE_SCOPE not in routes._qr_procs
        # A later poll reads an end, not "waiting" for a worker that has returned.
        assert routes._qr_state[ALICE_SCOPE]["error"] == routes.LINK_ACCOUNT_ENDED
    finally:
        revocation.restore_account(ALICE_SCOPE)


# -- review round: one scope key, and lanes that cannot forget their scope ---------------------

def test_the_routes_and_the_bridge_share_one_scope_key():
    assert routes._scope_key is wa._scope_key
    assert not hasattr(routes, "_flow_key")
    assert wa._scope_key(" AB12CD34-0000 ") == "ab12cd34-0000" and wa._scope_key(None) == ""


@pytest.mark.parametrize("lane", ["get_whatsapp_chats", "sync_whatsapp_chats", "get_lid_mappings",
                                  "get_contact_names", "get_avatar", "get_connection_status"])
def test_the_lane_wrappers_require_a_scope(lane):
    """A caller that forgets the scope fails loudly instead of reading nothing or a cache.
    MUTATION: give `user_scope_id` a default again - red."""
    import inspect
    param = inspect.signature(getattr(wa, lane)).parameters["user_scope_id"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY and param.default is inspect.Parameter.empty



# -- review round 2 ---------------------------------------------------------------------------

def _link_world(monkeypatch, rows):
    from vaf.core import node_runtime, tool_dispatch
    monkeypatch.setattr(node_runtime, "find", lambda name: "node")
    monkeypatch.setattr(wa, "ensure_bridge_deps", lambda *a, **k: (True, ""))
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver", rows)
    spawned = []
    monkeypatch.setattr(wa, "spawn_node_bridge", lambda *a, **k: spawned.append(a) or QRNode([]))
    return spawned


def test_a_link_of_an_account_the_directory_drops_spawns_nothing(world, monkeypatch):
    """Deleted in another process: no mark here, only the directory knows. MUTATION: the link
    spawn asks the in-memory mark only - red."""
    spawned = _link_world(monkeypatch, lambda: [{"username": "bob", "user_scope_id": BOB_SCOPE, "active": True}])
    flow = object()
    routes._qr_flows[ALICE_SCOPE] = flow
    routes._run_qr_login("alice", ALICE_SCOPE, flow)
    assert spawned == [] and routes._qr_state[ALICE_SCOPE]["error"] == routes.LINK_ACCOUNT_ENDED


def test_the_store_is_not_read_under_the_qr_lock(world, monkeypatch):
    """`/qr` takes `_qr_lock` on the event loop; a store read under it holds every account's
    poll. MUTATION: ask `account_stands` inside the lock again - red."""
    held = []

    def directory():
        held.append(routes._qr_lock.locked())
        return [{"username": "alice", "user_scope_id": ALICE_SCOPE, "active": True}]
    spawned = _link_world(monkeypatch, directory)
    flow = object()
    routes._qr_flows[ALICE_SCOPE] = flow
    routes._run_qr_login("alice", ALICE_SCOPE, flow)
    assert held and not any(held), held
    assert len(spawned) == 1


@pytest.mark.parametrize("route", ["/api/whatsapp/qr/reset", "/api/whatsapp/qr/start"])
def test_a_busy_account_transition_answers_at_once(world, monkeypatch, route):
    """Another start or reset of the account still holds its lock (ending a link Node that
    resists): this one answers FLOW_BUSY and touches nothing. Also pins that a reset takes the
    lock at all. MUTATION: `/qr/reset` without the lock (the reset row red); wait for the lock
    without a bound (both rows red by the elapsed-time check)."""
    folder = _folder("alice")
    monkeypatch.setattr(routes, "FLOW_LOCK_WAIT", 0.05)
    started = []
    monkeypatch.setattr(routes, "_run_qr_login", lambda *a: started.append(a))
    lock = routes._flow_lock(ALICE_SCOPE)
    lock.acquire()
    releaser = threading.Timer(3.0, lock.release)      # an unbounded wait ends, and the test fails
    releaser.start()
    began = time.monotonic()
    res = _client(ALICE).post(route)
    elapsed = time.monotonic() - began
    releaser.cancel()
    if lock.locked():
        lock.release()
    assert elapsed < 2.0, elapsed
    assert res.status_code == 409 and res.json()["detail"] == routes.FLOW_BUSY, res.text
    assert (folder / "creds.json").is_file() and started == []
