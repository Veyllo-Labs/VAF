# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One request file from a process without the bridge is sent once, however many sender loops
drain the request folder.

THE DEFECT, measured on the code before this change: the consumer read a request file and only
then unlinked it, so two loops could both read one file before either unlinked it. Two threads
draining three files per round handed out 410 of 600 files twice. Two loops ran at once after a
failed bridge thread, whose sender loop kept polling next to the next run's.

THE SECOND DEFECT, on the Windows CI job: the claim was a rename, and a rename is no mutex there
(MoveFileEx opens the source by name, then renames by handle). Requests vanished without being
handed out and without a file left behind (11 of 600 here, 2 of 20 in a fuzz draw). With Windows
file semantics emulated (tests/windows_file_semantics.py) the rename claim lost 329 of 600.

THE RULE: a consumer claims a file with an exclusive create of `<name>.json.claim` before it reads
it (`_claim_request_file`), unlinks the request before it drops the marker, and a loop whose run
has ended claims nothing. The concurrency tests run twice: as they are, and under the emulated
Windows semantics with a seeded pause inside every rename.

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

import windows_file_semantics
from vaf.api import whatsapp_bridge as wa
from vaf.core.platform import Platform

# A rename's hold between its open and its move, in the emulated Windows mode.
RENAME_PAUSES = (0.0, 0.0002, 0.001)


@pytest.fixture
def ipc(tmp_path, monkeypatch):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    wa._ensure_ipc_dirs()
    return wa._ipc_requests_dir()


def _seeded_pause(seed):
    rng = random.Random(seed)
    lock = threading.Lock()

    def pause(_src):
        with lock:
            s = rng.choice(RENAME_PAUSES)
        if s:
            time.sleep(s)
    return pause


@pytest.fixture(params=["plain", "windows"])
def mode(request, tmp_path, monkeypatch):
    """`windows`: the files under tmp_path behave as on Windows, every rename pausing for a
    seeded while between taking the file and moving it."""
    if request.param == "windows":
        seed = int(os.environ.get("VAF_FUZZ_SEED", "20261121"))
        windows_file_semantics.install(monkeypatch, tmp_path, pause=_seeded_pause(seed))
    return request.param


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
    return sorted(p.name for p in folder.iterdir() if p.name.endswith((".json", ".claim", ".claimed")))


def test_two_consumers_never_take_one_request_file(ipc, mode):
    """MUTATION: read then unlink, without the claim (the old consumer) -> files handed out
    twice. The rename claim back in `_claim_request_file` -> the windows mode loses files."""
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
    """The claim is the marker: while another consumer's `<name>.json.claim` stands, the file is
    not ours, and the glob does not match the marker. MUTATION: create the marker without
    `O_EXCL`."""
    _write_request("r1")
    (found,) = ipc.glob("*.json")
    marker = wa._claim_request_file(found)
    assert marker is not None and marker.name == found.name + ".claim"
    assert found.exists() and marker.exists()
    assert wa._claim_request_file(found) is None
    assert wa._dequeue_external_send_request() is None
    assert found.exists() and marker.exists()


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


def test_a_file_claimed_as_the_run_ends_is_handed_back(ipc, monkeypatch):
    """A loop that passed its stop check and claims a file just as its run ends drops its claim
    and leaves the request untouched for the next run's loop, instead of taking a request it
    will never send. MUTATION: keep the marker after a stop that came with the claim."""
    _write_request("for-the-next-run")
    (found,) = ipc.glob("*.json")
    before = found.read_bytes()
    stop = threading.Event()
    real_claim = wa._claim_request_file

    def claim_as_the_run_ends(path):
        claimed = real_claim(path)
        stop.set()
        return claimed
    monkeypatch.setattr(wa, "_claim_request_file", claim_as_the_run_ends)
    assert wa._dequeue_external_send_request(stop) is None
    assert _leftovers(ipc) == [found.name]
    assert found.read_bytes() == before
    monkeypatch.setattr(wa, "_claim_request_file", real_claim)
    item = wa._dequeue_external_send_request(threading.Event())
    assert item is not None and item[4] == "for-the-next-run"


def test_a_failed_producer_ends_the_schedule(ipc, monkeypatch):
    """The fuzz's own harness: a producer that raises still releases its consumers, and the
    error reaches the test. MUTATION: set `produced` only after a successful run."""
    import sys
    before = set(threading.enumerate())
    calls = []

    def fails_on_the_third(req_id):
        calls.append(req_id)
        if len(calls) == 3:
            raise OSError("disk full")
        _write_request_real(req_id)
    module = sys.modules[__name__]
    _write_request_real = module._write_request
    monkeypatch.setattr(module, "_write_request", fails_on_the_third)
    rng = random.Random(1)
    rng.randint = lambda a, b: 2 if (a, b) == (2, 4) else 10
    with pytest.raises(OSError, match="disk full"):
        _claim_schedule(rng)
    left = [t for t in threading.enumerate() if t not in before and t.is_alive()]
    assert left == []


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
    failed = []

    def producer():
        # `produced` in any case: consumers waiting for it would otherwise poll forever and,
        # as non-daemon threads, hold the interpreter open.
        try:
            for gap in gaps:
                req_id = uuid.uuid4().hex
                _write_request(req_id)
                written.append(req_id)
                time.sleep(gap)
        except BaseException as e:  # noqa: BLE001 - raised again in the test thread
            failed.append(e)
        finally:
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
    if failed:
        raise failed[0]
    assert not any(t.is_alive() for t in threads)
    return written, taken


def test_claim_fuzz(ipc, mode):
    """Seeded draws of concurrent consumers against a producer, in both modes. Oracle: every
    req_id written is handed out exactly once, and the folder holds no `.json`, `.claim` or
    `.claimed` file at the end. `VAF_FUZZ_DRAWS` raises the draw count outside the suite.
    MUTATION: the rename claim back in `_claim_request_file` -> the windows mode loses requests
    without a trace (`taken` < `written`, nothing left)."""
    draws = int(os.environ.get("VAF_FUZZ_DRAWS", "25"))
    seed = int(os.environ.get("VAF_FUZZ_SEED", "20261121"))
    master = random.Random(seed)
    failures = []
    for draw in range(draws):
        written, taken = _claim_schedule(random.Random(master.random()))
        left = _leftovers(ipc)
        if sorted(taken) != sorted(written) or left:
            failures.append((draw, len(written), len(taken), len(set(taken)), left[:3]))
    assert failures == [], f"seed {seed} ({mode}): {failures[:5]}"


def _unreadable_files_then_a_good_one(ipc):
    """Two files that are not a request, named to sort before any real `time_ns()` reading, and
    a good one after them. Fixed names, not two clock readings: on Windows before Python 3.13
    `time.time_ns()` ticks every 15.625 ms, so three readings in a row are usually equal."""
    (ipc / "0000000000000000001_bad.json").write_text("{not json", encoding="utf-8")
    (ipc / "0000000000000000002_list.json").write_text(json.dumps([1, 2]), encoding="utf-8")
    _write_request("good")
    item = wa._dequeue_external_send_request()
    assert item is not None and item[4] == "good"
    assert _leftovers(ipc) == []


def test_an_unreadable_request_file_leaves_nothing_behind(ipc):
    """A file that is not JSON, or not an object, is removed with its marker and the next file
    is served. MUTATION: skip such a file without unlinking it (it stays behind)."""
    _unreadable_files_then_a_good_one(ipc)


def test_the_request_order_does_not_depend_on_the_clock(ipc, monkeypatch):
    """The same, with every `time.time_ns()` reading inside one 15.625 ms tick, as on the
    Windows runner. MUTATION: name the list file `f"{time.time_ns() + 1}_list.json"` again ->
    it sorts after the good request and stays behind."""
    tick = 1_795_248_375_788_482_000 // 15_625_000 * 15_625_000
    monkeypatch.setattr(time, "time_ns", lambda: tick)
    _unreadable_files_then_a_good_one(ipc)


# -- After the claim: a read or an unlink that Windows refuses -------------------------------------

def test_a_request_that_cannot_be_read_yet_waits_for_the_next_pass(ipc, tmp_path, monkeypatch):
    """Another process holds the request while the claim's holder reads it (here a rename that
    is between taking the file and moving it, the hold Windows refuses an open during): the
    request stays, the marker goes, no result is written, and the next pass hands it out.
    MUTATION: delete the file when its read raises (the old `except Exception` branch)."""
    taken, release = threading.Event(), threading.Event()

    def hold(_src):
        taken.set()
        release.wait(10)
    seam = windows_file_semantics.install(monkeypatch, tmp_path, pause=hold)
    result = tmp_path / "data" / "result.json"
    wa._write_json_atomic(ipc / "0000000000000000001_r1.json", {
        "req_id": "r1", "username": "alice", "chat_jid": "491700000042@s.whatsapp.net",
        "text": "hi", "response_path": str(result)})
    (found,) = ipc.glob("*.json")
    occupied = ipc / "occupied"
    occupied.write_bytes(b"")
    errors = []

    def holder():
        try:
            os.rename(found, occupied)
        except FileExistsError:
            pass
        except BaseException as e:  # noqa: BLE001 - reported in the test thread
            errors.append(e)
    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert taken.wait(10)
    try:
        assert wa._dequeue_external_send_request() is None
        assert seam.refusals >= 1
        assert _leftovers(ipc) == [found.name]
        assert not result.exists()
    finally:
        release.set()
        t.join(10)
    assert errors == []
    item = wa._dequeue_external_send_request()
    assert item is not None and item[4] == "r1"
    assert _leftovers(ipc) == []


def test_a_request_whose_unlink_is_refused_is_never_handed_out_twice(ipc, tmp_path, monkeypatch):
    """The claim's holder read the request, but a Windows holder (Defender, the indexer) refuses
    its unlink: the request is handed out once, its marker stays as the record, and no later
    pass hands it out again. MUTATION: drop the marker although the unlink failed."""
    seam = windows_file_semantics.install(monkeypatch, tmp_path)
    _write_request("once")
    (found,) = ipc.glob("*.json")
    with found.open("rb"):
        item = wa._dequeue_external_send_request()
        assert item is not None and item[4] == "once"
        assert seam.refusals >= 1
        assert wa._dequeue_external_send_request() is None
    assert wa._dequeue_external_send_request() is None
    assert _leftovers(ipc) == sorted([found.name, found.name + ".claim"])
