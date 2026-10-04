# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A confirmation answer belongs to ONE question.

The measured gaps: the dialog was bound to the chat only, so a stale "always" from an old
dialog in a second tab answered whatever was asked next - a different command; an answer
faster than the agent's wait was dropped (the gate was registered after the dialog was
pushed) and the turn waited five minutes for an answer already given; the other tabs kept
their dialog open after one tab answered; and a tab that opened the chat later never saw it.
Each test names the mutation it catches.
"""
import json
import threading
import time

import pytest

SESSION = "web_chat-gate-binding"
SCOPE = "ab12cd34-0000-4000-8000-0000000000d4"


@pytest.fixture
def web():
    from vaf.core.web_interface import get_web_interface
    w = get_web_interface()
    w._pending_gates.pop(SESSION, None)
    yield w
    w._pending_gates.pop(SESSION, None)


@pytest.fixture
def trust_dir(monkeypatch, tmp_path):
    from vaf.core import tool_dispatch
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "config_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(tool_dispatch, "_confirmation_bypass_resolver", None)
    return tmp_path


def _wait(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_every_question_carries_its_own_id(trust_dir):
    """MUTATION: leave gate_id out of the decider's keywords."""
    from vaf.core.tool_dispatch import resolve_confirmation_gate
    events, seen = [], {}

    def decide(tool, reason, gate_id=None):
        seen["gate_id"] = gate_id
        return "allow_once"

    out = resolve_confirmation_gate("host_bash", reason="runs on the host", args={"command": "ls"},
                                    trust_dir=trust_dir, interactive=True, decide=decide,
                                    emit=events.append, session_id=SESSION, user_scope_id=SCOPE)
    asked = next(e for e in events if e["type"] == "gate_required")
    answered = next(e for e in events if e["type"] == "gate_decision")
    assert out is None and asked["gate_id"]
    assert seen["gate_id"] == asked["gate_id"] == answered["gate_id"]


def test_a_two_argument_decider_keeps_working(trust_dir):
    """`decide(tool_name, reason)` is the published contract."""
    from vaf.core.tool_dispatch import resolve_confirmation_gate
    out = resolve_confirmation_gate("host_bash", reason="r", args={}, trust_dir=trust_dir,
                                    interactive=True, decide=lambda t, r: "allow_once")
    assert out is None


def test_an_answer_faster_than_the_wait_is_kept(web):
    """MUTATION: open a fresh gate in wait_gate even when this one is open."""
    gate_id = web.open_gate(SESSION, {"type": "gate_required", "tool": "host_bash"})
    assert web.resolve_gate(SESSION, "allow_once", gate_id=gate_id)
    started = time.monotonic()
    assert web.wait_gate(SESSION, gate_id, timeout=5) == "allow_once"
    assert time.monotonic() - started < 1


def test_a_stale_always_does_not_approve_a_later_command(web, trust_dir):
    """MUTATION: let resolve_gate ignore gate_id, and the old dialog's "always" approves this
    command and stores the tool as always allowed."""
    from vaf.core.tool_dispatch import resolve_confirmation_gate
    from vaf.core.trust import get_tool_policy
    box = {}

    def _run():
        box["out"] = resolve_confirmation_gate(
            "host_bash", reason="r", args={"command": "rm -rf build"}, trust_dir=trust_dir,
            interactive=True, session_id=SESSION, user_scope_id=SCOPE,
            on_gate_required=lambda evt: web.open_gate(SESSION, evt),
            decide=lambda t, r, gate_id=None: web.wait_gate(SESSION, gate_id, timeout=10))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    assert _wait(lambda: web.pending_gate(SESSION) is not None)
    assert web.resolve_gate(SESSION, "allow_always", gate_id="an-older-dialog") is False
    assert web.pending_gate(SESSION) is not None, "the stale answer closed the question"
    assert web.resolve_gate(SESSION, "allow_once", gate_id=web.pending_gate(SESSION)["gate_id"])
    t.join(timeout=5)
    assert box.get("out") is None
    assert get_tool_policy("host_bash", SCOPE) == "ask"


def test_stop_ends_the_wait_with_cancel(web):
    started = time.monotonic()
    assert web.wait_gate(SESSION, None, should_cancel=lambda: True, timeout=60) == "cancel"
    assert time.monotonic() - started < 1.5 and web.pending_gate(SESSION) is None


def test_a_tab_that_opens_the_chat_later_sees_the_open_question(web):
    """MUTATION: drop the re-send from load_session (the source pin below), or keep no
    payload in open_gate."""
    from pathlib import Path
    web.open_gate(SESSION, {"type": "gate_required", "tool": "host_bash", "args_preview": "ls"})
    shown = web.pending_gate(SESSION)
    assert shown["type"] == "gate_required" and shown["sessionId"] == SESSION and shown["gate_id"]
    src = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "web_server.py").read_text(
        encoding="utf-8")
    load = src[src.index('elif type == "load_session":'):src.index('elif type == "delete_session":')]
    assert "manager.pending_gate(sid)" in load and "send_json(_open_gate)" in load


def test_the_socket_drops_an_answer_without_its_question(web, monkeypatch):
    """Over the real WebSocket: no id, or an id of a dialog that is not open, changes nothing
    and tells the tab; the right id answers. MUTATION: resolve without the id in the handler."""
    import jwt
    from starlette.testclient import TestClient

    import vaf.auth.crypto as crypto
    import vaf.auth.permissions as permissions
    from vaf.core.config import get_local_admin_scope_id
    from vaf.core.web_server import app

    async def _unknown(scope):
        return ("unknown", "")

    monkeypatch.setattr(permissions, "account_standing_async", _unknown)
    monkeypatch.setattr(crypto, "get_jwt_secret", lambda: "s" * 32)
    token = jwt.encode({"sub": "1", "user_scope_id": str(get_local_admin_scope_id()),
                        "username": "admin", "role": "admin"}, "s" * 32, algorithm="HS256")
    gate_id = web.open_gate(SESSION, {"type": "gate_required", "tool": "host_bash"})

    def _expired(ws):
        box = {}

        def pump():
            for _ in range(100):
                msg = ws.receive_json()
                if msg.get("type") == "gate_expired":
                    box["msg"] = msg
                    return

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        reader.join(10)
        return box.get("msg")

    with TestClient(app, client=("127.0.0.1", 40000)).websocket_connect(f"/ws?token={token}") as ws:
        ws.send_text(json.dumps({"type": "gate_response", "decision": "allow_always",
                                 "sessionId": SESSION}))
        assert _expired(ws) is not None
        ws.send_text(json.dumps({"type": "gate_response", "decision": "allow_always",
                                 "sessionId": SESSION, "gate_id": "an-older-dialog"}))
        assert _expired(ws)["gate_id"] == "an-older-dialog"
        assert web.pending_gate(SESSION) is not None
        gate = web._pending_gates[SESSION]
        ws.send_text(json.dumps({"type": "gate_response", "decision": "allow_once",
                                 "sessionId": SESSION, "gate_id": gate_id}))
        assert _wait(lambda: gate["event"].is_set())
    assert gate["decision"][0] == "allow_once"


def test_the_dialog_closes_only_once_the_answer_went_out():
    """With the socket down the answer is lost and the agent waits five minutes; the dialog
    must stay so the person can answer again. MUTATION: clear the dialog before the check."""
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    helper = page[page.index("const answerGate = "):]
    helper = helper[:helper.index("};")]
    assert helper.index("ws.readyState !== WebSocket.OPEN) return") < helper.index("setGateRequest(null)")
