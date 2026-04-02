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
    monkeypatch.setattr(wa, "_is_jid_whitelisted", lambda *args, **kwargs: True)

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
        "vaf.core.headless_runner",
        _sanitize_outgoing_message=lambda text: text,
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
