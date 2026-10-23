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
- set it although a chat could not be read -> both unreadable tests;
- no journal before the first write of a group -> the crash test;
- rewrite a group whose journal could not be written -> the without-the-journal test;
- write the journal without encryption -> the crash test;
- skip the journal at the next start -> the failed-rewrite and crash tests;
- take a journal's file names without checking them -> the journal test;
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


def _chat(directory, session_id, runtime_state, *, scope=OWNER_SCOPE):
    """A chat file as SessionManager writes it, encrypted, dated in the past."""
    session = Session(id=session_id, name=session_id)
    session.metadata["user_scope_id"] = scope
    session.add_message(role="user", content=f"hello from {session_id}")
    session.add_message(role="assistant", content="Gern.")
    session.runtime_state = runtime_state
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.json"
    data_files.write_bytes_atomic(path, json.dumps(session.to_dict(), indent=2).encode("utf-8"),
                                  encrypt=True)
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


def test_a_journal_never_reaches_past_the_chats(store):
    """The journal names the chats it still has to finish. A name that leaves the chat
    store is refused, and the repair stays open rather than guess at the group."""
    outside = _chat(store.parent.parent, "outside", _runtime_state("owner"))
    state = at_rest_migration._state_digest(_read(outside))
    at_rest_migration._write_journal(state, ["sessions/../../outside.json"])
    raw = outside.read_bytes()

    report = _start()

    assert outside.read_bytes() == raw
    assert report["context_copies"]["unreadable"] == 1
    assert _done() is False


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


def test_an_unreadable_chat_is_never_written_and_leaves_the_repair_open(store):
    """A chat whose key the ring does not hold must stay recoverable: written over, it would
    be lost for good. And it may be a copy, so the repair is not over while it is there."""
    _chat(store, "red654321", _runtime_state("owner"))
    _chat(store, "green123456", _runtime_state("owner"), scope=OTHER_SCOPE)
    locked = store / "web-locked.json"
    locked.write_bytes(data_files.FILE_MAGIC + b"\x00" * 64)
    raw = locked.read_bytes()

    report = _start()

    assert report["context_copies"]["unreadable"] == 1
    assert report["context_copies"]["dropped"] == 2
    assert locked.read_bytes() == raw
    assert _done() is False, "the repair was recorded as done with a chat it never read"

    locked.unlink()
    _start()
    assert _done() is True


def test_a_key_that_is_not_there_yet_leaves_the_repair_open(store, monkeypatch):
    """At a start where the file-store key cannot be opened, every chat is unreadable. The
    repair must not count that as done, or the copies come back with the key."""
    origin = _chat(store, "red654321", _runtime_state("owner"))
    copy = _chat(store, "green123456", _runtime_state("owner"), scope=OTHER_SCOPE)

    def no_key():
        raise RuntimeError("the keyring is locked")

    real_key = data_files._key
    monkeypatch.setattr(data_files, "_key", no_key)
    report = _start()
    assert report["context_copies"]["unreadable"] == 2 and _done() is False

    monkeypatch.setattr(data_files, "_key", real_key)
    _start()
    assert not _holds_state(origin) and not _holds_state(copy)
    assert _done() is True


def test_the_repair_runs_once_and_the_others_wait_for_it(store, monkeypatch):
    """The web startup, the runner and the terminal lanes all call it; the first one runs
    the pass, and a second caller must not go on (and save chats) before it is over."""
    started, release, calls = threading.Event(), threading.Event(), []

    def slow_pass(tree):
        calls.append(tree)
        started.set()
        release.wait(5)
        return {"dropped": 0, "changed": 0, "gone": 0, "unreadable": 0, "failed": 0}

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
