# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The session daemon: one held connection, mirrored to files.

Everything here runs against a scripted room - no sockets, no server. What the
tests pin is the contract the other commands rely on: frames land in the inbox
exactly once, a failed send keeps its payload, one room has one session, a dead
session's lock does not need a human, and the connection is closed on every
exit path, because the clean close is what frees the writer lease promptly.
"""
import json
import os

import pytest

import vaf.core.a2a.session as sess


@pytest.fixture
def paths(tmp_path, monkeypatch):
    import vaf.core.platform as plat
    monkeypatch.setattr(plat.Platform, "vaf_dir", classmethod(lambda cls: tmp_path))
    return sess.session_paths("room-test")


class ScriptedRoom:
    """A RemoteRoom stand-in: yields scripted messages, records submissions."""

    def __init__(self, script):
        self.script = list(script)   # list of rounds; each round is a list of messages
        self.submitted = []
        self.closed = False

    def frames(self, timeout=None):
        if not self.script:
            raise TimeoutError()
        round_ = self.script.pop(0)
        for message in round_:
            yield message

    def submit(self, payload, timeout=None):
        self.submitted.append(payload)
        return {"kind": "ack", "status": "committed", "seq": len(self.submitted)}

    def close(self):
        self.closed = True


def _msg(kind, lamport, id_=None, sender="p-x"):
    return {"v": 1, "id": id_ or f"m-{lamport}", "room": "room-test", "seq": lamport,
            "lamport": lamport, "ts": 0.0, "from": sender, "role": "peer",
            "to": {"room": True}, "kind": kind, "body": {"text": f"t{lamport}"}}


def test_frames_are_mirrored_and_transport_noise_is_not(paths):
    room = ScriptedRoom([[_msg("say", 1), {"kind": "ack"}, {"kind": "welcome"},
                          _msg("report", 2), {"kind": "sync"}]])
    code = sess.run_session("room-test", {"url": "wss://x", "seat": "s"},
                            connect=lambda: room, once=True, idle_s=0)
    assert code == 0
    lines = [json.loads(l) for l in paths.inbox.read_text().splitlines()]
    assert [l["kind"] for l in lines] == ["say", "report"]
    assert room.closed, "the clean close is what frees the lease - it must always run"


def test_outbox_payloads_are_sent_and_acked(paths):
    (paths.outbox / "001.json").write_text(json.dumps(
        {"kind": "say", "body": {"text": "hello"}}), encoding="utf-8")
    room = ScriptedRoom([[{"kind": "sync"}]])
    sess.run_session("room-test", {"url": "wss://x", "seat": "s"},
                     connect=lambda: room, once=True, idle_s=0)
    assert [p["body"]["text"] for p in room.submitted] == ["hello"]
    assert not (paths.outbox / "001.json").exists()
    assert (paths.outbox / "001.ack").exists()


def test_a_failed_send_keeps_the_payload_for_retry(paths):
    """The send did not happen, so losing the file would lose the message."""
    (paths.outbox / "001.json").write_text(json.dumps(
        {"kind": "say", "body": {"text": "keep me"}}), encoding="utf-8")

    class Refusing(ScriptedRoom):
        def submit(self, payload, timeout=None):
            raise ConnectionError("wire gone")

    room = Refusing([[{"kind": "sync"}]])
    code = sess.run_session("room-test", {"url": "wss://x", "seat": "s"},
                            connect=lambda: room, once=True, idle_s=0)
    assert code == 1
    assert (paths.outbox / "001.json").exists(), "a failed send must keep its payload"
    assert "wire gone" in (paths.outbox / "001.error").read_text()
    assert room.closed


def test_an_unreadable_payload_is_moved_aside_not_retried_forever(paths):
    (paths.outbox / "001.json").write_text("{not json", encoding="utf-8")
    room = ScriptedRoom([[{"kind": "sync"}]])
    sess.run_session("room-test", {"url": "wss://x", "seat": "s"},
                     connect=lambda: room, once=True, idle_s=0)
    assert not (paths.outbox / "001.json").exists()
    assert (paths.outbox / "001.rejected").exists()


def test_one_room_has_one_session(paths):
    sess._acquire_lock(paths)
    try:
        with pytest.raises(sess.SessionBusy) as excinfo:
            sess._acquire_lock(paths)
        assert excinfo.value.pid == os.getpid()
    finally:
        sess._release_lock(paths)


def test_a_dead_holders_lock_is_taken_over(paths, monkeypatch):
    """A crash must not require a human to delete a lock file."""
    paths.lock.write_text(json.dumps({"pid": 999999999, "started": 0}))
    monkeypatch.setattr(sess, "_pid_alive", lambda pid: False)
    sess._acquire_lock(paths)       # must not raise
    assert json.loads(paths.lock.read_text())["pid"] == os.getpid()
    sess._release_lock(paths)


def test_session_pid_checks_liveness_instead_of_believing_the_file(paths, monkeypatch):
    paths.lock.write_text(json.dumps({"pid": 424242, "started": 0}))
    monkeypatch.setattr(sess, "_pid_alive", lambda pid: False)
    assert sess.session_pid("room-test") == 0
    monkeypatch.setattr(sess, "_pid_alive", lambda pid: True)
    assert sess.session_pid("room-test") == 424242


def test_the_mirror_reads_back_as_frames_without_duplicates(paths):
    room = ScriptedRoom([[_msg("say", 1, id_="dup"), _msg("say", 2), {"kind": "sync"}]])
    sess.run_session("room-test", {"url": "wss://x", "seat": "s"},
                     connect=lambda: room, once=True, idle_s=0)
    # A daemon restart re-appends the backlog; the reader must collapse it.
    with paths.inbox.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_msg("say", 1, id_="dup")) + "\n")
    frames = sess.mirror_frames("room-test")
    assert [f.lamport for f in frames] == [1, 2]


def test_the_mirror_respects_a_cursor(paths):
    room = ScriptedRoom([[_msg("say", 1), _msg("say", 2), _msg("say", 3),
                          {"kind": "sync"}]])
    sess.run_session("room-test", {"url": "wss://x", "seat": "s"},
                     connect=lambda: room, once=True, idle_s=0)
    assert [f.lamport for f in sess.mirror_frames("room-test", since_lamport=2)] == [3]


def test_a_lease_refusal_waits_then_gives_up_honestly(paths, monkeypatch):
    from vaf.core.a2a.client import RemoteRefused

    tries = {"n": 0}

    def refused():
        tries["n"] += 1
        raise RemoteRefused("lease held")

    now = {"t": 0.0}
    code = sess.run_session(
        "room-test", {"url": "wss://x", "seat": "s"}, connect=refused, once=True,
        clock=lambda: now["t"],
        sleep=lambda s: now.__setitem__("t", now["t"] + s),
    )
    assert code == 1
    assert tries["n"] >= 2, "one refusal must not be the end - the lease drains"
    status = sess.read_status("room-test")
    assert status["connected"] is False
    assert "gave up" in status["last_error"]


def test_a_room_refusal_is_never_counted_as_sent(paths):
    """MUTATION: file any ack as success in drain_outbox.

    First field use, a foreign agent holding a session: the ack said
    not_writer, the payload was deleted, status.json said sent: 1 - a rejected
    message that read as delivered to everyone watching. The fate of a payload
    is the ROOM'S answer, not the wire holding.
    """
    (paths.outbox / "001.json").write_text(json.dumps(
        {"kind": "say", "body": {"text": "keep me"}}), encoding="utf-8")

    class NotWriter(ScriptedRoom):
        def submit(self, payload, timeout=None):
            self.submitted.append(payload)
            return {"kind": "ack", "status": "not_writer", "reason": "lease lapsed"}

    room = NotWriter([[{"kind": "sync"}]])
    sess.run_session("room-test", {"url": "wss://x", "seat": "s"},
                     connect=lambda: room, once=True, idle_s=0)
    assert (paths.outbox / "001.json").exists(), \
        "an unauthorized send was turned away unjudged - the payload must stay"
    assert "not_writer" in (paths.outbox / "001.error").read_text()
    status = json.loads(paths.status.read_text())
    assert status["sent"] == 0, "a refused message must never count as sent"


def test_a_judged_refusal_moves_aside_instead_of_retrying_forever(paths):
    """The room said no on the merits; the next round would hear the same no."""
    (paths.outbox / "001.json").write_text(json.dumps(
        {"kind": "directive", "body": {"text": "no orders in a round"}}),
        encoding="utf-8")

    class Refused(ScriptedRoom):
        def submit(self, payload, timeout=None):
            self.submitted.append(payload)
            return {"kind": "ack", "status": "refused", "reason": "round has no orders"}

    room = Refused([[{"kind": "sync"}]])
    sess.run_session("room-test", {"url": "wss://x", "seat": "s"},
                     connect=lambda: room, once=True, idle_s=0)
    assert not (paths.outbox / "001.json").exists()
    assert "round has no orders" in (paths.outbox / "001.rejected").read_text()
    status = json.loads(paths.status.read_text())
    assert status["sent"] == 0 and status["rejected"] == 1


def _jumping_clock(step=31.0):
    state = {"t": 0.0}

    def clk():
        state["t"] += step
        return state["t"]

    return clk


def test_a_held_line_renews_its_lease(paths):
    """MUTATION: drop the renew block from run_session.

    Contract C9 says leases are renewed while attached; the host renews only on
    successful submits, and a conversation is read-think-answer - thinking
    outlasts the 90s TTL. Without the keepalive a quiet session stayed
    connected, kept receiving, and lost its write right in the middle of
    ordinary use.
    """
    class Renewing(ScriptedRoom):
        def submit(self, payload, timeout=None):
            self.submitted.append(payload)
            if payload.get("kind") == "renew":
                return {"kind": "ack", "status": "renewed"}
            return {"kind": "ack", "status": "committed"}

    room = Renewing([[{"kind": "sync"}]])
    sess.run_session("room-test", {"url": "wss://x", "seat": "s"},
                     connect=lambda: room, once=True, idle_s=0,
                     clock=_jumping_clock())
    assert {"kind": "renew"} in room.submitted
    status = json.loads(paths.status.read_text())
    assert "lease_keepalive" not in status


def test_a_lost_lease_ends_the_session_instead_of_limping(paths):
    """Connected-but-mute is exactly the state the keepalive exists to end; a
    restart re-attaches cleanly and its own cursor decides the backlog."""
    class Lost(ScriptedRoom):
        def submit(self, payload, timeout=None):
            return {"kind": "ack", "status": "not_writer", "reason": "expired"}

    room = Lost([[{"kind": "sync"}]])
    code = sess.run_session("room-test", {"url": "wss://x", "seat": "s"},
                            connect=lambda: room, once=True, idle_s=0,
                            clock=_jumping_clock())
    assert code == 1
    status = json.loads(paths.status.read_text())
    assert status["connected"] is False
    assert "lease lost" in str(status.get("last_error") or "")


def test_an_old_host_is_asked_once_and_then_left_in_peace(paths):
    """A host that predates the verb refuses it; asking again every 30 seconds
    would be a question with a known answer. The session says so in its status
    and behaves as before the verb existed."""
    class OldHost(ScriptedRoom):
        def submit(self, payload, timeout=None):
            self.submitted.append(payload)
            return {"kind": "ack", "status": "malformed", "reason": "unknown kind"}

    # First round carries a frame, so `once` does not end the session before a
    # SECOND round has had the chance to ask again - which is exactly what the
    # fallback must prevent.
    room = OldHost([[_msg("say", 1), {"kind": "sync"}], [{"kind": "sync"}]])
    sess.run_session("room-test", {"url": "wss://x", "seat": "s"},
                     connect=lambda: room, once=True, idle_s=0,
                     clock=_jumping_clock())
    renews = [p for p in room.submitted if p.get("kind") == "renew"]
    assert len(renews) == 1, "one refusal is the whole answer"
    status = json.loads(paths.status.read_text())
    assert status.get("lease_keepalive") == "unsupported by host"
