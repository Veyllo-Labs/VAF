# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The link manager: turns in, replies out, the outbox, removal, and what the app channel
may and may not do."""
import ast
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app_link_client import link, paired_device
from app_link_env import SCOPE_A, SCOPE_B, app_link_home  # noqa: F401  (fixture)
from vaf.core import channel_message_store as store
from vaf.core.app_link import bridge, devices, pairing
from vaf.core.task_queue import TaskQueue

ROOT = Path(__file__).resolve().parents[1]
UID = "0f8fad5b-d9cb-469f-a165-70867728950e"
UID2 = "1f8fad5b-d9cb-469f-a165-70867728950e"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    bridge.reset()
    pairing.reset()
    import vaf.core.headless_runner as hr
    monkeypatch.setattr(hr, "get_web_interface", lambda: SimpleNamespace(
        emit_agent_message=lambda **k: None))
    yield
    bridge.reset()


def _queued(sid):
    return list(TaskQueue()._queued_by_session.get(sid, ()))


def _msg(row, text="hi", mid=UID):
    return {"t": "msg", "id": mid, "chat": devices.chat_session_id(row["user_scope_id"]), "text": text}


def test_msg_feeds_queue_in_process(app_link_home, monkeypatch):
    def _no_network(*a, **k):
        raise AssertionError("the app link opened a connection")
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    import httpx
    import requests
    monkeypatch.setattr(requests.Session, "request", _no_network)
    monkeypatch.setattr(httpx.Client, "send", _no_network)
    for transport in ("direct", "relay"):
        bridge.reset()
        device = paired_device(SCOPE_A, model=f"Phone {transport}")
        client, _pipe, row = link(SCOPE_A, device=device, transport=transport)
        message = _msg(row, f"hello over {transport}", mid=UID if transport == "direct" else UID2)
        message["user_scope_id"] = SCOPE_B
        message["username"] = "mallory"
        client.send(message)
        assert {"t": "ack", "id": message["id"]} in client.frames
        task = [t for t in _queued(devices.chat_session_id(SCOPE_A))
                if t.input_text == f"hello over {transport}"][0]
        assert task.source == "veyllo_app"
        assert task.metadata["user_scope_id"] == SCOPE_A and task.metadata["username"] == "alice"
        assert task.metadata["app_device_id"] == row["device_id"]
        assert task.metadata["app_message_id"] == message["id"]
    stored = store.get_message("alice", devices.chat_key(SCOPE_A), UID, direction="in",
                               user_scope_id=SCOPE_A, channel="veyllo_app")
    assert stored["body"] == "hello over direct"


def test_duplicate_wrong_chat_and_rate_limit(app_link_home):
    client, _pipe, row = link(SCOPE_A)
    client.send(_msg(row))
    client.send(_msg(row))
    assert len(_queued(devices.chat_session_id(SCOPE_A))) == 1
    assert client.of("ack").count({"t": "ack", "id": UID}) == 2
    bad = _msg(row, mid=UID2)
    bad["chat"] = devices.chat_session_id(SCOPE_B)
    client.send(bad)
    assert client.of("error")[-1] == {"t": "error", "code": "wrong_chat", "ref": UID2}
    from vaf.core.app_link.session import MSG_PER_MINUTE
    import uuid
    for _ in range(MSG_PER_MINUTE + 1):
        client.send(_msg(row, mid=str(uuid.uuid4())))
    assert client.of("error")[-1]["code"] == "rate_limited"


def _task_for(row, text="hi"):
    meta = {"user_scope_id": row["user_scope_id"], "username": "alice",
            "app_device_id": row["device_id"], "app_message_id": UID}
    return SimpleNamespace(source="veyllo_app", metadata=meta, session_id=devices.chat_session_id(row["user_scope_id"]))


def test_stream_deltas_and_done(app_link_home):
    import vaf.core.headless_runner as hr
    client, _pipe, row = link(SCOPE_A)
    task = _task_for(row)
    emit = hr._app_link_emit(task)
    client.pump()
    assert {"t": "read", "id": UID} in client.frames
    assert client.of("typing")[0]["on"] is True
    emit("Hello", 1, None)
    emit("Hello wor", 1, None)
    emit("Hello world", 1, None)
    emit("Next round", 2, None)
    hr._deliver_channel_reply(task, "Hello world\n\nNext round")
    client.pump()
    deltas = client.of("delta")
    assert [d["seq"] for d in deltas] == [1, 2, 3, 4]
    assert "".join(d["text"] for d in deltas) == "Hello world\n\nNext round"
    done = client.of("done")[0]
    assert done["final_text"] == "Hello world\n\nNext round" and done["id"] == deltas[0]["id"]
    assert client.of("typing")[-1]["on"] is False
    stored = store.get_message("alice", devices.chat_key(SCOPE_A), done["id"], direction="out",
                               user_scope_id=SCOPE_A, channel="veyllo_app")
    assert stored["body"] == "Hello world\n\nNext round"


def test_retraction(app_link_home):
    import vaf.core.headless_runner as hr
    client, _pipe, row = link(SCOPE_A)
    task = _task_for(row)
    emit = hr._app_link_emit(task)
    emit("I think the answer is 4", 1, None)
    emit("Actually", 1, None)
    emit("Actually, the correct answer is five", 1, None)
    hr._deliver_channel_reply(task, "Actually, the correct answer is five")
    client.pump()
    assert [d["text"] for d in client.of("delta")] == ["I think the answer is 4"]
    assert client.of("done")[0]["final_text"] == "Actually, the correct answer is five"


def test_removed_device_gets_nothing(app_link_home):
    import vaf.core.headless_runner as hr
    keep = paired_device(SCOPE_A, model="Tablet")
    other, _p2, _ = link(SCOPE_A, device=keep, peer="10.0.0.6", connection="c2")
    client, pipe, row = link(SCOPE_A)
    task = _task_for(row)
    emit = hr._app_link_emit(task)
    emit("first part", 1, None)
    client.pump()
    before = len(client.frames)
    # Another process removed it: the file changed, this process's session is still open.
    with devices._locked():
        state = devices._load()
        state["devices"] = [d for d in state["devices"] if d["device_id"] != row["device_id"]]
        devices._save(state)
    emit("first part and more", 1, None)
    hr._deliver_channel_reply(task, "first part and more")
    client.pump()
    other.pump()
    assert client.frames[before:] == []
    bridge.sweep()
    client.pump()
    assert pipe.session.closed and client.frames[before:] == [{"t": "bye", "reason": "removed"}]
    assert other.of("done")[0]["final_text"] == "first part and more"


def test_a_device_that_no_longer_stands_gets_nothing(app_link_home):
    import vaf.core.headless_runner as hr
    client, pipe, row = link(SCOPE_A)
    task = _task_for(row)
    emit = hr._app_link_emit(task)
    emit("before", 1, None)
    client.pump()
    before = [f for f in client.frames if f["t"] in ("delta", "done", "msg")]
    # The account's lane was switched off while the turn ran: the row is still there.
    app_link_home.lane(SCOPE_A, False)
    emit("before and after", 1, None)
    hr._deliver_channel_reply(task, "before and after")
    bridge.deliver(SCOPE_A, "alice", "a message")
    client.pump()
    assert [f for f in client.frames if f["t"] in ("delta", "done", "msg")] == before
    assert pipe.session.closed


def test_remove_closes_session(app_link_home):
    client, pipe, row = link(SCOPE_A)
    devices.remove_device(row["device_id"], by="alice", reason="lost")
    client.pump()
    assert pipe.session.closed and {"t": "bye", "reason": "removed"} in client.frames
    # Removed by another process: the sweep closes it within one round.
    client2, pipe2, row2 = link(SCOPE_A, peer="10.0.0.7", connection="c3")
    with devices._locked():
        state = devices._load()
        state["devices"] = []
        devices._save(state)
    bridge.sweep()
    assert pipe2.session.closed


def test_send_to_user_app_branch(app_link_home, monkeypatch):
    from vaf.auth import user_workspace
    from vaf.core import messaging_connections as mc
    monkeypatch.setattr(user_workspace, "get_user_workspace", lambda name: SimpleNamespace(
        get_user_identity=lambda: {"main_messenger": "veyllo_app"}))
    client, _pipe, row = link(SCOPE_A)
    away = paired_device(SCOPE_A, model="Tablet")[0]
    offline = []
    bridge.add_offline_hook(offline.append)
    try:
        assert "veyllo_app" in mc.get_messaging_connections("alice", SCOPE_A)["available"]
        assert mc.get_messaging_connections("bob", SCOPE_B)["available"] == []
        sent, channel = mc.send_to_main_messenger(SCOPE_A, "alice", "Your report is ready")
    finally:
        bridge.remove_hooks(offline.append)
    assert (sent, channel) == (True, "veyllo_app")
    client.pump()
    msg = client.of("msg")[-1]
    assert msg["text"] == "Your report is ready"
    for did in (row["device_id"], away["device_id"]):
        assert [e["id"] for e in devices.get(did)["outbox"]] == [msg["id"]]
    assert [r["device_id"] for r in offline] == [away["device_id"]]
    rows = store.get_chat_messages("alice", devices.chat_key(SCOPE_A), user_scope_id=SCOPE_A,
                                   channel="veyllo_app")
    assert len([r for r in rows if r["direction"] == "out"]) == 1


def test_outbox_replay_order_and_ack_per_device(app_link_home):
    one = paired_device(SCOPE_A, model="Phone")
    two = paired_device(SCOPE_A, model="Tablet")
    for text in ("first", "second", "third"):
        bridge.deliver(SCOPE_A, "alice", text)
    client, _pipe, row = link(SCOPE_A, device=one)
    assert [m["text"] for m in client.of("msg")] == ["first", "second", "third"]
    for m in client.of("msg")[:2]:
        client.send({"t": "ack", "id": m["id"]})
    assert len(devices.get(row["device_id"])["outbox"]) == 1
    assert len(devices.get(two[0]["device_id"])["outbox"]) == 3
    client.pump()
    again, _p2, _ = link(SCOPE_A, device=one, peer="10.0.0.8", connection="c4")
    assert [m["text"] for m in again.of("msg")] == ["third"]


def test_finished_reply_replayed_as_msg(app_link_home):
    import vaf.core.headless_runner as hr
    device = paired_device(SCOPE_A)
    task = _task_for(device[0])
    emit = hr._app_link_emit(task)
    emit("streamed while away", 1, None)
    hr._deliver_channel_reply(task, "the whole answer")
    client, _pipe, _row = link(SCOPE_A, device=device)
    assert client.of("delta") == []
    msgs = client.of("msg")
    assert len(msgs) == 1 and msgs[0]["text"] == "the whole answer"
    assert msgs[0]["id"] == task.metadata.get("app_reply_id", msgs[0]["id"])


def test_outbox_cap_and_notice(app_link_home):
    device = paired_device(SCOPE_A)
    for i in range(201):
        bridge.deliver(SCOPE_A, "alice", f"m{i}")
    old = devices.get(device[0]["device_id"])["outbox"][0]
    client, _pipe, _row = link(SCOPE_A, device=device)
    msgs = client.of("msg")
    assert len(msgs) == 201
    assert "1 older messages" in msgs[0]["text"] and msgs[1]["text"] == "m1"
    assert old["id"] == msgs[1]["id"]
    # Entries older than seven days are not sent.
    bridge.reset()
    device2 = paired_device(SCOPE_B, username="bob")
    bridge.deliver(SCOPE_B, "bob", "ancient")
    with devices._locked():
        state = devices._load()
        for d in state["devices"]:
            for e in d["outbox"]:
                e["ts"] = time.time() - 8 * 86400
        devices._save(state)
    client2, _p2, _ = link(SCOPE_B, device=device2)
    texts = [m["text"] for m in client2.of("msg")]
    assert "ancient" not in texts and len(texts) == 1


def test_caps_replace(app_link_home):
    client, _pipe, row = link(SCOPE_A, caps=["calendar", "contacts"])
    assert devices.get(row["device_id"])["caps"] == ["calendar", "contacts"]
    client.send({"t": "caps", "caps": ["location"]})
    assert devices.get(row["device_id"])["caps"] == ["location"]


def test_cancel_stops_the_running_reply(app_link_home, monkeypatch):
    import vaf.core.headless_runner as hr
    from vaf.core import revocation
    stopped = []
    monkeypatch.setattr(revocation, "stop_session", lambda sid, **k: stopped.append((sid, k)))
    client, _pipe, row = link(SCOPE_A)
    task = _task_for(row)
    hr._app_link_emit(task)
    client.send({"t": "cancel", "id": UID})
    assert stopped == [(devices.chat_session_id(SCOPE_A), {"include_subagents": False})]


def test_inbound_file_is_checked_and_saved(app_link_home, monkeypatch):
    import hashlib
    from vaf.core import threat_db
    from vaf.core import session as session_mod
    from vaf.core.app_link import protocol as pr
    workspace = app_link_home.root / "ws"
    monkeypatch.setattr(session_mod, "get_session_workspace_dir",
                        lambda sid, create=False, user_scope_id=None: workspace)
    client, _pipe, row = link(SCOPE_A)
    data = b"report body" * 100
    xfer = b"\x07" * 16
    message = _msg(row, "see file")
    message["attachments"] = [{"xfer": pr.b64url(xfer), "name": "report.txt", "mime": "text/plain",
                               "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]
    client.send(message)
    assert not client.of("ack")
    client.send_raw(pr.encode_chunk(xfer, 0, data))
    assert {"t": "ack", "id": UID} in client.frames
    task = _queued(devices.chat_session_id(SCOPE_A))[0]
    assert "[File: report.txt, saved as" in task.input_text
    assert (workspace / "from_phone" / "report.txt").read_bytes() == data
    # A blocked file: nothing saved, nothing queued.
    monkeypatch.setattr(threat_db, "inspect_upload",
                        lambda data, **k: threat_db.UploadVerdict(blocked=True, reason="listed"))
    xfer2 = b"\x08" * 16
    bad = _msg(row, "again", mid=UID2)
    bad["attachments"] = [dict(message["attachments"][0], xfer=pr.b64url(xfer2), name="evil.txt")]
    client.send(bad)
    client.send_raw(pr.encode_chunk(xfer2, 0, data))
    assert {"t": "error", "code": "xfer_abort", "ref": pr.b64url(xfer2)} in client.frames
    assert not (workspace / "from_phone" / "evil.txt").exists()
    assert len(_queued(devices.chat_session_id(SCOPE_A))) == 1


# ── what the app channel may do ──────────────────────────────────────────────

def test_send_veyllo_app_is_never_held():
    from vaf.core import outbound_hold
    assert outbound_hold.reaches_a_third_party("send_veyllo_app", {"message": "x"}) is False
    from vaf.tools.send_veyllo_app import SendVeylloAppTool
    assert "to" not in str(SendVeylloAppTool.parameters["properties"].keys())


def test_non_admin_save_keeps_block():
    from vaf.core.config import Config
    body = {"veyllo_app_config": {"enabled": False, "relay_api_base": "https://attacker.example",
                                  "relay_instance_id": "x"}, "theme": "dark"}
    existing = {"veyllo_app_config": dict(Config.DEFAULTS["veyllo_app_config"])}
    out = Config.split_connection_toggles(existing, body, SCOPE_B, is_admin=False)
    assert "veyllo_app_config" not in out
    assert existing["connection_enabled_by_scope"][SCOPE_B]["veyllo_app"] is False
    assert existing["veyllo_app_config"]["relay_api_base"] == "https://veyllo.app"
    doc = (ROOT / "docs" / "setup" / "CONFIG_SCHEMA.md").read_text(encoding="utf-8")
    assert f"`Config.DEFAULTS` ({len(Config.DEFAULTS)} keys)" in doc
    assert "| `veyllo_app_config` |" in doc


_APP_LINK = ROOT / "vaf" / "core" / "app_link"


def test_no_loopback_or_api_calls_in_the_app_link():
    for path in sorted(_APP_LINK.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in ("internal_api_base", "127.0.0.1", "localhost", "'/api/", '"/api/'):
            assert needle not in text, f"{path.name} contains {needle}"


def test_no_agent_tool_pairs_or_removes_devices():
    for path in sorted((ROOT / "vaf" / "tools").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("vaf.core.app_link"):
                names = {a.name for a in node.names}
                assert "pairing" not in names and node.module != "vaf.core.app_link.pairing", path.name
            if isinstance(node, ast.Attribute) and node.attr in ("remove_device", "remove_account_devices",
                                                                   "open_window", "issue", "confirm"):
                if isinstance(node.value, ast.Name) and node.value.id in ("devices", "pairing"):
                    raise AssertionError(f"{path.name} calls {node.value.id}.{node.attr}")


def test_no_secret_anywhere(app_link_home, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    pairing.open_window(SCOPE_A, "alice")
    import vaf.core.app_link.pairing as pm
    pm_direct = pm.direct_addresses
    pm.direct_addresses = lambda: ["wss://10.0.0.1:8443/api/app-link"]
    try:
        p = pairing.issue(SCOPE_A, "alice", "relay")
    finally:
        pm.direct_addresses = pm_direct
    from app_link_client import AppClient, Pipe, ca_der, device_key, pair_message
    import base64
    pipe = Pipe()
    client = AppClient(ca_der(), sni="pair.vaf.internal").attach(pipe).connect()
    key = device_key()
    message = pair_message(p.k, key)
    client.send(message)
    pairing.confirm(SCOPE_A, True)
    client.pump()
    paired = client.of("paired")[0]
    from vaf.core.app_link import protocol as pr
    secrets_seen = [p.k, pr.b64url(p.p), message["csr"], paired["cert"], p.qr.split("#", 1)[1]]
    haystack = caplog.text + repr(app_link_home.events)
    for root in (app_link_home.root,):
        for f in root.rglob("*"):
            if f.is_file() and f.suffix in (".log", ".jsonl", ".json", ".txt"):
                try:
                    haystack += f.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    pass
    for value in secrets_seen:
        assert value not in haystack
        assert base64.b64decode(paired["cert"]).hex() not in haystack


def test_send_to_user_falls_back_when_no_device_may_receive(app_link_home, monkeypatch):
    from vaf.auth import user_workspace
    from vaf.core import messaging_connections as mc
    monkeypatch.setattr(user_workspace, "get_user_workspace", lambda name: SimpleNamespace(
        get_user_identity=lambda: {"main_messenger": "veyllo_app"}))
    row = paired_device(SCOPE_A)[0]
    assert mc.send_to_main_messenger(SCOPE_A, "alice", "hello")[0] is True
    # Stale for 100 days: the device does not stand, the caller must fall back to the web UI.
    with devices._locked():
        state = devices._load()
        for d in state["devices"]:
            d["last_seen_at"] = d["created_at"] = time.time() - 100 * 86400
        devices._save(state)
    before = len(devices.get(row["device_id"])["outbox"])
    assert bridge.deliver(SCOPE_A, "alice", "again") is False
    assert len(devices.get(row["device_id"])["outbox"]) == before


def test_a_refused_message_can_be_sent_again_with_its_id(app_link_home, monkeypatch):
    import vaf.core.channel_ingress_policy as ip
    client, _pipe, row = link(SCOPE_A)
    with monkeypatch.context() as m:
        m.setattr(ip, "evaluate_ingress", lambda *a, **k: (False, "closed"))
        client.send(_msg(row, "first try"))
    assert client.of("error")[-1] == {"t": "error", "code": "refused", "ref": UID}
    client.send(_msg(row, "first try"))
    assert [t.input_text for t in _queued(devices.chat_session_id(SCOPE_A))] == ["first try"]
    assert {"t": "ack", "id": UID} in client.frames


def test_a_failure_before_the_queue_releases_the_id(app_link_home, monkeypatch):
    from vaf.core import task_queue
    client, pipe, row = link(SCOPE_A)
    real_add = task_queue.TaskQueue.add
    calls = {"n": 0}

    def _flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("queue down")
        return real_add(self, *a, **k)
    monkeypatch.setattr(task_queue.TaskQueue, "add", _flaky)
    client.send(_msg(row, "try"))
    assert calls["n"] == 1 and not bridge._seen_before(row["device_id"], UID)


def test_a_file_too_big_for_the_app_is_refused_with_a_reason(app_link_home, monkeypatch, tmp_path):
    import hashlib

    from vaf.core.app_link import protocol as pr
    from vaf.tools.send_veyllo_app import SendVeylloAppTool
    paired_device(SCOPE_A)
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 2048)
    monkeypatch.setattr(pr, "MAX_FILE_BYTES", 1024)
    assert bridge.deliver(SCOPE_A, "alice", "see", str(big)) is False
    import vaf.tools.send_veyllo_app as tool_mod
    monkeypatch.setattr(tool_mod, "_resolve_path", lambda s: (big, None))
    out = SendVeylloAppTool().run(message="see", file_path=str(big), user_scope_id=SCOPE_A,
                                  username="alice")
    assert "larger than 25 MiB" in out
    monkeypatch.setattr(pr, "MAX_FILE_BYTES", 26214400)
    client, _pipe, _row = link(SCOPE_A)
    assert bridge.deliver(SCOPE_A, "alice", "see", str(big)) is True
    client.pump()
    att = client.of("msg")[-1]["attachments"][0]
    assert att["size"] == 2048 and att["sha256"] == hashlib.sha256(b"x" * 2048).hexdigest()
    assert b"".join(c.data for c in client.chunks) == b"x" * 2048


def test_an_open_reply_is_closed_at_the_end_of_every_turn(app_link_home):
    import vaf.core.headless_runner as hr
    client, _pipe, row = link(SCOPE_A)
    task = _task_for(row)
    hr._app_link_emit(task)
    rid = task.metadata["app_reply_id"]
    hr._close_app_reply(task)          # a [SYSTEM_LOG_ONLY] turn: nothing else closed it
    hr._close_app_reply(task)          # and twice is once
    client.pump()
    assert client.of("done") == [{"t": "done", "id": rid}]
    assert client.of("typing")[-1]["on"] is False and (SCOPE_A, rid) not in bridge._replies
    src = open(hr.__file__, encoding="utf-8").read()
    assert src.count("                _close_app_reply(task)\n") == 1, "the chat lane closes it at its end"


def test_a_reply_nothing_ended_is_forgotten(app_link_home):
    import vaf.core.headless_runner as hr
    _client, _pipe, row = link(SCOPE_A)
    task = _task_for(row)
    hr._app_link_emit(task)
    rid = task.metadata["app_reply_id"]
    bridge._replies[(SCOPE_A, rid)].started -= bridge.REPLY_MAX_SECONDS + 1
    bridge.sweep()
    assert (SCOPE_A, rid) not in bridge._replies
