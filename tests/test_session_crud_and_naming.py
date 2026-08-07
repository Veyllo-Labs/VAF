# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Session create/rename/delete in the terminal app, and ONE name everywhere.

The engine half: `SessionManager.rename` used to go through `load()`/`save()`
with all their side effects - it repointed `_current` and, on a manager with
a bound state registry, restored the renamed session's runtime_state into the
LIVE registry (and wrote the live snapshot into the foreign file). The web
manager carries no registry and was safe by accident; the terminal app's
carries one and was not. `load(repoint=False)` is the primitive both rename
and the exit-time name check needed.

The consistency half: every rename path writes the file, so THE FILE is the
name's source of truth - the terminal app adopts the on-disk name before its
exit save (a web rename must not be overwritten by "last writer wins"), the
headless runner gained the RENAME_SESSION branch it silently swallowed, and
both surfaces now read the same engine list (`list_ui`: channel and thinking
sessions stay in their dashboards).
"""
import time
from types import SimpleNamespace

import pytest

from vaf.core.session import Session, SessionManager
from vaf.cli.tui_app.agent_bridge import AgentBridge


def _wait(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ── engine: rename without side effects ─────────────────────────────────────────────

class _Registry:
    """Fails the test the moment rename touches the live state."""
    def __init__(self):
        self.touched = []

    def is_enabled(self):
        return True

    def restore_snapshot(self, snap):
        self.touched.append(("restore", snap))

    def capture_snapshot(self):
        self.touched.append(("capture", None))
        return SimpleNamespace(to_dict=lambda: {})


def _mgr(tmp_path, registry=None):
    return SessionManager(storage_dir=str(tmp_path), state_registry=registry)


def test_rename_changes_the_file_and_nothing_else(tmp_path):
    reg = _Registry()
    mgr = _mgr(tmp_path, registry=reg)
    a = mgr.new()
    a.runtime_state = {"providers": {"x": 1}}
    mgr.save(a, sync_state=False)
    b = mgr.new()
    mgr.save(b, sync_state=False)
    mgr._current = b

    assert mgr.rename(a.id, "Steuererklaerung") is True
    assert mgr._current is b, "rename repointed what 'current' means"
    assert reg.touched == [], (
        "rename moved runtime state through the LIVE registry - cross-session "
        "contamination")
    disk = mgr.load(a.id, restore_state=False, repoint=False)
    assert disk.name == "Steuererklaerung"
    assert mgr._current is b


def test_rename_of_a_missing_session_is_false(tmp_path):
    assert _mgr(tmp_path).rename("nope", "x") is False


def test_load_without_repoint_reads_without_changing_current(tmp_path):
    mgr = _mgr(tmp_path)
    a = mgr.new()
    mgr.save(a)
    b = mgr.new()
    mgr.save(b)
    mgr._current = b
    read = mgr.load(a.id, restore_state=False, repoint=False)
    assert read.id == a.id and mgr._current is b


# ── engine: one surface list ────────────────────────────────────────────────────────

def test_list_ui_hides_channel_and_thinking_sessions(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    rows = [
        {"id": "green123456", "name": "Chat", "metadata": {}},
        {"id": "telegram_42", "name": "TG", "metadata": {}},
        {"id": "whatsapp_7", "name": "WA", "metadata": {}},
        {"id": "discord_1", "name": "DC", "metadata": {}},
        {"id": "thinking_9", "name": "T", "metadata": {}},
        {"id": "ab12cd34", "name": "T2", "metadata": {"source": "thinking"}},
    ]
    monkeypatch.setattr(mgr, "list", lambda limit=50, user_scope_id=None: rows)
    assert [s["id"] for s in mgr.list_ui()] == ["green123456"]


def test_the_web_sidebar_reads_the_same_engine_list():
    from pathlib import Path
    import vaf.core.web_server as ws

    src = Path(ws.__file__).read_text(encoding="utf-8")
    assert "def _web_ui_sessions" not in src, (
        "the web grew its private sidebar filter back")
    assert "session_mgr.list_ui(" in src


# ── headless runner: the swallowed rename ───────────────────────────────────────────

def test_the_headless_runner_repairs_its_in_memory_name():
    from vaf.core.headless_runner import _handle_command

    cur = SimpleNamespace(id="green123456", name="alt")
    mgr = SimpleNamespace(_current=cur)
    _handle_command("__CMD__:RENAME_SESSION:green123456:Neuer Name",
                    SimpleNamespace(), mgr)
    assert cur.name == "Neuer Name"

    other = SimpleNamespace(id="ab12cd34", name="fremd")
    mgr2 = SimpleNamespace(_current=other)
    _handle_command("__CMD__:RENAME_SESSION:green123456:X",
                    SimpleNamespace(), mgr2)
    assert other.name == "fremd"


# ── the bridge ──────────────────────────────────────────────────────────────────────

def _bridge(monkeypatch, *, session=None, mgr_extra=None):
    events = []

    class _Events:
        def __getattr__(self, name):
            def _rec(*args):
                events.append((name, *args))
            return _rec

    set_ids = []
    import vaf.core.subagent_ipc as ipc_mod
    monkeypatch.setattr(ipc_mod, "set_current_session_id",
                        lambda sid: set_ids.append(sid))

    session = session or SimpleNamespace(
        id="green123456", name="alt",
        messages=[{"role": "user", "content": "hi", "timestamp": ""}])
    calls = {"renamed": [], "deleted": [], "saved": []}
    mgr = SimpleNamespace(
        rename=lambda sid, name: calls["renamed"].append((sid, name)) or True,
        delete=lambda sid: calls["deleted"].append(sid) or True,
        save=lambda s, **kw: calls["saved"].append(s) or "/tmp/x",
        list=lambda limit=20, user_scope_id=None: [],
        list_ui=lambda limit=20, user_scope_id=None: [],
        load=lambda sid, **kw: session,
        new=lambda **kw: SimpleNamespace(id="red654321", name="neu",
                                         messages=[], metadata=kw),
    )
    for k, v in (mgr_extra or {}).items():
        setattr(mgr, k, v)
    agent = SimpleNamespace(get_token_usage=lambda: (1, 2),
                            set_event_sink=lambda s: None,
                            shutdown=lambda: None,
                            load_session_context=lambda sid: None)
    b = AgentBridge(agent, session, mgr, _Events(),
                    web_interface_getter=lambda: SimpleNamespace(
                        resolve_gate=lambda *a: True))
    return b, events, calls, set_ids


def test_a_runtime_switch_finally_stamps_the_ipc_session(monkeypatch):
    """Boot stamped it, the switch never did - the tasks line and the queue
    drain kept answering for the PREVIOUS session."""
    b, events, calls, set_ids = _bridge(monkeypatch)
    other = SimpleNamespace(id="ab12cd34", name="x",
                            messages=[{"role": "user", "content": "y",
                                       "timestamp": ""}])
    b.session_mgr.load = lambda sid, **kw: other
    b.load_session("ab12cd34")
    # The lane stamps its own session at start (thread-context fix), so the
    # switch target is the LAST stamp, not the only one.
    assert _wait(lambda: bool(set_ids) and set_ids[-1] == "ab12cd34"), set_ids
    b.shutdown()


def test_new_session_creates_stamps_and_switches(monkeypatch):
    b, events, calls, set_ids = _bridge(monkeypatch)
    b.new_session()
    assert _wait(lambda: any(e[0] == "session_switched" and e[1] == "red654321"
                             for e in events)), events
    assert calls["saved"], "the new session was never persisted"
    assert set_ids and set_ids[-1] == "red654321", set_ids
    scope_kw = calls["saved"][0].metadata
    assert "user_scope_id" in scope_kw, "the create lost its owner stamp"
    b.shutdown()


def test_rename_repairs_the_live_copy_and_tells_the_chrome(monkeypatch):
    b, events, calls, set_ids = _bridge(monkeypatch)
    b.rename_session("green123456", "  Neuer   Name ")
    assert _wait(lambda: calls["renamed"] == [("green123456", "Neuer Name")])
    assert _wait(lambda: b.session.name == "Neuer Name")
    assert _wait(lambda: any(e[0] == "chrome_changed" for e in events))
    b.shutdown()


def test_an_empty_rename_is_refused_before_the_engine(monkeypatch):
    b, events, calls, set_ids = _bridge(monkeypatch)
    b.rename_session("green123456", "   ")
    assert _wait(lambda: any(e[0] == "event_note" and "empty" in e[2]
                             for e in events))
    time.sleep(0.1)
    assert calls["renamed"] == []
    b.shutdown()


def test_deleting_the_live_session_is_refused(monkeypatch):
    b, events, calls, set_ids = _bridge(monkeypatch)
    b.delete_session("green123456")
    assert _wait(lambda: any(e[0] == "event_note" and "switch" in e[2]
                             for e in events))
    time.sleep(0.1)
    assert calls["deleted"] == []
    b.shutdown()


def test_deleting_another_session_deletes_and_refreshes(monkeypatch):
    b, events, calls, set_ids = _bridge(monkeypatch)
    b.delete_session("ab12cd34")
    assert _wait(lambda: calls["deleted"] == ["ab12cd34"])
    assert _wait(lambda: any(e[0] == "session_list" for e in events))
    b.shutdown()


def test_the_exit_save_adopts_the_on_disk_name(monkeypatch):
    """A web rename lands on disk; the terminal app's full-object exit save
    must carry it instead of resurrecting the boot-time name."""
    b, events, calls, set_ids = _bridge(monkeypatch)
    disk = SimpleNamespace(id="green123456", name="Vom Web umbenannt",
                           messages=[{"role": "user", "content": "hi",
                                      "timestamp": ""}])
    b.session_mgr.load = lambda sid, **kw: disk
    b.shutdown()
    assert _wait(lambda: bool(calls["saved"])), "the exit save never ran"
    assert b.session.name == "Vom Web umbenannt"


# ── panel and app wiring ────────────────────────────────────────────────────────────

def _panel(entries):
    from vaf.cli.tui_app.screens import SessionsPanel

    posted = []

    class _P(SessionsPanel):
        def post_message(self, msg):
            posted.append(msg)

    p = _P.__new__(_P)
    p._entries = entries
    p.query_one = lambda sel, cls=None: SimpleNamespace(index=0)
    return p, posted


def test_the_panel_keys_ask_the_app_for_crud():
    from vaf.cli.tui_app.screens import SessionsPanel

    p, posted = _panel([{"id": "ab12cd34", "name": "Alt"}])
    p.action_new_session()
    p.action_rename_session()
    p.action_delete_session()
    kinds = [type(m).__name__ for m in posted]
    assert kinds == ["NewRequested", "RenameRequested", "DeleteRequested"]
    assert posted[1].session_id == "ab12cd34" and posted[1].name == "Alt"
    binds = {b.key for b in SessionsPanel.BINDINGS}
    assert {"n", "r", "d"} <= binds


def test_rename_screen_cleans_and_treats_empty_as_cancel():
    from vaf.cli.tui_app.screens import RenameScreen

    results = []

    class _S(RenameScreen):
        def dismiss(self, value=None):
            results.append(value)

    s = _S.__new__(_S)
    s._submitted(SimpleNamespace(value="  Neuer   Name "))
    s._submitted(SimpleNamespace(value="   "))
    assert results == ["Neuer Name", None]


def test_the_session_words_route_new_and_rename():
    import vaf.cli.tui_app.app as app_mod

    calls = []

    class _A(app_mod.VafApp):
        pass

    a = _A.__new__(_A)
    a._bridge = SimpleNamespace(
        new_session=lambda: calls.append("new"),
        rename_session=lambda sid, name: calls.append(("rename", sid, name)),
        session=SimpleNamespace(id="green123456"),
        load_session=lambda sid: calls.append(("load", sid)),
        describe_session=lambda: calls.append("describe"),
    )
    a.add_event_note = lambda *args: calls.append(("note", args))
    a._cmd_session(["new"])
    a._cmd_session(["rename", "Mein", "Projekt"])
    a._cmd_session(["rename"])
    assert calls[0] == "new"
    assert calls[1] == ("rename", "green123456", "Mein Projekt")
    assert calls[2][0] == "note", "a bare rename must be a usage line, not a load"


# ── the legacy-scope claim ──────────────────────────────────────────────────────────

def test_claim_unscoped_stamps_only_the_ownerless(tmp_path):
    """Pre-scoping sessions leak their names into every user's list via the
    legacy visibility rule; the claim gives them their only possible owner."""
    mgr = _mgr(tmp_path)
    a = mgr.new()
    mgr.save(a, sync_state=False)
    b = mgr.new(user_scope_id="ab12cd34")
    mgr.save(b, sync_state=False)

    assert mgr.claim_unscoped("12345678-1234-5678-1234-567812345678") == 1
    da = mgr.load(a.id, restore_state=False, repoint=False)
    db = mgr.load(b.id, restore_state=False, repoint=False)
    assert da.metadata.get("user_scope_id") == "12345678-1234-5678-1234-567812345678"
    assert db.metadata.get("user_scope_id") == "ab12cd34", (
        "an already-owned session was relabeled")
    assert mgr.claim_unscoped("12345678-1234-5678-1234-567812345678") == 0, (
        "the claim is not idempotent")


def test_claim_without_a_scope_is_a_no_op(tmp_path):
    mgr = _mgr(tmp_path)
    a = mgr.new()
    mgr.save(a, sync_state=False)
    assert mgr.claim_unscoped("") == 0
    assert not (mgr.load(a.id, restore_state=False, repoint=False)
                .metadata.get("user_scope_id"))


def test_claimed_sessions_leave_other_users_lists(tmp_path):
    mgr = _mgr(tmp_path)
    a = mgr.new()
    mgr.save(a, sync_state=False)
    assert any(s["id"] == a.id for s in mgr.list(user_scope_id="ab12cd34")), (
        "precondition: the legacy rule shows unscoped sessions to strangers")
    mgr.claim_unscoped("12345678-1234-5678-1234-567812345678")
    assert not any(s["id"] == a.id for s in mgr.list(user_scope_id="ab12cd34")), (
        "the claimed session still leaks into a stranger's list")


def test_the_web_startup_claims_once():
    from pathlib import Path
    import vaf.core.web_server as ws

    src = Path(ws.__file__).read_text(encoding="utf-8")
    assert "claim_unscoped" in src and "_legacy_claim_done" in src, (
        "the web/tray boot lost its legacy-session claim")


# ── the thread gap: bridge threads know their session ───────────────────────────────

def test_the_lane_thread_knows_its_session(monkeypatch):
    """Tools execute on the bridge's lane thread, and a ContextVar set on the
    main thread at boot is invisible there - every session-scoped read and
    write answered for NO session. The live failure: working-memory writes
    landed in the legacy global store while the plan gate read the empty
    session store and bounced a fully-planned model until its loop-cap."""
    import vaf.core.subagent_ipc as ipc_mod

    # Built WITHOUT the rig's set_current_session_id stub: the lane stamps at
    # thread start, and a stub active at construction would swallow exactly
    # the stamp this test measures.
    session = SimpleNamespace(
        id="green123456", name="alt",
        messages=[{"role": "user", "content": "hi", "timestamp": ""}])
    agent = SimpleNamespace(get_token_usage=lambda: (1, 2),
                            set_event_sink=lambda s: None,
                            shutdown=lambda: None,
                            load_session_context=lambda sid: None)

    class _Ev:
        def __getattr__(self, name):
            return lambda *a, **k: None

    b = AgentBridge(agent, session, SimpleNamespace(), _Ev(),
                    web_interface_getter=lambda: SimpleNamespace(
                        resolve_gate=lambda *a: True))
    seen = []
    b._submit(lambda: seen.append(ipc_mod.get_current_session_id()))
    assert _wait(lambda: bool(seen)), "the lane never ran the probe"
    assert seen == ["green123456"], (
        f"a lane-side tool would serve session {seen[0]!r} - the global-store "
        f"split-brain again")
    b.shutdown()


def test_the_tasks_poll_thread_knows_its_session(monkeypatch):
    import threading

    import vaf.core.subagent_ipc as ipc_mod

    b, events, calls, set_ids = _bridge(monkeypatch)
    monkeypatch.undo()
    seen = []

    class _Ipc:
        def get_active_tasks_for_current_session(self):
            seen.append(ipc_mod.get_current_session_id())
            return []

        def get_paused_workflows_for_session(self, sid):
            return []

    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: _Ipc())
    after = []

    def _poll_and_probe():
        b.tasks_snapshot()
        # The stamp must NOT outlive the read: this thread was only borrowed,
        # and a permanent stamp here leaked the session onto foreign threads -
        # in the suite it was the main thread, and it killed the env fallback
        # for every later reader (two tests went red in a different file).
        monkeypatch.setenv("VAF_SESSION_ID", "ab12cd34")
        after.append(ipc_mod.get_current_session_id())

    t = threading.Thread(target=_poll_and_probe)
    t.start()
    t.join(timeout=5)
    assert seen == ["green123456"], (
        f"the tasks line polled for session {seen[0] if seen else '<never ran>'!r}")
    assert after == ["ab12cd34"], (
        f"the poll's stamp outlived the read: the borrowed thread now answers {after}")
    b.shutdown()


def test_shutdown_does_not_tell_the_callers_thread(monkeypatch):
    """The finalizer runs INLINE on the caller's thread when no turn is in
    flight - a permanent stamp there told that thread a session it never
    declared (in the suite: the main thread, and the env fallback died for
    every later reader; two tests in another file went red). The finalizer's
    stamp is scoped now: told inside, exactly as-before outside."""
    import vaf.core.subagent_ipc as ipc_mod

    session = SimpleNamespace(
        id="green123456", name="alt",
        messages=[{"role": "user", "content": "hi", "timestamp": ""}])
    agent = SimpleNamespace(get_token_usage=lambda: (1, 2),
                            set_event_sink=lambda s: None,
                            shutdown=lambda: None,
                            load_session_context=lambda sid: None)

    class _Ev:
        def __getattr__(self, name):
            return lambda *a, **k: None

    before = ipc_mod.get_current_session_id()
    b = AgentBridge(agent, session, SimpleNamespace(), _Ev(),
                    web_interface_getter=lambda: SimpleNamespace(
                        resolve_gate=lambda *a: True))
    b.shutdown()
    time.sleep(0.3)
    assert ipc_mod.get_current_session_id() == before, (
        "shutdown left the caller's thread told - the suite-wide context poison")
