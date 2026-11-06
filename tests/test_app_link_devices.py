# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Veyllo App device store: rows, the outbox, removal in one step, the revocation
listener, and the per-device drop of queued turns."""
import json

import pytest

from tests.app_link_env import app_link_home  # noqa: F401  (fixture)
from vaf.core import revocation
from vaf.core.app_link import devices
from vaf.core.task_queue import TaskQueue

SCOPE_A = "0a0b0c0d-0000-4000-8000-0000000000a1"
SCOPE_B = "0a0b0c0d-0000-4000-8000-0000000000b2"


def _add(scope=SCOPE_A, spki="aa" * 32, **kw):
    return devices.add_device(user_scope_id=scope, username=kw.pop("username", "alice"),
                              spki_sha256=spki, cert_serial=kw.pop("serial", "1"),
                              model=kw.pop("model", "Pixel 9"), platform="android",
                              app_version="1.4.0", **kw)


def test_rows_chat_and_format(app_link_home):
    row = _add()
    assert devices.get(row["device_id"])["user_scope_id"] == SCOPE_A
    assert devices.by_spki("AA" * 32)["device_id"] == row["device_id"]
    sid = devices.chat_session_id(SCOPE_A)
    assert sid.startswith("veyllo_app_") and SCOPE_A not in sid
    assert devices.chat_session_id(SCOPE_A) == sid
    assert devices.scope_for_chat(sid) == SCOPE_A
    assert devices.chat_session_id(SCOPE_B) != sid
    with pytest.raises(ValueError):
        _add(spki="aa" * 32)
    assert devices.resolve(row["device_id"][:8])["device_id"] == row["device_id"]
    assert devices.resolve(row["device_id"][:8], SCOPE_B) is None


def test_a_legacy_file_is_read_and_a_foreign_one_trusts_nobody(app_link_home):
    path = devices._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {"devices": [{"device_id": "d1", "user_scope_id": SCOPE_A, "spki_sha256": "bb"}],
              "chats": {SCOPE_A: "abc"}}
    path.write_text(json.dumps(legacy), encoding="utf-8")
    assert devices.get("d1") is not None
    path.write_text(json.dumps({**legacy, "format": "somebody-else-1"}), encoding="utf-8")
    assert devices.get("d1") is None
    path.unlink()
    _add()
    from vaf.core import data_files
    assert data_files.read_json(path)["format"] == "applink-devices-1-06c31c"


def test_stands_needs_account_lane_and_recent_use(app_link_home, monkeypatch):
    row = _add()
    assert devices.stands(row["device_id"])
    with monkeypatch.context() as m:
        m.setattr(revocation, "account_stands", lambda scope: False)
        assert not devices.stands(row["device_id"])
    app_link_home.lane(SCOPE_A, False)
    assert not devices.stands(row["device_id"])
    app_link_home.lane(SCOPE_A, True)
    stale = dict(devices.get(row["device_id"]), last_seen_at=0, created_at=0)
    assert not devices.row_stands(stale)
    assert devices.expire_idle(now=10**12) == 1
    assert devices.get(row["device_id"]) is None


def test_outbox_cap_age_and_ack_per_device(app_link_home):
    one = _add()
    two = _add(spki="cc" * 32)
    for i in range(201):
        devices.outbox_add(one["device_id"], f"m{i}", ts=1000.0 + i)
    devices.outbox_add(two["device_id"], "m0", ts=1000.0)
    due, dropped = devices.outbox_due(one["device_id"], now=1300.0)
    assert len(due) == 200 and due[0]["id"] == "m1" and dropped == 1
    assert devices.take_dropped(one["device_id"]) == 1
    assert devices.outbox_due(one["device_id"], now=1300.0)[1] == 0
    devices.outbox_ack(one["device_id"], "m1")
    assert devices.outbox_due(one["device_id"], now=1300.0)[0][0]["id"] == "m2"
    # An ack of device one leaves device two's outbox alone.
    devices.outbox_ack(one["device_id"], "m0")
    assert [e["id"] for e in devices.outbox_due(two["device_id"], now=1300.0)[0]] == ["m0"]
    due, dropped = devices.outbox_due(one["device_id"], now=1000.0 + 7 * 86400 + 150)
    assert due and due[0]["id"] == "m150" and dropped == 148


def test_caps_replace(app_link_home):
    row = _add()
    devices.set_caps(row["device_id"], ["calendar", "contacts"])
    devices.set_caps(row["device_id"], ["location"])
    assert devices.get(row["device_id"])["caps"] == ["location"]


def test_remove_in_one_step(app_link_home):
    row = _add(relay_member="0f8fad5b-d9cb-469f-a165-70867728950e")
    closed = []
    devices.add_removed_listener(closed.append)
    try:
        assert devices.remove_device(row["device_id"], by="alice", reason="lost") is True
    finally:
        devices.remove_removed_listener(closed.append)
    assert devices.get(row["device_id"]) is None
    assert closed == [row["device_id"]]
    assert devices.tombstones() == ["0f8fad5b-d9cb-469f-a165-70867728950e"]
    event = app_link_home.events[-1]
    assert event[0] == "channel_unpaired" and event[1]["channel"] == "veyllo_app"
    assert event[1]["path"] == row["device_id"] and "lost" in event[1]["detail"]
    devices.drop_tombstone("0f8fad5b-d9cb-469f-a165-70867728950e")
    assert devices.tombstones() == []
    assert devices.remove_device(row["device_id"], by="alice", reason="again") is False


def test_queued_turns_dropped_per_device(app_link_home):
    one = _add()
    two = _add(spki="cc" * 32)
    sid = devices.chat_session_id(SCOPE_A)
    queue = TaskQueue()
    queue.add(sid, "from one", source="veyllo_app", metadata={"app_device_id": one["device_id"]})
    queue.add(sid, "from two", source="veyllo_app", metadata={"app_device_id": two["device_id"]})
    queue.add("web_chat-1", "web", source="web")
    devices.remove_device(one["device_id"], by="alice", reason="lost")
    assert queue.has_queued(sid)
    left = [t.input_text for t in queue._queued_by_session[sid]]
    assert left == ["from two"]
    assert queue.has_queued("web_chat-1")
    taken = queue.get(timeout=0.1, worker_id="w1")
    seen = {taken.input_text}
    queue.task_done(worker_id="w1")
    taken = queue.get(timeout=0.1, worker_id="w1")
    seen.add(taken.input_text)
    assert seen == {"from two", "web"}


def test_revoke_listener_removes_and_narrowing_keeps(app_link_home):
    _add()
    _add(scope=SCOPE_B, spki="dd" * 32, username="bob")
    revocation.stop_account_work(SCOPE_A)
    assert len(devices.for_scope(SCOPE_A)) == 1
    revocation.revoke_account(SCOPE_A)
    try:
        assert devices.for_scope(SCOPE_A) == []
        assert len(devices.for_scope(SCOPE_B)) == 1
    finally:
        revocation.restore_account(SCOPE_A)
