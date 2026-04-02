import sys
from types import ModuleType, SimpleNamespace

from vaf.api import telegram_bridge
from vaf.core.config import Config
from vaf.tools.send_telegram import SendTelegramTool


def _install_fake_module(monkeypatch, name: str, **attrs):
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)


def test_send_telegram_uses_direct_fallback_without_bridge_callback(monkeypatch):
    calls = {}

    def fake_direct(chat_id, text, *, voice_lang=None, file_path=None):
        calls["chat_id"] = chat_id
        calls["text"] = text
        calls["voice_lang"] = voice_lang
        calls["file_path"] = file_path
        return True, ""

    monkeypatch.setattr(
        "vaf.core.messaging_connections.get_telegram_chat_id",
        lambda user_scope_id, username: "12345",
    )
    monkeypatch.setattr("vaf.core.telegram_reply.has_telegram_reply_callback", lambda: False)
    monkeypatch.setattr("vaf.api.telegram_bridge.send_telegram_message_direct", fake_direct)

    _install_fake_module(
        monkeypatch,
        "vaf.core.headless_runner",
        _sanitize_outgoing_message=lambda text: text,
    )

    class FakeSession:
        def __init__(self, id, name):
            self.id = id
            self.name = name
            self.messages = []

        def add_message(self, role, content):
            self.messages.append((role, content))

    class FakeSessionManager:
        def load(self, session_id, restore_state=False):
            raise FileNotFoundError

        def save(self, session, sync_state=False):
            return None

    _install_fake_module(
        monkeypatch,
        "vaf.core.session",
        SessionManager=FakeSessionManager,
        Session=FakeSession,
    )
    _install_fake_module(
        monkeypatch,
        "vaf.core.user_notifications",
        append_notification=lambda *args, **kwargs: None,
    )

    tool = SendTelegramTool()
    result = tool.run(
        message="Hallo aus der Automation",
        user_scope_id="scope-1",
        username="admin",
    )

    assert result == "Message sent to the user via Telegram."
    assert calls == {
        "chat_id": "12345",
        "text": "Hallo aus der Automation",
        "voice_lang": None,
        "file_path": None,
    }


def test_send_telegram_message_direct_posts_to_bot_api(monkeypatch):
    monkeypatch.setattr(
        Config,
        "get",
        staticmethod(
            lambda key, default=None: {
                "enabled": True,
                "verified": True,
                "bot_token": "bot-token",
            }
            if key == "telegram_config"
            else default
        ),
    )

    requests_calls = []

    def fake_post(url, json=None, data=None, files=None, timeout=None):
        requests_calls.append(
            {
                "url": url,
                "json": json,
                "data": data,
                "has_files": files is not None,
                "timeout": timeout,
            }
        )
        return SimpleNamespace(ok=True, status_code=200, text="OK")

    monkeypatch.setattr(telegram_bridge.requests, "post", fake_post)
    _install_fake_module(monkeypatch, "vaf.core.log_helper", log_telegram_reply=lambda message: None)

    ok, error = telegram_bridge.send_telegram_message_direct("999", "Testnachricht")

    assert ok is True
    assert error == ""
    assert requests_calls == [
        {
            "url": "https://api.telegram.org/botbot-token/sendMessage",
            "json": {"chat_id": "999", "text": "Testnachricht"},
            "data": None,
            "has_files": False,
            "timeout": 10,
        }
    ]
