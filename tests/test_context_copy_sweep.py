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
- read only `sessions/*.json` -> the archive test;
- run without consulting the done-marker -> the runs-once test;
- set the marker although a rewrite failed -> the failed-rewrite test;
- keep the rewritten half of a state whose other half failed -> the failed-rewrite test;
- drop without reading the file again -> the saved-since-the-scan test;
- the marker off the admin-only list -> the admin-only test.
"""
import json
import os

import pytest

from vaf.core import at_rest_migration, data_files, data_stores
from vaf.core.config import Config
from vaf.core.platform import Platform
from vaf.core.session import Session

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
    """Every root of the store list in a scratch folder, the enforcement branch held open
    (an empty scratch home would flip `allow_plaintext_at_rest` process-wide, the trap
    tests/test_scheduled_store_modes.py describes), and a config of this test's own, so
    the done-marker starts unset and never reaches another test."""
    named = {name: tmp_path / name for name in ("app", "vaf", "data", "config", "projects")}
    monkeypatch.setattr(data_stores, "roots", lambda: named)
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: named["vaf"]))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(at_rest_migration, "_done", False)
    monkeypatch.setattr(at_rest_migration, "_any_plaintext_left", lambda trees: True)
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Config, "CONFIG_FILE", config)
    sessions = named["vaf"] / "sessions"
    sessions.mkdir(parents=True)
    return sessions


def _chat(directory, session_id, runtime_state, *, scope=OWNER_SCOPE):
    """A chat file as SessionManager writes it (encrypted), dated in the past."""
    session = Session(id=session_id, name=session_id)
    session.metadata["user_scope_id"] = scope
    session.add_message(role="user", content=f"hello from {session_id}")
    session.add_message(role="assistant", content="Gern.")
    session.runtime_state = runtime_state
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.json"
    data_files.write_bytes_atomic(path, json.dumps(session.to_dict(), indent=2).encode("utf-8"))
    os.utime(path, ns=(PAST_NS, PAST_NS))
    return path


def _read(path):
    return json.loads(data_files.read_bytes(path).decode("utf-8"))


def _start():
    at_rest_migration._done = False
    return at_rest_migration.run_once(force=True)


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
    assert Config.get("context_copy_sweep_done") is True


def test_an_archived_chat_takes_part(store):
    """An archived chat is still a chat the agent reads (memory recall) and can be brought
    back; a copy that was archived, or an origin that was, counts the same."""
    live = _chat(store, "red654321", _runtime_state("owner"))
    archived = _chat(store / "archive" / OTHER_SCOPE[:8], "green123456",
                     _runtime_state("owner"), scope=OTHER_SCOPE)

    _start()

    for path in (live, archived):
        assert "context" not in _read(path)["runtime_state"]["providers"], path.name


def test_the_pass_runs_once(store):
    _chat(store, "red654321", _runtime_state("owner"))
    _chat(store, "green123456", _runtime_state("owner"), scope=OTHER_SCOPE)
    _start()
    assert Config.get("context_copy_sweep_done") is True

    # Two chats that share a state later (a fork, a hand copy) are not this repair's business.
    later = _chat(store, "web-later-a", _runtime_state("later"))
    _chat(store, "web-later-b", _runtime_state("later"))
    report = _start()

    assert "context_copies" not in report
    assert "context" in _read(later)["runtime_state"]["providers"]


def test_a_failed_rewrite_leaves_the_pass_for_the_next_start(store, monkeypatch):
    """All or nothing per shared state. The copy is rewritten first (the files go in name
    order) and the origin's write fails: kept like that, the copy would have lost its entry
    and the origin, now unique, would never be found again."""
    copy = _chat(store, "green123456", _runtime_state("owner"), scope=OTHER_SCOPE)
    origin = _chat(store, "red654321", _runtime_state("owner"))
    raw_copy, raw_origin = copy.read_bytes(), origin.read_bytes()
    real_write = data_files.write_bytes_atomic

    def refuse_the_origin(path, data, **kw):
        if os.path.basename(str(path)) == origin.name:
            raise OSError("read-only")
        return real_write(path, data, **kw)

    monkeypatch.setattr(data_files, "write_bytes_atomic", refuse_the_origin)
    report = _start()

    assert report["context_copies"]["failed"] == 1
    assert report["context_copies"]["put_back"] == 1
    assert origin.read_bytes() == raw_origin, "a failed rewrite touched the file"
    assert copy.read_bytes() == raw_copy, "the copy was not put back, the pair is broken up"
    assert copy.stat().st_mtime_ns == PAST_NS
    assert Config.get("context_copy_sweep_done") is False, "the pass was marked done with a copy left"

    monkeypatch.setattr(data_files, "write_bytes_atomic", real_write)
    _start()
    for path in (copy, origin):
        assert "context" not in _read(path)["runtime_state"]["providers"], path.name
    assert Config.get("context_copy_sweep_done") is True


def test_a_chat_saved_since_the_scan_is_not_overwritten(store):
    """The scan reads every chat before anything is written, and a turn can save one in
    between. The rewrite reads it again and leaves a chat that no longer holds the shared
    state as it is."""
    path = _chat(store, "red654321", _runtime_state("owner"))
    key = at_rest_migration._context_state_key(_read(path))
    _chat(store, "red654321", _runtime_state("owner, then a turn of its own"))
    raw = path.read_bytes()

    result, _ = at_rest_migration._drop_context_entry(path, key)

    assert result == "changed"
    assert path.read_bytes() == raw


def test_an_unreadable_chat_is_never_written(store):
    """A chat whose key the ring does not hold must stay recoverable: written over, it would
    be lost for good."""
    _chat(store, "red654321", _runtime_state("owner"))
    _chat(store, "green123456", _runtime_state("owner"), scope=OTHER_SCOPE)
    locked = store / "web-locked.json"
    locked.write_bytes(data_files.FILE_MAGIC + b"\x00" * 64)
    raw = locked.read_bytes()

    report = _start()

    assert report["context_copies"]["unreadable"] == 1
    assert report["context_copies"]["dropped"] == 2
    assert locked.read_bytes() == raw


def test_the_marker_is_admin_only():
    """Set back to false by a non-admin, the next start would rewrite everyone's chats."""
    assert "context_copy_sweep_done" in Config.DEFAULTS
    assert Config.is_global_config_key("context_copy_sweep_done")
    assert "context_copy_sweep_done" not in Config.filter_for_non_admin(
        {"context_copy_sweep_done": False, "theme": "dark"})
