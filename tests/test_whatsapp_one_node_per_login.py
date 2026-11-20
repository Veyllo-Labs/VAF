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
    routes._qr_flows[routes._flow_key(scope)] = flow
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

    for _ in range(rounds):
        with _FuzzNode.lock:
            for n in _FuzzNode.nodes:
                if n.died is None:
                    _FuzzNode.seq += 1
                    n.died = _FuzzNode.seq
        wa._spawned_nodes.clear()
        plans = [[rng.choice(folders) for _ in range(3)] for _ in range(rng.randint(2, 4))]
        threads = [threading.Thread(target=worker, args=(plan,)) for plan in plans]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    assert _FuzzNode.violations == [], _FuzzNode.violations[:5]
    assert refusals == [], refusals[:5]
    assert len(_FuzzNode.nodes) > rounds, "the fuzz spawned almost nothing; it is broken"
