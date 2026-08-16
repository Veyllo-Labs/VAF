# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Whose turn produced this log line.

Eight places in the tree write timeline events and exactly ONE of them ever
passed a scope, so the activity log could not be read per user at all. The scope
is now learned where identity is bound and stamped by the writer.

Two properties decide whether that is trustworthy rather than merely present:
the stamp must land BEFORE the hash (the chain covers every other field, and a
field added afterwards would break verification on a file nobody touched), and
an unbound turn must produce an UNATTRIBUTED record rather than inherit the
previous tenant's scope - one agent object serves many queued turns, and the
wrong name on an audit line is worse than no name.
"""
import json

import pytest

from vaf.core import log_helper as lh

ALICE = "aaaaaaaa-1111-2222-3333-444444444444"
BOB = "bbbbbbbb-1111-2222-3333-444444444444"


@pytest.fixture
def timeline(tmp_path, monkeypatch):
    monkeypatch.setattr(lh, "is_debug_logging_enabled", lambda: True)
    monkeypatch.setattr(lh, "get_app_log_dir", lambda: tmp_path)
    lh.set_log_scope(None)
    yield tmp_path
    lh.set_log_scope(None)


def _rows(tmp_path):
    path = next(tmp_path.glob("timeline_*.jsonl"))
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _chain_verifies(rows):
    prev = ""
    for r in rows:
        body = {k: v for k, v in r.items() if k != "hash"}
        if lh._timeline_hash(body) != r.get("hash"):
            return False
        if prev and r.get("prev_hash") != prev:
            return False
        prev = r["hash"]
    return True


def test_a_bound_turn_stamps_every_event_type(timeline):
    """tool_end, the sub-agent pair and the training pair never carried a scope;
    that is what made a per-user reading impossible."""
    lh.set_log_scope(ALICE)
    lh.log_timeline_event("tool_start", tool="read_file", call_id="1")
    lh.log_timeline_event("tool_end", tool="read_file", call_id="1", status="ok")
    lh.log_timeline_event("subagent_start", tool="coder", task_id="t1")
    rows = _rows(timeline)
    assert [r["type"] for r in rows] == ["tool_start", "tool_end", "subagent_start"]
    assert all(r.get("scope") == ALICE for r in rows)


def test_an_unbound_turn_leaves_the_record_unattributed(timeline):
    lh.log_timeline_event("tool_start", tool="cron_tick", call_id="9")
    assert "scope" not in _rows(timeline)[0]


def test_clearing_the_scope_does_not_inherit_the_previous_turn(timeline):
    """The failure this guards: one agent object serving a queue, stamping the
    last person's identity onto the next person's work."""
    lh.set_log_scope(ALICE)
    lh.log_timeline_event("tool_start", tool="a", call_id="1")
    lh.set_log_scope(None)
    lh.log_timeline_event("tool_start", tool="b", call_id="2")
    rows = _rows(timeline)
    assert rows[0]["scope"] == ALICE
    assert "scope" not in rows[1]


def test_a_rebound_scope_replaces_the_old_one(timeline):
    lh.set_log_scope(ALICE)
    lh.log_timeline_event("tool_start", tool="a", call_id="1")
    lh.set_log_scope(BOB)
    lh.log_timeline_event("tool_start", tool="b", call_id="2")
    assert [r["scope"] for r in _rows(timeline)] == [ALICE, BOB]


def test_an_explicit_scope_from_the_caller_still_wins(timeline):
    lh.set_log_scope(ALICE)
    lh.log_timeline_event("tool_start", tool="a", call_id="1", scope=BOB)
    assert _rows(timeline)[0]["scope"] == BOB


def test_the_stamp_lands_before_the_hash(timeline):
    """The trap: the chain covers every field except `hash`, and the reader
    recomputes it. Stamping afterwards would paint the audit badge red on an
    intact file."""
    lh.set_log_scope(ALICE)
    lh.log_timeline_event("tool_start", tool="a", call_id="1")
    lh.log_timeline_event("tool_end", tool="a", call_id="1", status="ok")
    lh.set_log_scope(None)
    lh.log_timeline_event("tool_start", tool="b", call_id="2")
    rows = _rows(timeline)
    assert _chain_verifies(rows)
    assert lh._timeline_hash({k: v for k, v in rows[0].items() if k != "hash"}) == rows[0]["hash"]


def test_the_reader_agrees_that_the_chain_is_intact(timeline):
    """Verified with the API's own verifier, not just a local recomputation."""
    from vaf.api.logs_routes import _verify_chain

    lh.set_log_scope(ALICE)
    for i in range(3):
        lh.log_timeline_event("tool_start", tool=f"t{i}", call_id=str(i))
    assert _verify_chain(_rows(timeline)) is True


def test_binding_an_identity_binds_the_log_scope(timeline):
    """The wiring, not just the stage: without this the writer would be correct
    and still never learn anything."""
    from vaf.core.identity_binding import Identity, bind_identity

    class _Agent:
        pass

    bind_identity(_Agent(), Identity(scope=ALICE, username="alice", role="user"))
    assert lh.current_log_scope() == ALICE
    lh.log_timeline_event("tool_start", tool="a", call_id="1")
    assert _rows(timeline)[0]["scope"] == ALICE


def test_binding_an_identity_without_a_scope_clears_it(timeline):
    from vaf.core.identity_binding import Identity, bind_identity

    class _Agent:
        pass

    lh.set_log_scope(ALICE)
    bind_identity(_Agent(), Identity(scope=None, username=None, role=None))
    assert lh.current_log_scope() == ""
