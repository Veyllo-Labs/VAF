# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The start-up sweep drops, once, the saved context state one chat carried into another.

Before the session loader cleared the agent's context manager on a switch, a chat with
nothing saved took over the previous chat's summary, decisions and goal, and the runner's
next save stored them as that chat's own, across accounts. Every later load restored them.
A copy cannot be told from its origin except by the identical text, so the repair drops the
`context` provider entry of every chat whose saved state is identical to another chat's,
and nothing else. The stores are scratch folders here; chats are written encrypted through
the same seam the product uses.

MUTATIONS, each turns the named tests red:
- drop every saved state, shared or not (no `< 2` check) -> the shared-state test;
- compare with the `last_updated` stamps -> the shared-state test;
- compare the whole provider state (the limits included) -> the shared-state test;
- treat an empty state as a state -> the shared-state test;
- drop more than the `context` entry -> the shared-state test;
- no `os.utime` after the rewrite -> the shared-state test;
- write the chat back without encryption -> the shared-state test;
- follow `file_encryption_enabled` for a chat that was ciphertext -> the encryption-off test;
- read only `sessions/*.json` -> the archive test;
- run without consulting the done flag -> the runs-once test;
- set the done flag although a rewrite failed -> the without-the-journal test;
- set it although the key was not there -> both key tests;
- ask for the key about a chat that is not ciphertext -> the plaintext-installation test;
- hold it open for a chat broken under the present key -> the broken-chat test;
- no journal before the first write of a group -> the crash test;
- rewrite a group whose journal could not be written -> the without-the-journal test;
- write the journal without encryption -> the crash test;
- skip the journal at the next start -> the failed-rewrite and crash tests;
- take a journal's file names without `contained_path` -> the journal-names test (the
  backslash case on any host);
- stop at a journal that cannot be used instead of setting it aside -> the set-aside test;
- set a journal aside while only the key is missing -> the journal-waits test;
- the claim, the migration or the repair outside `CHAT_REWRITE_LOCK` -> the two overlap
  tests;
- drop without reading the file again -> the saved-since-the-scan test;
- no lock around the repair -> the waits-for-it test;
- the runner or a web lifespan without the repair before its first turn -> the wiring test;
- the done flag off the admin-only list -> the admin-only test.
"""
import json
import os
import threading
from pathlib import Path

import pytest

from vaf.core import at_rest_migration, data_files, data_stores
from vaf.core.config import Config
from vaf.core.platform import Platform
from vaf.core.session import Session

_REPO = Path(__file__).resolve().parents[1]
OWNER_SCOPE = "6f9619ff-8b86-d011-b42d-00c04fc964ff"
OTHER_SCOPE = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"
PAST_NS = 1_700_000_000_000_000_000


def _runtime_state(tag, *, stamp="2026-10-01T10:00:00", max_tokens=45000, empty=False):
    words = (lambda text: []) if empty else (lambda text: [text])
    return {
        "timestamp": stamp,
        "schema_version": "1.0",
        "sidebar_documents": [{"name": f"{tag}.pdf", "content": "Angebot"}],
        "providers": {
            "context": {"version": "1.0", "captured_at": stamp, "state": {
                "intent": {"primary_goal": "" if empty else f"{tag} goal",
                           "sub_goals": words(f"{tag} topic"), "constraints": [], "keywords": [],
                           "last_updated": stamp},
                "state": {"files_created": [], "files_read": [], "files_modified": [],
                          "errors_encountered": [], "tools_used": [],
                          "key_decisions": words(f"{tag} decision"),
                          "narrative_summary": "" if empty else f"{tag} summary",
                          "last_updated": stamp},
                "max_tokens": max_tokens, "trigger_threshold": 0.85}},
            "tool_activity": {"version": "1.0", "captured_at": stamp, "state": {"recent": []}},
        },
    }


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Every root of the store list in a scratch folder; the enforcement branch held open
    (an empty scratch home would flip `allow_plaintext_at_rest` process-wide, as
    tests/test_scheduled_store_modes.py describes); a config and a journal of this test's
    own, so the done flag starts unset and nothing reaches another test."""
    named = {name: tmp_path / name for name in ("app", "vaf", "data", "config", "projects")}
    monkeypatch.setattr(data_stores, "roots", lambda: named)
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: named["vaf"]))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(at_rest_migration, "_done", False)
    monkeypatch.setattr(at_rest_migration, "_repair_ran", False)
    monkeypatch.setattr(at_rest_migration, "_any_plaintext_left", lambda trees: True)
    journal = named["app"] / "maintenance" / "context_repair.json"
    monkeypatch.setattr(at_rest_migration, "_journal_path", lambda: journal)
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Config, "CONFIG_FILE", config)
    sessions = named["vaf"] / "sessions"
    sessions.mkdir(parents=True)
    return sessions


def _chat(directory, session_id, runtime_state, *, scope=OWNER_SCOPE, encrypt=True):
    """A chat file as SessionManager writes it, encrypted unless told, dated in the past;
    `scope=None` is a chat from before scope stamping."""
    session = Session(id=session_id, name=session_id)
    if scope:
        session.metadata["user_scope_id"] = scope
    session.add_message(role="user", content=f"hello from {session_id}")
    session.add_message(role="assistant", content="Gern.")
    session.runtime_state = runtime_state
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.json"
    data_files.write_bytes_atomic(path, json.dumps(session.to_dict(), indent=2).encode("utf-8"),
                                  encrypt=encrypt)
    os.utime(path, ns=(PAST_NS, PAST_NS))
    return path


def _read(path):
    return json.loads(data_files.read_bytes(path).decode("utf-8"))


def _holds_state(path):
    return "context" in _read(path)["runtime_state"]["providers"]


def _start():
    at_rest_migration._done = False
    at_rest_migration._repair_ran = False
    return at_rest_migration.run_once(force=True)


def _done():
    return Config.get("context_copy_sweep_done")


def _writer_refusing(name, error):
    real = data_files.write_bytes_atomic

    def write(path, data, **kw):
        if os.path.basename(str(path)) == name:
            raise error
        return real(path, data, **kw)
    return write


def test_a_state_two_chats_share_is_dropped_from_both_and_nothing_else(store):
    origin = _chat(store, "red654321", _runtime_state("owner"))
    # The copy the runner saved into another account's chat: captured later, under other
    # limits, the same content.
    copy = _chat(store, "green123456",
                 _runtime_state("owner", stamp="2026-10-02T09:00:00", max_tokens=128000),
                 scope=OTHER_SCOPE)
    own = _chat(store, "yellow012345", _runtime_state("other"), scope=OTHER_SCOPE)
    empty_a = _chat(store, "web-empty-a", _runtime_state("a", empty=True))
    empty_b = _chat(store, "web-empty-b", _runtime_state("b", empty=True))
    before = {p: (p.read_bytes(), _read(p)) for p in (origin, copy, own, empty_a, empty_b)}

    report = _start()

    assert report["context_copies"]["dropped"] == 2
    for path in (origin, copy):
        data, old = _read(path), before[path][1]
        assert "context" not in data["runtime_state"]["providers"], f"{path.name} kept the copy"
        assert data["runtime_state"]["providers"]["tool_activity"] == \
            old["runtime_state"]["providers"]["tool_activity"]
        assert data["runtime_state"]["sidebar_documents"] == old["runtime_state"]["sidebar_documents"]
        assert data["messages"] == old["messages"] and data["metadata"] == old["metadata"]
        assert data["updated_at"] == old["updated_at"], "the chat looks updated"
        assert path.stat().st_mtime_ns == PAST_NS, "the chat moved in the chat list"
        assert data_files.is_encrypted(path.read_bytes()), "the chat was written back in plaintext"
    for path in (own, empty_a, empty_b):
        assert path.read_bytes() == before[path][0], f"{path.name} was rewritten"
    assert _done() is True
    assert not at_rest_migration._journal_path().exists()


def test_a_chat_that_was_ciphertext_stays_ciphertext_with_encryption_off(store):
    """With `file_encryption_enabled` off, new writes are plaintext. A repair is no new
    write the person asked for: a chat it rewrites keeps the form it had."""
    origin = _chat(store, "red654321", _runtime_state("owner"))
    copy = _chat(store, "green123456", _runtime_state("owner"), scope=OTHER_SCOPE)
    Config.CONFIG_FILE.write_text(json.dumps({"file_encryption_enabled": False}), encoding="utf-8")

    _start()

    for path in (origin, copy):
        assert data_files.is_encrypted(path.read_bytes()), f"{path.name} was downgraded to plaintext"
        assert not _holds_state(path)


def test_an_archived_chat_takes_part(store):
    """An archived chat is still a chat the agent reads (memory recall) and can be brought
    back; a copy that was archived, or an origin that was, counts the same."""
    live = _chat(store, "red654321", _runtime_state("owner"))
    archived = _chat(store / "archive" / OTHER_SCOPE[:8], "green123456",
                     _runtime_state("owner"), scope=OTHER_SCOPE)

    _start()

    for path in (live, archived):
        assert not _holds_state(path), path.name


def test_the_pass_runs_once(store):
    _chat(store, "red654321", _runtime_state("owner"))
    _chat(store, "green123456", _runtime_state("owner"), scope=OTHER_SCOPE)
    _start()
    assert _done() is True

    # Two chats that share a state later (a fork, a hand copy) are not this repair's business.
    later = _chat(store, "web-later-a", _runtime_state("later"))
    _chat(store, "web-later-b", _runtime_state("later"))
    report = _start()

    assert "context_copies" not in report
    assert _holds_state(later)


def test_a_failed_rewrite_leaves_the_pass_for_the_next_start(store, monkeypatch):
    """The copy is rewritten first (the files go in name order) and the origin's write
    fails. The journal keeps the group, so the next start finishes it: without it the
    origin, now the only chat with that state, would look unique and never be found."""
    copy = _chat(store, "green123456", _runtime_state("owner"), scope=OTHER_SCOPE)
    origin = _chat(store, "red654321", _runtime_state("owner"))
    raw_origin = origin.read_bytes()
    real_write = data_files.write_bytes_atomic

    monkeypatch.setattr(data_files, "write_bytes_atomic",
                        _writer_refusing(origin.name, OSError("read-only")))
    report = _start()

    assert report["context_copies"]["failed"] == 1
    assert origin.read_bytes() == raw_origin, "a failed rewrite touched the file"
    assert _done() is False, "the pass was recorded as done with a copy left"
    assert at_rest_migration._journal_path().exists()

    monkeypatch.setattr(data_files, "write_bytes_atomic", real_write)
    _start()
    assert not _holds_state(copy) and not _holds_state(origin)
    assert _done() is True
    assert not at_rest_migration._journal_path().exists()


def test_a_start_that_dies_in_the_middle_of_a_group_is_finished_by_the_next(store, monkeypatch):
    copy = _chat(store, "green123456", _runtime_state("owner"), scope=OTHER_SCOPE)
    origin = _chat(store, "red654321", _runtime_state("owner"))
    journal = at_rest_migration._journal_path()
    real_write = data_files.write_bytes_atomic

    monkeypatch.setattr(data_files, "write_bytes_atomic",
                        _writer_refusing(origin.name, KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        _start()

    assert not _holds_state(copy) and _holds_state(origin), "the fixture did not stop midway"
    assert journal.exists(), "nothing recorded the group before its first write"
    assert data_files.is_encrypted(journal.read_bytes()), "the journal was written in plaintext"
    assert _read(journal)["format"] == at_rest_migration.CONTEXT_REPAIR_FORMAT
    assert _done() is not True

    monkeypatch.setattr(data_files, "write_bytes_atomic", real_write)
    _start()
    assert not _holds_state(origin), "the next start left the rest of the group alone"
    assert _done() is True and not journal.exists()


def test_no_chat_is_rewritten_without_the_journal(store, monkeypatch):
    """A group whose journal cannot be written is not started: a crash in its middle could
    not be finished, and the repair stays open for the next start."""
    origin = _chat(store, "red654321", _runtime_state("owner"))
    copy = _chat(store, "green123456", _runtime_state("owner"), scope=OTHER_SCOPE)
    raw = {p: p.read_bytes() for p in (origin, copy)}
    monkeypatch.setattr(at_rest_migration, "_write_journal", lambda state, chats: False)

    report = _start()

    assert report["context_copies"]["failed"] == 2
    assert all(p.read_bytes() == raw[p] for p in (origin, copy)), "a chat was rewritten unjournaled"
    assert _done() is False


def _set_aside(store):
    return sorted(at_rest_migration._journal_path().parent.glob("context_repair.unusable-*.json"))


@pytest.mark.parametrize("name", ["../../outside.json", "..\\..\\outside.json", "absolute"])
def test_a_journal_never_reaches_past_the_chats(store, name):
    """The journal names the chats it still has to finish, inside the sessions folder. A
    name that leaves it is refused in either separator convention (a backslash walks out
    on Windows only, so the check must not depend on the host) and as an absolute path;
    the journal is set aside and the full scan runs instead."""
    outside = _chat(store.parent.parent, "outside", _runtime_state("owner"))
    state = at_rest_migration._state_digest(_read(outside))
    at_rest_migration._write_journal(state, [str(outside) if name == "absolute" else name])
    raw = outside.read_bytes()

    _start()

    assert outside.read_bytes() == raw, "a journal name reached a file outside the chats"
    assert len(_set_aside(store)) == 1 and not at_rest_migration._journal_path().exists()
    assert _done() is True


@pytest.mark.parametrize("kind", ["garbage", "plaintext_in_an_enforced_store", "foreign_format"])
def test_a_journal_that_cannot_be_used_is_set_aside_and_the_scan_runs(store, kind):
    """Only a key that is not there yet makes the repair wait. Any other journal it cannot
    use would stop it at every start for good: a plaintext journal left while encryption
    was off, read after the store was enforced, is the realistic way in."""
    origin = _chat(store, "red654321", _runtime_state("owner"))
    copy = _chat(store, "green123456", _runtime_state("owner"), scope=OTHER_SCOPE)
    journal = at_rest_migration._journal_path()
    journal.parent.mkdir(parents=True, exist_ok=True)
    if kind == "garbage":
        journal.write_bytes(data_files.FILE_MAGIC + b"\x01" * 48)
    elif kind == "foreign_format":
        data_files.write_json_atomic(journal, {"format": "ctxrepair-9-000000", "state": "x",
                                               "chats": []})
    else:
        journal.write_text(json.dumps({"format": at_rest_migration.CONTEXT_REPAIR_FORMAT,
                                       "state": "x", "chats": []}), encoding="utf-8")
        Config.CONFIG_FILE.write_text(json.dumps({"allow_plaintext_at_rest": False}),
                                      encoding="utf-8")
    before = journal.read_bytes()

    _start()

    assert [p.read_bytes() for p in _set_aside(store)] == [before], "the journal was not kept aside"
    assert not _holds_state(origin) and not _holds_state(copy), "the scan did not run"
    assert _done() is True


def test_a_journal_waits_while_the_key_is_not_there(store, monkeypatch):
    """A journal that is ciphertext while the key cannot be opened is no broken journal:
    it waits in place for the start where the key is back."""
    origin = _chat(store, "red654321", _runtime_state("owner"))
    copy = _chat(store, "green123456", _runtime_state("owner"), scope=OTHER_SCOPE)
    state = at_rest_migration._state_digest(_read(origin))
    at_rest_migration._write_journal(state, ["red654321.json", "green123456.json"])
    real_key = data_files._key

    def no_key():
        raise RuntimeError("the keyring is locked")

    monkeypatch.setattr(data_files, "_key", no_key)
    report = _start()
    assert report["context_copies"]["key_missing"] == 1 and _done() is False
    assert at_rest_migration._journal_path().exists() and not _set_aside(store)

    monkeypatch.setattr(data_files, "_key", real_key)
    _start()
    assert not _holds_state(origin) and not _holds_state(copy)
    assert _done() is True and not at_rest_migration._journal_path().exists()


def test_a_chat_saved_since_the_scan_is_not_overwritten(store):
    """The scan reads every chat before anything is written, and a turn can save one in
    between. The rewrite reads it again and leaves a chat that no longer holds the shared
    state as it is."""
    path = _chat(store, "red654321", _runtime_state("owner"))
    state = at_rest_migration._state_digest(_read(path))
    _chat(store, "red654321", _runtime_state("owner, then a turn of its own"))
    raw = path.read_bytes()

    assert at_rest_migration._drop_context_entry(path, state) == "changed"
    assert path.read_bytes() == raw


def test_a_chat_broken_under_the_present_key_is_left_alone_and_does_not_hold_the_repair(store):
    """A chat that cannot be read while the key is there is never written (it may still be
    recoverable), and it does not keep the repair open: VAF cannot load it either, and a
    repair held open by it would decrypt every chat at every start before the first turn."""
    _chat(store, "red654321", _runtime_state("owner"))
    _chat(store, "green123456", _runtime_state("owner"), scope=OTHER_SCOPE)
    broken = store / "web-broken.json"
    broken.write_bytes(data_files.FILE_MAGIC + b"\x00" * 64)
    raw = broken.read_bytes()

    report = _start()

    assert report["context_copies"]["unreadable"] == 1
    assert report["context_copies"]["dropped"] == 2
    assert broken.read_bytes() == raw
    assert _done() is True, "one broken chat held the repair open for every later start"


def test_a_plaintext_installation_is_never_handed_a_key(store, monkeypatch):
    """With encryption off nothing asks for the file-store key, and a chat that fails to
    parse must not either: `_key()` mints a key on an installation that has none, with a
    keyring and a recovery note. Only bytes that are ciphertext may ask whether their key is
    there."""
    Config.CONFIG_FILE.write_text(json.dumps({"file_encryption_enabled": False}),
                                  encoding="utf-8")
    origin = _chat(store, "red654321", _runtime_state("owner"), encrypt=False)
    copy = _chat(store, "green123456", _runtime_state("owner"), scope=OTHER_SCOPE, encrypt=False)
    broken = store / "web-broken.json"
    broken.write_text("{not json", encoding="utf-8")
    asked, real_key = [], data_files._key
    monkeypatch.setattr(data_files, "_key", lambda: asked.append(True) or real_key())

    report = _start()

    assert report["context_copies"]["unreadable"] == 1 and report["context_copies"]["dropped"] == 2
    assert at_rest_migration._drop_context_entry(broken, "any") == "unreadable"
    assert asked == [], "the repair asked for a file-store key on a plaintext installation"
    assert not data_files.is_encrypted(origin.read_bytes()) and not _holds_state(copy)
    assert _done() is True


def test_a_key_that_is_not_there_yet_leaves_the_repair_open(store, monkeypatch):
    """At a start where the file-store key cannot be opened, no chat can be read. The repair
    must not count that as done, or the copies come back with the key."""
    origin = _chat(store, "red654321", _runtime_state("owner"))
    copy = _chat(store, "green123456", _runtime_state("owner"), scope=OTHER_SCOPE)

    def no_key():
        raise RuntimeError("the keyring is locked")

    real_key = data_files._key
    monkeypatch.setattr(data_files, "_key", no_key)
    report = _start()
    assert report["context_copies"]["key_missing"] == 2 and _done() is False

    monkeypatch.setattr(data_files, "_key", real_key)
    _start()
    assert not _holds_state(origin) and not _holds_state(copy)
    assert _done() is True


def test_the_claim_of_unscoped_chats_and_the_repair_never_overlap(store, monkeypatch):
    """Both read a chat and write it back later. Interleaved, the claim wrote its older copy
    over the repair (the shared state back for good, the done flag already set), or the
    repair over the claim (an unscoped chat listed to every account again)."""
    from vaf.core.session import SessionManager

    unscoped = _chat(store, "web-unscoped", _runtime_state("owner"), scope=None)
    scoped = _chat(store, "green123456", _runtime_state("owner"), scope=OTHER_SCOPE)
    real_load, held, go = SessionManager.load, threading.Event(), threading.Event()

    def load(self, session_id, *a, **kw):
        session = real_load(self, session_id, *a, **kw)
        if session_id == "web-unscoped" and threading.current_thread().name == "claim":
            held.set()
            go.wait(5)
        return session

    monkeypatch.setattr(SessionManager, "load", load)
    claim = threading.Thread(target=SessionManager(storage_dir=str(store)).claim_unscoped,
                             args=(OWNER_SCOPE,), name="claim")
    claim.start()
    assert held.wait(5)
    repair = threading.Thread(target=at_rest_migration.repair_context_copies_once)
    repair.start()
    repair.join(0.3)
    try:
        assert repair.is_alive(), "the repair ran while the claim held a chat it had read"
    finally:
        go.set()
        claim.join(5)
        repair.join(5)
    data = _read(unscoped)
    assert data["metadata"].get("user_scope_id") == OWNER_SCOPE, "the claim was undone"
    assert "context" not in data["runtime_state"]["providers"], "the claim wrote the copy back"
    assert not _holds_state(scoped)


def test_the_at_rest_migration_and_the_repair_never_overlap(store, monkeypatch):
    """The migration reads a plaintext chat and writes it back as ciphertext. A repair that
    lands in between has its dropped copy written back by the migration's older bytes."""
    plain = _chat(store, "web-plain", _runtime_state("owner"), encrypt=False)
    other = _chat(store, "green123456", _runtime_state("owner"), scope=OTHER_SCOPE)
    real, held, go = data_files.is_encrypted, threading.Event(), threading.Event()

    def is_encrypted(data):
        if (threading.current_thread().name == "migration" and data
                and not data.startswith(data_files.FILE_MAGIC) and b"web-plain" in data):
            held.set()
            go.wait(5)
        return real(data)

    monkeypatch.setattr(data_files, "is_encrypted", is_encrypted)
    migration = threading.Thread(target=at_rest_migration.run_once, kwargs={"force": True},
                                 name="migration")
    migration.start()
    assert held.wait(5)
    repair = threading.Thread(target=at_rest_migration.repair_context_copies_once)
    repair.start()
    repair.join(0.3)
    try:
        assert repair.is_alive(), "the repair ran while the migration held a chat it had read"
    finally:
        go.set()
        migration.join(5)
        repair.join(5)
    assert data_files.is_encrypted(plain.read_bytes())
    assert not _holds_state(plain), "the migration wrote the copy back"
    assert not _holds_state(other)


def test_the_repair_runs_once_and_the_others_wait_for_it(store, monkeypatch):
    """The web startup, the runner and the terminal lanes all call it; the first one runs
    the pass, and a second caller must not go on (and save chats) before it is over."""
    started, release, calls = threading.Event(), threading.Event(), []

    def slow_pass(tree):
        calls.append(tree)
        started.set()
        release.wait(5)
        return {"dropped": 0, "changed": 0, "gone": 0, "unreadable": 0, "key_missing": 0,
                "failed": 0}

    monkeypatch.setattr(at_rest_migration, "_drop_shared_context_state", slow_pass)
    first = threading.Thread(target=at_rest_migration.repair_context_copies_once)
    first.start()
    assert started.wait(5)
    second = threading.Thread(target=at_rest_migration.repair_context_copies_once)
    second.start()
    second.join(0.3)
    try:
        assert second.is_alive(), "a second caller went on while the repair was still running"
    finally:
        release.set()
        first.join(5)
        second.join(5)
    assert len(calls) == 1


def test_no_turn_and_no_web_request_comes_before_the_repair():
    """`SessionManager.save` takes no lock the repair could hold, so a turn saved between
    the repair's read and its write would be lost. The runner waits for it before its first
    task, and every web lifespan before it serves or starts a scheduler (in TLS mode only
    one of the two runs `run_once`)."""
    runner = (_REPO / "vaf" / "core" / "headless_runner.py").read_bytes().decode("utf-8")
    body = runner.replace("\r\n", "\n").split("def run_headless_agent", 1)[1]
    call = body.index("repair_context_copies_once()")
    assert call < body.index("    # Main Loop") < body.index("tq.get(")

    web = (_REPO / "vaf" / "core" / "web_server.py").read_bytes().decode("utf-8")
    startup = web.replace("\r\n", "\n").split("async def startup_event", 1)[1]
    gate = ("\n    try:\n        from vaf.core.at_rest_migration import repair_context_copies_once\n"
            "        await asyncio.to_thread(repair_context_copies_once)\n")
    assert gate in startup, "the web startup waits for the repair only inside the run_once gate"
    at = startup.index(gate)
    assert at < startup.index("start_thinking_mode_background()")
    assert at < startup.index("ensure_scheduler_started(")

    sweep = (_REPO / "vaf" / "core" / "at_rest_migration.py").read_bytes().decode("utf-8")
    assert "counts = repair_context_copies_once()" in sweep.split("def run_once", 1)[1]


def test_the_done_flag_is_admin_only():
    """Set back to false by a non-admin, the next start would rewrite everyone's chats."""
    assert "context_copy_sweep_done" in Config.DEFAULTS
    assert Config.is_global_config_key("context_copy_sweep_done")
    assert "context_copy_sweep_done" not in Config.filter_for_non_admin(
        {"context_copy_sweep_done": False, "theme": "dark"})
