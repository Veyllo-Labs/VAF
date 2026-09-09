# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import json
import sys
import threading
import time
from types import ModuleType

from vaf.api import whatsapp_bridge as wa
from vaf.core.platform import Platform
from vaf.tools.send_whatsapp import SendWhatsAppTool


def _install_fake_module(monkeypatch, name: str, **attrs):
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)


def test_send_whatsapp_uses_external_ipc_when_local_bridge_state_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(wa, "_outgoing_queue", None)
    monkeypatch.setattr(wa, "_processes", {})
    monkeypatch.setattr(wa, "_is_reply_allowed", lambda *args, **kwargs: True)

    wa._write_json_atomic(
        wa._ipc_state_path(),
        {"running": True, "usernames": ["admin"], "updated_at": time.time()},
    )

    captured = {}

    def simulate_main_bridge():
        deadline = time.time() + 2
        while time.time() < deadline:
            request_files = sorted(wa._ipc_requests_dir().glob("*.json"))
            if request_files:
                request_path = request_files[0]
                payload = json.loads(request_path.read_text(encoding="utf-8"))
                request_path.unlink(missing_ok=True)
                captured.update(payload)
                wa._write_json_atomic(
                    wa._ipc_results_dir() / f"{payload['req_id']}.json",
                    {"success": True, "error": "", "updated_at": time.time()},
                )
                return
            time.sleep(0.05)
        raise AssertionError("WhatsApp IPC request was not written")

    worker = threading.Thread(target=simulate_main_bridge, daemon=True)
    worker.start()

    result = wa.send_whatsapp_with_confirmation(
        "admin",
        "491761234567@s.whatsapp.net",
        "Hallo aus dem Hintergrund",
        timeout=2.0,
    )

    worker.join(timeout=2.0)

    assert result == "Message sent via WhatsApp."
    assert captured["username"] == "admin"
    assert captured["chat_jid"] == "491761234567@s.whatsapp.net"
    assert captured["text"] == "Hallo aus dem Hintergrund"


def test_send_whatsapp_tool_relies_on_bridge_helper_not_local_process_state(monkeypatch):
    monkeypatch.setattr(
        "vaf.core.messaging_connections.get_whatsapp_chat_jid",
        lambda user_scope_id, username: "491761234567@s.whatsapp.net",
    )
    monkeypatch.setattr(
        "vaf.api.whatsapp_bridge.send_whatsapp_with_confirmation",
        lambda username, chat_jid, text, **kwargs: "Message sent via WhatsApp.",
    )

    _install_fake_module(
        monkeypatch,
        "vaf.core.outbound_sanitizer",
        sanitize_outgoing_message=lambda text: text,
    )
    _install_fake_module(
        monkeypatch,
        "vaf.core.user_notifications",
        append_notification=lambda *args, **kwargs: None,
    )

    tool = SendWhatsAppTool()
    result = tool.run(
        message="Kurzes Update",
        username="admin",
        user_scope_id="scope-1",
    )

    assert result == "Message sent via WhatsApp."


def _front_office_stubs(monkeypatch, sent):
    monkeypatch.setattr(
        "vaf.core.messaging_connections.get_whatsapp_chat_jid",
        lambda user_scope_id, username: "491761234567@s.whatsapp.net",
    )
    monkeypatch.setattr(
        "vaf.api.whatsapp_bridge.send_whatsapp_with_confirmation",
        lambda username, chat_jid, text, **kwargs: sent.append(chat_jid) or "Message sent via WhatsApp.",
    )
    _install_fake_module(monkeypatch, "vaf.core.outbound_sanitizer", sanitize_outgoing_message=lambda text: text)
    _install_fake_module(monkeypatch, "vaf.core.user_notifications", append_notification=lambda *args, **kwargs: None)


def test_in_front_office_the_owner_back_channel_goes_through_and_nobody_else_is_reachable(monkeypatch):
    """The reply to the contact is delivered by the runner; the tool's one job in a contact's
    turn is the owner notification (main_messenger = whatsapp). MUTATION: block the send
    without to_phone in Front Office (the old guard) and the first assertion fails; drop the
    recipient check and the refusal below never happens."""
    from types import SimpleNamespace

    from vaf.core.context import tool_result_is_error

    sent = []
    _front_office_stubs(monkeypatch, sent)
    agent = SimpleNamespace(_front_office_mode=True)
    tool = SendWhatsAppTool()
    assert tool.run(message="Bob asks for the invoice", username="admin", user_scope_id="scope-1", _agent=agent) \
        == "Message sent via WhatsApp."
    assert sent == ["491761234567@s.whatsapp.net"], "no to_phone in Front Office is the owner notification"
    # The owner's own number named explicitly is that same send.
    assert tool.run(message="again", to_phone="+491761234567", username="admin", user_scope_id="scope-1", _agent=agent) \
        == "Message sent via WhatsApp."
    assert sent == ["491761234567@s.whatsapp.net"] * 2
    # Anyone else is refused: a contact's turn must not become a messenger for third parties.
    blocked = tool.run(message="hi", to_phone="+491700000099", username="admin", user_scope_id="scope-1", _agent=agent)
    assert blocked.startswith("[TOOL BLOCKED]") and len(sent) == 2
    assert tool_result_is_error(blocked), "the refusal reads as an error, so nothing records it as a question to the owner"
    # Outside Front Office a third party is an ordinary recipient.
    assert tool.run(message="hi", to_phone="+491700000099", username="admin", user_scope_id="scope-1",
                    _agent=SimpleNamespace(_front_office_mode=False)) == "Message sent via WhatsApp."
    assert sent[-1] == "491700000099@s.whatsapp.net"
