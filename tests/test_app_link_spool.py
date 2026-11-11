# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A file sent while the Veyllo App is away reaches it later (vaf/core/app_link/spool.py).

Live: a PDF the agent sent while the app had no session reached it as its `[File: ...]` line
only, because the outbox kept message ids and the file went only to the devices connected at
that moment. Now every file a message carries is spooled, sealed, and replayed with its
message after the device's `welcome`, within per-account and per-device limits.
"""
import hashlib
import time

import pytest

from app_link_client import link, paired_device
from app_link_env import SCOPE_A, SCOPE_B, app_link_home  # noqa: F401  (fixture)
from vaf.core.app_link import bridge, devices, pairing, spool
from vaf.core.app_link import protocol as pr

PDF = b"%PDF-1.7 the BAFA overview " * 4000


@pytest.fixture(autouse=True)
def _fresh():
    bridge.reset()
    pairing.reset()
    yield
    bridge.reset()


def _file(tmp_path, name="Uebersicht.pdf", data=PDF):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _blob(mid):
    return spool._dir() / f"{mid}.bin"


def _file_msgs(client):
    return [m for m in client.of("msg") if m.get("attachments")]


def test_a_file_sent_while_away_arrives_after_welcome(app_link_home, tmp_path):
    """MUTATION: skip the spool branch in `replay` (`_replay_file` answers None) - red."""
    device = paired_device(SCOPE_A)
    path = _file(tmp_path)
    assert bridge.deliver(SCOPE_A, "alice", "Here is the overview.", str(path)) is True
    path.unlink()                         # the original is gone; the spool holds the bytes
    client, _pipe, _row = link(SCOPE_A, device=device)
    msgs = client.of("msg")
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg["text"] == "Here is the overview."
    assert [{k: v for k, v in a.items() if k != "xfer"} for a in msg["attachments"]] == [
        {"name": "Uebersicht.pdf", "mime": "application/pdf", "size": len(PDF),
         "sha256": hashlib.sha256(PDF).hexdigest()}]
    xfer = pr.b64url_decode(msg["attachments"][0]["xfer"])
    assert len(msg["attachments"][0]["xfer"]) == 22
    assert all(c.xfer == xfer for c in client.chunks)
    assert [c.index for c in client.chunks] == list(range(len(client.chunks)))
    assert b"".join(c.data for c in client.chunks) == PDF


def test_a_message_that_is_only_a_file_replays_with_no_text(app_link_home, tmp_path):
    device = paired_device(SCOPE_A)
    bridge.deliver(SCOPE_A, "alice", "", str(_file(tmp_path, data=b"x" * 10)))
    client, _pipe, _row = link(SCOPE_A, device=device)
    assert client.of("msg")[0]["text"] == "" and b"".join(c.data for c in client.chunks) == b"x" * 10


def test_the_spooled_file_is_sealed(app_link_home, tmp_path):
    """MUTATION: write the blob with `Path.write_bytes` - red."""
    from vaf.core import data_files
    paired_device(SCOPE_A)
    bridge.deliver(SCOPE_A, "alice", "see", str(_file(tmp_path)))
    mid = devices.all_devices()[0]["outbox"][0]["id"]
    blob = _blob(mid)
    assert blob.parent.name == "app_link_spool" and blob.parent.parent == spool._dir().parent
    assert data_files.encryption_enabled()
    assert blob.read_bytes().startswith(data_files.FILE_MAGIC)
    assert PDF[:64] not in blob.read_bytes()
    record = devices._load()["spool"][mid]
    assert record["scope"] == SCOPE_A and record["name"] == "Uebersicht.pdf"
    assert record["size"] == len(PDF) and record["sha256"] == hashlib.sha256(PDF).hexdigest()


def test_the_eleventh_file_evicts_the_oldest(app_link_home, tmp_path):
    """MUTATION: `FILES_PER_ACCOUNT = 11`, or evict across every account - red."""
    device = paired_device(SCOPE_A)
    other = paired_device(SCOPE_B, username="bob")
    bridge.deliver(SCOPE_B, "bob", "bob's file", str(_file(tmp_path, "bob.txt", b"bob")))
    for i in range(spool.FILES_PER_ACCOUNT + 1):
        bridge.deliver(SCOPE_A, "alice", f"file {i}", str(_file(tmp_path, f"f{i}.txt", b"%d" % i)))
        time.sleep(0.002)
    mine = [k for k, v in devices._load()["spool"].items() if v["scope"] == SCOPE_A]
    assert len(mine) == spool.FILES_PER_ACCOUNT
    client, _pipe, _row = link(SCOPE_A, device=device)
    msgs = client.of("msg")
    assert msgs[0]["text"] == "file 0\n[File: f0.txt]" and "attachments" not in msgs[0]
    assert [m["text"] for m in msgs[1:]] == [f"file {i}" for i in range(1, 11)]
    assert all(m.get("attachments") for m in msgs[1:])
    bob, _p2, _ = link(SCOPE_B, device=other, peer="10.0.0.6", connection="c2")
    assert bob.of("msg")[0]["text"] == "bob's file" and b"".join(c.data for c in bob.chunks) == b"bob"


def test_a_file_is_sent_three_times_at_most(app_link_home, tmp_path):
    """A device that never acks gets the file on three replays, then its text line.
    MUTATION: drop the `sends` check in `_replay_file` - red."""
    device = paired_device(SCOPE_A)
    bridge.deliver(SCOPE_A, "alice", "see", str(_file(tmp_path, data=b"abc")))
    seen = []
    for n in range(spool.FILE_SENDS_PER_DEVICE + 1):
        client, pipe, _row = link(SCOPE_A, device=device, peer=f"10.0.1.{n}", connection=f"c{n}")
        seen.append((client.of("msg")[0], b"".join(c.data for c in client.chunks)))
        pipe.session.close("gone", bye=False)
    for msg, data in seen[:spool.FILE_SENDS_PER_DEVICE]:
        assert msg["text"] == "see" and msg["attachments"] and data == b"abc"
    last, data = seen[-1]
    assert last["text"] == "see\n[File: Uebersicht.pdf]" and "attachments" not in last and data == b""
    assert devices.get(device[0]["device_id"])["outbox"][0]["sends"] == spool.FILE_SENDS_PER_DEVICE


def test_ack_and_sweep_collect_the_spool(app_link_home, tmp_path):
    """MUTATION: remove the `collect` call on `ack` - red."""
    one = paired_device(SCOPE_A, model="Phone")
    two = paired_device(SCOPE_A, model="Tablet")
    bridge.deliver(SCOPE_A, "alice", "see", str(_file(tmp_path)))
    mid = devices.get(one[0]["device_id"])["outbox"][0]["id"]
    c1, _p1, _ = link(SCOPE_A, device=one)
    c1.send({"t": "ack", "id": mid})
    assert _blob(mid).exists(), "the other device has not acked yet"
    c2, _p2, _ = link(SCOPE_A, device=two, peer="10.0.0.6", connection="c2")
    assert _file_msgs(c2)
    c2.send({"t": "ack", "id": mid})
    assert not _blob(mid).exists() and mid not in devices._load()["spool"]
    # A file whose device was removed goes with the hourly sweep.
    three = paired_device(SCOPE_A, model="Laptop")
    for row in (one, two):
        devices.remove_device(row[0]["device_id"], by="alice", reason="lost")
    bridge.deliver(SCOPE_A, "alice", "again", str(_file(tmp_path)))
    gone = devices.get(three[0]["device_id"])["outbox"][-1]["id"]
    devices.remove_device(three[0]["device_id"], by="alice", reason="lost")
    assert _blob(gone).exists()
    bridge.sweep(time.time() + 2 * bridge.EXPIRY_EVERY_SECONDS)
    assert not _blob(gone).exists() and devices._load()["spool"] == {}


def test_seven_days_and_orphans_are_collected(app_link_home, tmp_path):
    device = paired_device(SCOPE_A)
    bridge.deliver(SCOPE_A, "alice", "old", str(_file(tmp_path)))
    mid = devices.get(device[0]["device_id"])["outbox"][0]["id"]
    assert spool.collect() == 0 and _blob(mid).exists()
    assert spool.collect(time.time() + 8 * 86400) == 1 and not _blob(mid).exists()
    stray = _blob("0f8fad5b-d9cb-469f-a165-70867728950e")
    stray.write_bytes(b"no record")
    assert spool.collect() == 0, "a blob younger than the grace may be a message still being stored"
    import os
    old = time.time() - spool.ORPHAN_GRACE_SECONDS - 5
    os.utime(stray, (old, old))
    assert spool.collect() == 1 and not stray.exists()


def test_a_damaged_blob_falls_back_to_the_marker(app_link_home, tmp_path):
    """MUTATION: skip the sha256 check in `spool.read` - red."""
    from vaf.core import data_files
    device = paired_device(SCOPE_A)
    bridge.deliver(SCOPE_A, "alice", "see", str(_file(tmp_path)))
    mid = devices.get(device[0]["device_id"])["outbox"][0]["id"]
    data_files.write_bytes_atomic(_blob(mid), b"something else entirely")
    client, _pipe, _row = link(SCOPE_A, device=device)
    assert client.of("msg")[0]["text"] == "see\n[File: Uebersicht.pdf]"
    assert client.chunks == [] and mid not in devices._load()["spool"]


def test_an_entry_without_a_record_replays_as_before(app_link_home, tmp_path, monkeypatch):
    """From before this change, after a restore, or after a failed stage: the text line,
    exactly. MUTATION: always strip the marker (send the stripped text without a record) - red."""
    device = paired_device(SCOPE_A)
    monkeypatch.setattr(spool, "stage", lambda *a, **k: None)
    bridge.deliver(SCOPE_A, "alice", "see", str(_file(tmp_path)))
    assert devices._load()["spool"] == {}
    client, _pipe, _row = link(SCOPE_A, device=device)
    assert client.of("msg") and client.of("msg")[0]["text"] == "see\n[File: Uebersicht.pdf]"
    assert "attachments" not in client.of("msg")[0] and client.chunks == []


def test_a_connected_device_gets_the_file_live_and_the_spool_too(app_link_home, tmp_path):
    """A device that drops before its ack must not lose the file: it is spooled although every
    device took it at once."""
    device = paired_device(SCOPE_A)
    client, pipe, _row = link(SCOPE_A, device=device)
    bridge.deliver(SCOPE_A, "alice", "now", str(_file(tmp_path, data=b"live")))
    client.pump()
    assert b"".join(c.data for c in client.chunks) == b"live"
    mid = client.of("msg")[-1]["id"]
    assert _blob(mid).exists()
    pipe.session.close("dropped", bye=False)
    again, _p2, _ = link(SCOPE_A, device=device, peer="10.0.0.7", connection="c7")
    assert again.of("msg")[0]["id"] == mid and again.of("msg")[0]["attachments"]
    assert b"".join(c.data for c in again.chunks) == b"live"


def test_a_device_reconnecting_while_the_file_is_kept_still_gets_it(app_link_home, tmp_path,
                                                                     monkeypatch):
    """A device that connects, takes its replay, acks it and drops again while the file's
    bytes are still being written must not end up with the text line only: the message and
    its file become visible to a replay in one write. MUTATION: publish the outbox entries
    in `_store_out` before staging the file (record added afterwards) - red."""
    from vaf.core import data_files
    device = paired_device(SCOPE_A)
    write = data_files.write_bytes_atomic
    during = {}

    def _write(path, data, *a, **k):
        if spool._dir() in getattr(path, "parents", ()) and "client" not in during:
            client, pipe, _row = link(SCOPE_A, device=device, peer="10.0.0.8", connection="c8")
            during["client"] = client
            for msg in client.of("msg"):
                client.send({"t": "ack", "id": msg["id"]})
            pipe.session.close("dropped", bye=False)
        return write(path, data, *a, **k)
    monkeypatch.setattr(data_files, "write_bytes_atomic", _write)
    assert bridge.deliver(SCOPE_A, "alice", "see", str(_file(tmp_path, data=b"raced"))) is True
    assert during["client"].of("msg") == [], "nothing to replay before the message is published"
    monkeypatch.setattr(data_files, "write_bytes_atomic", write)
    spool.collect()
    again, _p2, _ = link(SCOPE_A, device=device, peer="10.0.0.9", connection="c9")
    assert _file_msgs(again) and b"".join(c.data for c in again.chunks) == b"raced"


def test_collect_never_sees_a_file_without_its_entries(app_link_home, tmp_path, monkeypatch):
    """The file's record and the outbox entries are one write of the device list.
    MUTATION: save the record in `outbox_publish` before the entries (two writes) - red."""
    paired_device(SCOPE_A)
    saves = []
    save = devices._save

    def _save(state):
        saves.append({k: dict(v) for k, v in state["spool"].items()})
        held = {e["id"] for d in state["devices"] for e in d.get("outbox") or []}
        assert set(state["spool"]) <= held, "a file record without its outbox entry"
        save(state)
    monkeypatch.setattr(devices, "_save", _save)
    bridge.deliver(SCOPE_A, "alice", "see", str(_file(tmp_path, data=b"x")))
    assert any(saves), "the record was written"


def test_an_older_device_list_without_spool_still_loads(app_link_home):
    from vaf.core import data_files
    paired_device(SCOPE_A)
    state = data_files.read_json(devices._path())
    state.pop("spool")
    data_files.write_json_atomic(devices._path(), state)
    assert devices._load()["spool"] == {} and devices.spool_get("x") is None
    state["spool"] = ["not", "a", "dict"]
    data_files.write_json_atomic(devices._path(), state)
    assert devices._load()["spool"] == {}


def test_a_file_no_device_takes_evicts_nothing(app_link_home, tmp_path, monkeypatch):
    """The account's last device stops standing while the eleventh file is being staged: that
    file goes with its bytes, and the ten files still pending for the device stay. MUTATION:
    record the spool in `outbox_publish` without checking that a device row took an entry -
    red (the oldest pending file is evicted and replays as its text line)."""
    device = paired_device(SCOPE_A)
    for i in range(spool.FILES_PER_ACCOUNT):
        bridge.deliver(SCOPE_A, "alice", f"file {i}", str(_file(tmp_path, f"f{i}.txt", b"%d" % i)))
        time.sleep(0.002)
    pending = set(devices.spool_ids())
    stage, stands = spool.stage, devices.row_stands
    staged = {}

    def _stage(message_id, path):
        staged["id"] = message_id
        record = stage(message_id, path)
        monkeypatch.setattr(devices, "row_stands", lambda *a, **k: False)
        return record
    monkeypatch.setattr(spool, "stage", _stage)
    bridge.deliver(SCOPE_A, "alice", "late", str(_file(tmp_path, "late.txt", b"late")))
    monkeypatch.setattr(spool, "stage", stage)
    monkeypatch.setattr(devices, "row_stands", stands)
    assert set(devices.spool_ids()) == pending
    assert staged["id"] not in pending and not _blob(staged["id"]).exists()
    assert devices.outbox_publish(SCOPE_A, [], "m-none") == []
    client, _pipe, _row = link(SCOPE_A, device=device)
    msgs = client.of("msg")
    assert [m["text"] for m in msgs] == [f"file {i}" for i in range(spool.FILES_PER_ACCOUNT)]
    assert all(m.get("attachments") for m in msgs)
