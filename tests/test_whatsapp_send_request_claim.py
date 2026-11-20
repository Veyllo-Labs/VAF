# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One request file from a process without the bridge is sent once, however many sender loops
drain the request folder.

THE DEFECT, measured on the code before this change: the consumer read a request file and only
then unlinked it, so two loops could both read one file before either unlinked it. Two threads
draining three files per round handed out 410 of 600 files twice. Two loops ran at once after a
failed bridge thread, whose sender loop kept polling next to the next run's.

THE RULE: a consumer claims a file by renaming it before it reads it (`_claim_request_file`), and
a loop whose run has ended claims nothing.

Files in a scratch data directory only: no bridge, no Node.
"""
import json
import os
import queue
import random
import threading
import time
import uuid

import pytest

from vaf.api import whatsapp_bridge as wa
from vaf.core.platform import Platform


@pytest.fixture
def ipc(tmp_path, monkeypatch):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    wa._ensure_ipc_dirs()
    return wa._ipc_requests_dir()


def _write_request(req_id):
    """A request file as `send_whatsapp_with_confirmation` writes it (temp name, then replace)."""
    wa._write_json_atomic(wa._ipc_requests_dir() / f"{time.time_ns()}_{req_id}.json", {
        "req_id": req_id, "username": "alice", "chat_jid": "491700000042@s.whatsapp.net",
        "text": "hi", "voice_path": "", "document_path": "", "response_path": "",
        "origin": "", "user_scope_id": "ab12cd34-0000-4000-8000-000000000001",
    })


def _drain(taken):
    """Dequeue until the folder answers nothing; every req_id handed out goes to `taken`."""
    while True:
        item = wa._dequeue_external_send_request()
        if item is None:
            return
        taken.append(item[4])


def _leftovers(folder):
    return sorted(p.name for p in folder.iterdir() if p.name.endswith((".json", ".claimed")))


def test_two_consumers_never_take_one_request_file(ipc):
    """MUTATION: read then unlink, without the claim (the old consumer) -> files handed out
    twice."""
    taken = []
    lock = threading.Lock()
    written = []
    for _ in range(200):
        ids = [uuid.uuid4().hex for _ in range(3)]
        for req_id in ids:
            _write_request(req_id)
        written.extend(ids)
        barrier = threading.Barrier(2)

        def consumer():
            mine = []
            barrier.wait()
            _drain(mine)
            with lock:
                taken.extend(mine)

        threads = [threading.Thread(target=consumer) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not any(t.is_alive() for t in threads)
    twice = len(taken) - len(set(taken))
    assert twice == 0, f"{twice} of {len(written)} request files handed out twice"
    assert sorted(taken) == sorted(written)
    assert _leftovers(ipc) == []


def test_a_claimed_file_is_not_taken_again(ipc):
    """The claim is the rename: a file another consumer renamed first is not ours, and the
    glob does not match the claimed name. MUTATION: `_claim_request_file` returns the path
    unrenamed."""
    _write_request("r1")
    (found,) = ipc.glob("*.json")
    claimed = wa._claim_request_file(found)
    assert claimed is not None and claimed.name.startswith(found.name) and claimed.name.endswith(".claimed")
    assert not found.exists() and claimed.exists()
    assert wa._claim_request_file(found) is None
    assert wa._dequeue_external_send_request() is None


def test_an_ended_runs_sender_loop_claims_nothing(ipc):
    """A loop whose run has ended exits between items and leaves a waiting request file for the
    next run. MUTATION: the loop claims before it looks at its stop."""
    _write_request("waiting")
    stop = threading.Event()
    stop.set()
    loop = threading.Thread(target=wa._sender_loop, kwargs={"q": queue.Queue(), "stop": stop}, daemon=True)
    loop.start()
    loop.join(timeout=2.5)
    assert not loop.is_alive()
    assert [p.name.endswith("_waiting.json") for p in ipc.iterdir()] == [True]


# -- Fuzz: the claim ------------------------------------------------------------------------------

def _claim_schedule(rng):
    """2 to 4 consumers drain while one producer writes 1 to 30 files at 0 to 2 ms gaps.
    Returns (written, taken)."""
    consumers = rng.randint(2, 4)
    count = rng.randint(1, 30)
    gaps = [rng.uniform(0, 0.002) for _ in range(count)]
    written, taken = [], []
    lock = threading.Lock()
    produced = threading.Event()

    def producer():
        for gap in gaps:
            req_id = uuid.uuid4().hex
            _write_request(req_id)
            written.append(req_id)
            time.sleep(gap)
        produced.set()

    def consumer():
        mine = []
        while True:
            done = produced.is_set()
            _drain(mine)
            if done:
                break
            time.sleep(0.0005)
        with lock:
            taken.extend(mine)

    threads = [threading.Thread(target=producer)] + [threading.Thread(target=consumer) for _ in range(consumers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads)
    return written, taken


def test_claim_fuzz(ipc):
    """Seeded draws of concurrent consumers against a producer. Oracle: every req_id written is
    handed out exactly once, and the folder holds no `.json` and no `.claimed` file at the end.
    `VAF_FUZZ_DRAWS` raises the draw count outside the suite."""
    draws = int(os.environ.get("VAF_FUZZ_DRAWS", "25"))
    seed = int(os.environ.get("VAF_FUZZ_SEED", "20261121"))
    master = random.Random(seed)
    failures = []
    for draw in range(draws):
        written, taken = _claim_schedule(random.Random(master.random()))
        left = _leftovers(ipc)
        if sorted(taken) != sorted(written) or left:
            failures.append((draw, len(written), len(taken), len(set(taken)), left[:3]))
    assert failures == [], f"seed {seed}: {failures[:5]}"


def test_an_unreadable_request_file_leaves_nothing_behind(ipc):
    """The claim keeps the old error handling, on the claimed file: one that is not JSON, or
    not an object, is removed and the next file is served. MUTATION: the error path unlinks
    the unclaimed name only (the claimed file stays behind)."""
    (ipc / f"{time.time_ns()}_bad.json").write_text("{not json", encoding="utf-8")
    (ipc / f"{time.time_ns() + 1}_list.json").write_text(json.dumps([1, 2]), encoding="utf-8")
    _write_request("good")
    item = wa._dequeue_external_send_request()
    assert item is not None and item[4] == "good"
    assert _leftovers(ipc) == []
