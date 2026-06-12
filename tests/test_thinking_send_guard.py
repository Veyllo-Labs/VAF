"""Tests for the thinking-mode outbound channel guard.

A background thinking run must only contact the user via the configured
main_messenger; without one, all send tools are removed and questions reach
the user as plain text via the Web UI fallback. send_mail is never allowed
(a run once mailed a hallucinated address).
"""
from vaf.core.thinking_mode import _filter_thinking_send_tools


def _registry():
    return {
        "send_mail": object(),
        "send_telegram": object(),
        "send_whatsapp": object(),
        "send_discord": object(),
        "send_slack": object(),
        "web_search": object(),
        "save_thinking_suggestion": object(),
        "thinking_done": object(),
    }


def test_no_main_messenger_removes_all_send_tools():
    tools = _registry()
    removed = _filter_thinking_send_tools(tools, "")
    assert sorted(removed) == [
        "send_discord", "send_mail", "send_slack", "send_telegram", "send_whatsapp",
    ]
    # Non-send tools stay untouched
    assert "web_search" in tools
    assert "save_thinking_suggestion" in tools
    assert "thinking_done" in tools


def test_configured_messenger_keeps_exactly_that_tool():
    tools = _registry()
    removed = _filter_thinking_send_tools(tools, "telegram")
    assert "send_telegram" in tools
    assert "send_mail" not in tools
    assert "send_whatsapp" not in tools
    assert "send_discord" not in tools
    assert "send_slack" not in tools
    assert "send_mail" in removed


def test_send_mail_is_never_kept():
    for messenger in ("", "telegram", "whatsapp", "discord", "slack", "mail", "email"):
        tools = _registry()
        _filter_thinking_send_tools(tools, messenger)
        assert "send_mail" not in tools, f"send_mail survived for messenger={messenger!r}"


def test_filter_handles_missing_tools_gracefully():
    tools = {"web_search": object()}
    removed = _filter_thinking_send_tools(tools, "telegram")
    assert removed == []
    assert "web_search" in tools
