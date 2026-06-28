# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""send_to_main_messenger — the single 'reach the user on their main channel' helper — and the
send_whatsapp_reply delivery-signal contract it relies on.

The helper must return (True, <channel>) ONLY when the message was actually handed to a live channel,
and (False, None) otherwise (no main_messenger, missing recipient id, or the underlying send failed) so
the caller falls back to the Web UI instead of silently swallowing the message. It must never raise.
"""
import pytest

import vaf.core.messaging_connections as mc
import vaf.core.whatsapp_reply as wr


# --- send_to_main_messenger: per-channel resolution ------------------------------------------------

def _conn(messenger):
    return lambda username, user_scope_id: {"main_messenger": messenger, "available": []}


def test_telegram_success(monkeypatch):
    monkeypatch.setattr(mc, "get_messaging_connections", _conn("telegram"))
    monkeypatch.setattr(mc, "get_telegram_chat_id", lambda scope, uname: "chat-1")
    monkeypatch.setattr("vaf.core.telegram_reply.send_telegram_reply", lambda chat_id, text: True)
    assert mc.send_to_main_messenger("u1", "alice", "hi") == (True, "telegram")


def test_telegram_send_failure_falls_back(monkeypatch):
    monkeypatch.setattr(mc, "get_messaging_connections", _conn("telegram"))
    monkeypatch.setattr(mc, "get_telegram_chat_id", lambda scope, uname: "chat-1")
    monkeypatch.setattr("vaf.core.telegram_reply.send_telegram_reply", lambda chat_id, text: False)
    assert mc.send_to_main_messenger("u1", "alice", "hi") == (False, None)


def test_telegram_missing_chat_id(monkeypatch):
    monkeypatch.setattr(mc, "get_messaging_connections", _conn("telegram"))
    monkeypatch.setattr(mc, "get_telegram_chat_id", lambda scope, uname: None)
    assert mc.send_to_main_messenger("u1", "alice", "hi") == (False, None)


def test_whatsapp_success(monkeypatch):
    monkeypatch.setattr(mc, "get_messaging_connections", _conn("whatsapp"))
    monkeypatch.setattr(mc, "get_whatsapp_chat_jid", lambda scope, uname: "jid-1")
    monkeypatch.setattr("vaf.core.whatsapp_reply.send_whatsapp_reply", lambda *a, **k: True)
    assert mc.send_to_main_messenger("u1", "alice", "hi") == (True, "whatsapp")


def test_whatsapp_bridge_down_does_not_report_false_success(monkeypatch):
    # The core of the WhatsApp regression: a dead bridge (send_whatsapp_reply -> False) must degrade to
    # (False, None) so deliver_tracked_message falls back to the Web UI instead of dropping the message.
    monkeypatch.setattr(mc, "get_messaging_connections", _conn("whatsapp"))
    monkeypatch.setattr(mc, "get_whatsapp_chat_jid", lambda scope, uname: "jid-1")
    monkeypatch.setattr("vaf.core.whatsapp_reply.send_whatsapp_reply", lambda *a, **k: False)
    assert mc.send_to_main_messenger("u1", "alice", "hi") == (False, None)


def test_discord_success(monkeypatch):
    monkeypatch.setattr(mc, "get_messaging_connections", _conn("discord"))
    monkeypatch.setattr(mc, "get_discord_user_id", lambda scope, uname: "duser-1")
    monkeypatch.setattr(mc.Config, "get",
                        staticmethod(lambda k, d=None: {"bot_token": "tok"} if k == "discord_config" else d))
    monkeypatch.setattr("vaf.core.discord_send.send_discord_dm", lambda token, uid, text, chunk=True: True)
    assert mc.send_to_main_messenger("u1", "alice", "hi") == (True, "discord")


def test_discord_without_bot_token_falls_back(monkeypatch):
    monkeypatch.setattr(mc, "get_messaging_connections", _conn("discord"))
    monkeypatch.setattr(mc, "get_discord_user_id", lambda scope, uname: "duser-1")
    monkeypatch.setattr(mc.Config, "get", staticmethod(lambda k, d=None: {} if k == "discord_config" else d))
    assert mc.send_to_main_messenger("u1", "alice", "hi") == (False, None)


def test_no_main_messenger(monkeypatch):
    monkeypatch.setattr(mc, "get_messaging_connections", _conn(None))
    assert mc.send_to_main_messenger("u1", "alice", "hi") == (False, None)


def test_empty_text_is_noop():
    assert mc.send_to_main_messenger("u1", "alice", "   ") == (False, None)
    assert mc.send_to_main_messenger("u1", "alice", None) == (False, None)


def test_never_raises(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("resolution exploded")
    monkeypatch.setattr(mc, "get_messaging_connections", boom)
    assert mc.send_to_main_messenger("u1", "alice", "hi") == (False, None)


# --- send_whatsapp_reply / has_whatsapp_reply_callback: delivery-signal contract -------------------

@pytest.fixture(autouse=True)
def _reset_whatsapp_callback():
    """The bridge callback is module-global; isolate every test from bleed."""
    wr.set_whatsapp_reply_callback(None)
    yield
    wr.set_whatsapp_reply_callback(None)


def test_whatsapp_reply_no_callback_returns_false():
    assert wr.send_whatsapp_reply("alice", "jid-1", "hi") is False


def test_whatsapp_reply_legacy_none_callback_assumed_accepted():
    # A legacy callback returns None (didn't raise) -> assumed accepted -> True.
    calls = []
    wr.set_whatsapp_reply_callback(lambda *a: calls.append(a))
    assert wr.send_whatsapp_reply("alice", "jid-1", "hi") is True
    assert len(calls) == 1


def test_whatsapp_reply_propagates_callback_bool():
    # A callback that returns a real enqueue bool is propagated verbatim: a dropped (non-whitelisted)
    # recipient -> False, so send_to_main_messenger can fall back instead of claiming success.
    wr.set_whatsapp_reply_callback(lambda *a, **k: False)
    assert wr.send_whatsapp_reply("alice", "jid-1", "hi") is False
    wr.set_whatsapp_reply_callback(lambda *a, **k: True)
    assert wr.send_whatsapp_reply("alice", "jid-1", "hi") is True


def test_whatsapp_reply_empty_recipient_returns_false():
    wr.set_whatsapp_reply_callback(lambda *a: None)
    assert wr.send_whatsapp_reply("", "jid-1", "hi") is False
    assert wr.send_whatsapp_reply("alice", "", "hi") is False


def test_whatsapp_reply_empty_body_returns_false():
    wr.set_whatsapp_reply_callback(lambda *a: None)
    assert wr.send_whatsapp_reply("alice", "jid-1", "") is False


def test_whatsapp_reply_callback_error_returns_false():
    def boom(*a):
        raise RuntimeError("bridge crashed mid-send")
    wr.set_whatsapp_reply_callback(boom)
    assert wr.send_whatsapp_reply("alice", "jid-1", "hi") is False
